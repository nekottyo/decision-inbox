import contextlib
from datetime import datetime, timezone
import fcntl
import io
import json
import os
from pathlib import Path
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import (
    CHECK_FAULT_SKILL,
    Store,
    _run_subprocess_bounded,
    _terminate_process_group,
    check_due_info,
    check_grace_seconds,
    check_list_data,
    check_lock_path,
    check_status_data,
    commit_check_run,
    parse_check_jsonl,
    prune_check_history,
    run_check_command,
    run_check_tick,
    run_wake_pass,
)
from test_app import isolated_env

ROOT = Path(__file__).resolve().parents[1]


def _git(repo, *args):
    result = subprocess.run(
        ['git', '-C', str(repo), *args],
        capture_output=True, text=True, check=True, timeout=10,
    )
    return result.stdout.strip()


def _write_exec(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding='utf-8')
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


CHECK_RUNNER_ENV_KEYS = (
    'CUE_CHECK_NOW',
    'CUE_CHECKS_CONFIG',
    'CUE_RUN_INBOX_BIN',
    'CUE_WAKE_RUN_ID',
    'CUE_CHECK_TICK_BUDGET_SEC',
    'CUE_CHECK_WAKE_BUDGET_SEC',
    'CUE_CHECK_WAKE_ITEM_TIMEOUT_SEC',
    'CUE_CHECK_KILL_GRACE_SEC',
    'FAKE_WAKE_SLEEP',
    'FAKE_ACTIVE_RC',
    'FAKE_SEND_RC',
    'MODE',
)


def _pid_is_zombie(pid):
    proc_stat = Path(f'/proc/{pid}/stat')
    if proc_stat.exists():
        try:
            content = proc_stat.read_text(encoding='utf-8', errors='replace')
            rparen = content.rfind(')')
            if rparen != -1:
                fields = content[rparen + 2:].split()
                if fields and fields[0] in ('Z', 'X', 'x'):
                    return True
            return False
        except OSError:
            return True
    try:
        out = subprocess.run(
            ['ps', '-o', 'stat=', '-p', str(pid)],
            capture_output=True, text=True, timeout=2,
        )
        state = out.stdout.strip()
        return not state or state.startswith('Z')
    except (OSError, subprocess.SubprocessError):
        return False


def _pid_stopped(pid):
    """プロセスが存在しないか、終了済み zombie (未 reap) なら True を返す。"""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    except OSError:
        return True
    return _pid_is_zombie(pid)


def _wait_pid_stopped(pid, timeout=2.0):
    deadline = time.monotonic() + max(0.0, timeout)
    while not _pid_stopped(pid):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)
    return True


def _pid_alive(pid):
    return not _pid_stopped(pid)


def _cleanup_pid_file(pid_file):
    if not pid_file.exists():
        return
    with contextlib.suppress(OSError, ValueError):
        pid = int(pid_file.read_text(encoding='utf-8').strip())
        if pid > 0 and _pid_alive(pid):
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                os.kill(pid, signal.SIGKILL)


def _cleanup_pgid_file(pgid_file):
    if not pgid_file.exists():
        return
    with contextlib.suppress(OSError, ValueError):
        pgid = int(pgid_file.read_text(encoding='utf-8').strip())
        if pgid > 0:
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                os.killpg(pgid, signal.SIGKILL)
            _wait_pid_stopped(pgid, timeout=2.0)


class FakeClock:
    def __init__(self, wall=1_700_000_000.0, mono=1000.0):
        self.wall = float(wall)
        self.mono = float(mono)

    def now(self):
        return self.wall

    def monotonic(self):
        return self.mono

    def advance(self, seconds):
        self.wall += float(seconds)
        self.mono += float(seconds)


