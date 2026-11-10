import copy
import http.client
import json
import sqlite3
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import AppError, Store, make_server

ROOT = Path(__file__).resolve().parents[1]


def request_payload(**changes):
    value = {
        'title': 'レビュー指摘への対応', 'source': 'テスト担当',
        'reason': '公開前に修正範囲の判断が必要', 'background': '明日公開予定',
        'priority': 'normal', 'urgency_reason': 'レビューが停止中',
        'options': [
            {'id': 'minimal', 'title': '小さく修正', 'benefits': '影響が小さい', 'concerns': '重複が残る'},
            {'id': 'refactor', 'title': '共通化する', 'benefits': '重複を減らせる', 'concerns': '公開が遅れる'},
        ],
        'recommended': 'minimal', 'recommendation': '影響を限定するため',
        'links': [{'url': 'https://github.com/example/repo/pull/1', 'label': '関連 PR', 'kind': 'pr'}],
    }
    value.update(changes)
    return value


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / 'queue.sqlite3'
        self.store = Store(self.db)

    def assert_app_error(self, status, operation):
        with self.assertRaises(AppError) as caught:
            operation()
        self.assertEqual(caught.exception.status, status)

    def answered(self, **changes):
        item = self.store.add(request_payload(**changes))
        return self.store.answer(item['id'], {'choice': 'minimal', 'memo': '条件を保存'})

    def test_empty_database_and_persistence(self):
        self.assertEqual(self.store.list(), [])
        item = self.answered()
        reopened = Store(self.db).get(item['id'])
        self.assertEqual(reopened, item)
        self.assertEqual(reopened['answer']['status'], 'pending')
        self.assertEqual(reopened['links'], request_payload()['links'])
        self.assertEqual(reopened['background'], '明日公開予定')
        self.assertEqual(len(self.store.list()), 1)

    def test_duplicate_answer_keeps_original_and_one_queue_entry(self):
        item = self.answered()
        self.assert_app_error(409, lambda: self.store.answer(item['id'], {'choice': 'refactor', 'memo': '上書き禁止'}))
        self.assertEqual(self.store.get(item['id']), item)
        claim = self.store.claim('worker-a')
        self.assertEqual(claim['decision']['id'], item['id'])
        self.assertIsNone(self.store.claim('worker-b')['decision'])

    def test_fifo_uses_answer_order_instead_of_priority(self):
        first = self.answered(priority='later')
        second = self.answered(priority='urgent')
        self.assertEqual(self.store.claim('worker-a')['decision']['id'], first['id'])
        self.assertEqual(self.store.claim('worker-b')['decision']['id'], second['id'])

    def test_concurrent_claim_has_one_owner(self):
        item = self.answered()
        gate = threading.Barrier(2)

        def claim(worker):
            separate = Store(self.db)
            gate.wait()
            return separate.claim(worker)

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(claim, ['worker-a', 'worker-b']))
        winners = [result for result in results if result['decision'] is not None]
        self.assertEqual(len(winners), 1)
        self.assertEqual(winners[0]['decision']['id'], item['id'])
        self.assertEqual(winners[0]['decision']['answer']['status'], 'processing')
        self.assertTrue(winners[0]['token'])
        self.assertIn(winners[0]['worker'], ['worker-a', 'worker-b'])
        self.assertTrue(winners[0]['expires_at'])

    def test_expired_claim_is_redelivered_and_old_tokens_are_rejected(self):
        item = self.answered()
        with patch('app.time.time', return_value=1000):
            first = self.store.claim('worker-a', 10)
        with patch('app.time.time', return_value=1011):
            self.assert_app_error(409, lambda: self.store.complete(item['id'], first['token']))
            second = Store(self.db).claim('worker-b', 30)
            self.assertEqual(second['decision']['id'], item['id'])
            self.assertEqual(second['worker'], 'worker-b')
            self.assertNotEqual(second['token'], first['token'])
            self.assert_app_error(409, lambda: self.store.complete(item['id'], first['token']))
            self.assertEqual(self.store.get(item['id'])['answer']['status'], 'processing')
            self.assertEqual(self.store.complete(item['id'], second['token'])['answer']['status'], 'completed')

    def test_complete_requires_current_token_and_is_not_repeated(self):
        item = self.answered()
        claim = self.store.claim('worker-a')
        self.assert_app_error(409, lambda: self.store.complete(item['id'], 'wrong-token'))
        self.assert_app_error(409, lambda: self.store.complete(item['id'], 'あ'))
        self.assertEqual(self.store.get(item['id'])['answer']['status'], 'processing')
        done = self.store.complete(item['id'], claim['token'])
        self.assertEqual(done['answer']['status'], 'completed')
        self.assert_app_error(409, lambda: self.store.complete(item['id'], claim['token']))
        self.assertIsNone(self.store.claim('worker-b')['decision'])
        self.assertNotIn('token', json.dumps(self.store.list()))

    def test_invalid_requests_never_write(self):
        bad = [
            None, [], {'title': '不足'}, request_payload(title=''),
            request_payload(title='x' * 10001), request_payload(source=5),
            request_payload(priority='highest'), request_payload(extra='unknown'),
            request_payload(options=[]), request_payload(recommended='unknown'),
            request_payload(deadline='2026-11-09T12:00:00'),
            request_payload(deadline='0001-01-01T00:00:00+14:00'),
            request_payload(deadline='9999-12-31T23:59:59-14:00'),
            request_payload(options=[request_payload()['options'][0]] * 2),
            request_payload(links=[{'url': 'javascript:alert(1)', 'label': '危険', 'kind': 'pr'}]),
            request_payload(links=[{'url': '//evil.example/path', 'label': '相対 URL', 'kind': 'pr'}]),
            request_payload(links=[{'url': 'https://example.com', 'label': '', 'kind': 'pr'}]),
            request_payload(image={'url': 'https://example.com/a.png'}),
            request_payload(image={'url': 'data:image/svg+xml,<svg/>', 'alt': '禁止'}),
            request_payload(image={'url': '/assets/../../app.py', 'alt': '禁止'}),
        ]
        for value in bad:
            with self.subTest(value=value):
                self.assert_app_error(400, lambda: self.store.add(copy.deepcopy(value)))
                self.assertEqual(self.store.list(), [])

    def test_invalid_answer_and_claim_keep_state(self):
        item = self.store.add(request_payload())
        for value in [None, [], {'choice': 'wrong'}, {'choice': 'minimal', 'memo': 1}, {'choice': 'minimal', 'memo': 'x' * 2001}, {'choice': 'minimal', 'extra': True}]:
            with self.subTest(value=value):
                self.assert_app_error(400, lambda: self.store.answer(item['id'], value))
                self.assertIsNone(self.store.get(item['id'])['answer'])
        for seconds in [True, 0, 3601, '300']:
            self.assert_app_error(400, lambda: self.store.claim('worker', seconds))
        self.assert_app_error(400, lambda: self.store.claim(''))


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / 'queue.sqlite3'
        self.start()
        self.addCleanup(self.stop)

    def start(self, demo=False):
        self.server = make_server(self.db, port=0, demo=demo)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=3)
            self.server = None

    def request(self, method, path, payload=None, headers=None, raw=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        try:
            body = json.dumps(payload).encode() if raw is None and payload is not None else raw
            actual_headers = {'Content-Type': 'application/json'} if body is not None else {}
            actual_headers.update(headers or {})
            connection.request(method, path, body=body, headers=actual_headers)
            response = connection.getresponse()
            content = response.read()
            data = json.loads(content) if 'application/json' in response.getheader('Content-Type', '') else content
            return response.status, data, dict(response.getheaders())
        finally:
            connection.close()

    def test_http_claim_key_prefix_filters_and_rejects_null(self):
        for key in ('run-a/1', 'run-b/1'):
            _, item, _ = self.request('POST', '/api/decisions', request_payload(request_key=key))
            self.request('POST', f"/api/decisions/{item['id']}/answer", {'choice': 'minimal'})
        for bad in (None, '', 'run a', 1):
            self.assertEqual(self.request('POST', '/api/claim', {'worker': 'w', 'key_prefix': bad})[0], 400)
        status, miss, _ = self.request('POST', '/api/claim', {'worker': 'w', 'key_prefix': 'run-c/'})
        self.assertEqual((status, miss), (200, {'decision': None}))
        status, hit, _ = self.request('POST', '/api/claim', {'worker': 'w', 'key_prefix': 'run-b/'})
        self.assertEqual(hit['decision']['request_key'], 'run-b/1')

    def test_http_restart_preserves_answer_and_consumer_round_trip(self):
        status, item, _ = self.request('POST', '/api/decisions', request_payload())
        self.assertEqual(status, 201)
        path = '/api/decisions/' + item['id']
        status, answer, _ = self.request('POST', path + '/answer', {'choice': 'minimal', 'memo': '再起動しても保持'})
        self.assertEqual(status, 200)
        self.stop()
        self.start()
        self.assertEqual(self.request('GET', path)[1], answer)
        status, claim, _ = self.request('POST', '/api/claim', {'worker': 'http-worker'})
        self.assertEqual(status, 200)
        self.assertEqual(claim['decision']['background'], '明日公開予定')
        self.assertEqual(claim['decision']['links'], item['links'])
        status, done, _ = self.request('POST', path + '/complete', {'token': claim['token']})
        self.assertEqual(status, 200)
        self.assertEqual(done['answer']['status'], 'completed')

    def test_database_failure_does_not_claim_a_success_or_erase_data(self):
        item = Store(self.db).add(request_payload())
        with patch.object(Store, 'answer', side_effect=sqlite3.OperationalError('検証用の書込失敗')):
            status, body, _ = self.request('POST', f"/api/decisions/{item['id']}/answer", {'choice': 'minimal'})
        self.assertEqual(status, 500)
        self.assertEqual(set(body), {'error'})
        self.assertEqual(Store(self.db).get(item['id']), item)

    def test_request_boundary_rejections_leave_database_empty(self):
        attempts = [
            ({'Host': 'evil.example'}, request_payload(), None, 403),
            ({'Origin': 'https://evil.example'}, request_payload(), None, 403),
            ({'Origin': 'null'}, request_payload(), None, 403),
            ({'Origin': f'http://127.0.0.1:{self.port + 1}'}, request_payload(), None, 403),
            ({'Sec-Fetch-Site': 'cross-site'}, request_payload(), None, 403),
            ({'Content-Type': 'text/plain'}, request_payload(), None, 415),
            ({}, request_payload(deadline='0001-01-01T00:00:00+14:00'), None, 400),
            ({}, request_payload(deadline='9999-12-31T23:59:59-14:00'), None, 400),
            ({}, None, b'{broken', 400),
            ({'Content-Length': str(1024 * 1024 + 1)}, None, b'{}', 413),
            ({'Content-Length': '9' * 5000}, None, b'{}', 400),
        ]
        for headers, payload, raw, expected in attempts:
            with self.subTest(headers=headers, expected=expected):
                self.assertEqual(self.request('POST', '/api/decisions', payload, headers, raw)[0], expected)
                self.assertEqual(Store(self.db).list(), [])
        origin = {'Origin': f'http://127.0.0.1:{self.port}'}
        self.assertEqual(self.request('POST', '/api/decisions', request_payload(), origin)[0], 201)

    def test_static_allowlist_and_no_cors(self):
        for path in ['/.git/config', '/app.py', '/README.md', '/queue.sqlite3', '/../../app.py', '/%2e%2e/app.py', '/assets/%2e%2e/app.py']:
            with self.subTest(path=path):
                self.assertEqual(self.request('GET', path)[0], 404)
        web_root = Path(self.temp.name) / 'ui'
        (web_root / 'assets').mkdir(parents=True)
        (web_root / 'index.html').write_text('<title>Cue</title>')
        (web_root / 'assets/app.js').write_text('console.log("test")')
        (web_root / 'assets/app.css').write_text('body {}')
        with patch('app.WEB_ROOT', web_root):
            for path in ['/', '/index.html', '/assets/app.css', '/assets/app.js', '/assets/demo-flow.svg']:
                status, _, headers = self.request('GET', path)
                self.assertEqual(status, 200)
                self.assertNotIn('Access-Control-Allow-Origin', headers)
            for path in ['/app.js', '/style.css', '/assets/unknown.js', '/frontend/package.json']:
                self.assertEqual(self.request('GET', path)[0], 404)
        self.assertEqual(self.request('GET', '/api/decisions', headers={'Host': 'evil.example'})[0], 403)

    def test_unbuilt_ui_has_setup_error_but_api_stays_available(self):
        with patch('app.WEB_ROOT', Path(self.temp.name) / 'missing-ui'):
            status, data, _ = self.request('GET', '/')
            self.assertEqual(status, 503)
            self.assertIn('npm ci && npm run build', data['error'])
            self.assertEqual(self.request('GET', '/api/decisions')[0], 200)

    def test_demo_does_not_modify_real_database(self):
        self.stop()
        item = Store(self.db).add(request_payload(title='実データ'))
        self.start(demo=True)
        status, data, _ = self.request('GET', '/api/decisions')
        self.assertEqual(status, 200)
        self.assertTrue(data['demo'])
        self.assertGreater(len(data['decisions']), 0)
        self.assertNotIn(item['id'], [value['id'] for value in data['decisions']])
        self.assertEqual(Store(self.db).list(), [item])

    def test_http_recovery_and_idempotent_registration(self):
        payload = request_payload(request_key='http-recovery/1')
        status, item, _ = self.request('POST', '/api/decisions', payload)
        self.assertEqual(status, 201)
        self.assertEqual(self.request('POST', '/api/decisions', payload)[1]['id'], item['id'])
        self.assertEqual(self.request('POST', '/api/decisions', {**payload, 'title': '異なる依頼'})[0], 409)
        path = '/api/decisions/' + item['id']
        _, answered, _ = self.request('POST', path + '/answer', {'choice': 'minimal'})
        self.assertTrue(answered['answer']['can_undo'])
        self.assertEqual(self.request('POST', path + '/undo', {})[0], 400)
        status, undone, _ = self.request('POST', path + '/undo', {'decided_at': answered['answer']['decided_at']})
        self.assertEqual(status, 200)
        self.assertIsNone(undone['answer'])
        self.request('POST', path + '/answer', {'choice': 'refactor'})
        _, claim, _ = self.request('POST', '/api/claim', {'worker': 'http-recovery'})
        self.assertEqual(self.request('POST', path + '/renew', {'token': 'old-token'})[0], 409)
        status, renewed, _ = self.request('POST', path + '/renew', {'token': claim['token'], 'lease_seconds': 3600})
        self.assertEqual(status, 200)
        self.assertIn('expires_at', renewed)
        status, stopped, _ = self.request('POST', path + '/report', {'token': claim['token'], 'status': 'blocked', 'reason': '承認が必要', 'next_step': '承認後に再試行'})
        self.assertEqual(status, 200)
        self.assertFalse(stopped['answer']['can_undo'])
        self.assertEqual(stopped['answer']['reason'], '承認が必要')
        self.assertIsNone(self.request('POST', '/api/claim', {'worker': 'other'})[1]['decision'])
        self.assertNotIn('token', json.dumps(self.request('GET', '/api/decisions')[1]))
        retry = {key: stopped['answer'][key] for key in ('decided_at', 'reported_at')}
        self.assertEqual(self.request('POST', path + '/retry', {**retry, 'reported_at': '古い報告'})[0], 409)
        self.assertEqual(self.request('POST', path + '/retry', retry)[1]['answer']['status'], 'pending')
        _, new_claim, _ = self.request('POST', '/api/claim', {'worker': 'http-recovery'})
        self.assertEqual(self.request('POST', path + '/complete', {'token': claim['token']})[0], 409)
        self.assertEqual(self.request('POST', path + '/complete', {'token': new_claim['token']})[0], 200)
        self.assertEqual(self.request('POST', path + '/retry', retry)[0], 409)
        self.assertEqual(self.request('POST', path + '/undo', {'decided_at': stopped['answer']['decided_at']})[0], 409)

    def test_cli_uses_same_database(self):
        fixture = Path(self.temp.name) / 'request.json'
        fixture.write_text(json.dumps(request_payload()), encoding='utf-8')

        def cli(*arguments):
            return subprocess.run([sys.executable, str(ROOT / 'app.py'), '--db', str(self.db), *arguments], capture_output=True, text=True, timeout=10)

        added = cli('add', '--file', str(fixture))
        self.assertEqual(added.returncode, 0, added.stderr)
        item = json.loads(added.stdout)
        self.assertEqual(self.request('POST', f"/api/decisions/{item['id']}/answer", {'choice': 'minimal'})[0], 200)
        listed = cli('list')
        self.assertEqual(listed.returncode, 0, listed.stderr)
        claim = cli('claim', '--worker', 'cli-worker')
        self.assertEqual(claim.returncode, 0, claim.stderr)
        token = json.loads(claim.stdout)['token']
        before = Store(self.db).get(item['id'])
        status, body, _ = self.request('POST', f"/api/decisions/{item['id']}/complete", {'token': 'あ'})
        self.assertEqual(status, 409)
        self.assertEqual(set(body), {'error'})
        invalid_token = cli('complete', '--id', item['id'], '--token', 'あ')
        self.assertNotEqual(invalid_token.returncode, 0)
        self.assertNotIn('Traceback', invalid_token.stderr)
        self.assertEqual(set(json.loads(invalid_token.stderr)), {'error'})
        self.assertEqual(Store(self.db).get(item['id']), before)
        completed = cli('complete', '--id', item['id'], '--token', token)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)['answer']['status'], 'completed')
        self.assertNotEqual(cli('complete', '--id', item['id'], '--token', 'wrong').returncode, 0)
        self.assertEqual(len(Store(self.db).list()), 1)
        for deadline in ['0001-01-01T00:00:00+14:00', '9999-12-31T23:59:59-14:00']:
            fixture.write_text(json.dumps(request_payload(deadline=deadline)), encoding='utf-8')
            invalid_date = cli('add', '--file', str(fixture))
            self.assertNotEqual(invalid_date.returncode, 0)
            self.assertNotIn('Traceback', invalid_date.stderr)
            self.assertEqual(set(json.loads(invalid_date.stderr)), {'error'})
            self.assertEqual(len(Store(self.db).list()), 1)


if __name__ == '__main__':
    unittest.main()
