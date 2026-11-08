"""SQLite 物理连接容量、等待超时、租约归还与关闭失败回归。

夹具只使用临时或演练数据；本地通过不代表真实官方授权。
Test: python3 -B -m unittest test_database_pool。
"""
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from test_support import fixture_app


class BoundedDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='emos-pool-test-')
        self.addCleanup(self.tmp.cleanup)
        self.app = fixture_app(self, Path(self.tmp.name), 'bounded_db_fixture')
        self.app.init_db()
        self.app.close_idle_db()
        self.addCleanup(self.app.close_idle_db)

    def test_eight_connections_and_finite_ninth_wait_then_idle_three(self):
        ready = threading.Barrier(9)
        release = threading.Event()
        failures = []
        def hold():
            try:
                with self.app.db() as conn:
                    conn.execute('SELECT 1')
                    ready.wait(5)
                    release.wait(10)
            except BaseException as error:
                failures.append(type(error).__name__)
        workers = [threading.Thread(target=hold) for _ in range(8)]
        for worker in workers:
            worker.start()
        try:
            ready.wait(5)
            self.assertEqual(self.app._db_total, 8)
            self.assertEqual(self.app._db_borrowed, 8)
            started = time.monotonic()
            with patch.object(self.app, '_DB_WAIT_TIMEOUT', 0.05):
                with self.assertRaises(self.app.DatabaseBusy):
                    self.app.db()
            self.assertLess(time.monotonic() - started, 2)
            self.assertEqual(self.app._db_total, 8)
            self.assertEqual(self.app._db_waiters, 0)
        finally:
            release.set()
            for worker in workers:
                worker.join(5)
        self.assertFalse(failures)
        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertEqual(self.app._db_borrowed, 0)
        self.assertEqual(self.app._db_total, 3)
        self.assertEqual(len(self.app._db_pool), 3)
        self.app.close_idle_db()
        self.assertEqual(self.app._db_total, 0)

    def test_nested_borrow_rejected_and_double_close_idempotent(self):
        connection = self.app.db()
        with self.assertRaises(sqlite3.ProgrammingError):
            self.app.db()
        self.assertEqual(self.app._db_borrowed, 1)
        connection.close()
        connection.close()
        self.assertEqual(self.app._db_borrowed, 0)
        self.assertEqual(self.app._db_total, 1)
        with self.app.db() as usable:
            self.assertEqual(usable.execute('SELECT 1').fetchone()[0], 1)

    def test_create_failure_returns_reserved_capacity(self):
        with patch.object(self.app.sqlite3, 'connect', side_effect=sqlite3.OperationalError('fixture')):
            with self.assertRaises(sqlite3.OperationalError):
                self.app.db()
        self.assertEqual(self.app._db_total, 0)
        self.assertEqual(self.app._db_borrowed, 0)
        self.assertFalse(self.app._db_owners)
        with self.app.db() as usable:
            self.assertEqual(usable.execute('SELECT 1').fetchone()[0], 1)

    def test_uncommitted_transaction_rolled_back_on_return(self):
        with self.app.write_db() as conn:
            conn.execute('CREATE TABLE pool_probe(value TEXT)')
        connection = self.app.db()
        connection.execute('INSERT INTO pool_probe VALUES(?)', ('uncommitted',))
        connection.close()
        with self.app.db() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM pool_probe').fetchone()[0], 0)

    def test_sealed_pool_rejects_and_resume_allows_new_lease(self):
        self.app._seal_db_pool(True)
        try:
            with self.assertRaises(self.app.DatabaseBusy):
                self.app.db()
        finally:
            self.app._seal_db_pool(False)
        with self.app.db() as conn:
            self.assertEqual(conn.execute('SELECT 1').fetchone()[0], 1)

    def test_physical_close_failure_quarantined_without_capacity_overflow(self):
        class ClosingFailure:
            def __init__(self):
                self.fail = True
            def close(self):
                if self.fail:
                    raise sqlite3.OperationalError('fixture')
        failing = ClosingFailure()
        with self.app._db_available:
            self.app._db_total = 1
            self.app._db_closing = 1
        with self.assertRaises(self.app.DatabaseBusy):
            self.app._close_physical(failing)
        self.assertEqual(self.app._db_total, 1)
        self.assertEqual(self.app._db_closing, 0)
        with self.assertRaises(self.app.DatabaseBusy):
            self.app.db()
        failing.fail = False
        self.app.close_idle_db()
        self.assertEqual(self.app._db_total, 0)
        self.assertFalse(self.app._db_quarantine)


if __name__ == '__main__':
    unittest.main()
