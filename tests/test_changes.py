from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import SCHEMA, AppError, Store, decision_text
from cue_tui import Desk, detail_lines, handle_key
from test_app import request_payload

ROOT = Path(__file__).resolve().parents[1]


class ChangeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / 'changes.sqlite3'
        self.store = Store(self.db)

    def error(self, status, operation):
        with self.assertRaises(AppError) as caught:
            operation()
        self.assertEqual(caught.exception.status, status)
        return str(caught.exception)

    def test_update_は内容を差し替え_revision_と変更前の内容を残す(self):
        item = self.store.add(request_payload(request_key='run/1'))
        self.assertEqual((item['revision'], item['updated_at'], item['update_note'], item['withdrawn']), (0, None, None, None))
        updated = self.store.update(item['id'], {'note': '期限が決まった', 'changes': {'title': '新しい題名', 'deadline': '2026-12-01T00:00:00+09:00'}})
        self.assertEqual((updated['title'], updated['revision'], updated['update_note']), ('新しい題名', 1, '期限が決まった'))
        self.assertEqual(updated['deadline'], '2026-11-30T15:00:00+00:00')
        self.assertEqual(updated['request_key'], 'run/1')
        self.assertIsNotNone(updated['updated_at'])
        self.assertEqual(self.store.get(item['id']), updated)
        with self.store.db() as connection:
            history = connection.execute('SELECT * FROM decision_changes').fetchall()
        self.assertEqual([row['event'] for row in history], ['update'])
        self.assertEqual(json.loads(history[0]['before'])['title'], request_payload()['title'])
        again = self.store.update(item['id'], {'note': '再送', 'changes': {'title': '新しい題名'}})
        self.assertEqual(again, updated)
        second = self.store.update(item['id'], {'note': '候補を入れ替え', 'changes': {'options': request_payload()['options'][::-1]}})
        self.assertEqual((second['revision'], second['update_note']), (2, '候補を入れ替え'))

    def test_update_の入力検査と状態による拒否(self):
        item = self.store.add(request_payload(request_key='run/1'))
        self.error(400, lambda: self.store.update(item['id'], {'note': '鍵を変える', 'changes': {'request_key': 'run/2'}}))
        self.error(400, lambda: self.store.update(item['id'], {'note': '未知の項目', 'changes': {'extra': 1}}))
        self.error(400, lambda: self.store.update(item['id'], {'note': ' ', 'changes': {'title': 'x'}}))
        self.error(400, lambda: self.store.update(item['id'], {'note': '推奨が消える', 'changes': {'recommended': 'none'}}))
        self.error(404, lambda: self.store.update('a' * 32, {'note': 'n', 'changes': {}}))
        self.assertEqual(self.store.get(item['id'])['revision'], 0)
        self.store.answer(item['id'], {'choice': 'minimal'})
        self.error(409, lambda: self.store.update(item['id'], {'note': '回答後', 'changes': {'title': 'x'}}))
        other = self.store.add(request_payload())
        self.store.withdraw(other['id'], {'note': '不要になった'})
        self.error(409, lambda: self.store.update(other['id'], {'note': '取り下げ後', 'changes': {'title': 'x'}}))

    def test_withdraw_は一覧から外し_get_と_include_withdrawn_では見える(self):
        item = self.store.add(request_payload())
        kept = self.store.add(request_payload(title='残る判断'))
        withdrawn = self.store.withdraw(item['id'], {'note': '別の方法で解決した'})
        self.assertEqual(withdrawn['withdrawn']['note'], '別の方法で解決した')
        self.assertIsNone(withdrawn['answer'])
        self.assertEqual([value['id'] for value in self.store.list()], [kept['id']])
        self.assertEqual([value['id'] for value in self.store.list(include_withdrawn=True)], [item['id'], kept['id']])
        self.assertEqual(self.store.get(item['id']), withdrawn)
        self.assertEqual(self.store.withdraw(item['id'], {'note': '再送'}), withdrawn)
        message = self.error(409, lambda: self.store.answer(item['id'], {'choice': 'minimal'}))
        self.assertIn('取り下げ', message)
        self.assertIsNone(self.store.claim('worker')['decision'])
        self.assertEqual(self.store.ready(), [])

    def test_回答済みの判断は_claim_中も含め取り下げられない(self):
        answered = self.store.add(request_payload())
        self.store.answer(answered['id'], {'choice': 'minimal'})
        self.error(409, lambda: self.store.withdraw(answered['id'], {'note': '取り下げたい'}))
        claim = self.store.claim('worker')
        message = self.error(409, lambda: self.store.withdraw(answered['id'], {'note': '取り下げたい'}))
        self.assertIn('report または complete', message)
        self.store.complete(answered['id'], claim['token'])
        self.error(409, lambda: self.store.withdraw(answered['id'], {'note': '完了後'}))
        with self.store.db() as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM decision_changes').fetchone()[0], 0)

    def test_answer_は_revision_が古いと_409(self):
        item = self.store.add(request_payload())
        self.store.update(item['id'], {'note': '理由を補足', 'changes': {'reason': '補足した理由'}})
        message = self.error(409, lambda: self.store.answer(item['id'], {'choice': 'minimal', 'revision': 0}))
        self.assertIn('更新されています', message)
        self.error(400, lambda: self.store.answer(item['id'], {'choice': 'minimal', 'revision': True}))
        self.error(400, lambda: self.store.answer(item['id'], {'choice': 'minimal', 'revision': '1'}))
        self.assertIsNone(self.store.get(item['id'])['answer'])
        self.assertEqual(self.store.answer(item['id'], {'choice': 'minimal', 'revision': 1})['answer']['choice'], 'minimal')

    def test_add_の再送は取り下げ済みなら_409_更新前の内容なら現行版(self):
        payload = request_payload(request_key='run/1')
        item = self.store.add(payload)
        updated = self.store.update(item['id'], {'note': '題名を直す', 'changes': {'title': '直した題名'}})
        self.assertEqual(self.store.add(payload), updated)
        self.assertEqual(self.store.add({**payload, 'title': '直した題名'}), updated)
        self.error(409, lambda: self.store.add({**payload, 'title': '別の題名'}))
        self.store.withdraw(item['id'], {'note': '不要'})
        message = self.error(409, lambda: self.store.add(payload))
        self.assertIn('新しい request_key', message)
        self.assertEqual(len(self.store.list(include_withdrawn=True)), 1)

    def test_decisions_表だけの旧_DB_を開いても動き_user_version_は_2_のまま(self):
        path = Path(self.temp.name) / 'old.sqlite3'
        with sqlite3.connect(path) as connection:
            connection.execute(SCHEMA)
            connection.execute("INSERT INTO decisions(id,created_at,payload) VALUES(?,?,?)", ('b' * 32, '2026-01-01T00:00:00+00:00', json.dumps({**request_payload(), 'deadline': None, 'image': None})))
            connection.execute('PRAGMA user_version=2')
        store = Store(path)
        self.assertEqual(store.list()[0]['revision'], 0)
        self.assertEqual(store.withdraw('b' * 32, {'note': '旧 DB でも取り下げ'})['withdrawn']['note'], '旧 DB でも取り下げ')
        with sqlite3.connect(path) as connection:
            self.assertEqual(connection.execute('PRAGMA user_version').fetchone()[0], 2)
            columns = [row[1] for row in connection.execute('PRAGMA table_info(decisions)')]
        self.assertEqual(columns, ['id', 'created_at', 'payload', 'choice', 'memo', 'decided_at', 'status', 'worker', 'token', 'lease_until', 'request_key', 'reason', 'next_step', 'reported_at'])

    def test_withdraw_と_answer_の競合で両方は成功しない(self):
        for _ in range(5):
            item = self.store.add(request_payload())
            stores = [Store(self.db), Store(self.db)]
            gate = threading.Barrier(2)

            def call(index, operation):
                gate.wait()
                try:
                    return operation(stores[index])
                except AppError as error:
                    return error.status

            with ThreadPoolExecutor(max_workers=2) as executor:
                first = executor.submit(call, 0, lambda store: store.withdraw(item['id'], {'note': '競合'}))
                second = executor.submit(call, 1, lambda store: store.answer(item['id'], {'choice': 'minimal'}))
                withdrawn, answered = first.result(), second.result()
            current = self.store.get(item['id'])
            if withdrawn == 409:
                self.assertEqual(answered['answer']['choice'], 'minimal')
                self.assertIsNone(current['withdrawn'])
            else:
                self.assertEqual(answered, 409)
                self.assertIsNone(current['answer'])
                self.assertIsNotNone(current['withdrawn'])

    def test_task_の判断表示は取り下げを区別する(self):
        item = self.store.add(request_payload(title='方針の判断'))
        self.store.add_task({'id': 't1', 'title': '依頼', 'decision_id': item['id']})
        self.store.withdraw(item['id'], {'note': '不要'})
        task = self.store.tasks()[0]
        self.assertEqual(task['decision'], {'title': '方針の判断', 'answered': False, 'withdrawn': True})
        self.assertTrue(decision_text(task).startswith('取り下げ 方針の判断'))


class ChangeTuiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'tui.sqlite3')
        self.first = self.store.add(request_payload(title='最初の判断'))
        self.second = self.store.add(request_payload(title='次の判断'))
        self.desk = Desk(self.store)

    def text(self):
        return '\n'.join(line for line, _ in detail_lines(self.desk, 100)[0])

    def test_更新は通知し詳細に常に出し古い候補の下書きを消す(self):
        handle_key(self.desk, '2')
        self.assertEqual(self.desk.draft()['choice'], 'refactor')
        options = [{'id': 'minimal', 'title': '小さく修正', 'benefits': '', 'concerns': ''}, {'id': 'later', 'title': '後で', 'benefits': '', 'concerns': ''}]
        self.store.update(self.first['id'], {'note': '候補を見直した', 'changes': {'options': options}})
        self.desk.refresh()
        self.assertEqual(self.desk.notice, '内容が更新されました: 候補を見直した。最新の内容を表示しています')
        self.assertEqual(self.desk.item['revision'], 1)
        self.assertIsNone(self.desk.draft()['choice'])
        self.desk.notice = ''
        self.desk.refresh()
        self.assertIn('更新あり', self.text())
        self.assertIn('候補を見直した', self.text())

    def test_表示後に更新された判断は古い_revision_で保存できない(self):
        self.store.update(self.first['id'], {'note': '理由を補足', 'changes': {'reason': '補足'}})
        handle_key(self.desk, '\r')
        self.assertIn('更新されています', self.desk.notice)
        self.assertIsNone(self.store.get(self.first['id'])['answer'])
        self.desk.refresh()
        handle_key(self.desk, '\r')
        self.assertEqual(self.store.get(self.first['id'])['answer']['choice'], 'minimal')

    def test_取り下げられたら通知して次の未判断へ移り下書きを捨てる(self):
        self.desk.draft()['memo'] = '捨てる下書き'
        self.assertTrue(self.desk.dirty())
        self.store.withdraw(self.first['id'], {'note': '不要になった'})
        self.desk.refresh()
        self.assertEqual(self.desk.notice, 'この判断は取り下げられました: 不要になった')
        self.assertEqual(self.desk.item['id'], self.second['id'])
        self.assertNotIn(self.first['id'], self.desk.drafts)
        self.assertFalse(self.desk.dirty())

    def test_選択中でない判断の取り下げでも終了確認が残らない(self):
        handle_key(self.desk, 'n')
        handle_key(self.desk, 'p')
        self.store.withdraw(self.second['id'], {'note': '不要'})
        self.desk.refresh()
        self.assertEqual(self.desk.item['id'], self.first['id'])
        self.assertFalse(self.desk.dirty())
        self.assertFalse(handle_key(self.desk, 'q'))


class ChangeCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / 'cli.sqlite3'

    def cli(self, *arguments):
        return subprocess.run([sys.executable, str(ROOT / 'app.py'), '--db', str(self.db), *arguments], capture_output=True, text=True, timeout=10)

    def test_update_withdraw_と_list_include_withdrawn(self):
        item = Store(self.db).add(request_payload())
        changes = Path(self.temp.name) / 'changes.json'
        changes.write_text(json.dumps({'title': 'CLI で直した題名'}), encoding='utf-8')
        updated = self.cli('update', '--id', item['id'], '--note', '題名の修正', '--file', str(changes))
        self.assertEqual(updated.returncode, 0, updated.stderr)
        self.assertEqual((json.loads(updated.stdout)['title'], json.loads(updated.stdout)['revision']), ('CLI で直した題名', 1))
        changes.write_text(json.dumps({'request_key': 'x'}), encoding='utf-8')
        rejected = self.cli('update', '--id', item['id'], '--note', '鍵', '--file', str(changes))
        self.assertEqual(rejected.returncode, 1)
        self.assertEqual(set(json.loads(rejected.stderr)), {'error'})
        withdrawn = self.cli('withdraw', '--id', item['id'], '--note', '不要')
        self.assertEqual(withdrawn.returncode, 0, withdrawn.stderr)
        self.assertEqual(json.loads(withdrawn.stdout)['withdrawn']['note'], '不要')
        self.assertEqual(json.loads(self.cli('list').stdout)['decisions'], [])
        self.assertEqual([value['id'] for value in json.loads(self.cli('list', '--include-withdrawn').stdout)['decisions']], [item['id']])


if __name__ == '__main__':
    unittest.main()
