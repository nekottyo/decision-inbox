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
from cue_tui import Desk, clip, decision_url, detail_lines, draw, edit_with_editor, handle_key, http_url, safe_text, width, wrap
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
        self.first = self.store.add(request_payload(title='最初の判断'))
        self.second = self.store.add(request_payload(title='次の判断'))
        self.desk = Desk(self.store, 'http://127.0.0.1:4317/')

    def test_arrow_selection_enter_save_and_no_repeat(self):
        handle_key(self.desk, '\r')
        self.assertIsNone(self.store.get(self.first['id'])['answer'])
        handle_key(self.desk, curses.KEY_DOWN)
        self.assertEqual(self.desk.draft()['choice'], 'minimal')
        handle_key(self.desk, curses.KEY_DOWN)
        self.assertEqual(self.desk.draft()['choice'], 'refactor')
        handle_key(self.desk, curses.KEY_UP)
        handle_key(self.desk, '\r')
        self.assertEqual(self.store.get(self.first['id'])['answer']['choice'], 'minimal')
        self.assertEqual(self.store.get(self.first['id'])['answer']['status'], 'pending')
        self.assertEqual(self.desk.item['id'], self.second['id'])
        self.assertIsNone(self.desk.draft()['choice'])
        handle_key(self.desk, '\r')
        self.assertIsNone(self.store.get(self.second['id'])['answer'])

    def test_refresh_preserves_case_draft_and_scroll_with_new_urgent(self):
        handle_key(self.desk, curses.KEY_DOWN)
        self.desk.draft()['memo'] = '入力中の日本語'
        self.desk.scroll = 12
        urgent = self.store.add(request_payload(title='新着', priority='urgent'))
        self.desk.refresh()
        self.assertEqual(self.desk.visible()[0]['id'], urgent['id'])
        self.assertEqual(self.desk.item['id'], self.first['id'])
        self.assertEqual(self.desk.draft(), {'choice': 'minimal', 'memo': '入力中の日本語'})
        self.assertEqual(self.desk.scroll, 12)

    def test_conflict_and_write_failure_preserve_draft(self):
        handle_key(self.desk, curses.KEY_DOWN)
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
        self.desk.draft()['memo'] = '下書き'
        handle_key(self.desk, curses.KEY_RIGHT)
        self.assertEqual(self.desk.item['id'], self.second['id'])
        handle_key(self.desk, curses.KEY_LEFT)
        self.assertEqual(self.desk.draft()['memo'], '下書き')
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

    def test_editor_uses_argv_and_rejects_oversized_memo(self):
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
            with self.assertRaises(ValueError):
                edit_with_editor('元のメモ')
        self.assertTrue(all(not Path(path).exists() for path in filenames))

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

    def test_pty_arrows_editor_resize_and_terminal_restore(self):
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
            os.write(master, b'\r')
            pump_until(lambda: self.store.get(self.first['id'])['answer'] is not None)
            self.assertEqual(self.store.get(self.first['id'])['answer']['memo'], '端末からのメモ')
            os.write(master, b'\r')
            pump_until(lambda: '候補を選択してから'.encode() in output)
            self.assertIsNone(self.store.get(self.second['id'])['answer'])
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
            self.assertIsNone(self.store.get(self.second['id'])['answer'])
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()


if __name__ == '__main__':
    unittest.main()
