"""一个获准 TG 操作者管理多个独立 emos Token 的真实 HTTP 回归。

仅用隔离目录、合成身份或 localhost；正式授权和真实生产资源不在此验收。
Test: python3 -B -m unittest test_multi_accounts"""
from contextlib import contextmanager
from datetime import datetime, timezone
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import bot
from test_bot import FakeTG
from test_runtime import Fixture, serve
from test_support import configure, fixture_app


class TokenHTTP(Fixture):
    def identity(self):
        token = (self.headers.get('Authorization') or '').removeprefix('Bearer ')
        self.server.credentials.append((self.command, self.path.split('?')[0], token))
        return super().identity()

    def do_GET(self):
        token = (self.headers.get('Authorization') or '').removeprefix('Bearer ')
        if token not in self.server.identities:
            self.server.credentials.append((self.command, self.path, token))
            return self.raw(401, {'message': 'Unauthenticated'})
        return super().do_GET()


class MultiAccountTests(unittest.TestCase):
    TOKENS = {'ePrimary': 'primary_multi_fixture', 'eOther': 'other_multi_fixture',
              'eUnbound': 'unbound_multi_fixture'}

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='emos-independent-accounts-')
        self.addCleanup(temporary.cleanup)
        self.app = fixture_app(self, temporary.name, 'multi_account_app', single_owner=True)
        self.app.init_db()
        self.addCleanup(self.app.close_idle_db)
        self.tg = FakeTG()
        self.bot = bot.Bot('fixture:bot', self.app.Store(), tg=self.tg, synchronous=True)
        self.bot._accept_me(self.tg.get_me())
        self.addCleanup(self.bot.stop)
        self.serial = 0
        clock = patch.object(self.app, 'now_local', return_value=datetime(2026, 11, 9, 9, tzinfo=timezone.utc))
        clock.start()
        self.addCleanup(clock.stop)
        wait = patch.object(self.app._shutdown, 'wait', return_value=False)
        wait.start()
        self.addCleanup(wait.stop)

    def message(self, text, actor=101):
        self.serial += 1
        self.bot._handle({'update_id': self.serial, 'message': {
            'message_id': self.serial, 'from': {'id': actor},
            'chat': {'id': actor, 'type': 'private'}, 'text': text}})
        self.assertTrue(self.bot.join_jobs(3))

    @contextmanager
    def logged_accounts(self):
        with serve(TokenHTTP) as (server, base):
            server.credentials = []
            server.identities = {
                self.TOKENS['ePrimary']: {'user_id': 'ePrimary', 'telegram_user_id': 101, 'carrot': 40},
                self.TOKENS['eOther']: {'user_id': 'eOther', 'telegram_user_id': 202, 'carrot': 777},
                self.TOKENS['eUnbound']: {'user_id': 'eUnbound', 'telegram_user_id': None, 'carrot': 12}}
            configure(self.app, base_url=base)
            self.message('/login ePrimary')
            self.message('/start emosLinkAgree-' + self.TOKENS['ePrimary'])
            self.message('/token ' + self.TOKENS['eOther'])
            self.message('/token')
            self.message(self.TOKENS['eUnbound'])
            service = self.app.Store().accounts
            own = service.owned(101)
            self.assertEqual({a['emos_id'] for a in own}, set(self.TOKENS))
            self.assertEqual(service.primary_account(101)['emos_id'], 'ePrimary')
            with self.app.db() as conn:
                self.assertEqual({row['owner_tg_id'] for row in conn.execute('SELECT owner_tg_id FROM accounts')}, {101})
            yield server, service

    def test_manual_sign_uses_each_token_and_foreign_actor_is_blocked(self):
        with self.logged_accounts() as (server, service):
            server.credentials.clear()
            self.message('/signall')
            self.assertEqual(server.signed, set(self.TOKENS))
            puts = [token for method, path, token in server.credentials
                    if method == 'PUT' and path == '/api/user/sign']
            self.assertCountEqual(puts, list(self.TOKENS.values()))
            self.assertEqual(len(puts), 3)
            messages = '\n'.join(action[2] for action in self.tg.actions if action[0] in {'send', 'edit'})
            for token in self.TOKENS.values():
                self.assertNotIn(token, messages)
            before = list(server.credentials)
            self.message('/sign', actor=202)
            self.message('/token ' + self.TOKENS['eOther'], actor=202)
            self.assertEqual(server.credentials, before)
            self.assertEqual(len(service.owned(101)), 3)

    def test_scheduler_and_web_sign_independent_accounts_and_show_balances(self):
        with self.logged_accounts() as (server, service):
            with self.app.write_db() as conn:
                conn.execute("UPDATE accounts SET sign_time='01:00',auto_sign=1")
            rows = self.app.scheduler_tick()
            self.assertEqual({row['emos_id'] for row in rows}, set(self.TOKENS))
            self.assertEqual(server.signed, set(self.TOKENS))
            web = self.app.personal_web()
            self.addCleanup(web.stop)
            state = web.state(101)
            self.assertEqual({row['emos_id']: row['carrot'] for row in state['owned']},
                             {'ePrimary': 40, 'eOther': 777, 'eUnbound': 12})
            server.signed.clear()
            with self.app.write_db() as conn:
                conn.execute("UPDATE accounts SET last_day='',retry_day='',next_run_at=0")
            started = web.start_sign(101, {'account_id': state['active']['id'],
                                          'request_id': 'independent_accounts_web_0001', 'all_accounts': True})
            worker = web.worker
            if worker is not None:
                worker.join(5)
                self.assertFalse(worker.is_alive())
            job = web.job(101, started['job_id'])
            self.assertEqual(job['status'], 'done')
            self.assertEqual({row['emos_id'] for row in job['rows']}, set(self.TOKENS))
            self.assertEqual(server.signed, set(self.TOKENS))

    def test_extra_remote_tg_changes_do_not_disable_valid_token(self):
        with self.logged_accounts() as (server, service):
            extra = next(a for a in service.owned(101) if a['emos_id'] == 'eOther')
            service.switch(101, extra['id'])
            for binding in (303, None, ''):
                with self.subTest(binding=binding):
                    server.identities[self.TOKENS['eOther']]['telegram_user_id'] = binding
                    rows = self.app.run_user(101, include_links=False, expected_root=extra['id'])
                    self.assertEqual(len(rows), 1)
                    self.assertIn(rows[0]['status'], ('signed', 'already'))
                    self.assertEqual(self.app._account_record(extra['id'])['enabled'], 1)

    def test_invalid_token_or_changed_account_id_is_still_rejected(self):
        with self.logged_accounts() as (server, service):
            before = len(service.owned(101))
            self.message('/token invalid_multi_fixture')
            self.assertEqual(len(service.owned(101)), before)
            extra = next(a for a in service.owned(101) if a['emos_id'] == 'eOther')
            service.switch(101, extra['id'])
            server.identities[self.TOKENS['eOther']]['user_id'] = 'eWrongIdentity'
            server.credentials.clear()
            rows = self.app.run_user(101, include_links=False, expected_root=extra['id'])
            self.assertEqual([row['status'] for row in rows], ['invalid'])
            self.assertFalse(any(method == 'PUT' for method, _, _ in server.credentials))
            self.assertEqual(service.primary_account(101)['emos_id'], 'ePrimary')

    def callback(self, data, actor=101):
        self.serial += 1
        self.bot._handle({'update_id': self.serial, 'callback_query': {
            'id': 'scope_callback_%d' % self.serial, 'from': {'id': actor}, 'data': data,
            'message': {'message_id': self.serial, 'chat': {'id': actor, 'type': 'private'}}}})
        self.assertTrue(self.bot.join_jobs(3))

    def test_sign_command_only_uses_selected_account_and_its_content(self):
        with self.logged_accounts() as (server, service):
            selected = service.active(101)
            server.credentials.clear()
            self.message('/sign 今日打卡')
            self.assertEqual(server.signed, {selected['emos_id']})
            self.assertEqual({token for _, _, token in server.credentials},
                             {self.TOKENS[selected['emos_id']]})
            self.assertEqual(len(server.puts), 1)
            path, body = server.puts[0]
            self.assertEqual(parse_qs(urlsplit(path).query), {'content': ['今日打卡']})
            self.assertEqual(body, b'')
            with self.app.db() as conn:
                days = {row['emos_id']: row['last_day'] for row in
                        conn.execute('SELECT emos_id,last_day FROM accounts')}
            self.assertEqual(days[selected['emos_id']], '2026-11-09')
            self.assertTrue(all(not day for name, day in days.items()
                                if name != selected['emos_id']))

    def test_current_and_all_buttons_send_distinct_real_requests(self):
        with self.logged_accounts() as (server, service):
            selected = service.active(101)
            buttons = {b['text']: b['callback_data'] for row in bot.menu_kb(selected) for b in row}
            self.assertEqual(buttons['✅ 签到当前账号'], 'sign:%d' % selected['id'])
            self.assertEqual(buttons['✅ 签到全部账号'], 'signall:%d' % selected['id'])
            self.callback(buttons['✅ 签到当前账号'])
            self.assertEqual(server.signed, {selected['emos_id']})
            server.signed.clear()
            server.credentials.clear()
            with self.app.write_db() as conn:
                conn.execute("UPDATE accounts SET last_day='',retry_day='',next_run_at=0")
            self.callback(buttons['✅ 签到全部账号'])
            self.assertEqual(server.signed, set(self.TOKENS))
            puts = [token for method, path, token in server.credentials
                    if method == 'PUT' and path == '/api/user/sign']
            self.assertCountEqual(puts, list(self.TOKENS.values()))

    def test_stale_single_and_all_buttons_never_sign_another_account(self):
        with self.logged_accounts() as (server, service):
            old = service.active(101)
            primary = service.primary_account(101)
            service.switch(101, primary['id'])
            server.credentials.clear()
            for prefix in ('sign', 'signall'):
                self.callback('%s:%d' % (prefix, old['id']))
            self.assertEqual(server.credentials, [])
            self.assertEqual(server.signed, set())

    def test_web_defaults_to_current_and_requires_explicit_all_scope(self):
        with self.logged_accounts() as (server, service):
            web = self.app.personal_web()
            self.addCleanup(web.stop)
            selected = service.active(101)
            data = {'account_id': selected['id'], 'request_id': 'scope_web_current_0001'}
            first = web.start_sign(101, data)
            worker = web.worker
            if worker is not None:
                worker.join(5)
                self.assertFalse(worker.is_alive())
            job = web.job(101, first['job_id'])
            self.assertEqual(job['status'], 'done')
            self.assertFalse(job['all_accounts'])
            self.assertEqual([row['emos_id'] for row in job['rows']], [selected['emos_id']])
            self.assertEqual(server.signed, {selected['emos_id']})
            before = list(server.credentials)
            self.assertEqual(web.start_sign(101, data)['job_id'], first['job_id'])
            with self.assertRaises(bot.DomainError):
                web.start_sign(101, dict(data, all_accounts=True))
            self.assertEqual(server.credentials, before)
            server.signed.clear()
            with self.app.write_db() as conn:
                conn.execute("UPDATE accounts SET last_day='',retry_day='',next_run_at=0")
            second = web.start_sign(101, dict(data, request_id='scope_web_all_0001', all_accounts=True))
            worker = web.worker
            if worker is not None:
                worker.join(5)
                self.assertFalse(worker.is_alive())
            job = web.job(101, second['job_id'])
            self.assertNotEqual(first['job_id'], second['job_id'])
            self.assertEqual(job['status'], 'done')
            self.assertTrue(job['all_accounts'])
            self.assertEqual({row['emos_id'] for row in job['rows']}, set(self.TOKENS))
            self.assertEqual(server.signed, set(self.TOKENS))


if __name__ == '__main__':
    unittest.main()
