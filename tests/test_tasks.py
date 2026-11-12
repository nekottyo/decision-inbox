import json
import sqlite3
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import AppError, Store
from unittest.mock import patch
from cue_tui import Desk, draw, handle_key
from test_app import request_payload
from test_tui import Screen

ROOT = Path(__file__).resolve().parents[1]


class TaskTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / 'tasks.sqlite3'
        self.store = Store(self.db)

    def status(self, call, *args):
        with self.assertRaises(AppError) as caught:
            call(*args)
        return caught.exception.status

    def test_追加した_task_は既定で_todo_になり作成と更新が同時刻になる(self):
        task = self.store.add_task({'id': 't1', 'title': '題'})
        self.assertEqual(self.store.tasks(), [{**task, 'decision': None}])
        self.assertEqual(task['lane'], 'todo')
        self.assertEqual(task['created_at'], task['updated_at'])
        self.assertIsNone(task['decision_id'])

    def test_同じ内容の追加は既存を返し内容違いは409(self):
        first = self.store.add_task({'id': 't1', 'title': '題', 'pane': 'p'})
        self.assertEqual(self.store.add_task({'id': 't1', 'title': '題', 'pane': 'p'}), first)
        self.assertEqual(self.status(self.store.add_task, {'id': 't1', 'title': '別'}), 409)

    def test_進んだ_task_への_add_の再送は既存を返す(self):
        self.store.add_task({'id': 't1', 'title': '題', 'lane': 'todo'})
        moved = self.store.update_task('t1', {'lane': 'worker', 'remaining': '残り'})
        self.assertEqual(self.store.add_task({'id': 't1', 'title': '題', 'lane': 'todo'}), moved)

    def test_更新は指定項目だけ変え更新時刻が進む(self):
        first = self.store.add_task({'id': 't1', 'title': '題', 'result': 'r'})
        updated = self.store.update_task('t1', {'remaining': '残り'})
        self.assertEqual((updated['title'], updated['result'], updated['remaining']), ('題', 'r', '残り'))
        self.assertGreater(updated['updated_at'], first['updated_at'])
        self.assertEqual(updated['created_at'], first['created_at'])
        self.assertEqual(self.status(self.store.update_task, 't1', {}), 400)
        self.assertEqual(self.status(self.store.update_task, 't1', {'id': 'x'}), 400)
        self.assertEqual(self.status(self.store.update_task, 'none', {'title': 'x'}), 404)

    def test_lane_の移動と不正な_lane(self):
        self.store.add_task({'id': 't1', 'title': '題'})
        self.assertEqual(self.store.update_task('t1', {'lane': 'done'})['lane'], 'done')
        self.assertEqual(self.store.tasks('todo'), [])
        self.assertEqual(len(self.store.tasks('done')), 1)
        self.assertEqual(self.status(self.store.update_task, 't1', {'lane': 'x'}), 400)
        self.assertEqual(self.status(self.store.add_task, {'id': 't2', 'title': 'a', 'lane': 'x'}), 400)

    def test_一覧は_lane_順で_lane_内は作成順(self):
        for identifier, lane in [('b', 'done'), ('a', 'worker'), ('c', 'todo'), ('d', 'worker')]:
            self.store.add_task({'id': identifier, 'title': identifier, 'lane': lane})
        self.store.update_task('a', {'title': 'z'})
        self.assertEqual([task['id'] for task in self.store.tasks()], ['c', 'a', 'd', 'b'])

    def test_削除と無い_id_の404(self):
        self.store.add_task({'id': 't1', 'title': '題'})
        self.assertEqual(self.store.delete_task('t1'), {'deleted': 't1'})
        self.assertEqual(self.store.tasks(), [])
        self.assertEqual(self.status(self.store.delete_task, 't1'), 404)

    def test_decision_id_の保存と検査(self):
        decision = self.store.add(request_payload())
        task = self.store.add_task({'id': 't1', 'title': '題', 'decision_id': decision['id']})
        self.assertEqual(task['decision_id'], decision['id'])
        self.assertEqual(self.status(self.store.update_task, 't1', {'decision_id': 'a' * 32}), 404)
        self.assertEqual(self.status(self.store.update_task, 't1', {'decision_id': 'zz'}), 400)
        self.assertIsNone(self.store.update_task('t1', {'decision_id': ''})['decision_id'])

    def test_一覧は判断の題名と回答の有無を添える(self):
        decision = self.store.add(request_payload(title='方針の判断'))
        self.store.add_task({'id': 't1', 'title': '題', 'decision_id': decision['id']})
        self.store.add_task({'id': 't2', 'title': '判断なし'})
        self.assertEqual([task['decision'] for task in self.store.tasks()], [{'title': '方針の判断', 'answered': False}, None])
        self.store.answer(decision['id'], {'choice': 'minimal'})
        self.assertEqual(self.store.tasks()[0]['decision'], {'title': '方針の判断', 'answered': True})

    def test_入力の検査(self):
        self.assertEqual(self.status(self.store.add_task, {'id': 'a b', 'title': 'x'}), 400)
        self.assertEqual(self.status(self.store.add_task, {'id': 'a', 'title': ' '}), 400)
        self.assertEqual(self.status(self.store.add_task, {'id': 'a', 'title': 'x', 'extra': 1}), 400)
        self.assertEqual(self.status(self.store.add_task, {'id': 'a', 'title': 'x' * 201}), 400)

    def test_user_version_と判断は_task_を足しても変わらない(self):
        decision = self.store.add(request_payload())
        self.store.answer(decision['id'], {'choice': 'minimal'})
        self.store.add_task({'id': 't1', 'title': '題'})
        reopened = Store(self.db)
        self.assertEqual(reopened.get(decision['id'])['answer']['choice'], 'minimal')
        with sqlite3.connect(self.db) as connection:
            self.assertEqual(connection.execute('PRAGMA user_version').fetchone()[0], 2)
            columns = [row[1] for row in connection.execute('PRAGMA table_info(decisions)')]
        self.assertEqual(columns, ['id', 'created_at', 'payload', 'choice', 'memo', 'decided_at', 'status', 'worker', 'token', 'lease_until', 'request_key', 'reason', 'next_step', 'reported_at'])

    def test_tasks_表が無い_version_2_の_DB_を開き直すと表ができる(self):
        decision = self.store.add(request_payload())
        with sqlite3.connect(self.db) as connection:
            connection.execute('DROP TABLE tasks')
        reopened = Store(self.db)
        self.assertEqual(reopened.tasks(), [])
        self.assertEqual(reopened.get(decision['id'])['id'], decision['id'])

    def test_backup_に_task_が入る(self):
        self.store.add_task({'id': 't1', 'title': '題'})
        output = Path(self.temp.name) / 'backup.sqlite3'
        self.store.backup(output)
        with sqlite3.connect(output) as connection:
            self.assertEqual(connection.execute('SELECT id FROM tasks').fetchall(), [('t1',)])


class TaskCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / 'cli.sqlite3'

    def cli(self, *arguments):
        return subprocess.run([sys.executable, str(ROOT / 'app.py'), '--db', str(self.db), 'task', *arguments], capture_output=True, text=True, timeout=10)

    def test_task_サブコマンドを通す(self):
        added = self.cli('add', '--id', 't1', '--title', '題', '--pane', 'p1')
        self.assertEqual(added.returncode, 0, added.stderr)
        self.assertEqual(json.loads(added.stdout)['lane'], 'todo')
        self.assertEqual(json.loads(self.cli('move', '--id', 't1', '--lane', 'human_decision').stdout)['lane'], 'human_decision')
        updated = json.loads(self.cli('update', '--id', 't1', '--result', 'ok', '--pane', '').stdout)
        self.assertEqual((updated['result'], updated['pane'], updated['title']), ('ok', '', '題'))
        listed = self.cli('list')
        self.assertEqual(listed.stdout.count('## '), 5)
        self.assertIn('## 人の判断待ち (1)', listed.stdout)
        self.assertIn('- 題  [t1]', listed.stdout)
        data = json.loads(self.cli('list', '--json').stdout)
        self.assertEqual([lane['id'] for lane in data['lanes']], ['todo', 'human_decision', 'worker', 'human_action', 'done'])
        only = json.loads(self.cli('list', '--json', '--lane', 'done').stdout)
        self.assertEqual(len(only['lanes']), 1)
        invalid = self.cli('list', '--json', '--lane', 'bogus')
        self.assertEqual((invalid.returncode, invalid.stdout), (1, ''))
        decision = Store(self.db).add(request_payload(title='方針の判断'))
        self.cli('update', '--id', 't1', '--decision', decision['id'])
        self.assertIn(f"判断: 未回答 方針の判断  [{decision['id']}]", self.cli('list').stdout)
        data = json.loads(self.cli('list', '--json').stdout)
        self.assertEqual(data['lanes'][1]['tasks'][0]['decision'], {'title': '方針の判断', 'answered': False})
        self.assertEqual(json.loads(self.cli('delete', '--id', 't1').stdout), {'deleted': 't1'})
        failed = self.cli('delete', '--id', 't1')
        self.assertEqual(failed.returncode, 1)
        self.assertIn('error', json.loads(failed.stderr))


class TaskTuiTests(unittest.TestCase):
    def test_b_で_task_一覧を開閉できる(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'tui.sqlite3')
            decision = store.add(request_payload(title='方針の判断'))
            store.answer(decision['id'], {'choice': 'minimal'})
            store.add_task({'id': 't1', 'title': '実装中の依頼', 'lane': 'worker', 'pane': 'p1', 'decision_id': decision['id']})
            desk = Desk(store)
            self.assertTrue(handle_key(desk, 'b'))
            self.assertEqual(desk.mode, 'tasks')
            for key in ('j', 'k'):
                self.assertTrue(handle_key(desk, key))
            screen = Screen(24, 100)
            with patch('cue_tui.curses.doupdate'):
                draw(screen, desk)
            output = '\n'.join(screen.output)
            self.assertIn('worker 作業中 (1)', output)
            self.assertIn('実装中の依頼', output)
            self.assertIn('判断 回答済み 方針の判断', output)
            self.assertIn('完了 (0)', output)
            handle_key(desk, 'b')
            self.assertIsNone(desk.mode)

    def test_task_の読み込み失敗は判断の表示と分けて_task_画面に出す(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'tui.sqlite3')
            store.add(request_payload())
            with patch.object(Store, 'tasks', side_effect=sqlite3.OperationalError('locked')):
                desk = Desk(store)
                self.assertEqual(desk.load_error, '')
                handle_key(desk, 'b')
            screen = Screen(24, 100)
            with patch('cue_tui.curses.doupdate'):
                draw(screen, desk)
            self.assertIn('task を読み込めません', '\n'.join(screen.output))


if __name__ == '__main__':
    unittest.main()
