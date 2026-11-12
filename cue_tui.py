"""Cue の端末表示。保存と検証は注入された Store に任せる。"""
import curses
import json
import locale
import os
from datetime import datetime
from pathlib import Path
import shlex
import socket
import subprocess
import tempfile
import time
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import webbrowser

from app import LANES, decision_text

PRIORITIES = {'urgent': 'すぐ判断', 'normal': '通常', 'later': 'あとでよい'}
STATUSES = {'pending': '処理待ち', 'processing': '対応中', 'completed': '完了', 'blocked': '承認・条件待ち', 'failed': '失敗'}
FILTERS = ('all', 'urgent', 'normal', 'later')
HEADING_COLORS = {'context': 117, 'options': 183, 'recommendation': 151, 'stopped': 221}
BIDI = set('\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069')


def heading_styles():
    attributes = {name: curses.A_BOLD for name in HEADING_COLORS}
    try:
        if not curses.has_colors() or curses.COLORS < 256 or curses.COLOR_PAIRS <= len(HEADING_COLORS):
            return attributes
        curses.use_default_colors()
        for pair, (name, color) in enumerate(HEADING_COLORS.items(), 1):
            curses.init_pair(pair, color, -1)
            attributes[name] |= curses.color_pair(pair)
    except curses.error:
        return {name: curses.A_BOLD for name in HEADING_COLORS}
    return attributes


def safe_text(value):
    result = []
    for char in str(value):
        category = unicodedata.category(char)
        if char in BIDI or category == 'Cs':
            result.append(f'[U+{ord(char):04X}]')
        elif category == 'Cc' and char not in '\n\t':
            result.append(f'\\x{ord(char):02x}')
        else:
            result.append(char)
    return ''.join(result).expandtabs(4)


def cell_width(char):
    if unicodedata.combining(char) or unicodedata.category(char) in ('Mn', 'Me', 'Cf'):
        return 0
    return 2 if unicodedata.east_asian_width(char) in ('F', 'W') else 1


def width(text):
    return sum(cell_width(char) for char in text)


def clip(text, limit):
    result, used = [], 0
    for char in safe_text(text).replace('\n', ' '):
        size = cell_width(char)
        if used + size > max(0, limit):
            break
        result.append(char)
        used += size
    return ''.join(result)


def wrap(text, columns):
    lines = []
    columns = max(1, columns)
    for paragraph in safe_text(text).split('\n'):
        line, used = [], 0
        for char in paragraph:
            size = cell_width(char)
            if line and used + size > columns:
                lines.append(''.join(line))
                line, used = [], 0
            if size <= columns:
                line.append(char)
                used += size
        lines.append(''.join(line))
    return lines


def http_url(value):
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
            return None
        if any(ord(char) <= 32 or ord(char) == 127 for char in value) or '\\' in value:
            return None
        parsed.port
        return value
    except (ValueError, TypeError):
        return None


def decision_url(base, identifier):
    if not base or not http_url(base):
        return None
    parsed = urlsplit(base)
    query = [(key, value) for key, value in parse_qsl(parsed.query) if key != 'decision']
    query.append(('decision', identifier))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path or '/', urlencode(query), parsed.fragment))


def edit_with_editor(memo):
    command = shlex.split(os.environ.get('VISUAL') or os.environ.get('EDITOR') or 'vi')
    if not command:
        raise ValueError('メモ用の editor が設定されていません')
    descriptor, filename = tempfile.mkstemp(prefix='cue-memo-', suffix='.txt')
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
            output.write(memo)
        result = subprocess.run([*command, filename], check=False)
        if result.returncode:
            raise ValueError('メモの編集を中止しました。元の入力を保持しています')
        updated = Path(filename).read_text(encoding='utf-8')
        if '\x00' in updated or any(0xD800 <= ord(char) <= 0xDFFF for char in updated):
            raise ValueError('メモに保存できない文字があります。元の入力を保持しています')
        return updated
    finally:
        Path(filename).unlink(missing_ok=True)


HERDR_DIR = Path.home() / '.config' / 'herdr'
EXTERNAL_TIMEOUT = 2


