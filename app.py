#!/usr/bin/env python3
"""Local decision inbox, using only the Python standard library."""
import argparse
from contextlib import ExitStack, contextmanager, suppress
from datetime import datetime, timedelta, timezone
import errno
import fcntl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import re
import secrets
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from urllib.parse import unquote, urlsplit
import uuid

ROOT = Path(__file__).resolve().parent
MAX_BODY = 131072
ASSETS = {'/assets/demo-flow.svg': ('assets/demo-flow.svg', 'image/svg+xml')}
LANES = {'todo': '未着手', 'human_decision': '人の判断待ち', 'worker': 'worker 作業中', 'human_action': '人の操作待ち', 'done': '完了'}
RECONCILE_HELP = 'reconciler だけが使う。lane と機械の欄 (machine) を同時に書く。remaining は変えない。lane を書く経路はこれだけ。指定した --from 系の条件が一致するときだけ書く (一致しなければ exit 3)'
LANE_REFUSED = 'lane は reconciler が書く。手で打っても次の reconcile が派生値に戻す'
# lane と machine は reconciler 専用 (reconcile_lane だけが書く) なので、add / update の項目に入れない
TASK_FIELDS = ('title', 'request', 'pane', 'run', 'result', 'remaining', 'decision_id', 'kind', 'done_when')
# 依頼の種類。判断 / 監視 / 調査 / 実装
TASK_KINDS = ('decision', 'monitor', 'research', 'implement')
SESSION_ROLES = ('frontdesk', 'worker', 'cue_worker')
SESSION_CLIS = ('claude', 'codex')
SESSION_CLOSE_REASONS = ('normal', 'replaced', 'crashed', 'closed')
SESSION_METRICS = ('turn_count', 'input_tokens', 'elapsed_seconds')
POST_STATES = ('unreceived', 'received')
POST_CLASSIFICATIONS = ('expected', 'unexpected')
# 人待ち理由で止まっている lane。投稿の分類 (想定内) は投稿時点のこの集合から導く (設計 Q13)
WAITING_LANES = ('human_decision', 'human_action')
# add の再送判定で比べる登録内容。result / remaining / decision_id は update で進むので比べない
TASK_IDENTITY = ('title', 'request', 'pane', 'run')
TASK_LIMITS = {'title': 200, 'request': 1000, 'pane': 200, 'run': 200, 'result': 2048, 'remaining': 2000, 'machine': 500, 'done_when': 1000}
WEB_ROOT = ROOT / 'frontend' / 'dist'
STATIC = {'/': ('index.html', 'text/html; charset=utf-8'), '/index.html': ('index.html', 'text/html; charset=utf-8'), '/assets/app.js': ('assets/app.js', 'text/javascript; charset=utf-8'), '/assets/app.css': ('assets/app.css', 'text/css; charset=utf-8')}


class AppError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class LaneConflict(AppError):
    """reconcile-lane の比較条件の不一致。409 は他の断りにも使うので、CLI が exit 3 に分けるための専用の型"""


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


def timestamp_after(previous):
    current = utc_now()
    if current <= previous:
        with suppress(ValueError):
            return (datetime.fromisoformat(previous) + timedelta(microseconds=1)).isoformat(timespec='microseconds')
    return current


JSON_TYPES = {str: 'string', list: 'array', dict: 'object', bool: 'boolean', int: 'number', float: 'number', type(None): 'null'}


def field(kind=None, required=False, values=None, fields=None, nullable=False, note=''):
    """項目の定義。kind は str / list / dict (None なら型を問わない)、fields は dict 自身か list の各要素が持つ項目"""
    return {'kind': kind, 'required': required, 'values': values, 'fields': fields, 'nullable': nullable, 'note': note}


def type_text(rule):
    if rule['values']:
        return ' / '.join(rule['values'])
    text = JSON_TYPES.get(rule['kind'], '') if rule['kind'] else ''
    if rule['fields'] is not None:
        names = ', '.join(key + ('' if item['required'] else '?') + (': ' + type_text(item) if type_text(item) else '') for key, item in rule['fields'].items())
        text = '{' + names + '}' if rule['kind'] is not list else '[{' + names + '}]'
    return text + (' | null' if rule['nullable'] else '')


def shape_problems(value, fields, path=''):
    """型違い・必須欠落・未知の項目・許されない値を、見つけた分すべて文章で返す"""
    if not isinstance(value, dict):
        return [f"{path or 'JSON'} は {type_text(field(dict, fields=fields))} を指定してください (受け取った型: {JSON_TYPES.get(type(value), type(value).__name__)})"]
    problems = []
    for key, rule in fields.items():
        here = f'{path}.{key}' if path else key
        if key not in value:
            if rule['required']:
                problems.append(f'{here} は必須です' + (f' ({type_text(rule)})' if type_text(rule) else ''))
            continue
        item = value[key]
        received = JSON_TYPES.get(type(item), type(item).__name__)
        if item is None and rule['nullable']:
            continue
        if rule['values'] is not None:
            if item not in rule['values']:
                problems.append(f"{here} は {type_text(rule)} のいずれかを指定してください (受け取った型: {received}, 値: {json.dumps(item, ensure_ascii=False)[:40]})")
        elif rule['kind'] and not isinstance(item, rule['kind']):
            problems.append(f'{here} は {type_text(rule)} を指定してください (受け取った型: {received})')
        elif rule['fields'] is not None and rule['kind'] is list:
            for index, element in enumerate(item):
                problems += shape_problems(element, rule['fields'], f'{here}[{index}]')
        elif rule['fields'] is not None:
            problems += shape_problems(item, rule['fields'], here)
    problems += [f"{path + '.' if path else ''}{key} は未知の項目です (指定できるのは {', '.join(fields)})" for key in sorted(value.keys() - fields.keys())]
    return problems


def check_shape(value, fields):
    problems = shape_problems(value, fields)
    if problems:
        raise AppError(400, 'JSON の項目を確認してください: ' + '; '.join(problems))


def object_fields(value, required, optional=()):
    check_shape(value, {**{key: field() for key in optional}, **{key: field(required=True) for key in required}})


def text(value, name, maximum, empty=False):
    if not isinstance(value, str) or len(value) > maximum or (not empty and not value.strip()) or '\x00' in value or any(0xD800 <= ord(char) <= 0xDFFF for char in value):
        raise AppError(400, f'{name} の型または文字数が不正です')
    return value


def parse_iso_timestamp(value, name):
    text(value, name, 64)
    if value.endswith('Z'):
        value = value[:-1] + '+00:00'
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise AppError(400, f'{name} は timezone 付き ISO 8601 で指定してください') from None


def url(value, image=False):
    text(value, 'URL', 2048)
    if image and value in ASSETS:
        return value
    if any(ord(char) <= 32 or ord(char) == 127 for char in value) or '\\' in value:
        raise AppError(400, 'URL が不正です')
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username is not None or parsed.password is not None:
            raise ValueError()
        parsed.port
    except ValueError:
        raise AppError(400, 'http(s) URL を指定してください') from None
    return value


PRIORITIES = ('urgent', 'normal', 'later')
LINK_KINDS = ('pr', 'issue', 'other')
# add / POST /api/decisions / update が受ける項目。型・必須・許す値の検査と add --help の一覧は、どちらもこの表から作る。文字数や URL の形は validate_request が見る
REQUEST_FIELDS = {
    'title': field(str, True), 'source': field(str, True), 'reason': field(str, True),
    'options': field(list, True, fields={key: field(str, True) for key in ('id', 'title', 'benefits', 'concerns')}, note='候補。id は重複しない値'),
    'recommended': field(str, True, note='options のどれかの id'),
    'recommendation': field(str, True),
    'background': field(str), 'priority': field(str, values=PRIORITIES, note='既定 normal'), 'urgency_reason': field(str),
    'deadline': field(str, nullable=True, note='timezone 付き ISO 8601'),
    'links': field(list, fields={'url': field(str, True), 'label': field(str, True), 'kind': field(str, True, values=LINK_KINDS)}, note='url は http(s)'),
    'files': field(list, fields={'path': field(str, True), 'label': field(str, True)}, note='path は .. を含まない絶対パス'),
    'image': field(dict, nullable=True, fields={'url': field(str, True), 'alt': field(str, True)}),
    'request_key': field(str, note='英数字と _ . : / -。元依頼ごとに一定にして、再送でも変えない'),
}
CHANGEABLE_FIELDS = tuple(key for key in REQUEST_FIELDS if key != 'request_key')
ADD_EXAMPLE = {
    'title': 'レビューの進め方を決める', 'source': 'frontdesk', 'reason': 'どちらで進めるかが決まらず作業が止まっている',
    'options': [{'id': 'minimal', 'title': '影響範囲を限定する', 'benefits': '確認が早い', 'concerns': '追加作業が残る'}, {'id': 'full', 'title': 'まとめて対応する', 'benefits': '残作業が減る', 'concerns': '公開が遅れる'}],
    'recommended': 'minimal', 'recommendation': '現在の作業への影響を抑えるため',
}


def add_help():
    def lines(required):
        return [f"  {key}: {type_text(rule)}" + (f"  ({rule['note']})" if rule['note'] else '') for key, rule in REQUEST_FIELDS.items() if rule['required'] is required]
    return '\n'.join(['JSON の最上位は object。未知の項目は拒否する。型・必須・許す値の不正は、error に項目名・期待する型・受け取った型を全件並べる。文字数・件数・URL の形の不正は 1 件ずつ返す', '', '必須の項目:', *lines(True), '任意の項目:', *lines(False), '', '最小の例 (そのまま add を通る):', json.dumps(ADD_EXAMPLE, ensure_ascii=False, indent=2)])


def validate_request(payload):
    check_shape(payload, REQUEST_FIELDS)
    item = {key: text(payload[key], key, maximum) for key, maximum in [('title', 200), ('source', 200), ('reason', 4000), ('recommendation', 4000)]}
    for key, maximum in [('background', 12000), ('urgency_reason', 2000)]:
        item[key] = text(payload.get(key, ''), key, maximum, empty=True)
    item['priority'] = payload.get('priority', 'normal')
    deadline = payload.get('deadline')
    if deadline is not None:
        text(deadline, 'deadline', 64)
        try:
            parsed = datetime.fromisoformat(deadline)
            if parsed.tzinfo is None:
                raise ValueError()
            deadline = parsed.astimezone(timezone.utc).isoformat()
        except (ValueError, OverflowError):
            raise AppError(400, '期限は timezone 付き ISO 8601 で指定してください') from None
    item['deadline'] = deadline
    options = payload['options']
    if not 2 <= len(options) <= 8:
        raise AppError(400, '候補は 2〜8 件を指定してください')
    ids = set()
    item['options'] = []
    for option in options:
        identifier = text(option['id'], '候補 ID', 64)
        if not re.fullmatch(r'[A-Za-z0-9_-]+', identifier) or identifier in ids:
            raise AppError(400, '候補 ID が不正または重複しています')
        ids.add(identifier)
        item['options'].append({'id': identifier, 'title': text(option['title'], '候補名', 300), 'benefits': text(option['benefits'], '利点', 2000, empty=True), 'concerns': text(option['concerns'], '懸念', 2000, empty=True)})
    if payload['recommended'] not in ids:
        raise AppError(400, '推奨候補が見つかりません')
    item['recommended'] = payload['recommended']
    links = payload.get('links', [])
    if len(links) > 20:
        raise AppError(400, '関連リンクは 20 件までです')
    item['links'] = []
    for link in links:
        item['links'].append({'url': url(link['url']), 'label': text(link['label'], 'リンク名', 200), 'kind': link['kind']})
    files = payload.get('files', [])
    if len(files) > 20:
        raise AppError(400, 'ファイルは 20 件までです')
    # 中身や存在は読むときに見る。登録時に見ると、下書きを消した後の update や再送が通らなくなる
    if files:
        item['files'] = []
        for entry in files:
            path = text(entry['path'], 'ファイルのパス', 4096)
            if not path.startswith('/') or '..' in path.split('/'):
                raise AppError(400, 'ファイルは .. を含まない絶対パスで指定してください')
            item['files'].append({'path': path, 'label': text(entry['label'], 'ファイル名', 200)})
    image = payload.get('image')
    if image is not None:
        image = {'url': url(image['url'], image=True), 'alt': text(image['alt'], '画像の説明', 1000)}
    item['image'] = image
    if 'request_key' in payload:
        item['request_key'] = text(payload['request_key'], 'request_key', 200)
        if not re.fullmatch(r'[A-Za-z0-9_.:/-]+', item['request_key']):
            raise AppError(400, 'request_key は英数字と _ . : / - で指定してください')
    if len(json.dumps(item, ensure_ascii=False).encode()) > MAX_BODY:
        raise AppError(413, '依頼のサイズが上限を超えています')
    return item


FILE_LIMIT = 262144


FILES_ROOTS = ('~/.agents/state/runs', '~/.ghq/github.com/nekottyo/obsidian-vault/nekottyo/claude-knowledge/review-repo')


def files_root(value=None):
    """読める root の一覧。launchd と plugin は flag を渡せないので、既定値に必要な場所を並べておく"""
    env = os.environ.get('CUE_FILES_ROOT')
    values = value or (env.split(os.pathsep) if env else FILES_ROOTS)
    if isinstance(values, (str, Path)):
        values = [values]
    return [Path(item).expanduser() for item in values if item]


def read_text_file(path, root):
    """root (1 つか一覧) の下にある UTF-8 テキストだけを返す。登録された path は外部入力なので、symlink と .. を realpath で解いてから比べる"""
    roots = [os.path.realpath(item) for item in ([root] if isinstance(root, (str, Path)) else root)]
    real = os.path.realpath(path)
    if not any(os.path.commonpath([item, real]) == item for item in roots):
        raise AppError(403, f'{"、".join(roots)} の外にあるファイルは表示しません')
    try:
        # FIFO を開いて待たないよう O_NONBLOCK で開き、通常ファイルかは開いた fd で確かめる
        with open(os.open(real, os.O_RDONLY | os.O_NONBLOCK), 'rb') as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise AppError(400, '通常のファイルではないため表示しません')
            raw = handle.read(FILE_LIMIT + 1)
    except FileNotFoundError:
        raise AppError(404, 'ファイルが見つかりません') from None
    except IsADirectoryError:
        raise AppError(400, '通常のファイルではないため表示しません') from None
    except PermissionError:
        raise AppError(403, 'ファイルを読む権限がありません') from None
    except NotADirectoryError:
        raise AppError(404, 'ファイルが見つかりません') from None
    except OSError as error:
        # symlink ループや長すぎる名前など。素の OSError で漏らすと Web は 500、TUI は pager の失敗と誤って出る
        raise AppError(400, f'ファイルを開けません ({error.strerror})') from None
    if len(raw) > FILE_LIMIT:
        raise AppError(413, f'{FILE_LIMIT // 1024} KiB を超えるファイルは表示しません')
    try:
        content = raw.decode('utf-8')
    except UnicodeDecodeError:
        content = '\x00'
    if '\x00' in content:
        raise AppError(415, 'UTF-8 のテキストではないため表示しません')
    return content


def canonical(item):
    return json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def validate_task(payload):
    """payload に入った項目だけを検査して返す。decision_id の存在確認は Store.check_decision が行う"""
    item = {}
    if 'id' in payload:
        item['id'] = text(payload['id'], 'id', 200)
        if not re.fullmatch(r'[A-Za-z0-9_.:/-]+', item['id']):
            raise AppError(400, 'id は英数字と _ . : / - で指定してください')
    for key, maximum in TASK_LIMITS.items():
        if key in payload:
            item[key] = text(payload[key], key, maximum, empty=key not in ('title', 'done_when'))
    if 'kind' in payload:
        if not isinstance(payload['kind'], str) or payload['kind'] not in TASK_KINDS:
            raise AppError(400, 'kind は ' + ' / '.join(TASK_KINDS) + ' のいずれかを指定してください')
        item['kind'] = payload['kind']
    if 'decision_id' in payload:
        value = payload['decision_id']
        if value in ('', None):
            item['decision_id'] = None
        else:
            if not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{32}', value):
                raise AppError(400, 'decision_id の形式が不正です')
            item['decision_id'] = value
    return item


# 依頼の種類ごとの必須。implement は完了の条件に PR か issue の URL (ADR-321 Q19)、decision は判断の ID。
# 番号だけの「PR 305」は repo が分からず、誰も辿れないので断る
PR_URL = re.compile(r'https://github\.com/[\w.-]+/[\w.-]+/(?:pull|issues)/[0-9]+(?![A-Za-z0-9_])')


def check_task_rules(kind, done_when, decision_id):
    # kind が空の legacy 行は対象外。add は kind を必須にしているので、ここへは来ない
    if kind == 'implement' and not PR_URL.search(done_when or ''):
        raise AppError(400, 'kind が implement のときは done_when に PR か issue の URL (https://github.com/<org>/<repo>/pull/<番号>) を書いてください')
    if kind == 'decision' and not decision_id:
        raise AppError(400, 'kind が decision のときは decision_id が必要です。先に判断を登録して --decision で指定してください')


def parse_json(raw):
    def pairs(entries):
        result = {}
        for key, value in entries:
            if key in result:
                raise ValueError('Duplicate JSON field')
            result[key] = value
        return result
    def invalid_constant(value):
        raise ValueError('Invalid JSON constant')
    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise AppError(400, '有効な JSON を指定してください') from None


SCHEMA = '''CREATE TABLE decisions (
    id TEXT PRIMARY KEY, created_at TEXT NOT NULL, payload TEXT NOT NULL,
    choice TEXT, memo TEXT, decided_at TEXT,
    status TEXT CHECK(status IN ('pending','processing','completed','blocked','failed')),
    worker TEXT, token TEXT, lease_until REAL, request_key TEXT UNIQUE,
    reason TEXT NOT NULL DEFAULT '', next_step TEXT NOT NULL DEFAULT '', reported_at TEXT,
    CHECK((choice IS NULL AND status IS NULL) OR (choice IS NOT NULL AND status IS NOT NULL)))'''
LEGACY_COLUMNS = 'id,created_at,payload,choice,memo,decided_at,status,worker,token,lease_until'


def validate_lease(seconds):
    if type(seconds) is not int or not 1 <= seconds <= 3600:
        raise AppError(400, 'lease_seconds は 1〜3600 の整数を指定してください')
    return seconds


# 旧 release に戻した間に取り下げ済みへ回答されると、回答済みかつ取り下げ済みの行ができる。claim / watch に渡さない
NOT_WITHDRAWN = " AND NOT EXISTS(SELECT 1 FROM decision_changes WHERE decision_id=decisions.id AND event='withdraw')"


def prefix_clause(key_prefix):
    if key_prefix is None:
        return '', ()
    key_prefix = text(key_prefix, 'key_prefix', 200)
    if not re.fullmatch(r'[A-Za-z0-9_.:/-]+', key_prefix):
        raise AppError(400, 'key_prefix は英数字と _ . : / - で指定してください')
    # LIKE はワイルドカードの扱いが紛れるため substr で前方一致を取る
    return ' AND substr(request_key,1,?)=?', (len(key_prefix), key_prefix)


