import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
import unittest.mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import FILE_LIMIT, AppError, Store, files_root, make_server, read_text_file
from cue_tui import Desk, detail_lines, handle_key
from test_app import HTTPTests, request_payload


class FileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.root = base / 'runs'
        self.root.mkdir()
        self.outside = base / 'secret.txt'
        self.outside.write_text('root の外', encoding='utf-8')
        self.draft = self.root / 'run-1' / 'reply.draft.md'
        self.draft.parent.mkdir()
        self.draft.write_text('# 返信の下書き\n<b>そのまま</b>\n', encoding='utf-8')
        self.store = Store(base / 'files.sqlite3')

    def error(self, status, operation):
        with self.assertRaises(AppError) as caught:
            operation()
        self.assertEqual(caught.exception.status, status)
        return str(caught.exception)

    def add(self, *paths):
        return self.store.add(request_payload(files=[{'path': str(path), 'label': f'ファイル {index}'} for index, path in enumerate(paths)]))

    def test_登録時は絶対パスと項目だけを検査し中身や存在は見ない(self):
        for files in ([{'path': 'relative.md', 'label': 'x'}], [{'path': '/a/../b.md', 'label': 'x'}],
                      [{'path': '/a.md'}], [{'path': '/a.md', 'label': 'x', 'kind': 'draft'}],
                      [{'path': '/a.md', 'label': ''}], [{'path': '/a.md', 'label': 'x'}] * 21, {'path': '/a.md'}):
            self.error(400, lambda: self.store.add(request_payload(files=files)))
        item = self.add('/存在しない/下書き.md')
        self.assertEqual(item['files'], [{'path': '/存在しない/下書き.md', 'label': 'ファイル 0'}])

    def test_files_なしの登録と再送は従来の内容のまま扱う(self):
        item = self.store.add(request_payload(request_key='run/1'))
        self.assertEqual(item['files'], [])
        with self.store.db() as connection:
            self.assertNotIn('files', connection.execute('SELECT payload FROM decisions').fetchone()[0])
        self.assertEqual(self.store.add(request_payload(request_key='run/1'))['id'], item['id'])
        self.assertEqual(self.store.add(request_payload(request_key='run/1', files=[]))['id'], item['id'])
        same = self.store.update(item['id'], {'note': '変化なし', 'changes': {'files': []}})
        self.assertEqual(same['revision'], 0)
        updated = self.store.update(item['id'], {'note': '下書きを添付', 'changes': {'files': [{'path': str(self.draft), 'label': '返信'}]}})
        self.assertEqual((updated['revision'], updated['files'][0]['label']), (1, '返信'))

    def test_root_の下のテキストを表示のたびに読み直す(self):
        item = self.add(self.draft)
        shown = self.store.file(item['id'], 0, self.root)
        self.assertEqual(shown, {'path': str(self.draft), 'label': 'ファイル 0', 'content': '# 返信の下書き\n<b>そのまま</b>\n'})
        self.draft.write_text('直した下書き', encoding='utf-8')
        self.assertEqual(self.store.file(item['id'], 0, self.root)['content'], '直した下書き')
        self.error(404, lambda: self.store.file(item['id'], 1, self.root))
        self.error(404, lambda: self.store.file('a' * 32, 0, self.root))

    def test_root_外_symlink_越え_dotdot_は理由付きで断る(self):
        link = self.root / 'link.md'
        link.symlink_to(self.outside)
        sibling = Path(str(self.root) + '-other')
        sibling.mkdir()
        (sibling / 'a.md').write_text('隣', encoding='utf-8')
        for path in (self.outside, link, sibling / 'a.md'):
            self.assertIn('の外にあるファイルは表示しません', self.error(403, lambda: read_text_file(str(path), self.root)))
        # 登録時の検査を通らない .. も、読む側で realpath により root 外と判定する
        self.error(403, lambda: read_text_file(str(self.root / 'run-1' / '..' / '..' / 'secret.txt'), self.root))

    def test_root_を複数持てどれかの下なら読める(self):
        review = Path(self.temp.name) / 'review-repo'
        (review / 'reviews').mkdir(parents=True)
        (review / 'reviews' / 'pr-1.md').write_text('レビュー記録', encoding='utf-8')
        roots = files_root([str(self.root), str(review)])
        self.assertEqual(read_text_file(str(review / 'reviews' / 'pr-1.md'), roots), 'レビュー記録')
        self.assertEqual(read_text_file(str(self.draft), roots), '# 返信の下書き\n<b>そのまま</b>\n')
        self.error(403, lambda: read_text_file(str(self.outside), roots))
        with unittest.mock.patch.dict(os.environ, {'CUE_FILES_ROOT': f'{self.root}{os.pathsep}{review}'}):
            self.assertEqual(files_root(), [self.root, review])
        with unittest.mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(len(files_root()), 2)

    def test_root_自体が_symlink_でも中のファイルは読める(self):
        alias = Path(self.temp.name) / 'alias'
        alias.symlink_to(self.root)
        self.assertEqual(read_text_file(str(self.draft), alias), '# 返信の下書き\n<b>そのまま</b>\n')
        self.assertEqual(read_text_file(str(alias / 'run-1' / 'reply.draft.md'), self.root), '# 返信の下書き\n<b>そのまま</b>\n')

    def test_存在しない_バイナリ_上限超え_通常ファイル以外は断る(self):
        self.assertEqual(self.error(404, lambda: read_text_file(str(self.root / 'none.md'), self.root)), 'ファイルが見つかりません')
        binary = self.root / 'image.png'
        binary.write_bytes(b'\x89PNG\r\n\x1a\n\x00\x00')
        self.assertIn('UTF-8 のテキストではない', self.error(415, lambda: read_text_file(str(binary), self.root)))
        latin = self.root / 'latin.txt'
        latin.write_bytes('caf\xe9'.encode('latin-1'))
        self.error(415, lambda: read_text_file(str(latin), self.root))
        large = self.root / 'large.md'
        large.write_bytes(b'a' * (FILE_LIMIT + 1))
        self.assertIn('KiB を超える', self.error(413, lambda: read_text_file(str(large), self.root)))
        large.write_bytes(b'a' * FILE_LIMIT)
        self.assertEqual(len(read_text_file(str(large), self.root)), FILE_LIMIT)
        self.error(400, lambda: read_text_file(str(self.root / 'run-1'), self.root))
        fifo = self.root / 'pipe'
        os.mkfifo(fifo)
        # 書き手のいない FIFO でも待たずに断る
        self.error(400, lambda: read_text_file(str(fifo), self.root))


