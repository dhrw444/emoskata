"""HTTP and deployment regressions; localhost fixtures and isolated data only.
Run: python3 -B -m unittest -v test_deploy
Build: python3 build_single.py | Deps: Python standard library.
"""
import ast
from contextlib import contextmanager
from datetime import datetime, timezone
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


class DeployTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='emos-deploy-test-')
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, {'DATA_DIR': self.temp.name,
                                           'PANEL_PASSWORD': 'fixture_password'})
        self.env.start()
        self.addCleanup(self.env.stop)
        for key in ('TG_BOT_TOKEN', 'SIGN_TIME', 'SIGN_TIMEZONE', 'EMOS_BASE_URL', 'EMOS_APP_USER_ID', 'PUBLIC_BASE'):
            os.environ.pop(key, None)
        spec = importlib.util.spec_from_file_location('deploy_fixture_app', ROOT / 'app.py')
        self.app = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.app)
        self.app.init_db()
        self.app.init_password()
        self.app.set_setting('base_url', 'http://127.0.0.1:1')

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
        self.app.set_setting('tg_bot_token', '123456:fixture_bot_secret')
        service = self.app.Store().accounts
        service.begin_login(101)
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
        os.environ['PUBLIC_BASE'] = 'https://manage.example'
        with self.panel() as server:
            _, headers, _ = self.request(server, '/api/login', 'POST', {'password': 'fixture_password'})
            self.assertIn('; Secure', headers['Set-Cookie'])

    def test_invalid_json_shape_and_body_length_fail_closed(self):
        with self.panel() as server:
            self.assertEqual(self.request(server, '/api/login', 'POST', [1])[0], 400)
            self.assertEqual(self.request(server, '/api/login', 'POST', 'bad-json')[0], 400)
            self.assertEqual(self.request(server, '/api/login', 'POST', '', {'Content-Length': '70000'})[0], 400)

    def test_settings_keep_existing_bot_token_when_field_blank(self):
        self.app.set_setting('tg_bot_token', '123456:fixture_keep')
        with self.panel() as server:
            cookie = self.cookie(server)
            data = {'tg_bot_token': '', 'emos_app_user_id': 'eAppFixture', 'sign_time': '00:15'}
            self.assertEqual(self.request(server, '/api/config', 'POST', data, {'Cookie': cookie})[0], 200)
        self.assertEqual(self.app.cfg_all()['tg_bot_token'], '123456:fixture_keep')
        self.assertEqual(self.app.cfg_all()['emos_app_user_id'], 'eAppFixture')
        self.assertEqual(self.app.cfg_all()['sign_time'], '01:15')

    def test_invalid_config_rolls_back_not_half_written(self):
        old = self.app.cfg_all()
        with self.panel() as server:
            cookie = self.cookie(server)
            data = {'content': 'x', 'base_url': 'file:///tmp/secret'}
            self.assertEqual(self.request(server, '/api/config', 'POST', data, {'Cookie': cookie})[0], 400)
        self.assertEqual(self.app.cfg_all()['content'], old['content'])
        self.assertEqual(self.app.cfg_all()['base_url'], old['base_url'])

    def test_environment_overrides_and_password_rotation_clear_sessions(self):
        old = self.app.create_session('127.0.0.1', 'fixture')
        os.environ['TG_BOT_TOKEN'] = '123456:fixture_env'
        os.environ['EMOS_APP_USER_ID'] = 'eAppEnv'
        self.assertEqual(self.app.cfg_all()['tg_bot_token'], '123456:fixture_env')
        self.assertEqual(self.app.cfg_all()['emos_app_user_id'], 'eAppEnv')
        os.environ['PANEL_PASSWORD'] = 'new_fixture_password'
        self.app.init_password()
        self.assertFalse(self.app.session_ok(old))
        self.assertTrue(self.app.verify_password('new_fixture_password'))

    def test_blank_environment_variables_keep_management_configuration(self):
        self.app.set_setting('tg_bot_token', '123456:fixture_stored')
        self.app.set_setting('emos_app_user_id', 'eAppStored')
        os.environ['TG_BOT_TOKEN'] = ''
        os.environ['EMOS_APP_USER_ID'] = ''
        self.assertEqual(self.app.cfg_all()['tg_bot_token'], '123456:fixture_stored')
        self.assertEqual(self.app.cfg_all()['emos_app_user_id'], 'eAppStored')

    def test_global_account_write_and_sign_routes_disabled(self):
        with self.panel() as server:
            cookie = self.cookie(server)
            for method, path in (('POST', '/api/accounts'), ('PATCH', '/api/accounts'),
                                 ('DELETE', '/api/accounts?id=1'), ('POST', '/api/run')):
                self.assertEqual(self.request(server, path, method, {}, {'Cookie': cookie})[0], 410)

    def test_rebuild_reproducible_and_namespace_separated(self):
        stage = Path(self.temp.name) / 'stage'
        stage.mkdir()
        for file in ('build_single.py', 'app.py', 'emos.py', 'accounts.py', 'bot.py'):
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
        self.assertNotIn('from accounts import', '\n'.join(single._SOURCES.values()))

    def test_single_file_only_real_login_associate_sign_revoke(self):
        stage = Path(self.temp.name) / 'single-only'
        stage.mkdir()
        sources = Path(self.temp.name) / 'sources'
        sources.mkdir()
        for file in ('build_single.py', 'app.py', 'emos.py', 'accounts.py', 'bot.py'):
            shutil.copy2(ROOT / file, sources / file)
        subprocess.run([sys.executable, '-B', str(sources / 'build_single.py')], check=True, capture_output=True)
        shutil.copy2(sources / 'emoskata.py', stage / 'emoskata.py')
        spec = importlib.util.spec_from_file_location('single_runtime_fixture', stage / 'emoskata.py')
        single = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(single)
        self.assertEqual([p.name for p in stage.glob('*.py')], ['emoskata.py'])
        single.app.DATA_DIR = str(stage / 'data')
        Path(single.app.DATA_DIR).mkdir()
        single.app.DB_PATH = str(stage / 'data' / 'data.db')
        single.app.init_db()
        with serve() as (server, base):
            single.app.set_setting('base_url', base)
            single.app.set_setting('emos_app_user_id', 'eAppFixture')
            server.identities = {'11_singleA': {'user_id': 'eA', 'telegram_user_id': 101},
                                 '11_singleB': {'user_id': 'eB', 'telegram_user_id': 202}}
            tg = FakeTG()
            instance = single.bot.Bot('fixture:bot', single.app.Store(), tg=tg, synchronous=True)
            instance._accept_me(tg.get_me())
            def command(number, actor, text):
                instance._handle({'update_id': number, 'message': {'message_id': number,
                    'from': {'id': actor}, 'chat': {'id': actor, 'type': 'private'}, 'text': text}})
            with patch.object(single.app._shutdown, 'wait', return_value=False):
                for n, actor, token in ((1, 101, '11_singleA'), (3, 202, '11_singleB')):
                    command(n, actor, '/login')
                    command(n + 1, actor, '/start emosLinkAgree-' + token)
                service = single.app.Store().accounts
                code = service.issue_code(202)
                command(5, 101, '/link eB ' + code['code'])
                command(6, 101, '/sign')
                self.assertEqual(server.signed, {'eA', 'eB'})
                a, b = service.active(101), service.active(202)
                service.unlink(202, a['id'], b['id'])
                server.signed.clear()
                command(7, 101, '/sign')
                self.assertEqual(server.signed, {'eA'})
            instance.stop()


if __name__ == '__main__':
    unittest.main()