class Store:
    def __init__(self, path, readonly=False):
        self.path = str(path)
        self.readonly = readonly
        if readonly:
            return
        parent = Path(path).parent
        if not parent.exists():
            parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(descriptor)
        with self.db() as connection:
            connection.execute('BEGIN IMMEDIATE')
            version = connection.execute('PRAGMA user_version').fetchone()[0]
            if version not in (0, 1, 2):
                raise AppError(500, '未対応の DB バージョンです')
            exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='decisions'").fetchone()
            if not exists:
                connection.execute(SCHEMA)
            else:
                columns = {row['name'] for row in connection.execute('PRAGMA table_info(decisions)')}
                if columns == set(LEGACY_COLUMNS.split(',')):
                    self.backup(self.path + '.pre-v2-' + uuid.uuid4().hex + '.sqlite3')
                    connection.execute('ALTER TABLE decisions RENAME TO decisions_legacy')
                    connection.execute(SCHEMA)
                    connection.execute(f'INSERT INTO decisions({LEGACY_COLUMNS}) SELECT {LEGACY_COLUMNS} FROM decisions_legacy')
                    connection.execute('DROP TABLE decisions_legacy')
                elif columns != set(LEGACY_COLUMNS.split(',')) | {'request_key', 'reason', 'next_step', 'reported_at'}:
                    raise AppError(500, '未対応の DB 形式です')
            connection.execute('''CREATE TABLE IF NOT EXISTS answer_history (
                id INTEGER PRIMARY KEY, decision_id TEXT NOT NULL, event TEXT NOT NULL,
                recorded_at TEXT NOT NULL, answer TEXT NOT NULL)''')
            # user_version は 2 のまま上げない。旧 release の Store は 0/1/2 以外を 500 で拒否するため、
            # 上げると release を戻せなくなる。表は IF NOT EXISTS で足すだけにする。
            # lane に CHECK を付けないのは、lane の追加・改名で表の作り直しを避けるため。
            connection.execute('''CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY, title TEXT NOT NULL, request TEXT NOT NULL DEFAULT '',
                pane TEXT NOT NULL DEFAULT '', run TEXT NOT NULL DEFAULT '', result TEXT NOT NULL DEFAULT '',
                remaining TEXT NOT NULL DEFAULT '', lane TEXT NOT NULL, decision_id TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL)''')
            # machine (機械の欄) は reconciler だけが書く。既存の tasks 表へは列を足すだけ。DEFAULT '' があるので旧 release の INSERT も通る
            if 'machine' not in {row['name'] for row in connection.execute('PRAGMA table_info(tasks)')}:
                connection.execute("ALTER TABLE tasks ADD COLUMN machine TEXT NOT NULL DEFAULT ''")
            # kind / done_when も同じ流儀で足す。legacy 行は空のまま読み、add だけが必須にする
            columns = {row['name'] for row in connection.execute('PRAGMA table_info(tasks)')}
            for column in ('kind', 'done_when'):
                if column not in columns:
                    connection.execute(f"ALTER TABLE tasks ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
            # 更新・取り下げは decisions に列を足さず別表に積む。decisions の列や状態の CHECK を変えると
            # 旧 release が開けなくなるため。取り下げ済み = withdraw 行があること。旧 release に戻すと
            # この表は読まれず、取り下げ済みの判断が未回答として再表示される
            connection.execute('''CREATE TABLE IF NOT EXISTS decision_changes (
                id INTEGER PRIMARY KEY, decision_id TEXT NOT NULL, event TEXT NOT NULL,
                recorded_at TEXT NOT NULL, note TEXT NOT NULL, before TEXT)''')
            connection.execute("CREATE INDEX IF NOT EXISTS decision_changes_withdrawn ON decision_changes(decision_id) WHERE event='withdraw'")
            # runs も同じく IF NOT EXISTS で足すだけで、user_version は上げない (理由は tasks と同じ)。
            # 開いた行 = closed_at が NULL。同じ run_id と role の開いた行は 1 つだけにする
            connection.execute('''CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, role TEXT NOT NULL, pane TEXT NOT NULL, socket TEXT NOT NULL,
                opened_at TEXT NOT NULL, closed_at TEXT)''')
            connection.execute('CREATE UNIQUE INDEX IF NOT EXISTS runs_open ON runs(run_id, role) WHERE closed_at IS NULL')
            # messages も同じく足すだけ。本文は持たない (本文はファイルが運ぶ外部入力で、これは窓口側が取り込む写し)
            connection.execute('''CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, msg TEXT NOT NULL, tag TEXT NOT NULL, from_pane TEXT NOT NULL, req TEXT,
                state TEXT NOT NULL, wait TEXT, received_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(run_id, msg))''')
            # sessions も同じく IF NOT EXISTS で足すだけで、user_version は上げない (理由は tasks / runs と同じ)。
            # 開いた行 = closed_at が NULL。開いた状態で (cli, native_session_id) と (run_id, role, pane, socket) をそれぞれ一意にする
            connection.execute('''CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, role TEXT NOT NULL,
                req_id TEXT NOT NULL DEFAULT '', pane TEXT NOT NULL, socket TEXT NOT NULL,
                cli TEXT NOT NULL, native_session_id TEXT NOT NULL,
                opened_at TEXT NOT NULL, closed_at TEXT, close_reason TEXT NOT NULL DEFAULT '')''')
            columns = {row['name'] for row in connection.execute('PRAGMA table_info(sessions)')}
            for column in SESSION_METRICS:
                if column not in columns:
                    connection.execute(f'ALTER TABLE sessions ADD COLUMN {column} INTEGER')
            connection.execute('CREATE UNIQUE INDEX IF NOT EXISTS sessions_open_native ON sessions(cli, native_session_id) WHERE closed_at IS NULL')
            connection.execute('CREATE UNIQUE INDEX IF NOT EXISTS sessions_open_slot ON sessions(run_id, role, pane, socket) WHERE closed_at IS NULL')
            # posts も同じく IF NOT EXISTS で足すだけ (理由は tasks / runs と同じ)。依頼 thread への人の投稿を
            # 配送試行として記録する。状態遷移は proofs/DeliveryAttempt.lean で形式化済み
            connection.execute('''CREATE TABLE IF NOT EXISTS posts (
                id INTEGER PRIMARY KEY, task_id TEXT NOT NULL, body TEXT NOT NULL,
                classification TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'unreceived',
                target_run TEXT NOT NULL DEFAULT '', target_pane TEXT NOT NULL DEFAULT '',
                target_socket TEXT NOT NULL DEFAULT '', target_session TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL, received_at TEXT, received_by TEXT NOT NULL DEFAULT '')''')
            connection.execute('CREATE INDEX IF NOT EXISTS posts_task ON posts(task_id, id)')
            connection.execute("CREATE INDEX IF NOT EXISTS posts_unreceived ON posts(state) WHERE state='unreceived'")
            if version != 2:
                connection.execute('PRAGMA user_version=2')

    @contextmanager
    def db(self):
        target = Path(self.path).resolve().as_uri() + '?mode=ro' if self.readonly else self.path
        connection = sqlite3.connect(target, timeout=10, uri=self.readonly)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def backup(self, output):
        output = Path(output).expanduser().absolute()
        if output.resolve() == Path(self.path).resolve() or output.exists() or output.is_symlink():
            raise AppError(409, 'バックアップ先は既存ファイルや元 DB と別のパスを指定してください')
        descriptor, temporary = tempfile.mkstemp(prefix='.cue-backup-', dir=output.parent)
        os.close(descriptor)
        try:
            source = sqlite3.connect(Path(self.path).resolve().as_uri() + '?mode=ro', uri=True, timeout=10)
            try:
                destination = sqlite3.connect(temporary)
                try:
                    source.backup(destination)
                    # 単一ファイルで復元できるよう、コピー元の WAL 設定を持ち込まない。
                    destination.execute('PRAGMA journal_mode=DELETE')
                    if destination.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                        raise AppError(500, 'バックアップの整合性を確認できませんでした')
                finally:
                    destination.close()
            finally:
                source.close()
            with open(temporary, 'rb') as handle:
                os.fsync(handle.fileno())
            try:
                os.link(temporary, output)
            except FileExistsError:
                raise AppError(409, 'バックアップ先が既に存在します') from None
        finally:
            for suffix in ('', '-wal', '-shm', '-journal'):
                try:
                    os.unlink(temporary + suffix)
                except FileNotFoundError:
                    pass
        return {'backup': str(output)}

    def changes(self, connection, identifier=None):
        """decision_id ごとの decision_changes の行 (記録順)。identifier が無ければ全件"""
        where, params = (' WHERE decision_id=?', (identifier,)) if identifier else ('', ())
        found = {}
        # TUI が毎秒呼ぶ list() で変更前の payload まで読まないよう before は引かない
        for row in connection.execute('SELECT decision_id,event,recorded_at,note FROM decision_changes' + where + ' ORDER BY id', params):
            found.setdefault(row['decision_id'], []).append(row)
        return found

    def fetch(self, connection, identifier):
        row = connection.execute('SELECT * FROM decisions WHERE id=?', (identifier,)).fetchone()
        return self.public(row, self.changes(connection, identifier).get(identifier, []))

    def public(self, row, changes):
        if row is None:
            raise AppError(404, '案件が見つかりません')
        # files は指定時だけ保存する (既存行の canonical を変えないため)。読む側には常に配列で渡す
        item = {'files': [], **json.loads(row['payload'])}
        updates = [change for change in changes if change['event'] == 'update']
        withdrawn = next((change for change in changes if change['event'] == 'withdraw'), None)
        item.update(id=row['id'], created_at=row['created_at'], answer=None, revision=len(updates),
                    updated_at=updates[-1]['recorded_at'] if updates else None, update_note=updates[-1]['note'] if updates else None,
                    withdrawn={'note': withdrawn['note'], 'withdrawn_at': withdrawn['recorded_at']} if withdrawn else None)
        if row['choice'] is not None:
            item['answer'] = {
                'choice': row['choice'], 'memo': row['memo'], 'decided_at': row['decided_at'], 'status': row['status'],
                'reason': row['reason'], 'next_step': row['next_step'], 'reported_at': row['reported_at'],
                'lease_until': row['lease_until'],
                'can_undo': row['status'] == 'pending' and row['worker'] is None and row['token'] is None and row['lease_until'] is None,
            }
        return item

    def stamp(self, connection, identifier, previous=''):
        latest = connection.execute('SELECT MAX(recorded_at) FROM answer_history WHERE decision_id=?', (identifier,)).fetchone()[0]
        return timestamp_after(max(previous, latest or ''))

    def record(self, connection, row, event):
        connection.execute('INSERT INTO answer_history(decision_id,event,recorded_at,answer) VALUES(?,?,?,?)',
                           (row['id'], event, self.stamp(connection, row['id'], max(row['decided_at'] or '', row['reported_at'] or '')), json.dumps(self.public(row, [])['answer'], ensure_ascii=False)))

    def active(self, row, token):
        self.public(row, [])
        text(token, 'token', 200)
        now = time.time()
        if not token.isascii() or row['status'] != 'processing' or not row['token'] or not secrets.compare_digest(row['token'], token) or row['lease_until'] is None or row['lease_until'] <= now:
            raise AppError(409, 'claim token が無効または期限切れです')
        return now

    def add(self, payload):
        item = validate_request(payload)
        expected = canonical(item)
        identifier = uuid.uuid4().hex
        with self.db() as connection:
            connection.execute('BEGIN IMMEDIATE')
            if 'request_key' in item:
                row = connection.execute('SELECT * FROM decisions WHERE request_key=?', (item['request_key'],)).fetchone()
                if row:
                    changes = self.changes(connection, row['id']).get(row['id'], [])
                    if any(change['event'] == 'withdraw' for change in changes):
                        raise AppError(409, 'この request_key の判断は取り下げ済みです。置き換えるときは新しい request_key で add してください')
                    # update 前の内容での再送も同じ依頼として受ける
                    known = [row['payload']] + [value for value, in connection.execute("SELECT before FROM decision_changes WHERE decision_id=? AND event='update'", (row['id'],))]
                    if expected not in [canonical(json.loads(value)) for value in known]:
                        raise AppError(409, '同じ request_key に異なる依頼が登録されています')
                    return self.public(row, changes)
            connection.execute('INSERT INTO decisions(id,created_at,payload,request_key) VALUES(?,?,?,?)', (identifier, utc_now(), expected, item.get('request_key')))
        return self.get(identifier)

    def list(self, include_withdrawn=False):
        with self.db() as connection:
            # 履歴と本文を同じ読み取り時点にそろえる
            connection.execute('BEGIN')
            changes = self.changes(connection)
            items = [self.public(row, changes.get(row['id'], [])) for row in connection.execute('SELECT * FROM decisions ORDER BY created_at,id')]
        # 窓口は answer が null の判断を未判断として人に案内するため、取り下げ済みは既定で外す
        return [item for item in items if include_withdrawn or item['withdrawn'] is None]

    def ready(self, key_prefix=None):
        clause, params = prefix_clause(key_prefix)
        with self.db() as connection:
            return [row['id'] for row in connection.execute("SELECT id FROM decisions WHERE (status='pending' OR (status='processing' AND lease_until<=?))" + NOT_WITHDRAWN + clause + " ORDER BY decided_at,id", (time.time(), *params))]

    def get(self, identifier):
        with self.db() as connection:
            connection.execute('BEGIN')
            return self.fetch(connection, identifier)

    def answer(self, identifier, payload):
        object_fields(payload, ('choice',), ('memo', 'revision'))
        choice = text(payload['choice'], '回答', 64)
        memo = text(payload.get('memo', ''), 'メモ', 2000, empty=True)
        revision = payload.get('revision')
        # null を未指定扱いにすると古い版への回答が黙って通る
        if 'revision' in payload and (type(revision) is not int or revision < 0):
            raise AppError(400, 'revision は 0 以上の整数を指定してください')
        with self.db() as connection:
            connection.execute('BEGIN IMMEDIATE')
            item = self.fetch(connection, identifier)
            if item['withdrawn'] is not None:
                raise AppError(409, 'この判断は取り下げられました')
            if item['answer'] is not None:
                raise AppError(409, '別の画面で判断済みです。入力は保存していません')
            if revision is not None and revision != item['revision']:
                raise AppError(409, '内容が更新されています。最新の内容を確認してから回答してください')
            if choice not in {option['id'] for option in item['options']}:
                raise AppError(400, '選択した候補が見つかりません')
            connection.execute("UPDATE decisions SET choice=?,memo=?,decided_at=?,status='pending' WHERE id=? AND choice IS NULL", (choice, memo, self.stamp(connection, identifier), identifier))
            # task の lane はここで書かない。回答済みは decisions 表の事実として残り、reconciler が写す
        return self.get(identifier)

    def undo(self, identifier, payload):
        object_fields(payload, ('decided_at',))
        expected = text(payload['decided_at'], 'decided_at', 64)
        with self.db() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute('SELECT * FROM decisions WHERE id=?', (identifier,)).fetchone()
            answer = self.public(row, [])['answer']
            if not answer or not answer['can_undo'] or row['decided_at'] != expected:
                raise AppError(409, '回答が変更済み、または処理開始済みのため取り消せません')
            self.record(connection, row, 'undo')
            connection.execute('UPDATE decisions SET choice=NULL,memo=NULL,decided_at=NULL,status=NULL WHERE id=?', (identifier,))
        return self.get(identifier)

    def update(self, identifier, payload):
        object_fields(payload, ('note', 'changes'))
        note = text(payload['note'], 'note', 2000)
        changes = payload['changes']
        if isinstance(changes, dict) and 'request_key' in changes:
            raise AppError(400, 'request_key は変更できません。別の依頼は新しい request_key で add してください')
        object_fields(changes, (), CHANGEABLE_FIELDS)
        with self.db() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute('SELECT * FROM decisions WHERE id=?', (identifier,)).fetchone()
            item = self.public(row, self.changes(connection, identifier).get(identifier, []))
            if item['withdrawn'] is not None:
                raise AppError(409, 'この判断は取り下げ済みのため更新できません')
            if item['answer'] is not None:
                raise AppError(409, '回答済みの判断は更新できません。回答を確認してください')
            before = canonical(json.loads(row['payload']))
            after = canonical(validate_request({**json.loads(row['payload']), **changes}))
            if after == before:
                return item
            connection.execute('UPDATE decisions SET payload=? WHERE id=? AND choice IS NULL', (after, identifier))
            connection.execute("INSERT INTO decision_changes(decision_id,event,recorded_at,note,before) VALUES(?,'update',?,?,?)", (identifier, utc_now(), note, before))
            return self.fetch(connection, identifier)

    def withdraw(self, identifier, payload):
        object_fields(payload, ('note',))
        note = text(payload['note'], 'note', 2000)
        with self.db() as connection:
            connection.execute('BEGIN IMMEDIATE')
            item = self.fetch(connection, identifier)
            if item['withdrawn'] is not None:
                return item
            # claim は回答済みしか拾わないので、未回答に限れば処理中の worker と重ならない
            if item['answer'] is not None:
                raise AppError(409, '回答済みの判断は取り下げられません。claim 中なら token を持つ worker が report または complete で閉じてください')
            connection.execute("INSERT INTO decision_changes(decision_id,event,recorded_at,note) VALUES(?,'withdraw',?,?)", (identifier, utc_now(), note))
            return self.fetch(connection, identifier)

    def file(self, identifier, index, root):
        """登録済みの files[index] を表示のたびに読む。任意のパスは受けない"""
        files = self.get(identifier)['files']
        if not 0 <= index < len(files):
            raise AppError(404, 'ファイルが見つかりません')
        return {**files[index], 'content': read_text_file(files[index]['path'], root)}

    def check_decision(self, connection, item):
        if item.get('decision_id') and not connection.execute('SELECT 1 FROM decisions WHERE id=?', (item['decision_id'],)).fetchone():
            raise AppError(404, '関連する判断が見つかりません')

    def add_task(self, payload):
        self.refuse_lane(payload)
        object_fields(payload, ('id', 'title', 'kind', 'done_when'), ('request', 'pane', 'run', 'result', 'remaining', 'decision_id'))
        item = validate_task(payload)
        item = {'request': '', 'pane': '', 'run': '', 'result': '', 'remaining': '', 'decision_id': None, **item}
        with self.db() as connection:
            connection.execute('BEGIN IMMEDIATE')
            self.check_decision(connection, item)
            row = connection.execute('SELECT * FROM tasks WHERE id=?', (item['id'],)).fetchone()
            if row:
                if any(row[key] != item[key] for key in TASK_IDENTITY):
                    raise AppError(409, '同じ id に異なる task が登録されています')
                return dict(row)
            # 再送は既存を返す約束なので、規則の検査は新規に作る場合だけ行う (この規則より前の行への再送を断らない)
            check_task_rules(item['kind'], item['done_when'], item['decision_id'])
            now = utc_now()
            # 新しい task は todo で始まり、以後の lane は reconciler が写す
            names = ','.join(('id', *TASK_FIELDS, 'lane', 'created_at', 'updated_at'))
            connection.execute(f'INSERT INTO tasks({names}) VALUES({",".join("?" * (len(TASK_FIELDS) + 4))})',
                               (item['id'], *(item[key] for key in TASK_FIELDS), 'todo', now, now))
            return dict(connection.execute('SELECT * FROM tasks WHERE id=?', (item['id'],)).fetchone())

    @staticmethod
    def refuse_lane(payload):
        # 断る場所は CLI でなく Store: HTTP でもスクリプトでも、lane と machine を書く経路を reconcile_lane に絞る
        if isinstance(payload, dict) and ('lane' in payload or 'machine' in payload):
            raise AppError(400, LANE_REFUSED)

    def update_task(self, identifier, payload):
        self.refuse_lane(payload)
        object_fields(payload, (), TASK_FIELDS)
        if not payload:
            raise AppError(400, '更新する項目を指定してください')
        item = validate_task(payload)
        with self.db() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute('SELECT * FROM tasks WHERE id=?', (identifier,)).fetchone()
            if not row:
                raise AppError(404, 'task が見つかりません')
            self.check_decision(connection, item)
            # 規則に関わる項目を書き換えるときだけ、書き換え後の行で検査する。result などの更新は止めない
            if {'kind', 'done_when', 'decision_id'} & item.keys():
                after = {**dict(row), **item}
                check_task_rules(after['kind'], after['done_when'], after['decision_id'])
            columns = ''.join(f'{key}=?,' for key in item)
            connection.execute(f'UPDATE tasks SET {columns}updated_at=? WHERE id=?',
                               (*item.values(), timestamp_after(row['updated_at']), identifier))
            return dict(connection.execute('SELECT * FROM tasks WHERE id=?', (identifier,)).fetchone())

    def reconcile_lane(self, identifier, lane, machine='', expect=None, *, pane=None, expect_pane=None,
                       expect_machine=None, expect_run=None, expect_request=None, expect_kind=None,
                       expect_remaining=None, expect_decision_id=None, expect_updated_at=None):
        """reconciler が観測した値が現在も一致する場合だけ、指定された担当の欄をまとめて書く。"""
        for name, value, optional in (('lane', lane, False), ('--from', expect, True)):
            if value not in LANES and not (optional and value is None):
                raise AppError(400, f'{name} は ' + ' / '.join(LANES) + ' のいずれかを指定してください')
        machine = text(machine, 'machine', TASK_LIMITS['machine'], empty=True)
        if pane is not None:
            text(pane, 'pane', TASK_LIMITS['pane'], empty=True)
            if any(value is None for value in (expect, expect_pane, expect_machine)):
                raise AppError(400, '--pane には --from、--from-pane、--from-machine が必要です')
        scope = (expect_run, expect_request, expect_kind)
        if scope != (None, None, None) and None in scope:
            raise AppError(400, '--from-run、--from-request、--from-kind は一緒に指定してください')
        if expect_kind is not None and expect_kind not in (*TASK_KINDS, ''):
            raise AppError(400, '--from-kind は ' + ' / '.join(TASK_KINDS) + ' または空文字を指定してください')
        conditions = {'lane': expect, 'pane': expect_pane, 'machine': expect_machine,
                      'run': expect_run, 'request': expect_request, 'kind': expect_kind}
        for key in ('pane', 'machine', 'run', 'request'):
            if conditions[key] is not None:
                text(conditions[key], '--from-' + key, TASK_LIMITS[key], empty=True)
        conditions = {key: value for key, value in conditions.items() if value is not None}
        snapshot = (expect_remaining, expect_decision_id, expect_updated_at)
        if snapshot != (None, None, None):
            if None in snapshot:
                raise AppError(400, '--from-remaining、--from-decision-id、--from-updated-at は一緒に指定してください')
            conditions.update(validate_task({'remaining': expect_remaining, 'decision_id': expect_decision_id}))
            conditions['updated_at'] = text(expect_updated_at, '--from-updated-at', 64)
        where = 'id=?' + ''.join(f' AND {key} IS ?' for key in conditions)
        if expect_remaining is not None and lane == 'done':
            # 紐付いた判断の状態は task の更新時刻と独立して変わる。
            where += ''' AND (decision_id IS NULL OR EXISTS (
                SELECT 1 FROM decisions WHERE id=tasks.decision_id AND (status='completed' OR EXISTS (
                    SELECT 1 FROM decision_changes WHERE decision_id=tasks.decision_id AND event='withdraw'
                ))
            ))'''
        params = (identifier, *conditions.values())
        assignment = {'lane': lane, 'machine': machine}
        if pane is not None:
            assignment['pane'] = pane
        columns = ','.join(f'{key}=?' for key in assignment)
        with self.db() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute(f'SELECT * FROM tasks WHERE {where}', params).fetchone()
            if not row:
                current = connection.execute('SELECT lane FROM tasks WHERE id=?', (identifier,)).fetchone()
                if not current:
                    raise AppError(404, 'task が見つかりません')
                if conditions.keys() == {'lane'}:
                    raise LaneConflict(409, f'lane が違うため書きませんでした (今は {current["lane"]}、--from は {expect})')
                raise LaneConflict(409, 'task が比較条件と一致しないため書きませんでした')
            # 再試行で同じ担当を書き込む場合は活動時刻を進めない。
            if pane is not None and all(row[key] == value for key, value in assignment.items()):
                return dict(row)
            connection.execute(f'UPDATE tasks SET {columns},updated_at=? WHERE {where}',
                               (*assignment.values(), timestamp_after(row['updated_at']), *params))
            return dict(connection.execute('SELECT * FROM tasks WHERE id=?', (identifier,)).fetchone())

    def delete_task(self, identifier):
        with self.db() as connection:
            if not connection.execute('DELETE FROM tasks WHERE id=?', (identifier,)).rowcount:
                raise AppError(404, 'task が見つかりません')
        return {'deleted': identifier}

    def open_run(self, payload):
        object_fields(payload, ('run_id', 'role', 'pane', 'socket'), ())
        # text() でサロゲートも断る。socket のパスに decode できないバイトがあると SQLite が bind できない
        for key, value in payload.items():
            text(value, key, 200)
        with self.db() as connection:
            connection.execute('BEGIN IMMEDIATE')
            if connection.execute('SELECT 1 FROM runs WHERE run_id=? AND role=? AND closed_at IS NULL', (payload['run_id'], payload['role'])).fetchone():
                raise AppError(409, '同じ run_id と role の開いた run があります')
            cursor = connection.execute('INSERT INTO runs(run_id,role,pane,socket,opened_at) VALUES(?,?,?,?,?)',
                                        (payload['run_id'], payload['role'], payload['pane'], payload['socket'], utc_now()))
            return dict(connection.execute('SELECT * FROM runs WHERE id=?', (cursor.lastrowid,)).fetchone())

    def close_run(self, run_id, role):
        text(run_id, 'run_id', 200)
        text(role, 'role', 200)
        with self.db() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute('SELECT id FROM runs WHERE run_id=? AND role=? AND closed_at IS NULL', (run_id, role)).fetchone()
            if not row:
                raise AppError(404, '開いた run が見つかりません')
            connection.execute('UPDATE runs SET closed_at=? WHERE id=?', (utc_now(), row['id']))
            return dict(connection.execute('SELECT * FROM runs WHERE id=?', (row['id'],)).fetchone())

    def open_run_ids(self, pane, socket):
        # 長さの検査をする runs() と違い、env の値をそのまま引く (長すぎる値は行に当たらないだけ)
        with self.db() as connection:
            return sorted({row['run_id'] for row in connection.execute('SELECT run_id FROM runs WHERE pane=? AND socket=? AND closed_at IS NULL', (pane, socket))})

    def runs(self, run_id=None, pane=None, socket=None, include_closed=False):
        if run_id is None and (pane is None or socket is None):
            raise AppError(400, '--run-id か、--pane と --socket の組を指定してください')
        if run_id is not None and (pane is not None or socket is not None):
            raise AppError(400, '--run-id と --pane / --socket は同時に指定できません')
        for name, value in (('run_id', run_id), ('pane', pane), ('socket', socket)):
            if value is not None:
                text(value, name, 200)
        where, values = ('run_id=?', (run_id,)) if run_id is not None else ('pane=? AND socket=?', (pane, socket))
        with self.db() as connection:
            rows = connection.execute(f"SELECT * FROM runs WHERE {where}{'' if include_closed else ' AND closed_at IS NULL'} ORDER BY id", values)
            return {'runs': [dict(row) for row in rows]}

    def open_session(self, payload):
        object_fields(payload, ('run_id', 'role', 'pane', 'socket', 'cli', 'native_session_id'), ('req_id',))
        run_id = text(payload['run_id'], 'run_id', 200)
        role = text(payload['role'], 'role', 64)
        if role not in SESSION_ROLES:
            raise AppError(400, 'role は ' + ' / '.join(SESSION_ROLES) + ' のいずれかを指定してください')
        req_id = text(payload.get('req_id', ''), 'req_id', 200, empty=True)
        pane = text(payload['pane'], 'pane', 200)
        socket = text(payload['socket'], 'socket', 200)
        cli = text(payload['cli'], 'cli', 64)
        if cli not in SESSION_CLIS:
            raise AppError(400, 'cli は ' + ' / '.join(SESSION_CLIS) + ' のいずれかを指定してください')
        native_session_id = text(payload['native_session_id'], 'native_session_id', 200)
        with self.db() as connection:
            connection.execute('BEGIN IMMEDIATE')
            if connection.execute('SELECT 1 FROM sessions WHERE cli=? AND native_session_id=? AND closed_at IS NULL', (cli, native_session_id)).fetchone():
                raise AppError(409, '同じ cli と native_session_id の開いた session があります')
            if connection.execute('SELECT 1 FROM sessions WHERE run_id=? AND role=? AND pane=? AND socket=? AND closed_at IS NULL', (run_id, role, pane, socket)).fetchone():
                raise AppError(409, '同じ run_id・role・pane・socket の開いた session があります')
            try:
                cursor = connection.execute(
                    'INSERT INTO sessions(run_id,role,req_id,pane,socket,cli,native_session_id,opened_at) VALUES(?,?,?,?,?,?,?,?)',
                    (run_id, role, req_id, pane, socket, cli, native_session_id, utc_now()),
                )
            except sqlite3.IntegrityError:
                raise AppError(409, '同じ条件の開いた session があります') from None
            return dict(connection.execute('SELECT * FROM sessions WHERE id=?', (cursor.lastrowid,)).fetchone())

    def close_session(self, *, cli=None, native_session_id=None, run_id=None, pane=None, socket=None, role=None, reason=None):
        reason = text(reason, 'reason', 64)
        if reason not in SESSION_CLOSE_REASONS:
            raise AppError(400, 'reason は ' + ' / '.join(SESSION_CLOSE_REASONS) + ' のいずれかを指定してください')
        has_native = cli is not None or native_session_id is not None
        has_slot = any(value is not None for value in (run_id, pane, socket, role))
        if has_native and has_slot:
            raise AppError(400, '--session-id / --cli と --pane / --socket / --run-id は同時に指定できません')
        if has_native:
            if cli is None or native_session_id is None:
                raise AppError(400, '--session-id と --cli は一緒に指定してください')
            cli = text(cli, 'cli', 64)
            if cli not in SESSION_CLIS:
                raise AppError(400, 'cli は ' + ' / '.join(SESSION_CLIS) + ' のいずれかを指定してください')
            native_session_id = text(native_session_id, 'native_session_id', 200)
            where, params = 'cli=? AND native_session_id=?', (cli, native_session_id)
        elif has_slot:
            if pane is None or socket is None or run_id is None:
                raise AppError(400, '--pane・--socket・--run-id は一緒に指定してください')
            run_id = text(run_id, 'run_id', 200)
            pane = text(pane, 'pane', 200)
            socket = text(socket, 'socket', 200)
            if role is not None:
                role = text(role, 'role', 64)
                if role not in SESSION_ROLES:
                    raise AppError(400, 'role は ' + ' / '.join(SESSION_ROLES) + ' のいずれかを指定してください')
                where, params = 'run_id=? AND role=? AND pane=? AND socket=?', (run_id, role, pane, socket)
            else:
                where, params = 'run_id=? AND pane=? AND socket=?', (run_id, pane, socket)
        else:
            raise AppError(400, '--session-id と --cli の組か、--pane・--socket・--run-id の組を指定してください')
        with self.db() as connection:
            connection.execute('BEGIN IMMEDIATE')
            rows = connection.execute(f'SELECT id FROM sessions WHERE {where} AND closed_at IS NULL', params).fetchall()
            if not rows:
                raise AppError(404, '開いた session が見つかりません')
            if len(rows) > 1:
                raise AppError(409, '複数の開いた session が一致します。--role か --session-id と --cli で絞り込んでください')
            target_id = rows[0]['id']
            connection.execute('UPDATE sessions SET closed_at=?, close_reason=? WHERE id=? AND closed_at IS NULL',
                               (utc_now(), reason, target_id))
            return dict(connection.execute('SELECT * FROM sessions WHERE id=?', (target_id,)).fetchone())

    def sample_session(self, session_id, observed_at, *, turn_count=None, input_tokens=None):
        if type(session_id) is not int or not 1 <= session_id < 2**63:
            raise AppError(400, 'id は正の SQLite 整数を指定してください')
        if turn_count is None and input_tokens is None:
            raise AppError(400, 'turn_count または input_tokens を指定してください')
        for name, value in (('turn_count', turn_count), ('input_tokens', input_tokens)):
            if value is not None and (type(value) is not int or not 0 <= value < 2**63):
                raise AppError(400, f'{name} は非負の SQLite 整数を指定してください')
        observed = parse_iso_timestamp(observed_at, 'observed_at')
        with self.db() as connection:
            connection.execute('BEGIN IMMEDIATE')
            session = connection.execute('SELECT opened_at, closed_at FROM sessions WHERE id=?', (session_id,)).fetchone()
            if session is None:
                raise AppError(404, 'session が見つかりません')
            if session['closed_at'] is not None and observed > parse_iso_timestamp(session['closed_at'], 'closed_at'):
                raise AppError(409, 'observed_at が session の終了時刻より後です')
            if observed > parse_iso_timestamp(utc_now(), 'now'):
                raise AppError(400, 'observed_at が現在時刻より後です')
            age = observed - parse_iso_timestamp(session['opened_at'], 'opened_at')
            elapsed = max(0, age.days * 86400 + age.seconds)
            connection.execute('''UPDATE sessions SET
                turn_count=COALESCE(MAX(turn_count, :turns), turn_count, :turns),
                input_tokens=COALESCE(MAX(input_tokens, :tokens), input_tokens, :tokens),
                elapsed_seconds=MAX(COALESCE(elapsed_seconds, 0), :elapsed)
                WHERE id=:id''', {'turns': turn_count, 'tokens': input_tokens, 'elapsed': elapsed, 'id': session_id})
            return dict(connection.execute('SELECT * FROM sessions WHERE id=?', (session_id,)).fetchone())

    def sessions(self, *, run_id=None, cli=None, open_only=False, since=None, until=None):
        clauses = []
        params = []
        if run_id is not None:
            clauses.append('run_id=?')
            params.append(text(run_id, 'run_id', 200))
        if cli is not None:
            cli = text(cli, 'cli', 64)
            if cli not in SESSION_CLIS:
                raise AppError(400, 'cli は ' + ' / '.join(SESSION_CLIS) + ' のいずれかを指定してください')
            clauses.append('cli=?')
            params.append(cli)
        if open_only:
            clauses.append('closed_at IS NULL')
        since_dt = parse_iso_timestamp(since, 'since') if since is not None else None
        until_dt = parse_iso_timestamp(until, 'until') if until is not None else None
        if since_dt is not None and until_dt is not None and since_dt > until_dt:
            raise AppError(400, 'since は until 以下を指定してください')
        where = (' WHERE ' + ' AND '.join(clauses)) if clauses else ''
        with self.db() as connection:
            rows = [dict.fromkeys(SESSION_METRICS) | dict(row)
                    for row in connection.execute(f'SELECT * FROM sessions{where} ORDER BY id', params)]
        if since_dt is not None or until_dt is not None:
            filtered = []
            for row in rows:
                opened_dt = datetime.fromisoformat(row['opened_at']).astimezone(timezone.utc)
                closed_dt = datetime.fromisoformat(row['closed_at']).astimezone(timezone.utc) if row['closed_at'] is not None else None
                if since_dt is not None and closed_dt is not None and closed_dt < since_dt:
                    continue
                if until_dt is not None and opened_dt > until_dt:
                    continue
                filtered.append(row)
            rows = filtered
        return {'sessions': rows}

    def _message_fields(self, payload):
        object_fields(payload, ('run_id', 'msg', 'state'), ('wait', 'req'))
        text(payload['run_id'], 'run_id', 200)
        text(payload['msg'], 'msg', 200)
        found = MSG_NAME.fullmatch(payload['msg'])
        if not found:
            raise AppError(400, 'msg の形が不正です')
        if payload['state'] not in MSG_STATES:
            raise AppError(400, 'state は ' + ' / '.join(MSG_STATES) + ' のいずれかを指定してください')
        wait = payload.get('wait')
        if (payload['state'] == 'held') != (wait is not None):
            raise AppError(400, 'wait は state が held のときだけ必要です')
        if wait is not None and (len(text(wait, 'wait', 500)) > 500 or not MSG_WAIT.fullmatch(wait)):
            raise AppError(400, 'wait は decision= / pr= / msg= の形で指定してください')
        req = payload.get('req')
        if req is not None and (len(text(req, 'req', 200)) > 200 or not MSG_REQ.fullmatch(req)):
            raise AppError(400, 'req の形が不正です')
        return found.group(1), found.group(2)

    def import_message(self, payload):
        tag, sender = self._message_fields(payload)
        now = utc_now()
        with self.db() as connection:
            connection.execute('BEGIN IMMEDIATE')
            cursor = connection.execute('INSERT INTO messages(run_id,msg,tag,from_pane,req,state,wait,received_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(run_id,msg) DO NOTHING',
                                        (payload['run_id'], payload['msg'], tag, sender, payload.get('req'), payload['state'], payload.get('wait'), now, now))
            row = connection.execute('SELECT * FROM messages WHERE run_id=? AND msg=?', (payload['run_id'], payload['msg'])).fetchone()
            return {'imported': bool(cursor.rowcount), 'message': dict(row)}

    def _upsert_message(self, connection, payload, tags, now, *, touch=True):
        tag, sender = tags
        connection.execute('''INSERT INTO messages(run_id,msg,tag,from_pane,req,state,wait,received_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(run_id,msg) DO UPDATE SET state=excluded.state, wait=excluded.wait, updated_at=excluded.updated_at, req=COALESCE(excluded.req, req)
            WHERE ? OR state IS NOT excluded.state OR wait IS NOT excluded.wait OR req IS NOT COALESCE(excluded.req, req)''',
                            (payload['run_id'], payload['msg'], tag, sender, payload.get('req'), payload['state'], payload.get('wait'), now, now, touch))

    def move_message(self, payload):
        tags = self._message_fields(payload)
        now = utc_now()
        with self.db() as connection:
            connection.execute('BEGIN IMMEDIATE')
            self._upsert_message(connection, payload, tags, now)
            return dict(connection.execute('SELECT * FROM messages WHERE run_id=? AND msg=?', (payload['run_id'], payload['msg'])).fetchone())

    def sync_messages(self, run_id, inbox):
        text(run_id, 'run_id', 200)
        with self.db() as connection:
            # 読み取りの前に lock を取り、先に走査した同期が後から新しい結果を上書きするのを防ぐ。
            connection.execute('BEGIN IMMEDIATE')
            messages = inbox_messages(inbox)
            now = utc_now()
            for msg, (tags, fields) in messages.items():
                self._upsert_message(connection, {'run_id': run_id, 'msg': msg, **fields}, tags, now, touch=False)
            active = connection.execute("SELECT id,msg FROM messages WHERE run_id=? AND state!='done'", (run_id,))
            done_ids = [(now, row['id']) for row in active if row['msg'] not in messages]
            connection.executemany("UPDATE messages SET state='done', wait=NULL, updated_at=? WHERE id=?", done_ids)
            return {'targets': len(messages), 'done': len(done_ids)}

    def messages(self, run_id, state=None):
        text(run_id, 'run_id', 200)
        if state is not None and state not in MSG_STATES:
            raise AppError(400, 'state は ' + ' / '.join(MSG_STATES) + ' のいずれかを指定してください')
        with self.db() as connection:
            rows = connection.execute('SELECT * FROM messages WHERE run_id=?' + ('' if state is None else ' AND state=?') + ' ORDER BY id', (run_id,) if state is None else (run_id, state))
            return {'messages': [dict(row) for row in rows]}

    def affiliation(self):
        """所属記録の観測用に runs と sessions の全行を返す (Board の所属マップと run タイムライン)。"""
        with self.db() as connection:
            runs = [dict(row) for row in connection.execute('SELECT * FROM runs ORDER BY id')]
            sessions = [dict(row) for row in connection.execute('SELECT * FROM sessions ORDER BY id')]
        return {'runs': runs, 'sessions': sessions}

    def posts(self, task_id=None):
        with self.db() as connection:
            if task_id is None:
                rows = connection.execute('SELECT * FROM posts ORDER BY id')
            else:
                rows = connection.execute('SELECT * FROM posts WHERE task_id=? ORDER BY id', (task_id,))
            return {'posts': [dict(row) for row in rows]}

    def unread(self):
        """未読は新しい仕組みではなく既存状態の表示: 未受領の配送試行と未回答の判断待ちの件数 (設計 Q9)。"""
        with self.db() as connection:
            posts = connection.execute("SELECT COUNT(*) FROM posts WHERE state='unreceived'").fetchone()[0]
        decisions = sum(1 for item in self.list() if item['answer'] is None)
        return {'posts': posts, 'decisions': decisions}

    def create_post(self, task_id, payload):
        """依頼 thread への人の投稿を配送試行として記録する (設計 Q6 / Q12 / Q13)。

        分類は投稿時点の lane が人待ちかどうかだけから導き、投稿者は選ばない。宛先は
        依頼の担当 pane (tasks.pane) を引き、同じ run の所属記録でその pane に開いている
        worker / cue_worker session が一意に定まる場合だけ、その写しを記録する。
        担当 pane が空・該当なし・複数ある場合は宛先なしで、未受領のまま残る。
        状態遷移の性質は proofs/DeliveryAttempt.lean で検査済み (宛先の導出規則は
        モデルの外。モデルは宛先を与えられるものとして扱う)。
        """
        object_fields(payload, ('body',), ())
        body = text(payload['body'], 'body', 4000)
        with self.db() as connection:
            connection.execute('BEGIN IMMEDIATE')
            task = connection.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
            if not task:
                raise AppError(404, 'task が見つかりません')
            classification = 'expected' if task['lane'] in WAITING_LANES else 'unexpected'
            target = {'target_run': '', 'target_pane': '', 'target_socket': '', 'target_session': ''}
            if task['run'] and task['pane']:
                sessions = connection.execute(
                    "SELECT * FROM sessions WHERE run_id=? AND pane=? AND role IN ('worker','cue_worker') AND closed_at IS NULL ORDER BY id",
                    (task['run'], task['pane'])).fetchall()
                if len(sessions) == 1:
                    session = sessions[0]
                    target = {'target_run': session['run_id'], 'target_pane': session['pane'],
                              'target_socket': session['socket'], 'target_session': session['native_session_id']}
            cursor = connection.execute(
                'INSERT INTO posts(task_id,body,classification,state,target_run,target_pane,target_socket,target_session,created_at) VALUES(?,?,?,?,?,?,?,?,?)',
                (task_id, body, classification, 'unreceived', target['target_run'], target['target_pane'],
                 target['target_socket'], target['target_session'], utc_now()))
            return dict(connection.execute('SELECT * FROM posts WHERE id=?', (cursor.lastrowid,)).fetchone())

    def receive_post(self, post_id, payload):
        """配送試行の受領を記録する。完全 Pull 配送側が宛先 worker の受領を確認したときに呼ぶ接続点で、
        worker inbox ファイルへの実体化は Cue の外にある。受領済みへの再受領は 409
        (proofs/DeliveryAttempt.lean の receivedIsTerminal)。"""
        object_fields(payload, (), ('received_by',))
        received_by = text(payload.get('received_by', ''), 'received_by', 200, empty=True)
        with self.db() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute('SELECT * FROM posts WHERE id=?', (post_id,)).fetchone()
            if not row:
                raise AppError(404, '配送試行が見つかりません')
            if row['state'] != 'unreceived':
                raise AppError(409, 'この配送試行は受領済みです')
            connection.execute("UPDATE posts SET state='received', received_at=?, received_by=? WHERE id=? AND state='unreceived'",
                               (utc_now(), received_by, post_id))
            return dict(connection.execute('SELECT * FROM posts WHERE id=?', (post_id,)).fetchone())

    def thread(self, task_id):
        """依頼 thread の材料を返す。task と、その run の所属記録 (sessions)、窓口が受けた
        msg (本文は持たずメタデータと状態だけ)、この task への投稿 (配送試行) を分けて返し、
        時系列への混合は表示側が行う (設計 Q7 / Q14)。判断は tasks() と同じ要約の形。"""
        with self.db() as connection:
            row = connection.execute('''SELECT tasks.*, decisions.payload AS decision_payload, decisions.choice AS decision_choice,
                EXISTS(SELECT 1 FROM decision_changes WHERE decision_id=tasks.decision_id AND event='withdraw') AS decision_withdrawn
                FROM tasks LEFT JOIN decisions ON decisions.id=tasks.decision_id WHERE tasks.id=?''', (task_id,)).fetchone()
            if not row:
                raise AppError(404, 'task が見つかりません')
            task = dict(row)
            payload, choice, withdrawn = task.pop('decision_payload'), task.pop('decision_choice'), task.pop('decision_withdrawn')
            task['decision'] = {'title': json.loads(payload)['title'], 'answered': choice is not None, 'withdrawn': bool(withdrawn)} if payload else None
            messages, sessions = [], []
            if task['run']:
                messages = [dict(item) for item in connection.execute('SELECT * FROM messages WHERE run_id=? ORDER BY id', (task['run'],))]
                sessions = [dict(item) for item in connection.execute('SELECT * FROM sessions WHERE run_id=? ORDER BY id', (task['run'],))]
            posts = [dict(item) for item in connection.execute('SELECT * FROM posts WHERE task_id=? ORDER BY id', (task_id,))]
        return {'task': task, 'messages': messages, 'posts': posts, 'sessions': sessions}

    def tasks(self, lane=None):
        if lane is not None and lane not in LANES:
            raise AppError(400, 'lane は ' + ' / '.join(LANES) + ' のいずれかを指定してください')
        with self.db() as connection:
            rows = [dict(row) for row in connection.execute('''SELECT tasks.*, decisions.payload AS decision_payload, decisions.choice AS decision_choice,
                EXISTS(SELECT 1 FROM decision_changes WHERE decision_id=tasks.decision_id AND event='withdraw') AS decision_withdrawn
                FROM tasks LEFT JOIN decisions ON decisions.id=tasks.decision_id ORDER BY tasks.created_at,tasks.id''')]
        # web / CLI / TUI が同じ形で判断の題名と回答の有無を出せるよう、ここで 1 度だけ引く
        for row in rows:
            payload, choice, withdrawn = row.pop('decision_payload'), row.pop('decision_choice'), row.pop('decision_withdrawn')
            row['decision'] = {'title': json.loads(payload)['title'], 'answered': choice is not None, 'withdrawn': bool(withdrawn)} if payload else None
        order = list(LANES)
        rows = [row for row in rows if lane is None or row['lane'] == lane]
        return sorted(rows, key=lambda row: order.index(row['lane']) if row['lane'] in order else len(order))

    def claim(self, worker, lease_seconds=300, key_prefix=None, on_claim=None):
        text(worker, 'worker', 200)
        validate_lease(lease_seconds)
        clause, params = prefix_clause(key_prefix)
        token = secrets.token_urlsafe(32)
        with self.db() as connection:
            connection.execute('BEGIN IMMEDIATE')
            now = time.time()
            expires = now + lease_seconds
            row = connection.execute("SELECT * FROM decisions WHERE (status='pending' OR (status='processing' AND lease_until<=?))" + NOT_WITHDRAWN + clause + " ORDER BY decided_at,id LIMIT 1", (now, *params)).fetchone()
            if row is None:
                return {'decision': None}
            connection.execute("UPDATE decisions SET status='processing',worker=?,token=?,lease_until=? WHERE id=?", (worker, token, expires, row['id']))
            result = {'decision': self.fetch(connection, row['id']), 'token': token, 'worker': worker, 'expires_at': datetime.fromtimestamp(expires, timezone.utc).isoformat()}
            # commit の前に呼ぶ。on_claim が失敗すれば claim ごと rollback され、token を失った claim が残らない
            if on_claim:
                on_claim(result)
                # 書き込みに lease 以上かかると、成功を返しても complete / renew が token を断る claim になる
                if time.time() >= expires:
                    raise AppError(409, 'token ファイルの書き込み中に lease が切れました')
        # 読み取りロックで commit が待たされる間に失効した行は、次の claim で再取得できる
        if time.time() >= expires:
            raise AppError(409, 'claim は保存しましたが lease は既に切れています。この token は使えません')
        return result

    def report(self, identifier, payload):
        object_fields(payload, ('token', 'status', 'reason', 'next_step'))
        if payload['status'] not in ('blocked', 'failed'):
            raise AppError(400, '報告状態は blocked または failed を指定してください')
        reason = text(payload['reason'], 'reason', 4000)
        next_step = text(payload['next_step'], 'next_step', 4000)
        with self.db() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute('SELECT * FROM decisions WHERE id=?', (identifier,)).fetchone()
            self.active(row, payload['token'])
            connection.execute('UPDATE decisions SET status=?,reason=?,next_step=?,reported_at=?,token=NULL,lease_until=NULL WHERE id=?',
                               (payload['status'], reason, next_step, self.stamp(connection, identifier), identifier))
            self.record(connection, connection.execute('SELECT * FROM decisions WHERE id=?', (identifier,)).fetchone(), 'report')
        return self.get(identifier)

    def retry(self, identifier, payload):
        return self._update_stopped(identifier, payload, 'retry', '再試行できない',
                                    "UPDATE decisions SET status='pending',reason='',next_step='',reported_at=NULL WHERE id=?")

    def close(self, identifier, payload):
        # 終端は completed を流用する。status の CHECK を変えると旧 release が開けなくなるため。
        # 閉じた判断は reason / reported_at が残ったままの completed で、通常の完了 (reason 空) と見分けられる。
        return self._update_stopped(identifier, payload, 'close', '閉じられない',
                                    "UPDATE decisions SET status='completed' WHERE id=?")

    def _update_stopped(self, identifier, payload, event, invalid_state, update_sql):
        object_fields(payload, ('decided_at', 'reported_at'))
        decided_at = text(payload['decided_at'], 'decided_at', 64)
        reported_at = text(payload['reported_at'], 'reported_at', 64)
        with self.db() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute('SELECT * FROM decisions WHERE id=?', (identifier,)).fetchone()
            self.public(row, [])
            if row['status'] not in ('blocked', 'failed') or row['decided_at'] != decided_at or row['reported_at'] != reported_at:
                raise AppError(409, f'停止理由が変更済み、または{invalid_state}状態です')
            self.record(connection, row, event)
            connection.execute(update_sql, (identifier,))
        return self.get(identifier)

    def renew(self, identifier, payload, on_renew=None):
        object_fields(payload, ('token',), ('lease_seconds',))
        seconds = validate_lease(payload.get('lease_seconds', 300))
        with self.db() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute('SELECT * FROM decisions WHERE id=?', (identifier,)).fetchone()
            now = self.active(row, payload['token'])
            expires = max(row['lease_until'], now + seconds)
            connection.execute('UPDATE decisions SET lease_until=? WHERE id=?', (expires, identifier))
            result = {'decision': self.fetch(connection, identifier), 'expires_at': datetime.fromtimestamp(expires, timezone.utc).isoformat()}
            # commit の前に呼ぶ。書き込みロックの中なので complete / report / 他の renew とファイル操作の順序が DB と揃う
            if on_renew:
                on_renew(result)
                # claim と同じく、ファイル更新に lease 以上かかったら延長を成功扱いにしない
                if time.time() >= expires:
                    raise AppError(409, 'token ファイルの更新中に lease が切れました')
        # commit は読み手のロック解放を busy timeout まで待つことがあり、その間に短い lease は切れうる
        if time.time() >= expires:
            raise AppError(409, '延長は保存しましたが lease は既に切れています。この token は使えません')
        return result

    def lease_of(self, identifier, token):
        """claims_dir の掃除用。この DB に無い判断は None、あれば token が一致する claim の lease_until (無ければ 0)"""
        with self.db() as connection:
            row = connection.execute('SELECT token, lease_until FROM decisions WHERE id=?', (identifier,)).fetchone()
        if row is None:
            return None
        matches = row['token'] and token.isascii() and secrets.compare_digest(row['token'], token)
        return (row['lease_until'] or 0) if matches else 0

    def holder(self, identifier, token):
        # claims の active 判定。ファイルの expires_at でなく DB の現在の token と lease で決める
        with self.db() as connection:
            row = connection.execute('SELECT * FROM decisions WHERE id=?', (identifier,)).fetchone()
        try:
            self.active(row, token)
        except AppError:
            return False
        return True

    def complete(self, identifier, token):
        with self.db() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute('SELECT * FROM decisions WHERE id=?', (identifier,)).fetchone()
            self.active(row, token)
            connection.execute("UPDATE decisions SET status='completed',token=NULL,lease_until=NULL WHERE id=?", (identifier,))
        return self.get(identifier)

def seed_demo(store):
    names = [
        ('レビュー指摘にどう対応するか', '開発チーム', 'urgent', '共通化を今回の修正に含めるか決めてください。', 'レビューで 3 か所の重複が指摘されました。明日のリリースへの影響を抑えたい状況です。'),
        ('段階的な公開の範囲を決める', 'プロダクトチーム', 'normal', '最初に公開する利用者の範囲を決めてください。', '基本動作の確認は完了しています。大量データの操作感は未確認です。'),
        ('導入ガイドの見せ方を選ぶ', 'サポートチーム', 'later', '詳しい手順をどこに置くか決めてください。', '問い合わせの多い手順は 4 ステップです。'),
        ('出力ファイルの既定形式を選ぶ', '分析チーム', 'normal', '既存の連携を踏まえて既定形式を決めてください。', '自動処理で利用するお客様がいます。')
    ]
    added = []
    for index, (title, source, priority, reason, background) in enumerate(names):
        item = store.add({'title': title, 'source': source, 'priority': priority, 'reason': reason, 'background': background, 'urgency_reason': '修正方針が決まるまでレビューが停止中' if index == 0 else '', 'options': [{'id': 'minimal', 'title': '影響範囲を限定して進める', 'benefits': '確認範囲を絞れる', 'concerns': '追加作業が残る'}, {'id': 'full', 'title': '今回まとめて対応する', 'benefits': '残作業を減らせる', 'concerns': '公開が遅れる可能性'}], 'recommended': 'minimal', 'recommendation': '現在の作業への影響を限定するため。', 'links': [{'url': 'https://github.com/example/repo/pull/1', 'label': '対象の PR (デモ)', 'kind': 'pr'}, {'url': 'https://github.com/example/repo/issues/2', 'label': '関連 issue (デモ)', 'kind': 'issue'}] if index == 0 else [], 'image': {'url': '/assets/demo-flow.svg', 'alt': '必要な修正を確認し、公開後に共通化を進める流れ'} if index == 0 else None})
        added.append(item)
        if index == 3:
            store.answer(item['id'], {'choice': 'minimal', 'memo': '既存の連携を優先'})
            store.claim('demo-worker', 300)
    # Board (所属マップ・依頼 thread・配送試行) のデモ。1 件目の判断に依頼を紐付け、同じ run に窓口と worker の所属を載せる
    store.open_run({'run_id': 'run-demo-1', 'role': 'frontdesk', 'pane': 'w1:p1', 'socket': '/tmp/demo.sock'})
    store.open_session({'run_id': 'run-demo-1', 'role': 'frontdesk', 'pane': 'w1:p1', 'socket': '/tmp/demo.sock', 'cli': 'claude', 'native_session_id': 'uuid-demo-frontdesk'})
    store.open_session({'run_id': 'run-demo-1', 'role': 'worker', 'req_id': 'demo-decision', 'pane': 'w1:p2', 'socket': '/tmp/demo.sock', 'cli': 'claude', 'native_session_id': 'uuid-demo-worker'})
    task = store.add_task({'id': 'demo-decision', 'title': 'レビュー指摘への対応判断', 'kind': 'decision', 'done_when': '判断への回答が記録される', 'request': '窓口 run-demo-1', 'pane': 'w1:p2', 'run': 'run-demo-1', 'decision_id': added[0]['id']})
    store.reconcile_lane(task['id'], 'human_decision')
    store.import_message({'run_id': 'run-demo-1', 'msg': '1-1730000000000000-report-w1:p1-ab12.msg', 'state': 'held', 'wait': f"decision={added[0]['id']}", 'req': 'demo-decision'})
    store.create_post('demo-decision', {'body': '判断待ちの件、確認しました。推奨どおり影響範囲を限定する方向でお願いします。'})
    other = store.add_task({'id': 'demo-implement', 'title': '所属記録の読み取り API を実装する', 'kind': 'implement', 'done_when': 'https://github.com/example/repo/pull/42 が merge される', 'request': '窓口 run-demo-1', 'pane': 'w1:p2', 'run': 'run-demo-1'})
    store.reconcile_lane(other['id'], 'worker')
    store.create_post('demo-implement', {'body': '進み具合を教えてください。'})


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True

    def server_close(self):
        super().server_close()
        if getattr(self, 'demo_directory', None):
            self.demo_directory.cleanup()


class Handler(BaseHTTPRequestHandler):
    server_version = 'DecisionInbox/1.0'

    def end_headers(self):
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' http: https:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        super().end_headers()

    def json_response(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def check_host(self):
        port = self.server.server_port
        hosts = {f'127.0.0.1:{port}', f'localhost:{port}'}
        if port == 80:
            hosts |= {'127.0.0.1', 'localhost'}
        values = self.headers.get_all('Host', [])
        if len(values) != 1 or values[0] not in hosts:
            raise AppError(403, '許可されていない Host です')
        if self.headers.get('Sec-Fetch-Site') not in (None, 'same-origin', 'none'):
            raise AppError(403, '同一 origin から操作してください')
        origins = self.headers.get_all('Origin', [])
        if origins and (len(origins) != 1 or origins[0] != 'http://' + values[0]):
            raise AppError(403, '同一 origin から操作してください')

    def body(self):
        if self.headers.get('Transfer-Encoding') is not None:
            raise AppError(400, 'Transfer-Encoding は対応していません')
        if len(self.headers.get_all('Content-Type', [])) != 1 or self.headers.get_content_type() != 'application/json':
            raise AppError(415, 'Content-Type は application/json を指定してください')
        lengths = self.headers.get_all('Content-Length', [])
        if len(lengths) != 1 or len(lengths[0]) > 10 or not re.fullmatch(r'[0-9]+', lengths[0]):
            raise AppError(400, 'Content-Length が不正です')
        size = int(lengths[0])
        if size > MAX_BODY:
            raise AppError(413, '送信サイズが上限を超えています')
        raw = self.rfile.read(size)
        if len(raw) != size:
            raise AppError(400, 'JSON の受信が完了していません')
        return parse_json(raw)

    def handle_action(self):
        try:
            self.connection.settimeout(10)
            self.check_host()
            if not self.path.startswith('/') or self.path.startswith('//'):
                raise AppError(404, '見つかりません')
            path = urlsplit(self.path).path
            store = self.server.store
            if self.command == 'GET':
                if path == '/api/decisions':
                    return self.json_response(200, {'decisions': store.list(), 'demo': self.server.demo})
                if path == '/api/tasks':
                    # 書き込みは CLI に一本化しているので、HTTP は一覧の GET だけを出す
                    return self.json_response(200, task_lanes(store))
                if path == '/api/board':
                    board = store.affiliation()
                    board['posts'] = store.posts()['posts']
                    board['unread'] = store.unread()
                    board['demo'] = self.server.demo
                    return self.json_response(200, board)
                match = re.fullmatch(r'/api/tasks/(.+)/thread', path)
                if match:
                    return self.json_response(200, store.thread(unquote(match[1])))
                match = re.fullmatch(r'/api/decisions/([a-f0-9]{32})', path)
                if match:
                    return self.json_response(200, store.get(match[1]))
                match = re.fullmatch(r'/api/decisions/([a-f0-9]{32})/files/([0-9]{1,2})', path)
                if match:
                    return self.json_response(200, store.file(match[1], int(match[2]), self.server.files_root))
                if path in ASSETS:
                    filename, content_type = ASSETS[path]
                    body = (ROOT / filename).read_bytes()
                elif path in STATIC:
                    filename, content_type = STATIC[path]
                    if not (WEB_ROOT / 'index.html').is_file():
                        raise AppError(503, 'UI が未ビルドです。frontend で npm ci && npm run build を実行してください')
                    body = (WEB_ROOT / filename).read_bytes()
                else:
                    raise AppError(404, '見つかりません')
                self.send_response(200)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.command == 'POST':
                payload = self.body()
                if path == '/api/decisions':
                    return self.json_response(201, store.add(payload))
                match = re.fullmatch(r'/api/tasks/(.+)/posts', path)
                if match:
                    return self.json_response(201, store.create_post(unquote(match[1]), payload))
                match = re.fullmatch(r'/api/posts/([0-9]+)/receive', path)
                if match:
                    return self.json_response(200, store.receive_post(int(match[1]), payload))
                if path == '/api/claim':
                    object_fields(payload, ('worker',), ('lease_seconds', 'key_prefix'))
                    if 'key_prefix' in payload and payload['key_prefix'] is None:
                        # null を未指定扱いにすると全件 claim になり、絞り込みが黙って外れる
                        raise AppError(400, 'key_prefix は文字列で指定してください')
                    return self.json_response(200, store.claim(payload['worker'], payload.get('lease_seconds', 300), payload.get('key_prefix')))
                match = re.fullmatch(r'/api/decisions/([a-f0-9]{32})/(answer|complete|undo|report|retry|close|renew|update|withdraw)', path)
                if match:
                    if match[2] == 'complete':
                        object_fields(payload, ('token',))
                        result = store.complete(match[1], payload['token'])
                    else:
                        result = getattr(store, match[2])(match[1], payload)
                    return self.json_response(200, result)
            raise AppError(404, '見つかりません')
        except AppError as error:
            self.json_response(error.status, {'error': str(error)})
        except (sqlite3.Error, OSError, ValueError) as error:
            self.log_error('Request failed: %s', type(error).__name__)
            try:
                self.json_response(500, {'error': '保存または読み込みに失敗しました。もう一度お試しください'})
            except OSError:
                pass

    do_GET = handle_action
    do_POST = handle_action


def make_server(db_path, port=0, demo=False, root=None):
    directory = tempfile.TemporaryDirectory(prefix='decision-inbox-demo-') if demo else None
    try:
        store = Store(Path(directory.name) / 'demo.sqlite3' if directory else db_path)
        if demo:
            seed_demo(store)
        server = LocalServer(('127.0.0.1', port), Handler)
    except BaseException:
        if directory:
            directory.cleanup()
        raise
    server.store = store
    server.demo = demo
    server.demo_directory = directory
    server.files_root = files_root(root)
    return server


def default_db():
    override = os.environ.get('DECISION_INBOX_DB')
    if override:
        return Path(override).expanduser()
    if sys.platform == 'darwin':
        return Path.home() / 'Library' / 'Application Support' / 'decision-inbox' / 'decisions.sqlite3'
    return Path(os.environ.get('XDG_DATA_HOME', str(Path.home() / '.local' / 'share'))) / 'decision-inbox' / 'decisions.sqlite3'


def tui_web_url(value):
    if value is None:
        return None
    url(value)
    if urlsplit(value).hostname not in ('localhost', '127.0.0.1', '::1'):
        raise AppError(400, 'Web の URL は loopback の http(s) URL を指定してください')
    return value


def positive_seconds(value):
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError('秒数は正の有限値を指定してください')
    return seconds


def watch_ready(store, interval, timeout, key_prefix=None):
    deadline = time.monotonic() + timeout
    previous = set()
    try:
        while True:
            ready = store.ready(key_prefix)
            added = [identifier for identifier in ready if identifier not in previous]
            for offset in range(0, len(added), 100):
                print(json.dumps({'event': 'cue.ready', 'decision_ids': added[offset:offset + 100]}), flush=True)
            previous = set(ready)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(interval, remaining))
    except KeyboardInterrupt:
        pass
    return 0


def decision_text(task):
    if not task['decision_id']:
        return ''
    decision = task['decision']
    if decision is None:
        return f"{task['decision_id']} (見つかりません)"
    state = '取り下げ' if decision['withdrawn'] else '回答済み' if decision['answered'] else '未回答'
    return f"{state} {decision['title']}  [{task['decision_id']}]"


def format_tasks(store, lane=None):
    lines = []
    tasks = store.tasks(lane)
    for key, label in LANES.items():
        if lane in (None, key):
            group = [task for task in tasks if task['lane'] == key]
            lines.append(f'## {label} ({len(group)})')
            for task in group:
                lines.append(f"- {task['title']}  [{task['id']}]")
                updated = datetime.fromisoformat(task['updated_at']).astimezone().strftime('%m-%d %H:%M')
                parts = [('依頼', task['request']), ('担当', f"{task['pane']} {task['run']}".strip()), ('結果', task['result']), ('種類', task.get('kind', '')), ('完了の条件', task.get('done_when', '')), ('機械', task.get('machine', '')), ('残り', task['remaining']), ('判断', decision_text(task)), ('更新', updated)]
                lines.append('  ' + ' / '.join(f'{name}: {value}' for name, value in parts if value))
    return '\n'.join(lines)


def run_task(store, args):
    if args.task_command == 'add':
        payload = {'id': args.id, 'title': args.title, 'request': args.request, 'pane': args.pane, 'run': args.run, 'result': args.result, 'remaining': args.remaining, 'decision_id': args.decision, 'kind': args.kind, 'done_when': args.done_when}
        if args.lane is not None:
            payload['lane'] = args.lane
        return store.add_task({key: value for key, value in payload.items() if value is not None})
    if args.task_command == 'move':
        return store.update_task(args.id, {'lane': args.lane})
    if args.task_command == 'reconcile-lane':
        return store.reconcile_lane(
            args.id, args.lane, args.machine, args.expect, pane=args.pane, expect_pane=args.expect_pane,
            expect_machine=args.expect_machine, expect_run=args.expect_run,
            expect_request=args.expect_request, expect_kind=args.expect_kind,
            expect_remaining=args.expect_remaining, expect_decision_id=args.expect_decision_id,
            expect_updated_at=args.expect_updated_at,
        )
    if args.task_command == 'update':
        payload = {'title': args.title, 'request': args.request, 'pane': args.pane, 'run': args.run, 'result': args.result, 'remaining': args.remaining, 'decision_id': args.decision, 'kind': args.kind, 'done_when': args.done_when}
        return store.update_task(args.id, {key: value for key, value in payload.items() if value is not None})
    if args.task_command == 'delete':
        return store.delete_task(args.id)
    return task_lanes(store, args.lane) if args.json else None


def task_lanes(store, lane=None):
    tasks = store.tasks(lane)
    return {'lanes': [{'id': key, 'label': label, 'tasks': [task for task in tasks if task['lane'] == key]} for key, label in LANES.items() if lane in (None, key)]}


def hooks_dir():
    override = os.environ.get('DECISION_INBOX_HOOKS_DIR')
    if override:
        return Path(override).expanduser().absolute()
    return Path(os.environ.get('XDG_CONFIG_HOME') or str(Path.home() / '.config')).expanduser().absolute() / 'decision-inbox' / 'hooks'


def pre_add_hook():
    path = hooks_dir() / 'pre-add'
    return path if path.is_file() and os.access(path, os.X_OK) else None


def claims_dir(db):
    override = os.environ.get('DECISION_INBOX_CLAIMS_DIR')
    return Path(override).expanduser().absolute() if override else Path(db).expanduser().absolute().parent / 'claims'


PRE_ADD_TIMEOUT = 10


def kill_pre_add(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def run_pre_add(item):
    # 登録の可否は呼び出し元 (agent の pane) の env で決まるので、CLI だけが呼ぶ。HTTP は server の env しか持たない
    hook = pre_add_hook()
    if hook is None:
        return
    # stderr は一時ファイルに逃がし、先頭だけ読む。PIPE だと饒舌な hook の出力を全量メモリに溜める
    try:
        errors = tempfile.TemporaryFile()
    except OSError as error:
        # 検査できない状態は起動失敗と同じく素通りさせない
        raise AppError(400, f'pre-add が登録を断りました: 一時ファイルを作れませんでした ({error.strerror or error})') from None
    with errors:
        try:
            # 別の process group で起動し、打ち切るときに hook が起こした子もまとめて止める
            process = subprocess.Popen([str(hook)], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=errors, start_new_session=True)
        except OSError as error:
            # 起動できない hook は検査が効いていない状態なので、素通りさせず登録しない
            raise AppError(400, f'pre-add が登録を断りました: 起動に失敗しました ({error.strerror or error})') from None
        try:
            process.communicate(json.dumps(item, ensure_ascii=False).encode(), timeout=PRE_ADD_TIMEOUT)
        except subprocess.TimeoutExpired:
            # 止まった hook で全 pane の add が詰まらないよう打ち切る。検査が終わっていないので登録はしない
            kill_pre_add(process)
            raise AppError(400, f'pre-add が登録を断りました: {PRE_ADD_TIMEOUT} 秒で終わりませんでした') from None
        except KeyboardInterrupt:
            # Ctrl-C でも hook と子を残さない
            kill_pre_add(process)
            raise
        if process.returncode != 0:
            # 背景の子が stderr を握ったまま書くと共有 offset が動くので、offset を使わず先頭から読む
            raise AppError(400, 'pre-add が登録を断りました: ' + os.pread(errors.fileno(), 2000, 0).decode(errors='replace')[:500])


def require_run_prefix(store, item):
    # pre-add の有無・場所に依らず効く層 (runs 表は --db が指す DB のものを引くので、別 DB を指すと素通りするが登録先も同じ別 DB)。呼び出し pane が開いた run の行を持つのに、request_key が
    # その run の frontdesk-<run>/ で始まらない add を断る。行の無い pane (他用途) は通す。HTTP は pane を知らないので対象外
    pane = os.environ.get('HERDR_PANE_ID')
    if not pane:
        return
    socket = os.environ.get('HERDR_SOCKET_PATH') or os.path.expanduser('~/.config/herdr/herdr.sock')
    runs = store.open_run_ids(pane, socket)
    key = item.get('request_key')
    if runs and not (isinstance(key, str) and any(key.startswith(f'frontdesk-{run}/') and len(key) > len(f'frontdesk-{run}/') for run in runs)):
        wants = ' か '.join(f'frontdesk-{run}/<名前>' for run in runs)
        raise AppError(400, f'この pane は run {", ".join(runs)} に開いており、request_key は {wants} の形が要ります')


def open_temp(directory):
    # 末尾を .json にしない。claims の列挙と掃除が書きかけを拾わないため
    path = Path(directory) / f'.claim-{secrets.token_hex(8)}.tmp'
    return os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), path


def write_synced(descriptor, data):
    with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
        json.dump(data, handle, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())


def fsync_dir(path):
    # 一時ファイルの fsync だけでは link / replace で増えた名前が電源断で消えうる
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def unlink_temp(path, what='token を含む一時ファイル'):
    try:
        path.unlink(missing_ok=True)
    except OSError as error:
        # 警告は出せたら出す。2>&- で stderr が None だと print は stdout に書いて結果の JSON を壊し、
        # 書き込みの失敗を投げると finally から claim の結果や元の例外を潰す
        if sys.stderr is not None:
            with suppress(OSError, ValueError):
                print(json.dumps({'warning': f'{what}を消せませんでした: {path} ({error.strerror or error})'}, ensure_ascii=False), file=sys.stderr)


TOKEN_FILE_LIMIT = 65536


def read_token_file(path, identifier):
    try:
        # O_NONBLOCK で開く。書き手の無い FIFO だと通常の open が戻らない
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(descriptor, 'rb') as handle:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ValueError
            raw = handle.read(TOKEN_FILE_LIMIT + 1)
        # 上限で切ってから decode すると、切れ目の後ろが壊れていても通ってしまう
        if len(raw) > TOKEN_FILE_LIMIT:
            raise ValueError
        data = json.loads(raw)
    # 64 KiB 以内でも入れ子が深いと json が RecursionError を出す。claims の列挙で飛ばせるよう 400 に揃える
    except (OSError, ValueError, UnicodeError, RecursionError):
        raise AppError(400, 'token ファイルを読めません') from None
    if not isinstance(data, dict) or not isinstance(data.get('token'), str):
        raise AppError(400, 'token ファイルの形式が不正です')
    if identifier is not None and data.get('id') != identifier:
        raise AppError(400, 'token ファイルの id が --id と一致しません')
    return data


# run-inbox.sh の publish が作る "<prio>-<micros>-<tag>-<from>-<rand>.msg"
MSG_NAME = re.compile(r'[01]-[0-9]+-([a-z]+)-([A-Za-z0-9:_.]+)-([A-Za-z0-9]+)\.msg')
MSG_STATES = ('new', 'held', 'done')
MSG_WAIT = re.compile(r'(decision|pr|msg)=\S+')
MSG_REQ = re.compile(r'[A-Za-z0-9._:/-]+')
INBOX_WAIT = re.compile(rb'(decision=[0-9a-f]{32}|pr=https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/[0-9]+|msg=[A-Za-z0-9_.:/-]+)')
INBOX_HEADER_LIMIT = 65536
CLAIM_FILE = re.compile(r'([0-9a-f]{32})-[0-9a-f]{16}\.json')


@contextmanager
def open_message_file(directory, name):
    try:
        # 送り手が置いたリンクをたどらず、FIFO でも待ち続けない。
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(descriptor, 'rb') as handle:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise OSError
            yield handle
    except OSError:
        raise AppError(400, f'msg のファイルを読めません: {name}') from None


def message_request(handle):
    remaining = INBOX_HEADER_LIMIT
    while remaining > 0:
        line = handle.readline(remaining + 1)
        remaining -= len(line)
        if remaining < 0 or line in (b'', b'\n', b'\r\n'):
            return None
        if line.startswith(b'req: '):
            req = line[5:].rstrip(b'\r\n').decode('ascii', errors='replace')
            return req if len(req) <= 200 and MSG_REQ.fullmatch(req) else None
    return None


def inbox_messages(inbox):
    def version(info):
        return info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns

    # hold は待ち先を CLI 引数で受け取る。個別の msg move より長い既存値も、OS の引数上限まで読む。
    wait_limit = os.sysconf('SC_ARG_MAX')
    messages = {}
    try:
        with ExitStack() as files:
            boxes = {}
            for name in ('new', 'hold'):
                box = inbox / name
                fd = os.open(box, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                files.callback(os.close, fd)
                boxes[box] = fd, version(os.fstat(fd))
            for box, (fd, _) in boxes.items():
                for msg in sorted(os.listdir(fd)):
                    if not msg.endswith('.msg'):
                        continue
                    match = MSG_NAME.fullmatch(msg)
                    if len(msg) > 200 or not match:
                        raise AppError(400, f'{box.name}/ に不正な msg 名があります')
                    if msg in messages:
                        raise AppError(409, f'new/ と hold/ に同じ msg があります: {msg}')
                    with open_message_file(fd, msg) as handle:
                        req = message_request(handle)
                    fields = {'state': 'new' if box.name == 'new' else 'held', 'req': req}
                    if box.name == 'hold':
                        with open_message_file(fd, msg + '.wait') as handle:
                            wait = handle.readline(wait_limit + 2).rstrip(b'\n')
                        if len(wait) > wait_limit or not INBOX_WAIT.fullmatch(wait):
                            raise AppError(400, f'hold/ に不正な待ち先があります: {msg}')
                        fields['wait'] = wait.decode('ascii')
                    messages[msg] = (match.group(1, 2), fields)
            # new/ から hold/ へ移動するときは .wait が先に置かれる。ここで検出した置き場の変更は次の同期に任せる。
            if any(version(box.lstat()) != before for box, (_, before) in boxes.items()):
                raise AppError(409, '走査中に inbox が更新されました。次の同期で再試行してください')
    except OSError:
        raise AppError(400, 'inbox の new/ と hold/ を読み取れるディレクトリにしてください') from None
    return messages


def sweep_claims(store, directory, now):
    # lease 期限から 24 時間は残す。session が消えた後に claims --id で探す余地を残すため
    for path in Path(directory).glob('*.json'):
        # claims_dir は env で他のファイルがある場所を指せるので、auto が作った名前と中身のものだけを消す
        name = CLAIM_FILE.fullmatch(path.name)
        try:
            data = read_token_file(path, name and name[1])
            expires = datetime.fromisoformat(data['expires_at']).timestamp()
        except (AppError, KeyError, TypeError, ValueError):
            continue
        if not name or not isinstance(data.get('worker'), str):
            continue
        # --token や HTTP の renew はファイルの expires_at を書き換えないので DB の lease も見る。
        # この DB に無い判断 (claims_dir を共有する別 DB の claim) は失効を確かめられないので残す
        lease = store.lease_of(data['id'], data['token'])
        if lease is not None and max(expires, lease) + 86400 < now:
            path.unlink(missing_ok=True)


def claim_to_file(store, args):
    # 空文字を従来の経路へ落とすと、空の変数からパスを組んだ呼び手の token が stdout に出る
    if not args.token_file:
        raise AppError(400, '--token-file には auto かパスを指定してください')
    directory = claims_dir(args.db)
    if args.token_file == 'auto':
        # 作るのは claims_dir だけにする。中間ディレクトリまで作ると、その名前を持つ祖先すべてを
        # commit 前に fsync しないと電源断で token ファイルへ辿れなくなる
        try:
            directory.mkdir(mode=0o700, exist_ok=True)
        except FileNotFoundError:
            raise AppError(400, f'claims_dir の親ディレクトリがありません: {directory.parent}') from None
        sweep_claims(store, directory, time.time())
        target = None
    else:
        target = Path(args.token_file).expanduser().absolute()
    # claim より先に開く。開けないなら claim せず、token の行き場が無い claim を作らない
    descriptor, temp = open_temp(directory if target is None else target.parent)
    published = []

    def save(result):
        nonlocal descriptor
        # claim ごとに別のファイル名にする。worker 名で固定すると、同じ worker 名で再 claim されたとき
        # 旧プロセスが新しい token を読んで照合を通る経路ができる
        final = target or directory / f"{result['decision']['id']}-{secrets.token_hex(8)}.json"
        handle, descriptor = descriptor, None
        write_synced(handle, {'id': result['decision']['id'], 'token': result['token'], 'worker': result['worker'], 'expires_at': result['expires_at']})
        # rename でなく link にする。宛先が既にあれば上書きせず失敗させ、他の claim のファイルを壊さない
        os.link(temp, final)
        # 一時名は link の直後に消す。commit の直後に落ちると finally が走らず、有効な token の入った
        # .claim-*.tmp が残る (列挙にも掃除にも掛からない)。ここで失敗しても finally がもう一度試して警告する
        published.append(final)
        with suppress(OSError):
            temp.unlink(missing_ok=True)
        fsync_dir(final.parent)
        # claims_dir は対象なしの claim でも作られるので、作った回に限らず親も毎回 fsync する
        if target is None:
            fsync_dir(directory.parent)
        result['token_file'] = str(final)

    try:
        result = store.claim(args.worker, args.lease_seconds, args.key_prefix, on_claim=save)
    except BaseException:
        for path in published:
            # 後始末の失敗で元の例外を隠さない
            with suppress(OSError):
                path.unlink(missing_ok=True)
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)
        # commit 後に一時名を消せなくても claim は成立しているので、結果は失わず警告だけ出す
        unlink_temp(temp)
    result.pop('token', None)
    return result


def renew_with_file(store, args):
    path = args.token_file
    data = read_token_file(path, args.id)

    def save(result):
        # 書き込みロックの中で読み直す。ファイルが別経路で消え、同じ PATH に別の claim が入っていたら上書きしない
        if read_token_file(path, args.id)['token'] != data['token']:
            raise AppError(409, 'token ファイルが別の claim のものに置き換わっています')
        descriptor, temp = open_temp(path.parent)
        try:
            write_synced(descriptor, {**data, 'expires_at': result['expires_at']})
            os.replace(temp, path)
            fsync_dir(path.parent)
        finally:
            unlink_temp(temp)

    return store.renew(args.id, {'token': data['token'], 'lease_seconds': args.lease_seconds}, on_renew=save)


def token_from(args):
    return read_token_file(args.token_file, args.id)['token'] if args.token_file else args.token


def list_claims(store, directory, identifier=None):
    claims = []
    for path in sorted(Path(directory).glob('*.json')) if Path(directory).is_dir() else []:
        try:
            data = read_token_file(path, None)
        except AppError:
            continue
        if identifier is not None and data.get('id') != identifier:
            continue
        claims.append({'id': data.get('id'), 'token_file': str(path), 'worker': data.get('worker'), 'expires_at': data.get('expires_at'),
                       'active': isinstance(data.get('id'), str) and store.holder(data['id'], data['token'])})
    return {'claims': claims}


CHECK_TICK_BUDGET_SEC = 240.0
CHECK_FINALIZE_RESERVE_SEC = 2.0
CHECK_COMMIT_RESERVE_SEC = 2.0
CHECK_COMMIT_BUSY_TIMEOUT_MS = 5000
CHECK_WAKE_PASS_BUDGET_SEC = 20.0
CHECK_WAKE_ITEM_TIMEOUT_SEC = 5.0
CHECK_WAKE_CB_LIMIT = 2
CHECK_GIT_TIMEOUT_SEC = 5.0
CHECK_MAX_TIMEOUT_SEC = 180.0
CHECK_MAX_FAULT_THRESHOLD = (1 << 63) - 1
CHECK_KILL_GRACE_SEC = 5.0
CHECK_INTERRUPT_KILL_GRACE_SEC = 0.5
CHECK_KILL_WAIT_SEC = 1.0
CHECK_DRAIN_JOIN_SEC = 0.5
CHECK_STDOUT_LIMIT = 65536
CHECK_STDERR_LIMIT = 65536
CHECK_EXCERPT_LIMIT = 4096
CHECK_EVENT_CAP = 20
CHECK_RETENTION_DAYS = 30
CHECK_PRUNE_BATCH = 200
CHECK_FAULT_SKILL = 'check-fault'
CHECK_KEY_RE = re.compile(r'^[A-Za-z0-9._/-]{1,200}$')
CHECK_SUCCESS_OUTCOMES = ('ok_silent', 'fired', 'suppressed', 'mixed')
CHECK_FAILURE_OUTCOMES = ('failed_exit', 'failed_timeout', 'failed_output')
CHECK_ALL_OUTCOMES = (*CHECK_SUCCESS_OUTCOMES, *CHECK_FAILURE_OUTCOMES, 'aborted')
CHECK_ALLOWED_KEYS = frozenset({'name', 'interval', 'command', 'timeout', 'skill', 'env', 'fault_threshold', 'enabled'})


class CheckConfigError(Exception):
    """checkout 検査または checks.toml の検証失敗。tick 全体を config_error として止める。"""


def _parse_check_now_env():
    """テスト用の現在時刻注入 (CUE_CHECK_NOW: epoch 秒または timezone 付き ISO 8601)。"""
    raw = os.environ.get('CUE_CHECK_NOW')
    if not raw:
        return None
    try:
        value = float(raw)
        if math.isfinite(value):
            return value
    except ValueError:
        pass
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).timestamp()


