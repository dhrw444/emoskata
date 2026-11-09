"""真实账号域驱动的私聊菜单、按钮和输入流程回归。

仅用隔离目录、合成身份或 localhost；正式授权和真实生产资源不在此验收。
Test: python3 -B -m unittest test_owner_bot"""
import tempfile
import unittest

import bot
from test_bot import FakeTG
from test_support import fixture_app, configure


class OwnerBotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='emos-owner-bot-')
        self.addCleanup(self.tmp.cleanup)
        self.app = fixture_app(self, self.tmp.name, 'owner_bot_fixture', single_owner=True)
        self.app.init_db()
        self.addCleanup(self.app.close_idle_db)
        self.tg = FakeTG()
        self.profiles = {
            'primary_token_123': {'user_id': 'ePrimary', 'telegram_user_id': 101, '_carrot': 50},
            'extra_token_12345': {'user_id': 'eExtra', 'telegram_user_id': 202, '_carrot': 777},
            'third_token_12345': {'user_id': 'eThird', 'telegram_user_id': None, '_carrot': None},
        }
        self.requests = []
        test = self
        class Client:
            def __init__(self, token, base):
                self.token = token
                test.requests.append(token)
            def profile(self):
                return dict(test.profiles[self.token])
        self.b = bot.Bot('fake:bot', self.app.Store(), tg=self.tg, client_factory=Client, synchronous=True)
        self.b._accept_me(self.tg.get_me())
        self.addCleanup(self.b.stop)
        self.serial = 0

    def message(self, text, actor=101, chat=None, group=False):
        self.serial += 1
        self.b._handle({'update_id': self.serial, 'message': {
            'message_id': 1000 + self.serial, 'from': {'id': actor, 'is_bot': False},
            'chat': {'id': actor if chat is None else chat, 'type': 'group' if group else 'private'},
            'text': text}})
        self.assertTrue(self.b.join_jobs(2))

    def callback(self, data, actor=101):
        self.serial += 1
        self.b._handle({'update_id': self.serial, 'callback_query': {
            'id': 'owner_cb_%d' % self.serial, 'from': {'id': actor}, 'data': data,
            'message': {'message_id': 9000, 'chat': {'id': actor, 'type': 'private'}}}})
        self.assertTrue(self.b.join_jobs(2))

    def text(self):
        return '\n'.join(a[2] for a in self.tg.actions if a[0] in {'send', 'edit'})

    def primary(self):
        self.message('/login ePrimary')
        self.message('/start emosLinkAgree-primary_token_123')
        row = self.app.Store().accounts.primary_account(101)
        self.assertIsNotNone(row, self.text())
        return row

    def test_token_requires_official_primary_without_network_request(self):
        self.message('/token')
        self.message('/token extra_token_12345')
        self.assertEqual(self.requests, [])
        self.assertIsNone(self.app.Store().accounts.primary_account(101))
        self.assertIn('官方', self.text())

    def test_token_prompt_and_saved_message_are_brief(self):
        self.primary()
        self.tg.actions.clear()
        before = len(self.requests)
        self.message('/token')
        messages = [a for a in self.tg.actions if a[0] in {'send', 'edit'}]
        self.assertEqual([a[2] for a in messages], ['发送该账号的 emos Token。'])
        self.assertEqual(len(self.requests), before)
        self.assertEqual(messages[0][3], [[{'text': '取消', 'callback_data': 'cancel'}]])
        self.tg.actions.clear()
        self.message('extra_token_12345')
        messages = [a for a in self.tg.actions if a[0] in {'send', 'edit'}]
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0][2], '⏳ 正在验证…')
        self.assertTrue(messages[1][2].startswith('✅ 已保存 '))
        self.assertEqual(len(messages[1][2].splitlines()), 2)
        self.assertIn('777', messages[1][2])
        self.assertNotIn('extra_token_12345', self.text())
        buttons = [button for row in messages[1][3] for button in row]
        self.assertEqual([button['text'] for button in buttons], ['继续添加', '菜单'])
        self.assertEqual(self.app.Store().accounts.input_state(101), '')
        self.tg.actions.clear()
        self.callback(buttons[0]['callback_data'])
        self.assertEqual(self.app.Store().accounts.input_state(101), 'token')
        self.assertEqual(self.text(), '发送该账号的 emos Token。')
        self.message('/cancel')
        self.assertEqual(self.app.Store().accounts.input_state(101), '')

    def test_inline_and_guided_tokens_add_real_domain_accounts(self):
        primary = self.primary()
        self.message('/token')
        self.assertEqual(self.app.Store().accounts.input_state(101), 'token')
        self.message('extra_token_12345')
        self.message('/token third_token_12345')
        service = self.app.Store().accounts
        self.assertEqual(len(service.owned(101)), 3)
        self.assertEqual(service.primary_account(101)['id'], primary['id'])
        self.assertEqual(len(service.run_scope(101)['account_ids']), 3)
        deleted = {a[2] for a in self.tg.actions if a[0] == 'delete'}
        self.assertGreaterEqual(len(deleted), 3)
        for token in self.profiles:
            self.assertNotIn(token, self.text())
        self.assertNotIn('输入会话类型无效', self.text())

    def test_login_reauthorizes_primary_even_after_switch_to_token_account(self):
        self.primary()
        self.message('/token extra_token_12345')
        self.tg.actions.clear()
        self.message('/login')
        urls = [b['url'] for a in self.tg.actions if a[0] in {'send', 'edit'}
                for row in a[3] or [] for b in row if 'url' in b]
        self.assertIn('https://t.me/emospg_bot?start=link_ePrimary-emos_test_bot', urls)
        self.message('/login eExtra')
        self.assertIn('其他账号', self.text())
        self.assertFalse(any('link_eExtra' in u for u in urls))

    def test_foreign_actor_group_and_wrong_chat_cannot_operate(self):
        self.message('/login ePrimary', actor=202)
        self.message('/login ePrimary', chat=202)
        self.message('/login ePrimary', group=True)
        self.assertEqual(self.requests, [])
        self.assertIsNone(self.app.Store().accounts.primary_account(101))
        self.assertEqual(self.tg.actions, [])

    def test_command_schema_and_real_registered_scope(self):
        expected = bot.command_menu()
        self.assertLessEqual(len(expected), 100)
        self.assertEqual(len({e['command'] for e in expected}), len(expected))
        for entry in expected:
            self.assertRegex(entry['command'], r'^[a-z0-9_]{1,32}$')
            self.assertTrue(1 <= len(entry['description']) <= 256)
        self.tg.on_poll = lambda _: self.b.stop()
        self.b.run()
        registered = [p for m, p in self.tg.api_calls if m == 'setMyCommands']
        self.assertEqual(registered, [{'commands': expected, 'scope': {'type': 'chat', 'chat_id': 101}}])
        menus = [p for m, p in self.tg.api_calls if m == 'setChatMenuButton']
        self.assertEqual(menus, [{'chat_id': 101, 'menu_button': {'type': 'commands'}}])

    def test_every_registered_command_has_real_handler(self):
        primary = self.primary()
        observed = []
        def sign(tg_id, include_links=True, expected_root=None, content=None):
            observed.append((tg_id, expected_root))
            return []
        self.b.store.run_user = sign
        for entry in bot.command_menu():
            cmd = entry['command']
            self.tg.actions.clear()
            self.message('/' + cmd + (' 08:15' if cmd == 'time' else ''))
            self.assertNotIn('不支持此命令', self.text(), cmd)
            self.assertNotIn('内部处理失败', self.text(), cmd)
            self.assertNotIn('输入会话类型无效', self.text(), cmd)
            if cmd == 'token':
                self.assertEqual(self.app.Store().accounts.input_state(101), 'token')
                self.message('/cancel')
        self.assertIn((101, primary['id']), observed)

    def test_generated_buttons_match_valid_callbacks(self):
        primary = self.primary()
        self.message('/token extra_token_12345')
        self.app.Store().accounts.switch(101, primary['id'])
        views = [self.b._view_accounts(101), self.b._view_settings(101), self.b._view_logs(101)]
        keyboards = [bot.menu_kb(primary)] + [keyboard for _, keyboard in views]
        allowed = {'sign', 'signall', 'accounts', 'settings', 'logs', 'login', 'token', 'web', 'help',
                   'mode', 'auto', 'guide', 'pref', 'menu', 'switch', 'cancel', 'relogin', 'hide', 'logout'}
        for keyboard in keyboards:
            for row in keyboard:
                for button in row:
                    self.assertEqual(len(set(button) & {'url', 'callback_data'}), 1)
                    data = button.get('callback_data')
                    if data is None:
                        continue
                    self.assertTrue(1 <= len(data.encode('utf-8')) <= 64)
                    self.assertIn(data.split(':')[0], allowed)
                    self.assertNotIn('link', data)
        self.b.store.run_user = lambda *a, **k: []
        root = primary['id']
        for data in ['menu', 'help', 'cancel', 'accounts:%d:0' % root,
                     'settings:%d' % root, 'logs:%d:0' % root, 'mode:%d:random' % root,
                     'mode:%d:normal' % root, 'auto:%d' % root, 'pref:notify_success',
                     'guide:time:%d' % root, 'token:%d' % root, 'cancel',
                     'login:%d' % root, 'cancel', 'sign:%d' % root, 'signall:%d' % root]:
            self.tg.actions.clear()
            self.callback(data)
            self.assertNotIn('按钮已失效或参数无效', self.text(), data)
            self.assertNotIn('内部处理失败', self.text(), data)

    def test_invalid_modes_pagination_and_shared_callbacks_fail_closed(self):
        root = self.primary()['id']
        original = self.app.Store().accounts.active(101)['sign_mode']
        for data in ['mode:%d:wrong' % root, 'accounts:%d:-1' % root,
                     'links:%d:0' % root, 'code:%d' % root, 'review:%d:1:2:1' % root]:
            self.callback(data)
        self.assertEqual(self.app.Store().accounts.active(101)['sign_mode'], original)
        self.assertNotIn('一次性码：', self.text())
        self.assertIn('跨 TG 共享', self.text())

    def test_owner_change_disables_menu_processing_immediately(self):
        self.primary()
        configure(self.app, tg_allowed_user_ids='202')
        before = len(self.tg.actions)
        self.message('/token extra_token_12345')
        self.callback('menu')
        self.assertEqual(len(self.requests), 1)
        self.assertFalse(any(a[0] in {'send', 'edit'} for a in self.tg.actions[before:]))
        self.assertIsNone(self.app.Store().accounts.primary_account(101))


if __name__ == '__main__':
    unittest.main()