class CheckRunnerTests(unittest.TestCase):
    def setUp(self):
        # 呼び出し側 shell の check-runner 系環境変数を隔離する (直接呼び出し・CLI 経由の両方)
        self._env_patch = mock.patch.dict(os.environ, {}, clear=False)
        self._env_patch.start()
        self.addCleanup(self._env_patch.stop)
        for key in CHECK_RUNNER_ENV_KEYS:
            os.environ.pop(key, None)

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / 'cue.sqlite3'
        self.repo = self.root / 'agents-main'
        self.repo.mkdir()
        _git(self.repo, 'init', '-b', 'main')
        _git(self.repo, 'config', 'user.email', 'test@example.com')
        _git(self.repo, 'config', 'user.name', 'Test User')
        self.config = self.repo / 'checks.toml'
        self.wake_log = self.root / 'wake.jsonl'
        self.run_inbox = self.root / 'fake-run-inbox.sh'
        self._install_fake_run_inbox()

    def _cli_env(self, env_extra=None):
        env = isolated_env(self.root)
        for key in CHECK_RUNNER_ENV_KEYS:
            env.pop(key, None)
        env['CUE_RUN_INBOX_BIN'] = str(self.run_inbox)
        if env_extra:
            env.update(env_extra)
        return env

    def _install_fake_run_inbox(self, *, active_rc=0, send_rc=0, sleep_sec=0.0):
        script = f"""#!/usr/bin/env python3
import json, os, sys, time
log_path = {str(self.wake_log)!r}
cmd = sys.argv[1] if len(sys.argv) > 1 else ''
sleep_sec = float(os.environ.get('FAKE_WAKE_SLEEP', {str(sleep_sec)!r}))
if sleep_sec > 0:
    time.sleep(sleep_sec)
if cmd == 'active':
    rc = int(os.environ.get('FAKE_ACTIVE_RC', {str(active_rc)!r}))
    if rc != 0:
        sys.stderr.write('no active run\\n')
        sys.exit(rc)
    print('run-test-1')
    sys.exit(0)
if cmd == 'send':
    rc = int(os.environ.get('FAKE_SEND_RC', {str(send_rc)!r}))
    with open(log_path, 'a', encoding='utf-8') as f:
        f.write(json.dumps({{'argv': sys.argv[1:], 'rc': rc}}, ensure_ascii=False) + '\\n')
    if rc != 0:
        sys.stderr.write('send failed\\n')
    sys.exit(rc)
sys.exit(2)
"""
        _write_exec(self.run_inbox, script)

    def commit_config(self, toml_text, scripts=None):
        if scripts:
            for rel_path, body in scripts.items():
                _write_exec(self.repo / rel_path, body)
        self.config.write_text(toml_text, encoding='utf-8')
        _git(self.repo, 'add', '.')
        _git(self.repo, 'commit', '-m', 'update checks')

    def cli(self, *args, env_extra=None, timeout=15):
        env = self._cli_env(env_extra)
        return subprocess.run(
            [sys.executable, str(ROOT / 'app.py'), '--db', str(self.db), *args],
            capture_output=True, text=True, timeout=timeout, env=env,
        )

    def query_all(self, sql, params=()):
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute(sql, params)]

    def query_one(self, sql, params=()):
        rows = self.query_all(sql, params)
        return rows[0] if rows else None

    # --- 1. Schema & user_version=2 & readonly list/status ---

    def test_additive_tables_keep_user_version_2_and_readonly_commands_do_not_create_db(self):
        # DB が無い状態での check list / check status は DB を作らず正常終了
        res_list = self.cli('check', 'list', '--json')
        self.assertEqual(res_list.returncode, 0, res_list.stderr)
        self.assertEqual(json.loads(res_list.stdout), {'checks': []})
        res_status = self.cli('check', 'status', '--json')
        self.assertEqual(res_status.returncode, 0, res_status.stderr)
        self.assertIsNone(json.loads(res_status.stdout)['last_ok_tick_at'])
        self.assertFalse(self.db.exists())

        self.commit_config(
            '[[checks]]\nname = "c1"\ninterval = 300\ncommand = "scripts/ok.sh"\ntimeout = 30\nskill = "s1"\n',
            {'scripts/ok.sh': '#!/bin/sh\nexit 0\n'},
        )
        res = self.cli('check', 'tick', '--config', str(self.config), env_extra={'CUE_CHECK_NOW': '1700000000'})
        self.assertEqual(res.returncode, 0, res.stderr)
        body = json.loads(res.stdout)
        self.assertEqual(body['status'], 'ok')

        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 2)
            self.assertEqual(conn.execute('PRAGMA journal_mode').fetchone()[0].lower(), 'wal')
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for expected in ('tasks', 'checks', 'check_runs', 'check_run_events', 'check_ticks', 'check_tasks'):
                self.assertIn(expected, tables)
            # 既存の tasks テーブルの列は変更していない
            task_cols = {r[1] for r in conn.execute('PRAGMA table_info(tasks)')}
            self.assertEqual(
                task_cols,
                {'id', 'title', 'request', 'pane', 'run', 'result', 'remaining', 'lane', 'decision_id',
                 'created_at', 'updated_at', 'machine', 'kind', 'done_when'},
            )

    # --- 2. 同時 tick の skip (flock) と lockfile の inode 維持 ---

    def test_concurrent_tick_is_skipped_without_unlinking_lockfile(self):
        self.commit_config(
            '[[checks]]\nname = "c1"\ninterval = 300\ncommand = "scripts/ok.sh"\ntimeout = 30\nskill = "s1"\n',
            {'scripts/ok.sh': '#!/bin/sh\nexit 0\n'},
        )
        # 初回 tick でテーブルを初期化
        first = self.cli('check', 'tick', '--config', str(self.config), env_extra={'CUE_CHECK_NOW': '1700000000'})
        self.assertEqual(first.returncode, 0, first.stderr)
        lock_file = check_lock_path(self.db)
        self.assertTrue(lock_file.exists())
        inode_before = lock_file.stat().st_ino

        # 別 fd で flock を保持したまま 2 回目の tick を実行 -> skipped になる
        fd = os.open(lock_file, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            second = self.cli('check', 'tick', '--config', str(self.config), env_extra={'CUE_CHECK_NOW': '1700000500'})
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(json.loads(second.stdout)['status'], 'skipped')
        finally:
            os.close(fd)

        # lockfile は unlink されず同じ inode が維持される
        self.assertTrue(lock_file.exists())
        self.assertEqual(lock_file.stat().st_ino, inode_before)
        ticks = self.query_all('SELECT status, checks_deferred FROM check_ticks ORDER BY id')
        self.assertEqual([t['status'] for t in ticks], ['ok', 'skipped'])
        # skip は check の状態を変えない
        runs = self.query_all('SELECT * FROM check_runs')
        self.assertEqual(len(runs), 1)

    def test_skip_when_db_not_initialized_does_not_create_db_or_block(self):
        lock_file = check_lock_path(self.db)
        lock_file.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_file, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            res = self.cli('check', 'tick', '--config', str(self.config))
            self.assertEqual(res.returncode, 0)
            self.assertEqual(json.loads(res.stdout)['status'], 'skipped')
            self.assertIn('skipped', res.stderr)
            # ロック未取得経路では DB ファイルも DDL も作らない
            self.assertFalse(self.db.exists())
        finally:
            os.close(fd)

    def test_skip_on_legacy_db_without_check_ticks_warns_without_running_ddl(self):
        # 既存 Store DB (check_ticks 表なし・WAL 未切替) を用意して flock を保持したまま tick を実行
        Store(self.db)
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            mode_before = conn.execute('PRAGMA journal_mode').fetchone()[0].lower()
            tables_before = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertNotIn('check_ticks', tables_before)

        lock_file = check_lock_path(self.db)
        lock_file.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_file, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            res = self.cli('check', 'tick', '--config', str(self.config))
            self.assertEqual(res.returncode, 0)
            body = json.loads(res.stdout)
            self.assertEqual(body['status'], 'skipped')
            self.assertIsNone(body['tick_id'])
            self.assertIn('warning: check tick skipped', res.stderr)
        finally:
            os.close(fd)

        # skip 経路では DDL も WAL 切替も実行されないこと
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            mode_after = conn.execute('PRAGMA journal_mode').fetchone()[0].lower()
            tables_after = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(mode_after, mode_before)
        for tbl in ('checks', 'check_runs', 'check_run_events', 'check_ticks', 'check_tasks'):
            self.assertNotIn(tbl, tables_after)

    def test_symlink_db_path_shares_lockfile_with_real_path(self):
        self.commit_config(
            '[[checks]]\nname = "c1"\ninterval = 300\ncommand = "scripts/ok.sh"\ntimeout = 30\nskill = "s1"\n',
            {'scripts/ok.sh': '#!/bin/sh\nexit 0\n'},
        )
        # 初回 tick で実体 DB と lockfile を作成
        first = self.cli('check', 'tick', '--config', str(self.config), env_extra={'CUE_CHECK_NOW': '1700000000'})
        self.assertEqual(first.returncode, 0, first.stderr)

        # (a) ファイル symlink と (b) ディレクトリ symlink の両方で lockfile が実体パスと一致すること
        link_db = self.root / 'link.sqlite3'
        link_db.symlink_to(self.db)
        real_dir = self.root / 'real_dir'
        real_dir.mkdir()
        real_db2 = real_dir / 'cue2.sqlite3'
        link_dir = self.root / 'link_dir'
        link_dir.symlink_to(real_dir)
        link_db2 = link_dir / 'cue2.sqlite3'

        self.assertEqual(check_lock_path(link_db), check_lock_path(self.db))
        self.assertEqual(check_lock_path(link_db2), check_lock_path(real_db2))

        # 実体パス側で flock 保持中、symlink 経由の 2 本目 tick は skipped になる
        real_lock = check_lock_path(self.db)
        fd = os.open(real_lock, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            res_sym, rc_sym = run_check_tick(
                link_db, self.config,
                now_fn=lambda: 1700000500.0,
                deliver_fn=lambda c, t: True,
            )
            self.assertEqual(rc_sym, 0)
            self.assertEqual(res_sym['status'], 'skipped')
        finally:
            os.close(fd)

        # symlink 側に別の lockfile が作られていないこと、skip が実体 DB に記録されていること
        self.assertFalse((self.root / 'link.sqlite3.check.lock').exists())
        ticks = self.query_all('SELECT status FROM check_ticks ORDER BY id')
        self.assertEqual([t['status'] for t in ticks], ['ok', 'skipped'])

        # 逆方向: ディレクトリ symlink 経由で flock 保持中、実体パス経由の tick も skipped になる
        res_init, rc_init = run_check_tick(
            link_db2, self.config,
            now_fn=lambda: 1700000000.0,
            deliver_fn=lambda c, t: True,
        )
        self.assertEqual((rc_init, res_init['status']), (0, 'ok'))
        lock2 = check_lock_path(link_db2)
        fd2 = os.open(lock2, os.O_RDWR)
        try:
            fcntl.flock(fd2, fcntl.LOCK_EX | fcntl.LOCK_NB)
            res_real, rc_real = run_check_tick(
                real_db2, self.config,
                now_fn=lambda: 1700000500.0,
                deliver_fn=lambda c, t: True,
            )
            self.assertEqual(rc_real, 0)
            self.assertEqual(res_real['status'], 'skipped')
        finally:
            os.close(fd2)

    # --- 3. kill -9 後の running 残留と次 tick の継続 ---

    def test_kill_9_leaves_running_tick_and_next_tick_recovers(self):
        pgid_file = self.root / 'slow.pgid'
        self.addCleanup(_cleanup_pgid_file, pgid_file)
        self.commit_config(
            '[[checks]]\nname = "slow"\ninterval = 300\ncommand = "scripts/slow.sh"\ntimeout = 30\nskill = "s1"\n',
            {
                'scripts/slow.sh': (
                    '#!/bin/sh\n'
                    f'tmp={str(pgid_file)!r}.tmp\n'
                    'echo $$ > "$tmp"\n'
                    f'mv "$tmp" {str(pgid_file)!r}\n'
                    'sleep 20\n'
                ),
            },
        )
        env = self._cli_env({'CUE_CHECK_NOW': '1700000000'})
        proc = subprocess.Popen(
            [sys.executable, str(ROOT / 'app.py'), '--db', str(self.db), 'check', 'tick', '--config', str(self.config)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 10
            while not pgid_file.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(pgid_file.exists(), 'slow check did not start')
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=5)
        finally:
            with contextlib.suppress(ProcessLookupError, OSError):
                os.killpg(proc.pid, signal.SIGKILL)
            # 独自セッションで起動した check 側のプロセスグループも明示的に掃除する
            _cleanup_pgid_file(pgid_file)
            if proc.stdout:
                proc.stdout.close()
            if proc.stderr:
                proc.stderr.close()

        # running 行が残留している
        ticks = self.query_all('SELECT status FROM check_ticks ORDER BY id')
        self.assertEqual([t['status'] for t in ticks], ['running'])

        # 軽い script に切り替えて次 tick を回すと OS が flock 解放済みなので継続できる
        self.commit_config(
            '[[checks]]\nname = "slow"\ninterval = 300\ncommand = "scripts/fast.sh"\ntimeout = 30\nskill = "s1"\n',
            {'scripts/fast.sh': '#!/bin/sh\nexit 0\n'},
        )
        nxt = self.cli('check', 'tick', '--config', str(self.config), env_extra={'CUE_CHECK_NOW': '1700000300'})
        self.assertEqual(nxt.returncode, 0, nxt.stderr)
        self.assertEqual(json.loads(nxt.stdout)['status'], 'ok')

        # check status が running 残留を警告する
        st = self.cli('check', 'status')
        self.assertEqual(st.returncode, 0)
        self.assertIn('WARNING', st.stdout)
        self.assertIn('running 残留', st.stdout)
        st_json = json.loads(self.cli('check', 'status', '--json').stdout)
        self.assertEqual(st_json['running_leftover_count'], 1)
        self.assertEqual([t['status'] for t in st_json['recent_ticks']], ['running', 'ok'])

    # --- 4. checkout 検査と TOML 検証 (config_error) ---

    def test_checkout_and_toml_validation_rules(self):
        self.commit_config(
            '[[checks]]\nname = "c1"\ninterval = 300\ncommand = "scripts/ok.sh"\ntimeout = 30\nskill = "s1"\n',
            {'scripts/ok.sh': '#!/bin/sh\nexit 0\n'},
        )
        # untracked ファイル (.DS_Store) と未 push のローカル commit は通過する
        (self.repo / '.DS_Store').write_text('x', encoding='utf-8')
        ok_res = self.cli('check', 'tick', '--config', str(self.config), env_extra={'CUE_CHECK_NOW': '1700000000'})
        self.assertEqual(ok_res.returncode, 0, ok_res.stderr)

        # tracked ファイルの未コミット変更 -> config_error (exit 1, checks_deferred=NULL)
        (self.repo / 'scripts/ok.sh').write_text('#!/bin/sh\n# dirty\nexit 0\n', encoding='utf-8')
        dirty = self.cli('check', 'tick', '--config', str(self.config), env_extra={'CUE_CHECK_NOW': '1700000300'})
        self.assertEqual(dirty.returncode, 1)
        dirty_json = json.loads(dirty.stdout)
        self.assertEqual(dirty_json['status'], 'config_error')
        self.assertIsNone(dirty_json['checks_deferred'])
        _git(self.repo, 'checkout', '--', 'scripts/ok.sh')

        # 別ブランチ -> config_error
        _git(self.repo, 'checkout', '-b', 'feature/x')
        br = self.cli('check', 'tick', '--config', str(self.config), env_extra={'CUE_CHECK_NOW': '1700000600'})
        self.assertEqual(br.returncode, 1)
        self.assertEqual(json.loads(br.stdout)['status'], 'config_error')
        _git(self.repo, 'checkout', 'main')

        # symlink 脱出 -> config_error
        outside = self.root / 'outside.sh'
        _write_exec(outside, '#!/bin/sh\nexit 0\n')
        esc = self.repo / 'scripts/escape.sh'
        esc.symlink_to(outside)
        self.commit_config(
            '[[checks]]\nname = "good"\ninterval = 300\ncommand = "scripts/ok.sh"\ntimeout = 30\nskill = "s1"\n'
            '[[checks]]\nname = "bad"\ninterval = 300\ncommand = "scripts/escape.sh"\ntimeout = 30\nskill = "s1"\n',
        )
        sym_res = self.cli('check', 'tick', '--config', str(self.config), env_extra={'CUE_CHECK_NOW': '1700000900'})
        self.assertEqual(sym_res.returncode, 1)
        self.assertEqual(json.loads(sym_res.stdout)['status'], 'config_error')
        # 部分適用されない ("good" は checks テーブルに追加されていない)
        names = [r['name'] for r in self.query_all('SELECT name FROM checks')]
        self.assertEqual(names, ['c1'])

        # 各不正 TOML パターン (timeout > 180, interval <= 0, duplicate name, empty skill, fault_threshold < 1, .. 脱出, env キー不正)
        bad_tomls = [
            '[[checks]]\nname = "a"\ninterval = 300\ncommand = "scripts/ok.sh"\ntimeout = 181\nskill = "s"\n',
            '[[checks]]\nname = "a"\ninterval = 0\ncommand = "scripts/ok.sh"\ntimeout = 10\nskill = "s"\n',
            '[[checks]]\nname = "a"\ninterval = 300\ncommand = "scripts/ok.sh"\ntimeout = 10\nskill = ""\n',
            '[[checks]]\nname = "a"\ninterval = 300\ncommand = "scripts/ok.sh"\ntimeout = 10\nskill = "s"\nfault_threshold = 0\n',
            '[[checks]]\nname = "a"\ninterval = 300\ncommand = "../outside.sh"\ntimeout = 10\nskill = "s"\n',
            '[[checks]]\nname = "dup"\ninterval = 300\ncommand = "scripts/ok.sh"\ntimeout = 10\nskill = "s"\n'
            '[[checks]]\nname = "dup"\ninterval = 300\ncommand = "scripts/ok.sh"\ntimeout = 10\nskill = "s"\n',
            '[[checks]]\nname = "a"\ninterval = 300\ncommand = "scripts/ok.sh"\ntimeout = 10\nskill = "s"\n[checks.env]\n"BAD=KEY" = "1"\n',
            '[[checks]]\nname = "a"\ninterval = 300\ncommand = "scripts/ok.sh"\ntimeout = 10\nskill = "s"\n[checks.env]\n"" = "1"\n',
            'not valid toml === [',
        ]
        for idx, bad in enumerate(bad_tomls):
            self.commit_config(bad)
            r = self.cli('check', 'tick', '--config', str(self.config), env_extra={'CUE_CHECK_NOW': str(1700001200 + idx * 10)})
            self.assertEqual(r.returncode, 1, f'case {idx} should fail')
            self.assertEqual(json.loads(r.stdout)['status'], 'config_error', f'case {idx}')

    def test_popen_value_error_is_recorded_as_check_failure_not_running_crash(self):
        self.commit_config(
            '[[checks]]\nname = "c1"\ninterval = 300\ncommand = "scripts/ok.sh"\ntimeout = 30\nskill = "s1"\n',
            {'scripts/ok.sh': '#!/bin/sh\nexit 0\n'},
        )
        # 1. run_check_command 直接呼び出しで不正 env (Popen ValueError) が例外死せず失敗として返ること
        chk = {
            'name': 'c1',
            'argv': [str(self.repo / 'scripts/ok.sh')],
            'timeout': 10.0,
            'env': {'BAD=KEY': '1'},
        }
        rc, out, err, timed_out, aborted = run_check_command(chk, self.repo, time.monotonic() + 30.0)
        self.assertIsNone(rc)
        self.assertEqual((out, timed_out, aborted), (b'', False, False))
        self.assertTrue(len(err) > 0)

        # 2. tick 内で check 起動時の Popen が ValueError を投げても running 残留せず failed_exit として記録されること
        real_popen = subprocess.Popen

        def popen_failing_on_check(argv, *args, **kwargs):
            if argv and str(argv[0]).endswith('scripts/ok.sh'):
                raise ValueError('illegal environment variable name')
            return real_popen(argv, *args, **kwargs)

        with mock.patch('app.subprocess.Popen', side_effect=popen_failing_on_check):
            res, exit_code = run_check_tick(
                self.db, self.config,
                now_fn=lambda: 1700000000.0,
                deliver_fn=lambda c, t: True,
            )
        self.assertEqual((exit_code, res['status']), (0, 'ok'))
        run = self.query_one('SELECT outcome, exit_code, stderr_excerpt FROM check_runs')
        self.assertEqual(run['outcome'], 'failed_exit')
        self.assertIsNone(run['exit_code'])
        self.assertIn('illegal environment variable name', run['stderr_excerpt'])
        self.assertEqual(self.query_all("SELECT * FROM check_ticks WHERE status='running'"), [])

    def test_config_error_still_runs_start_wake_before_failing(self):
        self.commit_config(
            '[[checks]]\nname = "c1"\ninterval = 300\ncommand = "scripts/fire.sh"\ntimeout = 30\nskill = "s1"\n',
            {'scripts/fire.sh': '#!/bin/sh\necho \'{"key":"k1","title":"t1","body":"b1"}\'\n'},
        )
        # 1 回目: wake を失敗させて未通知タスクを残す
        r1 = self.cli('check', 'tick', '--config', str(self.config), env_extra={'CUE_CHECK_NOW': '1700000000', 'FAKE_SEND_RC': '1'})
        self.assertEqual(r1.returncode, 0)
        self.assertEqual(json.loads(r1.stdout)['unnotified_tasks'], 1)
        self.assertEqual(json.loads(r1.stdout)['wake_failed'], 1)

        # 2 回目: config を壊した状態で wake 成功 -> 開始時 wake が走って未通知タスクが 0 になり、tick は config_error
        self.commit_config('not valid = [')
        r2 = self.cli('check', 'tick', '--config', str(self.config), env_extra={'CUE_CHECK_NOW': '1700000300', 'FAKE_SEND_RC': '0'})
        self.assertEqual(r2.returncode, 1)
        b2 = json.loads(r2.stdout)
        self.assertEqual(b2['status'], 'config_error')
        self.assertEqual(b2['unnotified_tasks'], 0)
        self.assertEqual(b2['wake_failed'], 0)
        self.assertIsNone(b2['checks_deferred'])

    # --- 5. due 判定・基準時刻切替・時刻巻き戻り・順序 ---

    def test_due_base_switch_between_success_and_failure_and_clock_rollback(self):
        self.commit_config(
            '[[checks]]\nname = "c1"\ninterval = 300\ncommand = "scripts/c1.sh"\ntimeout = 30\nskill = "s1"\n',
            {'scripts/c1.sh': '#!/bin/sh\nexit 0\n'},
        )
        clock = FakeClock(wall=1000.0, mono=100.0)
        # 初回 (履歴なし): 即 due。tick_started=1000 で成功
        res, rc = run_check_tick(self.db, self.config, now_fn=clock.now, mono_fn=clock.monotonic, deliver_fn=lambda c, t: True)
        self.assertEqual((rc, res['status']), (0, 'ok'))
        self.assertEqual(len(self.query_all('SELECT * FROM check_runs')), 1)

        # grace = min(30, 300*0.1) = 30 -> 1000 + 300 - 30 = 1270 未満は due でない
        clock.wall = 1269.0
        run_check_tick(self.db, self.config, now_fn=clock.now, mono_fn=clock.monotonic, deliver_fn=lambda c, t: True)
        self.assertEqual(len(self.query_all('SELECT * FROM check_runs')), 1)

        # 1270 ちょうどで due
        clock.wall = 1270.0
        run_check_tick(self.db, self.config, now_fn=clock.now, mono_fn=clock.monotonic, deliver_fn=lambda c, t: True)
        self.assertEqual(len(self.query_all('SELECT * FROM check_runs')), 2)

        # 失敗系 (actual_started_at = tick_started + 40 となるよう設定)
        self.commit_config(
            '[[checks]]\nname = "c1"\ninterval = 300\ncommand = "scripts/fail.sh"\ntimeout = 30\nskill = "s1"\n',
            {'scripts/fail.sh': '#!/bin/sh\nexit 1\n'},
        )
        # 2000 で tick 開始、actual_started_at を 2040 相当にするため tick 中に wall を進める
        calls = {'n': 0}

        def shifting_now():
            calls['n'] += 1
            # 1 回目が tick_started_ts、以降 (actual_started 等) は +40 秒
            return 2000.0 if calls['n'] == 1 else 2040.0

        run_check_tick(self.db, self.config, now_fn=shifting_now, mono_fn=clock.monotonic, deliver_fn=lambda c, t: True)
        self.assertEqual(len(self.query_all('SELECT * FROM check_runs')), 3)
        chk = self.query_one("SELECT * FROM checks WHERE name='c1'")
        self.assertEqual(chk['last_outcome'], 'failed_exit')

        # 失敗後は last_actual_started_at (2040) 基準 -> 2040 + 300 - 30 = 2310。2300 では due でない!
        clock.wall = 2300.0
        run_check_tick(self.db, self.config, now_fn=clock.now, mono_fn=clock.monotonic, deliver_fn=lambda c, t: True)
        self.assertEqual(len(self.query_all('SELECT * FROM check_runs')), 3)

        # 2310 で due
        clock.wall = 2310.0
        run_check_tick(self.db, self.config, now_fn=clock.now, mono_fn=clock.monotonic, deliver_fn=lambda c, t: True)
        self.assertEqual(len(self.query_all('SELECT * FROM check_runs')), 4)

        # 時刻巻き戻り (tick_started = 1900 < last_started = 2310) -> 即 due かつ clock_anomaly = 1
        clock.wall = 1900.0
        run_check_tick(self.db, self.config, now_fn=clock.now, mono_fn=clock.monotonic, deliver_fn=lambda c, t: True)
        runs = self.query_all('SELECT outcome, clock_anomaly FROM check_runs ORDER BY id')
        self.assertEqual(len(runs), 5)
        self.assertEqual(runs[-1]['clock_anomaly'], 1)

    def test_due_ordering_no_history_then_clock_anomaly_then_oldest(self):
        order_log = self.root / 'order.log'
        self.commit_config(
            f'[[checks]]\nname = "old_normal"\ninterval = 60\ncommand = "scripts/rec.sh"\ntimeout = 10\nskill = "s"\nenv = {{NAME = "old_normal"}}\n'
            f'[[checks]]\nname = "rollback"\ninterval = 60\ncommand = "scripts/rec.sh"\ntimeout = 10\nskill = "s"\nenv = {{NAME = "rollback"}}\n'
            f'[[checks]]\nname = "fresh"\ninterval = 60\ncommand = "scripts/rec.sh"\ntimeout = 10\nskill = "s"\nenv = {{NAME = "fresh"}}\n',
            {'scripts/rec.sh': f'#!/bin/sh\necho "$NAME" >> {str(order_log)!r}\n'},
        )
        # 初期登録だけ済ませて手動で履歴を配置
        res = self.cli('check', 'tick', '--config', str(self.config), env_extra={'CUE_CHECK_NOW': '1700000000'})
        self.assertEqual(res.returncode, 0)
        order_log.unlink()

        with contextlib.closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE checks SET last_started=NULL, last_actual_started_at=NULL, last_outcome=NULL WHERE name='fresh'")
            conn.execute(
                "UPDATE checks SET last_started=?, last_actual_started_at=?, last_outcome='ok_silent' WHERE name='rollback'",
                (datetime.fromtimestamp(1700005000, timezone.utc).isoformat(timespec='microseconds'),
                 datetime.fromtimestamp(1700005000, timezone.utc).isoformat(timespec='microseconds')),
            )
            conn.execute(
                "UPDATE checks SET last_started=?, last_actual_started_at=?, last_outcome='ok_silent' WHERE name='old_normal'",
                (datetime.fromtimestamp(1700000100, timezone.utc).isoformat(timespec='microseconds'),
                 datetime.fromtimestamp(1700000100, timezone.utc).isoformat(timespec='microseconds')),
            )

        res2 = self.cli('check', 'tick', '--config', str(self.config), env_extra={'CUE_CHECK_NOW': '1700001000'})
        self.assertEqual(res2.returncode, 0)
        executed = order_log.read_text(encoding='utf-8').splitlines()
        self.assertEqual(executed, ['fresh', 'rollback', 'old_normal'])

    # --- 6. tick 上限・先頭免除・2 件目以降ガード・aborted / failed_timeout・プロセスグループ終了 ---

    def test_first_check_exemption_aborted_does_not_mutate_check_state(self):
        self.commit_config(
            '[[checks]]\nname = "c1"\ninterval = 300\ncommand = "scripts/sleep.sh"\ntimeout = 10\nskill = "s1"\n'
            '[[checks]]\nname = "c2"\ninterval = 300\ncommand = "scripts/ok.sh"\ntimeout = 10\nskill = "s1"\n',
            {
                'scripts/sleep.sh': '#!/bin/sh\nsleep 5\n',
                'scripts/ok.sh': '#!/bin/sh\nexit 0\n',
            },
        )
        # まず c1 を 1 回失敗させて fault_count=1, last_outcome='failed_exit' を作っておく
        self.commit_config(
            '[[checks]]\nname = "c1"\ninterval = 60\ncommand = "scripts/fail_or_sleep.sh"\ntimeout = 10\nskill = "s1"\n'
            '[[checks]]\nname = "c2"\ninterval = 60\ncommand = "scripts/ok.sh"\ntimeout = 10\nskill = "s1"\n',
            {
                'scripts/fail_or_sleep.sh': '#!/bin/sh\nif [ "${MODE:-}" = sleep ]; then sleep 5; else exit 1; fi\n',
                'scripts/ok.sh': '#!/bin/sh\nexit 0\n',
            },
        )
        r0 = self.cli('check', 'tick', '--config', str(self.config), env_extra={'CUE_CHECK_NOW': '1700000000'})
        self.assertEqual(r0.returncode, 0)
        before_c1 = self.query_one("SELECT last_started, last_actual_started_at, last_outcome, fault_count FROM checks WHERE name='c1'")
        self.assertEqual((before_c1['last_outcome'], before_c1['fault_count']), ('failed_exit', 1))

        # tick_budget=4.5s, finalize_reserve=0.2s, commit_reserve=0.5s, kill_grace=0.2s
        # -> end_work_deadline=4.3s, commit_start_deadline=3.8s, cleanup_reserve=1.7s
        #    (kill_grace 0.2s + CHECK_KILL_WAIT_SEC 1.0s + CHECK_DRAIN_JOIN_SEC 0.5s),
        #    exec_deadline=2.1s (< c1.timeout=10s)。
        # 先頭 c1 は免除で着手し exec_deadline で aborted。孤児 reap が遅く cleanup が最大所要になっても
        # commit_start_deadline (3.8s) までに終わり、commit_reserve (0.5s) 内で aborted run を保存し、
        # c2 は未着手で checks_deferred=1、status='aborted' になる。
        # setup (git / SQLite) の所要時間に依存しないよう、最初の check 起動時に exec_deadline 0.3s 手前から時計を進める
        real_origin = [None]

        def mono_fn():
            if real_origin[0] is None:
                return 0.0
            return 1.8 + (time.monotonic() - real_origin[0])

        def start_clock_on_first_check(*args, **kwargs):
            if real_origin[0] is None:
                real_origin[0] = time.monotonic()
            return run_check_command(*args, **kwargs)

        with mock.patch.dict(os.environ, {'MODE': 'sleep'}), mock.patch(
            'app.run_check_command', side_effect=start_clock_on_first_check,
        ):
            b1, rc1 = run_check_tick(
                self.db, self.config,
                now_fn=lambda: 1700001000.0,
                mono_fn=mono_fn,
                tick_budget=4.5,
                finalize_reserve=0.2,
                commit_reserve=0.5,
                kill_grace=0.2,
                deliver_fn=lambda c, t: True,
            )
        self.assertEqual(rc1, 0)
        self.assertEqual(b1['status'], 'aborted')
        self.assertEqual(b1['checks_deferred'], 1)

        # check_runs に aborted が記録され、checks の実行状態カラムは一切変わらない!
        after_c1 = self.query_one("SELECT last_started, last_actual_started_at, last_outcome, fault_count FROM checks WHERE name='c1'")
        self.assertEqual(after_c1, before_c1)
        c1_runs = [r['outcome'] for r in self.query_all("SELECT outcome FROM check_runs WHERE check_name='c1' ORDER BY id")]
        self.assertEqual(c1_runs, ['failed_exit', 'aborted'])

    def test_step6_remaining_zero_aborts_with_zero_runs_and_all_deferred(self):
        self.commit_config(
            '[[checks]]\nname = "c1"\ninterval = 60\ncommand = "scripts/ok.sh"\ntimeout = 10\nskill = "s1"\n'
            '[[checks]]\nname = "c2"\ninterval = 60\ncommand = "scripts/ok.sh"\ntimeout = 10\nskill = "s1"\n',
            {'scripts/ok.sh': '#!/bin/sh\nexit 0\n'},
        )
        clock = FakeClock(wall=1700000000.0, mono=1000.0)
        orig_sync = sys.modules['app'].sync_checks_mirror

        def exhaust_budget_before_step6(*args, **kwargs):
            res = orig_sync(*args, **kwargs)
            # mirror 同期までは予算内で完了し、step (6) 開始時点で残り時間 0 に到達させる
            clock.advance(10.0)
            return res

        with mock.patch('app.sync_checks_mirror', side_effect=exhaust_budget_before_step6):
            body, rc = run_check_tick(
                self.db, self.config,
                now_fn=clock.now,
                mono_fn=clock.monotonic,
                tick_budget=10.0,
                deliver_fn=lambda c, t: True,
            )
        self.assertEqual(rc, 0)
        self.assertEqual(body['status'], 'aborted')
        self.assertEqual(body['checks_deferred'], 2)
        self.assertEqual(self.query_all('SELECT * FROM check_runs'), [])

    def test_second_check_guard_skips_heavy_and_runs_light_check_with_status_ok(self):
        self.commit_config(
            '[[checks]]\nname = "c1"\ninterval = 60\ncommand = "scripts/fast.sh"\ntimeout = 1\nskill = "s1"\n'
            '[[checks]]\nname = "c2_heavy"\ninterval = 60\ncommand = "scripts/fast.sh"\ntimeout = 50\nskill = "s1"\n'
            '[[checks]]\nname = "c3_light"\ninterval = 60\ncommand = "scripts/fast.sh"\ntimeout = 1\nskill = "s1"\n',
            {'scripts/fast.sh': '#!/bin/sh\nexit 0\n'},
        )
        # tick_budget = 5s: c1 (timeout 1) 実行 -> c2_heavy (timeout 50 > remaining) 見送り -> c3_light (timeout 1) 実行!
        clock = FakeClock(wall=1700000000.0, mono=1000.0)
        body, rc = run_check_tick(
            self.db, self.config,
            now_fn=clock.now,
            mono_fn=clock.monotonic,
            tick_budget=5.0,
            deliver_fn=lambda c, t: True,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(body['status'], 'ok')
        self.assertEqual(body['checks_deferred'], 1)
        ran = [r['check_name'] for r in self.query_all('SELECT check_name FROM check_runs ORDER BY id')]
        self.assertEqual(ran, ['c1', 'c3_light'])

    def test_checks_deferred_sums_guard_skip_and_post_abort_unstarted(self):
        self.commit_config(
            '[[checks]]\nname = "c1_fast"\ninterval = 60\ncommand = "scripts/fast.sh"\ntimeout = 1\nskill = "s1"\n'
            '[[checks]]\nname = "c2_guard_skip"\ninterval = 60\ncommand = "scripts/fast.sh"\ntimeout = 50\nskill = "s1"\n'
            '[[checks]]\nname = "c3_aborted"\ninterval = 60\ncommand = "scripts/slow.sh"\ntimeout = 1\nskill = "s1"\n'
            '[[checks]]\nname = "c4_unstarted"\ninterval = 60\ncommand = "scripts/fast.sh"\ntimeout = 1\nskill = "s1"\n'
            '[[checks]]\nname = "c5_unstarted"\ninterval = 60\ncommand = "scripts/fast.sh"\ntimeout = 1\nskill = "s1"\n',
            {
                'scripts/fast.sh': '#!/bin/sh\nexit 0\n',
                'scripts/slow.sh': '#!/bin/sh\nsleep 5\n',
            },
        )
        # tick_budget=4.5s, finalize_reserve=0.2s, commit_reserve=0.5s, kill_grace=0.1s
        # -> end_work_deadline=4.3s, commit_start_deadline=3.8s, cleanup_reserve=1.6s, exec_deadline=2.2s。
        # c1_fast (timeout=1) 完了後に c2_guard_skip (timeout=50) が着手前ガードで見送り (+1)、
        # c3_aborted (timeout=1) はガード通過後に残り時間が縮んで exec_deadline (2.2s) で aborted になり、
        # cleanup_reserve (1.6s) と commit_reserve (0.5s) 内で ('c3_aborted', 'aborted') を保存して tick は status='aborted' になる。
        # 残りの c4_unstarted / c5_unstarted (+2) と合算されて checks_deferred == 3 になること
        # setup 時間で c1_fast / c3_aborted の前提が崩れないよう、c3_aborted 着手時に exec_deadline 0.3s 手前から時計を進める
        c3_real_origin = [None]

        def mono_fn():
            if c3_real_origin[0] is None:
                return 0.0
            return 1.9 + (time.monotonic() - c3_real_origin[0])

        def wrapped_run_check(chk, *args, **kwargs):
            if chk['name'] == 'c3_aborted':
                c3_real_origin[0] = time.monotonic()
            return run_check_command(chk, *args, **kwargs)

        with mock.patch('app.run_check_command', side_effect=wrapped_run_check):
            body, rc = run_check_tick(
                self.db, self.config,
                now_fn=lambda: 1700000000.0,
                mono_fn=mono_fn,
                tick_budget=4.5,
                finalize_reserve=0.2,
                commit_reserve=0.5,
                kill_grace=0.1,
                deliver_fn=lambda c, t: True,
            )
        self.assertEqual(rc, 0)
        self.assertEqual(body['status'], 'aborted')
        self.assertEqual(body['checks_deferred'], 3)
        runs = [(r['check_name'], r['outcome']) for r in self.query_all('SELECT check_name, outcome FROM check_runs ORDER BY id')]
        self.assertEqual(runs, [('c1_fast', 'ok_silent'), ('c3_aborted', 'aborted')])
        tick_row = self.query_one('SELECT status, checks_deferred FROM check_ticks ORDER BY id DESC LIMIT 1')
        self.assertEqual((tick_row['status'], tick_row['checks_deferred']), ('aborted', 3))

    def test_deadline_crossed_after_guard_defers_unstarted_checks_consistently(self):
        ran_marker = self.root / 'should_not_run.marker'
        self.commit_config(
            '[[checks]]\nname = "c1"\ninterval = 60\ncommand = "scripts/mark.sh"\ntimeout = 10\nskill = "s1"\n'
            '[[checks]]\nname = "c2"\ninterval = 60\ncommand = "scripts/mark.sh"\ntimeout = 10\nskill = "s1"\n',
            {
                'scripts/mark.sh': f'#!/bin/sh\necho ran > {str(ran_marker)!r}\nexit 0\n',
            },
        )
        # (a) 先頭 check (idx == 0) がガード通過後・Popen 直前で deadline を跨いだ場合:
        #     step (6) 開始時と同じ規則で未着手の全件 (c1 + c2 = 2 件) を checks_deferred に数え、
        #     check_runs を書かず status='aborted' (exit 0) で終えること
        clock = FakeClock(wall=1700000000.0, mono=1000.0)
        spawned_commands = []
        orig_popen = subprocess.Popen

        def spy_popen(argv, *args, **kwargs):
            spawned_commands.append(list(argv))
            return orig_popen(argv, *args, **kwargs)

        def advance_past_deadline_after_guard(chk, *args, **kwargs):
            # budget guard を通過した後、run_check_command の Popen 直前で deadline を跨ぐ
            clock.advance(1.5)
            return run_check_command(chk, *args, **kwargs)

        with mock.patch('app.subprocess.Popen', side_effect=spy_popen), mock.patch(
            'app.run_check_command', side_effect=advance_past_deadline_after_guard,
        ):
            body, rc = run_check_tick(
                self.db, self.config,
                now_fn=clock.now,
                mono_fn=clock.monotonic,
                tick_budget=1.0,
                deliver_fn=lambda c, t: True,
            )

        self.assertEqual(rc, 0)
        self.assertEqual(body['status'], 'aborted')
        self.assertEqual(body['checks_deferred'], 2)
        self.assertFalse(ran_marker.exists())
        self.assertFalse(any(any('mark.sh' in part for part in cmd) for cmd in spawned_commands))
        self.assertEqual(self.query_all('SELECT * FROM check_runs'), [])
        c1_state = self.query_one("SELECT last_started, last_actual_started_at, last_outcome, fault_count FROM checks WHERE name='c1'")
        self.assertEqual(
            (c1_state['last_started'], c1_state['last_actual_started_at'], c1_state['last_outcome'], c1_state['fault_count']),
            (None, None, None, 0),
        )

        # (b) 2 件目 (idx == 1) がガード通過後・Popen 直前で exec_deadline を跨いだ場合:
        #     c1 は完了しており、未着手の c2 + c3 = 2 件が checks_deferred に計上され、
        #     check_runs には c1 のみ、tick は status='ok' (exit 0) になること
        c2_marker = self.root / 'c2_should_not_run.marker'
        self.commit_config(
            '[[checks]]\nname = "c1"\ninterval = 60\ncommand = "scripts/ok.sh"\ntimeout = 1\nskill = "s1"\n'
            '[[checks]]\nname = "c2"\ninterval = 60\ncommand = "scripts/mark_c2.sh"\ntimeout = 1\nskill = "s1"\n'
            '[[checks]]\nname = "c3"\ninterval = 60\ncommand = "scripts/ok.sh"\ntimeout = 1\nskill = "s1"\n',
            {
                'scripts/ok.sh': '#!/bin/sh\nexit 0\n',
                'scripts/mark_c2.sh': f'#!/bin/sh\necho ran > {str(c2_marker)!r}\nexit 0\n',
            },
        )
        clock2 = FakeClock(wall=1700000100.0, mono=2000.0)

        def advance_past_deadline_on_c2(chk, *args, **kwargs):
            if chk['name'] == 'c2':
                clock2.advance(5.0)
            return run_check_command(chk, *args, **kwargs)

        with mock.patch('app.run_check_command', side_effect=advance_past_deadline_on_c2):
            body2, rc2 = run_check_tick(
                self.db, self.config,
                now_fn=clock2.now,
                mono_fn=clock2.monotonic,
                tick_budget=5.0,
                deliver_fn=lambda c, t: True,
            )

        self.assertEqual(rc2, 0)
        self.assertEqual(body2['status'], 'ok')
        self.assertEqual(body2['checks_deferred'], 2)
        self.assertFalse(c2_marker.exists())
        runs2 = [(r['check_name'], r['outcome']) for r in self.query_all('SELECT check_name, outcome FROM check_runs ORDER BY id')]
        self.assertEqual(runs2, [('c1', 'ok_silent')])

    def test_cli_ignores_fixed_budget_override_env_vars(self):
        self.commit_config(
            '[[checks]]\nname = "c1"\ninterval = 60\ncommand = "scripts/ok.sh"\ntimeout = 10\nskill = "s1"\n',
            {'scripts/ok.sh': '#!/bin/sh\nexit 0\n'},
        )
        # 設計上固定の上限を環境変数で 0 に書き換えようとしても本番 CLI は無視し、通常通り実行される
        res = self.cli(
            'check', 'tick', '--config', str(self.config),
            env_extra={
                'CUE_CHECK_NOW': '1700000000',
                'CUE_CHECK_TICK_BUDGET_SEC': '0',
                'CUE_CHECK_WAKE_BUDGET_SEC': '0',
                'CUE_CHECK_WAKE_ITEM_TIMEOUT_SEC': '999',
                'CUE_CHECK_KILL_GRACE_SEC': '0',
            },
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        body = json.loads(res.stdout)
        self.assertEqual(body['status'], 'ok')
        self.assertEqual(body['checks_deferred'], 0)
        runs = self.query_all('SELECT check_name, outcome FROM check_runs')
        self.assertEqual(runs, [{'check_name': 'c1', 'outcome': 'ok_silent'}])

    def test_timeout_priority_and_process_group_sigterm_then_sigkill(self):
        child_pid_file = self.root / 'child.pid'
        self.addCleanup(_cleanup_pid_file, child_pid_file)
        self.commit_config(
            '[[checks]]\nname = "hang"\ninterval = 60\ncommand = "scripts/hang.sh"\ntimeout = 0.5\nskill = "s1"\n',
            {
                'scripts/hang.sh': (
                    '#!/bin/sh\n'
                    'trap "" TERM\n'
                    f'python3 -c \'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)\' &\n'
                    f'echo $! > {str(child_pid_file)!r}\n'
                    'wait\n'
                ),
            },
        )
        body, rc = run_check_tick(
            self.db, self.config,
            now_fn=lambda: 1700000000.0,
            kill_grace=0.2,
            deliver_fn=lambda c, t: True,
        )
        self.assertEqual((rc, body['status']), (0, 'ok'))
        run = self.query_one('SELECT outcome, exit_code FROM check_runs')
        self.assertEqual(run['outcome'], 'failed_timeout')
        self.assertIsNone(run['exit_code'])
        # 孫プロセスも SIGKILL で確実に停止していること (未 reap の zombie も停止済みとみなす)
        child_pid = int(child_pid_file.read_text(encoding='utf-8').strip())
        self.assertTrue(_wait_pid_stopped(child_pid, timeout=2.0), f'child pid {child_pid} still running')

    def test_timeout_sigkills_term_ignoring_child_after_leader_exits_on_term(self):
        child_pid_file = self.root / 'child_orphan.pid'
        self.addCleanup(_cleanup_pid_file, child_pid_file)
        child_ready_flag = self.root / 'child_ready.flag'
        leader_exited_flag = self.root / 'leader_exited.flag'
        child_code = (
            'import signal, time; '
            'signal.signal(signal.SIGTERM, signal.SIG_IGN); '
            f'open({str(child_ready_flag)!r}, "w", encoding="utf-8").write("ready"); '
            'time.sleep(60)'
        )
        leader_script = (
            '#!/usr/bin/env python3\n'
            'import os, signal, subprocess, sys, time\n'
            'child = subprocess.Popen(\n'
            f'    [sys.executable, "-c", {child_code!r}],\n'
            '    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,\n'
            ')\n'
            f'open({str(child_pid_file)!r}, "w", encoding="utf-8").write(str(child.pid))\n'
            'def on_term(_signum, _frame):\n'
            f'    open({str(leader_exited_flag)!r}, "w", encoding="utf-8").write("term")\n'
            '    os._exit(0)\n'
            'signal.signal(signal.SIGTERM, on_term)\n'
            'time.sleep(60)\n'
        )
        self.commit_config(
            '[[checks]]\nname = "leader_exits"\ninterval = 60\ncommand = "scripts/leader_exits.py"\ntimeout = 1.0\nskill = "s1"\n',
            {'scripts/leader_exits.py': leader_script},
        )
        body, rc = run_check_tick(
            self.db, self.config,
            now_fn=lambda: 1700000000.0,
            kill_grace=0.2,
            deliver_fn=lambda c, t: True,
        )
        self.assertEqual((rc, body['status']), (0, 'ok'))
        run = self.query_one('SELECT outcome, exit_code FROM check_runs')
        self.assertEqual(run['outcome'], 'failed_timeout')
        self.assertIsNone(run['exit_code'])
        # 子が SIGTERM 無視を設定済みであり、リーダーが SIGTERM で即終了したこと (既存テストと区別)
        self.assertTrue(child_ready_flag.exists())
        self.assertTrue(leader_exited_flag.exists())
        # TERM を無視した子プロセスも猶予後の SIGKILL で停止していること (未 reap の zombie も停止済みとみなす)
        child_pid = int(child_pid_file.read_text(encoding='utf-8').strip())
        self.assertTrue(_wait_pid_stopped(child_pid, timeout=2.0), f'orphan child pid {child_pid} still running')

    def test_terminate_process_group_bounds_wait_after_sigkill(self):
        signals_sent = []
        wait_timeouts = []

        class UnkillableFakeProcess:
            pid = 999999
            returncode = None

            def poll(self):
                return None

            def wait(self, timeout=None):
                wait_timeouts.append(timeout)
                if timeout is None:
                    raise AssertionError('unbounded wait() called after SIGKILL')
                time.sleep(min(timeout, 0.05))
                raise subprocess.TimeoutExpired(cmd=['fake'], timeout=timeout)

        def fake_killpg(pgid, sig):
            signals_sent.append((pgid, sig))

        stderr_buf = io.StringIO()
        started = time.monotonic()
        with mock.patch('app.os.killpg', side_effect=fake_killpg), contextlib.redirect_stderr(stderr_buf):
            _terminate_process_group(UnkillableFakeProcess(), grace_sec=0.05, kill_wait_sec=0.05)
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 0.5)
        self.assertIn((999999, signal.SIGTERM), signals_sent)
        self.assertIn((999999, signal.SIGKILL), signals_sent)
        self.assertGreaterEqual(len(wait_timeouts), 2)
        self.assertTrue(all(t is not None and t >= 0.0 for t in wait_timeouts))
        self.assertIn('warning:', stderr_buf.getvalue())
        self.assertIn('SIGKILL', stderr_buf.getvalue())

    # --- 7. JSONL 契約と検証違反 (failed_output) ---

    def test_jsonl_validation_and_failed_output_creates_zero_tasks(self):
        # 正常系 (空行・未知フィールド・空 body・key 省略)
        ok, evs = parse_check_jsonl(b'\n  \n{"title":"T","body":"","extra":123}\n')
        self.assertTrue(ok)
        self.assertEqual(evs, [('', 'T', '')])

        # 各種違反パターン
        invalid_cases = [
            b'a' * 65537,  # > 64KiB
            b'\xff\xfe',   # invalid UTF-8
            b'not json\n',
            b'[1, 2]\n',
            b'{"body":"b"}\n',  # missing title
            b'{"title":"t"}\n',  # missing body
            b'{"title":"","body":"b"}\n',  # empty title
            json.dumps({'title': 'x' * 201, 'body': 'b'}).encode(),  # > 200 code points
            b'{"key":null,"title":"t","body":"b"}\n',  # null key
            b'{"key":"","title":"t","body":"b"}\n',    # empty key
            b'{"key":"bad key!","title":"t","body":"b"}\n',  # invalid chars
            json.dumps({'key': 'a' * 201, 'title': 't', 'body': 'b'}).encode(),  # > 200 bytes
            b'{"key":"k1","title":"t1","body":"b"}\n{"key":"k1","title":"t2","body":"b"}\n',  # dup key
            b'{"title":"t1","body":"b"}\n{"title":"t2","body":"b"}\n',  # dup omitted key
        ]
        for raw in invalid_cases:
            valid, parsed = parse_check_jsonl(raw)
            self.assertFalse(valid, f'should be invalid: {raw[:60]!r}')
            self.assertIsNone(parsed)

        # 1 行目が正常でも 2 行目が不正なら 1 件も登録せず failed_output
        self.commit_config(
            '[[checks]]\nname = "badout"\ninterval = 60\ncommand = "scripts/bad.sh"\ntimeout = 10\nskill = "s1"\n',
            {'scripts/bad.sh': '#!/bin/sh\necho \'{"key":"k1","title":"ok","body":"b"}\'\necho \'{"key":"k1","title":"dup","body":"b"}\'\n'},
        )
        res = self.cli('check', 'tick', '--config', str(self.config), env_extra={'CUE_CHECK_NOW': '1700000000'})
        self.assertEqual(res.returncode, 0)
        self.assertEqual(self.query_one('SELECT outcome FROM check_runs')['outcome'], 'failed_output')
        self.assertEqual(self.query_all('SELECT * FROM tasks'), [])
        self.assertEqual(self.query_all('SELECT * FROM check_tasks'), [])

        # exit != 0 は不正 stdout でも failed_exit が優先
        self.commit_config(
            '[[checks]]\nname = "badexit"\ninterval = 60\ncommand = "scripts/badexit.sh"\ntimeout = 10\nskill = "s1"\n',
            {'scripts/badexit.sh': '#!/bin/sh\necho "not-json"\nexit 2\n'},
        )
        self.cli('check', 'tick', '--config', str(self.config), env_extra={'CUE_CHECK_NOW': '1700000100'})
        self.assertEqual(self.query_one("SELECT outcome, exit_code FROM check_runs WHERE check_name='badexit'"), {'outcome': 'failed_exit', 'exit_code': 2})

    # --- 8. 重複抑制 (check, kind, dedup_key) と 20 件起票上限 (suppressed 非消費) ---

    def test_dedup_boundaries_and_20_cap_does_not_consume_suppressed(self):
        # 25 件出す script (k0..k24)
        emit_py = (
            '#!/usr/bin/env python3\nimport json\n'
            'for i in range(25):\n'
            '    print(json.dumps({"key": f"k{i:02d}", "title": f"ev {i}", "body": f"key: fake-{i}\\nbody {i}"}))\n'
        )
        self.commit_config(
            '[[checks]]\nname = "many"\ninterval = 60\ncommand = "scripts/many.py"\ntimeout = 10\nskill = "my-skill"\n',
            {'scripts/many.py': emit_py},
        )
        # 1 回目: k00..k19 の 20 件起票、k20..k24 の 5 件が events_deferred
        r1 = self.cli('check', 'tick', '--config', str(self.config), env_extra={'CUE_CHECK_NOW': '1700000000'})
        self.assertEqual(r1.returncode, 0, r1.stderr)
        run1 = self.query_one('SELECT * FROM check_runs ORDER BY id DESC LIMIT 1')
        self.assertEqual(run1['outcome'], 'fired')
        self.assertEqual(run1['events_deferred'], 5)
        self.assertEqual(len(self.query_all('SELECT * FROM tasks')), 20)
        # skill は TOML の静的値、dedup_key は JSON の key (body の "key: fake" は無視)
        ct_keys = {r['dedup_key'] for r in self.query_all('SELECT dedup_key, skill FROM check_tasks')}
        self.assertEqual(ct_keys, {f'k{i:02d}' for i in range(20)})
        self.assertTrue(all(r['skill'] == 'my-skill' for r in self.query_all('SELECT skill FROM check_tasks')))

        # 2 回目: 先頭 20 件は suppressed (上限枠を消費しない!)、後ろ 5 件 (k20..k24) が新規起票!
        r2 = self.cli('check', 'tick', '--config', str(self.config), env_extra={'CUE_CHECK_NOW': '1700000100'})
        self.assertEqual(r2.returncode, 0, r2.stderr)
        run2 = self.query_one('SELECT * FROM check_runs ORDER BY id DESC LIMIT 1')
        self.assertEqual(run2['outcome'], 'mixed')
        self.assertEqual(run2['events_deferred'], 0)
        self.assertEqual(len(self.query_all('SELECT * FROM tasks')), 25)
        ev2 = self.query_all('SELECT outcome, suppressed_by_task_id, task_id FROM check_run_events WHERE run_id=?', (run2['id'],))
        self.assertEqual(sum(1 for e in ev2 if e['outcome'] == 'suppressed' and e['suppressed_by_task_id']), 20)
        self.assertEqual(sum(1 for e in ev2 if e['outcome'] == 'fired' and e['task_id']), 5)

        # 3 回目: 全 25 件が suppressed
        r3 = self.cli('check', 'tick', '--config', str(self.config), env_extra={'CUE_CHECK_NOW': '1700000200'})
        self.assertEqual(r3.returncode, 0)
        run3 = self.query_one('SELECT * FROM check_runs ORDER BY id DESC LIMIT 1')
        self.assertEqual(run3['outcome'], 'suppressed')
        self.assertEqual(run3['events_deferred'], 0)

        # k00 のタスクを done にすると k00 だけ再起票される
        k00_id = self.query_one("SELECT task_id FROM check_tasks WHERE dedup_key='k00'")['task_id']
        Store(self.db).reconcile_lane(k00_id, 'done')
        r4 = self.cli('check', 'tick', '--config', str(self.config), env_extra={'CUE_CHECK_NOW': '1700000300'})
        self.assertEqual(r4.returncode, 0)
        run4 = self.query_one('SELECT * FROM check_runs ORDER BY id DESC LIMIT 1')
        self.assertEqual(run4['outcome'], 'mixed')
        self.assertEqual(len(self.query_all('SELECT * FROM tasks')), 26)

    def test_suppressed_events_after_cap_are_still_recorded(self):
        # k20 が既に未完了、script は n00..n19 (20 新規) + k20 (suppressed) + n20 (deferred) を出す
        emit_py = (
            '#!/usr/bin/env python3\nimport json, os\n'
            'if os.environ.get("PHASE") == "seed":\n'
            '    print(json.dumps({"key": "k20", "title": "seed", "body": ""}))\n'
            'else:\n'
            '    for i in range(20):\n'
            '        print(json.dumps({"key": f"n{i:02d}", "title": f"n {i}", "body": ""}))\n'
            '    print(json.dumps({"key": "k20", "title": "dup after cap", "body": ""}))\n'
            '    print(json.dumps({"key": "n20", "title": "deferred", "body": ""}))\n'
        )
        self.commit_config(
            '[[checks]]\nname = "cap"\ninterval = 60\ncommand = "scripts/cap.py"\ntimeout = 10\nskill = "s"\n',
            {'scripts/cap.py': emit_py},
        )
        self.cli('check', 'tick', '--config', str(self.config), env_extra={'CUE_CHECK_NOW': '1700000000', 'PHASE': 'seed'})
        self.cli('check', 'tick', '--config', str(self.config), env_extra={'CUE_CHECK_NOW': '1700000100', 'PHASE': 'full'})
        run = self.query_one('SELECT * FROM check_runs ORDER BY id DESC LIMIT 1')
        self.assertEqual(run['outcome'], 'mixed')
        self.assertEqual(run['events_deferred'], 1)
        evs = self.query_all('SELECT dedup_key, outcome, suppressed_by_task_id FROM check_run_events WHERE run_id=?', (run['id'],))
        self.assertEqual(len(evs), 21)
        supp = [e for e in evs if e['dedup_key'] == 'k20']
        self.assertEqual(len(supp), 1)
        self.assertEqual(supp[0]['outcome'], 'suppressed')
        self.assertIsNotNone(supp[0]['suppressed_by_task_id'])

    # --- 9. 故障 counter と故障タスクの状態遷移表・名前空間分離・TOML 変更リセット ---

    def test_fault_lifecycle_namespace_isolation_and_config_reset(self):
        self.commit_config(
            '[[checks]]\nname = "flaky"\ninterval = 60\ncommand = "scripts/flaky.sh"\ntimeout = 10\nskill = "ev-skill"\nfault_threshold = 3\n',
            {
                'scripts/flaky.sh': (
                    '#!/bin/sh\n'
                    'case "${MODE:-ok}" in\n'
                    '  fail) echo "boom" >&2; exit 1 ;;\n'
                    '  fire) echo \'{"title":"ev-no-key","body":"b"}\'; exit 0 ;;\n'
                    '  ok) exit 0 ;;\n'
                    'esac\n'
                ),
            },
        )
        t = 1700000000
        # まず key 省略 (dedup_key="") の通常タスクを起票しておく (故障タスクとの名前空間分離を検証)
        self.cli('check', 'tick', '--config', str(self.config), env_extra={'CUE_CHECK_NOW': str(t), 'MODE': 'fire'})
        ev_tasks = self.query_all("SELECT * FROM check_tasks WHERE kind='event' AND dedup_key=''")
        self.assertEqual(len(ev_tasks), 1)

        # 失敗 1 回目 -> fault_count = 1
        t += 100
        self.cli('check', 'tick', '--config', str(self.config), env_extra={'CUE_CHECK_NOW': str(t), 'MODE': 'fail'})
        self.assertEqual(self.query_one("SELECT fault_count FROM checks WHERE name='flaky'")['fault_count'], 1)

        # env / timeout のみの変更では fault_count はリセットされない!
        self.commit_config(
            '[[checks]]\nname = "flaky"\ninterval = 60\ncommand = "scripts/flaky.sh"\ntimeout = 20\nskill = "ev-skill"\nfault_threshold = 3\n'
            '[checks.env]\nFOO = "bar"\n',
        )
        t += 100
        self.cli('check', 'tick', '--config', str(self.config), env_extra={'CUE_CHECK_NOW': str(t), 'MODE': 'fail'})
        self.assertEqual(self.query_one("SELECT fault_count FROM checks WHERE name='flaky'")['fault_count'], 2)

        # 成功すると fault_count は 0 に戻る
        t += 100
        self.cli('check', 'tick', '--config', str(self.config), env_extra={'CUE_CHECK_NOW': str(t), 'MODE': 'ok'})
        self.assertEqual(self.query_one("SELECT fault_count FROM checks WHERE name='flaky'")['fault_count'], 0)

        # 連続 3 回失敗 -> 未完了の通常タスク (dedup_key="") があっても故障タスクが 1 件起票され fault_count=0!
        for _ in range(3):
            t += 100
            self.cli('check', 'tick', '--config', str(self.config), env_extra={'CUE_CHECK_NOW': str(t), 'MODE': 'fail'})
        faults = self.query_all("SELECT * FROM check_tasks WHERE kind='fault'")
        self.assertEqual(len(faults), 1)
        self.assertEqual((faults[0]['dedup_key'], faults[0]['skill']), ('', CHECK_FAULT_SKILL))
        self.assertEqual(self.query_one("SELECT fault_count FROM checks WHERE name='flaky'")['fault_count'], 0)

        # 未完了の故障タスクがある間の追加失敗 -> fault_count=0 のまま、二重起票なし
        t += 100
        self.cli('check', 'tick', '--config', str(self.config), env_extra={'CUE_CHECK_NOW': str(t), 'MODE': 'fail'})
        self.assertEqual(self.query_one("SELECT fault_count FROM checks WHERE name='flaky'")['fault_count'], 0)
        self.assertEqual(len(self.query_all("SELECT * FROM check_tasks WHERE kind='fault'")), 1)

        # 通常タスク (dedup_key="") を完了後、未完了の故障タスクが存在していても通常タスクを再起票できる (名前空間分離)
        Store(self.db).reconcile_lane(ev_tasks[0]['task_id'], 'done')
        t += 100
        self.cli('check', 'tick', '--config', str(self.config), env_extra={'CUE_CHECK_NOW': str(t), 'MODE': 'fire'})
        self.assertEqual(len(self.query_all("SELECT * FROM check_tasks WHERE kind='event'")), 2)

        # 故障タスクを完了させると、再度 3 回失敗で再起票される
        Store(self.db).reconcile_lane(faults[0]['task_id'], 'done')
        for _ in range(3):
            t += 100
            self.cli('check', 'tick', '--config', str(self.config), env_extra={'CUE_CHECK_NOW': str(t), 'MODE': 'fail'})
        self.assertEqual(len(self.query_all("SELECT * FROM check_tasks WHERE kind='fault'")), 2)

        # 故障タスクを完了して 2 回失敗させた後、command 変更で fault_count=0 にリセット
        latest_fault = self.query_all("SELECT task_id FROM check_tasks WHERE kind='fault' ORDER BY created_at")[-1]['task_id']
        Store(self.db).reconcile_lane(latest_fault, 'done')
        for _ in range(2):
            t += 100
            self.cli('check', 'tick', '--config', str(self.config), env_extra={'CUE_CHECK_NOW': str(t), 'MODE': 'fail'})
        self.assertEqual(self.query_one("SELECT fault_count FROM checks WHERE name='flaky'")['fault_count'], 2)

        _write_exec(self.repo / 'scripts/flaky_v2.sh', '#!/bin/sh\nexit 0\n')
        self.commit_config(
            '[[checks]]\nname = "flaky"\ninterval = 60\ncommand = "scripts/flaky_v2.sh"\ntimeout = 20\nskill = "ev-skill"\nfault_threshold = 3\n',
        )
        t += 10  # due でなくても mirror sync だけで command 変更によるリセットが効く
        self.cli('check', 'tick', '--config', str(self.config), env_extra={'CUE_CHECK_NOW': str(t)})
        self.assertEqual(self.query_one("SELECT fault_count FROM checks WHERE name='flaky'")['fault_count'], 0)

    # --- 10. wake (run-inbox 投入・回収・再試行・順序・20 秒上限・CB・開始時失敗スキップ) ---

    def test_wake_delivery_gc_retry_cb_order_and_budget(self):
        self.commit_config(
            '[[checks]]\nname = "w"\ninterval = 60\ncommand = "scripts/w.py"\ntimeout = 10\nskill = "wake-skill"\n',
            {
                'scripts/w.py': (
                    '#!/usr/bin/env python3\nimport json\n'
                    'for k in ("a", "b", "c"):\n'
                    '    print(json.dumps({"key": k, "title": f"UNTRUSTED TITLE {k}", "body": f"UNTRUSTED BODY {k}"}))\n'
                ),
            },
        )
        # 1 回目: 3 件起票、wake が全失敗 -> 2 件連続失敗で CB 発動し 3 件目は試行されない!
        r1 = self.cli('check', 'tick', '--config', str(self.config), env_extra={'CUE_CHECK_NOW': '1700000000', 'FAKE_SEND_RC': '1'})
        self.assertEqual(r1.returncode, 0)
        b1 = json.loads(r1.stdout)
        self.assertEqual(b1['wake_failed'], 2)
        self.assertEqual(b1['unnotified_tasks'], 3)
        self.assertIn('warning:', r1.stderr)
        # wake のメッセージに非信頼テキスト (title / body) が混ざっていないこと
        sent_lines = [json.loads(line) for line in self.wake_log.read_text(encoding='utf-8').splitlines()]
        self.assertEqual(len(sent_lines), 2)
        for entry in sent_lines:
            joined = ' '.join(entry['argv'])
            self.assertNotIn('UNTRUSTED', joined)
            self.assertIn('skill=wake-skill', joined)

        # 3 件目のタスクを claim 済み (pane 設定) にして回収されるか検証
        cts = self.query_all('SELECT task_id, dedup_key, last_wake_attempt_at FROM check_tasks ORDER BY dedup_key')
        unattempted = [c for c in cts if c['last_wake_attempt_at'] is None]
        self.assertEqual(len(unattempted), 1)
        claimed_id = unattempted[0]['task_id']
        Store(self.db).update_task(claimed_id, {'pane': 'w1:p2'})

        # 2 回目: 回収で claimed_id は notified_at=NULL のまま needs_wake=0 に落ち、残り 2 件が wake 成功
        self.wake_log.unlink()
        r2 = self.cli('check', 'tick', '--config', str(self.config), env_extra={'CUE_CHECK_NOW': '1700000100', 'FAKE_SEND_RC': '0'})
        self.assertEqual(r2.returncode, 0)
        b2 = json.loads(r2.stdout)
        self.assertEqual(b2['wake_failed'], 0)
        self.assertEqual(b2['unnotified_tasks'], 0)

        claimed_row = self.query_one('SELECT needs_wake, notified_at FROM check_tasks WHERE task_id=?', (claimed_id,))
        self.assertEqual(claimed_row['needs_wake'], 0)
        self.assertIsNone(claimed_row['notified_at'])
        sent2 = [json.loads(line) for line in self.wake_log.read_text(encoding='utf-8').splitlines()]
        self.assertEqual(len(sent2), 2)

    def test_wake_order_fault_priority_and_budget_truncation_and_no_end_retry(self):
        # DB を初期化
        self.commit_config(
            '[[checks]]\nname = "noop"\ninterval = 60\ncommand = "scripts/ok.sh"\ntimeout = 10\nskill = "s"\n',
            {'scripts/ok.sh': '#!/bin/sh\nexit 0\n'},
        )
        self.cli('check', 'tick', '--config', str(self.config), env_extra={'CUE_CHECK_NOW': '1700000000'})

        # 同時刻に event タスク (t_ev) と fault タスク (t_fl) を作成 -> fault が先に試行される
        ts = datetime.fromtimestamp(1700000010, timezone.utc).isoformat(timespec='microseconds')
        with contextlib.closing(sqlite3.connect(self.db)) as conn, conn:
            for tid, kind, skill in (('t_aaa_ev', 'event', 's'), ('t_zzz_fl', 'fault', CHECK_FAULT_SKILL)):
                conn.execute(
                    "INSERT INTO tasks(id, title, lane, created_at, updated_at, kind, done_when) VALUES(?, ?, 'todo', ?, ?, 'monitor', 'w')",
                    (tid, tid, ts, ts),
                )
                conn.execute(
                    "INSERT INTO check_tasks(task_id, check_name, kind, dedup_key, skill, needs_wake, created_at) VALUES(?, 'noop', ?, '', ?, 1, ?)",
                    (tid, kind, skill, ts),
                )

        # pass_budget=7s, item_timeout=5s: 1 件目 (fault) に 3 秒かかって残り 4s < 5s -> 2 件目は打ち切り (truncated=True)
        clock = FakeClock(wall=1700000100.0, mono=100.0)
        seen = []

        def slow_deliver(cand, _timeout):
            seen.append(cand['task_id'])
            clock.advance(3.0)
            return True

        failed, truncated, attempted = run_wake_pass(
            self.db,
            now_iso_fn=lambda: datetime.fromtimestamp(clock.now(), timezone.utc).isoformat(timespec='microseconds'),
            mono_fn=clock.monotonic,
            pass_budget=7.0,
            item_timeout=5.0,
            deliver_fn=slow_deliver,
        )
        self.assertEqual(seen, ['t_zzz_fl'])
        self.assertEqual((failed, truncated, attempted), (set(), True, 1))

        # 開始時 wake で失敗したタスク (t_aaa_ev) は終了時 wake で再試行しない (distinct wake_failed == 1)
        attempts = []

        def fail_ev(cand, _timeout):
            attempts.append(cand['task_id'])
            return False

        err_buf = io.StringIO()
        with contextlib.redirect_stderr(err_buf):
            res, rc = run_check_tick(
                self.db, self.config,
                now_fn=lambda: 1700000200.0,
                mono_fn=clock.monotonic,
                deliver_fn=fail_ev,
            )
        self.assertEqual(rc, 0)
        self.assertEqual(attempts, ['t_aaa_ev'])  # 開始時の 1 回だけ (終了時 wake では再試行しない)
        self.assertEqual(res['wake_failed'], 1)
        self.assertIn('warning:', err_buf.getvalue())

    def test_wake_order_coalesce_moves_retried_task_behind_newer_unattempted(self):
        # DB を初期化
        self.commit_config(
            '[[checks]]\nname = "noop"\ninterval = 60\ncommand = "scripts/ok.sh"\ntimeout = 10\nskill = "s"\n',
            {'scripts/ok.sh': '#!/bin/sh\nexit 0\n'},
        )
        run_check_tick(self.db, self.config, now_fn=lambda: 1700000000.0, deliver_fn=lambda c, t: True)

        iso = lambda ts: datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec='microseconds')
        # created_at は t_retried_fault (T+5) < t_retried_ev (T+10) < t_fresh_newer (T+20) < t_newest (T+40) だが、
        # 再試行済み (last_wake_attempt_at = T+30) の 2 件は COALESCE(last_wake_attempt_at, created_at) に従い
        # より新しい未試行タスク t_fresh_newer (T+20) より後ろ、t_newest (T+40) より前に並ぶ。
        # created_at のみで並べる誤実装や NULLS FIRST/LAST の誤実装では一致しない入力。
        rows = [
            ('t_retried_fault', 'fault', CHECK_FAULT_SKILL, iso(1700000005), iso(1700000030)),
            ('t_retried_ev', 'event', 's', iso(1700000010), iso(1700000030)),
            ('t_fresh_newer', 'event', 's', iso(1700000020), None),
            ('t_newest', 'event', 's', iso(1700000040), None),
        ]
        with contextlib.closing(sqlite3.connect(self.db)) as conn, conn:
            for tid, kind, skill, created_iso, attempt_iso in rows:
                conn.execute(
                    "INSERT INTO tasks(id, title, lane, created_at, updated_at, kind, done_when) VALUES(?, ?, 'todo', ?, ?, 'monitor', 'w')",
                    (tid, tid, created_iso, created_iso),
                )
                conn.execute(
                    "INSERT INTO check_tasks(task_id, check_name, kind, dedup_key, skill, needs_wake, last_wake_attempt_at, created_at) "
                    "VALUES(?, 'noop', ?, '', ?, 1, ?, ?)",
                    (tid, kind, skill, attempt_iso, created_iso),
                )

        clock = FakeClock(wall=1700000100.0, mono=100.0)
        order = []

        def record_deliver(cand, _timeout):
            order.append(cand['task_id'])
            return True

        failed, truncated, attempted = run_wake_pass(
            self.db,
            now_iso_fn=lambda: iso(clock.now()),
            mono_fn=clock.monotonic,
            deliver_fn=record_deliver,
        )
        self.assertEqual((failed, truncated, attempted), (set(), False, 4))
        self.assertEqual(order, ['t_fresh_newer', 't_retried_fault', 't_retried_ev', 't_newest'])

    # --- 11. 30 日 prune と CLI list / status 表示 ---

    def test_prune_30_days_and_cli_list_and_status_metrics(self):
        self.commit_config(
            '[[checks]]\nname = "active_chk"\ninterval = 300\ncommand = "scripts/ok.sh"\ntimeout = 10\nskill = "s1"\n'
            '[[checks]]\nname = "disabled_chk"\ninterval = 60\ncommand = "scripts/ok.sh"\ntimeout = 10\nskill = "s1"\nenabled = false\n'
            '[[checks]]\nname = "nohist_chk"\ninterval = 60\ncommand = "scripts/ok.sh"\ntimeout = 10\nskill = "s1"\n',
            {'scripts/ok.sh': '#!/bin/sh\nexit 0\n'},
        )
        now = 1700000000
        self.cli('check', 'tick', '--config', str(self.config), env_extra={'CUE_CHECK_NOW': str(now)})

        # 31 日前の古い run / event / tick と、12 時間前の deferred を手動挿入
        old_iso = datetime.fromtimestamp(now - 31 * 86400, timezone.utc).isoformat(timespec='microseconds')
        recent_iso = datetime.fromtimestamp(now - 3600, timezone.utc).isoformat(timespec='microseconds')
        with contextlib.closing(sqlite3.connect(self.db)) as conn, conn:
            cur = conn.execute(
                "INSERT INTO check_ticks(started_at, finished_at, status, wake_failed, unnotified_tasks, checks_deferred) VALUES(?, ?, 'ok', 0, 0, 2)",
                (old_iso, old_iso),
            )
            old_tick_id = cur.lastrowid
            cur = conn.execute(
                "INSERT INTO check_runs(tick_id, check_name, started_at, finished_at, exit_code, outcome, events_deferred) VALUES(?, 'active_chk', ?, ?, 0, 'fired', 4)",
                (old_tick_id, old_iso, old_iso),
            )
            old_run_id = cur.lastrowid
            conn.execute(
                "INSERT INTO check_run_events(run_id, dedup_key, outcome, task_id) VALUES(?, 'oldk', 'fired', 't_old')",
                (old_run_id,),
            )
            # 直近 24 時間内の deferred 記録
            conn.execute(
                "INSERT INTO check_ticks(started_at, finished_at, status, wake_failed, unnotified_tasks, checks_deferred) VALUES(?, ?, 'ok', 0, 0, 3)",
                (recent_iso, recent_iso),
            )
            conn.execute(
                "INSERT INTO check_runs(tick_id, check_name, started_at, finished_at, exit_code, outcome, events_deferred) VALUES(1, 'active_chk', ?, ?, 0, 'fired', 7)",
                (recent_iso, recent_iso),
            )
            # nohist_chk の履歴を消して「履歴なしは最大遅延の対象外」を検証、disabled_chk に古い履歴を入れて対象外を検証
            conn.execute("UPDATE checks SET last_started=NULL, last_actual_started_at=NULL, last_outcome=NULL WHERE name='nohist_chk'")
            conn.execute(
                "UPDATE checks SET last_started=?, last_actual_started_at=?, last_outcome='ok_silent' WHERE name='disabled_chk'",
                (old_iso, old_iso),
            )

        # 次 tick (now + 100) で 31 日前の履歴が prune される
        res = self.cli('check', 'tick', '--config', str(self.config), env_extra={'CUE_CHECK_NOW': str(now + 100)})
        self.assertEqual(res.returncode, 0)
        self.assertIsNone(self.query_one('SELECT id FROM check_runs WHERE id=?', (old_run_id,)))
        self.assertEqual(self.query_all('SELECT * FROM check_run_events WHERE run_id=?', (old_run_id,)), [])
        self.assertIsNone(self.query_one('SELECT id FROM check_ticks WHERE id=?', (old_tick_id,)))

        # prune tick 後に nohist_chk の履歴を消して「履歴なしは最大遅延の対象外」を検証
        with contextlib.closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE checks SET last_started=NULL, last_actual_started_at=NULL, last_outcome=NULL WHERE name='nohist_chk'")

        # check list / check status の確認 (now + 500 時点で active_chk の遅延は (500 - 300) = 200s)
        lst = json.loads(self.cli('check', 'list', '--json').stdout)
        self.assertEqual({c['name'] for c in lst['checks']}, {'active_chk', 'disabled_chk', 'nohist_chk'})
        lst_text = self.cli('check', 'list').stdout
        self.assertIn('active_chk', lst_text)

        st = json.loads(self.cli('check', 'status', '--json', env_extra={'CUE_CHECK_NOW': str(now + 500)}).stdout)
        self.assertIsNotNone(st['last_ok_tick_at'])
        self.assertEqual(st['checks_deferred_24h'], {'total': 3, 'ticks': 1})
        self.assertEqual(st['events_deferred_24h'], 7)
        self.assertEqual(st['max_overdue_check'], 'active_chk')
        self.assertAlmostEqual(st['max_overdue_sec'], 200.0, delta=2.0)

        st_text = self.cli('check', 'status', env_extra={'CUE_CHECK_NOW': str(now + 500)}).stdout
        self.assertIn('last_ok_tick_at:', st_text)
        self.assertIn('active_chk', st_text)

    def test_mirror_sync_disables_removed_and_disabled_checks_preserving_history(self):
        self.commit_config(
            '[[checks]]\nname = "keep"\ninterval = 60\ncommand = "scripts/fail.sh"\ntimeout = 10\nskill = "s"\nfault_threshold = 5\n'
            '[[checks]]\nname = "drop"\ninterval = 60\ncommand = "scripts/fail.sh"\ntimeout = 10\nskill = "s"\nfault_threshold = 5\n',
            {'scripts/fail.sh': '#!/bin/sh\nexit 1\n'},
        )
        self.cli('check', 'tick', '--config', str(self.config), env_extra={'CUE_CHECK_NOW': '1700000000'})
        self.assertEqual(self.query_one("SELECT fault_count FROM checks WHERE name='keep'")['fault_count'], 1)
        self.assertEqual(self.query_one("SELECT fault_count FROM checks WHERE name='drop'")['fault_count'], 1)

        # keep は無効化、drop は TOML から削除 -> 両方 enabled=0, fault_count=0、履歴は残る
        self.commit_config(
            '[[checks]]\nname = "keep"\ninterval = 60\ncommand = "scripts/fail.sh"\ntimeout = 10\nskill = "s"\nfault_threshold = 5\nenabled = false\n',
        )
        self.cli('check', 'tick', '--config', str(self.config), env_extra={'CUE_CHECK_NOW': '1700000100'})
        for name in ('keep', 'drop'):
            row = self.query_one('SELECT enabled, fault_count FROM checks WHERE name=?', (name,))
            self.assertEqual((row['enabled'], row['fault_count']), (0, 0))
        self.assertEqual(len(self.query_all('SELECT * FROM check_runs')), 2)

    def test_command_cwd_env_and_4k_multibyte_excerpt_limit(self):
        self.commit_config(
            '[[checks]]\nname = "envcwd"\ninterval = 60\ncommand = "scripts/envcwd.py"\ntimeout = 10\nskill = "s"\n'
            '[checks.env]\nCUSTOM_VAR = "hello-env"\n',
            {
                'scripts/envcwd.py': (
                    '#!/usr/bin/env python3\nimport json, os, sys\n'
                    'assert os.path.realpath(os.getcwd()) == os.path.realpath(os.path.dirname(os.path.dirname(__file__)))\n'
                    'assert os.environ.get("CUSTOM_VAR") == "hello-env"\n'
                    'sys.stderr.write("あ" * 3000)\n'
                    'print(json.dumps({"key": "k1", "title": "ok", "body": "い" * 2000}))\n'
                ),
            },
        )
        res = self.cli('check', 'tick', '--config', str(self.config), env_extra={'CUE_CHECK_NOW': '1700000000'})
        self.assertEqual(res.returncode, 0, res.stderr)
        run = self.query_one('SELECT outcome, stdout_excerpt, stderr_excerpt FROM check_runs')
        self.assertEqual(run['outcome'], 'fired')
        self.assertLessEqual(len(run['stdout_excerpt'].encode('utf-8')), 4096)
        self.assertLessEqual(len(run['stderr_excerpt'].encode('utf-8')), 4096)
        # tasks.request は TASK_LIMITS['request'] (1000 文字) 以内に収まり reconcile-lane 等と互換
        task = self.query_one('SELECT request FROM tasks')
        self.assertLessEqual(len(task['request']), 1000)

    def test_git_timeout_triggers_config_error(self):
        self.commit_config(
            '[[checks]]\nname = "c1"\ninterval = 60\ncommand = "scripts/ok.sh"\ntimeout = 10\nskill = "s"\n',
            {'scripts/ok.sh': '#!/bin/sh\nexit 0\n'},
        )
        # mono_fn が呼ばれるたびに 3 秒進むようにして git 合計 5 秒上限を超過させる
        mono = {'t': 0.0}

        def fast_advancing_mono():
            mono['t'] += 3.0
            return mono['t']

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            res, rc = run_check_tick(
                self.db, self.config,
                now_fn=lambda: 1700000000.0,
                mono_fn=fast_advancing_mono,
                deliver_fn=lambda c, t: True,
            )
        self.assertEqual(rc, 1)
        self.assertEqual(res['status'], 'config_error')
        self.assertIn('5 秒', res['error'])

    def test_fault_threshold_int64_bounds_rejected_as_config_error(self):
        self.commit_config(
            '[[checks]]\nname = "overflow"\ninterval = 60\ncommand = "scripts/ok.sh"\ntimeout = 10\nskill = "s1"\n'
            'fault_threshold = 9223372036854775808\n',
            {'scripts/ok.sh': '#!/bin/sh\nexit 0\n'},
        )
        res = self.cli('check', 'tick', '--config', str(self.config), env_extra={'CUE_CHECK_NOW': '1700000000'})
        self.assertEqual(res.returncode, 1)
        body = json.loads(res.stdout)
        self.assertEqual(body['status'], 'config_error')
        self.assertEqual(self.query_all("SELECT * FROM check_ticks WHERE status='running'"), [])
        self.assertEqual(self.query_all('SELECT * FROM checks'), [])

        # signed 64-bit 最大値ちょうどは受理されて保存できる
        self.commit_config(
            '[[checks]]\nname = "max_i64"\ninterval = 60\ncommand = "scripts/ok.sh"\ntimeout = 10\nskill = "s1"\n'
            'fault_threshold = 9223372036854775807\n',
        )
        ok_res = self.cli('check', 'tick', '--config', str(self.config), env_extra={'CUE_CHECK_NOW': '1700000100'})
        self.assertEqual(ok_res.returncode, 0, ok_res.stderr)
        self.assertEqual(json.loads(ok_res.stdout)['status'], 'ok')
        row = self.query_one("SELECT fault_threshold FROM checks WHERE name='max_i64'")
        self.assertEqual(row['fault_threshold'], 9223372036854775807)

    def test_grace_seconds_floors_to_whole_seconds_matching_lean_model(self):
        self.assertEqual(check_grace_seconds(3), 0.0)
        self.assertEqual(check_grace_seconds(65), 6.0)
        self.assertEqual(check_grace_seconds(299), 29.0)
        self.assertEqual(check_grace_seconds(300), 30.0)
        self.assertEqual(check_grace_seconds(600), 30.0)

        self.commit_config(
            '[[checks]]\nname = "g65"\ninterval = 65\ncommand = "scripts/ok.sh"\ntimeout = 10\nskill = "s1"\n',
            {'scripts/ok.sh': '#!/bin/sh\nexit 0\n'},
        )
        clock = FakeClock(wall=1000.0, mono=100.0)
        res, rc = run_check_tick(self.db, self.config, now_fn=clock.now, mono_fn=clock.monotonic, deliver_fn=lambda c, t: True)
        self.assertEqual((rc, res['status']), (0, 'ok'))
        self.assertEqual(len(self.query_all('SELECT * FROM check_runs')), 1)

        # grace = floor(65 / 10) = 6 -> cutoff = 1000 + 65 - 6 = 1059
        # 旧実装 (grace = 6.5 -> cutoff = 1058.5) では due になっていた 1058.5 は due でないこと
        for ts in (1058.0, 1058.5, 1058.9):
            clock.wall = ts
            run_check_tick(self.db, self.config, now_fn=clock.now, mono_fn=clock.monotonic, deliver_fn=lambda c, t: True)
            self.assertEqual(len(self.query_all('SELECT * FROM check_runs')), 1, f'ts={ts} should not be due')

        # 1059.0 で due
        clock.wall = 1059.0
        run_check_tick(self.db, self.config, now_fn=clock.now, mono_fn=clock.monotonic, deliver_fn=lambda c, t: True)
        self.assertEqual(len(self.query_all('SELECT * FROM check_runs')), 2)

        # check_due_info 直接確認 (interval=3, grace=0 -> now=2 は False, now=3 は True)
        iso0 = datetime.fromtimestamp(0, timezone.utc).isoformat(timespec='microseconds')
        row3 = {'interval': 3.0, 'last_started': iso0, 'last_actual_started_at': iso0, 'last_outcome': 'ok_silent'}
        self.assertFalse(check_due_info(row3, 2.0)[0])
        self.assertTrue(check_due_info(row3, 3.0)[0])

    def test_command_string_and_array_forms_have_distinct_identity_in_mirror_sync(self):
        # literal なファイル名 `["probe"]` と `probe` の両方を用意する
        self.commit_config(
            '[[checks]]\nname = "ambig"\ninterval = 60\ncommand = \'["probe"]\'\ntimeout = 10\nskill = "s1"\nfault_threshold = 3\n',
            {
                '["probe"]': '#!/bin/sh\nexit 1\n',
                'probe': '#!/bin/sh\nexit 1\n',
            },
        )
        t = 1700000000
        res1 = self.cli('check', 'tick', '--config', str(self.config), env_extra={'CUE_CHECK_NOW': str(t)})
        self.assertEqual(res1.returncode, 0, res1.stderr)
        self.assertEqual(self.query_one("SELECT fault_count FROM checks WHERE name='ambig'")['fault_count'], 1)

        # 同じ文字列形 command のまま (timeout だけ変更) なら fault_count を保持して 2 になる
        self.commit_config(
            '[[checks]]\nname = "ambig"\ninterval = 60\ncommand = \'["probe"]\'\ntimeout = 20\nskill = "s1"\nfault_threshold = 3\n',
        )
        t += 100
        res2 = self.cli('check', 'tick', '--config', str(self.config), env_extra={'CUE_CHECK_NOW': str(t)})
        self.assertEqual(res2.returncode, 0, res2.stderr)
        self.assertEqual(self.query_one("SELECT fault_count FROM checks WHERE name='ambig'")['fault_count'], 2)

        # 配列形 command = ["probe"] (別ファイル `probe`) に切り替えると identity が変わり fault_count が 0 にリセットされる
        self.commit_config(
            '[[checks]]\nname = "ambig"\ninterval = 60\ncommand = ["probe"]\ntimeout = 20\nskill = "s1"\nfault_threshold = 3\n',
        )
        t += 10  # due 前なので mirror sync のみ走る
        res3 = self.cli('check', 'tick', '--config', str(self.config), env_extra={'CUE_CHECK_NOW': str(t)})
        self.assertEqual(res3.returncode, 0, res3.stderr)
        self.assertEqual(self.query_one("SELECT fault_count FROM checks WHERE name='ambig'")['fault_count'], 0)

        # 配列形のまま実行すると 1 になり、故障タスクは起票されない (旧 fault_count=2 を引き継いでいない)
        t += 100
        res4 = self.cli('check', 'tick', '--config', str(self.config), env_extra={'CUE_CHECK_NOW': str(t)})
        self.assertEqual(res4.returncode, 0, res4.stderr)
        self.assertEqual(self.query_one("SELECT fault_count FROM checks WHERE name='ambig'")['fault_count'], 1)
        self.assertEqual(self.query_all("SELECT * FROM check_tasks WHERE kind='fault'"), [])

    def test_helper_holding_stdout_open_is_not_recorded_as_ok_silent(self):
        helper_pid_file = self.root / 'stdout_holder.pid'
        self.addCleanup(_cleanup_pid_file, helper_pid_file)
        script = (
            '#!/usr/bin/env python3\n'
            'import json, subprocess, sys\n'
            'print(json.dumps({"key": "k1", "title": "important event", "body": "do not drop"}), flush=True)\n'
            'helper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])\n'
            f'open({str(helper_pid_file)!r}, "w", encoding="utf-8").write(str(helper.pid))\n'
            'sys.exit(0)\n'
        )
        # (a) command timeout が支配的 -> failed_timeout として記録され ok_silent にならない
        self.commit_config(
            '[[checks]]\nname = "holder_to"\ninterval = 60\ncommand = "scripts/holder.py"\ntimeout = 0.4\nskill = "s1"\n',
            {'scripts/holder.py': script},
        )
        body, rc = run_check_tick(
            self.db, self.config,
            now_fn=lambda: 1700000000.0,
            kill_grace=0.2,
            deliver_fn=lambda c, t: True,
        )
        self.assertEqual((rc, body['status']), (0, 'ok'))
        run = self.query_one("SELECT outcome, exit_code FROM check_runs WHERE check_name='holder_to'")
        self.assertEqual(run['outcome'], 'failed_timeout')
        self.assertIsNone(run['exit_code'])
        self.assertEqual(self.query_all('SELECT * FROM tasks'), [])
        chk_row = self.query_one("SELECT last_outcome, fault_count FROM checks WHERE name='holder_to'")
        self.assertEqual((chk_row['last_outcome'], chk_row['fault_count']), ('failed_timeout', 1))
        helper_pid = int(helper_pid_file.read_text(encoding='utf-8').strip())
        deadline = time.monotonic() + 2.0
        while _pid_alive(helper_pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertFalse(_pid_alive(helper_pid))

        # (b) exec_deadline が支配的 -> cleanup (kill_grace + kill_wait + drain = 1.7s) を
        # commit_start_deadline (3.8s) 内に収めた上で commit_reserve (0.5s) 内で aborted として記録され、
        # tick は status='aborted' になり checks 状態は進めない
        helper_pid_file.unlink()
        self.commit_config(
            '[[checks]]\nname = "holder_ab"\ninterval = 60\ncommand = "scripts/holder.py"\ntimeout = 10\nskill = "s1"\n',
        )
        real_origin = [None]

        def mono_fn():
            if real_origin[0] is None:
                return 0.0
            return 1.6 + (time.monotonic() - real_origin[0])

        def start_on_check(*args, **kwargs):
            if real_origin[0] is None:
                real_origin[0] = time.monotonic()
            return run_check_command(*args, **kwargs)

        with mock.patch('app.run_check_command', side_effect=start_on_check):
            body2, rc2 = run_check_tick(
                self.db, self.config,
                now_fn=lambda: 1700000200.0,
                mono_fn=mono_fn,
                tick_budget=4.5,
                finalize_reserve=0.2,
                commit_reserve=0.5,
                kill_grace=0.2,
                deliver_fn=lambda c, t: True,
            )
        self.assertEqual((rc2, body2['status']), (0, 'aborted'))
        run2 = self.query_one("SELECT outcome, exit_code FROM check_runs WHERE check_name='holder_ab'")
        self.assertIsNotNone(run2)
        self.assertEqual((run2['outcome'], run2['exit_code']), ('aborted', None))
        chk2 = self.query_one("SELECT last_started, last_outcome, fault_count FROM checks WHERE name='holder_ab'")
        self.assertEqual((chk2['last_started'], chk2['last_outcome'], chk2['fault_count']), (None, None, 0))
        helper_pid2 = int(helper_pid_file.read_text(encoding='utf-8').strip())
        deadline2 = time.monotonic() + 2.0
        while _pid_alive(helper_pid2) and time.monotonic() < deadline2:
            time.sleep(0.02)
        self.assertFalse(_pid_alive(helper_pid2))

    def test_keyboard_interrupt_terminates_subprocess_groups_before_lock_release(self):
        # (a) run_check_command / run_check_tick 中の KeyboardInterrupt で子グループを止めてから lock 解放
        child_pid_file = self.root / 'int_check_child.pid'
        self.addCleanup(_cleanup_pid_file, child_pid_file)
        script = (
            '#!/usr/bin/env python3\n'
            'import subprocess, sys, time\n'
            'child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])\n'
            f'open({str(child_pid_file)!r}, "w", encoding="utf-8").write(str(child.pid))\n'
            'time.sleep(60)\n'
        )
        self.commit_config(
            '[[checks]]\nname = "int_chk"\ninterval = 60\ncommand = "scripts/int.py"\ntimeout = 30\nskill = "s1"\n',
            {'scripts/int.py': script},
        )
        orig_wait = subprocess.Popen.wait
        orig_terminate = sys.modules['app']._terminate_process_group
        lock_file = check_lock_path(self.db)
        lock_held_at_interrupt = []
        lock_held_after_cleanup = []
        child_stopped_at_cleanup = []
        interrupted = [False]

        def _check_lock_held(path):
            fd = os.open(path, os.O_RDWR)
            try:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return False
                except OSError:
                    return True
            finally:
                os.close(fd)

        def interrupting_wait(proc_self, *args, **kwargs):
            # 子が起動して PID を書くまで待ってから Ctrl-C (KeyboardInterrupt) を 1 回だけ発生させる
            if not interrupted[0] and proc_self.args and any('int.py' in str(a) for a in proc_self.args):
                interrupted[0] = True
                deadline = time.monotonic() + 5.0
                while not child_pid_file.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                # 割り込み発生時点では tick lock が保持されていること
                lock_held_at_interrupt.append(_check_lock_held(lock_file))
                raise KeyboardInterrupt()
            return orig_wait(proc_self, *args, **kwargs)

        def spy_terminate_a(proc, *args, **kwargs):
            try:
                return orig_terminate(proc, *args, **kwargs)
            finally:
                # cleanup 完了直後 (tick へ制御が戻る前) に子孫が停止済みかつ tick lock がまだ保持されていること
                c_pid = int(child_pid_file.read_text(encoding='utf-8').strip())
                child_stopped_at_cleanup.append(_pid_stopped(c_pid))
                lock_held_after_cleanup.append(_check_lock_held(lock_file))

        with mock.patch.object(subprocess.Popen, 'wait', interrupting_wait), \
                mock.patch('app._terminate_process_group', side_effect=spy_terminate_a):
            with self.assertRaises(KeyboardInterrupt):
                run_check_tick(
                    self.db, self.config,
                    now_fn=lambda: 1700000000.0,
                    kill_grace=0.2,
                    deliver_fn=lambda c, t: True,
                )
        self.assertEqual(lock_held_at_interrupt, [True])
        self.assertEqual(child_stopped_at_cleanup, [True])
        self.assertEqual(lock_held_after_cleanup, [True])
        child_pid = int(child_pid_file.read_text(encoding='utf-8').strip())
        self.assertFalse(_pid_alive(child_pid))
        # tick 終了後に lock が解放されていること
        fd = os.open(lock_file, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)

        # (b) _run_subprocess_bounded (git / wake 用) でも KeyboardInterrupt で子グループを止めて再送出
        bounded_pid_file = self.root / 'int_bounded_child.pid'
        self.addCleanup(_cleanup_pid_file, bounded_pid_file)
        bounded_script = self.root / 'bounded_sleep.py'
        _write_exec(
            bounded_script,
            '#!/usr/bin/env python3\n'
            'import subprocess, sys, time\n'
            'child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])\n'
            f'open({str(bounded_pid_file)!r}, "w", encoding="utf-8").write(str(child.pid))\n'
            'time.sleep(60)\n',
        )
        orig_communicate = subprocess.Popen.communicate

        def interrupting_communicate(proc_self, *args, **kwargs):
            if proc_self.args and any('bounded_sleep.py' in str(a) for a in proc_self.args):
                deadline = time.monotonic() + 5.0
                while not bounded_pid_file.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                raise KeyboardInterrupt()
            return orig_communicate(proc_self, *args, **kwargs)

        with mock.patch.object(subprocess.Popen, 'communicate', interrupting_communicate):
            with self.assertRaises(KeyboardInterrupt):
                _run_subprocess_bounded([str(bounded_script)], timeout=30.0, kill_grace=0.2)
        b_pid = int(bounded_pid_file.read_text(encoding='utf-8').strip())
        deadline = time.monotonic() + 2.0
        while _pid_alive(b_pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertFalse(_pid_alive(b_pid))

        # (c) Popen 直後の drain thread 起動タイミング (1 本目 / 2 本目) での KeyboardInterrupt でも子グループが停止されること
        import threading as _threading
        orig_thread_start = _threading.Thread.start
        for interrupt_on_call in (1, 2):
            drain_pid_file = self.root / f'int_drain_child_{interrupt_on_call}.pid'
            self.addCleanup(_cleanup_pid_file, drain_pid_file)
            drain_script = (
                '#!/usr/bin/env python3\n'
                'import subprocess, sys, time\n'
                'child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])\n'
                f'open({str(drain_pid_file)!r}, "w", encoding="utf-8").write(str(child.pid))\n'
                'time.sleep(60)\n'
            )
            self.commit_config(
                '[[checks]]\nname = "int_drain"\ninterval = 60\ncommand = "scripts/int_drain.py"\ntimeout = 30\nskill = "s1"\n',
                {'scripts/int_drain.py': drain_script},
            )
            drain_calls = [0]
            drain_lock_held = []
            drain_lock_held_after_cleanup = []
            drain_child_stopped_at_cleanup = []

            def interrupting_thread_start(thread_self, *args, **kwargs):
                if getattr(thread_self, '_target', None) is sys.modules['app']._drain_pipe:
                    drain_calls[0] += 1
                    if drain_calls[0] == interrupt_on_call:
                        dl = time.monotonic() + 5.0
                        while not drain_pid_file.exists() and time.monotonic() < dl:
                            time.sleep(0.02)
                        drain_lock_held.append(_check_lock_held(lock_file))
                        raise KeyboardInterrupt()
                return orig_thread_start(thread_self, *args, **kwargs)

            def spy_terminate_c(proc, *args, **kwargs):
                try:
                    return orig_terminate(proc, *args, **kwargs)
                finally:
                    d_pid_now = int(drain_pid_file.read_text(encoding='utf-8').strip())
                    drain_child_stopped_at_cleanup.append(_pid_stopped(d_pid_now))
                    drain_lock_held_after_cleanup.append(_check_lock_held(lock_file))

            with mock.patch.object(_threading.Thread, 'start', interrupting_thread_start), \
                    mock.patch('app._terminate_process_group', side_effect=spy_terminate_c):
                with self.assertRaises(KeyboardInterrupt):
                    run_check_tick(
                        self.db, self.config,
                        now_fn=lambda: 1700001000.0 + interrupt_on_call * 100.0,
                        kill_grace=0.2,
                        deliver_fn=lambda c, t: True,
                    )
            self.assertEqual(drain_lock_held, [True], f'lock must be held on drain thread #{interrupt_on_call} interrupt')
            self.assertEqual(drain_child_stopped_at_cleanup, [True], f'grandchild must be stopped before cleanup returns on drain thread #{interrupt_on_call}')
            self.assertEqual(drain_lock_held_after_cleanup, [True], f'lock must remain held immediately after cleanup on drain thread #{interrupt_on_call}')
            d_pid = int(drain_pid_file.read_text(encoding='utf-8').strip())
            self.assertFalse(_pid_alive(d_pid), f'grandchild survived drain thread #{interrupt_on_call} interrupt')
            fd = os.open(lock_file, os.O_RDWR)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(fd)

    def test_normal_exit_observed_after_wait_until_classified_as_deadline_miss(self):
        script_path = self.repo / 'scripts/fast_emit.py'
        _write_exec(
            script_path,
            '#!/usr/bin/env python3\n'
            'import json\n'
            'print(json.dumps({"key": "k1", "title": "late event", "body": "b"}), flush=True)\n',
        )

        # wait_budget == 0.0 で process.wait() が既に終了した子を TimeoutExpired なしで回収する状況を再現
        def run_case(timeout_sec, tick_deadline_offset, *, observed_offset=None):
            calls = {'n': 0}
            obs = (min(timeout_sec, tick_deadline_offset) + 0.5) if observed_offset is None else observed_offset
            launched = []
            orig_popen = subprocess.Popen
            orig_wait = subprocess.Popen.wait

            def capture_popen(*args, **kwargs):
                proc = orig_popen(*args, **kwargs)
                launched.append(proc)
                return proc

            def mono_fn():
                calls['n'] += 1
                if calls['n'] == 1:
                    return 1000.0  # cmd_mono_start
                if calls['n'] == 2:
                    # Popen 後・process.wait() 直前: 子プロセスの終了を直接待ってから wait_until を跨ぐ
                    self.assertEqual(len(launched), 1)
                    self.assertEqual(orig_wait(launched[0], timeout=5.0), 0)
                return 1000.0 + obs

            wait_raised_timeout = []
            wait_zero_returns = []

            def spy_wait(proc_self, *args, **kwargs):
                timeout_arg = kwargs.get('timeout', args[0] if args else None)
                try:
                    rc = orig_wait(proc_self, *args, **kwargs)
                except subprocess.TimeoutExpired:
                    wait_raised_timeout.append(True)
                    raise
                if timeout_arg == 0.0:
                    wait_zero_returns.append(rc)
                return rc

            chk = {'name': 'late', 'argv': [str(script_path)], 'timeout': timeout_sec, 'env': {}, 'clock_anomaly': False, 'skill': 's1', 'fault_threshold': 3}
            with mock.patch.object(subprocess, 'Popen', capture_popen), \
                    mock.patch.object(orig_popen, 'wait', spy_wait):
                res = run_check_command(chk, self.repo, 1000.0 + tick_deadline_offset, mono_fn=mono_fn, kill_grace=0.1)
            self.assertEqual(wait_raised_timeout, [], 'test must exercise normal return from process.wait(timeout=0)')
            self.assertEqual(wait_zero_returns, [0], 'runner must invoke process.wait(timeout=0) and observe normal exit')
            return res

        # (a) tick deadline が支配的 (timeout=10, tick_deadline=+5) -> aborted & exit_code=None
        ec_a, _out_a, _err_a, to_a, ab_a = run_case(10.0, 5.0)
        self.assertIsNone(ec_a)
        self.assertEqual((to_a, ab_a), (False, True))

        # (b) command timeout が支配的 (timeout=5, tick_deadline=+10) -> failed_timeout & exit_code=None
        ec_b, _out_b, _err_b, to_b, ab_b = run_case(5.0, 10.0)
        self.assertIsNone(ec_b)
        self.assertEqual((to_b, ab_b), (True, False))

        # (c) 親の観測が相手側 deadline を越えるほど遅れても、先に到来した (支配的な) deadline で分類する
        # tick_deadline=+5, timeout=+10 だが観測が +11 まで遅れた場合 -> 支配的なのは tick_deadline なので aborted
        ec_c, _out_c, _err_c, to_c, ab_c = run_case(10.0, 5.0, observed_offset=11.0)
        self.assertIsNone(ec_c)
        self.assertEqual((to_c, ab_c), (False, True))

        # (d) timeout=+5, tick_deadline=+10 で観測が +11 まで遅れた場合 -> 支配的なのは command timeout なので failed_timeout
        ec_d, _out_d, _err_d, to_d, ab_d = run_case(5.0, 10.0, observed_offset=11.0)
        self.assertIsNone(ec_d)
        self.assertEqual((to_d, ab_d), (True, False))

        # (e) 両 deadline が同時 (timeout=+5, tick_deadline=+5) で観測が +11 でも優先順位に従い failed_timeout
        ec_e, _out_e, _err_e, to_e, ab_e = run_case(5.0, 5.0, observed_offset=11.0)
        self.assertIsNone(ec_e)
        self.assertEqual((to_e, ab_e), (True, False))

        # (f) TimeoutExpired 経路でも tick_deadline < timeout なら観測が timeout 超過後でも aborted
        sleep_script = self.repo / 'scripts/sleep_slow.py'
        _write_exec(sleep_script, '#!/usr/bin/env python3\nimport time\ntime.sleep(30)\n')
        te_calls = {'n': 0}

        def late_timeout_mono():
            te_calls['n'] += 1
            if te_calls['n'] == 1:
                return 1000.0  # cmd_mono_start
            if te_calls['n'] == 2:
                return 1004.95  # wait_budget = 0.05s (< tick_deadline=1005.0)
            return 1015.0  # 相手側 command timeout (1010.0) を大きく超過した観測時刻

        chk_slow = {'name': 'slow', 'argv': [str(sleep_script)], 'timeout': 10.0, 'env': {}, 'clock_anomaly': False, 'skill': 's1', 'fault_threshold': 3}
        ec_f, _out_f, _err_f, to_f, ab_f = run_check_command(chk_slow, self.repo, 1005.0, mono_fn=late_timeout_mono, kill_grace=0.1)
        self.assertIsNone(ec_f)
        self.assertEqual((to_f, ab_f), (False, True))

    def test_end_wake_and_pruning_charged_against_tick_deadline_with_finalize_reserve(self):
        self.commit_config(
            '[[checks]]\nname = "ev"\ninterval = 60\ncommand = "scripts/emit.py"\ntimeout = 10\nskill = "s1"\n',
            {
                'scripts/emit.py': (
                    '#!/usr/bin/env python3\nimport json\n'
                    'print(json.dumps({"key": "k1", "title": "t1", "body": "b1"}))\n'
                    'print(json.dumps({"key": "k2", "title": "t2", "body": "b2"}))\n'
                ),
            },
        )
        # 初期 DB を作って 31 日前の古い履歴を仕込む
        base_now = 1700000000.0
        old_iso = datetime.fromtimestamp(base_now - 31 * 86400, timezone.utc).isoformat(timespec='microseconds')
        run_check_tick(self.db, self.config, now_fn=lambda: base_now - 1000.0, deliver_fn=lambda c, t: True)
        with contextlib.closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('DELETE FROM check_tasks')
            conn.execute('DELETE FROM tasks')
            conn.execute('DELETE FROM check_runs')
            conn.execute('DELETE FROM check_ticks')
            conn.execute("UPDATE checks SET last_started=NULL, last_actual_started_at=NULL, last_outcome=NULL")
            cur = conn.execute(
                "INSERT INTO check_ticks(started_at, finished_at, status, wake_failed, unnotified_tasks, checks_deferred) VALUES(?, ?, 'ok', 0, 0, 0)",
                (old_iso, old_iso),
            )
            old_tick_id = cur.lastrowid
            cur = conn.execute(
                "INSERT INTO check_runs(tick_id, check_name, started_at, finished_at, exit_code, outcome) VALUES(?, 'ev', ?, ?, 0, 'ok_silent')",
                (old_tick_id, old_iso, old_iso),
            )
            old_run_id = cur.lastrowid

        # (a) 最後の check が tick 開始 237 秒で完了し、commit が 239 秒までかかった
        # (budget=240s, reserve=2s -> end_work_deadline=238s)。
        # end wake も pruning も新規の 20s 予算を得ずにスキップされ、最終 status 記録だけ完了すること
        clock = FakeClock(wall=base_now, mono=1000.0)
        delivered = []

        def slow_check_advancing_to_237(*args, **kwargs):
            res = run_check_command(*args, **kwargs)
            # commit は retry deadline (238s) より前に開始させる
            clock.advance(237.0)
            return res

        def commit_advancing_to_239(*args, **kwargs):
            res = commit_check_run(*args, **kwargs)
            # commit 自体は成功したが、完了が end_work_deadline を過ぎた状況を作る
            clock.advance(2.0)
            return res

        def record_deliver(cand, _timeout):
            delivered.append(cand['task_id'])
            return True

        err_buf = io.StringIO()
        with mock.patch('app.run_check_command', side_effect=slow_check_advancing_to_237), \
                mock.patch('app.commit_check_run', side_effect=commit_advancing_to_239), \
                contextlib.redirect_stderr(err_buf):
            body, rc = run_check_tick(
                self.db, self.config,
                now_fn=clock.now,
                mono_fn=clock.monotonic,
                tick_budget=240.0,
                wake_pass_budget=20.0,
                wake_item_timeout=5.0,
                deliver_fn=record_deliver,
            )
        self.assertEqual((rc, body['status']), (0, 'ok'))
        self.assertEqual(delivered, [])
        self.assertEqual(body['unnotified_tasks'], 2)
        self.assertIn('truncated=True', err_buf.getvalue())
        # pruning は走っていない (239s >= end_work_deadline=238s)
        self.assertIsNotNone(self.query_one('SELECT id FROM check_runs WHERE id=?', (old_run_id,)))
        self.assertIsNotNone(self.query_one('SELECT id FROM check_ticks WHERE id=?', (old_tick_id,)))

        # (b) 残り 8s (t=230s, end_work_deadline=238s): end wake は 20s でなく 8s に制限され、
        # 1 件目 (4s) の後で残り 4s < item_timeout(5s) により 2 件目を打ち切る。
        # さらに pruning は deadline=238s までに実行できるバッチだけ実行する
        clock2 = FakeClock(wall=base_now + 200.0, mono=2000.0)
        delivered2 = []
        with contextlib.closing(sqlite3.connect(self.db)) as conn, conn:
            # 開始時 wake ではなく終了時 wake を試すため、タスクを一旦消し check を due に戻す
            conn.execute('DELETE FROM check_tasks')
            conn.execute('DELETE FROM tasks')
            conn.execute("UPDATE checks SET last_started=NULL, last_actual_started_at=NULL, last_outcome=NULL")

        def check_advancing_to_230(*args, **kwargs):
            res = run_check_command(*args, **kwargs)
            clock2.advance(230.0)
            return res

        def deliver_taking_4s(cand, _timeout):
            delivered2.append(cand['task_id'])
            clock2.advance(4.0)
            return True

        with mock.patch('app.run_check_command', side_effect=check_advancing_to_230), contextlib.redirect_stderr(io.StringIO()):
            body2, rc2 = run_check_tick(
                self.db, self.config,
                now_fn=clock2.now,
                mono_fn=clock2.monotonic,
                tick_budget=240.0,
                wake_pass_budget=20.0,
                wake_item_timeout=5.0,
                deliver_fn=deliver_taking_4s,
            )
        self.assertEqual((rc2, body2['status']), (0, 'ok'))
        self.assertEqual(len(delivered2), 1)
        self.assertEqual(body2['unnotified_tasks'], 1)
        # 234s < 238s なので pruning は実行されて古い行が消える
        self.assertIsNone(self.query_one('SELECT id FROM check_runs WHERE id=?', (old_run_id,)))
        self.assertIsNone(self.query_one('SELECT id FROM check_ticks WHERE id=?', (old_tick_id,)))

        # (c) prune_check_history 自体も deadline 到達で残バッチを打ち切る
        with contextlib.closing(sqlite3.connect(self.db)) as conn, conn:
            for _ in range(2):
                conn.execute(
                    "INSERT INTO check_runs(tick_id, check_name, started_at, finished_at, exit_code, outcome) VALUES(1, 'ev', ?, ?, 0, 'ok_silent')",
                    (old_iso, old_iso),
                )
        prune_ticks = [0.0, 10.0, 10.0]  # 1 バッチ目開始時は 0.0 < 5.0、2 バッチ目開始時は 10.0 >= 5.0
        prune_idx = {'i': 0}

        def prune_mono():
            val = prune_ticks[min(prune_idx['i'], len(prune_ticks) - 1)]
            prune_idx['i'] += 1
            return val

        finished_all = prune_check_history(
            self.db, current_tick_id=999999,
            cutoff_iso=datetime.fromtimestamp(base_now - 1000.0, timezone.utc).isoformat(timespec='microseconds'),
            batch_size=1, deadline=5.0, mono_fn=prune_mono,
        )
        self.assertFalse(finished_all)
        remaining_old = self.query_all('SELECT id FROM check_runs WHERE started_at = ?', (old_iso,))
        self.assertEqual(len(remaining_old), 1)

    def test_end_of_tick_db_lock_wait_stays_within_tick_deadline(self):
        import threading

        self.commit_config(
            '[[checks]]\nname = "ok_chk"\ninterval = 60\ncommand = "scripts/ok.sh"\ntimeout = 10\nskill = "s1"\n',
            {'scripts/ok.sh': '#!/bin/sh\nexit 0\n'},
        )
        base_now = 1700000000.0
        old_iso = datetime.fromtimestamp(base_now - 31 * 86400, timezone.utc).isoformat(timespec='microseconds')
        run_check_tick(self.db, self.config, now_fn=lambda: base_now - 1000.0, deliver_fn=lambda c, t: True)

        def seed_old_row_and_reset_due():
            with contextlib.closing(sqlite3.connect(self.db)) as conn, conn:
                conn.execute('DELETE FROM check_runs')
                conn.execute('DELETE FROM check_ticks')
                conn.execute("UPDATE checks SET last_started=NULL, last_actual_started_at=NULL, last_outcome=NULL")
                cur = conn.execute(
                    "INSERT INTO check_ticks(started_at, finished_at, status, wake_failed, unnotified_tasks, checks_deferred) VALUES(?, ?, 'ok', 0, 0, 0)",
                    (old_iso, old_iso),
                )
                old_tid = cur.lastrowid
                cur = conn.execute(
                    "INSERT INTO check_runs(tick_id, check_name, started_at, finished_at, exit_code, outcome) VALUES(?, 'ok_chk', ?, ?, 0, 'ok_silent')",
                    (old_tid, old_iso, old_iso),
                )
                return old_tid, cur.lastrowid

        def start_writer_lock(hold_sec=5.0):
            acquired = threading.Event()
            release = threading.Event()

            def _worker():
                conn = sqlite3.connect(self.db, timeout=5.0, isolation_level=None)
                try:
                    conn.execute('BEGIN IMMEDIATE')
                    acquired.set()
                    release.wait(timeout=hold_sec)
                finally:
                    with contextlib.suppress(sqlite3.Error):
                        conn.execute('ROLLBACK')
                    conn.close()

            thread = threading.Thread(target=_worker, daemon=True)
            thread.start()
            self.assertTrue(acquired.wait(timeout=3.0))
            return thread, release

        # (a) prune_check_history は DB ロック保持中でも既定の 5 秒待たず deadline 内で False を返すこと
        seed_old_row_and_reset_due()
        t_a, rel_a = start_writer_lock(hold_sec=5.0)
        try:
            t0 = time.monotonic()
            finished = prune_check_history(
                self.db, current_tick_id=999999,
                cutoff_iso=datetime.fromtimestamp(base_now - 1000.0, timezone.utc).isoformat(timespec='microseconds'),
                deadline=t0 + 0.15,
                mono_fn=time.monotonic,
            )
            elapsed_a = time.monotonic() - t0
        finally:
            rel_a.set()
            t_a.join(timeout=3.0)
        self.assertFalse(finished)
        self.assertLess(elapsed_a, 1.0, f'prune should stay near 0.15s deadline, took {elapsed_a:.2f}s')

        # (b) prune 開始時に DB ロックされ end_work_deadline 後・tick_deadline 前に解放される場合:
        #     prune が 5 秒待ちせず切り上げ、finalize_reserve 内で最終 status=ok の更新が完了すること
        old_tid_b, old_rid_b = seed_old_row_and_reset_due()
        orig_prune = prune_check_history
        anchor_b = [None]
        lock_state_b = []

        def cleanup_b():
            for t_b, rel_b in lock_state_b:
                rel_b.set()
                t_b.join(timeout=3.0)

        self.addCleanup(cleanup_b)

        def mono_b():
            if anchor_b[0] is None:
                return 0.0
            return time.monotonic() - anchor_b[0]

        def locked_prune(*args, **kwargs):
            # end_work_deadline まで 0.15s、finalize_reserve=0.45s (tick_deadline=0.60s)
            # prune が返るまで writer ロックを保持し、prune 終了後 (Step (9) 前) に明示解放する
            t_b, rel_b = start_writer_lock(hold_sec=5.0)
            lock_state_b.append((t_b, rel_b))
            anchor_b[0] = time.monotonic()
            try:
                return orig_prune(*args, **kwargs)
            finally:
                rel_b.set()
                t_b.join(timeout=3.0)

        t0_b = time.monotonic()
        try:
            with mock.patch('app.prune_check_history', side_effect=locked_prune):
                body_b, rc_b = run_check_tick(
                    self.db, self.config,
                    now_fn=lambda: base_now,
                    mono_fn=mono_b,
                    tick_budget=0.60,
                    finalize_reserve=0.45,
                    deliver_fn=lambda c, t: True,
                )
        finally:
            cleanup_b()
        elapsed_b = time.monotonic() - anchor_b[0] if anchor_b[0] is not None else time.monotonic() - t0_b
        self.assertEqual((rc_b, body_b['status']), (0, 'ok'))
        self.assertLess(elapsed_b, 1.5, f'tick end work should finish well below 5s busy timeout, took {elapsed_b:.2f}s')
        self.assertIsNotNone(self.query_one('SELECT id FROM check_runs WHERE id=?', (old_rid_b,)))
        self.assertIsNotNone(self.query_one('SELECT id FROM check_ticks WHERE id=?', (old_tid_b,)))
        self.assertEqual(self.query_one('SELECT status FROM check_ticks WHERE id=?', (body_b['tick_id'],))['status'], 'ok')

        # (c) end wake・prune・最終 status 更新の全区間で DB ロックが保持される場合:
        #     既定の 5 秒待ちを越えず予算内で失敗 (status=failed, rc=1) として終了すること
        seed_old_row_and_reset_due()
        orig_commit_retry = sys.modules['app']._commit_check_run_with_retry
        anchor_c = [None]
        lock_state_c = []

        def mono_c():
            if anchor_c[0] is None:
                return 0.0
            return time.monotonic() - anchor_c[0]

        def lock_after_commit(*args, **kwargs):
            res = orig_commit_retry(*args, **kwargs)
            t_c, rel_c = start_writer_lock(hold_sec=5.0)
            lock_state_c.append((t_c, rel_c))
            anchor_c[0] = time.monotonic()
            return res

        err_buf_c = io.StringIO()
        try:
            with mock.patch('app._commit_check_run_with_retry', side_effect=lock_after_commit), \
                    contextlib.redirect_stderr(err_buf_c):
                body_c, rc_c = run_check_tick(
                    self.db, self.config,
                    now_fn=lambda: base_now + 100.0,
                    mono_fn=mono_c,
                    tick_budget=0.45,
                    finalize_reserve=0.20,
                    deliver_fn=lambda c, t: True,
                )
            elapsed_c = time.monotonic() - anchor_c[0]
        finally:
            for t_c, rel_c in lock_state_c:
                rel_c.set()
                t_c.join(timeout=3.0)
        self.assertEqual((rc_c, body_c['status']), (1, 'failed'))
        self.assertIn('error', body_c)
        self.assertIn('failed to finalize check tick', err_buf_c.getvalue())
        self.assertLess(elapsed_c, 1.5, f'finalize under DB lock must not wait 5s, took {elapsed_c:.2f}s')

    def test_prune_preserves_parent_tick_of_retained_runs_until_runs_expire(self):
        self.commit_config(
            '[[checks]]\nname = "p1"\ninterval = 60\ncommand = "scripts/ok.sh"\ntimeout = 10\nskill = "s1"\n',
            {'scripts/ok.sh': '#!/bin/sh\nexit 0\n'},
        )
        run_check_tick(self.db, self.config, now_fn=lambda: 1700000000.0, deliver_fn=lambda c, t: True)

        iso = lambda ts: datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec='microseconds')
        t_tick = 1690000000.0  # 12:00:00 相当
        t_cutoff_mid = t_tick + 30.0  # 12:00:30 相当
        t_run = t_tick + 60.0  # 12:01:00 相当
        t_cutoff_after = t_tick + 120.0  # 12:02:00 相当

        with contextlib.closing(sqlite3.connect(self.db)) as conn, conn:
            cur = conn.execute(
                "INSERT INTO check_ticks(started_at, finished_at, status, wake_failed, unnotified_tasks, checks_deferred) VALUES(?, ?, 'ok', 0, 0, 0)",
                (iso(t_tick), iso(t_run + 5.0)),
            )
            parent_tick_id = cur.lastrowid
            cur = conn.execute(
                "INSERT INTO check_runs(tick_id, check_name, started_at, finished_at, exit_code, outcome) VALUES(?, 'p1', ?, ?, 0, 'fired')",
                (parent_tick_id, iso(t_run), iso(t_run + 1.0)),
            )
            retained_run_id = cur.lastrowid
            conn.execute(
                "INSERT INTO check_run_events(run_id, dedup_key, outcome, task_id) VALUES(?, 'k1', 'fired', 't_ret')",
                (retained_run_id,),
            )

        # cutoff が tick と run の間にあるとき: run・event が保持されるため親 tick も消さない
        prune_check_history(self.db, current_tick_id=999999, cutoff_iso=iso(t_cutoff_mid))
        self.assertIsNotNone(self.query_one('SELECT id FROM check_runs WHERE id=?', (retained_run_id,)))
        self.assertEqual(len(self.query_all('SELECT * FROM check_run_events WHERE run_id=?', (retained_run_id,))), 1)
        self.assertIsNotNone(self.query_one('SELECT id FROM check_ticks WHERE id=?', (parent_tick_id,)))

        # 最後の run が期限切れになった後は run・event・親 tick がすべて prune される
        prune_check_history(self.db, current_tick_id=999999, cutoff_iso=iso(t_cutoff_after))
        self.assertIsNone(self.query_one('SELECT id FROM check_runs WHERE id=?', (retained_run_id,)))
        self.assertEqual(self.query_all('SELECT * FROM check_run_events WHERE run_id=?', (retained_run_id,)), [])
        self.assertIsNone(self.query_one('SELECT id FROM check_ticks WHERE id=?', (parent_tick_id,)))

    def test_commit_contention_retries_without_rerunning_command_and_fails_tick_if_unpersistable(self):
        import threading

        invocations_file = self.root / 'invocations.txt'
        self.commit_config(
            '[[checks]]\nname = "onetime"\ninterval = 60\ncommand = "scripts/onetime.py"\ntimeout = 10\nskill = "s1"\n',
            {
                'scripts/onetime.py': (
                    '#!/usr/bin/env python3\nimport json, pathlib\n'
                    f'p = pathlib.Path({str(invocations_file)!r})\n'
                    'n = int(p.read_text()) + 1 if p.exists() else 1\n'
                    'p.write_text(str(n))\n'
                    'if n == 1:\n'
                    '    print(json.dumps({"key": "once", "title": "one-time event", "body": "valuable"}))\n'
                ),
            },
        )
        # DB だけ先に初期化して、onetime は未実行のままにする
        run_check_tick(self.db, self.config, now_fn=lambda: 1700000000.0, deliver_fn=lambda c, t: True)
        with contextlib.closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('DELETE FROM check_tasks')
            conn.execute('DELETE FROM tasks')
            conn.execute('DELETE FROM check_runs')
            conn.execute("UPDATE checks SET last_started=NULL, last_actual_started_at=NULL, last_outcome=NULL")
        if invocations_file.exists():
            invocations_file.unlink()

        # (a) 一時的な DB ロック競合: command を再実行せず commit をリトライして成功結果を保存する
        lock_acquired = threading.Event()
        release_lock = threading.Event()

        def hold_writer(hold_sec):
            conn = sqlite3.connect(self.db, timeout=5.0, isolation_level=None)
            try:
                conn.execute('BEGIN IMMEDIATE')
                lock_acquired.set()
                release_lock.wait(timeout=hold_sec)
            finally:
                with contextlib.suppress(sqlite3.Error):
                    conn.execute('ROLLBACK')
                conn.close()

        orig_commit = commit_check_run
        commit_attempts = [0]
        holder_thread = [None]

        def _cleanup_holder_a():
            release_lock.set()
            if holder_thread[0] is not None:
                holder_thread[0].join(timeout=3.0)

        self.addCleanup(_cleanup_holder_a)

        def commit_with_transient_lock(*args, **kwargs):
            commit_attempts[0] += 1
            if commit_attempts[0] == 1:
                t = threading.Thread(target=hold_writer, args=(10.0,), daemon=True)
                holder_thread[0] = t
                t.start()
                self.assertTrue(lock_acquired.wait(timeout=3.0))
                try:
                    return orig_commit(*args, **kwargs)
                except sqlite3.Error as db_error:
                    if sys.modules['app']._is_sqlite_retryable_contention(db_error):
                        release_lock.set()
                        t.join(timeout=3.0)
                    raise
            return orig_commit(*args, **kwargs)

        try:
            with mock.patch('app.commit_check_run', side_effect=commit_with_transient_lock):
                res_ok, rc_ok = run_check_tick(
                    self.db, self.config,
                    now_fn=lambda: 1700000100.0,
                    tick_budget=15.0,
                    commit_busy_timeout_ms=80,
                    deliver_fn=lambda c, t: True,
                )
        finally:
            _cleanup_holder_a()

        self.assertEqual((rc_ok, res_ok['status']), (0, 'ok'))
        self.assertGreaterEqual(commit_attempts[0], 2, 'commit should have retried after busy timeout')
        self.assertEqual(int(invocations_file.read_text()), 1, 'one-time command must not be re-executed')
        self.assertEqual(len(self.query_all("SELECT * FROM check_tasks WHERE dedup_key='once'")), 1)
        self.assertEqual(self.query_one("SELECT outcome FROM check_runs WHERE check_name='onetime'")['outcome'], 'fired')

        # (b) 残り budget を超える恒常的な競合 -> その後 writer が解放されても ok/exit 0 にせず failed tick & 非 0 終了
        with contextlib.closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('DELETE FROM check_tasks')
            conn.execute('DELETE FROM tasks')
            conn.execute('DELETE FROM check_runs')
            conn.execute("UPDATE checks SET last_started=NULL, last_actual_started_at=NULL, last_outcome=NULL")
        invocations_file.unlink()

        err_buf = io.StringIO()
        lock_acquired2 = threading.Event()
        release_lock2 = threading.Event()
        holder2 = [None]

        def _cleanup_holder_b():
            release_lock2.set()
            if holder2[0] is not None:
                holder2[0].join(timeout=3.0)

        self.addCleanup(_cleanup_holder_b)

        def hold_writer_until_released():
            conn = sqlite3.connect(self.db, timeout=5.0, isolation_level=None)
            try:
                conn.execute('BEGIN IMMEDIATE')
                lock_acquired2.set()
                release_lock2.wait(timeout=10.0)
            finally:
                with contextlib.suppress(sqlite3.Error):
                    conn.execute('ROLLBACK')
                conn.close()

        # SQLite / git setup の所要時間が 0.6s budget を消費しないよう、
        # command 完了・writer lock 確保後の commit 境界から単調時計を進める
        commit_clock_origin_b = [None]

        def mono_b():
            if commit_clock_origin_b[0] is None:
                return 0.0
            return time.monotonic() - commit_clock_origin_b[0]

        orig_run_cmd = run_check_command

        def start_writer_after_command(*args, **kwargs):
            res = orig_run_cmd(*args, **kwargs)
            t = threading.Thread(target=hold_writer_until_released, daemon=True)
            holder2[0] = t
            t.start()
            self.assertTrue(lock_acquired2.wait(timeout=3.0))
            commit_clock_origin_b[0] = time.monotonic()
            return res

        # commit が失敗してリトライを諦めた直後に writer を解放する (finalization が走れる状況を作る)
        orig_commit_retry = sys.modules['app']._commit_check_run_with_retry

        def release_after_commit_fails(*args, **kwargs):
            try:
                return orig_commit_retry(*args, **kwargs)
            finally:
                _cleanup_holder_b()

        try:
            with mock.patch('app.run_check_command', side_effect=start_writer_after_command), \
                    mock.patch('app._commit_check_run_with_retry', side_effect=release_after_commit_fails), \
                    contextlib.redirect_stderr(err_buf):
                res_fail, rc_fail = run_check_tick(
                    self.db, self.config,
                    now_fn=lambda: 1700000200.0,
                    mono_fn=mono_b,
                    tick_budget=0.6,
                    finalize_reserve=0.25,
                    commit_busy_timeout_ms=60,
                    deliver_fn=lambda c, t: True,
                )
        finally:
            _cleanup_holder_b()
        self.assertIsNotNone(commit_clock_origin_b[0], 'setup must not exhaust tick budget before command/commit')
        self.assertEqual(rc_fail, 1)
        self.assertEqual(res_fail['status'], 'failed')
        self.assertEqual(int(invocations_file.read_text()), 1)
        tick_row = self.query_one('SELECT status FROM check_ticks WHERE id=?', (res_fail['tick_id'],))
        self.assertEqual(tick_row['status'], 'failed')
        self.assertEqual(self.query_all("SELECT * FROM check_ticks WHERE status='running'"), [])
        self.assertIn('failed to commit check run', err_buf.getvalue())
        self.assertNotIn('before the first attempt', err_buf.getvalue())

        # (c) deadline 到達後に初めて成功する commit は試行されず、成功記録を残さず tick が failed になること
        # また残り 50ms 未満のとき busy_timeout_ms が残り時間で clamp され 50ms 下限で超過しないこと
        with contextlib.closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('DELETE FROM check_tasks')
            conn.execute('DELETE FROM tasks')
            conn.execute('DELETE FROM check_runs')
            conn.execute("UPDATE checks SET last_started=NULL, last_actual_started_at=NULL, last_outcome=NULL")
        if invocations_file.exists():
            invocations_file.unlink()

        clock_c = FakeClock(wall=1700000300.0, mono=1000.0)
        seen_busy_ms = []
        commit_calls_after_deadline = []

        def cmd_leaving_20ms(*args, **kwargs):
            res = orig_run_cmd(*args, **kwargs)
            # tick_budget=10.0, finalize_reserve=2.0 -> end_work_deadline=1008.0。残り 20ms (50ms 未満) にする
            clock_c.advance(7.98)
            return res

        def commit_only_succeeds_after_deadline(*args, **kwargs):
            seen_busy_ms.append(kwargs.get('busy_timeout_ms'))
            if clock_c.monotonic() >= 1008.0:
                commit_calls_after_deadline.append(clock_c.monotonic())
                return orig_commit(*args, **kwargs)
            # 1 回目は deadline ちょうどまで競合失敗 (この後なら成功し得る状態)
            clock_c.advance(0.02)
            raise sqlite3.OperationalError('database is locked')

        err_buf_c = io.StringIO()
        with mock.patch('app.run_check_command', side_effect=cmd_leaving_20ms), \
                mock.patch('app.commit_check_run', side_effect=commit_only_succeeds_after_deadline), \
                contextlib.redirect_stderr(err_buf_c):
            res_c, rc_c = run_check_tick(
                self.db, self.config,
                now_fn=clock_c.now,
                mono_fn=clock_c.monotonic,
                tick_budget=10.0,
                finalize_reserve=2.0,
                commit_busy_timeout_ms=500,
                deliver_fn=lambda c, t: True,
            )
        self.assertEqual((rc_c, res_c['status']), (1, 'failed'))
        self.assertEqual(len(seen_busy_ms), 1, 'must not retry after retry deadline')
        self.assertLessEqual(seen_busy_ms[0], 20, f'busy_timeout_ms must be clamped to remaining 20ms, got {seen_busy_ms[0]}')
        self.assertEqual(commit_calls_after_deadline, [], 'must never attempt commit after deadline')
        self.assertEqual(self.query_all("SELECT * FROM check_runs WHERE check_name='onetime'"), [])
        self.assertEqual(self.query_all("SELECT * FROM check_tasks WHERE dedup_key='once'"), [])

        # (d) sleep 後に deadline 到達した場合も再試行せず、実 SQLite ロックが deadline 後に解放されても failed になること
        with contextlib.closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('DELETE FROM check_tasks')
            conn.execute('DELETE FROM tasks')
            conn.execute('DELETE FROM check_runs')
            conn.execute("UPDATE checks SET last_started=NULL, last_actual_started_at=NULL, last_outcome=NULL")
        if invocations_file.exists():
            invocations_file.unlink()

        lock_acq_d = threading.Event()
        release_lock_d = threading.Event()
        holder_d = [None]

        def _cleanup_holder_d():
            release_lock_d.set()
            if holder_d[0] is not None:
                holder_d[0].join(timeout=3.0)

        self.addCleanup(_cleanup_holder_d)

        def hold_writer_past_deadline(fallback_sec):
            conn = sqlite3.connect(self.db, timeout=5.0, isolation_level=None)
            try:
                conn.execute('BEGIN IMMEDIATE')
                lock_acq_d.set()
                release_lock_d.wait(timeout=fallback_sec)
            finally:
                with contextlib.suppress(sqlite3.Error):
                    conn.execute('ROLLBACK')
                conn.close()

        # tick 開始後の setup 時間を除き、check 完了時点から残り 120ms だけ retry_deadline まで残す。
        # _commit_check_run_with_retry が戻る/例外送出するまで実 SQLite ロックを保持し、
        # Step (9) finalization の直前に明示解放する (スレッドスケジューリング依存を避ける)。
        real_anchor_d = [None]

        def mono_d():
            if real_anchor_d[0] is None:
                return 0.0
            return time.monotonic() - real_anchor_d[0]

        def cmd_then_lock_past_deadline(*args, **kwargs):
            res = orig_run_cmd(*args, **kwargs)
            t = threading.Thread(target=hold_writer_past_deadline, args=(5.0,), daemon=True)
            holder_d[0] = t
            t.start()
            self.assertTrue(lock_acq_d.wait(timeout=3.0))
            real_anchor_d[0] = time.monotonic()
            return res

        def release_after_commit_d(*args, **kwargs):
            try:
                return orig_commit_retry(*args, **kwargs)
            finally:
                _cleanup_holder_d()

        err_buf_d = io.StringIO()
        try:
            with mock.patch('app.run_check_command', side_effect=cmd_then_lock_past_deadline), \
                    mock.patch('app._commit_check_run_with_retry', side_effect=release_after_commit_d), \
                    contextlib.redirect_stderr(err_buf_d):
                res_d, rc_d = run_check_tick(
                    self.db, self.config,
                    now_fn=lambda: 1700000400.0,
                    mono_fn=mono_d,
                    tick_budget=0.62,
                    finalize_reserve=0.50,  # end_work_deadline = 0.12s, finalize は 0.50s 余裕あり
                    commit_busy_timeout_ms=500,
                    deliver_fn=lambda c, t: True,
                )
        finally:
            _cleanup_holder_d()
        self.assertEqual((rc_d, res_d['status']), (1, 'failed'))
        self.assertEqual(self.query_all("SELECT * FROM check_runs WHERE check_name='onetime'"), [])
        self.assertEqual(self.query_all("SELECT * FROM check_tasks WHERE dedup_key='once'"), [])
        self.assertEqual(self.query_one('SELECT status FROM check_ticks WHERE id=?', (res_d['tick_id'],))['status'], 'failed')

        # (e) 決定的な backoff 後 deadline チェック:
        #     初回 busy エラー後に残り時間 (>= 1ms) を残して確実に backoff sleep に入り、
        #     backoff 中に注入時計が deadline へ到達したときは 2 回目の commit を開始しないこと
        with contextlib.closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('DELETE FROM check_tasks')
            conn.execute('DELETE FROM tasks')
            conn.execute('DELETE FROM check_runs')
            conn.execute("UPDATE checks SET last_started=NULL, last_actual_started_at=NULL, last_outcome=NULL")
        if invocations_file.exists():
            invocations_file.unlink()

        clock_e = FakeClock(wall=1700000450.0, mono=1000.0)
        commit_calls_e = []
        sleep_calls_e = []
        in_commit_retry_e = [False]
        orig_sleep = time.sleep
        orig_monotonic = time.monotonic

        def cmd_leaving_100ms_e(*args, **kwargs):
            res = orig_run_cmd(*args, **kwargs)
            # tick_budget=10.0, finalize_reserve=2.0 -> end_work_deadline=1008.0。残り 100ms にする
            clock_e.advance(7.90)
            return res

        def commit_busy_once_then_would_succeed(*args, **kwargs):
            commit_calls_e.append((clock_e.monotonic(), kwargs.get('busy_timeout_ms')))
            if len(commit_calls_e) == 1:
                # 20ms 消費して残り 80ms (> 1ms) のまま busy エラー -> 確実に backoff へ入る
                clock_e.advance(0.02)
                raise sqlite3.OperationalError('database is locked')
            return orig_commit(*args, **kwargs)

        def track_commit_retry_e(*args, **kwargs):
            in_commit_retry_e[0] = True
            try:
                return orig_commit_retry(*args, **kwargs)
            finally:
                in_commit_retry_e[0] = False

        def controlled_monotonic_e():
            if in_commit_retry_e[0]:
                return clock_e.monotonic()
            return orig_monotonic()

        def advance_to_deadline_during_backoff(sec):
            if in_commit_retry_e[0]:
                sleep_calls_e.append(sec)
                # backoff 中に end_work_deadline (1008.0) へ到達させる
                clock_e.advance(max(0.0, 1008.0 - clock_e.monotonic()))
                return
            orig_sleep(sec)

        err_buf_e = io.StringIO()
        with mock.patch('app.run_check_command', side_effect=cmd_leaving_100ms_e), \
                mock.patch('app._commit_check_run_with_retry', side_effect=track_commit_retry_e), \
                mock.patch('app.commit_check_run', side_effect=commit_busy_once_then_would_succeed), \
                mock.patch('app.time.monotonic', side_effect=controlled_monotonic_e), \
                mock.patch('app.time.sleep', side_effect=advance_to_deadline_during_backoff), \
                contextlib.redirect_stderr(err_buf_e):
            res_e, rc_e = run_check_tick(
                self.db, self.config,
                now_fn=clock_e.now,
                mono_fn=clock_e.monotonic,
                tick_budget=10.0,
                finalize_reserve=2.0,
                commit_busy_timeout_ms=500,
                deliver_fn=lambda c, t: True,
            )
        self.assertEqual((rc_e, res_e['status']), (1, 'failed'))
        self.assertEqual(len(sleep_calls_e), 1, 'first busy error must enter backoff sleep before deadline')
        self.assertAlmostEqual(sleep_calls_e[0], 0.04, places=3)
        self.assertEqual(len(commit_calls_e), 1, 'must not start a second commit after backoff reaches deadline')
        self.assertEqual(self.query_all("SELECT * FROM check_runs WHERE check_name='onetime'"), [])
        self.assertEqual(self.query_all("SELECT * FROM check_tasks WHERE dedup_key='once'"), [])
        self.assertEqual(self.query_one('SELECT status FROM check_ticks WHERE id=?', (res_e['tick_id'],))['status'], 'failed')

    def test_commit_retry_skips_initial_attempt_when_retry_deadline_already_passed(self):
        # 初回の commit 呼び出し時点で既に retry deadline を過ぎている場合は、
        # busy_timeout_ms=0 の即時試行すら行わず、保存せずに tick を failed にすること。
        self.commit_config(
            '[[checks]]\nname = "onetime"\ninterval = 60\ncommand = "scripts/ok.sh"\ntimeout = 10\nskill = "s1"\n',
            {'scripts/ok.sh': '#!/bin/sh\nexit 0\n'},
        )
        # DB だけ先に初期化して、onetime は未実行のままにする
        run_check_tick(self.db, self.config, now_fn=lambda: 1700000500.0, deliver_fn=lambda c, t: True)
        with contextlib.closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('DELETE FROM check_tasks')
            conn.execute('DELETE FROM tasks')
            conn.execute('DELETE FROM check_runs')
            conn.execute("UPDATE checks SET last_started=NULL, last_actual_started_at=NULL, last_outcome=NULL")

        clock = FakeClock(wall=1700000600.0, mono=2000.0)
        orig_run_cmd = run_check_command
        orig_commit = commit_check_run
        commit_calls = []

        def cmd_finishing_after_deadline(*args, **kwargs):
            res = orig_run_cmd(*args, **kwargs)
            # tick_budget=10.0, finalize_reserve=2.0 -> end_work_deadline=3008.0。
            # check 完了時点で deadline を確実に過ぎた状態にする。
            clock.advance(10.001)
            return res

        def commit_spy(*args, **kwargs):
            commit_calls.append(clock.monotonic())
            return orig_commit(*args, **kwargs)

        err_buf = io.StringIO()
        with mock.patch('app.run_check_command', side_effect=cmd_finishing_after_deadline), \
                mock.patch('app.commit_check_run', side_effect=commit_spy), \
                contextlib.redirect_stderr(err_buf):
            res, rc = run_check_tick(
                self.db, self.config,
                now_fn=clock.now,
                mono_fn=clock.monotonic,
                tick_budget=10.0,
                finalize_reserve=2.0,
                commit_busy_timeout_ms=500,
                deliver_fn=lambda c, t: True,
            )

        self.assertEqual((rc, res['status']), (1, 'failed'))
        self.assertEqual(commit_calls, [], 'must not attempt commit when retry deadline already passed')
        self.assertEqual(self.query_all("SELECT * FROM check_runs WHERE check_name='onetime'"), [])
        self.assertEqual(self.query_all("SELECT * FROM check_tasks WHERE dedup_key='once'"), [])
        self.assertEqual(self.query_one('SELECT status FROM check_ticks WHERE id=?', (res['tick_id'],))['status'], 'failed')
        self.assertIn('failed to commit check run', err_buf.getvalue())

    def test_status_does_not_report_active_running_tick_as_abnormal_exit(self):
        started_flag = self.root / 'active_tick.started'
        release_flag = self.root / 'active_tick.release'
        pgid_file = self.root / 'active_tick.pgid'
        self.commit_config(
            '[[checks]]\nname = "hold"\ninterval = 300\ncommand = "scripts/hold.py"\ntimeout = 20\nskill = "s1"\n',
            {
                'scripts/hold.py': (
                    '#!/usr/bin/env python3\nimport os, pathlib, time\n'
                    f'tmp = pathlib.Path({str(pgid_file)!r} + ".tmp")\n'
                    'tmp.write_text(str(os.getpgrp()), encoding="utf-8")\n'
                    f'tmp.replace({str(pgid_file)!r})\n'
                    f'pathlib.Path({str(started_flag)!r}).write_text("ok", encoding="utf-8")\n'
                    f'while not pathlib.Path({str(release_flag)!r}).exists():\n'
                    '    time.sleep(0.05)\n'
                ),
            },
        )
        env = self._cli_env({'CUE_CHECK_NOW': '1700000000'})
        proc = subprocess.Popen(
            [sys.executable, str(ROOT / 'app.py'), '--db', str(self.db), 'check', 'tick', '--config', str(self.config)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, start_new_session=True,
        )
        try:
            dl = time.monotonic() + 10.0
            while not started_flag.exists() and time.monotonic() < dl:
                time.sleep(0.05)
            self.assertTrue(started_flag.exists(), 'active tick did not start check')

            # 実行中の tick に対して別 tick が skipped を書いても、実行中 tick を異常終了と報告しないこと
            sk = self.cli('check', 'tick', '--config', str(self.config), env_extra={'CUE_CHECK_NOW': '1700000010'})
            self.assertEqual(sk.returncode, 0, sk.stderr)
            self.assertEqual(json.loads(sk.stdout)['status'], 'skipped')

            st_json = json.loads(self.cli('check', 'status', '--json').stdout)
            self.assertEqual(st_json['running_leftover_count'], 0)
            self.assertEqual(st_json['running_in_progress_count'], 1)
            st_text = self.cli('check', 'status').stdout
            self.assertNotIn('WARNING', st_text)
            self.assertNotIn('異常終了', st_text)

            # 正常完了させる
            release_flag.write_text('go', encoding='utf-8')
            out, err = proc.communicate(timeout=10)
            self.assertEqual(proc.returncode, 0, err)
            self.assertEqual(json.loads(out)['status'], 'ok')
        finally:
            if proc.poll() is None:
                with contextlib.suppress(ProcessLookupError, OSError):
                    os.killpg(proc.pid, signal.SIGKILL)
                with contextlib.suppress(subprocess.TimeoutExpired, OSError):
                    proc.wait(timeout=2)
            if pgid_file.exists():
                with contextlib.suppress(ValueError, ProcessLookupError, OSError):
                    os.killpg(int(pgid_file.read_text(encoding='utf-8').strip()), signal.SIGKILL)
            if proc.stdout:
                proc.stdout.close()
            if proc.stderr:
                proc.stderr.close()

        after_json = json.loads(self.cli('check', 'status', '--json').stdout)
        self.assertEqual(after_json['running_leftover_count'], 0)
        self.assertEqual(after_json['running_in_progress_count'], 0)
        self.assertIsNotNone(after_json['last_ok_tick_at'])

    def test_status_reads_all_queries_from_single_db_snapshot(self):
        # WAL DB を初期化し、status='running' の tick のみが存在する状態を作る
        self.commit_config(
            '[[checks]]\nname = "c1"\ninterval = 60\ncommand = "scripts/ok.sh"\ntimeout = 10\nskill = "s1"\n',
            {'scripts/ok.sh': '#!/bin/sh\nexit 0\n'},
        )
        run_check_tick(self.db, self.config, now_fn=lambda: 1700000000.0, deliver_fn=lambda c, t: True)
        ts0 = datetime.fromtimestamp(1700000100.0, timezone.utc).isoformat(timespec='microseconds')
        ts1 = datetime.fromtimestamp(1700000105.0, timezone.utc).isoformat(timespec='microseconds')
        with contextlib.closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('DELETE FROM check_ticks')
            cur = conn.execute(
                "INSERT INTO check_ticks(started_at, finished_at, status, wake_failed, unnotified_tasks, checks_deferred) VALUES(?, NULL, 'running', 0, 0, NULL)",
                (ts0,),
            )
            running_tick_id = cur.lastrowid

        orig_ro_db = sys.modules['app']._check_readonly_db
        committed_mid_read = [False]

        class SnapshotProbingConn:
            def __init__(self, inner, db_path):
                self._inner = inner
                self._db_path = db_path

            def execute(self, sql, *args, **kwargs):
                result = self._inner.execute(sql, *args, **kwargs)
                # last_ok_tick_at を読んだ直後・recent_ticks を読む前に別接続で running -> ok を commit する
                if "WHERE status='ok'" in sql and not committed_mid_read[0]:
                    committed_mid_read[0] = True
                    with contextlib.closing(sqlite3.connect(self._db_path)) as writer, writer:
                        writer.execute(
                            "UPDATE check_ticks SET status='ok', finished_at=?, checks_deferred=0 WHERE id=?",
                            (ts1, running_tick_id),
                        )
                return result

            def __getattr__(self, name):
                return getattr(self._inner, name)

        @contextlib.contextmanager
        def wrapped_ro_db(db_path):
            with orig_ro_db(db_path) as conn:
                yield SnapshotProbingConn(conn, db_path) if conn is not None else None

        with mock.patch('app._check_readonly_db', side_effect=wrapped_ro_db):
            snap = check_status_data(self.db, now_fn=lambda: 1700000110.0)

        self.assertTrue(committed_mid_read[0])
        # 同じスナップショットから読んでいるため last_ok_tick_at=None と recent_ticks=[running] が整合する
        self.assertIsNone(snap['last_ok_tick_at'])
        self.assertEqual([t['status'] for t in snap['recent_ticks']], ['running'])

        # 次の check_status_data 呼び出しでは新しいスナップショットで両方 ok として整合する
        snap_after = check_status_data(self.db, now_fn=lambda: 1700000110.0)
        self.assertEqual(snap_after['last_ok_tick_at'], ts1)
        self.assertEqual([t['status'] for t in snap_after['recent_ticks']], ['ok'])

    def test_fixture_isolates_check_runner_env_overrides_and_distinguishes_zombie(self):
        # 1. setUp は親環境からの check-runner 系設定を除去していること
        for key in CHECK_RUNNER_ENV_KEYS:
            if key != 'CUE_RUN_INBOX_BIN':
                self.assertNotIn(key, os.environ)
        # 2. _cli_env も親環境の check-runner 系 override を除去し、明示指定だけを残すこと
        with mock.patch.dict(os.environ, {
            'CUE_CHECK_TICK_BUDGET_SEC': '0',
            'CUE_CHECK_NOW': '123',
            'CUE_WAKE_RUN_ID': 'leaked-run',
            'CUE_CHECKS_CONFIG': '/tmp/leaked.toml',
            'MODE': 'sleep',
        }):
            clean = self._cli_env()
            for key in ('CUE_CHECK_TICK_BUDGET_SEC', 'CUE_CHECK_NOW', 'CUE_WAKE_RUN_ID', 'CUE_CHECKS_CONFIG', 'MODE'):
                self.assertNotIn(key, clean)
            overridden = self._cli_env({'CUE_CHECK_NOW': '1700000999', 'MODE': 'fail'})
            self.assertEqual(overridden['CUE_CHECK_NOW'], '1700000999')
            self.assertEqual(overridden['MODE'], 'fail')
            self.assertNotIn('CUE_CHECK_TICK_BUDGET_SEC', overridden)

        # 3. 終了済みだが未 reap の zombie プロセスを停止済みとみなせること
        zombie = subprocess.Popen([sys.executable, '-c', 'pass'])
        try:
            self.assertTrue(_wait_pid_stopped(zombie.pid, timeout=2.0))
            self.assertTrue(_pid_stopped(zombie.pid))
        finally:
            zombie.wait(timeout=5)

    def test_wake_delivery_failure_counted_even_when_bookkeeping_hits_db_contention(self):
        import threading

        self.commit_config(
            '[[checks]]\nname = "ev"\ninterval = 60\ncommand = "scripts/emit.py"\ntimeout = 10\nskill = "s1"\n',
            {
                'scripts/emit.py': (
                    '#!/usr/bin/env python3\nimport json\n'
                    'print(json.dumps({"key": "k1", "title": "t1", "body": "b1"}))\n'
                ),
            },
        )
        # DB 初期化後にクリーン状態へ戻す
        run_check_tick(self.db, self.config, now_fn=lambda: 1700000000.0, deliver_fn=lambda c, t: True)

        def reset_for_case():
            with contextlib.closing(sqlite3.connect(self.db)) as conn, conn:
                conn.execute('DELETE FROM check_tasks')
                conn.execute('DELETE FROM tasks')
                conn.execute('DELETE FROM check_runs')
                conn.execute("UPDATE checks SET last_started=NULL, last_actual_started_at=NULL, last_outcome=NULL")

        def run_case(*, delivery_ok, tick_now):
            reset_for_case()
            acquired = threading.Event()
            release = threading.Event()
            holder = [None]

            def cleanup():
                release.set()
                if holder[0] is not None:
                    holder[0].join(timeout=3.0)

            self.addCleanup(cleanup)

            def hold_writer():
                conn = sqlite3.connect(self.db, timeout=5.0, isolation_level=None)
                try:
                    conn.execute('BEGIN IMMEDIATE')
                    acquired.set()
                    release.wait(timeout=5.0)
                finally:
                    with contextlib.suppress(sqlite3.Error):
                        conn.execute('ROLLBACK')
                    conn.close()

            orig_wake = run_wake_pass
            wake_calls = [0]
            deliver_calls = [0]
            # 回収・候補取得やスケジューリング遅延で初回配送ガード前に 150ms 予算を使い切らないよう、
            # スケジューリング時計は固定し、配送後の bookkeeping DB 競合は run_wake_pass の実時間上限で打ち切る。
            clock = FakeClock(wall=tick_now, mono=1000.0)

            def deliver_then_lock_bookkeeping(cand, _timeout):
                deliver_calls[0] += 1
                t = threading.Thread(target=hold_writer, daemon=True)
                holder[0] = t
                t.start()
                self.assertTrue(acquired.wait(timeout=3.0))
                return delivery_ok

            def wrapped_wake(*args, **kwargs):
                wake_calls[0] += 1
                try:
                    return orig_wake(*args, **kwargs)
                finally:
                    # end wake 終了後、Step (9) finalization 前に writer を解放する
                    if wake_calls[0] == 2:
                        cleanup()

            err_buf = io.StringIO()
            try:
                with mock.patch('app.run_wake_pass', side_effect=wrapped_wake), \
                        contextlib.redirect_stderr(err_buf):
                    body, rc = run_check_tick(
                        self.db, self.config,
                        now_fn=clock.now,
                        mono_fn=clock.monotonic,
                        tick_budget=20.0,
                        wake_pass_budget=0.15,
                        wake_item_timeout=0.05,
                        deliver_fn=deliver_then_lock_bookkeeping,
                    )
            finally:
                cleanup()
            self.assertEqual(deliver_calls[0], 1, 'delivery callback must run exactly once')
            return body, rc, err_buf.getvalue()

        # (a) 配送失敗 + bookkeeping DB 競合 -> wake_failed=1 として保存されタスクは未通知のまま残る
        body_fail, rc_fail, err_fail = run_case(delivery_ok=False, tick_now=1700000100.0)
        self.assertEqual((rc_fail, body_fail['status']), (0, 'ok'))
        self.assertEqual(body_fail['wake_failed'], 1)
        self.assertEqual(body_fail['unnotified_tasks'], 1)
        self.assertIn('wake_failed=1', err_fail)
        self.assertIn('truncated=True', err_fail)
        tick_fail = self.query_one('SELECT wake_failed, unnotified_tasks FROM check_ticks WHERE id=?', (body_fail['tick_id'],))
        self.assertEqual((tick_fail['wake_failed'], tick_fail['unnotified_tasks']), (1, 1))
        ct_fail = self.query_one('SELECT needs_wake, notified_at, last_wake_attempt_at FROM check_tasks')
        self.assertEqual((ct_fail['needs_wake'], ct_fail['notified_at'], ct_fail['last_wake_attempt_at']), (1, None, None))

        # (b) 配送成功 + bookkeeping DB 競合 -> wake_failed は増やさず (0)、タスクは未通知のまま残る
        body_ok, rc_ok, err_ok = run_case(delivery_ok=True, tick_now=1700000200.0)
        self.assertEqual((rc_ok, body_ok['status']), (0, 'ok'))
        self.assertEqual(body_ok['wake_failed'], 0)
        self.assertEqual(body_ok['unnotified_tasks'], 1)
        self.assertIn('wake_failed=0', err_ok)
        self.assertIn('truncated=True', err_ok)
        tick_ok = self.query_one('SELECT wake_failed, unnotified_tasks FROM check_ticks WHERE id=?', (body_ok['tick_id'],))
        self.assertEqual((tick_ok['wake_failed'], tick_ok['unnotified_tasks']), (0, 1))
        ct_ok = self.query_one('SELECT needs_wake, notified_at, last_wake_attempt_at FROM check_tasks')
        self.assertEqual((ct_ok['needs_wake'], ct_ok['notified_at'], ct_ok['last_wake_attempt_at']), (1, None, None))

    def test_start_wake_db_contention_finalizes_failed_tick_instead_of_raising(self):
        import threading

        self.commit_config(
            '[[checks]]\nname = "c1"\ninterval = 60\ncommand = "scripts/ok.sh"\ntimeout = 10\nskill = "s1"\n',
            {'scripts/ok.sh': '#!/bin/sh\nexit 0\n'},
        )
        # DB を初期化しておく
        run_check_tick(self.db, self.config, now_fn=lambda: 1700000000.0, deliver_fn=lambda c, t: True)

        def start_writer_lock():
            acquired = threading.Event()
            release = threading.Event()

            def _worker():
                conn = sqlite3.connect(self.db, timeout=5.0, isolation_level=None)
                try:
                    conn.execute('BEGIN IMMEDIATE')
                    acquired.set()
                    release.wait(timeout=5.0)
                finally:
                    with contextlib.suppress(sqlite3.Error):
                        conn.execute('ROLLBACK')
                    conn.close()

            thread = threading.Thread(target=_worker, daemon=True)
            thread.start()
            self.assertTrue(acquired.wait(timeout=3.0))
            return thread, release

        # (a) start wake 回収/候補取得が DB 競合で失敗した後、finalization 前に writer が解放される場合:
        #     例外送出せず status='failed' へ最終更新し、running 行を残さないこと
        orig_wake = run_wake_pass
        config_called = []
        holder_a = []

        def cleanup_a():
            for t, rel in holder_a:
                rel.set()
                t.join(timeout=3.0)

        self.addCleanup(cleanup_a)

        def locked_start_wake(*args, **kwargs):
            t, rel = start_writer_lock()
            holder_a.append((t, rel))
            try:
                return orig_wake(*args, **kwargs)
            finally:
                rel.set()
                t.join(timeout=3.0)

        err_a = io.StringIO()
        with mock.patch('app.run_wake_pass', side_effect=locked_start_wake), \
                mock.patch('app.load_and_validate_checks_config', side_effect=lambda *a, **kw: config_called.append(True)), \
                contextlib.redirect_stderr(err_a):
            body_a, rc_a = run_check_tick(
                self.db, self.config,
                now_fn=lambda: 1700000100.0,
                tick_budget=10.0,
                wake_pass_budget=0.15,
                commit_busy_timeout_ms=60,
                deliver_fn=lambda c, t: True,
            )
        self.assertEqual((rc_a, body_a['status']), (1, 'failed'))
        self.assertIsNone(body_a['checks_deferred'])
        self.assertIn('error', body_a)
        self.assertEqual(config_called, [], 'config validation must not run after start-wake DB failure')
        self.assertEqual(self.query_all("SELECT * FROM check_ticks WHERE status='running'"), [])
        row_a = self.query_one('SELECT status, finished_at, checks_deferred, wake_failed FROM check_ticks WHERE id=?', (body_a['tick_id'],))
        self.assertEqual(row_a['status'], 'failed')
        self.assertIsNotNone(row_a['finished_at'])
        self.assertIsNone(row_a['checks_deferred'])
        self.assertEqual(row_a['wake_failed'], 0)
        self.assertIn('failed during start wake pass', err_a.getvalue())

        # (b) start wake 中に 1 件配送失敗した直後の bookkeeping が DB 競合で失敗した場合:
        #     wake_failed=1 を失わずに status='failed' へ最終更新すること
        now_iso = datetime.fromtimestamp(1700000150.0, timezone.utc).isoformat(timespec='microseconds')
        with contextlib.closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                "INSERT INTO tasks(id, title, request, pane, run, result, remaining, lane, decision_id, created_at, updated_at, machine, kind, done_when) "
                "VALUES('t_sw_fail', 'title', 'body', '', '', '', '', 'todo', NULL, ?, ?, '', 'monitor', 'done')",
                (now_iso, now_iso),
            )
            conn.execute(
                "INSERT INTO check_tasks(task_id, check_name, kind, dedup_key, skill, needs_wake, notified_at, last_wake_attempt_at, created_at) "
                "VALUES('t_sw_fail', 'c1', 'event', 'k_sw', 's1', 1, NULL, NULL, ?)",
                (now_iso,),
            )

        holder_b = []

        def cleanup_b():
            for t, rel in holder_b:
                rel.set()
                t.join(timeout=3.0)

        self.addCleanup(cleanup_b)

        def fail_deliver_then_lock(cand, _timeout):
            t, rel = start_writer_lock()
            holder_b.append((t, rel))
            return False

        def release_after_start_wake(*args, **kwargs):
            try:
                return orig_wake(*args, **kwargs)
            finally:
                cleanup_b()

        err_b = io.StringIO()
        with mock.patch('app.run_wake_pass', side_effect=release_after_start_wake), \
                contextlib.redirect_stderr(err_b):
            body_b, rc_b = run_check_tick(
                self.db, self.config,
                now_fn=lambda: 1700000200.0,
                tick_budget=10.0,
                wake_pass_budget=0.20,
                wake_item_timeout=0.05,
                commit_busy_timeout_ms=60,
                deliver_fn=fail_deliver_then_lock,
            )
        self.assertEqual((rc_b, body_b['status']), (1, 'failed'))
        self.assertEqual(body_b['wake_failed'], 1)
        self.assertEqual(body_b['unnotified_tasks'], 1)
        self.assertEqual(self.query_all("SELECT * FROM check_ticks WHERE status='running'"), [])
        row_b = self.query_one('SELECT status, wake_failed, unnotified_tasks FROM check_ticks WHERE id=?', (body_b['tick_id'],))
        self.assertEqual((row_b['status'], row_b['wake_failed'], row_b['unnotified_tasks']), ('failed', 1, 1))

        # (c) finalization までずっと DB ロックが続く場合でも例外で落ちず (result_body, 1) を返すこと
        holder_c = []

        def cleanup_c():
            for t, rel in holder_c:
                rel.set()
                t.join(timeout=3.0)

        self.addCleanup(cleanup_c)
        anchor_c = [None]

        def mono_c():
            if anchor_c[0] is None:
                return 0.0
            return time.monotonic() - anchor_c[0]

        def lock_through_finalize(*args, **kwargs):
            t, rel = start_writer_lock()
            holder_c.append((t, rel))
            anchor_c[0] = time.monotonic()
            return orig_wake(*args, **kwargs)

        err_c = io.StringIO()
        try:
            with mock.patch('app.run_wake_pass', side_effect=lock_through_finalize), \
                    contextlib.redirect_stderr(err_c):
                body_c, rc_c = run_check_tick(
                    self.db, self.config,
                    now_fn=lambda: 1700000300.0,
                    mono_fn=mono_c,
                    tick_budget=0.25,
                    wake_pass_budget=0.10,
                    commit_busy_timeout_ms=60,
                    deliver_fn=lambda c, t: True,
                )
        finally:
            cleanup_c()
        self.assertEqual((rc_c, body_c['status']), (1, 'failed'))
        self.assertIsNone(body_c['unnotified_tasks'])
        self.assertIn('error', body_c)
        self.assertIn('failed to finalize check tick', err_c.getvalue())

    def test_mirror_sync_db_contention_finalizes_failed_tick_without_running_checks(self):
        import threading

        ran_marker = self.root / 'mirror_fail_should_not_run.marker'
        self.commit_config(
            '[[checks]]\nname = "c1"\ninterval = 60\ncommand = "scripts/mark.sh"\ntimeout = 10\nskill = "s1"\n',
            {'scripts/mark.sh': f'#!/bin/sh\necho ran > {str(ran_marker)!r}\nexit 0\n'},
        )
        # DB を初期化しておく
        run_check_tick(self.db, self.config, now_fn=lambda: 1700000000.0, deliver_fn=lambda c, t: True)
        if ran_marker.exists():
            ran_marker.unlink()

        def start_writer_lock():
            acquired = threading.Event()
            release = threading.Event()

            def _worker():
                conn = sqlite3.connect(self.db, timeout=5.0, isolation_level=None)
                try:
                    conn.execute('BEGIN IMMEDIATE')
                    acquired.set()
                    release.wait(timeout=5.0)
                finally:
                    with contextlib.suppress(sqlite3.Error):
                        conn.execute('ROLLBACK')
                    conn.close()

            thread = threading.Thread(target=_worker, daemon=True)
            thread.start()
            self.assertTrue(acquired.wait(timeout=3.0))
            return thread, release

        orig_sync = sys.modules['app'].sync_checks_mirror

        # (a) mirror 同期が DB 競合で失敗した後、finalization 前に writer が解放される場合:
        #     例外送出せず status='failed' (exit 1, checks_deferred=None) へ最終更新し、
        #     running 行を残さず、check の選択・起動を行わないこと
        holder_a = []

        def cleanup_a():
            for t, rel in holder_a:
                rel.set()
                t.join(timeout=3.0)

        self.addCleanup(cleanup_a)

        def locked_sync(*args, **kwargs):
            t, rel = start_writer_lock()
            holder_a.append((t, rel))
            try:
                return orig_sync(*args, **kwargs)
            finally:
                rel.set()
                t.join(timeout=3.0)

        select_called_a = []
        run_called_a = []
        err_a = io.StringIO()
        with mock.patch('app.sync_checks_mirror', side_effect=locked_sync), \
                mock.patch('app.select_due_checks', side_effect=lambda *a, **kw: select_called_a.append(True)), \
                mock.patch('app.run_check_command', side_effect=lambda *a, **kw: run_called_a.append(True)), \
                contextlib.redirect_stderr(err_a):
            body_a, rc_a = run_check_tick(
                self.db, self.config,
                now_fn=lambda: 1700000100.0,
                tick_budget=10.0,
                commit_busy_timeout_ms=80,
                deliver_fn=lambda c, t: True,
            )

        self.assertEqual((rc_a, body_a['status']), (1, 'failed'))
        self.assertIsNone(body_a['checks_deferred'])
        self.assertIn('error', body_a)
        self.assertEqual(select_called_a, [], 'select_due_checks must not run after mirror sync failure')
        self.assertEqual(run_called_a, [], 'run_check_command must not run after mirror sync failure')
        self.assertFalse(ran_marker.exists())
        self.assertIn('failed to sync checks mirror', err_a.getvalue())
        self.assertEqual(self.query_all("SELECT * FROM check_ticks WHERE status='running'"), [])
        tick_a = self.query_one('SELECT status, finished_at, checks_deferred FROM check_ticks WHERE id=?', (body_a['tick_id'],))
        self.assertEqual(tick_a['status'], 'failed')
        self.assertIsNotNone(tick_a['finished_at'])
        self.assertIsNone(tick_a['checks_deferred'])

        # (b) mirror 同期から finalization まで一貫してロックされ続ける場合も
        #     例外送出せず failed (exit 1) を返し、check を起動しないこと
        holder_b = []

        def cleanup_b():
            for t, rel in holder_b:
                rel.set()
                t.join(timeout=3.0)

        self.addCleanup(cleanup_b)
        anchor_b = [None]

        def mono_b():
            if anchor_b[0] is None:
                return 0.0
            return time.monotonic() - anchor_b[0]

        def locked_sync_through_finalize(*args, **kwargs):
            t, rel = start_writer_lock()
            holder_b.append((t, rel))
            anchor_b[0] = time.monotonic()
            return orig_sync(*args, **kwargs)

        select_called_b = []
        run_called_b = []
        err_b = io.StringIO()
        try:
            with mock.patch('app.sync_checks_mirror', side_effect=locked_sync_through_finalize), \
                    mock.patch('app.select_due_checks', side_effect=lambda *a, **kw: select_called_b.append(True)), \
                    mock.patch('app.run_check_command', side_effect=lambda *a, **kw: run_called_b.append(True)), \
                    contextlib.redirect_stderr(err_b):
                body_b, rc_b = run_check_tick(
                    self.db, self.config,
                    now_fn=lambda: 1700000200.0,
                    mono_fn=mono_b,
                    tick_budget=0.25,
                    commit_busy_timeout_ms=60,
                    deliver_fn=lambda c, t: True,
                )
        finally:
            cleanup_b()

        self.assertEqual((rc_b, body_b['status']), (1, 'failed'))
        self.assertIsNone(body_b['checks_deferred'])
        self.assertIsNone(body_b['unnotified_tasks'])
        self.assertIn('error', body_b)
        self.assertEqual(select_called_b, [])
        self.assertEqual(run_called_b, [])
        self.assertFalse(ran_marker.exists())
        self.assertIn('failed to sync checks mirror', err_b.getvalue())
        self.assertIn('failed to finalize check tick', err_b.getvalue())

    def test_unified_budget_defers_unpersistable_checks_and_bounds_first_check_exemption(self):
        # 1. 2 件目以降: timeout が tick_deadline / end_work_deadline に収まっても
        #    commit_reserve を残せない check は着手せず deferred とし、ワンタイムイベントを失わないこと。
        #    後続の軽い check (exec_deadline 内に収まる) は実行して保存できること。
        c2_invocations = self.root / 'c2_invocations.txt'
        self.commit_config(
            '[[checks]]\nname = "c1"\ninterval = 60\ncommand = "scripts/fast.sh"\ntimeout = 2\nskill = "s1"\n'
            '[[checks]]\nname = "c2_border"\ninterval = 60\ncommand = "scripts/c2_onetime.py"\ntimeout = 5\nskill = "s1"\n'
            '[[checks]]\nname = "c3_light"\ninterval = 60\ncommand = "scripts/c3_emit.py"\ntimeout = 3\nskill = "s1"\n',
            {
                'scripts/fast.sh': '#!/bin/sh\nexit 0\n',
                'scripts/c2_onetime.py': (
                    '#!/usr/bin/env python3\nimport json, pathlib\n'
                    f'p = pathlib.Path({str(c2_invocations)!r})\n'
                    'p.write_text("1")\n'
                    'print(json.dumps({"key": "k_c2", "title": "c2 event", "body": "once"}))\n'
                ),
                'scripts/c3_emit.py': (
                    '#!/usr/bin/env python3\nimport json\n'
                    'print(json.dumps({"key": "k_c3", "title": "c3 event", "body": "stored"}))\n'
                ),
            },
        )
        # tick_budget=100.0, finalize_reserve=2.0 -> end_work_deadline=1098.0
        # commit_reserve=2.0 -> commit_start_deadline=1096.0
        # default kill_grace=5.0 + CHECK_KILL_WAIT_SEC=1.0 + CHECK_DRAIN_JOIN_SEC=0.5 -> cleanup_reserve=6.5
        # exec_deadline=1089.5
        # c1 完了後に clock=1085.5 とすると:
        #   tick_deadline まで 14.5s (>= 5s), end_work_deadline まで 12.5s (>= 5s),
        #   commit_start_deadline まで 10.5s (>= 5s),
        #   しかし exec_deadline まで 4.0s (< c2_border.timeout=5s) -> c2_border は見送り!
        #   c3_light (timeout=3s <= 4.0s) は着手し、timeout ギリギリ (3s 消費して 1088.5) で完了しても
        #   end_work_deadline (1098.0) まで 9.5s 残るため保存に成功する!
        clock1 = FakeClock(wall=1700000000.0, mono=1000.0)
        orig_run = run_check_command

        def advance_after_checks(chk, *args, **kwargs):
            res = orig_run(chk, *args, **kwargs)
            if chk['name'] == 'c1':
                clock1.advance(85.5)
            elif chk['name'] == 'c3_light':
                clock1.advance(3.0)
            return res

        with mock.patch('app.run_check_command', side_effect=advance_after_checks):
            body1, rc1 = run_check_tick(
                self.db, self.config,
                now_fn=clock1.now,
                mono_fn=clock1.monotonic,
                tick_budget=100.0,
                finalize_reserve=2.0,
                commit_reserve=2.0,
                deliver_fn=lambda c, t: True,
            )
        self.assertEqual((rc1, body1['status']), (0, 'ok'))
        self.assertEqual(body1['checks_deferred'], 1)
        self.assertFalse(c2_invocations.exists(), 'c2_border must not run when commit window would be lost')
        runs1 = [(r['check_name'], r['outcome']) for r in self.query_all('SELECT check_name, outcome FROM check_runs ORDER BY id')]
        self.assertEqual(runs1, [('c1', 'ok_silent'), ('c3_light', 'fired')])
        self.assertEqual(len(self.query_all("SELECT * FROM check_tasks WHERE dedup_key='k_c3'")), 1)

        # 2. 先頭免除 (idx == 0) も exec_deadline 内でのみ許可され、
        #    cleanup_reserve / commit_reserve / finalize_reserve 内では起動しないこと
        for case_label, advance_sec, tick_wall in (
            ('inside_cleanup_reserve', 90.0, 1700000100.0),
            ('inside_commit_reserve', 96.5, 1700000200.0),
            ('inside_finalize_reserve', 98.5, 1700000400.0),
        ):
            with self.subTest(case=case_label):
                with contextlib.closing(sqlite3.connect(self.db)) as conn, conn:
                    conn.execute('DELETE FROM check_runs')
                    conn.execute("UPDATE checks SET last_started=NULL, last_actual_started_at=NULL, last_outcome=NULL")
                if c2_invocations.exists():
                    c2_invocations.unlink()
                clock2 = FakeClock(wall=tick_wall, mono=2000.0)
                orig_sync = sys.modules['app'].sync_checks_mirror
                spawned = []

                def advance_before_step6(*args, **kwargs):
                    res = orig_sync(*args, **kwargs)
                    clock2.advance(advance_sec)
                    return res

                def spy_run(*args, **kwargs):
                    spawned.append(args[0]['name'])
                    return orig_run(*args, **kwargs)

                with mock.patch('app.sync_checks_mirror', side_effect=advance_before_step6), \
                        mock.patch('app.run_check_command', side_effect=spy_run):
                    body2, rc2 = run_check_tick(
                        self.db, self.config,
                        now_fn=clock2.now,
                        mono_fn=clock2.monotonic,
                        tick_budget=100.0,
                        finalize_reserve=2.0,
                        commit_reserve=2.0,
                        deliver_fn=lambda c, t: True,
                    )
                self.assertEqual((rc2, body2['status']), (0, 'aborted'))
                self.assertEqual(body2['checks_deferred'], 3)
                self.assertEqual(spawned, [], 'first-check exemption must not run after exec_deadline')
                self.assertEqual(self.query_all('SELECT * FROM check_runs'), [])
                self.assertEqual(self.query_one('SELECT status FROM check_ticks WHERE id=?', (body2['tick_id'],))['status'], 'aborted')

    def test_sigterm_ignoring_first_check_at_default_reserves_still_commits_aborted_run(self):
        # 既定の tick_budget (240s) / kill_grace (5s) / commit_reserve (2s) / finalize_reserve (2s) で、
        # SIGTERM を無視する先頭 check が exec_deadline (229.5s) に到達しても、
        # cleanup が commit_start_deadline (236s) 内に収まり、aborted run と出力抜粋が
        # end_work_deadline (238s) 前に commit され status='aborted' (exit 0) で完了すること。
        ready_flag = self.root / 'ignore_sigterm.ready'
        pid_file = self.root / 'ignore_sigterm.pid'
        self.addCleanup(_cleanup_pid_file, pid_file)
        script = (
            '#!/usr/bin/env python3\n'
            'import os, pathlib, signal, sys, time\n'
            'signal.signal(signal.SIGTERM, signal.SIG_IGN)\n'
            f'pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()), encoding="utf-8")\n'
            'print("partial stdout before abort", flush=True)\n'
            'print("partial stderr before abort", file=sys.stderr, flush=True)\n'
            f'pathlib.Path({str(ready_flag)!r}).write_text("ready", encoding="utf-8")\n'
            'time.sleep(60)\n'
        )
        self.commit_config(
            '[[checks]]\nname = "c1_ign"\ninterval = 60\ncommand = "scripts/ign.py"\ntimeout = 60\nskill = "s1"\n'
            '[[checks]]\nname = "c2_after"\ninterval = 60\ncommand = "scripts/ok.sh"\ntimeout = 10\nskill = "s1"\n',
            {
                'scripts/ign.py': script,
                'scripts/ok.sh': '#!/bin/sh\nexit 0\n',
            },
        )

        # setup 中は mono=0.0、c1_ign 起動直前は 229.0 (exec_deadline=229.5 の 0.5s 手前)。
        # プロセスが SIG_IGN 設定と出力 flush を完了した時点で exec_deadline 0.1s 手前 (229.4) に合わせ、
        # 以降は実時間の経過をそのまま mono_fn に反映する。
        phase = ['setup']
        real_origin = [None]
        orig_run = run_check_command
        orig_popen = subprocess.Popen
        orig_commit = commit_check_run
        commit_mono_at = []

        def mono_fn():
            if phase[0] == 'setup':
                return 0.0
            if real_origin[0] is None:
                return 229.0
            return 229.4 + (time.monotonic() - real_origin[0])

        def start_check_phase(chk, *args, **kwargs):
            if chk['name'] == 'c1_ign':
                phase[0] = 'check'
            return orig_run(chk, *args, **kwargs)

        def popen_wait_ready(argv, *args, **kwargs):
            proc = orig_popen(argv, *args, **kwargs)
            if any('ign.py' in str(part) for part in argv):
                try:
                    dl = time.monotonic() + 5.0
                    while not ready_flag.exists() and time.monotonic() < dl:
                        time.sleep(0.01)
                    self.assertTrue(ready_flag.exists(), 'c1_ign did not become ready in time')
                    real_origin[0] = time.monotonic()
                except BaseException:
                    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                        os.killpg(proc.pid, signal.SIGKILL)
                    if proc.poll() is None:
                        with contextlib.suppress(OSError):
                            proc.kill()
                    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                        proc.wait(timeout=2.0)
                    for stream in (proc.stdin, proc.stdout, proc.stderr):
                        if stream is not None:
                            with contextlib.suppress(OSError, ValueError):
                                stream.close()
                    raise
            return proc

        def spy_commit(*args, **kwargs):
            commit_mono_at.append(mono_fn())
            return orig_commit(*args, **kwargs)

        with mock.patch('app.run_check_command', side_effect=start_check_phase), \
                mock.patch('app.subprocess.Popen', side_effect=popen_wait_ready), \
                mock.patch('app.commit_check_run', side_effect=spy_commit):
            body, rc = run_check_tick(
                self.db, self.config,
                now_fn=lambda: 1700000000.0,
                mono_fn=mono_fn,
                deliver_fn=lambda c, t: True,
            )

        elapsed_real = time.monotonic() - real_origin[0]
        self.assertGreaterEqual(elapsed_real, 4.5, 'default 5s kill_grace must actually run against SIGTERM-ignoring check')
        self.assertEqual((rc, body['status']), (0, 'aborted'))
        self.assertEqual(body['checks_deferred'], 1)
        self.assertEqual(len(commit_mono_at), 1)
        self.assertLess(commit_mono_at[0], 236.0, 'cleanup must finish within commit_start_deadline (236s)')
        self.assertLess(commit_mono_at[0], 238.0, 'aborted run commit must start before end_work_deadline')
        self.assertLess(mono_fn(), 240.0, 'tick must finish within nominal tick_deadline')

        run_row = self.query_one("SELECT outcome, exit_code, stdout_excerpt, stderr_excerpt FROM check_runs WHERE check_name='c1_ign'")
        self.assertIsNotNone(run_row)
        self.assertEqual((run_row['outcome'], run_row['exit_code']), ('aborted', None))
        self.assertIn('partial stdout before abort', run_row['stdout_excerpt'])
        self.assertIn('partial stderr before abort', run_row['stderr_excerpt'])
        chk_row = self.query_one("SELECT last_started, last_outcome, fault_count FROM checks WHERE name='c1_ign'")
        self.assertEqual((chk_row['last_started'], chk_row['last_outcome'], chk_row['fault_count']), (None, None, 0))
        tick_row = self.query_one('SELECT status, checks_deferred FROM check_ticks WHERE id=?', (body['tick_id'],))
        self.assertEqual((tick_row['status'], tick_row['checks_deferred']), ('aborted', 1))
        ign_pid = int(pid_file.read_text(encoding='utf-8').strip())
        self.assertFalse(_pid_alive(ign_pid))

    def test_terminate_process_group_enforces_absolute_deadline_under_delays(self):
        # 1. 定時開始 (229.5s, deadline=236.0s, grace=5.0, kill_wait=1.0, tail=0.5) は設定どおりの猶予を維持
        # 2. 開始遅延 (+233.5s) でも各フェーズの待ち上限が絶対期限 236.0s を超えない
        # 3. term_start 取得直後の遅延でも後から取った時刻に grace を足さず、term_deadline - now で上限される
        # 4. 既に deadline 超過 (+236.5s) のときは待ち時間 0 で即時 SIGKILL へ進む
        def run_fake_term(clock_seq, *, deadline, grace=5.0, kill_wait=1.0, tail=0.5, real_seq=None):
            idx = [0]
            real_idx = [0]
            cur = [clock_seq[0]]
            signals_sent = []
            wait_calls = []

            def seq_mono():
                i = idx[0]
                idx[0] += 1
                val = clock_seq[i] if i < len(clock_seq) else clock_seq[-1]
                cur[0] = val
                return val

            def seq_real():
                if real_seq is None:
                    return cur[0]
                i = real_idx[0]
                real_idx[0] += 1
                return real_seq[i] if i < len(real_seq) else real_seq[-1]

            class StubProc:
                pid = 888888
                returncode = None

                def poll(self):
                    return None

                def wait(self, timeout=None):
                    wait_calls.append(timeout)
                    raise subprocess.TimeoutExpired(cmd=['stub'], timeout=timeout)

            err_buf = io.StringIO()
            with mock.patch('app.os.killpg', side_effect=lambda pg, sig: signals_sent.append(sig)), \
                    mock.patch('app._pgid_alive', return_value=False), \
                    mock.patch('app.time.monotonic', side_effect=seq_real), \
                    contextlib.redirect_stderr(err_buf):
                _terminate_process_group(
                    StubProc(), grace, kill_wait_sec=kill_wait,
                    deadline=deadline, tail_reserve_sec=tail, mono_fn=seq_mono,
                )
            return signals_sent, wait_calls

        # (a) 定時開始 229.5s: TERM wait は 5.0s、SIGKILL wait は 1.0s
        sigs_a, waits_a = run_fake_term(
            [229.5, 229.5, 234.5, 234.5],
            deadline=236.0,
        )
        self.assertEqual(sigs_a, [signal.SIGTERM, signal.SIGKILL])
        self.assertEqual(len(waits_a), 2)
        self.assertAlmostEqual(waits_a[0], 5.0, places=3)
        self.assertAlmostEqual(waits_a[1], 1.0, places=3)

        # (b) 開始遅延 233.5s (残り 2.5s): 比率縮小 + 絶対期限 236.0s 内に収まる
        sigs_b, waits_b = run_fake_term(
            [233.5, 233.5, 235.423, 235.423],
            deadline=236.0,
        )
        self.assertEqual(sigs_b, [signal.SIGTERM, signal.SIGKILL])
        self.assertEqual(len(waits_b), 2)
        self.assertLessEqual(233.5 + waits_b[0], 236.0)
        self.assertAlmostEqual(waits_b[0], 5.0 * (2.5 / 6.5), places=3)
        self.assertLessEqual(235.423 + waits_b[1], 236.0)
        self.assertAlmostEqual(waits_b[1], 1.0 * (2.5 / 6.5), places=3)

        # (c) term_start (229.5s) と term_real_deadline / loop 先頭の間に 2.0s の遅延が挟まっても、
        #     term_deadline (234.5s) が後ろへ延びず wait timeout は 3.0s (234.5 - 231.5) になること
        sigs_c, waits_c = run_fake_term(
            [229.5, 231.5, 235.8, 235.8],
            real_seq=[231.5, 231.5, 235.8, 235.8],
            deadline=236.0,
        )
        self.assertEqual(sigs_c, [signal.SIGTERM, signal.SIGKILL])
        self.assertEqual(len(waits_c), 2)
        self.assertAlmostEqual(waits_c[0], 3.0, places=3)
        # SIGKILL 開始が 235.8s まで遅れても kill_wait は 236.0 - 235.8 = 0.2s に上限される
        self.assertAlmostEqual(waits_c[1], 0.2, places=3)

        # (d) 既に deadline 超過 (236.5s): wait() を呼ばず即時 SIGTERM -> SIGKILL で抜ける
        sigs_d, waits_d = run_fake_term([236.5, 236.5, 236.5, 236.5], deadline=236.0)
        self.assertEqual(sigs_d, [signal.SIGTERM, signal.SIGKILL])
        self.assertEqual(waits_d, [])

    def test_delayed_cleanup_helper_start_still_finishes_by_cleanup_deadline_and_commits_aborted_run(self):
        # _terminate_process_group 開始前に 4.0s 遅延して +233.5s から cleanup が始まっても、
        # 絶対期限 cleanup_deadline (236.0s = commit_start_deadline) までに cleanup が終わり、
        # aborted run が end_work_deadline (238.0s) 前に確実に commit されること。
        ready_flag = self.root / 'delayed_ign.ready'
        pid_file = self.root / 'delayed_ign.pid'
        self.addCleanup(_cleanup_pid_file, pid_file)
        script = (
            '#!/usr/bin/env python3\n'
            'import os, pathlib, signal, sys, time\n'
            'signal.signal(signal.SIGTERM, signal.SIG_IGN)\n'
            f'pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()), encoding="utf-8")\n'
            'print("delayed abort stdout", flush=True)\n'
            f'pathlib.Path({str(ready_flag)!r}).write_text("ready", encoding="utf-8")\n'
            'time.sleep(60)\n'
        )
        self.commit_config(
            '[[checks]]\nname = "c1_delay"\ninterval = 60\ncommand = "scripts/ign.py"\ntimeout = 60\nskill = "s1"\n',
            {'scripts/ign.py': script},
        )

        phase = ['setup']
        real_origin = [None]
        delay_injected = [False]
        delay_base_real = [None]
        orig_run = run_check_command
        orig_popen = subprocess.Popen
        orig_terminate = _terminate_process_group
        orig_commit = commit_check_run
        commit_mono_at = []

        def mono_fn():
            if phase[0] == 'setup':
                return 0.0
            if delay_injected[0]:
                # _terminate_process_group 開始時に +233.5s (4.0s 遅延) へ進めた後、実時間で進む
                return 233.5 + (time.monotonic() - delay_base_real[0])
            if real_origin[0] is None:
                return 229.0
            return 229.45 + (time.monotonic() - real_origin[0])

        def start_check_phase(chk, *args, **kwargs):
            phase[0] = 'check'
            return orig_run(chk, *args, **kwargs)

        def popen_wait_ready(argv, *args, **kwargs):
            proc = orig_popen(argv, *args, **kwargs)
            if any('ign.py' in str(part) for part in argv):
                try:
                    dl = time.monotonic() + 5.0
                    while not ready_flag.exists() and time.monotonic() < dl:
                        time.sleep(0.01)
                    self.assertTrue(ready_flag.exists(), 'c1_delay did not become ready in time')
                    real_origin[0] = time.monotonic()
                except BaseException:
                    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                        os.killpg(proc.pid, signal.SIGKILL)
                    if proc.poll() is None:
                        with contextlib.suppress(OSError):
                            proc.kill()
                    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                        proc.wait(timeout=2.0)
                    for stream in (proc.stdin, proc.stdout, proc.stderr):
                        if stream is not None:
                            with contextlib.suppress(OSError, ValueError):
                                stream.close()
                    raise
            return proc

        def delayed_terminate(*args, **kwargs):
            # helper 開始直前に +233.5s まで遅延が発生した状況を再現
            delay_base_real[0] = time.monotonic()
            delay_injected[0] = True
            return orig_terminate(*args, **kwargs)

        def spy_commit(*args, **kwargs):
            commit_mono_at.append(mono_fn())
            return orig_commit(*args, **kwargs)

        with mock.patch('app.run_check_command', side_effect=start_check_phase), \
                mock.patch('app.subprocess.Popen', side_effect=popen_wait_ready), \
                mock.patch('app._terminate_process_group', side_effect=delayed_terminate), \
                mock.patch('app.commit_check_run', side_effect=spy_commit):
            body, rc = run_check_tick(
                self.db, self.config,
                now_fn=lambda: 1700000000.0,
                mono_fn=mono_fn,
                deliver_fn=lambda c, t: True,
            )

        self.assertEqual((rc, body['status']), (0, 'aborted'))
        self.assertEqual(len(commit_mono_at), 1)
        self.assertLessEqual(
            commit_mono_at[0], 236.0,
            'delayed cleanup must still finish by cleanup_deadline (236s) so full commit window remains',
        )
        self.assertLess(commit_mono_at[0], 238.0)
        run_row = self.query_one("SELECT outcome, exit_code, stdout_excerpt FROM check_runs WHERE check_name='c1_delay'")
        self.assertIsNotNone(run_row)
        self.assertEqual((run_row['outcome'], run_row['exit_code']), ('aborted', None))
        self.assertIn('delayed abort stdout', run_row['stdout_excerpt'])
        ign_pid = int(pid_file.read_text(encoding='utf-8').strip())
        self.assertFalse(_pid_alive(ign_pid))

    def test_mirror_sync_busy_timeout_is_capped_by_remaining_work_budget_and_skips_when_exhausted(self):
        # 1. 検証が +237s で終わった場合、mirror の busy_timeout_ms は 5000ms ではなく
        #    end_work_deadline (238s) までの残り 1000ms に cap されること (余裕があるときは 5000ms のまま)。
        # 2. 実 SQLite 書き込みロック競合下でも mirror 待ちが残り作業時間で打ち切られ、
        #    finalize_reserve 内にロック解放されれば running を残さず failed へ最終更新できること。
        # 3. end_work_deadline を既に超過している場合は mirror 書き込みを開始せず、
        #    構造化された finalization (status='aborted', checks_deferred=None) で終了すること。
        self.commit_config(
            '[[checks]]\nname = "c1"\ninterval = 60\ncommand = "scripts/ok.sh"\ntimeout = 10\nskill = "s1"\n',
            {'scripts/ok.sh': '#!/bin/sh\nexit 0\n'},
        )
        orig_validate = sys.modules['app'].load_and_validate_checks_config
        orig_sync = sys.modules['app'].sync_checks_mirror

        # (a) 正常経路 (余裕あり): busy_timeout_ms は既定の 5000ms のまま
        seen_busy_normal = []
        clock_norm = FakeClock(wall=1700000000.0, mono=0.0)

        def spy_sync_norm(*args, **kwargs):
            seen_busy_normal.append(kwargs.get('busy_timeout_ms'))
            return orig_sync(*args, **kwargs)

        with mock.patch('app.sync_checks_mirror', side_effect=spy_sync_norm):
            body_n, rc_n = run_check_tick(
                self.db, self.config,
                now_fn=clock_norm.now,
                mono_fn=clock_norm.monotonic,
                tick_budget=240.0,
                finalize_reserve=2.0,
                commit_busy_timeout_ms=5000,
                deliver_fn=lambda c, t: True,
            )
        self.assertEqual((rc_n, body_n['status']), (0, 'ok'))
        self.assertEqual(seen_busy_normal, [5000])

        # (b) 検証が +237.0s で終了 -> end_work_deadline (238.0s) まで残り 1.0s なので busy_timeout_ms=1000
        seen_busy_tight = []
        clock_tight = FakeClock(wall=1700000100.0, mono=0.0)

        def validate_ending_at_237(*args, **kwargs):
            res = orig_validate(*args, **kwargs)
            clock_tight.advance(237.0)
            return res

        def spy_sync_tight(*args, **kwargs):
            seen_busy_tight.append(kwargs.get('busy_timeout_ms'))
            return orig_sync(*args, **kwargs)

        with mock.patch('app.load_and_validate_checks_config', side_effect=validate_ending_at_237), \
                mock.patch('app.sync_checks_mirror', side_effect=spy_sync_tight):
            body_t, rc_t = run_check_tick(
                self.db, self.config,
                now_fn=clock_tight.now,
                mono_fn=clock_tight.monotonic,
                tick_budget=240.0,
                finalize_reserve=2.0,
                commit_busy_timeout_ms=5000,
                deliver_fn=lambda c, t: True,
            )
        self.assertEqual(seen_busy_tight, [1000])
        self.assertEqual((rc_t, body_t['status']), (0, 'aborted'))
        self.assertEqual(self.query_all("SELECT * FROM check_ticks WHERE status='running'"), [])

        # (c) 実 SQLite ロック競合: commit_busy_timeout_ms=5000 でも残り作業時間 80ms で cap され、
        #     mirror 失敗後に writer が解放すれば finalize_reserve 内で status='failed' へ確定できること
        import threading
        lock_acq = threading.Event()
        lock_rel = threading.Event()
        holder = []

        def _hold_writer():
            conn = sqlite3.connect(self.db, timeout=5.0, isolation_level=None)
            try:
                conn.execute('BEGIN IMMEDIATE')
                lock_acq.set()
                lock_rel.wait(timeout=5.0)
            finally:
                with contextlib.suppress(sqlite3.Error):
                    conn.execute('ROLLBACK')
                conn.close()

        def _cleanup_holder():
            lock_rel.set()
            for t in holder:
                t.join(timeout=3.0)

        self.addCleanup(_cleanup_holder)
        admission_reached_c = [False]
        anchor_c = [None]
        seen_busy_c = []
        mirror_elapsed_c = []

        def mono_c():
            # tick_budget=2.5, finalize_reserve=0.5 -> end_work_deadline=2.0。
            # admission (mirror_rem の確認) が終わるまでは 1.92 (残り 80ms) に固定し、
            # sync に入った時点から実時間で進める。検証や writer 取得のスケジューリング
            # 遅延が mirror の残量判定に影響しないようにする。
            if not admission_reached_c[0]:
                return 0.0
            if anchor_c[0] is None:
                return 1.92
            return 1.92 + (time.monotonic() - anchor_c[0])

        def validate_then_lock(*args, **kwargs):
            res = orig_validate(*args, **kwargs)
            t = threading.Thread(target=_hold_writer, daemon=True)
            holder.append(t)
            t.start()
            self.assertTrue(lock_acq.wait(timeout=3.0))
            admission_reached_c[0] = True
            return res

        def sync_then_release_for_finalize(*args, **kwargs):
            seen_busy_c.append(kwargs.get('busy_timeout_ms'))
            started_sync = time.monotonic()
            anchor_c[0] = started_sync
            try:
                return orig_sync(*args, **kwargs)
            finally:
                mirror_elapsed_c.append(time.monotonic() - started_sync)
                _cleanup_holder()

        err_c = io.StringIO()
        try:
            with mock.patch('app.load_and_validate_checks_config', side_effect=validate_then_lock), \
                    mock.patch('app.sync_checks_mirror', side_effect=sync_then_release_for_finalize), \
                    contextlib.redirect_stderr(err_c):
                body_c, rc_c = run_check_tick(
                    self.db, self.config,
                    now_fn=lambda: 1700000200.0,
                    mono_fn=mono_c,
                    tick_budget=2.5,
                    finalize_reserve=0.5,
                    commit_busy_timeout_ms=5000,
                    deliver_fn=lambda c, t: True,
                )
        finally:
            _cleanup_holder()
        self.assertEqual((rc_c, body_c['status']), (1, 'failed'))
        self.assertIsNone(body_c['checks_deferred'])
        self.assertEqual(len(seen_busy_c), 1)
        self.assertLessEqual(seen_busy_c[0], 80)
        self.assertEqual(len(mirror_elapsed_c), 1)
        self.assertLess(
            mirror_elapsed_c[0], 1.0,
            'mirror wait must not spend full 5000ms busy timeout',
        )
        self.assertEqual(self.query_all("SELECT * FROM check_ticks WHERE status='running'"), [])
        tick_c = self.query_one('SELECT status, finished_at FROM check_ticks WHERE id=?', (body_c['tick_id'],))
        self.assertEqual(tick_c['status'], 'failed')
        self.assertIsNotNone(tick_c['finished_at'])

        # (d) 検証終了時点で end_work_deadline (238.0s) を超過 (+238.5s):
        #     mirror 書き込みを一切開始せず、select_due_checks / run_check_command も呼ばずに
        #     status='aborted' (checks_deferred=None) で終了処理すること
        clock_over = FakeClock(wall=1700000300.0, mono=0.0)
        sync_called_over = []
        select_called_over = []
        run_called_over = []

        def validate_past_end_work(*args, **kwargs):
            res = orig_validate(*args, **kwargs)
            clock_over.advance(238.5)
            return res

        with mock.patch('app.load_and_validate_checks_config', side_effect=validate_past_end_work), \
                mock.patch('app.sync_checks_mirror', side_effect=lambda *a, **kw: sync_called_over.append(True)), \
                mock.patch('app.select_due_checks', side_effect=lambda *a, **kw: select_called_over.append(True)), \
                mock.patch('app.run_check_command', side_effect=lambda *a, **kw: run_called_over.append(True)):
            body_o, rc_o = run_check_tick(
                self.db, self.config,
                now_fn=clock_over.now,
                mono_fn=clock_over.monotonic,
                tick_budget=240.0,
                finalize_reserve=2.0,
                commit_busy_timeout_ms=5000,
                deliver_fn=lambda c, t: True,
            )
        self.assertEqual((rc_o, body_o['status']), (0, 'aborted'))
        self.assertIsNone(body_o['checks_deferred'])
        self.assertEqual(sync_called_over, [], 'mirror write must not start when no work budget remains')
        self.assertEqual(select_called_over, [])
        self.assertEqual(run_called_over, [])
        self.assertEqual(self.query_all("SELECT * FROM check_ticks WHERE status='running'"), [])
        tick_o = self.query_one('SELECT status, finished_at, checks_deferred FROM check_ticks WHERE id=?', (body_o['tick_id'],))
        self.assertEqual(tick_o['status'], 'aborted')
        self.assertIsNotNone(tick_o['finished_at'])
        self.assertIsNone(tick_o['checks_deferred'])


if __name__ == '__main__':
    unittest.main()
