"""更新事务的同 PID 恢复、WAL 快照和状态校验回归。

夹具只使用临时或演练数据；本地通过不代表真实官方授权。
Test: python3 -B -m unittest test_updates。"""
import json
import os
from pathlib import Path
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

import updates

MODULE_SOURCE = Path(updates.__file__).read_text(encoding='utf-8')


def embedded_prefix():
    return (
        'import os, sys, types, sqlite3, json\n'
        '_runtime = types.ModuleType("embedded_updates")\n'
        '_runtime.__file__ = __file__\n'
        'exec(compile(' + repr(MODULE_SOURCE) + ', "<embedded-updates>", "exec"), '
        '_runtime.__dict__)\n'
        '_runtime.recover_entry(__file__)\n'
        '_runtime.capture_image(__file__, globals().get("__emos_image__"))\n'
        '_runtime.capture_image(__file__)\n'
    )


LAUNCHER = (
    'import os, sys, types, tempfile, json, sqlite3\n'
    'entry, config, candidate, *dbs = sys.argv[1:]\n'
    'runtime = types.ModuleType("launcher_updates")\n'
    'exec(compile(' + repr(MODULE_SOURCE) + ', "<launcher-updates>", "exec"), '
    'runtime.__dict__)\n'
    'runtime.recover_entry(entry)\n'
    'runtime.capture_image(entry)\n'
    'runtime.mark_ready(entry, config)\n'
    'print("LAUNCH " + json.dumps({"pid": os.getpid()}), flush=True)\n'
    'with open(candidate, "rb") as handle:\n'
    '    candidate_bytes = handle.read()\n'
    'with open(entry, "wb") as handle:\n'
    '    handle.write(candidate_bytes)\n'
    'with tempfile.TemporaryFile() as image:\n'
    '    image.write(candidate_bytes)\n'
    '    image.seek(0)\n'
    '    runtime.exec_update(image, entry, config, dbs)\n'
)

FRESH_LAUNCHER = (
    'import os, sys, types, json\n'
    'entry = sys.argv[1]\n'
    'runtime = types.ModuleType("fresh_updates")\n'
    'exec(compile(' + repr(MODULE_SOURCE) + ', "<fresh-updates>", "exec"), '
    'runtime.__dict__)\n'
    'print("FRESH " + json.dumps({"pid": os.getpid()}), flush=True)\n'
    'runtime.recover_entry(entry)\n'
    'raise RuntimeError("pending recovery unexpectedly returned")\n'
)


class UpdateIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='emos-update-test-')
        self.root = Path(self.temporary.name)
        self.entry = self.root / 'single.py'
        self.config = self.root / 'config.env'
        self.database = self.root / 'data.db'
        self.target = self.root / 'target.db'
        self.config.write_bytes(b'OLD_CONFIG=private-test\n# preserve raw\r\n')
        os.chmod(self.config, 0o600)
        with sqlite3.connect(self.database) as connection:
            connection.execute('CREATE TABLE values_table (value TEXT)')
            connection.execute('INSERT INTO values_table VALUES (?)', ('old',))
        self.old = (embedded_prefix() +
                    '_config = os.environ["EMOS_ENV_FILE"]\n'
                    '_db = os.path.join(os.path.dirname(__file__), "data.db")\n'
                    'with sqlite3.connect(_db) as connection:\n'
                    '    values = [row[0] for row in connection.execute('
                    '"SELECT value FROM values_table ORDER BY rowid")]\n'
                    'with open(_config, "rb") as handle:\n'
                    '    config_raw = handle.read().hex()\n'
                    '_runtime.mark_ready(__file__, _config)\n'
                    'print("FALLBACK " + json.dumps({"pid": os.getpid(), '
                    '"config": config_raw, "values": values, '
                    '"active": os.environ.get("EMOS_UPDATE_ACTIVE")}), flush=True)\n'
                    ).encode('utf-8')
        self.entry.write_bytes(self.old)
        self.candidate_path = self.root / 'candidate.input'
        self.environment = dict(os.environ)
        self.environment['EMOS_ENV_FILE'] = str(self.config)
        self.environment.pop('EMOS_UPDATE_ACTIVE', None)
        self.environment['PYTHONDONTWRITEBYTECODE'] = '1'
        self.children = []

    def tearDown(self):
        for child in self.children:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=10)
        self.temporary.cleanup()

    @property
    def state_path(self):
        return self.root / '.single.py.update-state.json'

    @property
    def last_good(self):
        return self.root / '.single.py.last-good'

    def candidate(self, ending, delete_entry=False):
        before = 'import os\nos.unlink(__file__)\n' if delete_entry else ''
        return (before + embedded_prefix() +
                '_config = os.environ["EMOS_ENV_FILE"]\n'
                'with open(_config, "wb") as handle:\n'
                '    handle.write(b"NEW_CONFIG=secret-test\\n")\n'
                '_connections = []\n'
                'for filename in ("data.db", "target.db"):\n'
                '    connection = sqlite3.connect(os.path.join('
                'os.path.dirname(__file__), filename))\n'
                '    _connections.append(connection)\n'
                '    connection.execute("PRAGMA journal_mode=WAL")\n'
                '    connection.execute("CREATE TABLE IF NOT EXISTS values_table (value TEXT)")\n'
                '    connection.execute("DELETE FROM values_table")\n'
                '    connection.execute("INSERT INTO values_table VALUES (\'candidate\')")\n'
                '    connection.commit()\n'
                'print("CANDIDATE " + json.dumps({"pid": os.getpid()}), flush=True)\n' +
                ending).encode('utf-8')

    def launch(self, source):
        self.candidate_path.write_bytes(source)
        child = subprocess.Popen(
            [sys.executable, '-u', '-c', LAUNCHER, str(self.entry), str(self.config),
             str(self.candidate_path), str(self.database), str(self.target)],
            cwd=self.root, env=self.environment,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.children.append(child)
        return child

    def finish(self, child, expected_return=0):
        stdout, stderr = child.communicate(timeout=25)
        self.assertEqual(child.returncode, expected_return, stderr + stdout)
        self.assertNotIn('Traceback', stderr)
        self.assertNotIn('secret-test', stderr)
        self.assertNotIn('do-not-log', stderr)
        records = {}
        for line in stdout.splitlines():
            tag, _, payload = line.partition(' ')
            if tag in ('LAUNCH', 'CANDIDATE', 'FALLBACK', 'FRESH'):
                records[tag] = json.loads(payload)
        return records, stderr

    def assert_restored(self, child, expected_values=None):
        records, stderr = self.finish(child)
        self.assertEqual(records['LAUNCH']['pid'], child.pid)
        self.assertEqual(records['FALLBACK']['pid'], child.pid)
        if 'CANDIDATE' in records:
            self.assertEqual(records['CANDIDATE']['pid'], child.pid)
        self.assertEqual(records['FALLBACK']['values'], expected_values or ['old'])
        self.assertEqual(bytes.fromhex(records['FALLBACK']['config']),
                         b'OLD_CONFIG=private-test\n# preserve raw\r\n')
        self.assertIsNone(records['FALLBACK']['active'])
        self.assertEqual(self.entry.read_bytes(), self.old)
        self.assertEqual(self.last_good.read_bytes(), self.old)
        self.assertFalse(self.target.exists())
        self.assertFalse(Path(str(self.database) + '-wal').exists())
        self.assertFalse(Path(str(self.database) + '-shm').exists())
        state = json.loads(self.state_path.read_text())
        self.assertEqual(state['status'], 'rolledback')
        self.assertFalse(Path(state['stage']).exists())
        for path in (self.entry, self.config, self.database, self.state_path, self.last_good):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        return records, stderr

    def test_exception_before_ready_restores_same_pid_and_deleted_entry(self):
        source = self.candidate('raise RuntimeError("do-not-log secret-test")\n',
                                delete_entry=True)
        self.assert_restored(self.launch(source))

    def test_system_exit_zero_before_ready_rolls_back(self):
        self.assert_restored(self.launch(self.candidate('raise SystemExit(0)\n')))

    def test_return_before_ready_rolls_back(self):
        self.assert_restored(self.launch(self.candidate('def main():\n    return\nmain()\n')))

    def test_exception_after_ready_commits_without_rollback(self):
        source = self.candidate('_runtime.mark_ready(__file__, _config)\n'
                                'raise RuntimeError("do-not-log secret-test")\n')
        child = self.launch(source)
        records, stderr = self.finish(child, expected_return=1)
        self.assertNotIn('FALLBACK', records)
        self.assertEqual(records['LAUNCH']['pid'], child.pid)
        self.assertEqual(records['CANDIDATE']['pid'], child.pid)
        self.assertEqual(self.entry.read_bytes(), source)
        self.assertEqual(self.last_good.read_bytes(), source)
        self.assertEqual(self.config.read_bytes(), b'NEW_CONFIG=secret-test\n')
        with sqlite3.connect(self.database) as connection:
            self.assertEqual(connection.execute('SELECT value FROM values_table').fetchall(),
                             [('candidate',)])
        state = json.loads(self.state_path.read_text())
        self.assertEqual(state['status'], 'committed')
        self.assertFalse(Path(state['stage']).exists())
        environment = dict(self.environment)
        script = ('import types; runtime=types.ModuleType("clean"); exec(' +
                  repr(MODULE_SOURCE) + ', runtime.__dict__); '
                  'assert runtime.recover_entry(' + repr(str(self.entry)) + ') is False')
        clean = subprocess.run([sys.executable, '-u', '-c', script], cwd=self.root,
                               env=environment, capture_output=True, text=True, timeout=10)
        self.assertEqual(clean.returncode, 0, clean.stderr)
        self.assertEqual(self.config.read_bytes(), b'NEW_CONFIG=secret-test\n')

    def test_system_exit_zero_after_ready_is_normal_exit(self):
        source = self.candidate('_runtime.mark_ready(__file__, _config)\n'
                                'raise SystemExit(0)\n')
        records, stderr = self.finish(self.launch(source))
        self.assertNotIn('FALLBACK', records)
        self.assertEqual(self.last_good.read_bytes(), source)
        self.assertEqual(json.loads(self.state_path.read_text())['status'], 'committed')

    def test_syntax_error_rolls_back_without_candidate_imports(self):
        self.assert_restored(self.launch(b'if:\n'))

    def test_wal_committed_rows_are_in_backup(self):
        # 保持 writer 打开，committed 数据只留在 WAL，不让 close 自动 checkpoint。
        writer = sqlite3.connect(self.database)
        try:
            writer.execute('PRAGMA journal_mode=WAL')
            writer.execute('PRAGMA wal_autocheckpoint=0')
            writer.execute('INSERT INTO values_table VALUES (?)', ('wal-only',))
            writer.commit()
            wal = Path(str(self.database) + '-wal')
            self.assertGreater(wal.stat().st_size, 32)
            self.assert_restored(self.launch(self.candidate('raise SystemExit(0)\n')),
                                 expected_values=['old', 'wal-only'])
        finally:
            writer.close()

    def test_sigkill_pending_is_recovered_by_fresh_launcher(self):
        source = self.candidate('import signal\n'
                                'print("WAITING", flush=True)\n'
                                'while True:\n    signal.pause()\n')
        child = self.launch(source)
        lines = []
        arrived = threading.Event()
        def consume():
            for line in child.stdout:
                lines.append(line)
                if line.strip() == 'WAITING':
                    arrived.set()
                    return
        reader = threading.Thread(target=consume, daemon=True)
        reader.start()
        self.assertTrue(arrived.wait(20), 'candidate did not reach pending startup')
        reader.join(timeout=2)
        state = json.loads(self.state_path.read_text())
        self.assertEqual(state['status'], 'pending')
        self.assertEqual(stat.S_IMODE(Path(state['stage']).stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.state_path.stat().st_mode), 0o600)
        for path in [state['source_copy'], state['config']['copy'],
                     state['databases'][0]['copy']]:
            self.assertEqual(stat.S_IMODE(Path(path).stat().st_mode), 0o600)
        self.assertFalse(state['databases'][1]['existed'])
        self.assertIsNone(state['databases'][1]['copy'])
        child.send_signal(signal.SIGKILL)
        unused_stdout, stderr = child.communicate(timeout=10)
        self.assertEqual(child.returncode, -signal.SIGKILL)
        self.assertNotIn('Traceback', stderr)
        launch_record = next(json.loads(line.partition(' ')[2]) for line in lines
                             if line.startswith('LAUNCH '))
        candidate_record = next(json.loads(line.partition(' ')[2]) for line in lines
                                if line.startswith('CANDIDATE '))
        self.assertEqual(launch_record['pid'], child.pid)
        self.assertEqual(candidate_record['pid'], child.pid)
        # fresh launcher 是 Docker/普通重启的新进程；其内部恢复 exec 不再换 PID。
        fresh = subprocess.Popen([sys.executable, '-u', '-c', FRESH_LAUNCHER,
                                  str(self.entry)], cwd=self.root, env=self.environment,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.children.append(fresh)
        records, stderr = self.finish(fresh)
        self.assertEqual(records['FRESH']['pid'], fresh.pid)
        self.assertEqual(records['FALLBACK']['pid'], fresh.pid)
        self.assertEqual(records['FALLBACK']['values'], ['old'])
        self.assertIsNone(records['FALLBACK']['active'])
        self.assertEqual(self.entry.read_bytes(), self.old)
        self.assertEqual(self.config.read_bytes(), b'OLD_CONFIG=private-test\n# preserve raw\r\n')
        state = json.loads(self.state_path.read_text())
        self.assertEqual(state['status'], 'rolledback')
        self.assertFalse(Path(state['stage']).exists())
        self.assertFalse(self.target.exists())

    def test_os_exit_pending_is_recovered_on_next_start(self):
        child = self.launch(self.candidate('os._exit(0)\n'))
        records, stderr = self.finish(child)
        self.assertNotIn('FALLBACK', records)
        self.assertEqual(json.loads(self.state_path.read_text())['status'], 'pending')
        fresh = subprocess.Popen([sys.executable, '-u', '-c', FRESH_LAUNCHER,
                                  str(self.entry)], cwd=self.root, env=self.environment,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.children.append(fresh)
        records, stderr = self.finish(fresh)
        self.assertEqual(records['FRESH']['pid'], fresh.pid)
        self.assertEqual(records['FALLBACK']['pid'], fresh.pid)
        self.assertEqual(self.entry.read_bytes(), self.old)
        self.assertEqual(records['FALLBACK']['values'], ['old'])


class UpdateValidationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='emos-update-validation-')
        self.root = Path(self.temporary.name)
        self.entry = self.root / 'entry.py'
        self.config = self.root / 'config.env'
        self.database = self.root / 'data.db'
        self.entry.write_bytes(b'print("old")\n')
        self.config.write_bytes(b'PRIVATE=test\n')
        os.chmod(self.config, 0o640)
        with sqlite3.connect(self.database) as connection:
            connection.execute('CREATE TABLE t (v TEXT)')
            connection.execute('INSERT INTO t VALUES ("old")')
        self.environment_patch = mock.patch.dict(os.environ,
                                                 {'EMOS_ENV_FILE': str(self.config)})
        self.environment_patch.start()
        os.environ.pop('EMOS_UPDATE_ACTIVE', None)
        self.entry_key = str(self.entry)

    def tearDown(self):
        updates._IMAGES.pop(self.entry_key, None)
        updates._READY_ENTRIES.discard(self.entry_key)
        self.environment_patch.stop()
        self.temporary.cleanup()

    @property
    def state_path(self):
        return self.root / '.entry.py.update-state.json'

    def prepare_ready(self):
        updates.capture_image(self.entry)
        updates.mark_ready(self.entry, self.config)

    def candidate_file(self):
        image = tempfile.TemporaryFile(dir=self.root)
        image.write(b'print("candidate")\n')
        image.seek(0)
        return image

    def test_capture_cached_when_disk_entry_deleted(self):
        source = b'print("memory source")\n'
        self.assertEqual(updates.capture_image(self.entry, source), source)
        self.entry.unlink()
        self.assertEqual(updates.capture_image(self.entry), source)
        last_good = updates.mark_ready(self.entry, self.config)
        self.assertEqual(Path(last_good).read_bytes(), source)

    def test_capture_limit_and_requires_explicit_ready(self):
        with self.assertRaises(RuntimeError):
            updates.capture_image(self.entry, b'x' * (2 * 1024 * 1024 + 1))
        updates.capture_image(self.entry)
        with self.assertRaises(RuntimeError):
            updates.exec_update(self.candidate_file(), self.entry, self.config, [self.database])
        self.assertFalse(self.state_path.exists())

    def test_exec_failure_cancels_pending_without_restoring_current_files(self):
        self.prepare_ready()
        before_config = self.config.read_bytes()
        before_database = self.database.read_bytes()
        self.entry.write_bytes(b'print("uploaded replacement")\n')
        image = self.candidate_file()
        with mock.patch.object(updates.os, 'execve', side_effect=OSError('do-not-log')), \
                self.assertRaisesRegex(OSError, '当前版本未替换'):
            updates.exec_update(image, self.entry, self.config, [self.database])
        self.assertTrue(image.closed)
        self.assertFalse(self.state_path.exists())
        self.assertEqual(self.entry.read_bytes(), b'print("uploaded replacement")\n')
        self.assertEqual(self.config.read_bytes(), before_config)
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o640)
        self.assertEqual(self.database.read_bytes(), before_database)
        self.assertFalse(any(path.name.startswith('.entry.py.update-stage-')
                             for path in self.root.iterdir()))

    def test_malformed_state_fails_closed_before_exec(self):
        self.state_path.write_bytes(b'{invalid PRIVATE=do-not-log')
        os.chmod(self.state_path, 0o600)
        with mock.patch.object(updates.os, 'execve') as execute, \
                self.assertRaises(RuntimeError) as raised:
            updates.recover_entry(self.entry)
        execute.assert_not_called()
        self.assertNotIn('PRIVATE', str(raised.exception))
        self.assertNotIn('do-not-log', str(raised.exception))
        self.assertTrue(self.state_path.exists())

    def test_wrong_state_permissions_fail_closed(self):
        self.state_path.write_bytes(b'{}')
        os.chmod(self.state_path, 0o644)
        with self.assertRaises(RuntimeError):
            updates.recover_entry(self.entry)

    def test_pending_state_path_and_entry_mismatch_fail_closed(self):
        self.prepare_ready()
        retained = {}
        def hold_exec(executable, argv, environment):
            retained['state'] = json.loads(self.state_path.read_text())
            raise OSError('hold')
        with mock.patch.object(updates.os, 'execve', side_effect=hold_exec), \
                self.assertRaises(OSError):
            updates.exec_update(self.candidate_file(), self.entry, self.config,
                                [self.database, self.root / 'new.db'])
        base = retained['state']
        for field, value in [('entry', str(self.root / 'wrong.py')),
                             ('source_copy', str(self.root / 'outside')),
                             ('config_path', str(self.root / 'wrong.env'))]:
            with self.subTest(field=field):
                state = dict(base, status='committed')
                state[field] = value
                self.state_path.write_text(json.dumps(state))
                os.chmod(self.state_path, 0o600)
                with self.assertRaises(RuntimeError):
                    updates.recover_entry(self.entry)
        state = dict(base, status='committed')
        state['databases'] = [dict(base['databases'][0], copy=str(self.root / 'outside'))]
        self.state_path.write_text(json.dumps(state))
        os.chmod(self.state_path, 0o600)
        with self.assertRaises(RuntimeError):
            updates.recover_entry(self.entry)

    def test_ready_requires_existing_matching_last_good_for_update(self):
        self.prepare_ready()
        last_good = self.root / '.entry.py.last-good'
        last_good.unlink()
        with self.assertRaises(OSError):
            updates.exec_update(self.candidate_file(), self.entry, self.config,
                                [self.database])
        last_good.write_bytes(b'print("wrong")\n')
        os.chmod(last_good, 0o600)
        with self.assertRaises(RuntimeError):
            updates.exec_update(self.candidate_file(), self.entry, self.config,
                                [self.database])
        self.assertFalse(self.state_path.exists())

    def test_no_active_pending_recovery_requires_matching_config_path(self):
        self.prepare_ready()
        retained = {}
        def hold_exec(executable, argv, environment):
            retained['state'] = json.loads(self.state_path.read_text())
            raise OSError('hold')
        with mock.patch.object(updates.os, 'execve', side_effect=hold_exec), \
                self.assertRaises(OSError):
            updates.exec_update(self.candidate_file(), self.entry, self.config, [])
        state = retained['state']
        stage = Path(state['stage'])
        stage.mkdir(mode=0o700)
        Path(state['source_copy']).write_bytes(b'print("old")\n')
        Path(state['config']['copy']).write_bytes(b'PRIVATE=test\n')
        os.chmod(state['source_copy'], 0o600)
        os.chmod(state['config']['copy'], 0o600)
        self.state_path.write_text(json.dumps(state))
        os.chmod(self.state_path, 0o600)
        # 正确 active nonce 的候选 recover 是 no-op，不会碰配置或重新 exec。
        with mock.patch.dict(os.environ, {'EMOS_UPDATE_ACTIVE': state['nonce']}), \
                mock.patch.object(updates.os, 'execve') as execute:
            self.assertFalse(updates.recover_entry(self.entry))
            execute.assert_not_called()
        self.assertEqual(json.loads(self.state_path.read_text())['status'], 'pending')

    def test_state_publication_failure_after_replace_cancels_pending(self):
        self.prepare_ready()
        original_write = updates._write_state
        def failed_write(path, state):
            original_write(path, state)
            raise OSError('do-not-log fsync failure')
        with mock.patch.object(updates, '_write_state', side_effect=failed_write), \
                self.assertRaises(OSError):
            updates.exec_update(self.candidate_file(), self.entry, self.config,
                                [self.database])
        self.assertFalse(self.state_path.exists())
        self.assertEqual(self.entry.read_bytes(), b'print("old")\n')
        self.assertFalse(any(path.name.startswith('.entry.py.update-stage-')
                             for path in self.root.iterdir()))

    def test_non_main_thread_update_is_rejected_and_image_closed(self):
        self.prepare_ready()
        image = self.candidate_file()
        errors = []
        def worker():
            try:
                updates.exec_update(image, self.entry, self.config, [self.database])
            except RuntimeError as error:
                errors.append(str(error))
        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertTrue(image.closed)
        self.assertFalse(self.state_path.exists())


if __name__ == '__main__':
    unittest.main()
