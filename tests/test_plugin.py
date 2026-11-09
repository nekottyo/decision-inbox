import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from app import AppError, main, tui_web_url

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('cue_plugin', ROOT / 'herdr-plugin' / 'cue_plugin.py')
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)


class TUICommandTests(unittest.TestCase):
    def test_help_and_nonterminal_do_not_create_database(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / 'unused.sqlite3'
            result = subprocess.run([sys.executable, str(ROOT / 'app.py'), '--db', str(database), 'tui', '--help'], capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('--web-url', result.stdout)
            self.assertFalse(database.exists())
            result = subprocess.run([sys.executable, str(ROOT / 'app.py'), '--db', str(database), 'tui'], capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 1)
            self.assertIn('対話可能な端末', json.loads(result.stderr)['error'])
            self.assertFalse(database.exists())

    def test_tui_receives_shared_store_without_loading_herdr(self):
        calls = []
        module = types.SimpleNamespace(run=lambda store, web_url=None: calls.append((store.path, store.list(), web_url)) or 0)
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / 'tui.sqlite3'
            with patch.dict(sys.modules, {'cue_tui': module}), patch('sys.stdin.isatty', return_value=True), patch('sys.stdout.isatty', return_value=True):
                self.assertEqual(main(['--db', str(database), 'tui', '--web-url', 'http://127.0.0.1:4317/']), 0)
            self.assertEqual(calls, [(str(database), [], 'http://127.0.0.1:4317/')])

    def test_web_url_stays_on_loopback(self):
        for value in [None, 'http://localhost:4317/', 'http://127.0.0.1:4317/', 'http://[::1]:4317/']:
            self.assertEqual(tui_web_url(value), value)
        for value in ['https://example.com/', 'file:///tmp/data', 'javascript:alert(1)', 'http://user@localhost/', 'http://localhost.evil/', 'http://localhost/\x1b', 12]:
            with self.subTest(value=value), self.assertRaises(AppError):
                tui_web_url(value)


class PluginTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = Path(self.temp.name) / 'config.json'
        self.environment = patch.dict(os.environ, {'HERDR_PLUGIN_CONFIG_DIR': self.temp.name, 'HERDR_BIN_PATH': '/tmp/herdr with spaces'})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_open_uses_argv_and_propagates_failure(self):
        self.config.write_text(json.dumps({'web_url': 'http://127.0.0.1:56366/'}))
        with patch.object(plugin.subprocess, 'run', return_value=types.SimpleNamespace(returncode=7)) as run:
            self.assertEqual(plugin.main(['open']), 7)
            run.assert_called_once_with(['/tmp/herdr with spaces', 'plugin', 'pane', 'open', '--plugin', 'nekottyo.cue', '--entrypoint', 'desk'], check=False)

    def test_tui_executes_same_app_and_inherits_db_environment(self):
        self.config.write_text(json.dumps({'web_url': 'http://127.0.0.1:56366/'}))
        with patch.object(plugin.os, 'execv') as execute:
            plugin.main(['tui'])
        execute.assert_called_once_with(sys.executable, [sys.executable, str(ROOT / 'app.py'), 'tui', '--web-url', 'http://127.0.0.1:56366/'])

    def test_missing_config_has_no_port_guess(self):
        self.assertEqual(plugin.configuration(), {})
        with patch.object(plugin.os, 'execv') as execute:
            plugin.main(['tui'])
        execute.assert_called_once_with(sys.executable, [sys.executable, str(ROOT / 'app.py'), 'tui'])

    def test_invalid_configuration_never_starts_command(self):
        values = ['{broken', '{"web_url":"http://localhost/","web_url":"http://localhost/"}', '{"unknown":1}', '{"web_url":"https://example.com/"}', 'x' * 4097]
        for value in values:
            with self.subTest(value=value[:50]):
                self.config.write_text(value)
                with patch.object(plugin.subprocess, 'run') as run, patch.object(plugin.os, 'execv') as execute, contextlib.redirect_stderr(io.StringIO()) as errors:
                    self.assertEqual(plugin.main(['open']), 1)
                    self.assertIn('error', json.loads(errors.getvalue()))
                    run.assert_not_called()
                    execute.assert_not_called()

    def test_configuration_read_failure_is_reported(self):
        self.config.mkdir()
        with contextlib.redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(plugin.main(['open']), 1)
        self.assertIn('error', json.loads(errors.getvalue()))


if __name__ == '__main__':
    unittest.main()
