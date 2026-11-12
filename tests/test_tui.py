import curses
import fcntl
import os
from pathlib import Path
import select
import shlex
import signal
import sqlite3
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import Store
from cue_tui import Desk, HEADING_COLORS, clip, decision_url, detail_lines, draw, edit_with_editor, handle_key, heading_styles, http_url, safe_text, width, wrap
from test_app import request_payload

ROOT = Path(__file__).resolve().parents[1]


class Screen:
    def __init__(self, rows, columns):
        self.rows, self.columns, self.output = rows, columns, []

    def erase(self):
        self.output = []

    def getmaxyx(self):
        return self.rows, self.columns

    def addstr(self, row, column, value, attribute):
        assert 0 <= row < self.rows
        assert 0 <= column < self.columns - 1
        assert width(value) <= self.columns - column - 1
        self.output.append(value)

    def noutrefresh(self):
        pass


class TuiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.db = Path(self.directory.name) / 'tui.sqlite3'
        self.store = Store(self.db)
        self.first = self.store.add(request_payload(title='最初の判断', recommended='refactor'))
        self.second = self.store.add(request_payload(title='次の判断'))
        self.desk = Desk(self.store, 'http://127.0.0.1:4317/')

    def test_first_choice_is_selected_without_saving_and_arrows_change_it(self):
        self.assertEqual(self.desk.draft()['choice'], 'minimal')
        self.assertNotEqual(self.desk.draft()['choice'], self.first['recommended'])
        self.assertIsNone(self.store.get(self.first['id'])['answer'])
        lines, _ = detail_lines(self.desk, 100)
        options = [line for line, _ in lines if line.startswith(('1.', '2.'))]
        self.assertTrue(options[0].startswith('1. ●'))
        self.assertNotIn('●', options[1])
        self.assertFalse(self.desk.dirty())
        handle_key(self.desk, curses.KEY_DOWN)
        self.assertEqual(self.desk.draft()['choice'], 'refactor')
        handle_key(self.desk, curses.KEY_UP)
        self.assertEqual(self.desk.draft()['choice'], 'minimal')
        handle_key(self.desk, '\r')
        self.assertEqual(self.store.get(self.first['id'])['answer']['choice'], 'minimal')
        self.assertEqual(self.store.get(self.first['id'])['answer']['status'], 'pending')
        self.assertEqual(self.desk.item['id'], self.second['id'])
        self.assertEqual(self.desk.draft()['choice'], 'minimal')
        self.assertIsNone(self.store.get(self.second['id'])['answer'])
        handle_key(self.desk, '\r')
        self.assertEqual(self.store.get(self.second['id'])['answer']['choice'], 'minimal')

    def test_default_enter_saves_and_only_success_flushes_buffered_input(self):
        with patch('cue_tui.curses.flushinp') as flush:
            handle_key(self.desk, '\r')
            flush.assert_called_once_with()
        with patch('cue_tui.curses.flushinp') as flush, patch.object(self.store, 'answer', side_effect=sqlite3.OperationalError('保存失敗')):
            handle_key(self.desk, '\r')
            flush.assert_not_called()
        self.assertEqual(self.desk.draft()['choice'], 'minimal')
        self.assertIsNone(self.store.get(self.second['id'])['answer'])

    def test_next_case_keeps_an_existing_choice_and_memo_draft(self):
        handle_key(self.desk, curses.KEY_RIGHT)
        handle_key(self.desk, curses.KEY_DOWN)
        self.desk.draft()['memo'] = '次の案件の下書き'
        handle_key(self.desk, curses.KEY_LEFT)
        handle_key(self.desk, '\r')
        self.assertEqual(self.desk.item['id'], self.second['id'])
        self.assertEqual(self.desk.draft(), {'choice': 'refactor', 'memo': '次の案件の下書き'})
        self.assertIsNone(self.store.get(self.second['id'])['answer'])

    def test_answered_view_uses_saved_choice_without_new_draft(self):
        self.store.answer(self.first['id'], {'choice': 'refactor', 'memo': '保存済み'})
        desk = Desk(self.store)
        desk.change_view()
        self.assertEqual(desk.item['id'], self.first['id'])
        self.assertNotIn(self.first['id'], desk.drafts)
        lines, _ = detail_lines(desk, 100)
        options = [line for line, _ in lines if line.startswith(('1.', '2.'))]
        self.assertNotIn('●', options[0])
        self.assertTrue(options[1].startswith('2. ●'))
        self.assertFalse(desk.dirty())

    def test_refresh_preserves_case_draft_and_scroll_with_new_urgent(self):
        self.desk.draft()['memo'] = '入力中の日本語'
        self.desk.scroll = 12
        urgent = self.store.add(request_payload(title='新着', priority='urgent'))
        self.desk.refresh()
        self.assertEqual(self.desk.visible()[0]['id'], urgent['id'])
        self.assertEqual(self.desk.item['id'], self.first['id'])
        self.assertEqual(self.desk.draft(), {'choice': 'minimal', 'memo': '入力中の日本語'})
        self.assertEqual(self.desk.scroll, 12)

    def test_undo_returns_answer_to_pending_and_retains_its_draft(self):
        self.store.answer(self.first['id'], {'choice': 'refactor', 'memo': '回答のメモ'})
        self.desk.change_view()
        self.assertTrue(self.desk.item['answer']['can_undo'])
        handle_key(self.desk, 'u')
        self.assertEqual(self.desk.view, 'pending')
        self.assertEqual(self.desk.item['id'], self.first['id'])
        self.assertIsNone(self.store.get(self.first['id'])['answer'])
        self.assertEqual(self.desk.draft(), {'choice': 'refactor', 'memo': '回答のメモ'})
        self.assertIn('取り消し', self.desk.notice)

    def test_undo_race_preserves_display_and_claimed_answer(self):
        self.store.answer(self.first['id'], {'choice': 'refactor', 'memo': '回答のメモ'})
        self.desk.change_view()
        self.store.claim('別の処理担当')
        self.desk.drafts[self.first['id']] = {'choice': 'minimal', 'memo': '保持する下書き'}
        handle_key(self.desk, 'u')
        self.assertIn('変更できません', self.desk.notice)
        self.assertEqual(self.desk.item['answer']['status'], 'pending')
        self.assertEqual(self.desk.draft()['memo'], '保持する下書き')
        self.assertEqual(self.store.get(self.first['id'])['answer']['status'], 'processing')
        self.desk.refresh()
        handle_key(self.desk, 'u')
        self.assertIn('処理開始後', self.desk.notice)

    def test_stopped_reason_is_first_and_retry_is_explicit(self):
        self.store.answer(self.first['id'], {'choice': 'refactor', 'memo': '回答のメモ'})
        claim = self.store.claim('処理担当')
        self.store.report(self.first['id'], {'token': claim['token'], 'status': 'blocked', 'reason': '承認が必要', 'next_step': '承認後に再処理'})
        self.desk.change_view()
        lines, _ = detail_lines(self.desk, 100)
        text = '\n'.join(line for line, _ in lines)
        self.assertLess(text.index('承認が必要'), text.index('判断が必要な理由'))
        self.assertIn('承認後に再処理', text)
        self.assertIn('t: 再処理', text)
        self.assertEqual(self.store.get(self.first['id'])['answer']['status'], 'blocked')
        handle_key(self.desk, 't')
        self.assertEqual(self.store.get(self.first['id'])['answer']['status'], 'pending')
        self.assertFalse(self.desk.item['answer']['can_undo'])

    def test_stale_retry_preserves_new_report(self):
        self.store.answer(self.first['id'], {'choice': 'minimal'})
        claim = self.store.claim('処理担当')
        self.store.report(self.first['id'], {'token': claim['token'], 'status': 'failed', 'reason': '最初の失敗', 'next_step': '再確認'})
        self.desk.change_view()
        self.store.retry(self.first['id'], {key: self.desk.item['answer'][key] for key in ('decided_at', 'reported_at')})
        claim = self.store.claim('処理担当')
        self.store.report(self.first['id'], {'token': claim['token'], 'status': 'failed', 'reason': '新しい失敗', 'next_step': 'もう一度確認'})
        handle_key(self.desk, 't')
        self.assertIn('変更できません', self.desk.notice)
        self.assertEqual(self.store.get(self.first['id'])['answer']['reason'], '新しい失敗')

    def test_external_undo_keeps_case_and_draft_visible(self):
        saved = self.store.answer(self.first['id'], {'choice': 'minimal'})
        self.desk.change_view()
        self.store.undo(self.first['id'], {'decided_at': saved['answer']['decided_at']})
        self.desk.refresh()
        self.assertEqual(self.desk.view, 'pending')
        self.assertEqual(self.desk.item['id'], self.first['id'])
        self.assertIsNone(self.desk.item['answer'])

    def test_conflict_and_write_failure_preserve_draft(self):
        self.desk.draft()['memo'] = '保存前のメモ'
        with patch.object(self.store, 'answer', side_effect=sqlite3.OperationalError('書き込み失敗')):
            handle_key(self.desk, '\r')
        self.assertEqual(self.desk.item['id'], self.first['id'])
        self.assertEqual(self.desk.draft()['memo'], '保存前のメモ')
        self.assertIsNone(self.store.get(self.first['id'])['answer'])
        self.store.answer(self.first['id'], {'choice': 'refactor', 'memo': '他の画面の回答'})
        handle_key(self.desk, '\r')
        self.assertIn('別の画面', self.desk.notice)
        self.assertEqual(self.desk.draft(), {'choice': 'minimal', 'memo': '保存前のメモ'})
        self.desk.refresh()
        self.assertEqual(self.desk.draft()['memo'], '保存前のメモ')
        self.assertEqual(self.desk.item['answer']['memo'], '他の画面の回答')
        handle_key(self.desk, '\r')
        self.assertEqual(self.store.get(self.first['id'])['answer']['choice'], 'refactor')

    def test_case_navigation_views_and_priority(self):
        handle_key(self.desk, curses.KEY_DOWN)
        self.desk.draft()['memo'] = '下書き'
        handle_key(self.desk, curses.KEY_RIGHT)
        self.assertEqual(self.desk.item['id'], self.second['id'])
        handle_key(self.desk, curses.KEY_LEFT)
        self.assertEqual(self.desk.draft()['memo'], '下書き')
        self.assertEqual(self.desk.draft()['choice'], 'refactor')
        self.store.answer(self.first['id'], {'choice': 'minimal'})
        handle_key(self.desk, '\t')
        self.assertEqual(self.desk.view, 'done')
        self.assertEqual(self.desk.item['id'], self.first['id'])
        handle_key(self.desk, 'f')
        self.assertEqual(self.desk.priority, 'urgent')
        self.assertIsNone(self.desk.item)
        self.assertEqual(self.desk.visible(), [])

    def test_quit_confirmation_uses_only_arrows_and_enter(self):
        handle_key(self.desk, curses.KEY_DOWN)
        self.assertTrue(handle_key(self.desk, 'q'))
        self.assertEqual(self.desk.mode, 'quit')
        self.assertTrue(handle_key(self.desk, '\r'))
        self.assertIsNone(self.desk.mode)
        handle_key(self.desk, 'q')
        handle_key(self.desk, curses.KEY_DOWN)
        self.assertFalse(handle_key(self.desk, '\r'))
        self.assertIsNone(self.store.get(self.first['id'])['answer'])

    def test_escape_exits_every_mode_without_saving(self):
        for mode in (None, 'quit', 'help', 'links'):
            with self.subTest(mode=mode):
                self.desk.mode = mode
                self.desk.draft().update(choice='minimal', memo='未保存のメモ')
                with patch.object(self.store, 'answer') as answer, patch('cue_tui.webbrowser.open') as opened:
                    self.assertFalse(handle_key(self.desk, '\x1b'))
                    answer.assert_not_called()
                    opened.assert_not_called()
                self.assertIsNone(self.store.get(self.first['id'])['answer'])

    def test_terminal_controls_and_japanese_width(self):
        malicious = '日本語\x1b]52;c;hidden\x07\r\u009b\u202eabc'
        escaped = safe_text(malicious)
        self.assertNotIn('\x1b', escaped)
        self.assertNotIn('\x07', escaped)
        self.assertNotIn('\r', escaped)
        self.assertNotIn('\u009b', escaped)
        self.assertNotIn('\u202e', escaped)
        self.assertEqual(width('日本a\u0301'), 5)
        self.assertEqual(clip('日本abc', 5), '日本a')
        self.assertTrue(all(width(line) <= 6 for line in wrap('日本語abc\na\u0301日', 6)))
        self.desk.item = {**self.desk.item, 'title': malicious}
        for rows, columns in [(24, 110), (13, 40), (4, 20)]:
            screen = Screen(rows, columns)
            with patch('cue_tui.curses.doupdate'):
                draw(screen, self.desk)
            self.assertNotIn('\x1b', ''.join(screen.output))
        handle_key(self.desk, curses.KEY_DOWN)
        handle_key(self.desk, 'q')
        screen = Screen(4, 20)
        with patch('cue_tui.curses.doupdate'):
            draw(screen, self.desk)
        self.assertIn('戻る', ''.join(screen.output))

    def test_heading_colors_keep_body_normal_and_fall_back_to_bold(self):
        with patch('cue_tui.curses.has_colors', return_value=True), patch.object(curses, 'COLORS', 256, create=True), patch.object(curses, 'COLOR_PAIRS', 256, create=True), patch('cue_tui.curses.use_default_colors'), patch('cue_tui.curses.init_pair') as initialize, patch('cue_tui.curses.color_pair', side_effect=lambda pair: pair << 8):
            styles = heading_styles()
            self.assertEqual(initialize.call_count, 4)
        self.store.answer(self.first['id'], {'choice': 'minimal'})
        claim = self.store.claim('見出し検証')
        self.store.report(self.first['id'], {'token': claim['token'], 'status': 'blocked', 'reason': '承認が必要', 'next_step': '承認後に再処理'})
        self.desk.change_view()
        self.desk.heading_styles = styles
        lines, _ = detail_lines(self.desk, 100)
        attributes = dict(lines)
        for label, kind in [('判断が必要な理由', 'context'), ('依頼元の推奨', 'recommendation'), ('候補 (↑↓で選択、Enter で確定)', 'options'), ('止まった理由', 'stopped'), ('再開に必要なこと', 'stopped')]:
            self.assertEqual(attributes[label], styles[kind])
        for body in ['承認が必要', '承認後に再処理', self.first['reason'], self.first['recommendation']]:
            self.assertEqual(attributes[body], 0)
        for colors, supported in [(0, False), (8, True)]:
            with patch('cue_tui.curses.has_colors', return_value=supported), patch.object(curses, 'COLORS', colors, create=True), patch('cue_tui.curses.init_pair') as initialize:
                self.assertTrue(all(attribute == curses.A_BOLD for attribute in heading_styles().values()))
                initialize.assert_not_called()
        with patch('cue_tui.curses.has_colors', side_effect=curses.error('色なし')):
            self.assertTrue(all(attribute == curses.A_BOLD for attribute in heading_styles().values()))

    def test_pty_soft_headings_and_monochrome_exit_without_saving(self):
        self.store.answer(self.first['id'], {'choice': 'minimal'})
        claim = self.store.claim('端末色の検証')
        self.store.report(self.first['id'], {'token': claim['token'], 'status': 'blocked', 'reason': '承認が必要', 'next_step': '承認後に再処理'})
        for terminal_name in ['xterm-256color', 'xterm', 'vt100']:
            with self.subTest(terminal=terminal_name):
                master, slave = os.openpty()
                fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 80, 120, 0, 0))
                process = subprocess.Popen([sys.executable, str(ROOT / 'app.py'), '--db', str(self.db), 'tui'], stdin=slave, stdout=slave, stderr=slave, env={**os.environ, 'TERM': terminal_name})
                output = bytearray()

                def pump(condition):
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        if select.select([master], [], [], .02)[0]:
                            output.extend(os.read(master, 65536))
                        if condition():
                            return
                    self.fail(f'色の端末検証が完了しませんでした: {bytes(output)[-1000:]}')

                try:
                    pump(lambda: 'Enter 確定'.encode() in output)
                    os.write(master, b'\t')
                    pump(lambda: '再開に必要なこと'.encode() in output)
                    if terminal_name == 'xterm-256color':
                        for color in HEADING_COLORS.values():
                            self.assertIn(f'38;5;{color}m'.encode(), output)
                    else:
                        self.assertNotIn(b'38;5;', output)
                    os.write(master, b'\x1b')
                    pump(lambda: process.poll() is not None)
                    self.assertEqual(process.returncode, 0)
                    self.assertIsNone(self.store.get(self.second['id'])['answer'])
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait()
                    os.close(master)
                    os.close(slave)

    def test_editor_uses_argv_and_preserves_oversized_memo(self):
        filenames = []

        def write_memo(arguments, check):
            filenames.append(arguments[-1])
            self.assertEqual(arguments[:2], ['fake-editor', '--flag'])
            Path(arguments[-1]).write_text('日本語のメモ', encoding='utf-8')
            return subprocess.CompletedProcess(arguments, 0)

        with patch.dict(os.environ, {'VISUAL': '', 'EDITOR': 'fake-editor --flag'}), patch('cue_tui.subprocess.run', side_effect=write_memo):
            self.assertEqual(edit_with_editor('元のメモ'), '日本語のメモ')
        self.assertTrue(all(not Path(path).exists() for path in filenames))
        self.desk.draft()['memo'] = '元のメモ'
        handle_key(self.desk, 'm', edit=lambda value: (_ for _ in ()).throw(ValueError('編集失敗')))
        self.assertEqual(self.desk.draft()['memo'], '元のメモ')

        def too_long(arguments, check):
            filenames.append(arguments[-1])
            Path(arguments[-1]).write_text('𠮷' * 2001, encoding='utf-8')
            return subprocess.CompletedProcess(arguments, 0)

        with patch.dict(os.environ, {'VISUAL': '', 'EDITOR': 'fake-editor'}), patch('cue_tui.subprocess.run', side_effect=too_long):
            self.assertEqual(edit_with_editor('元のメモ'), '𠮷' * 2001)
        self.assertTrue(all(not Path(path).exists() for path in filenames))
        handle_key(self.desk, curses.KEY_DOWN)
        handle_key(self.desk, 'm', edit=lambda value: '𠮷' * 2001)
        handle_key(self.desk, '\r')
        self.assertIsNone(self.store.get(self.first['id'])['answer'])
        self.assertEqual(self.desk.draft()['memo'], '𠮷' * 2001)

    def test_link_picker_scrolls_to_selected_link(self):
        self.desk.item = {**self.desk.item, 'links': [{'label': f'関連{index}', 'url': f'https://example.com/{index}'} for index in range(20)]}
        self.desk.mode, self.desk.menu_index = 'links', 19
        screen = Screen(12, 50)
        with patch('cue_tui.curses.doupdate'):
            draw(screen, self.desk)
        self.assertIn('関連19', screen.output)
        self.assertNotIn('関連0', screen.output)

    def test_failed_initial_read_is_not_an_empty_queue(self):
        with patch.object(self.store, 'list', side_effect=sqlite3.OperationalError('取得失敗')):
            desk = Desk(self.store)
        lines, _ = detail_lines(desk, 60)
        self.assertIn('読み込めません', lines[0][0])
        self.assertIn('取得失敗', desk.load_error)

    def test_web_and_related_links_open_only_after_explicit_keys(self):
        for target in ['file:///tmp/secret', 'javascript:alert(1)', 'https://a.test/\x1b', 'https://user:password@a.test/']:
            self.assertIsNone(http_url(target))
        target = decision_url('http://127.0.0.1:4317/?x=1&decision=old', self.first['id'])
        self.assertIn(f'decision={self.first["id"]}', target)
        self.assertNotIn('decision=old', target)
        with patch('cue_tui.webbrowser.open', return_value=True) as opened:
            self.desk.refresh()
            opened.assert_not_called()
            handle_key(self.desk, 'w')
            self.assertIn(self.first['id'], opened.call_args.args[0])
            handle_key(self.desk, 'o')
            self.assertEqual(opened.call_count, 1)
            handle_key(self.desk, '\r')
            self.assertEqual(opened.call_count, 2)
        self.assertIsNone(self.store.get(self.first['id'])['answer'])

    def test_pty_single_escape_closes_promptly_from_every_mode(self):
        for mode, keys, expected in [(None, b'', 'Enter で選択'), ('quit', b'q', '未保存の候補'), ('help', b'?', 'Cue の操作'), ('links', b'o', '関連リンク・ファイルを開く')]:
            with self.subTest(mode=mode):
                master, slave = os.openpty()
                fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 24, 100, 0, 0))
                before = termios.tcgetattr(slave)
                process = subprocess.Popen([sys.executable, str(ROOT / 'app.py'), '--db', str(self.db), 'tui'], stdin=slave, stdout=slave, stderr=slave, env={**os.environ, 'TERM': 'xterm-256color'})
                output = bytearray()

                def pump_until(condition, timeout=8):
                    deadline = time.monotonic() + timeout
                    while time.monotonic() < deadline:
                        if select.select([master], [], [], 0.02)[0]:
                            try:
                                output.extend(os.read(master, 65536))
                            except OSError:
                                break
                        if condition():
                            return
                        if process.poll() is not None:
                            break
                    self.fail(f'Esc の端末操作が完了しませんでした: {bytes(output).decode(errors="replace")[-1000:]}')

                try:
                    pump_until(lambda: b'Cue' in output)
                    os.write(master, b'\x1b')
                    time.sleep(0.03)
                    os.write(master, b'OB' + keys)
                    pump_until(lambda: expected.encode() in output)
                    started = time.monotonic()
                    os.write(master, b'\x1b')
                    pump_until(lambda: process.poll() is not None, 0.6)
                    self.assertLess(time.monotonic() - started, 0.6)
                    self.assertEqual(process.returncode, 0)
                    after = termios.tcgetattr(slave)
                    after[3] &= ~getattr(termios, 'PENDIN', 0)
                    before[3] &= ~getattr(termios, 'PENDIN', 0)
                    self.assertEqual(after, before)
                    self.assertIsNone(self.store.get(self.first['id'])['answer'])
                    self.assertIsNone(self.store.get(self.second['id'])['answer'])
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait()
                    os.close(master)
                    os.close(slave)

    def test_pty_arrows_editor_resize_and_terminal_restore(self):
        third = self.store.add(request_payload(title='最後の判断'))
        editor = Path(self.directory.name) / 'editor.py'
        editor.write_text("import pathlib,sys\npathlib.Path(sys.argv[1]).write_text('端末からのメモ', encoding='utf-8')\n")
        master, slave = os.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 24, 100, 0, 0))
        before = termios.tcgetattr(slave)
        environment = {**os.environ, 'TERM': 'xterm-256color', 'VISUAL': '', 'EDITOR': f'{shlex.quote(sys.executable)} {shlex.quote(str(editor))}'}
        process = subprocess.Popen([sys.executable, str(ROOT / 'app.py'), '--db', str(self.db), 'tui'], stdin=slave, stdout=slave, stderr=slave, env=environment)
        output = bytearray()

        def pump_until(condition, timeout=8):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if select.select([master], [], [], 0.05)[0]:
                    try:
                        output.extend(os.read(master, 65536))
                    except OSError:
                        break
                if condition():
                    return
                if process.poll() is not None:
                    break
            self.fail(f'端末操作が完了しませんでした: {bytes(output).decode(errors="replace")[-1600:]}')

        try:
            pump_until(lambda: b'Cue' in output)
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 14, 44, 0, 0))
            os.kill(process.pid, signal.SIGWINCH)
            os.write(master, b'\x1bOBm')
            pump_until(lambda: 'メモを下書きに反映'.encode() in output)
            os.write(master, b'\r\r')
            pump_until(lambda: self.store.get(self.first['id'])['answer'] is not None)
            self.assertEqual(self.store.get(self.first['id'])['answer']['memo'], '端末からのメモ')
            pump_until(lambda: '回答を保存しました'.encode() in output)
            settled = time.monotonic()
            pump_until(lambda: time.monotonic() - settled >= 0.2)
            self.assertIsNone(self.store.get(self.second['id'])['answer'])
            os.write(master, b'\r')
            pump_until(lambda: self.store.get(self.second['id'])['answer'] is not None)
            self.assertEqual(self.store.get(self.second['id'])['answer']['choice'], 'minimal')
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 26, 110, 0, 0))
            os.kill(process.pid, signal.SIGWINCH)
            os.write(master, b'\x1bOBq')
            pump_until(lambda: '未保存の候補'.encode() in output)
            os.write(master, b'\x1bOB\r')
            pump_until(lambda: process.poll() is not None)
            self.assertEqual(process.returncode, 0)
            after = termios.tcgetattr(slave)
            # PENDIN は kernel の入力再表示状態なので、復帰する設定値から除外する
            after[3] &= ~getattr(termios, 'PENDIN', 0)
            before[3] &= ~getattr(termios, 'PENDIN', 0)
            self.assertEqual(after, before)
            self.assertIsNone(self.store.get(third['id'])['answer'])
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()


if __name__ == '__main__':
    unittest.main()
