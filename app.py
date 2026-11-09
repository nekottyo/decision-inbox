#!/usr/bin/env python3
"""Local decision inbox, using only the Python standard library."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import sys
import tempfile
import time
from urllib.parse import urlsplit
import uuid

ROOT = Path(__file__).resolve().parent
MAX_BODY = 131072
ASSETS = {'/assets/demo-flow.svg': ('assets/demo-flow.svg', 'image/svg+xml')}
STATIC = {'/': ('index.html', 'text/html; charset=utf-8'), '/index.html': ('index.html', 'text/html; charset=utf-8'), '/app.js': ('app.js', 'text/javascript; charset=utf-8'), '/style.css': ('style.css', 'text/css; charset=utf-8'), **ASSETS}


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
    object_fields(payload, ('title', 'source', 'reason', 'options', 'recommended', 'recommendation'), ('background', 'priority', 'urgency_reason', 'deadline', 'links', 'image'))
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
    image = payload.get('image')
    if image is not None:
        object_fields(image, ('url', 'alt'))
        image = {'url': url(image['url'], image=True), 'alt': text(image['alt'], '画像の説明', 1000)}
    item['image'] = image
    if len(json.dumps(item, ensure_ascii=False).encode()) > MAX_BODY:
        raise AppError(413, '依頼のサイズが上限を超えています')
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


class Store:
    def __init__(self, path):
        self.path = str(path)
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
            connection.execute('''CREATE TABLE IF NOT EXISTS decisions (
                id TEXT PRIMARY KEY, created_at TEXT NOT NULL, payload TEXT NOT NULL,
                choice TEXT, memo TEXT, decided_at TEXT,
                status TEXT CHECK(status IN ('pending','processing','completed')),
                worker TEXT, token TEXT, lease_until REAL,
                CHECK((choice IS NULL AND status IS NULL) OR (choice IS NOT NULL AND status IS NOT NULL)))''')

    @contextmanager
    def db(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def public(self, row):
        if row is None:
            raise AppError(404, '案件が見つかりません')
        item = json.loads(row['payload'])
        item.update(id=row['id'], created_at=row['created_at'], answer=None)
        if row['choice'] is not None:
            item['answer'] = {'choice': row['choice'], 'memo': row['memo'], 'decided_at': row['decided_at'], 'status': row['status']}
        return item

    def add(self, payload):
        item = validate_request(payload)
        identifier = uuid.uuid4().hex
        with self.db() as connection:
            connection.execute('INSERT INTO decisions(id,created_at,payload) VALUES(?,?,?)', (identifier, utc_now(), json.dumps(item, ensure_ascii=False)))
        return self.get(identifier)

    def list(self):
        with self.db() as connection:
            return [self.public(row) for row in connection.execute('SELECT * FROM decisions ORDER BY created_at,id')]

    def get(self, identifier):
        with self.db() as connection:
            return self.public(connection.execute('SELECT * FROM decisions WHERE id=?', (identifier,)).fetchone())

    def answer(self, identifier, payload):
        object_fields(payload, ('choice',), ('memo',))
        choice = text(payload['choice'], '回答', 64)
        memo = text(payload.get('memo', ''), 'メモ', 2000, empty=True)
        with self.db() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute('SELECT * FROM decisions WHERE id=?', (identifier,)).fetchone()
            item = self.public(row)
            if item['answer'] is not None:
                raise AppError(409, '別の画面で判断済みです。入力は保存していません')
            if choice not in {option['id'] for option in item['options']}:
                raise AppError(400, '選択した候補が見つかりません')
            connection.execute("UPDATE decisions SET choice=?,memo=?,decided_at=?,status='pending' WHERE id=? AND choice IS NULL", (choice, memo, utc_now(), identifier))
        return self.get(identifier)

    def claim(self, worker, lease_seconds=300):
        text(worker, 'worker', 200)
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 3600:
            raise AppError(400, 'lease_seconds は 1〜3600 の整数を指定してください')
        token = secrets.token_urlsafe(32)
        with self.db() as connection:
            connection.execute('BEGIN IMMEDIATE')
            now = time.time()
            expires = now + lease_seconds
            row = connection.execute("SELECT * FROM decisions WHERE status='pending' OR (status='processing' AND lease_until<=?) ORDER BY decided_at,id LIMIT 1", (now,)).fetchone()
            if row is None:
                return {'decision': None}
            connection.execute("UPDATE decisions SET status='processing',worker=?,token=?,lease_until=? WHERE id=?", (worker, token, expires, row['id']))
            decision = self.public(connection.execute('SELECT * FROM decisions WHERE id=?', (row['id'],)).fetchone())
        return {'decision': decision, 'token': token, 'worker': worker, 'expires_at': datetime.fromtimestamp(expires, timezone.utc).isoformat()}

    def complete(self, identifier, token):
        text(token, 'token', 200)
        if not token.isascii():
            raise AppError(409, 'claim token が無効です')
        with self.db() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute('SELECT * FROM decisions WHERE id=?', (identifier,)).fetchone()
            self.public(row)
            if row['status'] != 'processing' or not row['token'] or not secrets.compare_digest(row['token'], token) or row['lease_until'] <= time.time():
                raise AppError(409, 'claim token が無効または期限切れです')
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
                match = re.fullmatch(r'/api/decisions/([a-f0-9]{32})', path)
                if match:
                    return self.json_response(200, store.get(match[1]))
                if path not in STATIC:
                    raise AppError(404, '見つかりません')
                filename, content_type = STATIC[path]
                body = (ROOT / filename).read_bytes()
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
                    object_fields(payload, ('worker',), ('lease_seconds',))
                    return self.json_response(200, store.claim(payload['worker'], payload.get('lease_seconds', 300)))
                match = re.fullmatch(r'/api/decisions/([a-f0-9]{32})/(answer|complete)', path)
                if match:
                    if match[2] == 'answer':
                        return self.json_response(200, store.answer(match[1], payload))
                    object_fields(payload, ('token',))
                    return self.json_response(200, store.complete(match[1], payload['token']))
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


def make_server(db_path, port=0, demo=False):
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
    return server


def default_db():
    override = os.environ.get('DECISION_INBOX_DB')
    if override:
        return Path(override).expanduser()
    if sys.platform == 'darwin':
        return Path.home() / 'Library' / 'Application Support' / 'decision-inbox' / 'decisions.sqlite3'
    return Path(os.environ.get('XDG_DATA_HOME', str(Path.home() / '.local' / 'share'))) / 'decision-inbox' / 'decisions.sqlite3'


def main(argv=None):
    parser = argparse.ArgumentParser(description='Local decision inbox')
    parser.add_argument('--db', type=Path, default=default_db())
    commands = parser.add_subparsers(dest='command', required=True)
    serve = commands.add_parser('serve')
    serve.add_argument('--port', type=int, default=4317)
    serve.add_argument('--demo', action='store_true')
    add = commands.add_parser('add')
    add.add_argument('--file', type=Path, required=True)
    commands.add_parser('list')
    claim = commands.add_parser('claim')
    claim.add_argument('--worker', required=True)
    claim.add_argument('--lease-seconds', type=int, default=300)
    complete = commands.add_parser('complete')
    complete.add_argument('--id', required=True)
    complete.add_argument('--token', required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'serve':
            if not 0 <= args.port <= 65535:
                raise AppError(400, 'port は 0〜65535 を指定してください')
            with make_server(args.db, args.port, args.demo) as server:
                print(f'Listening on http://127.0.0.1:{server.server_port}/' + (' (demo)' if args.demo else ''), flush=True)
                try:
                    server.serve_forever()
                except KeyboardInterrupt:
                    pass
            return 0
        store = Store(args.db)
        if args.command == 'add':
            with args.file.open('rb') as handle:
                raw = handle.read(MAX_BODY + 1)
            if len(raw) > MAX_BODY:
                raise AppError(413, '送信サイズが上限を超えています')
            result = store.add(parse_json(raw))
        elif args.command == 'list':
            result = {'decisions': store.list(), 'demo': False}
        elif args.command == 'claim':
            result = store.claim(args.worker, args.lease_seconds)
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