def herdr_sessions():
    """herdr の session 名と socket の組。default は herdr.sock 直下"""
    found = [('default', HERDR_DIR / 'herdr.sock')]
    found += [(path.parent.name, path) for path in sorted((HERDR_DIR / 'sessions').glob('*/herdr.sock'))]
    return [(name, path) for name, path in found if path.exists()]


def herdr_call(socket_path, method, params):
    """1 行の JSON を送り、1 行の JSON を受ける。接続できなければ OSError"""
    request = json.dumps({'id': 'cue', 'method': method, 'params': params}) + '\n'
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(EXTERNAL_TIMEOUT)
        connection.connect(str(socket_path))
        connection.sendall(request.encode())
        data = b''
        while not data.endswith(b'\n'):
            chunk = connection.recv(65536)
            if not chunk:
                break
            data += chunk
    return json.loads(data)


def run_text(argv):
    return subprocess.run(argv, check=True, capture_output=True, text=True, timeout=EXTERNAL_TIMEOUT).stdout


def herdr_session_of(args):
    """herdr client の引数から session 名を返す。client でなければ None"""
    try:
        words = shlex.split(args)
    except ValueError:
        words = args.split()
    if not words or os.path.basename(words[0]) != 'herdr':
        return None
    rest, session, index = words[1:], 'default', 0
    while index < len(rest):
        if rest[index] == '--session' and index + 1 < len(rest):
            session, index = rest[index + 1], index + 2
        elif rest[index].startswith('--session='):
            session, index = rest[index].split('=', 1)[1], index + 1
        elif rest[index:index + 2] == ['session', 'attach'] and index + 2 < len(rest):
            session, index = rest[index + 2], index + 3
        else:
            # pane list などの CLI 呼び出しは client ではない
            return None
    return session


def tmux_clients(run=run_text):
    """herdr session 名 -> その client が動く tmux の pane_id。tmux が使えなければ None"""
    try:
        panes = run(['tmux', 'list-panes', '-a', '-F', '#{pane_id}\t#{pane_pid}'])
        table = run(['ps', '-A', '-o', 'pid=,ppid=,args='])
    except (OSError, subprocess.SubprocessError):
        return None
    commands, children = {}, {}
    for line in table.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            commands[parts[0]] = parts[2]
            children.setdefault(parts[1], []).append(parts[0])
    found = {}
    for line in panes.splitlines():
        tmux_pane, _, pid = line.partition('\t')
        # 子孫全体は見ない。pane 内で走る herdr pane list を拾うため
        for candidate in [pid, *children.get(pid, [])]:
            session = herdr_session_of(commands.get(candidate, ''))
            if session is not None:
                found.setdefault(session, tmux_pane)
    return found


def focus_pane(pane, sessions=herdr_sessions, call=herdr_call, clients=None, run=run_text, own_socket=None):
    """担当 pane へ herdr の focus で移る。(成否, 画面に出す 1 行) を返す"""
    pane = (pane or '').strip()
    if not pane:
        return False, 'この task には担当 pane がありません'
    reached, hits = 0, []
    for name, path in sessions():
        try:
            response = call(path, 'pane.get', {'pane_id': pane})
        except (OSError, ValueError):
            continue
        reached += 1
        if 'result' in response:
            hits.append((name, path))
    if not reached:
        return False, 'herdr に接続できないため移れません'
    if not hits:
        return False, f'担当 pane {pane} は herdr にもうありません'
    if len(hits) > 1:
        return False, f"担当 pane {pane} が複数の session ({', '.join(name for name, _ in hits)}) にあるため移れません"
    session, path = hits[0]
    if own_socket is None:
        own_socket = os.environ.get('HERDR_SOCKET_PATH', '')
    try:
        same = bool(own_socket) and Path(own_socket).resolve() == Path(path).resolve()
    except OSError:
        same = False
    tmux_pane, note = None, ''
    if not same:
        mapping = clients() if clients else tmux_clients(run)
        if mapping is None:
            note = NO_TMUX_NOTE
        elif session not in mapping:
            return False, f'{session} session を開いている端末が無いため移れません'
        else:
            tmux_pane = mapping[session]
    try:
        response = call(path, 'pane.focus', {'pane_id': pane})
    except (OSError, ValueError) as error:
        return False, f'{pane} へ移れませんでした: {error}'
    if 'error' in response:
        return False, f"{pane} へ移れませんでした: {response['error'].get('message', '')}"
    if tmux_pane:
        try:
            run(['tmux', 'select-window', '-t', tmux_pane])
            run(['tmux', 'select-pane', '-t', tmux_pane])
        except (OSError, subprocess.SubprocessError):
            return False, 'herdr の focus は移しましたが、端末の window を切り替えられませんでした'
    if note:
        return True, f'{pane} ({session}) を focus しました{note}'
    return True, f'{pane} ({session}) へ移りました'


