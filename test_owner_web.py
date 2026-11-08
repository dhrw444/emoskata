"""owner 变更后的网页、通知及调度权限回归。

仅使用临时目录、伪凭证和本地服务；不把替身成功视为生产授权或容器验收。
Runtime: Python 3.10+ / 标准库。
Test: python3 -B -m unittest test_owner_web
"""
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from accounts import DomainError
from test_support import fixture_app, configure
from test_runtime import serve
import bot


class OwnerWebTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='emos-owner-web-')
        self.addCleanup(self.tmp.cleanup)
        self.app = fixture_app(self, self.tmp.name, 'owner_web_fixture', single_owner=True)
        self.app.init_db()
        self.addCleanup(self.app.close_idle_db)
        configure(self.app, web_base_url='http://127.0.0.1:8080', tg_bot_token='123456:fixture_test_bot')
        self.service = self.app.Store().accounts
        self.service.begin_login(101, 'ePrimary')
        self.primary = self.service.finish_login(101, 'primary_token_123', {
            'user_id': 'ePrimary', 'telegram_user_id': 101, '_carrot': 40})
        self.extra = self.service.login_with_token(101, 'extra_token_12345', {
            'user_id': 'eExtra', 'telegram_user_id': 101, '_carrot': 777})
        self.web = self.app.personal_web()
        self.addCleanup(self.web.stop)

    def login(self):
        code = parse_qs(urlsplit(self.web.issue_url(101)).fragment)['code'][0]
        return self.web.exchange(code)

    def test_web_has_all_owner_balances_not_last_rewards(self):
        state = self.web.state(101)
        self.assertTrue(state['single_owner'])
        self.assertEqual({a['carrot'] for a in state['owned']}, {40, 777})
        self.assertEqual(state['targets'][0]['carrot'], 40)
        self.assertIn('carrot_at', state['targets'][0])
        self.assertEqual(state['links'], {'incoming': [], 'outgoing': []})
        self.assertEqual(len(state['owned']), 2)

    def test_owner_change_invalidates_existing_web_session_and_actions(self):
        auth = self.login()
        self.assertIsNotNone(self.web.authenticate(auth['sid']))
        configure(self.app, tg_allowed_user_ids='202')
        self.assertIsNone(self.web.authenticate(auth['sid']))
        for op in (lambda: self.web.state(101), lambda: self.web.logs(101),
                   lambda: self.web.issue_url(101),
                   lambda: self.web.action(101, 'preferences', {'notify_success': False})):
            with self.assertRaises(DomainError):
                op()

    def test_cross_owner_cannot_consume_personal_entry_code(self):
        code = parse_qs(urlsplit(self.web.issue_url(101)).fragment)['code'][0]
        configure(self.app, tg_allowed_user_ids='202')
        with self.assertRaises(DomainError):
            self.web.exchange(code)
        configure(self.app, tg_allowed_user_ids='101')
        self.assertEqual(self.web.exchange(code)['tg_id'], 101)

    def test_old_shared_actions_all_closed(self):
        for action in ('code', 'link', 'review', 'withdraw', 'unlink', 'toggle'):
            with self.subTest(action=action), self.assertRaises(DomainError):
                self.web.action(101, action, {'account_id': self.primary['id']})

    def test_notification_uses_owner_not_arbitrary_chat_id(self):
        task = (self.extra['id'], 101, 'notify_success', {
            'id': self.extra['id'], 'emos_id': 'eExtra', 'name': 'Extra',
            'status': 'signed', 'message': '签到成功', 'carrot': 3})
        delivered = []
        with patch.object(bot.TG, 'send', side_effect=lambda chat, *a, **k: delivered.append(chat)):
            self.assertTrue(self.app._deliver_notification(task))
            self.assertEqual(delivered, [101])
            configure(self.app, tg_allowed_user_ids='202')
            self.assertFalse(self.app._deliver_notification(task))
            self.assertEqual(delivered, [101])

    def test_scheduler_never_sends_old_shared_or_foreign_owner_accounts(self):
        with serve() as (server, base):
            server.identities = {
                'primary_token_123': {'user_id': 'ePrimary', 'telegram_user_id': 101},
                'extra_token_12345': {'user_id': 'eExtra', 'telegram_user_id': 101},
                'foreign_token_123': {'user_id': 'eForeign', 'telegram_user_id': 202}}
            with self.app.write_db() as conn:
                conn.execute('UPDATE accounts SET base_url=?,sign_time=?', (base, '01:00'))
                conn.execute("INSERT INTO accounts(name,token,emos_id,owner_tg_id,base_url,auto_sign,sign_time) "
                             "VALUES('Foreign','foreign_token_123','eForeign',202,?,1,'01:00')", (base,))
                foreign = conn.execute("SELECT id FROM accounts WHERE emos_id='eForeign'").fetchone()[0]
                conn.execute('INSERT INTO account_links(source_id,target_id,enabled) VALUES(?,?,1)',
                             (self.primary['id'], foreign))
            with patch.object(self.app._shutdown, 'wait', return_value=False):
                rows = self.app.scheduler_tick()
            self.assertEqual({row['emos_id'] for row in rows}, {'ePrimary', 'eExtra'})
            self.assertEqual(server.signed, {'ePrimary', 'eExtra'})
            self.assertFalse(any(who == 'eForeign' for _, _, who in server.hits))
            configure(self.app, tg_allowed_user_ids='')
            before = list(server.hits)
            self.assertEqual(self.app.scheduler_tick(), [])
            self.assertEqual(server.hits, before)


if __name__ == '__main__':
    unittest.main()
