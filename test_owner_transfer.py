"""账号归属迁移：单个迁移、整组换号、主号身份清除与会话失效。

用直接查库建号与断言，避开单人模式下 owned() 的 owner 限制；
只使用临时 SQLite 与合成数据，不代表真实官方授权。
Test: python3 -B -m unittest test_owner_transfer
"""
import tempfile
import unittest

from accounts import DomainError
from test_support import fixture_app


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='emos-transfer-')
        self.addCleanup(self.tmp.cleanup)
        self.app = fixture_app(self, self.tmp.name, 'transfer_app', single_owner=True)
        self.app.init_db()
        self.addCleanup(self.app.close_idle_db)
        self.service = self.app.Store().accounts

    def seed(self, tg_id, emos, token, primary=False):
        with self.app.write_db() as conn:
            conn.execute("INSERT INTO tg_users(tg_id,chat_id) VALUES(?,?) "
                         "ON CONFLICT(tg_id) DO NOTHING", (tg_id, tg_id))
            cur = conn.execute(
                "INSERT INTO accounts(name,token,emos_id,owner_tg_id) VALUES(?,?,?,?)",
                (emos, token, emos, tg_id))
            account_id = cur.lastrowid
            if primary:
                conn.execute('UPDATE tg_users SET primary_account_id=?,active_account_id=? '
                             'WHERE tg_id=?', (account_id, account_id, tg_id))
            else:
                conn.execute('UPDATE tg_users SET active_account_id=? WHERE tg_id=?',
                             (account_id, tg_id))
        return account_id

    def owner_of(self, account_id):
        with self.app.db() as conn:
            row = conn.execute('SELECT owner_tg_id FROM accounts WHERE id=?',
                               (account_id,)).fetchone()
        return row['owner_tg_id'] if row else None

    def emos_of_owner(self, tg_id):
        with self.app.db() as conn:
            return sorted(row['emos_id'] for row in conn.execute(
                'SELECT emos_id FROM accounts WHERE owner_tg_id=? ORDER BY id', (tg_id,)))

    def has_tg_row(self, tg_id):
        with self.app.db() as conn:
            return conn.execute('SELECT 1 FROM tg_users WHERE tg_id=?', (tg_id,)).fetchone() is not None

    def test_single_account_moves_and_clears_primary_identity(self):
        main = self.seed(101, 'eMain', '11_fixture_token_a', primary=True)
        self.assertEqual(self.service.primary_account(101)['id'], main)
        result = self.service.transfer_account(main, 202)
        self.assertEqual(result['moved'], [main])
        self.assertEqual(result['source'], 101)
        self.assertEqual(self.owner_of(main), 202)
        self.assertTrue(self.has_tg_row(202))
        # 主号身份随账号迁移被清除，新 TG 必须重新 /login 证明
        with self.app.db() as conn:
            row = conn.execute('SELECT primary_account_id FROM tg_users WHERE tg_id=?',
                               (202,)).fetchone()
        self.assertIsNone(row['primary_account_id'])

    def test_transfer_owner_moves_all_and_drops_source_row(self):
        self.seed(101, 'eMain', '11_fixture_token_a', primary=True)
        self.seed(101, 'eExtra', '22_fixture_token_b')
        self.assertEqual(self.emos_of_owner(101), ['eExtra', 'eMain'])
        result = self.service.transfer_owner(101, 303)
        self.assertEqual(result['source'], 101)
        self.assertEqual(self.emos_of_owner(303), ['eExtra', 'eMain'])
        self.assertEqual(self.emos_of_owner(101), [])
        self.assertFalse(self.has_tg_row(101))

    def test_source_with_remaining_accounts_keeps_row_and_reselects(self):
        first = self.seed(101, 'eFirst', '33_fixture_token_c')
        second = self.seed(101, 'eSecond', '44_fixture_token_d')
        self.service.transfer_account(first, 404)
        self.assertTrue(self.has_tg_row(101))
        self.assertEqual(self.owner_of(first), 404)
        with self.app.db() as conn:
            active = conn.execute('SELECT active_account_id FROM tg_users WHERE tg_id=?',
                                  (101,)).fetchone()['active_account_id']
        self.assertEqual(active, second)

    def test_transfer_rejects_same_target_and_empty_source(self):
        main = self.seed(101, 'eMain', '11_fixture_token_a', primary=True)
        with self.assertRaisesRegex(DomainError, '相同'):
            self.service.transfer_owner(101, 101)
        with self.assertRaisesRegex(DomainError, '没有可迁移'):
            self.service.transfer_owner(555, 666)
        with self.assertRaises(DomainError):
            self.service.transfer_account(main, 0)

    def test_owners_overview_lists_counts_without_tokens(self):
        self.seed(101, 'eMain', '11_fixture_token_a', primary=True)
        self.seed(202, 'eOther', '55_fixture_token_e')
        rows = self.service.owners_overview()
        by_id = {row['tg_id']: row for row in rows}
        self.assertEqual(by_id[101]['accounts'], 1)
        self.assertEqual(by_id[202]['accounts'], 1)
        for row in rows:
            self.assertNotIn('token', row)


if __name__ == '__main__':
    unittest.main()
