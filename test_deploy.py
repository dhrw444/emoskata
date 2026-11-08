"""管理员 HTTP 权限、单文件命名空间和账号网页完整流程回归。

夹具只使用临时或演练数据；本地通过不代表真实官方授权。
Test: python3 -B -m unittest test_deploy。
"""
from contextlib import contextmanager
import ast
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
from test_runtime import FakeTG, serve
from test_support import configure, BUILD_FILES, fixture_app


class DeployTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='emos-deploy-test-')
        self.addCleanup(self.temp.cleanup)
        from test_support import fixture_app
        self.app = fixture_app(self, self.temp.name, 'deploy_fixture_app')
        self.app.init_db()
        self.app.init_password()

    @contextmanager
    def panel(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), self.app.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def request(self, server, path, method='GET', body=None, headers=None):
        headers = dict(headers or {})
        if isinstance(body, (dict, list)):
            body = json.dumps(body)
            headers['Content-Type'] = 'application/json'
        conn = HTTPConnection('127.0.0.1', server.server_address[1], timeout=5)
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        data = response.read().decode()
        result = response.status, dict(response.getheaders()), data
        conn.close()
        return result

    def cookie(self, server):
        code, headers, _ = self.request(server, '/api/login', 'POST', {'password': 'fixture_password'})
        self.assertEqual(code, 200)
        return headers['Set-Cookie'].split(';')[0]

    def test_health_available_but_config_and_account_list_require_login(self):
        with self.panel() as server:
            self.assertEqual(self.request(server, '/api/health')[0], 200)
            self.assertEqual(self.request(server, '/api/config')[0], 401)
            self.assertEqual(self.request(server, '/api/accounts')[0], 401)
            self.assertEqual(self.request(server, '/')[0], 302)

    def test_retired_oauth_never_writes_accounts_or_persistent_state(self):
        with self.panel() as server:
            for path in ('/oauth/start', '/oauth/callback?token=11_fixture&uuid=anything'):
                self.assertEqual(self.request(server, path)[0], 410)
        with self.app.db() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM accounts').fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM settings WHERE key LIKE 'oauth_%'").fetchone()[0], 0)

    def test_logged_in_management_lists_no_raw_credentials(self):
        configure(self.app, tg_bot_token='123456:fixture_bot_secret')
        service = self.app.Store().accounts
        service.begin_login(101, 'eFixture')
        service.finish_login(101, '11_fixture_sensitive', {'user_id': 'eFixture', 'telegram_user_id': 101})
        with self.panel() as server:
            cookie = self.cookie(server)
            config = self.request(server, '/api/config', headers={'Cookie': cookie})
            rows = self.request(server, '/api/accounts', headers={'Cookie': cookie})
            self.assertEqual((config[0], rows[0]), (200, 200))
            self.assertNotIn('fixture_bot_secret', config[2])
            self.assertNotIn('11_fixture_sensitive', rows[2])
            self.assertTrue(json.loads(config[2])['tg_bot_configured'])

    def test_same_origin_required_and_password_cookie_strict(self):
        with self.panel() as server:
            origin = 'http://127.0.0.1:%d' % server.server_address[1]
            code, headers, _ = self.request(server, '/api/login', 'POST', {'password': 'fixture_password'}, {'Origin': origin})
            self.assertEqual(code, 200)
            self.assertIn('SameSite=Strict', headers['Set-Cookie'])
            cookie = headers['Set-Cookie'].split(';')[0]
            self.assertEqual(self.request(server, '/api/config', 'POST', {'content': 'x'},
                             {'Cookie': cookie, 'Origin': 'https://other.invalid'})[0], 403)
            self.assertEqual(self.request(server, '/api/config', 'POST', {'content': 'x'},
                             {'Cookie': cookie, 'Sec-Fetch-Site': 'same-site'})[0], 403)

    def test_https_management_cookie_secure(self):
        configure(self.app, public_base='https://manage.example')
        with self.panel() as server:
            _, headers, _ = self.request(server, '/api/login', 'POST', {'password': 'fixture_password'})
            self.assertIn('; Secure', headers['Set-Cookie'])

    def test_invalid_json_shape_and_body_length_fail_closed(self):
        with self.panel() as server:
            self.assertEqual(self.request(server, '/api/login', 'POST', [1])[0], 400)
            self.assertEqual(self.request(server, '/api/login', 'POST', 'bad-json')[0], 400)
            self.assertEqual(self.request(server, '/api/login', 'POST', '', {'Content-Length': '70000'})[0], 400)

    def test_settings_keep_existing_bot_token_when_field_blank(self):
        configure(self.app, tg_bot_token='123456:fixture_keep')
        with self.panel() as server:
            cookie = self.cookie(server)
            data = {'tg_bot_token': '', 'sign_time': '01:15'}
            self.assertEqual(self.request(server, '/api/config', 'POST', data, {'Cookie': cookie})[0], 200)
        self.assertEqual(self.app.cfg_all()['tg_bot_token'], '123456:fixture_keep')
        self.assertEqual(self.app.cfg_all()['sign_time'], '01:15')

    def test_invalid_config_rolls_back_not_half_written(self):
        old = self.app.cfg_all()
        with self.panel() as server:
            cookie = self.cookie(server)
            data = {'sign_time': '10:30', 'base_url': 'file:///tmp/secret'}
            self.assertEqual(self.request(server, '/api/config', 'POST', data, {'Cookie': cookie})[0], 400)
        self.assertEqual(self.app.cfg_all()['sign_time'], old['sign_time'])
        self.assertEqual(self.app.cfg_all()['base_url'], old['base_url'])

    def test_process_environment_is_ignored_and_env_password_rotation_clears_sessions(self):
        old = self.app.create_session('127.0.0.1', 'fixture')
        os.environ['TG_BOT_TOKEN'] = '123456:ignored_process_env'
        os.environ['EMOS_APP_USER_ID'] = 'ignoredAppEnv'
        self.assertEqual(self.app.cfg_all()['tg_bot_token'], '')
        self.assertNotIn('emos_app_user_id', self.app.cfg_all())
        configure(self.app, panel_password='new_fixture_password')
        self.app.init_password()
        self.assertFalse(self.app.session_ok(old))
        self.assertTrue(self.app.verify_password('new_fixture_password'))

    def test_blank_environment_variables_keep_management_configuration(self):
        configure(self.app, tg_bot_token='123456:fixture_stored')
        os.environ['TG_BOT_TOKEN'] = ''
        os.environ['EMOS_APP_USER_ID'] = ''
        self.assertEqual(self.app.cfg_all()['tg_bot_token'], '123456:fixture_stored')
        self.assertNotIn('emos_app_user_id', self.app.cfg_all())

    def test_admin_configures_bot_not_users_official_id(self):
        with self.panel() as server:
            cookie = self.cookie(server)
            body = {'tg_bot_token': '123456:fixture_from_web',
                    'web_base_url': 'https://sign.example.com'}
            self.assertEqual(self.request(server, '/api/config', 'POST', body, {'Cookie': cookie})[0], 200)
            _, _, exposed = self.request(server, '/api/config', headers={'Cookie': cookie})
            self.assertTrue(json.loads(exposed)['tg_bot_configured'])
            self.assertNotIn('fixture_from_web', exposed)
            page = self.request(server, '/', headers={'Cookie': cookie})[2]
            self.assertIn('id="token"', page)
            self.assertNotIn('id="app-id"', page)
            self.assertIn('id="web-base"', page)
        self.assertEqual(self.app.cfg_all()['tg_bot_token'], '123456:fixture_from_web')
        self.assertNotIn('emos_app_user_id', self.app.cfg_all())
        self.assertEqual(self.app.cfg_all()['web_base_url'], 'https://sign.example.com')

    def test_global_account_write_and_sign_routes_disabled(self):
        with self.panel() as server:
            cookie = self.cookie(server)
            for method, path in (('POST', '/api/accounts'), ('PATCH', '/api/accounts'),
                                 ('DELETE', '/api/accounts?id=1'), ('POST', '/api/run')):
                self.assertEqual(self.request(server, path, method, {}, {'Cookie': cookie})[0], 410)

    def test_rebuild_reproducible_and_namespace_separated(self):
        stage = Path(self.temp.name) / 'stage'
        stage.mkdir()
        for file in BUILD_FILES:
            shutil.copy2(ROOT / file, stage / file)
        subprocess.run([sys.executable, '-B', str(stage / 'build_single.py')], check=True, capture_output=True)
        first = (stage / 'emoskata.py').read_bytes()
        subprocess.run([sys.executable, '-B', str(stage / 'build_single.py')], check=True, capture_output=True)
        self.assertEqual(first, (stage / 'emoskata.py').read_bytes())
        spec = importlib.util.spec_from_file_location('single_fixture', stage / 'emoskata.py')
        single = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(single)
        self.assertIsNot(single.bot._flag, single.accounts._flag)
        self.assertIs(single.bot.DomainError, single.accounts.DomainError)
        self.assertIs(single.app.AccountService, single.accounts.AccountService)
        self.assertEqual(single.emos.EmosClient('11_fixture').bases, list(single.emos.BASES))
        tree = ast.parse(first)
        embedded = next(node.value for node in tree.body if isinstance(node, ast.Assign)
                        and any(isinstance(target, ast.Name) and target.id == '_SOURCES'
                                for target in node.targets))
        sources = ast.literal_eval(embedded)
        self.assertEqual(len(sources), 8)
        self.assertNotIn('from accounts import', '\n'.join(sources.values()))
        self.assertEqual(single._SOURCES, {})
        self.assertNotIn('_source', vars(single))

    def test_single_file_official_first_token_second_owner_web_sign(self):
        stage = Path(self.temp.name) / 'single-only'
        stage.mkdir()
        sources = Path(self.temp.name) / 'sources'
        sources.mkdir()
        for file in BUILD_FILES:
            shutil.copy2(ROOT / file, sources / file)
        subprocess.run([sys.executable, '-B', str(sources / 'build_single.py')], check=True, capture_output=True)
        shutil.copy2(sources / 'emoskata.py', stage / 'emoskata.py')
        single = fixture_app(self, stage, 'single_runtime_fixture', stage / 'emoskata.py', single_owner=True)
        self.assertEqual([p.name for p in stage.glob('*.py')], ['emoskata.py'])
        single.app.init_db()
        with serve() as (server, base):
            configure(single.app, base_url=base)
            server.identities = {'11_singleA': {'user_id': 'eA', 'telegram_user_id': 101, 'carrot': 41},
                                 '11_singleB': {'user_id': 'eB', 'telegram_user_id': 202, 'carrot': 777}}
            tg = FakeTG()
            instance = single.bot.Bot('fixture:bot', single.app.Store(), tg=tg, synchronous=True)
            instance._accept_me(tg.get_me())
            def command(number, actor, text):
                instance._handle({'update_id': number, 'message': {'message_id': number,
                    'from': {'id': actor}, 'chat': {'id': actor, 'type': 'private'}, 'text': text}})
            with patch.object(single.app._shutdown, 'wait', return_value=False):
                command(1, 101, '/login eA')
                command(2, 101, '/start emosLinkAgree-11_singleA')
                command(3, 101, '/token 11_singleB')
                service = single.app.Store().accounts
                self.assertEqual(len(service.owned(101)), 2)
                self.assertEqual(service.primary_account(101)['emos_id'], 'eA')
                self.assertEqual(service.active(101)['emos_id'], 'eB')
                command(4, 101, '/sign')
                self.assertEqual(server.signed, {'eB'})
                server.signed.clear()
                command(5, 101, '/signall')
                self.assertEqual(server.signed, {'eA', 'eB'})
                before = list(server.hits)
                command(6, 202, '/sign')
                self.assertEqual(before, server.hits)
                configure(single.app, web_base_url='http://127.0.0.1:8080')
                web = single.app.personal_web()
                self.addCleanup(web.stop)
                from urllib.parse import parse_qs, urlsplit
                code = parse_qs(urlsplit(web.issue_url(101)).fragment)['code'][0]
                auth = web.exchange(code)
                self.assertEqual(web.authenticate(auth['sid'])['tg_id'], 101)
                state = web.state(101)
                self.assertTrue(state['single_owner'])
                self.assertEqual(len(state['owned']), 2)
                self.assertEqual(state['links'], {'incoming': [], 'outgoing': []})
                server.signed.clear()
                started = web.start_sign(101, {'account_id': state['active']['id'],
                    'request_id': 'fixture_single_web_one_0001'})
                web.queue.join()
                self.assertEqual(len(web.job(101, started['job_id'])['rows']), 1)
                self.assertEqual(server.signed, {'eB'})
                server.signed.clear()
                started = web.start_sign(101, {'account_id': state['active']['id'],
                    'request_id': 'fixture_single_web_all_0001', 'all_accounts': True})
                web.queue.join()
                self.assertEqual(len(web.job(101, started['job_id'])['rows']), 2)
                self.assertEqual(server.signed, {'eA', 'eB'})
                configure(single.app, tg_allowed_user_ids='202')
                self.assertIsNone(web.authenticate(auth['sid']))
            instance.stop()


if __name__ == '__main__':
    unittest.main()