# 端末を切り替えられなかった成功の注記。popup を閉じると読めないので go_task が見分ける
NO_TMUX_NOTE = '。tmux が無いため端末は切り替えていません'


class Desk:
    def __init__(self, store, web_url=None, focus=focus_pane, popup=False):
        self.store, self.web_url = store, web_url
        self.focus, self.popup = focus, popup
        self.items, self.item, self.drafts = [], None, {}
        self.view, self.priority, self.scroll = 'pending', 'all', 0
        self.notice, self.load_error = '', ''
        self.mode, self.menu_index = None, 0
        self.jump_choice = False
        self.tasks, self.task_scroll, self.task_error = [], 0, ''
        self.task_index, self.task_id, self.task_message = 0, None, ''
        self.heading_styles = {}
        self.refresh()

    def visible(self):
        items = [item for item in self.items if (item['answer'] is None) == (self.view == 'pending') and (self.priority == 'all' or item['priority'] == self.priority)]
        return sorted(items, key=lambda item: (list(PRIORITIES).index(item['priority']), item['created_at'], item['id']))

    def refresh(self):
        # task の失敗で判断の表示を止めない
        try:
            self.tasks, self.task_error = self.store.tasks(), ''
            self.sync_task()
        except Exception as error:
            self.task_error = f'task を読み込めません: {error}。r で再試行できます'
        try:
            fresh = self.store.list()
        except Exception as error:
            self.load_error = f'更新できません: {error}。r で再試行できます'
            return
        old_ids = {item['id'] for item in self.items}
        self.items, self.load_error = fresh, ''
        if self.item is not None:
            previous_answer = self.item['answer']
            current = next((item for item in fresh if item['id'] == self.item['id']), None)
            if current is not None:
                self.item = current
                if previous_answer is None and current['answer'] is not None:
                    self.notice = '別の画面で判断済みです。下書きは保持しています。←→で案件を移動できます'
                elif previous_answer is not None and current['answer'] is None:
                    self.view = 'pending'
                    self.notice = '回答が取り消され、判断待ちへ戻りました。下書きは保持しています'
        elif self.visible():
            self.select(self.visible()[0])
        if old_ids and {item['id'] for item in fresh} - old_ids and not self.notice:
            self.notice = '新しい判断が届きました。閲覧中の案件と下書きは保持しています'

    def ordered_tasks(self):
        # task 画面の表示順 (lane 順、lane 内は一覧順)
        return [task for key in LANES for task in self.tasks if task['lane'] == key]

    def sync_task(self):
        # 一覧が変わっても同じ task id を保ち、消えたら範囲内に丸める
        ids = [task['id'] for task in self.ordered_tasks()]
        if self.task_id in ids:
            self.task_index = ids.index(self.task_id)
        else:
            self.task_index = max(0, min(self.task_index, len(ids) - 1))
        self.task_id = ids[self.task_index] if ids else None

    def move_task(self, direction):
        self.task_message = ''
        count = len(self.ordered_tasks())
        self.task_index = max(0, min(self.task_index + direction, count - 1))
        self.task_id = self.ordered_tasks()[self.task_index]['id'] if count else None

    def go_task(self):
        """選択中 task の担当 pane へ移る。TUI を終えるべきなら True"""
        tasks = self.ordered_tasks()
        if not tasks:
            return False
        if self.popup and self.dirty():
            self.task_message = '未保存の入力があるため移れません。判断の画面で保存するか破棄してください'
            return False
        ok, self.task_message = self.focus(tasks[self.task_index]['pane'])
        return ok and self.popup and not self.task_message.endswith(NO_TMUX_NOTE)

    def select(self, item):
        self.item, self.scroll = item, 0
        if item['answer'] is None:
            self.drafts.setdefault(item['id'], {'choice': item['options'][0]['id'], 'memo': ''})

    def draft(self):
        if not self.item:
            return {'choice': None, 'memo': ''}
        if self.item['answer'] is not None:
            return self.drafts.get(self.item['id'], {'choice': None, 'memo': ''})
        return self.drafts.setdefault(self.item['id'], {'choice': self.item['options'][0]['id'], 'memo': ''})

    def move_case(self, direction):
        items = self.visible()
        if not items:
            self.item, self.scroll = None, 0
            return
        current = next((index for index, item in enumerate(items) if self.item and item['id'] == self.item['id']), None)
        self.select(items[(current + direction) % len(items)] if current is not None else items[0 if direction > 0 else -1])

    def change_view(self):
        self.refresh()
        self.view = 'done' if self.view == 'pending' else 'pending'
        items = self.visible()
        self.item, self.scroll = None, 0
        if items:
            self.select(items[0])

    def change_filter(self):
        self.refresh()
        self.priority = FILTERS[(FILTERS.index(self.priority) + 1) % len(FILTERS)]
        items = self.visible()
        if not self.item or all(item['id'] != self.item['id'] for item in items):
            self.item, self.scroll = None, 0
            if items:
                self.select(items[0])

    def choose(self, number=None, direction=0):
        if not self.item or self.item['answer'] is not None:
            self.notice = '未判断の案件で候補を選択してください'
            return
        options = self.item['options']
        if number is None:
            index = next((index for index, option in enumerate(options) if option['id'] == self.draft()['choice']), None)
            number = (index + direction) % len(options) if index is not None else (0 if direction > 0 else len(options) - 1)
        if 0 <= number < len(options):
            self.draft()['choice'] = options[number]['id']
            self.jump_choice, self.notice = True, 'Enter で選択した回答を確定します'

    def save(self):
        if not self.item or self.item['answer'] is not None:
            self.notice = 'この案件は回答できません。←→で未判断の案件へ移動してください'
            return
        draft = self.draft()
        if not draft['choice']:
            self.notice = '↑↓で候補を選択してから Enter を押してください'
            return
        if len(draft['memo']) > 2000:
            self.notice = 'メモは 2,000 文字までです。入力は保持しています'
            return
        identifier = self.item['id']
        try:
            saved = self.store.answer(identifier, dict(draft))
        except Exception as error:
            self.notice = f'保存できません: {error}。入力は保持しています'
            return
        self.drafts.pop(identifier, None)
        self.item = saved
        self.refresh()
        items = self.visible()
        self.item, self.scroll = None, 0
        if items:
            self.select(items[0])
        self.notice = '回答を保存しました。依頼元の処理を待っています'
        return True

    def dirty(self):
        defaults = {item['id']: item['options'][0]['id'] for item in self.items}
        return any(draft['memo'] or draft['choice'] not in (None, defaults.get(identifier)) for identifier, draft in self.drafts.items())

    def revise(self, action):
        answer = self.item['answer'] if self.item else None
        if not answer:
            self.notice = '判断済みの案件で操作してください'
            return
        if action == 'undo' and not answer.get('can_undo'):
            self.notice = '処理開始後の回答は取り消せません'
            return
        if action == 'retry' and answer['status'] not in ('blocked', 'failed'):
            self.notice = '承認・条件待ち、または失敗した案件だけ再処理を依頼できます'
            return
        payload = {'decided_at': answer['decided_at']}
        if action == 'retry':
            payload['reported_at'] = answer['reported_at']
        try:
            updated = getattr(self.store, action)(self.item['id'], payload)
        except Exception as error:
            self.notice = f'変更できません: {error}。表示と下書きは保持しています'
            return
        if action == 'undo':
            self.drafts[updated['id']] = {'choice': answer['choice'], 'memo': answer['memo']}
            self.view = 'pending'
        self.item = updated
        self.refresh()
        self.scroll = 0
        self.notice = '回答を取り消し、判断待ちへ戻しました。候補とメモは下書きに残しています' if action == 'undo' else '再処理を依頼しました。依頼元の処理を待っています'

    def links(self):
        if not self.item:
            return []
        return [(link['label'], link['url']) for link in self.item.get('links', []) if http_url(link['url'])]

    def open_web(self):
        target = decision_url(self.web_url, self.item['id']) if self.item else None
        if not target:
            self.notice = 'Web を開くには起動時に --web-url を指定してください'
            return
        self.open_url(target)

    def open_url(self, target):
        if not http_url(target):
            self.notice = 'http(s) のリンクだけを開けます'
            return
        try:
            opened = webbrowser.open(target)
            self.notice = 'ブラウザで開きました' if opened else 'ブラウザを開けませんでした'
        except Exception as error:
            self.notice = f'ブラウザを開けません: {error}'


