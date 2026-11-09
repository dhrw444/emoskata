"""单用户准入、官方主号证明和全部有效账号范围回归。

仅用隔离目录、合成身份或 localhost；正式授权和真实生产资源不在此验收。
Test: python3 -B -m unittest test_owner_accounts"""
import sys
sys.dont_write_bytecode = True

import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from accounts import AccountService, DomainError
from test_accounts import OLD_SCHEMA, TempStore


OWNER = 101
OTHER = 202
OWNER_CONFIG = {'tg_allowed_user_ids': str(OWNER), 'sign_time': '08:00',
                'base_url': 'https://example.invalid'}
OLD_USERS_SCHEMA = '''
CREATE TABLE tg_users (
    tg_id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL,
    active_account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
    login_until INTEGER NOT NULL DEFAULT 0,
    input_kind TEXT NOT NULL DEFAULT '', input_until INTEGER NOT NULL DEFAULT 0,
    notify_success INTEGER NOT NULL DEFAULT 1, notify_fail INTEGER NOT NULL DEFAULT 1,
    notify_already INTEGER NOT NULL DEFAULT 0, created TEXT DEFAULT '',
    login_emos_id TEXT NOT NULL DEFAULT '', login_revision INTEGER NOT NULL DEFAULT 0
);
'''


class OwnerAccountCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='emoskata-owner-account-')
        self.addCleanup(self.temp.cleanup)
        self.store = TempStore(Path(self.temp.name) / 'owner.sqlite3', OWNER_CONFIG)
        self.service = AccountService(self.store.db, self.store.write, self.store.cfg)
        with self.store.write() as conn:
            AccountService.init_schema(conn)

    @staticmethod
    def profile(official, tg_id=OWNER, **extra):
        profile = {'user_id': official, '_user_id': official,
                   'telegram_user_id': tg_id, '_display': '账号 ' + str(official)}
        profile.update(extra)
        return profile

    def official(self, official='PRIMARY', token=None, **extra):
        self.service.begin_login(OWNER, official)
        return self.service.finish_login(OWNER, token or 'fake-official-' + official,
                                         self.profile(official, **extra))

    def token(self, official, token=None, expected_root=None, **extra):
        return self.service.login_with_token(OWNER, token or 'fake-extra-' + official,
                                             self.profile(official, **extra),
                                             expected_root=expected_root)

    def rows(self, sql, params=()):
        with closing(self.store.db()) as conn:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]

    def mutate(self, sql, params=()):
        with self.store.write() as conn:
            conn.execute(sql, params)

    def snapshot(self):
        return {table: self.rows('SELECT * FROM ' + table + ' ORDER BY 1')
                for table in ('accounts', 'tg_users', 'logs', 'account_links',
                              'link_codes', 'link_requests')}

    def foreign(self):
        with self.store.write() as conn:
            cur = conn.execute('INSERT INTO accounts(name,token,emos_id,owner_tg_id) VALUES(?,?,?,?)',
                               ('别人的保留账号', 'fake-foreign-secret', 'FOREIGN', OTHER))
            conn.execute('INSERT INTO tg_users(tg_id,chat_id,active_account_id) VALUES(?,?,?)',
                         (OTHER, OTHER, cur.lastrowid))
            return dict(conn.execute('SELECT * FROM accounts WHERE id=?', (cur.lastrowid,)).fetchone())

    def add_log(self, account, message='已签到'):
        self.mutate("INSERT INTO logs(account_id,name,ts,status,message,carrot) VALUES(?,?,'test','signed',?,1)",
                    (account['id'], account['name'], message))

    def assert_safe(self, value, tokens=()):
        rendered = json.dumps(value, ensure_ascii=False)
        for token in tokens:
            self.assertNotIn(token, rendered)
        if isinstance(value, dict):
            self.assertTrue({'token', 'base_url', 'owner_tg_id', 'code_hash'}.isdisjoint(value))
            for item in value.values():
                self.assert_safe(item, tokens)
        elif isinstance(value, (list, tuple)):
            for item in value:
                self.assert_safe(item, tokens)

    def test_token_and_token_input_require_completed_official_primary(self):
        self.assertIsNone(self.service.primary_account(OWNER))
        self.assertFalse(self.service.has_primary_account(OWNER))
        self.service.ensure_user(OWNER)
        for pending in (False, True):
            if pending:
                self.service.begin_login(OWNER, 'PRIMARY')
            before = self.snapshot()
            with self.assertRaisesRegex(DomainError, 'emospg_bot'):
                self.token('FIRST-TOKEN')
            with self.assertRaisesRegex(DomainError, 'emospg_bot'):
                self.service.set_input(OWNER, 'token')
            self.assertEqual(self.snapshot(), before)
        self.assertIsNone(self.service.user(OWNER)['primary_account_id'])
        self.assertEqual(self.rows('SELECT COUNT(*) AS n FROM accounts')[0]['n'], 0)

    def test_official_first_login_matches_pending_id_and_telegram_binding(self):
        self.service.begin_login(OWNER, '00123', expected_root=0)
        wrong = [self.profile('123'), self.profile('00123', OTHER),
                 self.profile('00123', True), self.profile('00123', None),
                 self.profile('00123', '101.0')]
        for profile in wrong:
            with self.subTest(profile=profile), self.assertRaises(DomainError):
                self.service.finish_login(OWNER, 'fake-first-secret', profile)
            self.assertIsNone(self.service.primary_account(OWNER))
        with self.assertRaises(DomainError):
            self.service.finish_login(OTHER, 'fake-first-secret', self.profile('00123', OTHER))
        account = self.service.finish_login(OWNER, ' fake-first-secret ',
                                            self.profile('00123', '101', _carrot=128),
                                            expected_root=0)
        self.assertEqual(account['emos_id'], '00123')
        self.assertEqual(self.service.user(OWNER)['primary_account_id'], account['id'])
        self.assertEqual(self.service.primary_account(OWNER)['id'], account['id'])
        self.assertTrue(self.service.has_primary_account(OWNER))
        self.assertFalse(self.service.login_pending(OWNER))
        self.assert_safe((account, self.service.primary_account(OWNER)), ('fake-first-secret',))

    def test_official_primary_and_balance_persist_across_service_restart(self):
        with patch('accounts.time.time', return_value=1000):
            account = self.official(_carrot=256)
        self.assertEqual((account['carrot'], account['carrot_at']), (256, 1000))
        service = AccountService(self.store.db, self.store.write, self.store.cfg)
        self.assertEqual(service.primary_account(OWNER)['id'], account['id'])
        self.assertTrue(service.has_primary_account(OWNER))
        with self.store.write() as conn:
            AccountService.init_schema(conn)
        self.assertEqual(service.primary_account(OWNER)['carrot'], 256)
        self.assertEqual(self.rows('PRAGMA foreign_key_check'), [])

    def test_primary_reauthorization_keeps_identity_and_history_when_extra_is_active(self):
        primary = self.official(_carrot=40)
        extra = self.token('EXTRA')
        self.add_log(primary, '历史保留')
        self.mutate("UPDATE accounts SET enabled=0,last_status='invalid',auto_sign=0,"
                    "sign_time='11:30',last_day='kept-day',last_carrot=9 WHERE id=?", (primary['id'],))
        self.service.begin_login(OWNER, primary['emos_id'], expected_root=extra['id'])
        pending = self.service.user(OWNER)
        updated = self.service.finish_login(OWNER, 'fake-rotated-primary', self.profile('PRIMARY', _carrot=41),
                                            expected_root=extra['id'],
                                            expected_login_until=pending['login_until'],
                                            expected_login_revision=pending['login_revision'])
        self.assertEqual(updated['id'], primary['id'])
        self.assertEqual(self.service.primary_account(OWNER)['id'], primary['id'])
        self.assertEqual((updated['enabled'], updated['carrot'], updated['auto_sign'],
                          updated['sign_time'], updated['last_day'], updated['last_carrot']),
                         (1, 41, 0, '11:30', 'kept-day', 9))
        self.assertEqual(self.service.logs(OWNER)[0]['message'], '历史保留')
        self.assertEqual(len(self.service.owned(OWNER)), 2)

    def test_extra_oauth_rejected_at_begin_and_finish_without_replacing_primary(self):
        primary = self.official()
        self.token('EXTRA')
        before = self.snapshot()
        with self.assertRaisesRegex(DomainError, 'token'):
            self.service.begin_login(OWNER, 'EXTRA')
        self.assertEqual(self.snapshot(), before)
        self.service.begin_login(OWNER, 'PRIMARY')
        self.mutate("UPDATE tg_users SET login_emos_id='EXTRA' WHERE tg_id=?", (OWNER,))
        before = self.snapshot()
        with self.assertRaisesRegex(DomainError, 'token'):
            self.service.finish_login(OWNER, 'fake-new-oauth', self.profile('EXTRA'))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.service.primary_account(OWNER)['id'], primary['id'])

    def test_two_token_accounts_cover_owner_visibility_logs_and_run_not_foreign_links(self):
        primary = self.official()
        second, third = self.token('SECOND'), self.token('THIRD')
        foreign = self.foreign()
        self.mutate("INSERT INTO account_links VALUES(?,?,1,'old-link')", (third['id'], foreign['id']))
        for account in (primary, second, third, foreign):
            self.add_log(account)
        ids = [third['id'], primary['id'], second['id']]
        self.assertEqual([row['id'] for row in self.service.visible(OWNER)], ids)
        self.assertEqual(self.service.run_scope(OWNER), {'root_id': third['id'], 'account_ids': ids})
        self.assertEqual(self.service.run_scope(OWNER, False)['account_ids'], [third['id']])
        for account in (primary, second, third):
            self.assertTrue(self.service.can_run(OWNER, third['id'], account['id']))
        self.assertFalse(self.service.can_run(OWNER, third['id'], foreign['id']))
        self.assertFalse(self.service.can_run(OTHER, third['id'], primary['id']))
        self.assertEqual({row['account_id'] for row in self.service.logs(OWNER)}, set(ids))
        self.assertEqual({row['id'] for row in self.service.admin_overview()}, set(ids))
        self.assertEqual([row['tg_id'] for row in self.service.watchers(second['id'])], [OWNER])
        self.assertEqual(self.service.watchers(foreign['id']), [])
        before = self.snapshot()
        link_ops = [lambda: self.service.issue_code(OWNER),
                    lambda: self.service.redeem(OWNER, 'FOREIGN', 'fake-code'),
                    lambda: self.service.links(OWNER), lambda: self.service.pending_links(OWNER),
                    lambda: self.service.toggle_link(OWNER, third['id'], foreign['id']),
                    lambda: self.service.unlink(OWNER, third['id'], foreign['id']),
                    lambda: self.service.review_link(OWNER, third['id'], foreign['id'], True),
                    lambda: self.service.withdraw_link(OWNER, third['id'], foreign['id']),
                    lambda: self.service.set_input(OWNER, 'link')]
        for operation in link_ops:
            with self.assertRaises(DomainError):
                operation()
        self.assertEqual(self.snapshot(), before)
        self.mutate("UPDATE accounts SET enabled=0,last_status='invalid' WHERE id=?", (second['id'],))
        self.assertEqual(self.service.run_scope(OWNER)['account_ids'], [third['id'], primary['id']])
        self.assertEqual([row['id'] for row in self.service.visible(OWNER)], ids)
        self.assertEqual({row['account_id'] for row in self.service.logs(OWNER)}, set(ids))
        self.assert_safe(self.service.visible(OWNER), ('fake-foreign-secret',))

    def test_run_and_token_write_recheck_selected_root_owner_and_eligibility(self):
        primary = self.official()
        extra = self.token('EXTRA')
        snapshot = self.service.run_scope(OWNER)
        self.service.switch(OWNER, primary['id'], expected_root=extra['id'])
        self.assertFalse(self.service.can_run(OWNER, snapshot['root_id'], primary['id']))
        before = self.snapshot()
        with self.assertRaisesRegex(DomainError, '已切换'):
            self.token('RACED', expected_root=extra['id'])
        self.assertEqual(self.snapshot(), before)
        self.assertTrue(self.service.can_run(OWNER, primary['id'], extra['id']))
        self.mutate('UPDATE accounts SET owner_tg_id=? WHERE id=?', (OTHER, extra['id']))
        self.assertFalse(self.service.can_run(OWNER, primary['id'], extra['id']))
        self.mutate("UPDATE accounts SET last_status='invalid' WHERE id=?", (primary['id'],))
        self.assertFalse(self.service.can_run(OWNER, primary['id'], primary['id']))
        with self.assertRaises(DomainError):
            self.service.run_scope(OWNER)
        self.assertIsNotNone(self.service.primary_account(OWNER))

    def test_invalid_owner_change_and_key_removal_deny_without_data_loss(self):
        primary = self.official()
        self.add_log(primary)
        before = self.snapshot()
        for owner in (None, True, False, 0, -1, '0', '101,202', '101 202',
                      '101,101', '101,', '１０１', '01', '1.0', 2**63, [], {}, str(OTHER)):
            self.store.config['tg_allowed_user_ids'] = owner
            with self.subTest(owner=owner):
                self.assertIsNone(self.service.primary_account(OWNER))
                self.assertFalse(self.service.has_primary_account(OWNER))
                self.assertFalse(self.service.can_run(OWNER, primary['id'], primary['id']))
                self.assertEqual(self.service.admin_overview(), [])
                self.assertEqual(self.service.watchers(primary['id']), [])
                for operation in (lambda: self.service.ensure_user(OWNER),
                                  lambda: self.service.user(OWNER), lambda: self.service.owned(OWNER),
                                  lambda: self.service.active(OWNER, False), lambda: self.service.visible(OWNER),
                                  lambda: self.service.logs(OWNER), lambda: self.service.run_scope(OWNER),
                                  lambda: self.token('BLOCKED'),
                                  lambda: self.service.set_preferences(OWNER, notify_success=0)):
                    with self.assertRaises(DomainError):
                        operation()
                self.assertEqual(self.snapshot(), before)
        del self.store.config['tg_allowed_user_ids']
        self.assertIsNone(self.service.primary_account(OWNER))
        with self.assertRaises(DomainError):
            self.service.visible(OTHER)
        self.store.config['tg_allowed_user_ids'] = str(OWNER)
        self.assertEqual(self.service.primary_account(OWNER)['id'], primary['id'])
        self.assertEqual(self.snapshot(), before)

    def test_configuration_change_inside_write_rolls_back_without_new_account(self):
        primary = self.official()
        before = self.snapshot()
        self.store.next_write = lambda conn: self.store.config.update(tg_allowed_user_ids=str(OTHER))
        with self.assertRaises(DomainError):
            self.token('RACED-OWNER', expected_root=primary['id'])
        self.store.config['tg_allowed_user_ids'] = str(OWNER)
        self.assertEqual(self.snapshot(), before)
        calls = 0

        def config():
            nonlocal calls
            calls += 1
            if calls == 3:
                self.store.config['tg_allowed_user_ids'] = ''
            return self.store.cfg()

        service = AccountService(self.store.db, self.store.write, config)
        with self.assertRaises(DomainError):
            service.login_with_token(OWNER, 'fake-post-write', self.profile('POST-WRITE'))
        self.store.config['tg_allowed_user_ids'] = str(OWNER)
        self.assertEqual(self.snapshot(), before)

    def test_independent_tokens_keep_local_owner_and_cannot_take_existing_local_account(self):
        primary = self.official()
        foreign = self.foreign()
        for index, tg_id in enumerate((None, '', OTHER, '202')):
            official = 'EXTRA-%d' % index
            with self.subTest(tg_id=tg_id):
                added = self.service.login_with_token(OWNER, 'fake-extra-%d' % index,
                                                      self.profile(official, tg_id))
                self.assertEqual(added['emos_id'], official)
                self.assertEqual(self.service.primary_account(OWNER)['id'], primary['id'])
                self.assertEqual(self.rows('SELECT owner_tg_id FROM accounts WHERE id=?',
                                          (added['id'],))[0]['owner_tg_id'], OWNER)
        before = self.snapshot()
        with self.assertRaises(DomainError):
            self.service.login_with_token(OWNER, foreign['token'], self.profile('FOREIGN', OTHER))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(len(self.service.owned(OWNER)), 5)
        self.service.set_input(OWNER, 'token', ttl=600)
        self.assertEqual(self.service.input_state(OWNER), 'token')
        self.service.clear_input(OWNER)
        self.assertEqual(self.service.input_state(OWNER), '')

    def test_unknown_canonical_balance_preserves_value_time_and_real_zero(self):
        with patch('accounts.time.time', return_value=1000):
            primary = self.official(_carrot=128)
        with patch('accounts.time.time', return_value=1100):
            updated = self.official(_carrot=None, carrot=999, earn_point=999)
        self.assertEqual((updated['carrot'], updated['carrot_at']), (128, 1000))
        with patch('accounts.time.time', return_value=1200):
            updated = self.token('PRIMARY', _carrot=None, carrot=777)
            unknown = self.token('UNKNOWN', _carrot=None, carrot=777)
        self.assertEqual((updated['carrot'], updated['carrot_at']), (128, 1000))
        self.assertEqual((unknown['carrot'], unknown['carrot_at']), (None, 0))
        with patch('accounts.time.time', return_value=1300):
            updated = self.token('PRIMARY', _carrot=0)
        self.assertEqual((updated['carrot'], updated['carrot_at']), (0, 1300))
        self.assertEqual(self.service.primary_account(OWNER)['id'], primary['id'])

    def test_old_database_migration_preserves_rows_and_requires_official_recovery(self):
        store = TempStore(Path(self.temp.name) / 'old.sqlite3', OWNER_CONFIG)
        with closing(store.db()) as conn:
            conn.executescript(OLD_SCHEMA + OLD_USERS_SCHEMA)
            conn.execute("INSERT INTO accounts(id,name,token,last_status,last_carrot,created) "
                         "VALUES(7,'旧账号','fake-legacy','invalid',9,'old-created')")
            conn.execute("INSERT INTO logs(account_id,name,ts,status,message,carrot) "
                         "VALUES(7,'旧账号','old-time','signed','old-history',9)")
            conn.execute('INSERT INTO tg_users(tg_id,chat_id,active_account_id) VALUES(101,101,7)')
            accounts = [dict(row) for row in conn.execute('SELECT * FROM accounts')]
            logs = [dict(row) for row in conn.execute('SELECT * FROM logs')]
        with store.write() as conn:
            AccountService.init_schema(conn)
            AccountService.init_schema(conn)
        service = AccountService(store.db, store.write, store.cfg)
        with closing(store.db()) as conn:
            after = [dict(row) for row in conn.execute('SELECT * FROM accounts')]
            self.assertEqual([{key: row[key] for key in accounts[0]} for row in after], accounts)
            self.assertEqual([dict(row) for row in conn.execute('SELECT * FROM logs')], logs)
            self.assertIsNone(conn.execute('SELECT primary_account_id FROM tg_users').fetchone()[0])
            conn.execute("UPDATE accounts SET emos_id='RECOVER',owner_tg_id=101")
        self.assertIsNone(service.primary_account(OWNER))
        with self.assertRaises(DomainError):
            service.login_with_token(OWNER, 'fake-legacy', self.profile('RECOVER'))
        with self.assertRaises(DomainError):
            service.run_scope(OWNER)
        service.begin_login(OWNER, 'RECOVER', expected_root=7)
        recovered = service.finish_login(OWNER, 'fake-recovered-official', self.profile('RECOVER', _carrot=10),
                                          expected_root=7)
        self.assertEqual((recovered['id'], recovered['created'], recovered['last_carrot']), (7, 'old-created', 9))
        self.assertEqual(service.primary_account(OWNER)['id'], 7)
        self.assertEqual(service.logs(OWNER)[0]['message'], 'old-history')
        with closing(store.db()) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM accounts').fetchone()[0], 1)
            self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(), [])
        with store.write() as conn:
            conn.execute('UPDATE tg_users SET primary_account_id=NULL')
        self.assertIsNone(service.primary_account(OWNER))
        with self.assertRaises(DomainError):
            service.login_with_token(OWNER, 'fake-recovered-official', self.profile('RECOVER'))
        service.begin_login(OWNER, 'RECOVER')
        self.assertEqual(service.finish_login(OWNER, 'fake-recovered-again', self.profile('RECOVER'))['id'], 7)


if __name__ == '__main__':
    unittest.main()