class FileHttpTests(unittest.TestCase):
    request = HTTPTests.request

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / 'queue.sqlite3'
        self.files_root = Path(self.temp.name) / 'runs'
        self.files_root.mkdir()
        self.server = make_server(self.db, port=0, root=self.files_root)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]
        self.addCleanup(self.stop)

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=3)
            self.server = None

    def test_http_で登録済みファイルを読み_root_外は理由付きで断る(self):
        draft = self.files_root / 'reply.md'
        draft.write_text('下書き本文', encoding='utf-8')
        outside = Path(self.temp.name) / 'outside.md'
        outside.write_text('外', encoding='utf-8')
        status, item, _ = self.request('POST', '/api/decisions', request_payload(files=[{'path': str(draft), 'label': '返信'}, {'path': str(outside), 'label': '外'}]))
        self.assertEqual(status, 201)
        status, data, headers = self.request('GET', f"/api/decisions/{item['id']}/files/0")
        self.assertEqual((status, data['content'], data['label']), (200, '下書き本文', '返信'))
        self.assertIn('application/json', headers['Content-Type'])
        status, data, _ = self.request('GET', f"/api/decisions/{item['id']}/files/1")
        self.assertEqual(status, 403)
        self.assertIn('の外にあるファイルは表示しません', data['error'])
        self.assertEqual(self.request('GET', f"/api/decisions/{item['id']}/files/2")[0], 404)
        self.assertEqual(self.request('GET', f"/api/decisions/{item['id']}/files/-1")[0], 404)
        self.assertEqual(self.request('POST', f"/api/decisions/{item['id']}/files/0", {})[0], 404)


class FileTuiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'runs'
        self.root.mkdir()
        self.draft = self.root / 'reply.md'
        self.draft.write_text('下書き\x1b[31m本文', encoding='utf-8')
        self.store = Store(Path(self.temp.name) / 'tui.sqlite3')
        self.store.add(request_payload(links=[], files=[{'path': str(self.draft), 'label': '返信'}, {'path': '/etc/hosts', 'label': '外'}]))
        self.desk = Desk(self.store, files_root=self.root)
        self.pages = []

    def test_o_の一覧からファイルを_pager_で開き_root_外は通知で断る(self):
        text = '\n'.join(line for line, _ in detail_lines(self.desk, 120)[0])
        self.assertIn(f'ファイル: 返信: {self.draft}', text)
        handle_key(self.desk, 'o')
        self.assertEqual(self.desk.mode, 'links')
        handle_key(self.desk, '\r', page=self.pages.append)
        self.assertEqual((self.pages, self.desk.mode), (['下書き\x1b[31m本文'], None))
        handle_key(self.desk, 'o')
        handle_key(self.desk, 'j')
        handle_key(self.desk, '\r', page=self.pages.append)
        self.assertEqual(len(self.pages), 1)
        self.assertIn('ファイルを表示できません', self.desk.notice)
        self.assertIn('の外にあるファイルは表示しません', self.desk.notice)


if __name__ == '__main__':
    unittest.main()
