#!/usr/bin/env python3
"""Local decision inbox, using only the Python standard library."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import sys
import tempfile
import time
from urllib.parse import urlsplit
import uuid

ROOT = Path(__file__).resolve().parent
MAX_BODY = 131072
ASSETS = {'/assets/demo-flow.svg': ('assets/demo-flow.svg', 'image/svg+xml')}
LANES = {'todo': '未着手', 'human_decision': '人の判断待ち', 'worker': 'worker 作業中', 'human_action': '人の操作待ち', 'done': '完了'}
TASK_FIELDS = ('title', 'request', 'pane', 'run', 'result', 'remaining', 'lane', 'decision_id')
# add の再送判定で比べる登録内容。lane / result / remaining / decision_id は move / update で進むので比べない
TASK_IDENTITY = ('title', 'request', 'pane', 'run')
TASK_LIMITS = {'title': 200, 'request': 1000, 'pane': 200, 'run': 200, 'result': 2048, 'remaining': 2000}
WEB_ROOT = ROOT / 'frontend' / 'dist'
STATIC = {'/': ('index.html', 'text/html; charset=utf-8'), '/index.html': ('index.html', 'text/html; charset=utf-8'), '/assets/app.js': ('assets/app.js', 'text/javascript; charset=utf-8'), '/assets/app.css': ('assets/app.css', 'text/css; charset=utf-8')}


class AppError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


def object_fields(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= value.keys() or value.keys() - set(required) - set(optional):
        raise AppError(400, 'JSON の項目を確認してください')


def text(value, name, maximum, empty=False):
    if not isinstance(value, str) or len(value) > maximum or (not empty and not value.strip()) or '\x00' in value or any(0xD800 <= ord(char) <= 0xDFFF for char in value):
        raise AppError(400, f'{name} の型または文字数が不正です')
    return value


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


def validate_request(payload):
    object_fields(payload, ('title', 'source', 'reason', 'options', 'recommended', 'recommendation'), ('background', 'priority', 'urgency_reason', 'deadline', 'links', 'files', 'image', 'request_key'))
    item = {key: text(payload[key], key, maximum) for key, maximum in [('title', 200), ('source', 200), ('reason', 4000), ('recommendation', 4000)]}
    for key, maximum in [('background', 12000), ('urgency_reason', 2000)]:
        item[key] = text(payload.get(key, ''), key, maximum, empty=True)
    priority = payload.get('priority', 'normal')
    if priority not in ('urgent', 'normal', 'later'):
        raise AppError(400, '優先度が不正です')
    item['priority'] = priority
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
    if not isinstance(options, list) or not 2 <= len(options) <= 8:
        raise AppError(400, '候補は 2〜8 件を指定してください')
    ids = set()
    item['options'] = []
    for option in options:
        object_fields(option, ('id', 'title', 'benefits', 'concerns'))
        identifier = text(option['id'], '候補 ID', 64)
        if not re.fullmatch(r'[A-Za-z0-9_-]+', identifier) or identifier in ids:
            raise AppError(400, '候補 ID が不正または重複しています')
        ids.add(identifier)
        item['options'].append({'id': identifier, 'title': text(option['title'], '候補名', 300), 'benefits': text(option['benefits'], '利点', 2000, empty=True), 'concerns': text(option['concerns'], '懸念', 2000, empty=True)})
    if not isinstance(payload['recommended'], str) or payload['recommended'] not in ids:
        raise AppError(400, '推奨候補が見つかりません')
    item['recommended'] = payload['recommended']
    links = payload.get('links', [])
    if not isinstance(links, list) or len(links) > 20:
        raise AppError(400, '関連リンクは 20 件までです')
    item['links'] = []
    for link in links:
        object_fields(link, ('url', 'label', 'kind'))
        if link['kind'] not in ('pr', 'issue', 'other'):
            raise AppError(400, 'リンクの種別が不正です')
        item['links'].append({'url': url(link['url']), 'label': text(link['label'], 'リンク名', 200), 'kind': link['kind']})
    files = payload.get('files', [])
    if not isinstance(files, list) or len(files) > 20:
        raise AppError(400, 'ファイルは 20 件までです')
    # 中身や存在は読むときに見る。登録時に見ると、下書きを消した後の update や再送が通らなくなる
    if files:
        item['files'] = []
        for entry in files:
            object_fields(entry, ('path', 'label'))
            path = text(entry['path'], 'ファイルのパス', 4096)
            if not path.startswith('/') or '..' in path.split('/'):
                raise AppError(400, 'ファイルは .. を含まない絶対パスで指定してください')
            item['files'].append({'path': path, 'label': text(entry['label'], 'ファイル名', 200)})
    image = payload.get('image')
    if image is not None:
        object_fields(image, ('url', 'alt'))
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
            item[key] = text(payload[key], key, maximum, empty=key != 'title')
    if 'lane' in payload:
        if payload['lane'] not in LANES:
            raise AppError(400, 'lane は ' + ' / '.join(LANES) + ' のいずれかを指定してください')
        item['lane'] = payload['lane']
    if 'decision_id' in payload:
        value = payload['decision_id']
        if value in ('', None):
            item['decision_id'] = None
        else:
            if not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{32}', value):
                raise AppError(400, 'decision_id の形式が不正です')
            item['decision_id'] = value
    return item


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
            # 更新・取り下げは decisions に列を足さず別表に積む。decisions の列や状態の CHECK を変えると
            # 旧 release が開けなくなるため。取り下げ済み = withdraw 行があること。旧 release に戻すと
            # この表は読まれず、取り下げ済みの判断が未回答として再表示される
            connection.execute('''CREATE TABLE IF NOT EXISTS decision_changes (
                id INTEGER PRIMARY KEY, decision_id TEXT NOT NULL, event TEXT NOT NULL,
                recorded_at TEXT NOT NULL, note TEXT NOT NULL, before TEXT)''')
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
                'can_undo': row['status'] == 'pending' and row['worker'] is None and row['token'] is None and row['lease_until'] is None,
            }
        return item

    def stamp(self, connection, identifier, previous=''):
        latest = connection.execute('SELECT MAX(recorded_at) FROM answer_history WHERE decision_id=?', (identifier,)).fetchone()[0]
        previous = max(previous, latest or '')
        current = utc_now()
        if current <= previous:
            current = (datetime.fromisoformat(previous) + timedelta(microseconds=1)).isoformat(timespec='microseconds')
        return current

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
            # 回答の保存と task の移動を同じ transaction に置く。窓口が lane を手で動かし忘れ、
            # 回答済みの判断が human_decision に残っていた (Web と TUI の回答はここを通る)。
            # undo では戻さない: 取り消しの後に窓口が何を待つかは機械で決められない
            connection.execute("UPDATE tasks SET lane='todo',remaining=substr(?||remaining,1,2000),updated_at=? WHERE decision_id=? AND lane='human_decision'",
                               (f'Cue 回答済み: {choice}。', utc_now(), identifier))
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
        object_fields(changes, (), ('title', 'source', 'reason', 'background', 'priority', 'urgency_reason', 'deadline', 'options', 'recommended', 'recommendation', 'links', 'files', 'image'))
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
        object_fields(payload, ('id', 'title'), ('request', 'pane', 'run', 'result', 'remaining', 'lane', 'decision_id'))
        item = validate_task(payload)
        item = {'request': '', 'pane': '', 'run': '', 'result': '', 'remaining': '', 'lane': 'todo', 'decision_id': None, **item}
        with self.db() as connection:
            connection.execute('BEGIN IMMEDIATE')
            self.check_decision(connection, item)
            row = connection.execute('SELECT * FROM tasks WHERE id=?', (item['id'],)).fetchone()
            if row:
                if any(row[key] != item[key] for key in TASK_IDENTITY):
                    raise AppError(409, '同じ id に異なる task が登録されています')
                return dict(row)
            now = utc_now()
            connection.execute('INSERT INTO tasks(id,title,request,pane,run,result,remaining,lane,decision_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                               (item['id'], *(item[key] for key in TASK_FIELDS), now, now))
            return dict(connection.execute('SELECT * FROM tasks WHERE id=?', (item['id'],)).fetchone())

    def update_task(self, identifier, payload):
        object_fields(payload, (), TASK_FIELDS)
        if not payload:
            raise AppError(400, '更新する項目を指定してください')
        item = validate_task(payload)
        with self.db() as connection:
            connection.execute('BEGIN IMMEDIATE')
            if not connection.execute('SELECT 1 FROM tasks WHERE id=?', (identifier,)).fetchone():
                raise AppError(404, 'task が見つかりません')
            self.check_decision(connection, item)
            columns = ''.join(f'{key}=?,' for key in item)
            connection.execute(f'UPDATE tasks SET {columns}updated_at=? WHERE id=?', (*item.values(), utc_now(), identifier))
            return dict(connection.execute('SELECT * FROM tasks WHERE id=?', (identifier,)).fetchone())

    def delete_task(self, identifier):
        with self.db() as connection:
            if not connection.execute('DELETE FROM tasks WHERE id=?', (identifier,)).rowcount:
                raise AppError(404, 'task が見つかりません')
        return {'deleted': identifier}

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

    def claim(self, worker, lease_seconds=300, key_prefix=None):
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
            decision = self.fetch(connection, row['id'])
        return {'decision': decision, 'token': token, 'worker': worker, 'expires_at': datetime.fromtimestamp(expires, timezone.utc).isoformat()}

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
        object_fields(payload, ('decided_at', 'reported_at'))
        decided_at = text(payload['decided_at'], 'decided_at', 64)
        reported_at = text(payload['reported_at'], 'reported_at', 64)
        with self.db() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute('SELECT * FROM decisions WHERE id=?', (identifier,)).fetchone()
            self.public(row, [])
            if row['status'] not in ('blocked', 'failed') or row['decided_at'] != decided_at or row['reported_at'] != reported_at:
                raise AppError(409, '停止理由が変更済み、または再試行できない状態です')
            self.record(connection, row, 'retry')
            connection.execute("UPDATE decisions SET status='pending',reason='',next_step='',reported_at=NULL WHERE id=?", (identifier,))
        return self.get(identifier)

    def renew(self, identifier, payload):
        object_fields(payload, ('token',), ('lease_seconds',))
        seconds = validate_lease(payload.get('lease_seconds', 300))
        with self.db() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute('SELECT * FROM decisions WHERE id=?', (identifier,)).fetchone()
            now = self.active(row, payload['token'])
            expires = max(row['lease_until'], now + seconds)
            connection.execute('UPDATE decisions SET lease_until=? WHERE id=?', (expires, identifier))
            decision = self.fetch(connection, identifier)
        return {'decision': decision, 'expires_at': datetime.fromtimestamp(expires, timezone.utc).isoformat()}

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
    for index, (title, source, priority, reason, background) in enumerate(names):
        item = store.add({'title': title, 'source': source, 'priority': priority, 'reason': reason, 'background': background, 'urgency_reason': '修正方針が決まるまでレビューが停止中' if index == 0 else '', 'options': [{'id': 'minimal', 'title': '影響範囲を限定して進める', 'benefits': '確認範囲を絞れる', 'concerns': '追加作業が残る'}, {'id': 'full', 'title': '今回まとめて対応する', 'benefits': '残作業を減らせる', 'concerns': '公開が遅れる可能性'}], 'recommended': 'minimal', 'recommendation': '現在の作業への影響を限定するため。', 'links': [{'url': 'https://github.com/example/repo/pull/1', 'label': '対象の PR (デモ)', 'kind': 'pr'}, {'url': 'https://github.com/example/repo/issues/2', 'label': '関連 issue (デモ)', 'kind': 'issue'}] if index == 0 else [], 'image': {'url': '/assets/demo-flow.svg', 'alt': '必要な修正を確認し、公開後に共通化を進める流れ'} if index == 0 else None})
        if index == 3:
            store.answer(item['id'], {'choice': 'minimal', 'memo': '既存の連携を優先'})
            store.claim('demo-worker', 300)


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
                if path == '/api/claim':
                    object_fields(payload, ('worker',), ('lease_seconds', 'key_prefix'))
                    if 'key_prefix' in payload and payload['key_prefix'] is None:
                        # null を未指定扱いにすると全件 claim になり、絞り込みが黙って外れる
                        raise AppError(400, 'key_prefix は文字列で指定してください')
                    return self.json_response(200, store.claim(payload['worker'], payload.get('lease_seconds', 300), payload.get('key_prefix')))
                match = re.fullmatch(r'/api/decisions/([a-f0-9]{32})/(answer|complete|undo|report|retry|renew|update|withdraw)', path)
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
                parts = [('依頼', task['request']), ('担当', f"{task['pane']} {task['run']}".strip()), ('結果', task['result']), ('残り', task['remaining']), ('判断', decision_text(task)), ('更新', updated)]
                lines.append('  ' + ' / '.join(f'{name}: {value}' for name, value in parts if value))
    return '\n'.join(lines)


def run_task(store, args):
    if args.task_command == 'add':
        payload = {'id': args.id, 'title': args.title, 'request': args.request, 'pane': args.pane, 'run': args.run, 'result': args.result, 'remaining': args.remaining, 'lane': args.lane, 'decision_id': args.decision}
        return store.add_task({key: value for key, value in payload.items() if value is not None})
    if args.task_command == 'move':
        return store.update_task(args.id, {'lane': args.lane})
    if args.task_command == 'update':
        payload = {'title': args.title, 'request': args.request, 'pane': args.pane, 'run': args.run, 'result': args.result, 'remaining': args.remaining, 'decision_id': args.decision}
        return store.update_task(args.id, {key: value for key, value in payload.items() if value is not None})
    if args.task_command == 'delete':
        return store.delete_task(args.id)
    return task_lanes(store, args.lane) if args.json else None


def task_lanes(store, lane=None):
    tasks = store.tasks(lane)
    return {'lanes': [{'id': key, 'label': label, 'tasks': [task for task in tasks if task['lane'] == key]} for key, label in LANES.items() if lane in (None, key)]}


def main(argv=None):
    parser = argparse.ArgumentParser(description='Local decision inbox')
    parser.add_argument('--db', type=Path, default=default_db())
    commands = parser.add_subparsers(dest='command', required=True)
    serve = commands.add_parser('serve')
    serve.add_argument('--port', type=int, default=4317)
    serve.add_argument('--demo', action='store_true')
    serve.add_argument('--files-root', action='append', help='判断の files を読める場所。繰り返し指定できる (既定は CUE_FILES_ROOT を : で区切ったもの、無ければ FILES_ROOTS)')
    add = commands.add_parser('add')
    add.add_argument('--file', type=Path, required=True)
    listing = commands.add_parser('list')
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
    complete = commands.add_parser('complete')
    complete.add_argument('--id', required=True)
    complete.add_argument('--token', required=True)
    undo = commands.add_parser('undo')
    undo.add_argument('--id', required=True)
    undo.add_argument('--decided-at', required=True)
    report = commands.add_parser('report')
    report.add_argument('--id', required=True)
    report.add_argument('--token', required=True)
    report.add_argument('--status', choices=('blocked', 'failed'), required=True)
    report.add_argument('--reason', required=True)
    report.add_argument('--next-step', required=True)
    retry = commands.add_parser('retry')
    retry.add_argument('--id', required=True)
    retry.add_argument('--decided-at', required=True)
    retry.add_argument('--reported-at', required=True)
    renew = commands.add_parser('renew')
    renew.add_argument('--id', required=True)
    renew.add_argument('--token', required=True)
    renew.add_argument('--lease-seconds', type=int, default=300)
    backup = commands.add_parser('backup')
    backup.add_argument('--output', type=Path, required=True)
    task = commands.add_parser('task', help='依頼の進み具合を管理する')
    task_commands = task.add_subparsers(dest='task_command', required=True)
    for name in ('add', 'move', 'update', 'delete', 'list'):
        sub = task_commands.add_parser(name)
        if name != 'list':
            sub.add_argument('--id', required=True)
        if name == 'add':
            sub.add_argument('--title', required=True)
        if name == 'update':
            sub.add_argument('--title')
        if name in ('add', 'update'):
            for option in ('request', 'pane', 'run', 'result', 'remaining', 'decision'):
                sub.add_argument('--' + option)
        if name in ('add', 'move'):
            sub.add_argument('--lane', required=name == 'move')
        if name == 'list':
            sub.add_argument('--lane')
            sub.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    try:
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
            result = store.add(parse_json(raw)) if args.command == 'add' else store.update(args.id, {'note': args.note, 'changes': parse_json(raw)})
        elif args.command == 'withdraw':
            result = store.withdraw(args.id, {'note': args.note})
        elif args.command == 'list':
            result = {'decisions': store.list(args.include_withdrawn), 'demo': False}
        elif args.command == 'claim':
            result = store.claim(args.worker, args.lease_seconds, args.key_prefix)
        elif args.command == 'undo':
            result = store.undo(args.id, {'decided_at': args.decided_at})
        elif args.command == 'report':
            result = store.report(args.id, {'token': args.token, 'status': args.status, 'reason': args.reason, 'next_step': args.next_step})
        elif args.command == 'retry':
            result = store.retry(args.id, {'decided_at': args.decided_at, 'reported_at': args.reported_at})
        elif args.command == 'renew':
            result = store.renew(args.id, {'token': args.token, 'lease_seconds': args.lease_seconds})
        elif args.command == 'backup':
            result = store.backup(args.output)
        else:
            result = store.complete(args.id, args.token)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (AppError, sqlite3.Error, OSError) as error:
        message = str(error) if isinstance(error, AppError) else 'データの読み書きに失敗しました'
        print(json.dumps({'error': message}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
