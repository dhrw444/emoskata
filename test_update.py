"""标准库单文件启动、删除重传、exec 与持久数据保留回归。

仅用隔离目录、合成身份或 localhost；正式授权和真实生产资源不在此验收。
Test: python3 -B -m unittest test_update"""
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import closing
from http.client import HTTPConnection
from unittest.mock import patch

from envconfig import ConfigStore, EnvConfigError
from test_support import fixture_app, write_env, without_public_probe

ROOT = Path(__file__).resolve().parent


def _single_file(sources):
    """构造会实际 exec 内嵌模块的最小入口夹具。

    只赋值 _SOURCES 不会执行模块，无法验证导入期错误拦截。
    """
    return ('''import sys as _sys
import types as _types
import importlib.util as _import_util

_SOURCES = %r
_package_name = '_emoskata_embedded'
_package = _types.ModuleType(_package_name)
_package.__path__ = []
_package.__file__ = __file__
_sys.modules[_package_name] = _package
for _name, _source in _SOURCES.items():
    _qualified = _package_name + '.' + _name
    _module = _types.ModuleType(_qualified)
    _module.__file__ = __file__
    _module.__package__ = _package_name
    _module.__spec__ = _import_util.spec_from_loader(_qualified, loader=None)
    _sys.modules[_qualified] = _module
    setattr(_package, _name, _module)
    exec(compile(_source, __file__ + ':' + _name, 'exec'), _module.__dict__)
''' % (sources,))


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
        self.entry.write_bytes(without_public_probe((ROOT / 'emoskata.py').read_bytes()))
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            self.port = probe.getsockname()[1]
        # 部署者视角：先写完整 .env，再让程序只读加载。
        self.config = ConfigStore(write_env(
            self.root / 'config' / '.env', 'fixture_password',
            DATA_DIR='./data', HTTP_HOST='127.0.0.1', PORT=str(self.port),
            SIGN_TIME='23:59', SCHEDULER_INTERVAL='300',
            TG_BOT_TOKEN='', TG_ALLOWED_USER_IDS='101'))
        self.process = None
        self.log = None

    def start(self):
        self.log = (self.root / 'process.log').open('ab')
        self.process = subprocess.Popen([sys.executable, '-I', '-S', '-u', str(self.entry)], cwd=self.root,
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
            # 这里只轮询 localhost 就绪状态，不轮询官方接口。
            time.sleep(0.08)
        self.fail('local process readiness timed out')

    def dump_data(self):
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            return {table: conn.execute('SELECT * FROM ' + table + ' ORDER BY rowid').fetchall()
                    for table in ('accounts', 'tg_users', 'account_links', 'link_requests', 'logs',
                                  'settings', 'sessions', 'web_auth_codes', 'web_sessions')}

    def seed(self):
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
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
            conn.execute("UPDATE tg_users SET primary_account_id=1,notify_already=1 WHERE tg_id=101")
            conn.execute("UPDATE accounts SET carrot=777,carrot_at=1700000000,sign_mode='random' WHERE id=1")
            conn.execute("INSERT INTO accounts(id,name,token,emos_id,owner_tg_id,auto_sign,sign_time,carrot) "
                         "VALUES(3,'Fixture Extra','22_synthetic_extra','eExtra',101,0,'19:00',42)")
            conn.execute("INSERT INTO settings VALUES('quality_marker','kept')")
            conn.execute("INSERT INTO web_auth_codes VALUES('fixture-hashed-code',101,2000000000)")
            conn.execute("INSERT INTO web_sessions VALUES('fixture-hashed-session',101,1700000000,"
                         "2000000000,'fixture-csrf')")

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
        self.assertIn('已从最近一次成功启动的快照恢复', data['error'])
        self.assertEqual(self.entry.read_bytes(), (self.config.path.parent / '.emoskata.py.last-good').read_bytes())
        self.assertIsNone(self.process.poll())
        self.entry.write_bytes(without_public_probe((ROOT / 'emoskata.py').read_bytes()))
        with self.entry.open('ab') as handle:
            handle.write(b'\ndef broken(:\n')
        self.assertEqual(request(self.port, '/api/restart', {'confirm': True}, self.cookie)[0], 400)
        self.assertEqual(request(self.port, '/api/health')[0], 200)
        self.entry.write_bytes(without_public_probe((ROOT / 'emoskata.py').read_bytes()))
        self.assertEqual(request(self.port, '/api/restart', {'confirm': True}, self.cookie)[0], 202)
        # 请求接受后即删除入口，验证 exec 仍读取已验过的临时镜像。
        self.entry.unlink()
        self.wait(lambda: request(self.port, '/api/runtime', cookie=self.cookie)[2].get('restart_status') == 'running')
        value = request(self.port, '/api/runtime', cookie=self.cookie)[2]
        self.assertEqual(value['pid'], pid)
        self.assertLess(value['uptime_seconds'], uptime + 1)
        self.assertEqual(self.dump_data(), before)
        self.assertEqual(self.config.path.read_bytes(), config_before)
        self.assertEqual(request(self.port, '/api/config', cookie=self.cookie)[0], 200)
        self.entry.write_bytes(without_public_probe((ROOT / 'emoskata.py').read_bytes()))
        self.assertEqual(request(self.port, '/api/restart', {'confirm': True}, self.cookie)[0], 202)
        self.wait(lambda: request(self.port, '/api/runtime', cookie=self.cookie)[2].get('restart_status') == 'running')
        self.assertEqual(self.dump_data(), before)
        self.assertEqual(self.config.path.read_bytes(), config_before)
        self.process.terminate()
        self.assertEqual(self.process.wait(timeout=8), 0)

    def test_clean_interpreter_starts_with_only_one_python_file(self):
        self.assertEqual([file.name for file in self.runtime.glob('*.py')], ['emoskata.py'])
        self.start()  # -I -S 禁用 site 初始化及外部 Python 路径。
        self.assertEqual(request(self.port, '/api/health')[0], 200)
        self.assertTrue(self.db_path.is_file())
        self.assertIsNone(self.process.poll())

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
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
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

    def test_preflight_rejects_import_time_crash_that_is_syntactically_valid(self):
        # 夹具要真实执行内嵌模块，才能验证导入异常被拒绝。
        # 只有 _SOURCES 赋值不会触发模块导入。
        module = fixture_app(self, self.root / 'fixture', 'crash_app')
        body = {name: "VALUE = 1\n" for name in
                ('envconfig', 'runtime_info', 'emos', 'accounts', 'webui', 'bot', 'app')}
        with patch.object(module, '__file__', str(self.entry)):
            self.entry.write_text(_single_file(body))
            image = module._prepare_core()      # 干净版本要能过闸
            image.close()
            broken = dict(body, app="raise RuntimeError('导入即崩')\n")
            self.entry.write_text(_single_file(broken))
            with self.assertRaises(EnvConfigError) as caught:
                module._prepare_core()
            self.assertIn('自检', str(caught.exception))
            self.assertIn('导入即崩', str(caught.exception))
        self.assertFalse(module._shutdown.is_set())

    def test_missing_entry_restores_from_last_good_snapshot(self):
        # 入口被删且无 pending 事务时，用成功快照原地恢复，不让人手工重传。
        module = fixture_app(self, self.root / 'fixture', 'restore_app')
        body = {name: "VALUE = 1\n" for name in
                ('envconfig', 'runtime_info', 'emos', 'accounts', 'webui', 'bot', 'app')}
        self.entry.write_text(_single_file(body))
        with patch.object(module, '__file__', str(self.entry)):
            image = module._prepare_core()
            image.close()
            # 成功快照与更新事务固定在配置目录，入口可在另一个目录。
            snapshot = module.CONFIG.path.parent / ('.' + self.entry.name + '.last-good')
            snapshot.write_bytes(self.entry.read_bytes())
            snapshot.chmod(0o600)
            original = self.entry.read_bytes()
            self.entry.unlink()
            with self.assertRaises(EnvConfigError) as caught:
                module._prepare_core()
            self.assertIn('已从最近一次成功启动的快照恢复', str(caught.exception))
            self.assertTrue(self.entry.exists())
            self.assertEqual(self.entry.read_bytes(), original)
            self.assertEqual(self.entry.stat().st_mode & 0o777, 0o600)

    def test_missing_entry_without_snapshot_is_refused_not_invented(self):
        # 没有快照时必须明确拒绝，不能凭空虚构一个入口。
        module = fixture_app(self, self.root / 'fixture', 'nosnap_app')
        with patch.object(module, '__file__', str(self.entry)):
            self.entry.write_text(_single_file({'app': "VALUE = 1\n"}))
            self.entry.unlink()
            with self.assertRaises(EnvConfigError) as caught:
                module._prepare_core()
            self.assertIn('没有可用的成功快照', str(caught.exception))
            self.assertFalse(self.entry.exists())

    def test_probe_does_not_consume_the_image_that_execv_needs(self):
        # 导入预检返回时，源码镜像必须仍从第 0 字节可读。
        module = fixture_app(self, self.root / 'fixture', 'offset_app')
        body = {name: "VALUE = 1\n" for name in
                ('envconfig', 'runtime_info', 'emos', 'accounts', 'webui', 'bot', 'app')}
        self.entry.write_text(_single_file(body))
        with patch.object(module, '__file__', str(self.entry)):
            image = module._prepare_core()
            try:
                self.assertEqual(image.read(), self.entry.read_bytes())
            finally:
                image.close()

    def test_probe_uses_a_copy_of_the_env_file(self):
        # 自检主动改写临时 .env，确认真实配置不被修改。
        module = fixture_app(self, self.root / 'fixture', 'envcopy_app')
        body = {name: "VALUE = 1\n" for name in
                ('envconfig', 'runtime_info', 'emos', 'accounts', 'webui', 'bot', 'app')}
        body['app'] = ("import os\n"
                       "import pathlib\n"
                       "_target = pathlib.Path(os.environ['EMOS_ENV_FILE'])\n"
                       "_target.write_bytes(_target.read_bytes() + b'# probe rewrote me\\n')\n")
        self.entry.write_text(_single_file(body))
        original = module.CONFIG.path.read_bytes()
        with patch.object(module, '__file__', str(self.entry)):
            image = module._prepare_core()
            image.close()
        self.assertEqual(module.CONFIG.path.read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
