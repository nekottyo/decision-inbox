import contextlib
import io
import json
from pathlib import Path
import select
import signal
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import Store, main, watch_ready
from test_app import request_payload

ROOT = Path(__file__).resolve().parents[1]


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / 'queue.sqlite3'
        self.store = Store(self.db)

    def answered(self):
        item = self.store.add(request_payload())
        return self.store.answer(item['id'], {'choice': 'minimal'})['id']

    def test_ready_excludes_unanswered_completed_and_active_leases_without_writing(self):
        self.store.add(request_payload())
        completed = self.answered()
        with patch('app.time.time', return_value=1000):
            claim = self.store.claim('done')
            self.store.complete(completed, claim['token'])
            self.answered()
            self.store.claim('active', 100)
            expired = self.answered()
            self.store.claim('expired', 5)
        pending = self.answered()
        before = self.db.read_bytes()
        readonly = Store(self.db, readonly=True)
        with patch('app.time.time', return_value=1006):
            self.assertEqual(readonly.ready(), [expired, pending])
        with self.assertRaises(sqlite3.OperationalError):
            readonly.add(request_payload())
        self.assertEqual(self.db.read_bytes(), before)

    def test_watch_emits_only_newly_ready_ids_and_reappearing_ids(self):
        store = SimpleNamespace(ready=Mock(side_effect=[['a'], ['a'], [], ['a', 'b'], ['a', 'b']]))
        clock = [0]
        output = io.StringIO()
        with patch('app.time.monotonic', side_effect=lambda: clock[0]), patch('app.time.sleep', side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)), contextlib.redirect_stdout(output):
            self.assertEqual(watch_ready(store, 1, 4), 0)
        self.assertEqual([json.loads(line) for line in output.getvalue().splitlines()], [
            {'event': 'cue.ready', 'decision_ids': ['a']},
            {'event': 'cue.ready', 'decision_ids': ['a', 'b']},
        ])
        self.assertEqual(clock[0], 4)

    def test_watch_batches_large_ready_sets_into_complete_json_lines(self):
        identifiers = [f'{index:032x}' for index in range(250)]
        store = SimpleNamespace(ready=Mock(return_value=identifiers))
        output = io.StringIO()
        with patch('app.time.monotonic', side_effect=[0, 1]), contextlib.redirect_stdout(output):
            self.assertEqual(watch_ready(store, 2, 1), 0)
        lines = output.getvalue().splitlines()
        events = [json.loads(line) for line in lines]
        self.assertEqual([event['event'] for event in events], ['cue.ready'] * 3)
        self.assertEqual([len(event['decision_ids']) for event in events], [100, 100, 50])
        self.assertEqual([identifier for event in events for identifier in event['decision_ids']], identifiers)
        self.assertTrue(all(len(line.encode()) < 65536 for line in lines))

    def test_cli_flushes_initial_ready_event_and_stops_on_interrupt(self):
        identifier = self.answered()
        before = self.store.list()
        process = subprocess.Popen([sys.executable, str(ROOT / 'app.py'), '--db', str(self.db), 'watch', '--interval', '0.05', '--timeout', '10'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertTrue(select.select([process.stdout], [], [], 3)[0], '通知が flush されること')
            self.assertEqual(json.loads(process.stdout.readline()), {'event': 'cue.ready', 'decision_ids': [identifier]})
            self.assertFalse(select.select([process.stdout], [], [], 0.15)[0], '継続中の同じ回答を再通知しないこと')
            process.send_signal(signal.SIGINT)
            stdout, stderr = process.communicate(timeout=3)
            self.assertEqual((process.returncode, stdout, stderr), (0, '', ''))
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()
        self.assertEqual(self.store.list(), before)

    def test_watch_does_not_create_missing_database(self):
        missing = Path(self.temp.name) / 'missing' / 'queue.sqlite3'
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = main(['--db', str(missing), 'watch', '--timeout', '1e-300'])
        self.assertEqual(result, 1)
        self.assertEqual(stdout.getvalue(), '')
        self.assertEqual(set(json.loads(stderr.getvalue())), {'error'})
        self.assertFalse(missing.parent.exists())

    def test_invalid_watch_seconds_fail_before_database_access(self):
        missing = Path(self.temp.name) / 'missing.sqlite3'
        for option in ('--interval', '--timeout'):
            for value in ('0', '-1', 'nan', 'inf'):
                with self.subTest(option=option, value=value), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
                    main(['--db', str(missing), 'watch', option, value])
                self.assertEqual(caught.exception.code, 2)
        self.assertFalse(missing.exists())


class KeyPrefixTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'queue.sqlite3')

    def answered(self):
        item = self.store.add(request_payload())
        return self.store.answer(item['id'], {'choice': 'minimal'})['id']

    def keyed(self, key):
        item = self.store.add(request_payload(request_key=key))
        return self.store.answer(item['id'], {'choice': 'minimal'})['id']

    def test_claim_and_ready_only_see_matching_prefix(self):
        other = self.keyed('run-b/1')
        self.answered()
        mine = self.keyed('run-a/1')
        self.assertEqual(self.store.ready('run-a/'), [mine])
        self.assertEqual(len(self.store.ready()), 3)
        self.assertEqual(self.store.claim('w', 300, 'run-a/')['decision']['id'], mine)
        self.assertIsNone(self.store.claim('w', 300, 'run-a/')['decision'])
        self.assertEqual(self.store.claim('w')['decision']['id'], other)

    def test_prefix_is_literal_and_validated(self):
        self.keyed('run_a/1')
        with self.assertRaises(Exception):
            self.store.ready('run%')
        self.assertEqual(self.store.ready('run_'), self.store.ready())
        with self.assertRaises(Exception):
            self.store.claim('w', 300, 'run a')
        with self.assertRaises(Exception):
            self.store.claim('w', 300, '')


if __name__ == '__main__':
    unittest.main()
