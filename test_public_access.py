"""公开 TG 访问与账号隔离契约回归。

公开只扩大准入，不恢复跨 TG 代签；使用合成身份与隔离 SQLite，不访问真实 Bot。
Test: python3 -B -m unittest test_public_access
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bot
from accounts import DomainError
from envconfig import configured_owner, owner_permitted, tg_access_owner
from test_bot import ClientFactory, FakeStore, FakeTG
from test_support import configure, fixture_app


class AccessPolicyTests(unittest.TestCase):
    def test_empty_is_public_filled_is_restricted_invalid_is_closed(self):
        for raw in ('', '  '):
            config = {'tg_allowed_user_ids': raw}
            self.assertIsNone(tg_access_owner(config))
            self.assertIsNone(configured_owner(config))
            for actor in (101, 202):
                self.assertTrue(owner_permitted(config, actor))
                self.assertTrue(bot.allowed_actor(actor, config))
            for actor in (0, -1, True, None, '101', 2**63):
                self.assertFalse(owner_permitted(config, actor))
        self.assertEqual(tg_access_owner({'tg_allowed_user_ids': '101'}), 101)
        self.assertTrue(owner_permitted({'tg_allowed_user_ids': '101'}, 101))
        self.assertFalse(owner_permitted({'tg_allowed_user_ids': '101'}, 202))
        for config in ({}, None, {'tg_allowed_user_ids': None}, {'tg_allowed_user_ids': False},
                       {'tg_allowed_user_ids': 'bad'}, {'tg_allowed_user_ids': '101,202'}):
            self.assertEqual(tg_access_owner(config), 0)
            self.assertFalse(owner_permitted(config, 101))

    def test_public_bot_menu_registers_all_private_chats_without_global_chat_id(self):
        store, tg = FakeStore(), FakeTG()
        store.config['tg_allowed_user_ids'] = ''
        instance = bot.Bot('fake', store, tg=tg, client_factory=ClientFactory(), synchronous=True)
        self.addCleanup(store.close)
        self.addCleanup(instance.stop)
        tg.on_poll = lambda _: instance.stop()
        instance.run()
        menus = [body for method, body in tg.api_calls if method == 'setMyCommands']
        buttons = [body for method, body in tg.api_calls if method == 'setChatMenuButton']
        self.assertEqual(menus[0]['scope'], {'type': 'all_private_chats'})
        self.assertNotIn('chat_id', buttons[0])
        self.assertEqual(buttons[0]['menu_button'], {'type': 'commands'})


class PublicAccountTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='emos-public-access-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = fixture_app(self, self.root, 'public_access_app', single_owner=True)
        self.app.init_db()
        configure(self.app, tg_allowed_user_ids='', tg_bot_token='123456:public_fixture_token',
                  web_base_url='https://cdn.fixture.invalid', sign_time='08:00')
        self.service = self.app.Store().accounts
        self.web = self.app.personal_web()

    def primary(self, actor, official):
        self.service.ensure_user(actor, actor)
        self.service.begin_login(actor, official, expected_root=0)
        return self.service.finish_login(actor, '11_public_' + official,
            {'_user_id': official, '_display': official, '_carrot': actor, 'telegram_user_id': actor}, expected_root=0)

    def test_two_public_users_own_login_token_scope_sessions_and_notifications(self):
        first, second = self.primary(101, 'PRIMARY_A'), self.primary(202, 'PRIMARY_B')
        extra = self.service.login_with_token(101, '11_public_extra',
            {'_user_id': 'EXTRA_A', '_display': 'Extra', '_carrot': 25}, expected_root=first['id'])
        foreign_extra = self.service.login_with_token(202, '11_public_other_extra',
            {'_user_id': 'EXTRA_B', '_display': 'Other', '_carrot': 28}, expected_root=second['id'])
        with self.app.write_db() as conn:
            conn.execute('INSERT INTO account_links VALUES(?,?,1,?)', (extra['id'], foreign_extra['id'], 'old'))
        self.assertEqual({row['id'] for row in self.service.visible(101)}, {first['id'], extra['id']})
        self.assertEqual({row['id'] for row in self.service.visible(202)}, {second['id'], foreign_extra['id']})
        self.assertFalse(self.service.can_run(101, extra['id'], foreign_extra['id']))
        self.assertEqual([row['tg_id'] for row in self.service.watchers(foreign_extra['id'])], [202])
        self.assertEqual(self.service.run_scope(101)['account_ids'], [extra['id'], first['id']])
        with self.assertRaises(DomainError):
            self.service.switch(101, second['id'])
        with self.assertRaises(DomainError):
            self.service.toggle_link(101, extra['id'], foreign_extra['id'])
        import urllib.parse
        first_url = self.web.issue_url(101)
        code = urllib.parse.parse_qs(urllib.parse.urlsplit(first_url).fragment)['code'][0]
        first_session = self.web.exchange(code)
        second_url = self.web.issue_url(202)
        code = urllib.parse.parse_qs(urllib.parse.urlsplit(second_url).fragment)['code'][0]
        session = self.web.exchange(code)
        self.assertEqual(self.web.authenticate(first_session['sid'])['tg_id'], 101)
        self.assertEqual(self.web.authenticate(session['sid'])['tg_id'], 202)
        self.assertEqual({row['id'] for row in self.web.state(101)['owned']}, {first['id'], extra['id']})
        cfg = self.app.cfg_all()
        with patch.object(self.app, 'now_local', return_value=datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)), \
                patch.object(self.app, '_run_ids', return_value=[]) as run:
            self.app.scheduler_tick(cfg)
            self.assertEqual(set(run.call_args.args[1]),
                             {first['id'], second['id'], extra['id'], foreign_extra['id']})
            self.assertEqual(run.call_args.kwargs['scopes'][foreign_extra['id']], [(202, foreign_extra['id'])])
        with patch('bot.TG.send') as sender:
            self.assertTrue(self.app._deliver_notification((foreign_extra['id'], 202, 'notify_success',
                                    {'status': 'signed', 'name': 'Other', 'carrot': 3, 'message': '签到成功'})))
            self.assertEqual(sender.call_args.args[0], 202)
        configure(self.app, tg_allowed_user_ids='101')
        self.assertIsNone(self.web.authenticate(session['sid']))
        with self.assertRaises(DomainError):
            self.service.visible(202)
        self.assertEqual({row['id'] for row in self.service.visible(101)}, {first['id'], extra['id']})
        configure(self.app, tg_allowed_user_ids='')
        self.assertEqual({row['id'] for row in self.service.visible(202)}, {second['id'], foreign_extra['id']})

    def test_open_policy_does_not_allow_token_before_own_official_primary(self):
        self.primary(101, 'PRIMARY_A')
        self.service.ensure_user(202, 202)
        with self.assertRaises(DomainError):
            self.service.login_with_token(202, '11_unproved', {'_user_id': 'UNPROVED'})
        self.assertFalse(self.service.has_primary_account(202))

    def test_policy_change_inside_transaction_rolls_back_even_if_actor_stays_allowed(self):
        self.primary(101, 'PRIMARY_A')
        original = self.service._write
        @contextmanager
        def racing_write():
            with original() as conn:
                configure(self.app, tg_allowed_user_ids='101')
                yield conn
        self.service._write = racing_write
        with self.assertRaises(DomainError):
            self.service.set_preferences(101, notify_success=0)
        self.assertEqual(self.service.user(101)['notify_success'], 1)
