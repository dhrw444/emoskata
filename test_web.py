"""个人网页一次性码、会话、任务与旧数据库兼容回归。

仅用隔离目录、合成身份或 localhost；正式授权和真实生产资源不在此验收。
Test: python3 -B -m unittest test_web"""
import contextlib
import hashlib
import json
from pathlib import Path
import queue
import re
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from accounts import DomainError
from test_runtime import FakeTG, serve
import bot
import webui

ROOT = Path(__file__).resolve().parent


class WebTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='emos-personal-web-')
        self.addCleanup(self.tmp.cleanup)
        from test_support import fixture_app, configure
        self.app = fixture_app(self, self.tmp.name, 'web_test_app')
        self.app.init_db()
        self.app.init_password()
        configure(self.app, web_base_url='https://sign.example.com')
        self.service = self.app.personal_web()
        self.accounts = self.service.accounts
        self.actors = {'A': 101, 'B': 202, 'C': 303}
        self.ids = {}
        for name, actor in self.actors.items():
            self.accounts.begin_login(actor, 'e' + name)
            row = self.accounts.finish_login(actor, '11_fixture_' + name,
                {'user_id': 'e' + name, 'username': 'Private_' + name, 'telegram_user_id': actor},
                'http://127.0.0.1:1')
            self.ids[name] = row['id']
        self.addCleanup(self.service.stop)

    def code(self, actor):
        return parse_qs(urlsplit(self.service.issue_url(actor)).fragment)['code'][0]

    def login(self, actor):
        return self.service.exchange(self.code(actor))

    def link(self, source='A', target='B'):
        code = self.accounts.issue_code(self.actors[target])
        result = self.service.action(self.actors[source], 'link', {
            'account_id': self.ids[source], 'target_emos_id': 'e' + target, 'code': code['code']})
        self.assertTrue(result['pending'])
        self.assertFalse(self.accounts.can_run(self.actors[source], self.ids[source], self.ids[target]))
        self.service.action(self.actors[target], 'review', {'account_id': self.ids[target],
            'source_id': self.ids[source], 'target_id': self.ids[target], 'approve': True})
        return result

    @contextlib.contextmanager
    def panel(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), self.app.Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            yield server
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=3)

    def request(self, server, path, method='GET', data=None, cookie='', csrf=None, origin=None):
        conn = HTTPConnection('127.0.0.1', server.server_address[1], timeout=5)
        headers = {'Content-Type': 'application/json'}
        if cookie:
            headers['Cookie'] = cookie
        if csrf is not None:
            headers['X-CSRF-Token'] = csrf
        if origin is not None:
            headers['Origin'] = origin
        conn.request(method, path, json.dumps(data) if data is not None else None, headers)
        result = conn.getresponse()
        body = result.read().decode()
        value = (result.status, dict(result.getheaders()), body)
        conn.close()
        return value

    def wait_job(self, job_id):
        self.service.queue.join()
        return self.service.job(101, job_id)

    def test_each_login_identity_is_self_no_shared_account_pool(self):
        a, b = self.login(101), self.login(202)
        self.assertNotEqual(a['sid'], b['sid'])
        self.assertEqual(self.service.authenticate(a['sid'])['tg_id'], 101)
        self.assertEqual(self.service.authenticate(b['sid'])['tg_id'], 202)
        for actor, name in ((101, 'A'), (202, 'B'), (303, 'C')):
            value = self.service.state(actor)
            self.assertEqual([r['emos_id'] for r in value['owned']], ['e' + name])
            self.assertEqual(value['targets'], [])
            self.assertEqual(value['links'], {'incoming': [], 'outgoing': []})
            self.assertNotIn('11_fixture_', json.dumps(value))
            for other in {'A', 'B', 'C'} - {name}:
                self.assertNotIn('Private_' + other, json.dumps(value))

    def test_code_and_sessions_hash_only_in_db_not_query_or_logs(self):
        url = self.service.issue_url(101)
        self.assertEqual(urlsplit(url).path, '/web/auth')
        self.assertFalse(urlsplit(url).query)
        code = parse_qs(urlsplit(url).fragment)['code'][0]
        with self.app.db() as conn:
            row = dict(conn.execute('SELECT * FROM web_auth_codes').fetchone())
        self.assertEqual(row['code_hash'], hashlib.sha256(code.encode()).hexdigest())
        self.assertNotIn(code, json.dumps(row))
        login = self.service.exchange(code)
        with self.app.db() as conn:
            stored = dict(conn.execute('SELECT * FROM web_sessions').fetchone())
        self.assertEqual(stored['sid_hash'], hashlib.sha256(login['sid'].encode()).hexdigest())
        self.assertNotIn(login['sid'], json.dumps(stored))

    def test_code_replay_rotation_and_expiry_all_rejected(self):
        old = self.code(101)
        new = self.code(101)
        with self.assertRaises(DomainError):
            self.service.exchange(old)
        self.service.exchange(new)
        with self.assertRaises(DomainError):
            self.service.exchange(new)
        with patch.object(webui.time, 'time', return_value=1000):
            expired = self.code(101)
        with patch.object(webui.time, 'time', return_value=1120), self.assertRaises(DomainError):
            self.service.exchange(expired)

    def test_one_web_code_concurrent_exchange_exactly_once(self):
        code = self.code(101)
        barrier = threading.Barrier(5)
        result = queue.Queue()
        def exchange():
            barrier.wait(timeout=5)
            try:
                self.service.exchange(code)
                result.put('ok')
            except DomainError:
                result.put('denied')
        workers = [threading.Thread(target=exchange) for _ in range(4)]
        for worker in workers:
            worker.start()
        barrier.wait(timeout=5)
        for worker in workers:
            worker.join(timeout=8)
            self.assertFalse(worker.is_alive())
        values = [result.get_nowait() for _ in range(4)]
        self.assertEqual(values.count('ok'), 1)
        self.assertEqual(values.count('denied'), 3)

    def test_arbitrary_tg_id_sid_and_removed_owner_do_not_authenticate(self):
        self.assertIsNone(self.service.authenticate('x' * 43))
        self.assertIsNone(self.service.authenticate('101'))
        login = self.login(101)
        with self.app.write_db() as conn:
            conn.execute('UPDATE accounts SET owner_tg_id=NULL WHERE id=?', (self.ids['A'],))
        self.assertIsNone(self.service.authenticate(login['sid']))

    def test_public_pages_have_matching_nonce_and_code_fragment_removed(self):
        with self.panel() as server:
            code, headers, page = self.request(server, '/web/auth')
            self.assertEqual(code, 200)
            nonce = re.search(r'<script nonce="([A-Za-z0-9_-]+)">', page)[1]
            self.assertIn("script-src 'nonce-" + nonce + "'", headers['Content-Security-Policy'])
            self.assertIn('<style nonce="' + nonce + '">', page)
            self.assertIn('URLSearchParams(location.hash', page)
            self.assertIn("history.replaceState(null,'','/web/auth')", page)
            self.assertNotIn('localStorage', page)
            self.assertEqual(headers['Referrer-Policy'], 'no-referrer')
            self.assertEqual(headers['X-Frame-Options'], 'DENY')

    def test_admin_and_personal_cookie_roles_never_mix(self):
        login = self.login(101)
        admin = self.app.create_session('127.0.0.1', 'fixture')
        with self.panel() as server:
            for path in ('/api/config', '/api/accounts', '/api/logs'):
                self.assertEqual(self.request(server, path, cookie='wsid=' + login['sid'])[0], 401)
            self.assertEqual(self.request(server, '/', cookie='wsid=' + login['sid'])[0], 302)
            self.assertEqual(self.request(server, '/api/web/me', cookie='sid=' + admin)[0], 401)
            self.assertEqual(self.request(server, '/api/web/me', cookie='sid=' + login['sid'])[0], 401)
            self.assertEqual(self.request(server, '/api/web/me', cookie='wsid=' + admin)[0], 401)

    def test_http_login_only_one_time_code_sets_http_only_secure_cookie(self):
        with self.panel() as server:
            code = self.code(101)
            status, headers, _ = self.request(server, '/api/web/login', 'POST', {'code': code})
            self.assertEqual(status, 200)
            cookie = headers['Set-Cookie']
            self.assertIn('wsid=', cookie)
            for required in ('HttpOnly', 'Secure', 'SameSite=Strict'):
                self.assertIn(required, cookie)
            self.assertEqual(self.request(server, '/api/web/login', 'POST', {'code': code})[0], 400)
            self.assertEqual(self.request(server, '/api/web/login', 'POST', {'tg_id': 202})[0], 400)

    def test_cross_user_settings_switch_code_and_job_denied(self):
        a = self.login(101)
        bad = [
            ('settings', {'account_id': self.ids['B'], 'auto_sign': False}),
            ('switch', {'account_id': self.ids['A'], 'target_id': self.ids['B']}),
            ('code', {'account_id': self.ids['B']}),
            ('toggle', {'account_id': self.ids['A'], 'source_id': self.ids['B'], 'target_id': self.ids['C'], 'enabled': False}),
            ('unlink', {'account_id': self.ids['A'], 'source_id': self.ids['B'], 'target_id': self.ids['C']}),
        ]
        with self.panel() as server:
            for action, data in bad:
                with self.subTest(action=action):
                    self.assertEqual(self.request(server, '/api/web/' + action, 'POST', data,
                        'wsid=' + a['sid'], a['csrf_token'])[0], 400)
        self.assertEqual(self.accounts.active(202)['auto_sign'], 1)
        self.assertEqual(self.accounts.active(101)['emos_id'], 'eA')

    def test_csrf_required_invalid_unicode_and_cross_origin_fail_without_action(self):
        a = self.login(101)
        data = {'account_id': self.ids['A'], 'auto_sign': False}
        with self.panel() as server:
            cookie = 'wsid=' + a['sid']
            for csrf in (None, '', 'wrong', 'é' * 32):
                with self.subTest(csrf_type='none' if csrf is None else 'invalid'):
                    self.assertEqual(self.request(server, '/api/web/settings', 'POST', data, cookie, csrf)[0], 403)
            self.assertEqual(self.request(server, '/api/web/settings', 'POST', data, cookie,
                                          a['csrf_token'], 'https://evil.example')[0], 403)
            self.assertEqual(self.request(server, '/api/web/settings', 'POST', data, cookie,
                                          a['csrf_token'], 'https://sign.example.com')[0], 200)
        self.assertEqual(self.accounts.active(101)['auto_sign'], 0)
        self.assertEqual(self.accounts.active(202)['auto_sign'], 1)

    def test_association_is_not_login_to_target_and_no_transitive_detail(self):
        self.link('A', 'B')
        self.link('B', 'C')
        value = self.service.state(101)
        self.assertEqual([r['emos_id'] for r in value['owned']], ['eA'])
        self.assertEqual(value['active']['emos_id'], 'eA')
        self.assertEqual([r['emos_id'] for r in value['targets']], ['eB'])
        self.assertEqual(set(value['targets'][0]), {'id', 'emos_id', 'name', 'last_status', 'last_carrot', 'last_run'})
        self.assertNotIn('Private_C', json.dumps(value))
        for field in ('token', 'owner_tg_id', 'base_url', 'auto_sign', 'sign_time'):
            self.assertNotIn(field, value['targets'][0])
        with self.assertRaises(DomainError):
            self.service.action(101, 'switch', {'account_id': self.ids['A'], 'target_id': self.ids['B']})
        with self.assertRaises(DomainError):
            self.service.action(101, 'settings', {'account_id': self.ids['B'], 'sign_time': '10:00'})

    def test_settings_sign_mode_validated_and_persisted_per_account(self):
        self.login(101)
        self.service.action(101, 'settings', {'account_id': self.ids['A'], 'sign_mode': 'random'})
        self.assertEqual(self.accounts.active(101)['sign_mode'], 'random')
        self.assertEqual(self.accounts.active(202)['sign_mode'], 'normal')
        with self.assertRaises(DomainError):
            self.service.action(101, 'settings', {'account_id': self.ids['A'], 'sign_mode': 'weird'})
        self.assertEqual(self.accounts.active(101)['sign_mode'], 'random')
        self.assertEqual(self.service.state(101)['active']['sign_mode'], 'random')

    def test_expected_root_transaction_prevents_old_page_editing_new_account(self):
        old = self.ids['A']
        self.accounts.begin_login(101, 'eA2')
        new = self.accounts.finish_login(101, '11_fixture_A2', {'user_id': 'eA2', 'telegram_user_id': 101})
        for action, data in [('settings', {'account_id': old, 'auto_sign': False}),
                             ('code', {'account_id': old}),
                             ('link', {'account_id': old, 'target_emos_id': 'eB', 'code': 'unknown-code'})]:
            with self.subTest(action=action), self.assertRaises(DomainError):
                self.service.action(101, action, data)
        self.assertEqual(self.accounts.active(101)['id'], new['id'])
        self.assertEqual(self.accounts.active(101)['auto_sign'], 1)

    def test_link_toggle_idempotent_and_both_endpoints_can_revoke(self):
        self.link('A', 'B')
        payload = {'account_id': self.ids['A'], 'source_id': self.ids['A'], 'target_id': self.ids['B'], 'enabled': False}
        self.service.action(101, 'toggle', payload)
        self.service.action(101, 'toggle', payload)
        self.assertEqual(self.accounts.links(101)['outgoing'][0]['enabled'], 0)
        self.assertFalse(self.accounts.can_run(101, self.ids['A'], self.ids['B']))
        self.service.action(202, 'unlink', {'account_id': self.ids['B'], 'source_id': self.ids['A'], 'target_id': self.ids['B']})
        self.assertEqual(self.service.state(101)['targets'], [])
        self.assertEqual(self.service.state(202)['links']['incoming'], [])

    def test_async_job_only_actor_sees_result_and_target_errors_reduced(self):
        self.link('A', 'B')
        def run(actor, include_links=True, expected_root=None):
            return [{'id': self.ids['A'], 'emos_id': 'eA', 'name': 'Private_A', 'status': 'failed',
                     'message': 'A own detail', 'carrot': None},
                    {'id': self.ids['B'], 'emos_id': 'eB', 'name': 'Private_B', 'status': 'failed',
                     'message': 'B_PRIVATE_FAILED_DETAIL', 'carrot': None}]
        self.service.run_user = run
        posted = self.service.start_sign(101, {'account_id': self.ids['A'], 'request_id': 'fixture_request_0001', 'all_accounts': True})
        result = self.wait_job(posted['job_id'])
        self.assertEqual(result['status'], 'done')
        self.assertEqual(result['rows'][0]['message'], 'A own detail')
        self.assertEqual(result['rows'][1]['message'], '签到失败')
        self.assertNotIn('B_PRIVATE_FAILED_DETAIL', json.dumps(result))
        with self.assertRaises(DomainError):
            self.service.job(202, posted['job_id'])
        self.accounts.unlink(202, self.ids['A'], self.ids['B'])
        self.assertEqual([row['emos_id'] for row in self.service.job(101, posted['job_id'])['rows']], ['eA'])

    def test_sign_scope_is_boolean_and_replay_cannot_change_it(self):
        calls = []
        self.service.run_user = lambda actor, **options: calls.append(options) or []
        data = {'account_id': self.ids['A'], 'request_id': 'scope_replay_fixture_001'}
        for invalid in ('all', 1, None, []):
            with self.subTest(value=invalid), self.assertRaises(DomainError):
                self.service.start_sign(101, dict(data, all_accounts=invalid))
        self.assertEqual(calls, [])
        first = self.service.start_sign(101, data)
        job = self.wait_job(first['job_id'])
        self.assertFalse(job['all_accounts'])
        self.assertEqual(calls, [{'include_links': False, 'expected_root': self.ids['A']}])
        with self.assertRaises(DomainError):
            self.service.start_sign(101, dict(data, all_accounts=True))
        second = self.service.start_sign(101, dict(data, request_id='scope_replay_fixture_002', all_accounts=True))
        job = self.wait_job(second['job_id'])
        self.assertTrue(job['all_accounts'])
        self.assertEqual(calls[-1], {'include_links': True, 'expected_root': self.ids['A']})
        self.assertNotEqual(first['job_id'], second['job_id'])

    def test_double_sign_request_replay_uses_same_job_not_new_run(self):
        calls = []
        self.service.run_user = lambda actor, **kwargs: calls.append(actor) or []
        data = {'account_id': self.ids['A'], 'request_id': 'fixture_request_replay'}
        first = self.service.start_sign(101, data)
        self.wait_job(first['job_id'])
        second = self.service.start_sign(101, data)
        self.assertEqual(second['job_id'], first['job_id'])
        self.assertEqual(calls, [101])

    def test_switch_while_previous_job_pending_does_not_return_old_job_as_new(self):
        entered, release = threading.Event(), threading.Event()
        def run(*args, **kwargs):
            entered.set()
            release.wait(3)
            return []
        self.service.run_user = run
        first = self.service.start_sign(101, {'account_id': self.ids['A'], 'request_id': 'fixture_pending_0001'})
        self.assertTrue(entered.wait(1))
        try:
            self.accounts.begin_login(101, 'eA2')
            new = self.accounts.finish_login(101, '11_fixture_A2', {'user_id': 'eA2', 'telegram_user_id': 101})
            with self.assertRaises(DomainError):
                self.service.start_sign(101, {'account_id': new['id'], 'request_id': 'fixture_pending_0002'})
            self.assertEqual(self.service.job(101, first['job_id'])['status'], 'stale')
        finally:
            release.set()
            self.service.queue.join()

    def test_full_http_personal_link_sign_and_revoke_uses_real_runtime(self):
        with serve() as (upstream, base), self.panel() as server:
            for name, actor in self.actors.items():
                upstream.identities['11_fixture_' + name] = {'user_id': 'e' + name, 'username': 'Private_' + name,
                                                           'telegram_user_id': actor}
            with self.app.write_db() as conn:
                conn.execute('UPDATE accounts SET base_url=?', (base,))
            a, b, c = self.login(101), self.login(202), self.login(303)
            def post(actor, action, data):
                auth = {101: a, 202: b, 303: c}[actor]
                result = self.request(server, '/api/web/' + action, 'POST', data, 'wsid=' + auth['sid'], auth['csrf_token'])
                self.assertIn(result[0], (200, 202), result[2])
                return json.loads(result[2])
            code_b = post(202, 'code', {'account_id': self.ids['B']})
            post(101, 'link', {'account_id': self.ids['A'], 'target_emos_id': 'eB', 'code': code_b['code']})
            self.assertEqual(self.service.state(101)['targets'], [])
            post(202, 'review', {'account_id': self.ids['B'], 'source_id': self.ids['A'],
                                 'target_id': self.ids['B'], 'approve': True})
            code_c = post(303, 'code', {'account_id': self.ids['C']})
            post(202, 'link', {'account_id': self.ids['B'], 'target_emos_id': 'eC', 'code': code_c['code']})
            post(303, 'review', {'account_id': self.ids['C'], 'source_id': self.ids['B'],
                                 'target_id': self.ids['C'], 'approve': True})
            with patch.object(self.app._shutdown, 'wait', return_value=False):
                started = post(101, 'sign', {'account_id': self.ids['A'], 'request_id': 'fixture_http_sign_0001', 'all_accounts': True})
                self.service.queue.join()
                result = self.request(server, '/api/web/job?job_id=' + started['job_id'], cookie='wsid=' + a['sid'])
                self.assertEqual(result[0], 200)
                self.assertEqual([row['emos_id'] for row in json.loads(result[2])['rows']], ['eA', 'eB'])
                self.assertEqual(upstream.signed, {'eA', 'eB'})
                post(202, 'unlink', {'account_id': self.ids['B'], 'source_id': self.ids['A'], 'target_id': self.ids['B']})
                state = json.loads(self.request(server, '/api/web/me', cookie='wsid=' + a['sid'])[2])
                self.assertEqual(state['targets'], [])
                upstream.signed.clear()
                post(101, 'sign', {'account_id': self.ids['A'], 'request_id': 'fixture_http_sign_0002'})
                self.service.queue.join()
                self.assertEqual(upstream.signed, {'eA'})

    def test_logout_revokes_session_without_touching_other_user(self):
        a, b = self.login(101), self.login(202)
        self.service.logout(a['sid'])
        self.assertIsNone(self.service.authenticate(a['sid']))
        self.assertIsNotNone(self.service.authenticate(b['sid']))

    def test_public_origin_validation_no_external_http_credentials_or_path(self):
        for value in ('http://public.example', 'https://x.example/subpath', 'https://user:pw@x.example',
                      'https://x.example/?code=x', 'https://x.example/#x', 'javascript:alert(1)'):
            with self.subTest(value=value), self.assertRaises(DomainError):
                webui.web_origin(value)
        self.assertEqual(webui.web_origin('https://x.example/'), 'https://x.example')
        self.assertEqual(webui.web_origin('http://127.0.0.1:8080'), 'http://127.0.0.1:8080')

    def test_bot_web_entry_is_only_given_after_self_login(self):
        original = self.app.cfg_all
        self.app.cfg_all = lambda: dict(original(), tg_allowed_user_ids='101')
        tg = FakeTG()
        instance = bot.Bot('fixture:bot', self.app.Store(), tg=tg, synchronous=True)
        instance._accept_me(tg.get_me())
        instance._handle({'update_id': 1, 'message': {'message_id': 1, 'from': {'id': 101},
            'chat': {'id': 101, 'type': 'private'}, 'text': '/web'}})
        url = tg.messages[-1][2][0][0]['url']
        code = parse_qs(urlsplit(url).fragment)['code'][0]
        auth = self.service.exchange(code)
        self.assertEqual(auth['tg_id'], 101)
        instance._handle({'update_id': 2, 'message': {'message_id': 2, 'from': {'id': 404},
            'chat': {'id': 404, 'type': 'private'}, 'text': '/web'}})
        latest = [row for row in tg.messages if row[0] == 404]
        self.assertFalse(any(button.get('url', '').startswith('https://sign.example.com')
                             for row in latest for line in (row[2] or []) for button in line))
        instance.stop()


if __name__ == '__main__':
    unittest.main()