def _iso_utc(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat(timespec='microseconds')


def _parse_iso_ts(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).timestamp()


def _has_surrogate(value):
    return any(0xD800 <= ord(ch) <= 0xDFFF for ch in value)


def _excerpt_4k(raw):
    """バイト列を最大 4KiB の有効な UTF-8 文字列抜粋にする。"""
    if not raw:
        return ''
    text_out = raw[:CHECK_EXCERPT_LIMIT].decode('utf-8', errors='replace')
    encoded = text_out.encode('utf-8')
    if len(encoded) <= CHECK_EXCERPT_LIMIT:
        return text_out
    # U+FFFD 置換で 4KiB をわずかに超えた場合だけ末尾文字を削って 4KiB 以内に収める
    while len(text_out.encode('utf-8')) > CHECK_EXCERPT_LIMIT:
        text_out = text_out[:-1]
    return text_out


def _resolve_check_db_path(db_path):
    """DB パスの symlink を解決した実体パスを返す。"""
    return Path(db_path).expanduser().resolve()


def check_lock_path(db_path):
    """DB と同じ永続ディレクトリに置く固定パスの flock 用 lockfile。終了時に unlink しない。"""
    target = _resolve_check_db_path(db_path)
    return target.parent / (target.name + '.check.lock')


def default_run_inbox_bin():
    override = os.environ.get('CUE_RUN_INBOX_BIN')
    if override:
        return override
    candidate = Path.home() / '.claude' / 'skills' / 'herdr-orchestrate' / 'scripts' / 'run-inbox.sh'
    return str(candidate) if candidate.exists() else 'run-inbox.sh'


@contextmanager
def _check_db(db_path, *, busy_timeout_ms=5000):
    """check runner 用の DB 接続。WAL 切替や DDL は行わない (flock 取得後の初期化のみが行う)。"""
    resolved = _resolve_check_db_path(db_path)
    timeout_ms = max(0, int(busy_timeout_ms))
    connection = sqlite3.connect(str(resolved), timeout=timeout_ms / 1000.0)
    connection.row_factory = sqlite3.Row
    connection.execute(f'PRAGMA busy_timeout={timeout_ms}')
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def ensure_check_schema(db_path):
    """flock 取得後の (1)〜(2) の間でのみ呼ぶ。WAL 切替と additive テーブル作成を行う (user_version は 2 のまま)。"""
    resolved = _resolve_check_db_path(db_path)
    Store(resolved)
    connection = sqlite3.connect(str(resolved), timeout=5.0)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute('PRAGMA busy_timeout=5000')
        connection.execute('PRAGMA journal_mode=WAL')
        with connection:
            connection.execute('BEGIN IMMEDIATE')
            connection.execute('''CREATE TABLE IF NOT EXISTS checks (
                name TEXT PRIMARY KEY,
                interval REAL NOT NULL,
                command TEXT NOT NULL,
                timeout REAL NOT NULL,
                skill TEXT NOT NULL,
                env TEXT NOT NULL DEFAULT '{}',
                fault_threshold INTEGER NOT NULL DEFAULT 3,
                enabled INTEGER NOT NULL DEFAULT 1,
                toml_order INTEGER NOT NULL DEFAULT 0,
                last_started TEXT,
                last_actual_started_at TEXT,
                last_outcome TEXT,
                fault_count INTEGER NOT NULL DEFAULT 0,
                prev_command TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL)''')
            connection.execute('''CREATE TABLE IF NOT EXISTS check_runs (
                id INTEGER PRIMARY KEY,
                tick_id INTEGER NOT NULL,
                check_name TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL,
                exit_code INTEGER,
                outcome TEXT NOT NULL,
                stdout_excerpt TEXT NOT NULL DEFAULT '',
                stderr_excerpt TEXT NOT NULL DEFAULT '',
                events_deferred INTEGER NOT NULL DEFAULT 0,
                clock_anomaly INTEGER NOT NULL DEFAULT 0)''')
            connection.execute('CREATE INDEX IF NOT EXISTS check_runs_started ON check_runs(started_at)')
            connection.execute('CREATE INDEX IF NOT EXISTS check_runs_check ON check_runs(check_name, id)')
            connection.execute('CREATE INDEX IF NOT EXISTS check_runs_tick ON check_runs(tick_id)')
            connection.execute('''CREATE TABLE IF NOT EXISTS check_run_events (
                id INTEGER PRIMARY KEY,
                run_id INTEGER NOT NULL,
                dedup_key TEXT NOT NULL DEFAULT '',
                outcome TEXT NOT NULL,
                task_id TEXT,
                suppressed_by_task_id TEXT)''')
            connection.execute('CREATE INDEX IF NOT EXISTS check_run_events_run ON check_run_events(run_id)')
            connection.execute('''CREATE TABLE IF NOT EXISTS check_ticks (
                id INTEGER PRIMARY KEY,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL,
                wake_failed INTEGER NOT NULL DEFAULT 0,
                unnotified_tasks INTEGER NOT NULL DEFAULT 0,
                checks_deferred INTEGER)''')
            connection.execute('CREATE INDEX IF NOT EXISTS check_ticks_started ON check_ticks(started_at)')
            connection.execute('''CREATE TABLE IF NOT EXISTS check_tasks (
                task_id TEXT PRIMARY KEY,
                check_name TEXT NOT NULL,
                kind TEXT NOT NULL,
                dedup_key TEXT NOT NULL DEFAULT '',
                skill TEXT NOT NULL,
                needs_wake INTEGER NOT NULL DEFAULT 1,
                notified_at TEXT,
                last_wake_attempt_at TEXT,
                created_at TEXT NOT NULL)''')
            connection.execute('CREATE INDEX IF NOT EXISTS check_tasks_dedup ON check_tasks(check_name, kind, dedup_key)')
            connection.execute('CREATE INDEX IF NOT EXISTS check_tasks_wake ON check_tasks(needs_wake) WHERE needs_wake=1')
    finally:
        connection.close()


def _record_skipped_tick(db_path, started_iso):
    """ロック不取得時の最小 skip 記録。DDL・WAL 切替は行わず、競合で書けない場合は stderr に残して終了する。"""
    resolved = _resolve_check_db_path(db_path)
    if not resolved.exists():
        print('warning: check tick skipped (lock held, DB not initialized)', file=sys.stderr)
        return None
    connection = None
    try:
        # ブロックし続けないよう短い timeout にする
        connection = sqlite3.connect(str(resolved), timeout=0.5)
        connection.execute('PRAGMA busy_timeout=500')
        with connection:
            cursor = connection.execute(
                "INSERT INTO check_ticks(started_at, finished_at, status, wake_failed, unnotified_tasks, checks_deferred) VALUES(?, ?, 'skipped', 0, 0, NULL)",
                (started_iso, started_iso),
            )
            return cursor.lastrowid
    except sqlite3.Error as error:
        print(f'warning: check tick skipped (failed to record skip: {error})', file=sys.stderr)
        return None
    finally:
        if connection is not None:
            with suppress(sqlite3.Error):
                connection.close()


def _pgid_alive(pgid):
    """プロセスグループ pgid に残存プロセスがあるか確認する。"""
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _terminate_process_group(
    process, grace_sec, *, kill_wait_sec=CHECK_KILL_WAIT_SEC,
    deadline=None, tail_reserve_sec=0.0, mono_fn=time.monotonic,
):
    """プロセスグループ全体へ SIGTERM → grace_sec 猶予後に残存があれば SIGKILL。
    deadline (mono_fn 基準の絶対期限) が指定されている場合、SIGTERM 猶予と SIGKILL 後の回収待ちを
    その絶対期限内に収める。各フェーズ開始時点の単一時刻サンプルと deadline の最小値でフェーズ期限を決め、
    開始遅延や後から取った時刻への加算で cleanup 窓が後ろへずれないようにする。"""
    pgid = process.pid
    with suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(pgid, signal.SIGTERM)

    grace_cap = max(0.0, float(grace_sec))
    kill_wait_cap = max(0.0, float(kill_wait_sec))
    tail_cap = max(0.0, float(tail_reserve_sec))

    term_start = mono_fn()
    if deadline is None:
        grace_budget = grace_cap
        kill_wait_budget = kill_wait_cap
        term_deadline = term_start + grace_budget
    else:
        cleanup_rem = max(0.0, deadline - term_start)
        total_cap = grace_cap + kill_wait_cap + tail_cap
        if total_cap <= 0 or cleanup_rem <= 0:
            grace_budget = 0.0
            kill_wait_budget = 0.0
        elif cleanup_rem >= total_cap:
            grace_budget = grace_cap
            kill_wait_budget = kill_wait_cap
        else:
            scale = cleanup_rem / total_cap
            grace_budget = grace_cap * scale
            kill_wait_budget = kill_wait_cap * scale
        term_deadline = min(term_start + grace_budget, deadline)

    term_real_deadline = time.monotonic() + max(0.0, term_deadline - term_start)
    while True:
        remaining = min(term_deadline - mono_fn(), term_real_deadline - time.monotonic())
        if process.returncode is None:
            if remaining > 0:
                try:
                    process.wait(timeout=remaining)
                except subprocess.TimeoutExpired:
                    break
            else:
                process.poll()
                if process.returncode is None:
                    break
        # リーダー終了済み (reap 済み)。グループ内の子孫残存を killpg(pgid, 0) で確認する
        if not _pgid_alive(pgid):
            return
        remaining = min(term_deadline - mono_fn(), term_real_deadline - time.monotonic())
        if remaining <= 0:
            break
        time.sleep(min(0.05, remaining))

    if process.returncode is None or _pgid_alive(pgid):
        with suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(pgid, signal.SIGKILL)
    kill_wait_start = mono_fn()
    kill_wait_deadline = kill_wait_start + kill_wait_budget
    if deadline is not None:
        kill_wait_deadline = min(kill_wait_deadline, deadline)
    kill_wait_effective = max(0.0, kill_wait_deadline - kill_wait_start)
    kill_wait_real_deadline = time.monotonic() + kill_wait_effective
    leader_wait_timed_out = False
    if process.returncode is None:
        wait_remaining = min(
            kill_wait_deadline - mono_fn(),
            kill_wait_real_deadline - time.monotonic(),
        )
        if wait_remaining > 0:
            try:
                process.wait(timeout=wait_remaining)
            except subprocess.TimeoutExpired:
                leader_wait_timed_out = True
            except OSError:
                pass
        else:
            process.poll()
            if process.returncode is None:
                leader_wait_timed_out = True
    while _pgid_alive(pgid):
        wait_remaining = min(
            kill_wait_deadline - mono_fn(),
            kill_wait_real_deadline - time.monotonic(),
        )
        if wait_remaining <= 0:
            break
        time.sleep(min(0.01, wait_remaining))
    if leader_wait_timed_out or process.returncode is None or _pgid_alive(pgid):
        print(
            f'warning: process group {pgid} did not terminate within {kill_wait_effective:.2f}s after SIGKILL',
            file=sys.stderr,
        )


def _run_subprocess_bounded(argv, *, cwd=None, env=None, timeout=5.0, kill_grace=0.5):
    """プロセスグループで実行し、タイムアウトや割り込み時はグループごと確実に終了させる。"""
    try:
        process = subprocess.Popen(
            argv, cwd=cwd, env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except (OSError, ValueError) as error:
        return None, b'', str(error).encode('utf-8', errors='replace'), False
    try:
        out, err = process.communicate(timeout=max(0.01, timeout))
        return process.returncode, out, err, False
    except subprocess.TimeoutExpired:
        _terminate_process_group(process, kill_grace)
        with suppress(OSError, ValueError):
            if process.stdout:
                process.stdout.close()
            if process.stderr:
                process.stderr.close()
        return None, b'', b'timeout', True
    except BaseException:
        # KeyboardInterrupt 等で抜ける前にプロセスグループを上限付きで停止・回収する
        _terminate_process_group(process, min(kill_grace, CHECK_INTERRUPT_KILL_GRACE_SEC))
        with suppress(OSError, ValueError):
            if process.stdout:
                process.stdout.close()
            if process.stderr:
                process.stderr.close()
        raise


def _run_git_checked(args, cwd, deadline, mono_fn):
    remaining = deadline - mono_fn()
    if remaining <= 0:
        raise CheckConfigError('git checkout 検査が 5 秒の上限を超過しました')
    rc, out, err, timed_out = _run_subprocess_bounded(['git', '-C', str(cwd), *args], timeout=remaining, kill_grace=0.5)
    if timed_out or mono_fn() > deadline:
        raise CheckConfigError('git checkout 検査が 5 秒の上限を超過しました')
    if rc != 0:
        detail = err.decode('utf-8', errors='replace').strip()
        raise CheckConfigError(f'git checkout 検査に失敗しました: {detail or rc}')
    return out.decode('utf-8', errors='replace')


def load_and_validate_checks_config(config_path, *, mono_fn=time.monotonic, git_timeout=CHECK_GIT_TIMEOUT_SEC):
    """checkout の整合性 (main ブランチ・tracked clean・合計 5 秒上限) と checks.toml を全件検証する。"""
    path = Path(config_path).expanduser()
    if not path.is_file():
        raise CheckConfigError(f'checks.toml が見つかりません: {config_path}')
    config_dir = path.parent.resolve()
    git_deadline = mono_fn() + git_timeout
    top_out = _run_git_checked(['rev-parse', '--show-toplevel'], config_dir, git_deadline, mono_fn).strip()
    if not top_out:
        raise CheckConfigError('checks.toml のディレクトリが git リポジトリではありません')
    checkout_root = os.path.realpath(top_out)
    real_config = os.path.realpath(path)
    try:
        if os.path.commonpath([checkout_root, real_config]) != checkout_root:
            raise CheckConfigError('checks.toml の実体が checkout 配下にありません')
    except ValueError:
        raise CheckConfigError('checks.toml の実体が checkout 配下にありません') from None

    branch = _run_git_checked(['symbolic-ref', '--quiet', '--short', 'HEAD'], checkout_root, git_deadline, mono_fn).strip()
    if branch != 'main':
        raise CheckConfigError(f'checkout のブランチが main ではありません: {branch or "(detached)"}')

    status = _run_git_checked(['-c', 'core.quotepath=false', 'status', '--porcelain', '--untracked-files=no'], checkout_root, git_deadline, mono_fn)
    if status.strip():
        raise CheckConfigError('checkout の tracked ファイルに未コミットの変更があります')

    try:
        raw_text = path.read_text(encoding='utf-8')
        parsed = tomllib.loads(raw_text)
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise CheckConfigError(f'checks.toml の読み込みに失敗しました: {error}') from None

    if not isinstance(parsed, dict):
        raise CheckConfigError('checks.toml の最上位はテーブルである必要があります')
    unknown_top = set(parsed) - {'checks'}
    if unknown_top:
        raise CheckConfigError(f'checks.toml に未知の最上位キーがあります: {", ".join(sorted(unknown_top))}')
    raw_checks = parsed.get('checks')
    if not isinstance(raw_checks, list):
        raise CheckConfigError('checks.toml には [[checks]] 配列が必要です')

    validated = []
    seen_names = set()
    for index, entry in enumerate(raw_checks):
        if not isinstance(entry, dict):
            raise CheckConfigError(f'checks[{index}] はテーブルである必要があります')
        unknown_keys = set(entry) - CHECK_ALLOWED_KEYS
        if unknown_keys:
            raise CheckConfigError(f'checks[{index}] に未知の項目があります: {", ".join(sorted(unknown_keys))}')

        name = entry.get('name')
        if not isinstance(name, str) or not name.strip() or '\x00' in name or _has_surrogate(name):
            raise CheckConfigError(f'checks[{index}].name は非空の文字列である必要があります')
        if name in seen_names:
            raise CheckConfigError(f'checks.toml 内で name が重複しています: {name}')
        seen_names.add(name)

        timeout = entry.get('timeout')
        try:
            timeout_val = None if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) else float(timeout)
        except OverflowError:
            timeout_val = None
        if timeout_val is None or not math.isfinite(timeout_val) or not (0 < timeout_val <= CHECK_MAX_TIMEOUT_SEC):
            raise CheckConfigError(f'check {name!r} の timeout は 0 < timeout <= {int(CHECK_MAX_TIMEOUT_SEC)} である必要があります')

        interval = entry.get('interval')
        try:
            interval_val = None if isinstance(interval, bool) or not isinstance(interval, (int, float)) else float(interval)
        except OverflowError:
            interval_val = None
        if interval_val is None or not math.isfinite(interval_val) or interval_val <= 0:
            raise CheckConfigError(f'check {name!r} の interval は正の数である必要があります')

        fault_threshold = entry.get('fault_threshold', 3)
        if isinstance(fault_threshold, bool) or not isinstance(fault_threshold, int) or not (1 <= fault_threshold <= CHECK_MAX_FAULT_THRESHOLD):
            raise CheckConfigError(
                f'check {name!r} の fault_threshold は 1 以上 {CHECK_MAX_FAULT_THRESHOLD} 以下の整数である必要があります'
            )

        skill = entry.get('skill')
        if not isinstance(skill, str) or not skill.strip() or '\x00' in skill or _has_surrogate(skill):
            raise CheckConfigError(f'check {name!r} の skill は非空の文字列である必要があります')

        enabled = entry.get('enabled', True)
        if not isinstance(enabled, bool):
            raise CheckConfigError(f'check {name!r} の enabled は真偽値である必要があります')

        env_raw = entry.get('env', {})
        if not isinstance(env_raw, dict):
            raise CheckConfigError(f'check {name!r} の env はテーブルである必要があります')
        env_clean = {}
        for env_key, env_val in env_raw.items():
            if (
                not isinstance(env_key, str) or not env_key or '\x00' in env_key or '=' in env_key or _has_surrogate(env_key)
                or not isinstance(env_val, str) or '\x00' in env_val or _has_surrogate(env_val)
            ):
                raise CheckConfigError(f'check {name!r} の env は有効なキーと文字列の組である必要があります')
            env_clean[env_key] = env_val

        command_raw = entry.get('command')
        if isinstance(command_raw, str):
            if not command_raw.strip() or '\x00' in command_raw:
                raise CheckConfigError(f'check {name!r} の command が不正です')
            cmd_path = command_raw
            cmd_args = []
        elif isinstance(command_raw, list) and command_raw and all(isinstance(part, str) and '\x00' not in part for part in command_raw):
            if not command_raw[0].strip():
                raise CheckConfigError(f'check {name!r} の command[0] が空です')
            cmd_path = command_raw[0]
            cmd_args = command_raw[1:]
        else:
            raise CheckConfigError(f'check {name!r} の command はパス文字列または非空の文字列配列である必要があります')

        candidate = Path(cmd_path) if os.path.isabs(cmd_path) else (config_dir / cmd_path)
        real_cmd = os.path.realpath(candidate)
        try:
            inside = os.path.commonpath([checkout_root, real_cmd]) == checkout_root and real_cmd != checkout_root
        except ValueError:
            inside = False
        if not inside:
            raise CheckConfigError(f'check {name!r} の command は checkout 配下を指す必要があります: {cmd_path}')
        if not os.path.isfile(real_cmd):
            raise CheckConfigError(f'check {name!r} の command ファイルが見つかりません: {cmd_path}')

        resolved_argv = [real_cmd, *cmd_args]
        # 文字列形 ('["probe"]') と配列形 (["probe"]) が衝突しないよう、配列 JSON として一意に表現する
        canonical_cmd = json.dumps([cmd_path, *cmd_args], ensure_ascii=False)
        command_identity = json.dumps(resolved_argv, ensure_ascii=False)

        validated.append({
            'name': name,
            'interval': interval_val,
            'command': canonical_cmd,
            'command_identity': command_identity,
            'argv': resolved_argv,
            'timeout': timeout_val,
            'skill': skill,
            'env': env_clean,
            'fault_threshold': fault_threshold,
            'enabled': enabled,
            'toml_order': index,
        })

    return checkout_root, config_dir, validated


def sync_checks_mirror(db_path, checks_cfg, now_iso, *, busy_timeout_ms=CHECK_COMMIT_BUSY_TIMEOUT_MS):
    """TOML 定義を checks テーブルへ鏡同期する。TOML に無い定義は無効化し履歴を残す。
    command 変更または無効化時のみ fault_count を 0 に戻す (env / timeout のみの変更では戻さない)。"""
    with _check_db(db_path, busy_timeout_ms=busy_timeout_ms) as connection:
        connection.execute('BEGIN IMMEDIATE')
        existing = {row['name']: dict(row) for row in connection.execute('SELECT * FROM checks')}
        seen = set()
        for item in checks_cfg:
            name = item['name']
            seen.add(name)
            env_json = json.dumps(item['env'], ensure_ascii=False, sort_keys=True)
            enabled_int = 1 if item['enabled'] else 0
            if name in existing:
                prev = existing[name]
                command_changed = prev['prev_command'] != item['command_identity']
                fault_count = 0 if (command_changed or not item['enabled']) else prev['fault_count']
                connection.execute(
                    '''UPDATE checks
                       SET interval=?, command=?, timeout=?, skill=?, env=?, fault_threshold=?,
                           enabled=?, toml_order=?, fault_count=?, prev_command=?, updated_at=?
                       WHERE name=?''',
                    (
                        item['interval'], item['command'], item['timeout'], item['skill'], env_json,
                        item['fault_threshold'], enabled_int, item['toml_order'], fault_count,
                        item['command_identity'], now_iso, name,
                    ),
                )
            else:
                connection.execute(
                    '''INSERT INTO checks(
                           name, interval, command, timeout, skill, env, fault_threshold, enabled,
                           toml_order, last_started, last_actual_started_at, last_outcome,
                           fault_count, prev_command, updated_at)
                       VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, 0, ?, ?)''',
                    (
                        name, item['interval'], item['command'], item['timeout'], item['skill'], env_json,
                        item['fault_threshold'], enabled_int, item['toml_order'], item['command_identity'], now_iso,
                    ),
                )
        for name in existing:
            if name not in seen:
                connection.execute(
                    'UPDATE checks SET enabled=0, fault_count=0, updated_at=? WHERE name=?',
                    (now_iso, name),
                )


def check_grace_seconds(interval):
    """proofs/CheckRunner.lean の grace(interval) = min(30, floor(interval / 10)) と一致する整数秒 grace を返す。"""
    return float(min(30, max(0, math.floor(float(interval) / 10.0))))


def check_due_info(row, tick_started_ts):
    """due 判定と優先群・巻き戻りフラグを返す: (is_due, priority_group, clock_anomaly, ls_ts, la_ts)。
    整数秒の時刻・interval に対して proofs/CheckRunner.lean の isDue / isClockAnomaly / dueBase と一致させる。"""
    last_started = row['last_started']
    last_actual = row['last_actual_started_at']
    last_outcome = row['last_outcome']
    ls_ts = _parse_iso_ts(last_started) if last_started is not None else None
    la_ts = _parse_iso_ts(last_actual) if last_actual is not None else None

    if ls_ts is None or la_ts is None or last_outcome is None:
        return True, 0, False, ls_ts, la_ts

    if ls_ts > tick_started_ts or la_ts > tick_started_ts:
        return True, 1, True, ls_ts, la_ts

    base_ts = ls_ts if last_outcome in CHECK_SUCCESS_OUTCOMES else la_ts
    grace = check_grace_seconds(row['interval'])
    due = tick_started_ts >= base_ts + float(row['interval']) - grace
    return due, 2, False, ls_ts, la_ts


def select_due_checks(db_path, checks_cfg, tick_started_ts):
    """有効な check のうち tick_started_ts 時点で due なものを規定の実行順に並べて返す。"""
    cfg_by_name = {item['name']: item for item in checks_cfg}
    with _check_db(db_path) as connection:
        rows = [dict(row) for row in connection.execute('SELECT * FROM checks WHERE enabled=1')]

    due_entries = []
    for row in rows:
        cfg = cfg_by_name.get(row['name'])
        if cfg is None or not cfg['enabled']:
            continue
        is_due, group, anomaly, ls_ts, la_ts = check_due_info(row, tick_started_ts)
        if not is_due:
            continue
        ls_key = (0, 0.0) if ls_ts is None else (1, ls_ts)
        la_key = (0, 0.0) if la_ts is None else (1, la_ts)
        due_entries.append((
            (group, ls_key, la_key, int(row['toml_order'])),
            {
                **cfg,
                'clock_anomaly': anomaly,
                'last_started': row['last_started'],
                'last_actual_started_at': row['last_actual_started_at'],
                'last_outcome': row['last_outcome'],
                'fault_count': row['fault_count'],
            },
        ))
    due_entries.sort(key=lambda pair: pair[0])
    return [entry for _, entry in due_entries]


def parse_check_jsonl(raw_stdout):
    """stdout バイト列を DB 書き込み前に全行まとめて検証する。
    違反があれば (False, None)、正常なら (True, [(dedup_key, title, body), ...]) を返す。"""
    if len(raw_stdout) > CHECK_STDOUT_LIMIT:
        return False, None
    try:
        decoded = raw_stdout.decode('utf-8')
    except UnicodeDecodeError:
        return False, None

    def _reject_constant(_value):
        raise ValueError('invalid JSON constant')

    events = []
    seen_keys = set()
    for raw_line in decoded.split('\n'):
        line = raw_line.rstrip('\r')
        if not line.strip():
            continue
        try:
            obj = json.loads(line, parse_constant=_reject_constant)
        except (ValueError, RecursionError):
            return False, None
        if type(obj) is not dict:
            return False, None
        if 'title' not in obj or 'body' not in obj:
            return False, None
        title = obj['title']
        body = obj['body']
        if type(title) is not str or not (1 <= len(title) <= 200) or '\x00' in title or _has_surrogate(title):
            return False, None
        if type(body) is not str or _has_surrogate(body):
            return False, None
        if 'key' in obj:
            key = obj['key']
            if type(key) is not str or not key or not CHECK_KEY_RE.fullmatch(key):
                return False, None
            dedup_key = key
        else:
            dedup_key = ''
        if dedup_key in seen_keys:
            return False, None
        seen_keys.add(dedup_key)
        events.append((dedup_key, title, body))
    return True, events


def _drain_pipe(stream, limit, state):
    total = 0
    try:
        while True:
            data = stream.read(4096)
            if not data:
                state['eof'] = True
                break
            if total < limit:
                take = data[:limit - total]
                state['chunks'].append(take)
                total += len(take)
    except OSError:
        state['error'] = True
    finally:
        with suppress(OSError):
            stream.close()


def run_check_command(
    chk, config_dir, tick_deadline, *,
    mono_fn=time.monotonic, kill_grace=CHECK_KILL_GRACE_SEC,
    cleanup_deadline=None, launch_state=None,
):
    """1 check の command を独立プロセスグループで実行し、(exit_code, raw_stdout, raw_stderr, timed_out, aborted) を返す。

    Policy:
    - wait_until = min(cmd_mono_start + timeout, tick_deadline) をプロセス終了と stdout/stderr の EOF 回収の両方に適用する。
    - wait_until 以降に観測された正常終了 (wait_budget == 0.0 で既に終了していた子を TimeoutExpired なしで回収した場合を含む) は
      TimeoutExpired と同一の deadline miss として分類し (command timeout が支配的なら failed_timeout、そうでなければ aborted)、
      exit code を捨てる。
    - helper が stdout/stderr を開いたまま等で EOF 回収が wait_until 内に完了しない場合も、不完全な出力を parse せず
      プロセスグループを終了させ deadline miss (または失敗) として記録する。
    - cleanup_deadline が指定されている場合、SIGTERM 猶予・SIGKILL 後の回収待ち・drain thread join の合計を
      cleanup_deadline 内に収め、後続の commit 予約予算を圧迫しない。
    - KeyboardInterrupt 等の割り込み時は、tick lock 解放前にプロセスグループを上限付きで終了・回収してから再送出する。
    """
    cmd_env = {**os.environ, **chk['env']}
    cmd_mono_start = mono_fn()
    if cmd_mono_start >= tick_deadline:
        if launch_state is not None:
            launch_state['refused'] = True
        return None, b'', b'', False, True
    timeout_at = cmd_mono_start + chk['timeout']
    wait_until = min(timeout_at, tick_deadline)

    def _classify_deadline_miss():
        # 先に到来した (支配的な) deadline で分類する。
        # 同時 (timeout_at == tick_deadline) は優先順位 failed_timeout > aborted に従い failed_timeout。
        if timeout_at <= tick_deadline:
            return True, False
        return False, True

    out_state = {'chunks': [], 'eof': False, 'error': False}
    err_state = {'chunks': [], 'eof': False, 'error': False}
    out_thread = None
    err_thread = None
    timed_out = False
    aborted = False
    capture_incomplete = False
    try:
        process = subprocess.Popen(
            chk['argv'],
            cwd=str(config_dir),
            env=cmd_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except (OSError, ValueError) as error:
        return None, b'', str(error).encode('utf-8', errors='replace'), False, False

    def _cleanup_group():
        _terminate_process_group(
            process,
            kill_grace,
            kill_wait_sec=CHECK_KILL_WAIT_SEC,
            deadline=cleanup_deadline,
            tail_reserve_sec=0.0 if cleanup_deadline is None else CHECK_DRAIN_JOIN_SEC,
            mono_fn=mono_fn,
        )

    try:
        out_thread = threading.Thread(target=_drain_pipe, args=(process.stdout, CHECK_STDOUT_LIMIT + 1, out_state), daemon=True)
        err_thread = threading.Thread(target=_drain_pipe, args=(process.stderr, CHECK_STDERR_LIMIT, err_state), daemon=True)
        out_thread.start()
        err_thread.start()

        wait_budget = max(0.0, wait_until - mono_fn())
        try:
            process.wait(timeout=wait_budget)
        except subprocess.TimeoutExpired:
            timed_out, aborted = _classify_deadline_miss()
            _cleanup_group()
        else:
            now_m = mono_fn()
            if now_m >= wait_until:
                # wait_until 以降に観測された完了は TimeoutExpired と同じく deadline miss とし exit code を捨てる
                timed_out, aborted = _classify_deadline_miss()
                _cleanup_group()
            else:
                # stdout / stderr の EOF 回収にも同じ wait_until を適用する
                for thread in (out_thread, err_thread):
                    pipe_rem = max(0.0, wait_until - mono_fn())
                    thread.join(timeout=pipe_rem)
                    if thread.is_alive():
                        break
                now_m = mono_fn()
                if out_thread.is_alive() or err_thread.is_alive():
                    # pipe が deadline までに EOF へ到達しなかった: グループを停止し不完全出力を parse させない
                    timed_out, aborted = _classify_deadline_miss()
                    _cleanup_group()
                elif not out_state['eof'] or not err_state['eof']:
                    # EOF に到達しない読み取り異常: 空出力を ok_silent と誤認させない
                    capture_incomplete = True
                    _cleanup_group()
                elif now_m >= wait_until:
                    timed_out, aborted = _classify_deadline_miss()
                    _cleanup_group()

        # グループ停止後の drain thread 終了を上限付きで待ち、抜粋を可能な限り回収する
        if cleanup_deadline is None:
            drain_real_deadline = time.monotonic() + CHECK_DRAIN_JOIN_SEC
            for thread in (out_thread, err_thread):
                if thread.is_alive():
                    thread.join(timeout=max(0.0, drain_real_deadline - time.monotonic()))
        else:
            drain_mono_start = mono_fn()
            drain_mono_deadline = min(drain_mono_start + CHECK_DRAIN_JOIN_SEC, cleanup_deadline)
            drain_real_deadline = time.monotonic() + max(0.0, drain_mono_deadline - drain_mono_start)
            for thread in (out_thread, err_thread):
                if thread.is_alive():
                    rem_t = min(
                        max(0.0, drain_mono_deadline - mono_fn()),
                        max(0.0, drain_real_deadline - time.monotonic()),
                    )
                    if rem_t <= 0:
                        break
                    thread.join(timeout=rem_t)
    except BaseException:
        _terminate_process_group(process, min(kill_grace, CHECK_INTERRUPT_KILL_GRACE_SEC))
        for thread, stream in ((out_thread, process.stdout), (err_thread, process.stderr)):
            if thread is not None and thread.is_alive():
                thread.join(timeout=CHECK_DRAIN_JOIN_SEC)
            if (thread is None or thread.ident is None) and stream is not None:
                with suppress(OSError, ValueError):
                    stream.close()
        raise

    raw_stdout = b''.join(list(out_state['chunks']))
    raw_stderr = b''.join(list(err_state['chunks']))
    exit_code = None if (timed_out or aborted or capture_incomplete) else process.returncode
    return exit_code, raw_stdout, raw_stderr, timed_out, aborted


def _is_sqlite_retryable_contention(error):
    if not isinstance(error, sqlite3.OperationalError):
        return False
    errcode = getattr(error, 'sqlite_errorcode', None)
    if errcode is not None and (errcode & 0xFF) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
        return True
    msg = str(error).lower()
    return 'locked' in msg or 'busy' in msg


def commit_check_run(
    db_path, tick_id, chk, tick_started_iso, actual_started_iso, finished_iso,
    *, exit_code, raw_stdout, raw_stderr, timed_out, aborted,
    busy_timeout_ms=CHECK_COMMIT_BUSY_TIMEOUT_MS,
):
    """1 回の check 実行結果を 1 つの BEGIN IMMEDIATE トランザクションで確定する。
    aborted の場合は check_runs の INSERT のみを行い、checks の実行状態カラムは一切更新しない。"""
    stdout_excerpt = _excerpt_4k(raw_stdout)
    stderr_excerpt = _excerpt_4k(raw_stderr)
    clock_anomaly_int = 1 if chk['clock_anomaly'] else 0

    # outcome 優先順位: failed_timeout > aborted > failed_exit > failed_output > 正常系
    events = None
    if timed_out:
        pre_outcome = 'failed_timeout'
    elif aborted:
        pre_outcome = 'aborted'
    elif exit_code != 0:
        pre_outcome = 'failed_exit'
    else:
        valid, events = parse_check_jsonl(raw_stdout)
        pre_outcome = None if valid else 'failed_output'

    with _check_db(db_path, busy_timeout_ms=busy_timeout_ms) as connection:
        connection.execute('BEGIN IMMEDIATE')
        if pre_outcome == 'aborted':
            cursor = connection.execute(
                '''INSERT INTO check_runs(
                       tick_id, check_name, started_at, finished_at, exit_code, outcome,
                       stdout_excerpt, stderr_excerpt, events_deferred, clock_anomaly)
                   VALUES(?, ?, ?, ?, NULL, 'aborted', ?, ?, 0, ?)''',
                (tick_id, chk['name'], actual_started_iso, finished_iso, stdout_excerpt, stderr_excerpt, clock_anomaly_int),
            )
            return {'run_id': cursor.lastrowid, 'outcome': 'aborted', 'events_deferred': 0, 'created_task_ids': []}

        created_task_ids = []
        run_events = []
        events_deferred = 0

        if pre_outcome is None:
            fired_count = 0
            suppressed_count = 0
            for dedup_key, title, body in events:
                existing_row = connection.execute(
                    '''SELECT ct.task_id
                       FROM check_tasks ct
                       JOIN tasks t ON t.id = ct.task_id
                       WHERE ct.check_name=? AND ct.kind='event' AND ct.dedup_key=? AND t.lane != 'done'
                       ORDER BY ct.created_at ASC, ct.task_id ASC
                       LIMIT 1''',
                    (chk['name'], dedup_key),
                ).fetchone()
                if existing_row is not None:
                    suppressed_count += 1
                    run_events.append((dedup_key, 'suppressed', None, existing_row['task_id']))
                elif fired_count < CHECK_EVENT_CAP:
                    task_id = uuid.uuid4().hex
                    safe_request = body.replace('\x00', '')[:TASK_LIMITS['request']]
                    done_when = f"skill:{chk['skill']} の手順で対応し完了する"[:TASK_LIMITS['done_when']]
                    connection.execute(
                        '''INSERT INTO tasks(id, title, request, pane, run, result, remaining, lane, decision_id, created_at, updated_at, machine, kind, done_when)
                           VALUES(?, ?, ?, '', '', '', '', 'todo', NULL, ?, ?, '', 'monitor', ?)''',
                        (task_id, title, safe_request, finished_iso, finished_iso, done_when),
                    )
                    connection.execute(
                        '''INSERT INTO check_tasks(task_id, check_name, kind, dedup_key, skill, needs_wake, notified_at, last_wake_attempt_at, created_at)
                           VALUES(?, ?, 'event', ?, ?, 1, NULL, NULL, ?)''',
                        (task_id, chk['name'], dedup_key, chk['skill'], finished_iso),
                    )
                    fired_count += 1
                    created_task_ids.append(task_id)
                    run_events.append((dedup_key, 'fired', task_id, None))
                else:
                    events_deferred += 1

            if fired_count > 0 and suppressed_count > 0:
                outcome = 'mixed'
            elif fired_count > 0:
                outcome = 'fired'
            elif suppressed_count > 0:
                outcome = 'suppressed'
            else:
                outcome = 'ok_silent'
            new_fault_count = 0
        else:
            outcome = pre_outcome
            open_fault = connection.execute(
                '''SELECT ct.task_id
                   FROM check_tasks ct
                   JOIN tasks t ON t.id = ct.task_id
                   WHERE ct.check_name=? AND ct.kind='fault' AND ct.dedup_key='' AND t.lane != 'done'
                   LIMIT 1''',
                (chk['name'],),
            ).fetchone()
            if open_fault is not None:
                # 未完了の故障タスクが存在する間は fault_count を進めない (0 のまま維持する)
                new_fault_count = 0
            else:
                cur_row = connection.execute('SELECT fault_count, fault_threshold FROM checks WHERE name=?', (chk['name'],)).fetchone()
                cur_fc = cur_row['fault_count'] if cur_row is not None else 0
                threshold = cur_row['fault_threshold'] if cur_row is not None else chk['fault_threshold']
                next_fc = cur_fc + 1
                if next_fc >= threshold:
                    task_id = uuid.uuid4().hex
                    fault_title = f"[check-fault] {chk['name']} ({outcome})"[:TASK_LIMITS['title']]
                    fault_body = f"check={chk['name']} outcome={outcome} exit_code={exit_code}\n{stderr_excerpt}".strip()[:TASK_LIMITS['request']]
                    done_when = f"skill:{CHECK_FAULT_SKILL} の手順で {chk['name']} の故障を解消する"[:TASK_LIMITS['done_when']]
                    connection.execute(
                        '''INSERT INTO tasks(id, title, request, pane, run, result, remaining, lane, decision_id, created_at, updated_at, machine, kind, done_when)
                           VALUES(?, ?, ?, '', '', '', '', 'todo', NULL, ?, ?, '', 'monitor', ?)''',
                        (task_id, fault_title, fault_body, finished_iso, finished_iso, done_when),
                    )
                    connection.execute(
                        '''INSERT INTO check_tasks(task_id, check_name, kind, dedup_key, skill, needs_wake, notified_at, last_wake_attempt_at, created_at)
                           VALUES(?, ?, 'fault', '', ?, 1, NULL, NULL, ?)''',
                        (task_id, chk['name'], CHECK_FAULT_SKILL, finished_iso),
                    )
                    created_task_ids.append(task_id)
                    new_fault_count = 0
                else:
                    new_fault_count = next_fc

        cursor = connection.execute(
            '''INSERT INTO check_runs(
                   tick_id, check_name, started_at, finished_at, exit_code, outcome,
                   stdout_excerpt, stderr_excerpt, events_deferred, clock_anomaly)
               VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (tick_id, chk['name'], actual_started_iso, finished_iso, exit_code, outcome, stdout_excerpt, stderr_excerpt, events_deferred, clock_anomaly_int),
        )
        run_id = cursor.lastrowid
        if run_events:
            connection.executemany(
                'INSERT INTO check_run_events(run_id, dedup_key, outcome, task_id, suppressed_by_task_id) VALUES(?, ?, ?, ?, ?)',
                [(run_id, dk, ev_out, tid, sup_id) for dk, ev_out, tid, sup_id in run_events],
            )
        connection.execute(
            '''UPDATE checks
               SET last_started=?, last_actual_started_at=?, last_outcome=?, fault_count=?, updated_at=?
               WHERE name=?''',
            (tick_started_iso, actual_started_iso, outcome, new_fault_count, finished_iso, chk['name']),
        )
        return {'run_id': run_id, 'outcome': outcome, 'events_deferred': events_deferred, 'created_task_ids': created_task_ids}


def _deliver_wake_run_inbox(candidate, *, run_inbox_bin, run_id_box, timeout, mono_fn):
    """窓口の run-inbox へ 1 件投入する (本文に非信頼テキストは混ぜない)。"""
    deadline = mono_fn() + timeout
    target_run = run_id_box[0]
    if not target_run:
        rem = deadline - mono_fn()
        if rem <= 0:
            return False, 'active timeout'
        rc, out, err, timed_out = _run_subprocess_bounded([run_inbox_bin, 'active'], timeout=rem, kill_grace=0.2)
        if timed_out or rc != 0:
            return False, f"run-inbox active failed (rc={rc}): {err.decode('utf-8', errors='replace').strip()}"
        resolved = out.decode('utf-8', errors='replace').strip().splitlines()
        if not resolved or not resolved[0].strip():
            return False, 'run-inbox active returned empty run id'
        target_run = resolved[0].strip()
        run_id_box[0] = target_run

    rem = deadline - mono_fn()
    if rem <= 0:
        return False, 'send timeout'
    # 非信頼データ (title / body) は載せず、返信不要タグ [info] と静的メタデータのみ送る
    message = f"[info] check={candidate['check_name']} kind={candidate['kind']} task_id={candidate['task_id']} skill={candidate['skill']}"
    rc, _out, err, timed_out = _run_subprocess_bounded(
        [run_inbox_bin, 'send', '--run', target_run, '--req', candidate['task_id'], message],
        timeout=rem,
        kill_grace=0.2,
    )
    if timed_out or rc != 0:
        return False, f"run-inbox send failed (rc={rc}): {err.decode('utf-8', errors='replace').strip()}"
    return True, ''


def run_wake_pass(
    db_path, *, now_iso_fn, mono_fn=time.monotonic,
    run_inbox_bin=None, wake_run_id=None, skip_task_ids=None,
    pass_budget=CHECK_WAKE_PASS_BUDGET_SEC, item_timeout=CHECK_WAKE_ITEM_TIMEOUT_SEC,
    cb_limit=CHECK_WAKE_CB_LIMIT, deliver_fn=None,
    busy_timeout_ms=CHECK_COMMIT_BUSY_TIMEOUT_MS,
    stop_on_db_contention=False,
    failed_ids_out=None,
):
    """1 回の wake パスを実行する。
    (1) I/O ループ前に回収 (claim 済み・終了済みの needs_wake=0) を無条件に一括 UPDATE。
    (2) トランザクション外で run-inbox 投入 (1 件 5 秒、パス合計 20 秒、2 件連続失敗で CB)。
    戻り値: (failed_task_ids: set[str], truncated_by_budget: bool, attempted_count: int)。"""
    pass_budget_pos = max(0.0, float(pass_budget))
    pass_deadline = mono_fn() + pass_budget_pos
    pass_real_deadline = time.monotonic() + pass_budget_pos
    skip = set(skip_task_ids or ())

    def _wake_db_busy_ms():
        rem = min(pass_deadline - mono_fn(), pass_real_deadline - time.monotonic())
        if rem <= 0:
            return 0
        return min(max(0, int(busy_timeout_ms)), int(rem * 1000))

    # 回収 (wake I/O ループの前に無条件で実行。notified_at は NULL のまま維持)
    try:
        with _check_db(db_path, busy_timeout_ms=_wake_db_busy_ms()) as connection:
            connection.execute('BEGIN IMMEDIATE')
            connection.execute(
                '''UPDATE check_tasks
                   SET needs_wake = 0
                   WHERE needs_wake = 1
                     AND NOT EXISTS (
                         SELECT 1 FROM tasks
                         WHERE tasks.id = check_tasks.task_id
                           AND tasks.lane = 'todo'
                           AND tasks.pane = ''
                           AND tasks.run = ''
                     )'''
            )

        # 候補取得 (I/O 中はトランザクションを保持しない)
        with _check_db(db_path, busy_timeout_ms=_wake_db_busy_ms()) as connection:
            candidates = [
                dict(row)
                for row in connection.execute(
                    '''SELECT ct.task_id, ct.check_name, ct.kind, ct.dedup_key, ct.skill,
                              ct.created_at, ct.last_wake_attempt_at
                       FROM check_tasks ct
                       JOIN tasks t ON t.id = ct.task_id
                       WHERE ct.needs_wake = 1
                         AND t.lane = 'todo'
                         AND t.pane = ''
                         AND t.run = ''
                       ORDER BY COALESCE(ct.last_wake_attempt_at, ct.created_at) ASC,
                                CASE WHEN ct.kind = 'fault' THEN 0 ELSE 1 END ASC,
                                ct.task_id ASC'''
                )
            ]
    except sqlite3.Error as db_error:
        if stop_on_db_contention and _is_sqlite_retryable_contention(db_error):
            return set(), True, 0
        raise

    candidates = [item for item in candidates if item['task_id'] not in skip]
    failed_ids = set()
    truncated = False
    consecutive_failures = 0
    attempted = 0
    run_id_box = [wake_run_id or os.environ.get('CUE_WAKE_RUN_ID') or None]
    inbox_bin = run_inbox_bin or default_run_inbox_bin()

    for candidate in candidates:
        remaining = pass_deadline - mono_fn()
        if remaining < item_timeout:
            truncated = True
            break
        attempted += 1
        attempt_iso = now_iso_fn()
        if deliver_fn is not None:
            ok = bool(deliver_fn(candidate, item_timeout))
        else:
            ok, _reason = _deliver_wake_run_inbox(
                candidate, run_inbox_bin=inbox_bin, run_id_box=run_id_box, timeout=item_timeout, mono_fn=mono_fn,
            )
        if ok:
            consecutive_failures = 0
        else:
            failed_ids.add(candidate['task_id'])
            if failed_ids_out is not None:
                failed_ids_out.add(candidate['task_id'])
            consecutive_failures += 1
        try:
            with _check_db(db_path, busy_timeout_ms=_wake_db_busy_ms()) as connection:
                connection.execute('BEGIN IMMEDIATE')
                if ok:
                    connection.execute(
                        'UPDATE check_tasks SET needs_wake=0, notified_at=?, last_wake_attempt_at=? WHERE task_id=?',
                        (attempt_iso, attempt_iso, candidate['task_id']),
                    )
                else:
                    connection.execute(
                        'UPDATE check_tasks SET last_wake_attempt_at=? WHERE task_id=?',
                        (attempt_iso, candidate['task_id']),
                    )
        except sqlite3.Error as db_error:
            if stop_on_db_contention and _is_sqlite_retryable_contention(db_error):
                return failed_ids, True, attempted
            raise
        if consecutive_failures >= cb_limit:
            break

    return failed_ids, truncated, attempted


def _commit_check_run_with_retry(
    db_path, tick_id, chk, tick_started_iso, actual_started_iso, finished_iso,
    *, exit_code, raw_stdout, raw_stderr, timed_out, aborted,
    retry_deadline, mono_fn, busy_timeout_ms=CHECK_COMMIT_BUSY_TIMEOUT_MS,
):
    """SQLite の一時的な書き込み競合 (locked / busy) に対し、command を再実行せず残り budget 内で commit を再試行する。
    rem <= 0 では commit を試行しない (初回を含む)。各試行の busy timeout は残り時間で clamp する。
    初回時点で既に rem <= 0 の場合は commit を呼ばず、期限超過を表す sqlite3.OperationalError を送出する。"""
    retry_mono_start = mono_fn()
    retry_budget = max(0.0, retry_deadline - retry_mono_start)
    retry_real_deadline = time.monotonic() + retry_budget
    last_error = None
    while True:
        mono_rem = retry_deadline - mono_fn()
        real_rem = retry_real_deadline - time.monotonic()
        rem = min(mono_rem, real_rem)
        if rem <= 0:
            if last_error is not None:
                raise last_error
            raise sqlite3.OperationalError(
                'commit retry deadline exceeded before the first attempt'
            )
        attempt_busy_ms = min(max(0, int(busy_timeout_ms)), max(0, int(rem * 1000))) if rem > 0 else 0
        try:
            return commit_check_run(
                db_path, tick_id, chk, tick_started_iso, actual_started_iso, finished_iso,
                exit_code=exit_code, raw_stdout=raw_stdout, raw_stderr=raw_stderr,
                timed_out=timed_out, aborted=aborted,
                busy_timeout_ms=attempt_busy_ms,
            )
        except sqlite3.Error as db_error:
            if not _is_sqlite_retryable_contention(db_error):
                raise
            last_error = db_error
            mono_rem = retry_deadline - mono_fn()
            real_rem = retry_real_deadline - time.monotonic()
            rem = min(mono_rem, real_rem)
            if rem <= 0 or int(rem * 1000) <= 0:
                raise
            time.sleep(min(0.05, rem / 2.0))
            mono_rem = retry_deadline - mono_fn()
            real_rem = retry_real_deadline - time.monotonic()
            if min(mono_rem, real_rem) <= 0:
                raise


def prune_check_history(
    db_path, current_tick_id, cutoff_iso, *,
    batch_size=CHECK_PRUNE_BATCH, deadline=None, mono_fn=time.monotonic,
    busy_timeout_ms=CHECK_COMMIT_BUSY_TIMEOUT_MS,
):
    """30 日を超えた check_runs / check_run_events / check_ticks を小さいバッチに分けて削除する。
    deadline が指定されている場合は残り budget 内のバッチのみ実行し、DB 待ちも残り budget 内に抑え、
    保持される run を持つ親 tick は削除しない。"""
    real_deadline = None
    first_mono = None
    if deadline is not None:
        first_mono = mono_fn()
        real_deadline = time.monotonic() + max(0.0, deadline - first_mono)

    def _batch_busy_ms():
        nonlocal first_mono
        if deadline is None:
            return max(0, int(busy_timeout_ms))
        if first_mono is not None:
            now_m = first_mono
            first_mono = None
        else:
            now_m = mono_fn()
        rem = min(deadline - now_m, real_deadline - time.monotonic())
        if rem <= 0:
            return None
        return min(max(0, int(busy_timeout_ms)), int(rem * 1000))

    while True:
        batch_busy_ms = _batch_busy_ms()
        if batch_busy_ms is None:
            return False
        try:
            with _check_db(db_path, busy_timeout_ms=batch_busy_ms) as connection:
                connection.execute('BEGIN IMMEDIATE')
                run_ids = [
                    row['id']
                    for row in connection.execute(
                        'SELECT id FROM check_runs WHERE started_at < ? ORDER BY id LIMIT ?',
                        (cutoff_iso, batch_size),
                    )
                ]
                if not run_ids:
                    break
                marks = ','.join('?' * len(run_ids))
                connection.execute(f'DELETE FROM check_run_events WHERE run_id IN ({marks})', run_ids)
                connection.execute(f'DELETE FROM check_runs WHERE id IN ({marks})', run_ids)
        except sqlite3.Error as db_error:
            if deadline is not None and _is_sqlite_retryable_contention(db_error):
                return False
            raise

    while True:
        batch_busy_ms = _batch_busy_ms()
        if batch_busy_ms is None:
            return False
        try:
            with _check_db(db_path, busy_timeout_ms=batch_busy_ms) as connection:
                connection.execute('BEGIN IMMEDIATE')
                tick_ids = [
                    row['id']
                    for row in connection.execute(
                        '''SELECT id FROM check_ticks
                           WHERE started_at < ?
                             AND id != ?
                             AND NOT EXISTS (SELECT 1 FROM check_runs WHERE check_runs.tick_id = check_ticks.id)
                           ORDER BY id LIMIT ?''',
                        (cutoff_iso, current_tick_id, batch_size),
                    )
                ]
                if not tick_ids:
                    break
                marks = ','.join('?' * len(tick_ids))
                connection.execute(f'DELETE FROM check_ticks WHERE id IN ({marks})', tick_ids)
        except sqlite3.Error as db_error:
            if deadline is not None and _is_sqlite_retryable_contention(db_error):
                return False
            raise
    return True


def run_check_tick(
    db_path, config_path, *,
    run_inbox_bin=None, wake_run_id=None,
    now_fn=None, mono_fn=time.monotonic,
    tick_budget=CHECK_TICK_BUDGET_SEC,
    wake_pass_budget=CHECK_WAKE_PASS_BUDGET_SEC,
    wake_item_timeout=CHECK_WAKE_ITEM_TIMEOUT_SEC,
    kill_grace=CHECK_KILL_GRACE_SEC,
    finalize_reserve=None,
    commit_reserve=None,
    commit_busy_timeout_ms=CHECK_COMMIT_BUSY_TIMEOUT_MS,
    deliver_fn=None,
):
    """app.py check tick の本体。設計書 (issue-701-v9) の処理順 (1)〜(9) を厳密に実行する。"""
    db_path = _resolve_check_db_path(db_path)
    if tick_budget is None:
        tick_budget = CHECK_TICK_BUDGET_SEC
    if wake_pass_budget is None:
        wake_pass_budget = CHECK_WAKE_PASS_BUDGET_SEC
    if wake_item_timeout is None:
        wake_item_timeout = CHECK_WAKE_ITEM_TIMEOUT_SEC
    if kill_grace is None:
        kill_grace = CHECK_KILL_GRACE_SEC

    mono_start = mono_fn()
    tick_deadline = mono_start + tick_budget
    env_now = _parse_check_now_env() if now_fn is None else None
    if now_fn is not None:
        tick_started_ts = float(now_fn())
        wall_now_fn = lambda: float(now_fn())
    elif env_now is not None:
        tick_started_ts = env_now
        wall_now_fn = lambda: tick_started_ts + max(0.0, mono_fn() - mono_start)
    else:
        tick_started_ts = time.time()
        wall_now_fn = time.time
    tick_started_iso = _iso_utc(tick_started_ts)
    now_iso_fn = lambda: _iso_utc(wall_now_fn())

    # (1) flock 取得 (DB と同じ永続ディレクトリの固定 lockfile。終了時に unlink しない)
    lock_file = check_lock_path(db_path)
    lock_file.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    lock_fd = os.open(lock_file, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno not in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                raise
            # ロック不取得: WAL 切替・DDL を行わず最小の skip 記録だけ行う
            skip_id = _record_skipped_tick(db_path, tick_started_iso)
            return {
                'tick_id': skip_id, 'status': 'skipped', 'started_at': tick_started_iso,
                'finished_at': tick_started_iso, 'wake_failed': 0, 'unnotified_tasks': 0,
                'checks_deferred': None,
            }, 0

        # (1) と (2) の間: WAL 切替と additive テーブル作成はここでのみ行う
        ensure_check_schema(db_path)

        # (2) check_ticks に status='running' で INSERT
        with _check_db(db_path) as connection:
            connection.execute('BEGIN IMMEDIATE')
            cursor = connection.execute(
                "INSERT INTO check_ticks(started_at, finished_at, status, wake_failed, unnotified_tasks, checks_deferred) VALUES(?, NULL, 'running', 0, 0, NULL)",
                (tick_started_iso,),
            )
            tick_id = cursor.lastrowid

        all_failed = set()
        any_wake_truncated = False

        def _finalize_tick(status, exit_code, checks_deferred_val, *, error_msg=None, end_wake_skipped=False):
            nonlocal any_wake_truncated
            finished_iso = now_iso_fn()
            finalize_rem = tick_deadline - mono_fn()
            finalize_busy_ms = min(CHECK_COMMIT_BUSY_TIMEOUT_MS, max(0, int(finalize_rem * 1000)))
            unnotified = None
            result_status = status
            result_exit_code = exit_code
            result_error = error_msg
            try:
                with _check_db(db_path, busy_timeout_ms=finalize_busy_ms) as connection:
                    connection.execute('BEGIN IMMEDIATE')
                    unnotified = connection.execute('SELECT COUNT(*) FROM check_tasks WHERE needs_wake=1').fetchone()[0]
                    connection.execute(
                        'UPDATE check_ticks SET finished_at=?, status=?, wake_failed=?, unnotified_tasks=?, checks_deferred=? WHERE id=?',
                        (finished_iso, status, len(all_failed), unnotified, checks_deferred_val, tick_id),
                    )
            except sqlite3.Error as db_error:
                print(f'error: failed to finalize check tick {tick_id}: {db_error}', file=sys.stderr)
                if result_status != 'config_error':
                    result_status = 'failed'
                    result_exit_code = 1
                if result_error is None:
                    result_error = str(db_error)

            if end_wake_skipped and (unnotified is None or unnotified > 0):
                any_wake_truncated = True

            if all_failed or any_wake_truncated:
                print(
                    f'warning: check tick wake issue (wake_failed={len(all_failed)}, truncated={any_wake_truncated}, unnotified={unnotified})',
                    file=sys.stderr,
                )

            result_body = {
                'tick_id': tick_id, 'status': result_status, 'started_at': tick_started_iso,
                'finished_at': finished_iso, 'wake_failed': len(all_failed),
                'unnotified_tasks': unnotified, 'checks_deferred': checks_deferred_val,
            }
            if result_error is not None:
                result_body['error'] = result_error
            return result_body, result_exit_code

        # (3) tick 開始時 wake (config 検査より前に行う)
        try:
            start_failed, start_truncated, _ = run_wake_pass(
                db_path, now_iso_fn=now_iso_fn, mono_fn=mono_fn,
                run_inbox_bin=run_inbox_bin, wake_run_id=wake_run_id,
                pass_budget=wake_pass_budget, item_timeout=wake_item_timeout,
                deliver_fn=deliver_fn,
                busy_timeout_ms=commit_busy_timeout_ms,
                failed_ids_out=all_failed,
            )
        except sqlite3.Error as db_error:
            any_wake_truncated = True
            print(f'error: failed during start wake pass for check tick {tick_id}: {db_error}', file=sys.stderr)
            return _finalize_tick('failed', 1, None, error_msg=str(db_error))
        all_failed |= start_failed
        any_wake_truncated = start_truncated

        # (4) checkout 検査と TOML 読込・検証
        try:
            if not config_path:
                raise CheckConfigError('--config または CUE_CHECKS_CONFIG を指定してください')
            _checkout_root, config_dir, checks_cfg = load_and_validate_checks_config(config_path, mono_fn=mono_fn)
        except CheckConfigError as error:
            # config_error: (5)〜(8) を飛ばし、(9) は必ず実行
            print(f'error: check tick config_error: {error}', file=sys.stderr)
            return _finalize_tick('config_error', 1, None, error_msg=str(error))

        effective_reserve = (
            min(CHECK_FINALIZE_RESERVE_SEC, max(0.0, float(tick_budget) * 0.1))
            if finalize_reserve is None
            else max(0.0, float(finalize_reserve))
        )
        end_work_deadline = tick_deadline - effective_reserve

        # (5) checks テーブルの鏡同期 (DB 待ち時間は end_work_deadline までの残り作業時間で上限する)
        mirror_rem = end_work_deadline - mono_fn()
        if mirror_rem <= 0:
            return _finalize_tick('aborted', 0, None)
        mirror_busy_ms = min(max(0, int(commit_busy_timeout_ms)), max(0, int(mirror_rem * 1000)))
        try:
            sync_checks_mirror(
                db_path, checks_cfg, now_iso_fn(),
                busy_timeout_ms=mirror_busy_ms,
            )
        except sqlite3.Error as db_error:
            print(f'error: failed to sync checks mirror for check tick {tick_id}: {db_error}', file=sys.stderr)
            return _finalize_tick('failed', 1, None, error_msg=str(db_error))

        # (6) due な check の実行
        due_checks = select_due_checks(db_path, checks_cfg, tick_started_ts)
        work_budget = max(0.0, end_work_deadline - mono_start)
        effective_commit_reserve = (
            min(CHECK_COMMIT_RESERVE_SEC, work_budget * 0.1)
            if commit_reserve is None
            else max(0.0, float(commit_reserve))
        )
        commit_start_deadline = end_work_deadline - effective_commit_reserve
        pre_commit_budget = max(0.0, commit_start_deadline - mono_start)
        max_cleanup_sec = max(0.0, float(kill_grace)) + CHECK_KILL_WAIT_SEC + CHECK_DRAIN_JOIN_SEC
        effective_cleanup_reserve = min(max_cleanup_sec, pre_commit_budget * 0.5)
        exec_deadline = commit_start_deadline - effective_cleanup_reserve
        checks_deferred = 0
        tick_status = 'ok'
        tick_exit_code = 0

        if (exec_deadline - mono_fn()) <= 0:
            # step (6) 開始時点で実行・cleanup・保存予算の残り時間 0 以下: 0 件実行、全件 checks_deferred、status=aborted
            checks_deferred = len(due_checks)
            tick_status = 'aborted'
        else:
            for idx, chk in enumerate(due_checks):
                remaining = exec_deadline - mono_fn()
                if idx == 0:
                    if remaining <= 0:
                        checks_deferred += len(due_checks) - idx
                        tick_status = 'aborted'
                        break
                else:
                    if remaining < chk['timeout']:
                        checks_deferred += 1
                        continue

                actual_started_iso = now_iso_fn()
                launch_state = {'refused': False}
                exit_code, raw_stdout, raw_stderr, timed_out, aborted = run_check_command(
                    chk, config_dir, exec_deadline,
                    mono_fn=mono_fn, kill_grace=kill_grace,
                    cleanup_deadline=commit_start_deadline, launch_state=launch_state,
                )
                if aborted and launch_state['refused']:
                    # 起動直前ガードで未着手となった due check は step (6) 開始時・ループ前ガードと
                    # 同一規則 (issue-701-v9) で記録せず checks_deferred に計上する。
                    checks_deferred += len(due_checks) - idx
                    if idx == 0:
                        tick_status = 'aborted'
                    break
                finished_run_iso = now_iso_fn()
                try:
                    run_result = _commit_check_run_with_retry(
                        db_path, tick_id, chk, tick_started_iso, actual_started_iso, finished_run_iso,
                        exit_code=exit_code, raw_stdout=raw_stdout, raw_stderr=raw_stderr,
                        timed_out=timed_out, aborted=aborted,
                        retry_deadline=end_work_deadline, mono_fn=mono_fn,
                        busy_timeout_ms=commit_busy_timeout_ms,
                    )
                except sqlite3.Error as db_error:
                    print(f"error: failed to commit check run {chk['name']!r}: {db_error}", file=sys.stderr)
                    checks_deferred += len(due_checks) - idx - 1
                    tick_status = 'failed'
                    tick_exit_code = 1
                    break

                if run_result['outcome'] == 'aborted':
                    checks_deferred += len(due_checks) - idx - 1
                    tick_status = 'aborted'
                    break

        # (7) tick 終了時 wake と (8) prune (step (6) が aborted / failed の場合は飛ばす。
        # end 側の任意作業は tick の残り budget (finalize 予約を除く) 内に制限する)
        end_wake_skipped_by_budget = False
        if tick_status == 'ok':
            remaining_for_end = max(0.0, end_work_deadline - mono_fn())
            if remaining_for_end > 0:
                end_wake_budget = min(wake_pass_budget, remaining_for_end)
                end_failed, end_truncated, _ = run_wake_pass(
                    db_path, now_iso_fn=now_iso_fn, mono_fn=mono_fn,
                    run_inbox_bin=run_inbox_bin, wake_run_id=wake_run_id,
                    skip_task_ids=start_failed,
                    pass_budget=end_wake_budget, item_timeout=wake_item_timeout,
                    deliver_fn=deliver_fn,
                    busy_timeout_ms=commit_busy_timeout_ms,
                    stop_on_db_contention=True,
                )
                all_failed |= end_failed
                any_wake_truncated = any_wake_truncated or end_truncated
            else:
                end_wake_skipped_by_budget = True

            if mono_fn() < end_work_deadline:
                cutoff_iso = _iso_utc(tick_started_ts - CHECK_RETENTION_DAYS * 86400)
                prune_check_history(
                    db_path, tick_id, cutoff_iso,
                    deadline=end_work_deadline, mono_fn=mono_fn,
                )

        # (9) check_ticks を最終 status に UPDATE (DB 待ちも公称 tick_deadline 内に制限)
        return _finalize_tick(
            tick_status, tick_exit_code, checks_deferred,
            end_wake_skipped=end_wake_skipped_by_budget,
        )
    finally:
        with suppress(OSError):
            os.close(lock_fd)


@contextmanager
def _check_readonly_db(db_path):
    """読み取り専用の接続。WAL 切替・DDL・DB 新規作成を行わない。"""
    target = _resolve_check_db_path(db_path)
    if not target.exists():
        yield None
        return
    connection = sqlite3.connect(target.as_uri() + '?mode=ro', timeout=5.0, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
    finally:
        with suppress(sqlite3.Error):
            connection.rollback()
        connection.close()


def _table_exists(connection, name):
    return connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def check_list_data(db_path):
    with _check_readonly_db(db_path) as connection:
        if connection is None:
            return {'checks': []}
        connection.execute('BEGIN')
        if not _table_exists(connection, 'checks'):
            return {'checks': []}
        rows = connection.execute('SELECT * FROM checks ORDER BY enabled DESC, toml_order ASC, name ASC').fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item['enabled'] = bool(item['enabled'])
            try:
                item['env'] = json.loads(item['env'])
            except (TypeError, ValueError):
                item['env'] = {}
            items.append(item)
        return {'checks': items}


def format_check_list(data):
    checks = data['checks']
    if not checks:
        return '登録されている check はありません'
    lines = []
    for item in checks:
        state = 'enabled' if item['enabled'] else 'disabled'
        outcome = item['last_outcome'] or '-'
        started = item['last_started'] or '-'
        lines.append(
            f"- {item['name']} [{state}] interval={item['interval']}s timeout={item['timeout']}s "
            f"skill={item['skill']} last={outcome} ({started}) faults={item['fault_count']}/{item['fault_threshold']}"
        )
    return '\n'.join(lines)


def check_status_data(db_path, *, limit=20, now_fn=None):
    env_now = _parse_check_now_env() if now_fn is None else None
    now_ts = float(now_fn()) if now_fn is not None else (env_now if env_now is not None else time.time())
    cutoff_24h = _iso_utc(now_ts - 86400)

    empty = {
        'last_ok_tick_at': None,
        'recent_ticks': [],
        'running_leftover_count': 0,
        'running_in_progress_count': 0,
        'unnotified_tasks': 0,
        'last_completed_wake_failed': None,
        'checks_deferred_24h': {'total': 0, 'ticks': 0},
        'events_deferred_24h': 0,
        'max_overdue_sec': 0.0,
        'max_overdue_check': None,
    }
    with _check_readonly_db(db_path) as connection:
        if connection is None:
            return empty
        # 最初の table probe の前に read transaction を開始し、全クエリを単一スナップショットから読む
        connection.execute('BEGIN')
        if _table_exists(connection, 'check_ticks'):
            ok_row = connection.execute(
                "SELECT COALESCE(finished_at, started_at) AS ts FROM check_ticks WHERE status='ok' ORDER BY id DESC LIMIT 1"
            ).fetchone()
            empty['last_ok_tick_at'] = ok_row['ts'] if ok_row else None

            ticks = [
                dict(row)
                for row in connection.execute(
                    'SELECT id, started_at, finished_at, status, wake_failed, unnotified_tasks, checks_deferred FROM check_ticks ORDER BY id DESC LIMIT ?',
                    (max(1, int(limit)),),
                )
            ]
            ticks.reverse()
            empty['recent_ticks'] = ticks
            # flock で直列化された後続の非 skipped tick が存在する場合のみ、それ以前の running 行を異常終了の残留と確定できる。
            # 最新の非 skipped tick が running の場合は正常に実行中であり得るため異常終了と断定しない。
            latest_holder = connection.execute(
                "SELECT id, status FROM check_ticks WHERE status != 'skipped' ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if latest_holder is not None:
                empty['running_leftover_count'] = connection.execute(
                    "SELECT COUNT(*) FROM check_ticks WHERE status='running' AND id < ?",
                    (latest_holder['id'],),
                ).fetchone()[0]
                empty['running_in_progress_count'] = 1 if latest_holder['status'] == 'running' else 0

            completed_row = connection.execute(
                "SELECT wake_failed FROM check_ticks WHERE status IN ('ok', 'aborted', 'config_error', 'failed') ORDER BY id DESC LIMIT 1"
            ).fetchone()
            empty['last_completed_wake_failed'] = completed_row['wake_failed'] if completed_row else None

            def_row = connection.execute(
                '''SELECT COALESCE(SUM(checks_deferred), 0) AS total,
                          SUM(CASE WHEN checks_deferred > 0 THEN 1 ELSE 0 END) AS ticks
                   FROM check_ticks
                   WHERE started_at >= ? AND checks_deferred IS NOT NULL''',
                (cutoff_24h,),
            ).fetchone()
            empty['checks_deferred_24h'] = {'total': int(def_row['total'] or 0), 'ticks': int(def_row['ticks'] or 0)}

        if _table_exists(connection, 'check_tasks'):
            empty['unnotified_tasks'] = connection.execute(
                'SELECT COUNT(*) FROM check_tasks WHERE needs_wake=1'
            ).fetchone()[0]

        if _table_exists(connection, 'check_runs'):
            ev_row = connection.execute(
                'SELECT COALESCE(SUM(events_deferred), 0) AS total FROM check_runs WHERE started_at >= ?',
                (cutoff_24h,),
            ).fetchone()
            empty['events_deferred_24h'] = int(ev_row['total'] or 0)

        if _table_exists(connection, 'checks'):
            max_overdue = 0.0
            max_name = None
            for row in connection.execute(
                'SELECT name, interval, last_started, last_actual_started_at, last_outcome FROM checks WHERE enabled=1'
            ):
                if row['last_started'] is None or row['last_actual_started_at'] is None or row['last_outcome'] is None:
                    continue
                base_iso = row['last_started'] if row['last_outcome'] in CHECK_SUCCESS_OUTCOMES else row['last_actual_started_at']
                overdue = (now_ts - _parse_iso_ts(base_iso)) - float(row['interval'])
                if overdue > max_overdue:
                    max_overdue = overdue
                    max_name = row['name']
            empty['max_overdue_sec'] = round(max_overdue, 3)
            empty['max_overdue_check'] = max_name

    return empty


def format_check_status(data):
    lines = [
        f"last_ok_tick_at: {data['last_ok_tick_at'] or 'none'}",
        f"unnotified_tasks: {data['unnotified_tasks']}",
        f"last_completed_wake_failed: {data['last_completed_wake_failed'] if data['last_completed_wake_failed'] is not None else 'none'}",
        f"checks_deferred_24h: total={data['checks_deferred_24h']['total']} ticks={data['checks_deferred_24h']['ticks']}",
        f"events_deferred_24h: {data['events_deferred_24h']}",
        f"max_overdue: {data['max_overdue_sec']}s" + (f" ({data['max_overdue_check']})" if data['max_overdue_check'] else ''),
    ]
    if data['running_leftover_count'] > 0:
        lines.append(f"WARNING: running 残留 tick が {data['running_leftover_count']} 件あります (異常終了の痕跡)")
    if data.get('running_in_progress_count', 0) > 0:
        lines.append('running_in_progress: 1 (最新 tick が status=running です。実行中の可能性があります)')
    if data['recent_ticks']:
        lines.append('recent_ticks:')
        for tick in data['recent_ticks']:
            deferred = '-' if tick['checks_deferred'] is None else str(tick['checks_deferred'])
            lines.append(
                f"  #{tick['id']} {tick['started_at']} status={tick['status']} "
                f"wake_failed={tick['wake_failed']} unnotified={tick['unnotified_tasks']} deferred={deferred}"
            )
    else:
        lines.append('recent_ticks: (none)')
    return '\n'.join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Local decision inbox')
    parser.add_argument('--db', type=Path, default=default_db())
    commands = parser.add_subparsers(dest='command', required=True)
    serve = commands.add_parser('serve')
    serve.add_argument('--port', type=int, default=4317)
    serve.add_argument('--demo', action='store_true')
    serve.add_argument('--files-root', action='append', help='判断の files を読める場所。繰り返し指定できる (既定は CUE_FILES_ROOT を : で区切ったもの、無ければ FILES_ROOTS)')
    epilog_format = argparse.RawDescriptionHelpFormatter
    add = commands.add_parser('add', help='判断を登録する', formatter_class=epilog_format, epilog=add_help())
    add.add_argument('--file', type=Path, required=True, help='登録する判断の JSON ファイル')
    listing = commands.add_parser('list', help='判断の一覧を JSON で出す', formatter_class=epilog_format, epilog='出力は JSON の object で、配列ではない: {"decisions": [判断, ...], "demo": false}。jq なら .decisions[]\n判断は登録時の項目に id, created_at, answer (未回答は null), revision, withdrawn (取り下げ済みでなければ null) などが付く')
    listing.add_argument('--include-withdrawn', action='store_true')
    update = commands.add_parser('update', help='未回答の判断の内容を差し替える')
    update.add_argument('--id', required=True)
    update.add_argument('--note', required=True)
    update.add_argument('--file', type=Path, required=True, help='変更する項目だけの JSON オブジェクト')
    withdraw = commands.add_parser('withdraw', help='未回答の判断を取り下げる')
    withdraw.add_argument('--id', required=True)
    withdraw.add_argument('--note', required=True)
    watch = commands.add_parser('watch', help='処理可能な回答の ID を JSONL で通知する')
    watch.add_argument('--interval', type=positive_seconds, default=2)
    watch.add_argument('--timeout', type=positive_seconds, default=1740)
    watch.add_argument('--key-prefix')
    tui = commands.add_parser('tui', help='判断を端末で確認して回答する')
    tui.add_argument('--web-url', default=os.environ.get('CUE_WEB_URL') or None)
    tui.add_argument('--files-root', action='append', help='serve と同じ')
    claim = commands.add_parser('claim')
    claim.add_argument('--worker', required=True)
    claim.add_argument('--lease-seconds', type=int, default=300)
    claim.add_argument('--key-prefix')
    claim.add_argument('--token-file', help='token を stdout に出さずファイルへ保存する。auto は claims_dir に claim ごとの新しいファイルを作る')
    claims = commands.add_parser('claims', help='claims_dir の token ファイルを列挙する (token の値は出さない)')
    claims.add_argument('--id')
    commands.add_parser('hooks', help='pre-add の実行パスを JSON で出す')
    complete = commands.add_parser('complete')
    complete.add_argument('--id', required=True)
    undo = commands.add_parser('undo')
    undo.add_argument('--id', required=True)
    undo.add_argument('--decided-at', required=True)
    report = commands.add_parser('report')
    report.add_argument('--id', required=True)
    report.add_argument('--status', choices=('blocked', 'failed'), required=True)
    report.add_argument('--reason', required=True)
    report.add_argument('--next-step', required=True)
    retry = commands.add_parser('retry')
    retry.add_argument('--id', required=True)
    retry.add_argument('--decided-at', required=True)
    retry.add_argument('--reported-at', required=True)
    close = commands.add_parser('close', help='停止した判断を完了扱いで閉じる')
    close.add_argument('--id', required=True)
    close.add_argument('--decided-at', required=True)
    close.add_argument('--reported-at', required=True)
    renew = commands.add_parser('renew')
    renew.add_argument('--id', required=True)
    renew.add_argument('--lease-seconds', type=int, default=300)
    backup = commands.add_parser('backup')
    backup.add_argument('--output', type=Path, required=True)
    task = commands.add_parser('task', help='依頼の進み具合を管理する')
    task_commands = task.add_subparsers(dest='task_command', required=True)
    for name in ('add', 'move', 'reconcile-lane', 'update', 'delete', 'list'):
        # move と add の --lane は Store が断る。error の文を出すため parser には残す (help には出さない)
        sub = task_commands.add_parser(name, formatter_class=epilog_format, help=RECONCILE_HELP if name == 'reconcile-lane' else None, description=RECONCILE_HELP if name == 'reconcile-lane' else None, epilog='既定は lane ごとの文字の一覧。--json は {"lanes": [{"id": "lane 名", "label": "表示名", "tasks": [task, ...]}, ...]} (lane は ' + ' / '.join(LANES) + ' の順)' if name == 'list' else None)
        if name != 'list':
            sub.add_argument('--id', required=True)
        if name == 'add':
            sub.add_argument('--title', required=True)
        if name == 'update':
            sub.add_argument('--title')
        if name in ('add', 'update'):
            for option in ('request', 'pane', 'run', 'result', 'remaining', 'decision'):
                sub.add_argument('--' + option)
            # choices にせず Store の検査に任せる (error は JSON で exit 1)
            sub.add_argument('--kind', help='種類 (add では必須): ' + ' / '.join(TASK_KINDS) + ' (判断 / 監視 / 調査 / 実装)')
            sub.add_argument('--done-when', help='完了の条件 (add では必須。1〜1000 字)')
        if name in ('add', 'move', 'reconcile-lane'):
            sub.add_argument('--lane', required=name == 'reconcile-lane', help=None if name == 'reconcile-lane' else argparse.SUPPRESS)
        if name == 'reconcile-lane':
            sub.add_argument('--from', dest='expect', help='書く前の lane。今の lane が違えば何も書かず exit 3 (競合)。省くと lane を確かめずに書く')
            sub.add_argument('--machine', default='', help='機械の欄。停止の疑い (機械判定の日付) や Cue 回答済みの印。省くと空にする')
            sub.add_argument('--pane', help='更新する pane。--from / --from-pane / --from-machine が必要。空文字で解除')
            for key in ('pane', 'machine', 'run', 'request', 'kind'):
                scope_help = '。--from-run / --from-request / --from-kind は一緒に指定' if key in ('run', 'request', 'kind') else ''
                sub.add_argument('--from-' + key, dest='expect_' + key, help=f'書く前の {key}。空文字も比較する{scope_help}')
            snapshot_help = '。--from-remaining / --from-decision-id / --from-updated-at は一緒に指定'
            sub.add_argument('--from-remaining', dest='expect_remaining', help='書く前の remaining。空文字も比較する' + snapshot_help)
            sub.add_argument('--from-decision-id', dest='expect_decision_id', help='書く前の decision_id。空文字は関連付けなし' + snapshot_help)
            sub.add_argument('--from-updated-at', dest='expect_updated_at', help='書く前の updated_at' + snapshot_help)
        if name == 'list':
            sub.add_argument('--lane')
            sub.add_argument('--json', action='store_true')
    run = commands.add_parser('run', help='agent の実行 (run) と pane の対応を記録する')
    run_commands = run.add_subparsers(dest='run_command', required=True)
    for name in ('open', 'close', 'show'):
        sub = run_commands.add_parser(name, help={'open': 'run を開く。同じ run_id と role の開いた行があれば断る', 'close': 'run を閉じて closed_at を入れる', 'show': 'run_id か pane と socket の組で引く。既定は開いた行だけ'}[name])
        if name == 'show':
            sub.add_argument('--run-id')
            sub.add_argument('--pane')
            sub.add_argument('--socket')
            sub.add_argument('--all', action='store_true', help='閉じた行も出す')
        else:
            sub.add_argument('--run-id', required=True)
            sub.add_argument('--role', required=True)
        if name == 'open':
            sub.add_argument('--pane', required=True)
            sub.add_argument('--socket', required=True)
    session = commands.add_parser('session', help='セッションの所属と累積計測を sessions 表に記録する')
    session_commands = session.add_subparsers(dest='session_command', required=True)
    for name in ('open', 'close', 'sample', 'list'):
        sub = session_commands.add_parser(
            name,
            formatter_class=epilog_format,
            help={'open': 'session を開く。開いた重複があれば断る', 'close': '開いた session に closed_at と close_reason を入れる', 'sample': 'session ごとの累積計測を記録する', 'list': '累積計測を含む session の一覧を id 順に出す'}[name],
            epilog='出力は {"sessions": [session, ...]} (id 順)' if name == 'list' else None,
        )
        if name == 'open':
            sub.add_argument('--run-id', required=True)
            # choices にせず Store の検査に任せる (error は JSON で exit 1)
            sub.add_argument('--role', required=True, help=' / '.join(SESSION_ROLES))
            sub.add_argument('--cli', required=True, help=' / '.join(SESSION_CLIS))
            sub.add_argument('--session-id', required=True, help='CLI 固有のセッション ID (UUID 等)')
            sub.add_argument('--pane', required=True)
            sub.add_argument('--socket', required=True)
            sub.add_argument('--req', help='関連する依頼 id (task id)')
        elif name == 'close':
            sub.add_argument('--session-id', help='--cli と組で指定する')
            sub.add_argument('--cli', help='--session-id と組で指定する (' + ' / '.join(SESSION_CLIS) + ')')
            sub.add_argument('--pane', help='--socket・--run-id と組で指定する')
            sub.add_argument('--socket', help='--pane・--run-id と組で指定する')
            sub.add_argument('--run-id', help='--pane・--socket と組で指定する')
            sub.add_argument('--role', help='--pane・--socket・--run-id で role を絞り込む (' + ' / '.join(SESSION_ROLES) + ')')
            sub.add_argument('--reason', required=True, help=' / '.join(SESSION_CLOSE_REASONS))
        elif name == 'sample':
            sub.add_argument('--id', required=True, type=int, help='session list の行 ID')
            sub.add_argument('--observed-at', required=True, help='観測時刻 (timezone 付き ISO 8601)')
            sub.add_argument('--turn-count', type=int, help='累積 turn 数 (--input-tokens と少なくとも一方を指定)')
            sub.add_argument('--input-tokens', type=int, help='累積入力 token 数 (--turn-count と少なくとも一方を指定)')
        else:
            sub.add_argument('--run-id')
            sub.add_argument('--cli', help=' / '.join(SESSION_CLIS))
            sub.add_argument('--open-only', action='store_true', help='開いた行だけを出す')
            sub.add_argument('--since', help='この時刻以降に開いていた行を出す (timezone 付き ISO 8601)')
            sub.add_argument('--until', help='この時刻以前に開いた行を出す (timezone 付き ISO 8601)')
    msg = commands.add_parser('msg', help='窓口が受けた msg の状態を messages 表に記録する (本文はファイルが運ぶ)')
    msg_commands = msg.add_subparsers(dest='msg_command', required=True)
    for name in ('import', 'move', 'list', 'sync'):
        sub = msg_commands.add_parser(name, help={'import': 'msg を取り込む。あれば何もしない (冪等)', 'move': 'msg の状態を更新する。無ければ入れる', 'list': 'run の msg を id 順に出す', 'sync': 'inbox の new/ と hold/ から run の msg を同期する'}[name])
        sub.add_argument('--run-id', required=True)
        if name == 'list':
            # choices にせず Store の検査に任せる (error は JSON で exit 1)
            sub.add_argument('--state', help=' / '.join(MSG_STATES))
        elif name == 'sync':
            sub.add_argument('--inbox', required=True, type=Path, help='対象 run の inbox ディレクトリ')
        else:
            sub.add_argument('--msg', required=True)
            sub.add_argument('--state', required=True, help=' / '.join(MSG_STATES))
            sub.add_argument('--wait', help='state が held のときだけ。decision=ID / pr=URL / msg=名前')
            sub.add_argument('--req', help='msg の req: ヘッダの依頼 id (task id)')
    check = commands.add_parser('check', help='定期監視 check の実行と状態確認を行う')
    check_commands = check.add_subparsers(dest='check_command', required=True)
    check_tick = check_commands.add_parser('tick', help='due な check を実行し、タスク登録と run-inbox 通知を行う')
    check_tick.add_argument('--config', type=Path, default=None, help='checks.toml の絶対パス (既定は環境変数 CUE_CHECKS_CONFIG)')
    check_tick.add_argument('--run-inbox', dest='run_inbox', default=None, help='run-inbox.sh のパス (既定は CUE_RUN_INBOX_BIN)')
    check_tick.add_argument('--run', dest='wake_run', default=None, help='通知先の run id (省略時は run-inbox.sh active で解決)')
    check_list = check_commands.add_parser('list', help='checks テーブルの鏡一覧を表示する')
    check_list.add_argument('--json', action='store_true', help='JSON で出力する')
    check_status = check_commands.add_parser('status', help='tick 履歴・未通知タスク・遅延などの稼働状態を表示する')
    check_status.add_argument('--limit', type=int, default=20, help='直近 tick 表示件数 (既定 20)')
    check_status.add_argument('--json', action='store_true', help='JSON で出力する')
    for sub in (complete, report, renew):
        group = sub.add_mutually_exclusive_group(required=True)
        group.add_argument('--token')
        group.add_argument('--token-file', type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == 'hooks':
            # Store を開かない。対応版かを確かめるだけの呼び出しで DB を作ったり移行したりしないため
            hook = pre_add_hook()
            print(json.dumps({'pre_add': hook and str(hook)}, ensure_ascii=False))
            return 0
        if args.command == 'check':
            # Store を先に開かない。skip 経路と読み取り専用経路で WAL 切替・DDL を起こさないため
            if args.check_command == 'tick':
                config = args.config or (Path(os.environ['CUE_CHECKS_CONFIG']) if os.environ.get('CUE_CHECKS_CONFIG') else None)
                result, exit_code = run_check_tick(
                    args.db, config,
                    run_inbox_bin=args.run_inbox,
                    wake_run_id=args.wake_run,
                )
                print(json.dumps(result, ensure_ascii=False))
                return exit_code
            if args.check_command == 'list':
                data = check_list_data(args.db)
                print(json.dumps(data, ensure_ascii=False) if args.json else format_check_list(data))
                return 0
            if args.limit < 1:
                raise AppError(400, '--limit は 1 以上の整数を指定してください')
            data = check_status_data(args.db, limit=args.limit)
            print(json.dumps(data, ensure_ascii=False) if args.json else format_check_status(data))
            return 0
        if args.command == 'tui':
            web_url = tui_web_url(args.web_url)
            if not sys.stdin.isatty() or not sys.stdout.isatty():
                raise AppError(400, 'TUI は対話可能な端末から起動してください')
            try:
                from cue_tui import run
            except ImportError:
                raise AppError(400, 'この環境では TUI を利用できません') from None
            return run(Store(args.db), web_url=web_url, files_root=files_root(args.files_root))
        if args.command == 'serve':
            if not 0 <= args.port <= 65535:
                raise AppError(400, 'port は 0〜65535 を指定してください')
            with make_server(args.db, args.port, args.demo, args.files_root) as server:
                print(f'Listening on http://127.0.0.1:{server.server_port}/' + (' (demo)' if args.demo else ''), flush=True)
                try:
                    server.serve_forever()
                except KeyboardInterrupt:
                    pass
            return 0
        store = Store(args.db, readonly=args.command in ('watch', 'backup'))
        if args.command == 'run':
            if args.run_command == 'open':
                result = store.open_run({'run_id': args.run_id, 'role': args.role, 'pane': args.pane, 'socket': args.socket})
            elif args.run_command == 'close':
                result = store.close_run(args.run_id, args.role)
            else:
                result = store.runs(args.run_id, args.pane, args.socket, args.all)
            print(json.dumps(result, ensure_ascii=False))
            return 0
        if args.command == 'session':
            if args.session_command == 'open':
                payload = {
                    'run_id': args.run_id, 'role': args.role, 'pane': args.pane,
                    'socket': args.socket, 'cli': args.cli, 'native_session_id': args.session_id,
                }
                if args.req is not None:
                    payload['req_id'] = args.req
                result = store.open_session(payload)
            elif args.session_command == 'close':
                result = store.close_session(
                    cli=args.cli, native_session_id=args.session_id,
                    run_id=args.run_id, pane=args.pane, socket=args.socket,
                    role=args.role, reason=args.reason,
                )
            elif args.session_command == 'sample':
                result = store.sample_session(
                    args.id, args.observed_at, turn_count=args.turn_count, input_tokens=args.input_tokens,
                )
            else:
                result = store.sessions(
                    run_id=args.run_id, cli=args.cli, open_only=args.open_only,
                    since=args.since, until=args.until,
                )
            print(json.dumps(result, ensure_ascii=False))
            return 0
        if args.command == 'msg':
            if args.msg_command == 'list':
                result = store.messages(args.run_id, args.state)
            elif args.msg_command == 'sync':
                result = store.sync_messages(args.run_id, args.inbox)
            else:
                payload = {'run_id': args.run_id, 'msg': args.msg, 'state': args.state, **{k: v for k, v in (('wait', args.wait), ('req', args.req)) if v is not None}}
                result = (store.import_message if args.msg_command == 'import' else store.move_message)(payload)
            print(json.dumps(result, ensure_ascii=False))
            return 0
        if args.command == 'task':
            result = run_task(store, args)
            if result is None:
                print(format_tasks(store, args.lane))
                return 0
            print(json.dumps(result, ensure_ascii=False))
            return 0
        if args.command == 'watch':
            return watch_ready(store, args.interval, args.timeout, args.key_prefix)
        if args.command in ('add', 'update'):
            with args.file.open('rb') as handle:
                raw = handle.read(MAX_BODY + 1)
            if len(raw) > MAX_BODY:
                raise AppError(413, '送信サイズが上限を超えています')
            if args.command == 'add':
                payload = parse_json(raw)
                item = validate_request(payload)
                run_pre_add(item)
                require_run_prefix(store, item)
                result = store.add(payload)
            else:
                result = store.update(args.id, {'note': args.note, 'changes': parse_json(raw)})
        elif args.command == 'withdraw':
            result = store.withdraw(args.id, {'note': args.note})
        elif args.command == 'list':
            result = {'decisions': store.list(args.include_withdrawn), 'demo': False}
        elif args.command == 'claim':
            result = claim_to_file(store, args) if args.token_file is not None else store.claim(args.worker, args.lease_seconds, args.key_prefix)
        elif args.command == 'claims':
            result = list_claims(store, claims_dir(args.db), args.id)
        elif args.command == 'undo':
            result = store.undo(args.id, {'decided_at': args.decided_at})
        elif args.command == 'report':
            result = store.report(args.id, {'token': token_from(args), 'status': args.status, 'reason': args.reason, 'next_step': args.next_step})
            if args.token_file:
                unlink_temp(args.token_file, 'token ファイル')
        elif args.command == 'retry':
            result = store.retry(args.id, {'decided_at': args.decided_at, 'reported_at': args.reported_at})
        elif args.command == 'close':
            result = store.close(args.id, {'decided_at': args.decided_at, 'reported_at': args.reported_at})
        elif args.command == 'renew':
            if args.token_file:
                result = renew_with_file(store, args)
            else:
                result = store.renew(args.id, {'token': args.token, 'lease_seconds': args.lease_seconds})
        elif args.command == 'backup':
            result = store.backup(args.output)
        else:
            result = store.complete(args.id, token_from(args))
            # DB は commit 済みなので、消せなくても失敗にしない。呼び手が失敗とみなして再試行しても
            # token は既に無効で断られる。auto の file は残っても sweep_claims が lease 期限から 24 時間後に消す (明示パスは消えない)
            if args.token_file:
                unlink_temp(args.token_file, 'token ファイル')
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (AppError, sqlite3.Error, OSError) as error:
        message = str(error) if isinstance(error, AppError) else 'データの読み書きに失敗しました'
        print(json.dumps({'error': message}, ensure_ascii=False), file=sys.stderr)
        # 競合 (reconcile-lane の比較条件の不一致) だけ 3 にし、task 無しなどの 1 と呼び手が分けられるようにする (2 は argparse)
        return 3 if isinstance(error, LaneConflict) else 1


if __name__ == '__main__':
    raise SystemExit(main())