def detail_lines(desk, columns):
    lines, choice_positions = [], {}
    item = desk.item
    if not item:
        message = '判断を読み込めません。r で再試行してください' if desk.load_error else ('判断待ちの案件はありません' if desk.view == 'pending' else '判断済みの案件はありません')
        return [(message, curses.A_BOLD)], choice_positions

    def append(value='', attribute=0):
        lines.extend((line, attribute) for line in wrap(value, columns))

    def heading(value, kind):
        append(value, desk.heading_styles.get(kind, curses.A_BOLD))

    append(item['title'], curses.A_BOLD)
    status = STATUSES.get(item['answer']['status'], '判断済み') if item['answer'] else '判断待ち'
    append(f"[{PRIORITIES[item['priority']]}] {status}")
    if item['answer']:
        answer = item['answer']
        if answer.get('reason'):
            heading('止まった理由', 'stopped')
            append(answer['reason'])
        if answer.get('next_step'):
            heading('再開に必要なこと', 'stopped')
            append(answer['next_step'])
        if answer.get('can_undo'):
            append('u: 回答を取り消す (処理開始前のみ)')
        elif answer['status'] in ('blocked', 'failed'):
            append('t: 再処理を依頼する / 自動では再試行しません')
        else:
            append('処理開始後の回答は取り消せません')
    append(f"依頼元: {item['source']}  受付: {item['created_at']}")
    if item.get('deadline'):
        append(f"期限: {item['deadline']}")
    if item.get('urgency_reason'):
        append(item['urgency_reason'])
    for label, key in [('判断が必要な理由', 'reason'), ('背景', 'background')]:
        if item.get(key):
            append()
            heading(label, 'context')
            append(item[key])
    if item.get('image'):
        append()
        append(f"図・画像: {item['image']['alt']}")
        append('画像は w で対象案件の Web 画面から確認できます')
    if desk.links():
        append()
        append('関連する PR・issue (o で開く)', curses.A_BOLD)
        for label, target in desk.links():
            append(f'{label}: {target}')
    append()
    heading('依頼元の推奨', 'recommendation')
    append(item['recommendation'])
    append()
    heading('候補 (↑↓で選択、Enter で確定)', 'options')
    choice = desk.draft()['choice'] if item['answer'] is None else item['answer']['choice']
    for index, option in enumerate(item['options']):
        choice_positions[option['id']] = len(lines)
        marker = '●' if choice == option['id'] else ' '
        recommended = ' [推奨]' if item['recommended'] == option['id'] else ''
        append(f"{index + 1}. {marker} {option['title']}{recommended}", curses.A_BOLD if choice == option['id'] else 0)
        if option['benefits']:
            append(f"   利点: {option['benefits']}")
        if option['concerns']:
            append(f"   懸念: {option['concerns']}")
        append()
    append('判断のメモ (m で編集)', curses.A_BOLD)
    append(item['answer']['memo'] if item['answer'] else desk.draft()['memo'] or '未入力')
    if item['answer']:
        append(f"判断日時: {item['answer']['decided_at']}")
        if desk.drafts.get(item['id'], {}).get('memo'):
            append('別画面の回答前に入力したメモ (未保存)', curses.A_BOLD)
            append(desk.drafts[item['id']]['memo'])
    return lines, choice_positions


