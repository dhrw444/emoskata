# Python 3.10+ | Build: python3 build_single.py
# Run: python3 -B -m unittest -v test_update
# Deps: standard library, localhost-only fixtures; no real Telegram/emos requests.
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from http.client import HTTPConnection
from unittest.mock import patch

from envconfig import ConfigStore, EnvConfigError
from test_support import fixture_app

ROOT = Path(__file__).resolve().parent


def request(port, path, body=None, cookie=''):
    client = HTTPConnection('127.0.0.1', port, timeout=3)
    client.request('POST' if body is not None else 'GET', path,
                   json.dumps(body) if body is not None else None,
                   {'Content-Type': 'application/json', 'Cookie': cookie})
    response = client.getresponse()
    result = response.status, dict(response.getheaders()), json.loads(response.read())
    client.close()
    return result


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='emos-update-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runtime = self.root / 'runtime'
        self.runtime.mkdir()
        self.entry = self.runtime / 'emoskata.py'
        shutil.copyfile(ROOT / 'emoskata.py', self.entry)
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            self.port = probe.getsockname()[1]
        self.config = ConfigStore(self.root / 'config' / '.env', create=True)
        self.config.update({'DATA_DIR': './data', 'PANEL_PASSWORD': 'fixture_password',
                           'HTTP_HOST': '127.0.0.1', 'PORT': str(self.port),
                           'SIGN_TIME': '23:59', 'SCHEDULER_INTERVAL': '300'})
        self.process = None
        self.log = None

    def start(self):
        self.log = (self.root / 'process.log').open('ab')
        self.process = subprocess.Popen([sys.executable, '-u', str(self.entry)], cwd=self.root,
            env=dict(os.environ, EMOS_ENV_FILE=str(self.config.path), PYTHONDONTWRITEBYTECODE='1'),
            stdout=self.log, stderr=subprocess.STDOUT)
        self.addCleanup(self.stop)
        self.wait(lambda: request(self.port, '/api/health')[0] == 200)
        self.db_path = self.runtime / 'data' / 'data.db'
        self.assertTrue(self.db_path.is_file())
        status, headers, _ = request(self.port, '/api/login', {'password': 'fixture_password'})
        self.assertEqual(status, 200)
        self.cookie = headers['Set-Cookie'].split(';')[0]

    def stop(self):
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=3)
            if self.log:
                self.log.close()
            self.process = None

    def wait(self, predicate, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process is not None and self.process.poll() is not None:
                self.fail('test process stopped unexpectedly')
            try:
                if predicate():
                    return
            except (OSError, ValueError):
                pass
            # This local integration test polls loopback, not the official service.
            time.sleep(0.08)
        self.fail('local process readiness timed out')

    def dump_data(self):
        with sqlite3.connect(self.db_path) as conn:
            return {table: conn.execute('SELECT * FROM ' + table + ' ORDER BY rowid').fetchall()
                    for table in ('accounts', 'tg_users', 'account_links', 'link_requests', 'logs')}

    def seed(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("INSERT INTO accounts(id,name,token,emos_id,owner_tg_id,auto_sign,sign_time) "
                         "VALUES(1,'Fixture A','11_synthetic_A','eA',101,0,'15:30')")
            conn.execute("INSERT INTO accounts(id,name,token,emos_id,owner_tg_id,auto_sign,sign_time) "
                         "VALUES(2,'Fixture B','11_synthetic_B','eB',202,0,'16:30')")
            conn.execute("INSERT INTO tg_users(tg_id,chat_id,active_account_id,notify_success,notify_fail) "
                         "VALUES(101,101,1,0,1)")
            conn.execute("INSERT INTO tg_users(tg_id,chat_id,active_account_id) VALUES(202,202,2)")
            conn.execute("INSERT INTO account_links VALUES(1,2,1,'fixture')")
            conn.execute("INSERT INTO logs(account_id,name,ts,status,message,carrot) "
                         "VALUES(1,'Fixture A','fixture','signed','fixture saved',3)")

    def test_delete_py_running_reupload_exec_same_pid_and_preserve_user_data(self):
        self.start()
        self.seed()
        before, config_before = self.dump_data(), self.config.path.read_bytes()
        pid = self.process.pid
        uptime = request(self.port, '/api/runtime', cookie=self.cookie)[2]['uptime_seconds']
        self.entry.unlink()
        self.assertEqual(request(self.port, '/api/health')[0], 200)
        status, _, data = request(self.port, '/api/restart', {'confirm': True}, self.cookie)
        self.assertEqual(status, 400)
        self.assertIn('保留运行', data['error'])
        self.assertIsNone(self.process.poll())
        shutil.copyfile(ROOT / 'emoskata.py', self.entry)
        with self.entry.open('ab') as handle:
            handle.write(b'\ndef broken(:\n')
        self.assertEqual(request(self.port, '/api/restart', {'confirm': True}, self.cookie)[0], 400)
        self.assertEqual(request(self.port, '/api/health')[0], 200)
        shutil.copyfile(ROOT / 'emoskata.py', self.entry)
        self.assertEqual(request(self.port, '/api/restart', {'confirm': True}, self.cookie)[0], 202)
        # Validated image survives unlink after acceptance; no half-upload race.
        self.entry.unlink()
        self.wait(lambda: request(self.port, '/api/runtime', cookie=self.cookie)[2].get('restart_status') == 'running')
        value = request(self.port, '/api/runtime', cookie=self.cookie)[2]
        self.assertEqual(value['pid'], pid)
        self.assertLess(value['uptime_seconds'], uptime + 1)
        self.assertEqual(self.dump_data(), before)
        self.assertEqual(self.config.path.read_bytes(), config_before)
        self.assertEqual(request(self.port, '/api/config', cookie=self.cookie)[0], 200)
        shutil.copyfile(ROOT / 'emoskata.py', self.entry)
        self.assertEqual(request(self.port, '/api/restart', {'confirm': True}, self.cookie)[0], 202)
        self.wait(lambda: request(self.port, '/api/runtime', cookie=self.cookie)[2].get('restart_status') == 'running')
        self.assertEqual(self.dump_data(), before)
        self.assertEqual(self.config.path.read_bytes(), config_before)
        self.process.terminate()
        self.assertEqual(self.process.wait(timeout=8), 0)

    def test_restart_requires_confirmation_and_admin_not_personal_role(self):
        self.start()
        self.assertEqual(request(self.port, '/api/restart', {'confirm': True})[0], 401)
        self.assertEqual(request(self.port, '/api/restart', {}, self.cookie)[0], 400)
        self.assertEqual(request(self.port, '/api/restart', {'confirm': 'true'}, self.cookie)[0], 400)
        self.assertEqual(request(self.port, '/api/runtime', cookie='wsid=synthetic_only')[0], 401)
        self.assertEqual(request(self.port, '/api/health')[0], 200)

    def test_idle_process_uses_one_process_no_account_http_and_bounded_threads(self):
        self.start()
        before = request(self.port, '/api/runtime', cookie=self.cookie)[2]
        time.sleep(2)
        after = request(self.port, '/api/runtime', cookie=self.cookie)[2]
        self.assertEqual(before['pid'], after['pid'])
        self.assertLessEqual(after['threads'], 6)
        with sqlite3.connect(self.db_path) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM logs').fetchone()[0], 0)
        self.assertLess(after['cpu_seconds'] - before['cpu_seconds'], 1.0)

    def test_preflight_checks_embedded_module_syntax_not_only_outer_py(self):
        module = fixture_app(self, self.root / 'fixture', 'preflight_app')
        self.entry.write_text("_SOURCES=" + repr({name: 'pass' for name in
            ('envconfig','runtime_info','emos','accounts','webui','bot','app')}))
        source = self.entry.read_text().replace("'app': 'pass'", "'app': 'def bad(:'")
        self.entry.write_text(source)
        with patch.object(module, '__file__', str(self.entry)), self.assertRaises(EnvConfigError):
            module._prepare_core()
        self.assertFalse(module._shutdown.is_set())


if __name__ == '__main__':
    unittest.main()
