"""限流表迁移、失败记录回收、模块预检和线程收尾回归。

夹具只使用临时或演练数据；本地通过不代表真实官方授权。
Test: python3 -B -m unittest test_quality_runtime。"""
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from test_support import BUILD_FILES, ROOT, fixture_app


class QualityRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='emos-quality-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = fixture_app(self, self.root, 'quality_runtime_fixture')
        self.app.init_db()
        self.addCleanup(self.app.close_idle_db)

    def failures(self):
        with self.app.db() as conn:
            return {row['ip']: dict(row) for row in conn.execute('SELECT * FROM login_fail')}

    def test_unlocked_failures_expire_on_startup_but_active_locks_survive(self):
        now = 1_700_000_000
        with patch.object(self.app.time, 'time', return_value=now):
            for index in range(192):
                self.app.rl_fail('old-'+str(index))
        later = now + self.app.LOGIN_FAIL_TTL
        with patch.object(self.app.time, 'time', return_value=later):
            for _ in range(self.app.LOGIN_MAX_FAIL):
                self.app.rl_fail('locked-now')
            self.app.init_db()
            self.assertEqual(set(self.failures()), {'locked-now'})
            self.assertEqual(self.app.rl_blocked('locked-now'), self.app.LOGIN_LOCK_SEC)

    def test_failure_sweep_cleans_stale_sources_without_restart(self):
        now = 1_700_000_000
        with patch.object(self.app.time, 'time', return_value=now):
            self.app.rl_fail('stale')
        self.app._RL_SWEEP = 63
        with patch.object(self.app.time, 'time', return_value=now+self.app.LOGIN_FAIL_TTL):
            self.assertEqual(self.app.rl_fail('fresh'), 1)
        self.assertEqual(set(self.failures()), {'fresh'})

    def test_same_source_restarts_count_after_lock_or_retention_expiry(self):
        now = 1_700_000_000
        with patch.object(self.app.time, 'time', return_value=now):
            for _ in range(self.app.LOGIN_MAX_FAIL):
                self.app.rl_fail('locked')
            self.app.rl_fail('infrequent')
        with patch.object(self.app.time, 'time', return_value=now+self.app.LOGIN_LOCK_SEC):
            self.assertEqual(self.app.rl_fail('locked'), 1)
            self.assertEqual(self.app.rl_blocked('locked'), 0)
        with patch.object(self.app.time, 'time', return_value=now+self.app.LOGIN_FAIL_TTL):
            self.assertEqual(self.app.rl_fail('infrequent'), 1)

    def test_old_failure_schema_migrates_once_without_clearing_live_lock(self):
        now = 1_700_000_000
        with self.app.write_db() as conn:
            conn.execute('DROP TABLE login_fail')
            conn.execute('CREATE TABLE login_fail(ip TEXT PRIMARY KEY,count INTEGER,until INTEGER)')
            conn.execute('INSERT INTO login_fail VALUES(?,?,?)', ('legacy', 5, now+120))
        with patch.object(self.app.time, 'time', return_value=now):
            self.app.init_db()
            self.assertEqual(self.app.rl_blocked('legacy'), 120)
            self.assertEqual(self.failures()['legacy']['updated_at'], now)
        with patch.object(self.app.time, 'time', return_value=now+30):
            self.app.init_db()
            self.assertEqual(self.failures()['legacy']['updated_at'], now)
            self.assertEqual(self.app.rl_blocked('legacy'), 90)

    def test_modular_preflight_reads_sibling_modules_without_real_configuration(self):
        stage = self.root/'modules'
        stage.mkdir()
        for name in BUILD_FILES:
            (stage/name).write_bytes((ROOT/name).read_bytes())
        entry = stage/'app.py'
        module = fixture_app(self, self.root/'modular-config', 'quality_modular_fixture', entry)
        self.addCleanup(module.close_idle_db)
        before = module.CONFIG.path.read_bytes()
        with patch.dict(os.environ, {'PYTHONPATH': '/path/which/does/not/exist'}):
            image = module._prepare_core()
            try:
                self.assertEqual(image.read(), entry.read_bytes())
            finally:
                image.close()
        self.assertEqual(module.CONFIG.path.read_bytes(), before)

    def test_healthy_threads_finish_inside_short_shutdown_grace(self):
        started = threading.Event()
        def waiting_loop():
            started.set()
            self.app._shutdown.wait()
        worker = threading.Thread(target=waiting_loop, name='emos-quality-exit')
        worker.start()
        self.assertTrue(started.wait(1))
        self.app._CORE_THREADS = [worker]
        try:
            self.assertTrue(self.app._stop_core(2.0))
            self.assertFalse(worker.is_alive())
        finally:
            self.app._shutdown.set()
            worker.join(2)


if __name__ == '__main__':
    unittest.main()