def task_lines(desk, columns):
    counts = {key: sum(task['lane'] == key for task in desk.tasks) for key in LANES}
    human = ('human_decision', 'human_action')
    # 人が動く lane を先に数える。並びの残りは LANES の順
    order = sorted(LANES, key=lambda key: key not in human)
    # 色は判断画面に合わせる (人が止めている = 止まった理由の色、回答済み = 推奨の色)。
    # 色を出せない端末でも記号と文字で区別できるよう、色は補助にとどめる
    styles = desk.heading_styles
    waiting = styles.get('stopped', curses.A_BOLD)
    answered = styles.get('recommendation', 0) & ~curses.A_BOLD
    lines, selected = [(' / '.join(f'{LANES[key]} {counts[key]}' for key in order), curses.A_BOLD)], None
    for key, label in LANES.items():
        lines.append((f"{'▲' if key in human else '■'} {label} ({counts[key]})", waiting if key in human else curses.A_BOLD))
        for task in desk.tasks:
            if task['lane'] != key:
                continue
            start = len(lines)
            lines.append((f"{'>' if task['id'] == desk.task_id else ' '} {task['title']}", 0))
            updated = datetime.fromisoformat(task['updated_at']).astimezone().strftime('%m-%d %H:%M')
            parts = [f"担当 {task['pane']} {task['run']}".strip() if task['pane'] or task['run'] else '']
            parts += [f"残り {task['remaining']}" if task['remaining'] else '', f"結果 {task['result']}" if task['result'] else '']
            parts.append(f'更新 {updated}')
            lines.append(('    ' + ' / '.join(part for part in parts if part), 0))
            if task['decision_id']:
                decision = task['decision']
                lines.append((f'    判断 {decision_text(task)}', answered if decision and decision['answered'] else waiting))
            if task['id'] == desk.task_id:
                selected = (start, len(lines) - 1)
    return lines, selected


