"""外置配置恢复、并发上传、登录代际、快照及停机唤醒回归。

仅用隔离目录、合成身份或 localhost；正式授权和真实生产资源不在此验收。
Test: python3 -B -m unittest test_handover_runtime"""
import queue
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from envconfig import EnvConfigError
from test_support import fixture_app


class HandoverRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='emos-handover-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = fixture_app(self, self.root / 'config', 'handover_runtime_fixture', single_owner=True)
        self.app.init_db()
        self.addCleanup(self.app.close_idle_db)

    def seed_accounts(self):
        with self.app.write_db() as conn:
            conn.execute("INSERT INTO accounts(id,name,token,emos_id,owner_tg_id,enabled,auto_sign,sign_time) VALUES(1,'primary','11_synthetic_primary','ePrimary',101,1,0,'01:00')")
            conn.execute("INSERT INTO accounts(id,name,token,emos_id,owner_tg_id,enabled,auto_sign,sign_time) VALUES(2,'extra','22_synthetic_extra','eExtra',101,1,1,'01:00')")
            conn.execute("INSERT INTO tg_users(tg_id,chat_id,primary_account_id,active_account_id) VALUES(101,101,1,2)")

    def test_deleted_entry_uses_snapshot_next_to_external_env(self):
        entry = self.root / 'runtime' / 'emoskata.py'
        entry.parent.mkdir()
        source = b'print("last good source")\n'
        good = self.app.CONFIG.path.parent / '.emoskata.py.last-good'
        good.write_bytes(source)
        good.chmod(0o600)
        self.assertFalse(entry.exists())
        with patch.object(self.app, '__file__', str(entry)):
            with self.assertRaises(EnvConfigError) as error:
                self.app._prepare_core()
        self.assertIn('已从最近一次成功启动的快照恢复', str(error.exception))
        self.assertEqual(entry.read_bytes(), source)
        self.assertEqual(entry.stat().st_mode & 0o777, 0o600)

    def test_missing_modular_dependency_does_not_overwrite_existing_entry(self):
        entry = self.root / 'runtime' / 'app.py'
        entry.parent.mkdir()
        source = b'print("candidate entry")\n'
        entry.write_bytes(source)
        good = self.app.CONFIG.path.parent / '.app.py.last-good'
        good.write_bytes(b'print("old entry")\n')
        good.chmod(0o600)
        with patch.object(self.app, '__file__', str(entry)), patch.object(self.app, 'BASE_DIR', str(entry.parent)), patch.object(self.app, '_restore_last_good', wraps=self.app._restore_last_good) as restore:
            with self.assertRaises(EnvConfigError):
                self.app._prepare_core()
        restore.assert_not_called()
        self.assertEqual(entry.read_bytes(), source)

    def test_primary_local_logout_blocks_extra_auto_permission(self):
        self.seed_accounts()
        cfg = self.app.cfg_all()
        self.assertTrue(self.app._auto_permission(101, 2, 2, cfg))
        with self.app.write_db() as conn:
            conn.execute("UPDATE accounts SET token='',enabled=0 WHERE id=1")
        self.assertFalse(self.app.Store().accounts.has_primary_account(101))
        self.assertFalse(self.app._auto_permission(101, 2, 2, cfg))
        with patch.object(self.app, '_run_ids') as run:
            self.assertEqual(self.app.scheduler_tick(cfg), [])
        run.assert_not_called()

    def test_primary_local_logout_blocks_manual_pre_request(self):
        self.seed_accounts()
        with self.app.write_db() as conn:
            conn.execute("UPDATE accounts SET token='',enabled=0 WHERE id=1")
        with patch.object(self.app, 'EmosClient') as client:
            client.return_value.sign_once.return_value = {'status': 'already', 'message': 'fixture', 'point': None}
            result = self.app._run_ids(self.app.cfg_all(), [2])
        self.assertEqual(result, [])
        client.assert_not_called()

    def test_eligibility_does_not_treat_blank_token_as_ready(self):
        self.seed_accounts()
        account = self.app._account_record(2)
        account['token'] = ' '
        cfg = self.app.cfg_all()
        self.assertFalse(self.app._eligible(account, self.app.now_local(cfg), cfg, False))

    def test_notification_shutdown_wakes_without_periodic_polling(self):
        worker = threading.Thread(target=self.app.notification_loop, name='emos-handover-notify')
        worker.start()
        try:
            start = time.monotonic()
            self.app._request_shutdown()
            worker.join(1.5)
            self.assertFalse(worker.is_alive())
            self.assertLess(time.monotonic() - start, 1.5)
            self.assertEqual(self.app._notifications.unfinished_tasks, 0)
        finally:
            self.app._shutdown.set()
            if worker.is_alive():
                self.app._notifications.put(None)
            worker.join(2)
            while True:
                try:
                    self.app._notifications.get_nowait()
                except queue.Empty:
                    break
                else:
                    self.app._notifications.task_done()

    def test_old_login_result_cannot_overwrite_same_token_reauthorization(self):
        self.seed_accounts()
        account = self.app._account_record(2)
        self.assertIsNotNone(account['owner_login_revision'])
        before = self.app._account_record(2)
        with self.app.write_db() as conn:
            conn.execute('UPDATE tg_users SET login_revision=login_revision+1 WHERE tg_id=101')
        result = self.app._store_result(account, {'status': 'invalid', 'message': 'stale', 'point': None},
                                        self.app.cfg_all(), 'auto')
        self.assertIsNone(result)
        self.assertEqual(self.app._account_record(2)['enabled'], before['enabled'])
        with self.app.db() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM logs').fetchone()[0], 0)

    def test_repeated_shutdown_requests_do_not_accumulate_wakeups(self):
        self.app._request_shutdown()
        count = self.app._notifications.qsize()
        for _ in range(12):
            self.app._request_shutdown()
        self.assertEqual(self.app._notifications.qsize(), count)

    def test_snapshot_restore_does_not_replace_concurrent_upload(self):
        import os
        import updates
        entry = self.root / 'runtime' / 'emoskata.py'
        entry.parent.mkdir()
        good = self.app.CONFIG.path.parent / '.emoskata.py.last-good'
        good.write_bytes(b'print("old source")\n')
        good.chmod(0o600)
        real_link = os.link
        def concurrent_upload(source, destination):
            entry.write_bytes(b'print("new upload")\n')
            real_link(source, destination)
        with patch.object(updates.os, 'link', side_effect=concurrent_upload):
            self.assertIsNone(self.app._restore_last_good(str(entry)))
        self.assertEqual(entry.read_bytes(), b'print("new upload")\n')

    def test_unchanged_ready_snapshot_is_not_rewritten(self):
        import updates
        entry = self.root / 'runtime' / 'emoskata.py'
        entry.parent.mkdir()
        entry.write_bytes(b'print("source")\n')
        updates.capture_image(entry)
        try:
            good = Path(updates.mark_ready(entry, self.app.CONFIG.path))
            before = good.stat()
            with patch.object(updates, '_atomic_bytes', wraps=updates._atomic_bytes) as write:
                self.assertEqual(Path(updates.mark_ready(entry, self.app.CONFIG.path)), good)
            write.assert_not_called()
            after = good.stat()
            self.assertEqual((before.st_ino, before.st_mtime_ns), (after.st_ino, after.st_mtime_ns))
        finally:
            updates._IMAGES.pop(str(entry), None)
            updates._READY_ENTRIES.discard(str(entry))

    def test_database_snapshot_timeout_is_bounded(self):
        import updates
        with self.assertRaisesRegex(RuntimeError, '快照超时'):
            updates.snapshot_database(self.app.DB_PATH, str(self.root / 'timeout.db'), timeout=0)


if __name__ == '__main__':
    unittest.main()
