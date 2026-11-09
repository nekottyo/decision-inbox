import contextlib
from concurrent.futures import ThreadPoolExecutor
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import AppError, LEGACY_COLUMNS, Store, main
from test_app import request_payload


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / 'queue.sqlite3'
        self.store = Store(self.db)

    def answered(self, **changes):
        item = self.store.add(request_payload(**changes))
        return self.store.answer(item['id'], {'choice': 'minimal', 'memo': '回答の原本'})

    def error(self, status, operation):
        with self.assertRaises(AppError) as caught:
            operation()
        self.assertEqual(caught.exception.status, status)

    def report(self, item, status='blocked'):
        claim = self.store.claim('worker')
        return self.store.report(item['id'], {'token': claim['token'], 'status': status, 'reason': '承認が必要', 'next_step': '方針の承認後に再試行'})

    def retry_payload(self, item):
        return {key: item['answer'][key] for key in ('decided_at', 'reported_at')}

    def test_undo_preserves_original_answer_and_rejects_stale_requests(self):
        item = self.answered()
        self.assertTrue(item['answer']['can_undo'])
        self.error(409, lambda: self.store.undo(item['id'], {'decided_at': '古い回答'}))
        undone = self.store.undo(item['id'], {'decided_at': item['answer']['decided_at']})
        self.assertIsNone(undone['answer'])
        self.assertEqual(undone['id'], item['id'])
        with self.store.db() as connection:
            history = connection.execute('SELECT * FROM answer_history').fetchone()
        self.assertEqual(history['event'], 'undo')
        self.assertEqual(json.loads(history['answer']), item['answer'])
        again = self.store.answer(item['id'], {'choice': 'refactor', 'memo': '変更後'})
        self.error(409, lambda: self.store.undo(item['id'], {'decided_at': item['answer']['decided_at']}))
        self.assertEqual(self.store.get(item['id']), again)

    def test_undo_and_claim_race_cannot_both_succeed(self):
        item = self.answered()
        stores = [Store(self.db), Store(self.db)]
        gate = threading.Barrier(2)

        def undo():
            gate.wait()
            try:
                return stores[0].undo(item['id'], {'decided_at': item['answer']['decided_at']})
            except AppError as error:
                return error.status

        def claim():
            gate.wait()
            return stores[1].claim('racing-worker')

        with ThreadPoolExecutor(max_workers=2) as executor:
            first, second = executor.submit(undo), executor.submit(claim)
            undone, claimed = first.result(), second.result()
        if claimed['decision'] is None:
            self.assertIsNone(undone['answer'])
            self.assertIsNone(self.store.get(item['id'])['answer'])
        else:
            self.assertEqual(undone, 409)
            self.assertFalse(claimed['decision']['answer']['can_undo'])
            self.assertEqual(self.store.get(item['id'])['answer']['status'], 'processing')

    def test_claimed_answer_cannot_be_undone_even_after_lease_expires(self):
        item = self.answered()
        with patch('app.time.time', return_value=1000):
            self.store.claim('started', 1)
        with patch('app.time.time', return_value=1002):
            self.error(409, lambda: self.store.undo(item['id'], {'decided_at': item['answer']['decided_at']}))
        self.assertFalse(self.store.get(item['id'])['answer']['can_undo'])

    def test_report_stops_delivery_and_retry_keeps_processing_history(self):
        for status in ('blocked', 'failed'):
            with self.subTest(status=status):
                item = self.answered()
                stopped = self.report(item, status)
                self.assertEqual(stopped['answer']['status'], status)
                self.assertEqual(stopped['answer']['reason'], '承認が必要')
                self.assertEqual(stopped['answer']['next_step'], '方針の承認後に再試行')
                self.assertTrue(stopped['answer']['reported_at'])
                self.assertFalse(stopped['answer']['can_undo'])
                self.assertEqual(self.store.ready(), [])
                self.assertIsNone(self.store.claim('other')['decision'])
                retried = self.store.retry(item['id'], self.retry_payload(stopped))
                self.assertEqual(retried['answer']['status'], 'pending')
                self.assertEqual(retried['answer']['reason'], '')
                self.assertIsNone(retried['answer']['reported_at'])
                self.assertFalse(retried['answer']['can_undo'])
                self.error(409, lambda: self.store.undo(item['id'], {'decided_at': item['answer']['decided_at']}))
                claim = self.store.claim('worker')
                self.store.complete(item['id'], claim['token'])
        with self.store.db() as connection:
            rows = connection.execute('SELECT event,answer FROM answer_history').fetchall()
            worker = connection.execute('SELECT worker FROM decisions LIMIT 1').fetchone()['worker']
        self.assertEqual([row['event'] for row in rows], ['report', 'retry', 'report', 'retry'])
        self.assertEqual(json.loads(rows[0]['answer'])['reason'], '承認が必要')
        self.assertEqual(worker, 'worker')
        self.assertNotIn('token', json.dumps(self.store.list()))
        self.assertNotIn('token', ''.join(row['answer'] for row in rows))

    def test_retry_rejects_a_report_from_an_earlier_attempt(self):
        item = self.answered()
        first = self.report(item)
        self.store.retry(item['id'], self.retry_payload(first))
        second = self.report(item, 'failed')
        self.assertEqual(first['answer']['decided_at'], second['answer']['decided_at'])
        self.assertNotEqual(first['answer']['reported_at'], second['answer']['reported_at'])
        self.error(409, lambda: self.store.retry(item['id'], self.retry_payload(first)))
        self.error(409, lambda: self.store.retry(item['id'], {**self.retry_payload(second), 'decided_at': '古い回答'}))
        self.assertEqual(self.store.get(item['id']), second)

    def test_frozen_clock_cannot_reuse_answer_or_report_expectations(self):
        with patch('app.utc_now', return_value='2026-11-10T00:00:00.000000+00:00'):
            first = self.answered()
            self.store.undo(first['id'], {'decided_at': first['answer']['decided_at']})
            second = self.store.answer(first['id'], {'choice': 'minimal'})
            self.assertNotEqual(first['answer']['decided_at'], second['answer']['decided_at'])
            self.error(409, lambda: self.store.undo(first['id'], {'decided_at': first['answer']['decided_at']}))
            report = self.report(second)
            self.store.retry(first['id'], self.retry_payload(report))
            latest = self.report(second)
            self.assertNotEqual(report['answer']['reported_at'], latest['answer']['reported_at'])
            self.error(409, lambda: self.store.retry(first['id'], self.retry_payload(report)))

    def test_renew_extends_live_claim_without_shortening_or_accepting_expired_tokens(self):
        item = self.answered()
        with patch('app.time.time', return_value=1000):
            claim = self.store.claim('worker', 10)
        with patch('app.time.time', return_value=1005):
            renewed = self.store.renew(item['id'], {'token': claim['token'], 'lease_seconds': 20})
            unchanged = self.store.renew(item['id'], {'token': claim['token'], 'lease_seconds': 1})
        self.assertEqual(renewed['expires_at'], unchanged['expires_at'])
        self.assertNotIn('token', renewed)
        with patch('app.time.time', return_value=1012):
            self.assertIsNone(self.store.claim('other')['decision'])
        with patch('app.time.time', return_value=1026):
            self.error(409, lambda: self.store.renew(item['id'], {'token': claim['token']}))
            second = self.store.claim('other')
            self.error(409, lambda: self.store.renew(item['id'], {'token': claim['token']}))
            self.assertEqual(self.store.complete(item['id'], second['token'])['answer']['status'], 'completed')
            self.error(409, lambda: self.store.renew(item['id'], {'token': second['token']}))

    def test_invalid_reports_and_renewals_preserve_state(self):
        item = self.answered()
        claim = self.store.claim('worker')
        report = {'token': claim['token'], 'status': 'failed', 'reason': '処理失敗', 'next_step': '確認後に再試行'}
        before = self.store.get(item['id'])
        for changes in ({'status': 'completed'}, {'reason': ''}, {'next_step': 'x' * 4001}, {'reason': 1}):
            self.error(400, lambda: self.store.report(item['id'], {**report, **changes}))
        for token in ('wrong', '非 ASCII'):
            self.error(409, lambda: self.store.report(item['id'], {**report, 'token': token}))
        for seconds in (0, True, 3601, '300'):
            self.error(400, lambda: self.store.renew(item['id'], {'token': claim['token'], 'lease_seconds': seconds}))
        with patch('app.time.time', return_value=10**11):
            self.error(409, lambda: self.store.report(item['id'], report))
        self.assertEqual(self.store.get(item['id']), before)

    def test_idempotent_add_is_atomic_and_rejects_conflicting_content(self):
        payload = request_payload(request_key='run-1/msg:1')
        stores = [Store(self.db), Store(self.db)]
        gate = threading.Barrier(2)

        def add(index):
            gate.wait()
            return stores[index].add(payload)

        with ThreadPoolExecutor(max_workers=2) as executor:
            items = list(executor.map(add, (0, 1)))
        self.assertEqual(items[0]['id'], items[1]['id'])
        self.assertEqual(len(self.store.list()), 1)
        self.assertEqual(self.store.add(dict(reversed(list(payload.items())))), items[0])
        self.error(409, lambda: self.store.add({**payload, 'background': '変更された背景'}))
        for key in ('', '空白入り', 'with space', 'x' * 201, None):
            self.error(400, lambda: self.store.add({**payload, 'request_key': key}))
        self.store.add(request_payload())
        self.store.add(request_payload())
        self.assertEqual(len(self.store.list()), 3)

    def legacy_database(self):
        path = Path(self.temp.name) / 'legacy.sqlite3'
        rows = []
        with sqlite3.connect(path) as connection:
            connection.execute('''CREATE TABLE decisions (
                id TEXT PRIMARY KEY, created_at TEXT NOT NULL, payload TEXT NOT NULL,
                choice TEXT,memo TEXT,decided_at TEXT,
                status TEXT CHECK(status IN ('pending','processing','completed')),
                worker TEXT,token TEXT,lease_until REAL,
                CHECK((choice IS NULL AND status IS NULL) OR (choice IS NOT NULL AND status IS NOT NULL)))''')
            for index, status in enumerate((None, 'pending', 'processing', 'completed')):
                row = (str(index), '2026-11-10', json.dumps(request_payload()), 'minimal' if status else None,
                       '保存済みメモ' if status else None, '2026-11-10' if status else None, status,
                       'old-worker' if status in ('processing', 'completed') else None,
                       'old-token' if status == 'processing' else None, 1000 if status == 'processing' else None)
                connection.execute('INSERT INTO decisions VALUES(?,?,?,?,?,?,?,?,?,?)', row)
                rows.append(row)
        return path, rows

    def test_legacy_migration_preserves_all_rows_and_private_online_backup(self):
        path, original = self.legacy_database()
        migrated = Store(path)
        backups = list(path.parent.glob('legacy.sqlite3.pre-v2-*.sqlite3'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)
        with sqlite3.connect(backups[0]) as backup:
            self.assertEqual(backup.execute(f'SELECT {LEGACY_COLUMNS} FROM decisions ORDER BY id').fetchall(), original)
            self.assertEqual(backup.execute('PRAGMA user_version').fetchone()[0], 0)
        with migrated.db() as connection:
            current = [tuple(row) for row in connection.execute(f'SELECT {LEGACY_COLUMNS} FROM decisions ORDER BY id')]
            self.assertEqual(current, original)
            self.assertEqual(connection.execute('PRAGMA user_version').fetchone()[0], 2)
        self.assertTrue(migrated.get('1')['answer']['can_undo'])
        self.assertFalse(migrated.get('2')['answer']['can_undo'])
        Store(path)
        self.assertEqual(list(path.parent.glob('legacy.sqlite3.pre-v2-*.sqlite3')), backups)

    def test_migration_failure_rolls_back_schema_and_original_rows(self):
        path, original = self.legacy_database()
        original_db = Store.db

        @contextlib.contextmanager
        def denied_db(store):
            with original_db(store) as connection:
                connection.set_authorizer(lambda action, name, *_: sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_CREATE_TABLE and name == 'answer_history' else sqlite3.SQLITE_OK)
                yield connection

        with patch.object(Store, 'db', denied_db), self.assertRaises(sqlite3.DatabaseError):
            Store(path)
        with sqlite3.connect(path) as connection:
            self.assertEqual(connection.execute(f'SELECT {LEGACY_COLUMNS} FROM decisions ORDER BY id').fetchall(), original)
            self.assertNotIn('request_key', {row[1] for row in connection.execute('PRAGMA table_info(decisions)')})
            self.assertIsNone(connection.execute("SELECT name FROM sqlite_master WHERE name='decisions_legacy'").fetchone())
        self.assertEqual(len(list(path.parent.glob('legacy.sqlite3.pre-v2-*.sqlite3'))), 1)

    def test_backup_includes_committed_wal_data_and_restores_without_overwriting(self):
        live = sqlite3.connect(self.db)
        self.addCleanup(live.close)
        live.execute('PRAGMA journal_mode=WAL')
        live.execute('PRAGMA wal_autocheckpoint=0')
        item = self.answered()
        backup = Path(self.temp.name) / 'backup.sqlite3'
        result = Store(self.db, readonly=True).backup(backup)
        self.assertEqual(result['backup'], str(backup))
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
        restored = Store(backup)
        self.assertEqual(restored.get(item['id']), item)
        original_bytes = backup.read_bytes()
        self.error(409, lambda: self.store.backup(backup))
        self.assertEqual(backup.read_bytes(), original_bytes)
        self.error(409, lambda: self.store.backup(self.db))
        alias = Path(self.temp.name) / 'alias.sqlite3'
        alias.symlink_to(self.db)
        self.error(409, lambda: self.store.backup(alias))
        dangling = Path(self.temp.name) / 'dangling.sqlite3'
        dangling.symlink_to(Path(self.temp.name) / 'absent.sqlite3')
        self.error(409, lambda: self.store.backup(dangling))
        self.assertFalse(list(Path(self.temp.name).glob('.cue-backup-*')))

    def test_backup_failure_does_not_create_source_or_replace_destination(self):
        missing = Path(self.temp.name) / 'missing.sqlite3'
        destination = Path(self.temp.name) / 'new.sqlite3'
        with self.assertRaises(sqlite3.OperationalError):
            Store(missing, readonly=True).backup(destination)
        self.assertFalse(missing.exists())
        self.assertFalse(destination.exists())
        with patch('app.os.link', side_effect=FileExistsError):
            self.error(409, lambda: self.store.backup(destination))
        self.assertFalse(destination.exists())
        self.assertFalse(list(Path(self.temp.name).glob('.cue-backup-*')))

    def test_cli_recovery_commands_share_the_same_state_and_backup(self):
        def cli(*arguments):
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = main(['--db', str(self.db), *arguments])
            self.assertEqual(code, 0, stderr.getvalue())
            return json.loads(stdout.getvalue())

        item = self.answered()
        self.assertIsNone(cli('undo', '--id', item['id'], '--decided-at', item['answer']['decided_at'])['answer'])
        self.store.answer(item['id'], {'choice': 'minimal'})
        claim = cli('claim', '--worker', 'cli-worker')
        self.assertIn('expires_at', cli('renew', '--id', item['id'], '--token', claim['token'], '--lease-seconds', '3600'))
        stopped = cli('report', '--id', item['id'], '--token', claim['token'], '--status', 'failed', '--reason', '確認が必要', '--next-step', '修正して再試行')
        retried = cli('retry', '--id', item['id'], '--decided-at', stopped['answer']['decided_at'], '--reported-at', stopped['answer']['reported_at'])
        self.assertEqual(retried['answer']['status'], 'pending')
        backup = Path(self.temp.name) / 'cli-backup.sqlite3'
        self.assertEqual(cli('backup', '--output', str(backup)), {'backup': str(backup)})
        self.assertEqual(Store(backup).get(item['id']), retried)


if __name__ == '__main__':
    unittest.main()