def draw(screen, desk):
    screen.erase()
    rows, columns = screen.getmaxyx()

    def put(y, x, value, attribute=0, maximum=None):
        if y < 0 or y >= rows or x < 0 or x >= columns - 1:
            return
        value = clip(value, min(columns - x - 1, maximum if maximum is not None else columns))
        try:
            screen.addstr(y, x, value, attribute)
        except curses.error:
            pass

    if rows < 10 or columns < 32:
        put(0, 0, 'Cue: 端末を広げてください', curses.A_BOLD)
        if desk.mode == 'quit':
            put(1, 0, ('戻る', '破棄して閉じる')[desk.menu_index], curses.A_REVERSE)
            put(2, 0, '↑↓ 選択 / Enter 決定')
        else:
            put(2, 0, 'q / Esc で終了')
        screen.noutrefresh()
        curses.doupdate()
        return 1
    pending = sum(item['answer'] is None for item in desk.items)
    put(0, 1, f'Cue   判断待ち {pending} / 判断済み {len(desk.items) - pending}', curses.A_BOLD)
    view = '判断待ち' if desk.view == 'pending' else '判断済み'
    priority = 'すべて' if desk.priority == 'all' else PRIORITIES[desk.priority]
    put(1, 1, f'{view}  優先度: {priority}   Tab: 切替 / f: 優先度')
    body_height = rows - 7
    sidebar = min(32, columns // 3) if columns >= 100 else 0
    start = sidebar + 2 if sidebar else 1
    body_width = columns - start - 2
    items = desk.visible()
    if sidebar:
        selected = next((index for index, item in enumerate(items) if desk.item and item['id'] == desk.item['id']), 0)
        first = max(0, selected - body_height // 6)
        for index, item in enumerate(items[first:first + body_height // 3]):
            active = desk.item and item['id'] == desk.item['id']
            attribute = curses.A_REVERSE if active else 0
            put(3 + index * 3, 1, f"[{PRIORITIES[item['priority']]}]", attribute, sidebar - 2)
            put(4 + index * 3, 1, item['title'], attribute, sidebar - 2)
    elif desk.item:
        index = next((index for index, item in enumerate(items) if item['id'] == desk.item['id']), None)
        put(2, 1, f"案件 {index + 1 if index is not None else '-'} / {len(items)}  ←→で移動")
    lines, choice_positions = detail_lines(desk, body_width)
    if desk.jump_choice and desk.draft()['choice'] in choice_positions:
        position = choice_positions[desk.draft()['choice']]
        if position < desk.scroll or position >= desk.scroll + body_height:
            desk.scroll = position
        desk.jump_choice = False
    desk.scroll = min(max(0, desk.scroll), max(0, len(lines) - body_height))
    for offset, (value, attribute) in enumerate(lines[desk.scroll:desk.scroll + body_height]):
        put(3 + offset, start, value, attribute, body_width)
    message = desk.load_error or desk.notice
    put(rows - 4, 1, message, curses.A_BOLD)
    put(rows - 3, 1, '↑↓ 選択  Enter 確定  ←→ 案件  m メモ  PgUp/PgDn スクロール')
    put(rows - 2, 1, 'Esc 閉じる  Tab 待ち/済み  u 取消  t 再処理  f 優先度  w Web  o リンク  b task  ? ヘルプ')
    if desk.mode:
        screen.erase()
        if desk.mode == 'quit':
            put(1, 2, '未保存の候補・メモを破棄しますか？', curses.A_BOLD)
            for index, label in enumerate(('戻る', '破棄して閉じる')):
                put(4 + index * 2, 2, label, curses.A_REVERSE if desk.menu_index == index else 0)
            put(rows - 2, 2, '↑↓ / ←→で選択、Enter で決定、Esc で閉じる')
        elif desk.mode == 'tasks':
            # 読み込み失敗を 0 件と見分けられるよう、件数の代わりに失敗を出す
            lines, selected = task_lines(desk, columns)
            put(0, 2, desk.task_error or 'task  ' + lines[0][0], curses.A_BOLD)
            lines = lines[1:]
            visible = max(1, rows - 3)
            if selected is not None:
                # 題名行から判断の行までがはみ出さないように追従する
                first, last = selected[0] - 1, selected[1] - 1
                desk.task_scroll = min(max(desk.task_scroll, last + 1 - visible), first)
                selected = first
            desk.task_scroll = min(max(0, desk.task_scroll), max(0, len(lines) - visible))
            for offset, (value, attribute) in enumerate(lines[desk.task_scroll:desk.task_scroll + visible]):
                put(1 + offset, 2, value, curses.A_REVERSE | curses.A_BOLD if desk.task_scroll + offset == selected else attribute)
            put(rows - 2, 2, desk.task_message, curses.A_BOLD)
            put(rows - 1, 2, '↑↓ 選択 / Enter 担当 pane へ移る / b で戻る')
        elif desk.mode == 'links':
            put(1, 2, '関連リンクを開く', curses.A_BOLD)
            visible = max(1, (rows - 5) // 2)
            first = max(0, desk.menu_index - visible + 1)
            for offset, (label, target) in enumerate(desk.links()[first:first + visible]):
                put(3 + offset * 2, 2, label, curses.A_REVERSE if desk.menu_index == first + offset else 0)
            put(rows - 2, 2, '↑↓で選択、Enter で開く、Esc で閉じる')
        else:
            help_lines = ['Cue の操作', '', '↑↓ / j k: 候補を選択 (数字 1〜8 でも選択)', 'Enter: 選択した回答を保存し、次の未判断へ', '←→ / p n: 案件を移動', 'PageUp / PageDown: 本文をスクロール', 'Tab: 判断待ち / 判断済みを切り替え', 'u: 処理開始前の回答を取り消す', 't: 停止した案件の再処理を依頼する', 'f: 優先度の絞り込み', 'm: $VISUAL / $EDITOR (既定 vi) でメモを編集', 'r: データを再取得', 'w: 対象案件の Web 画面を開く', 'o: 関連する PR / issue を開く', 'b: task (依頼の進み具合) を表示。↑↓ で選択、Enter で担当 pane へ移る', 'Esc: 未保存入力を破棄して即終了', 'q: 終了 (未保存入力があれば確認)', '', 'Enter / ? で戻る、Esc で終了']
            for index, line in enumerate(help_lines):
                put(index + 1, 2, line, curses.A_BOLD if index == 0 else 0)
    screen.noutrefresh()
    curses.doupdate()
    return body_height


def handle_key(desk, key, page_height=10, edit=edit_with_editor):
    if key == '\x1b':
        return False
    if desk.mode:
        if desk.mode == 'tasks':
            if key in ('b', 'q', '?'):
                desk.mode = None
            elif key in (curses.KEY_UP, 'k'):
                desk.move_task(-1)
            elif key in (curses.KEY_DOWN, 'j'):
                desk.move_task(1)
            elif key in ('\n', '\r', curses.KEY_ENTER):
                return not desk.go_task()
            elif key == 'r':
                desk.refresh()
        elif desk.mode == 'help':
            if key in ('\n', '\r', curses.KEY_ENTER, '?', 'q'):
                desk.mode = None
        elif key in (curses.KEY_UP, curses.KEY_LEFT, 'k'):
            desk.menu_index = (desk.menu_index - 1) % (2 if desk.mode == 'quit' else len(desk.links()))
        elif key in (curses.KEY_DOWN, curses.KEY_RIGHT, 'j'):
            desk.menu_index = (desk.menu_index + 1) % (2 if desk.mode == 'quit' else len(desk.links()))
        elif key in ('\n', '\r', curses.KEY_ENTER):
            if desk.mode == 'quit' and desk.menu_index == 1:
                return False
            if desk.mode == 'links':
                desk.open_url(desk.links()[desk.menu_index][1])
            desk.mode = None
        return True
    if key == 'q':
        if desk.dirty():
            desk.mode, desk.menu_index = 'quit', 0
        else:
            return False
    elif key in (curses.KEY_UP, 'k'):
        desk.choose(direction=-1)
    elif key in (curses.KEY_DOWN, 'j'):
        desk.choose(direction=1)
    elif key in (curses.KEY_LEFT, 'p'):
        desk.move_case(-1)
    elif key in (curses.KEY_RIGHT, 'n'):
        desk.move_case(1)
    elif isinstance(key, str) and key in '12345678':
        desk.choose(number=int(key) - 1)
    elif key in ('\n', '\r', curses.KEY_ENTER):
        if desk.save():
            try:
                # 同時に届いた Enter を次の案件の回答に使わない
                curses.flushinp()
            except curses.error:
                pass
    elif key == '\t':
        desk.change_view()
    elif key == 'f':
        desk.change_filter()
    elif key == curses.KEY_NPAGE:
        desk.scroll += page_height
    elif key == curses.KEY_PPAGE:
        desk.scroll = max(0, desk.scroll - page_height)
    elif key == 'r':
        desk.refresh()
    elif key == 'u':
        desk.revise('undo')
    elif key == 't':
        desk.revise('retry')
    elif key == 'w':
        desk.open_web()
    elif key == 'o':
        if desk.links():
            desk.mode, desk.menu_index = 'links', 0
        else:
            desk.notice = '関連リンクはありません'
    elif key == '?':
        desk.mode = 'help'
    elif key == 'b':
        desk.refresh()
        desk.mode, desk.task_scroll, desk.task_message = 'tasks', 0, ''
    elif key == 'm':
        if desk.item and desk.item['answer'] is None:
            try:
                desk.draft()['memo'] = edit(desk.draft()['memo'])
                desk.notice = 'メモは 2,000 文字までです。下書きは保持しています。m で修正してください' if len(desk.draft()['memo']) > 2000 else 'メモを下書きに反映しました。回答の確定時に保存します'
            except (Exception, KeyboardInterrupt) as error:
                desk.notice = str(error) or 'メモの編集を中止しました。元の入力を保持しています'
        else:
            desk.notice = '未判断の案件でメモを編集してください'
    return True


def run(store, *, web_url=None):
    locale.setlocale(locale.LC_ALL, '')

    def interactive(screen):
        curses.set_escdelay(100)
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        screen.keypad(True)
        screen.timeout(1000)
        desk = Desk(store, web_url, popup=bool(os.environ.get('HERDR_PLUGIN_ENTRYPOINT_ID')))
        desk.heading_styles = heading_styles()
        next_refresh = time.monotonic() + 1

        def edit(memo):
            curses.def_prog_mode()
            curses.endwin()
            try:
                return edit_with_editor(memo)
            finally:
                curses.reset_prog_mode()
                screen.keypad(True)
                screen.timeout(1000)
                screen.clearok(True)
                try:
                    curses.curs_set(0)
                except curses.error:
                    pass

        while True:
            if time.monotonic() >= next_refresh:
                desk.refresh()
                next_refresh = time.monotonic() + 1
            page_height = draw(screen, desk)
            try:
                key = screen.get_wch()
            except curses.error:
                continue
            except KeyboardInterrupt:
                key = 'q'
            if not handle_key(desk, key, page_height, edit):
                return 0

    return curses.wrapper(interactive)
