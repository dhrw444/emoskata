"""真单文件的首次创建、跨版本代码激活、补项和失败回滚验收。

仅在临时目录启动准确记录 PID 的 Python，使用合成账号，不访问外部接口。
Test: python3 -B -m unittest test_single_file_upgrade
"""
import ast
import hashlib
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
from http.client import HTTPConnection

from build_single import build
from envconfig import SCHEMA, serialize_env, template_values


ROOT = Path(__file__).resolve().parent
PASSWORD = 'single_upgrade_fixture_password'


def image(version, expand=False, fail=False):
    """基于真实生成器构造两版演练镜像；只额外注入观察标记和演练 schema。
    """
    source = build(write=False)
    tree = ast.parse(source)
    node = next(node for node in tree.body if isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == '_SOURCES'
                        for target in node.targets))
    modules = ast.literal_eval(node.value)
    for name in modules:
        modules[name] += '\nUPGRADE_MARK = ' + repr(version) + '\n'
    modules['app'] += '''
_upgrade_snapshot = resource_snapshot
def resource_snapshot():
    from . import updates, envconfig, runtime_info, emos, accounts, webui, bot, app
    result = _upgrade_snapshot()
    result['embedded_versions'] = {module.__name__.rsplit('.', 1)[-1]: module.UPGRADE_MARK
                                  for module in (updates, envconfig, runtime_info, emos,
                                                 accounts, webui, bot, app)}
    result['token_guide_doc'] = bot.Bot._guide_token.__doc__
    return result
'''
    modules['bot'] += '\nBot._guide_token.__doc__ = ' + repr('version:' + version) + '\n'
    modules['app'] += '\nPAGE = PAGE.replace("</body>", "<i hidden>version:%s</i></body>")\n' % version
    modules['webui'] += '\n_DASHBOARD_JS += "\\n// version:%s\\n"\n' % version
    if expand:
        modules['envconfig'] += "\nSCHEMA['UPGRADE_TEST_OPTION'] = Field('fixture-added', '升级演练字段', '仅测试镜像', 'text')\n"
        modules['envconfig'] += "CFG_TO_ENV['upgrade_test_option'] = 'UPGRADE_TEST_OPTION'\n"
        modules['app'] += '''
_upgrade_init_db = init_db
def init_db():
    _upgrade_init_db()
    with write_db() as conn:
        columns = {row['name'] for row in conn.execute('PRAGMA table_info(accounts)')}
        if 'upgrade_test_column' not in columns:
            conn.execute('ALTER TABLE accounts ADD COLUMN upgrade_test_column INTEGER NOT NULL DEFAULT 7')
        conn.execute('CREATE TABLE IF NOT EXISTS upgrade_test_items(id INTEGER PRIMARY KEY,value TEXT)')
'''
    if fail:
        modules['envconfig'] += "\nSCHEMA['FAILED_TEST_OPTION'] = Field('not-to-keep', '失败演练字段', '仅测试镜像', 'text')\n"
        modules['app'] += '''
_failed_init_db = init_db
def init_db():
    _failed_init_db()
    if not _STARTUP_PROBE:
        CONFIG.update({'EMOS_USER_ID': 'eChangedByFailedCandidate'})
        with write_db() as conn:
            conn.execute("UPDATE accounts SET token='failed_fixture_token',name='failed name'")
            conn.execute('CREATE TABLE failed_test_items(value TEXT)')
        raise RuntimeError('intentional startup failure after changing config and account data')
'''
    for name, text in modules.items():
        compile(text, 'fixture:' + name, 'exec')
    lines = source.splitlines(keepends=True)
    lines[node.lineno - 1:node.end_lineno] = ['_SOURCES = ' + repr(modules) + '\n']
    source = ''.join(lines)
    compile(source, 'fixture-single.py', 'exec')
    return source.encode('utf-8')


def env_values(path):
    return {key: json.loads(value) for line in path.read_text().splitlines()
            if line.strip() and not line.lstrip().startswith('#')
            for key, separator, value in [line.partition('=')] if separator}


class SingleFileUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='emos-single-upgrade-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runtime = self.root / 'runtime'
        self.runtime.mkdir()
        self.entry = self.runtime / 'emoskata.py'
        self.config = self.root / 'config' / '.env'
        self.db = self.runtime / 'data' / 'data.db'
        with socket.socket() as handle:
            handle.bind(('127.0.0.1', 0))
            self.port = handle.getsockname()[1]
        self.process = None
        self.log = None
        self.cookie = None
        self.addCleanup(self.stop)

    def request(self, path, body=None, authenticated=True):
        connection = HTTPConnection('127.0.0.1', self.port, timeout=4)
        try:
            headers = {'Origin': 'http://127.0.0.1:%s' % self.port}
            if authenticated and self.cookie:
                headers['Cookie'] = self.cookie
            if body is not None:
                headers['Content-Type'] = 'application/json'
            connection.request('GET' if body is None else 'POST', path,
                               body=None if body is None else json.dumps(body), headers=headers)
            response = connection.getresponse()
            raw = response.read().decode()
            data = json.loads(raw) if 'application/json' in response.getheader('Content-Type', '') else raw
            return response.status, dict(response.getheaders()), data
        finally:
            connection.close()

    def wait(self, predicate, timeout=25):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                self.fail('isolated application exited before ready: %s' % self.process.returncode)
            try:
                if predicate():
                    return
            except (OSError, ValueError):
                pass
            time.sleep(0.08)
        self.fail('isolated application readiness timed out')

    def config_values(self):
        return template_values(PASSWORD, PORT=str(self.port), HTTP_HOST='127.0.0.1',
                               DATA_DIR='./data', SIGN_TIME='08:00', GAP_SECONDS='8',
                               SIGN_TIMEZONE='UTC', SCHEDULER_INTERVAL='300',
                               TG_BOT_TOKEN='', TG_ALLOWED_USER_IDS='101', EMOS_USER_ID='ePrimary')

    def start(self, source=None, existing_config=True):
        self.entry.write_bytes(source if source is not None else (ROOT / 'emoskata.py').read_bytes())
        environment = dict(os.environ)
        for name in SCHEMA:
            environment.pop(name, None)
        values = self.config_values()
        if existing_config:
            self.config.parent.mkdir()
            self.config.write_text(serialize_env(values))
            self.config.chmod(0o600)
        else:
            environment.update(values)
        environment.pop('EMOS_UPDATE_ACTIVE', None)
        environment.pop('EMOS_STARTUP_PROBE', None)
        environment.pop('EMOS_PROBE_REUSE_BIND', None)
        environment['EMOS_ENV_FILE'] = str(self.config)
        environment['PYTHONDONTWRITEBYTECODE'] = '1'
        self.log = (self.root / 'process.log').open('wb')
        self.process = subprocess.Popen([sys.executable, '-I', '-S', '-u', str(self.entry)],
                                        cwd=self.root, env=environment, stdin=subprocess.DEVNULL,
                                        stdout=self.log, stderr=subprocess.STDOUT)
        self.wait(lambda: self.request('/api/health', authenticated=False)[0] == 200)
        status, headers, _ = self.request('/api/login', {'password': PASSWORD}, authenticated=False)
        self.assertEqual(status, 200)
        self.cookie = headers['Set-Cookie'].split(';')[0]
        self.assertEqual([path.name for path in self.runtime.glob('*.py')], ['emoskata.py'])

    def stop(self):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        if self.log:
            self.log.close()

    def seed(self):
        with sqlite3.connect(self.db) as conn:
            conn.execute("INSERT INTO accounts(id,name,token,emos_id,owner_tg_id,auto_sign,sign_time,carrot,sign_mode) VALUES(1,'primary','11_fixture_primary','ePrimary',101,0,'16:30',777,'random')")
            conn.execute("INSERT INTO accounts(id,name,token,emos_id,owner_tg_id,auto_sign,sign_time,carrot) VALUES(2,'independent','22_fixture_extra','eExtra',101,0,'17:30',42)")
            conn.execute("INSERT INTO tg_users(tg_id,chat_id,active_account_id,primary_account_id,notify_success,notify_fail) VALUES(101,101,2,1,0,1)")
            conn.execute("INSERT INTO logs(account_id,name,ts,status,message,carrot) VALUES(1,'primary','fixture','signed','kept log',3)")
            conn.execute("INSERT INTO settings VALUES('upgrade_user_setting','preserve')")
            conn.execute("INSERT INTO web_sessions VALUES('fixture_session_hash',101,1700000000,2000000000,'fixture_csrf')")

    def data(self, columns=None):
        with sqlite3.connect(self.db) as conn:
            if columns is None:
                names = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
                columns = {name: [row[1] for row in conn.execute('PRAGMA table_info("%s")' % name)] for name in names}
            rows = {name: conn.execute('SELECT %s FROM "%s" ORDER BY rowid' % (
                ','.join('"%s"' % col for col in fields), name)).fetchall()
                for name, fields in columns.items()}
        return columns, rows

    def versions(self):
        status, _, data = self.request('/api/runtime')
        self.assertEqual(status, 200)
        return data['embedded_versions']

    def restart(self, source, version):
        old = self.process.pid
        self.entry.unlink()
        self.assertEqual(self.request('/api/health', authenticated=False)[0], 200)
        self.entry.write_bytes(source)
        self.assertEqual(self.request('/api/restart', {'confirm': True})[0], 202)
        self.wait(lambda: set(self.versions().values()) == {version}
                  and self.request('/api/runtime')[2]['restart_status'] == 'running')
        self.assertEqual(self.request('/api/runtime')[2]['pid'], old)

    def seed_web_login(self):
        import secrets
        raw_sid = secrets.token_urlsafe(32)
        now = int(time.time())
        with sqlite3.connect(self.db) as conn:
            conn.execute('INSERT INTO web_sessions VALUES(?,?,?,?,?)',
                         (hashlib.sha256(raw_sid.encode('ascii')).hexdigest(), 101, now,
                          now + 86400, 'fixture_csrf'))
        return raw_sid

    def dashboard(self, sid, path='/web'):
        connection = HTTPConnection('127.0.0.1', self.port, timeout=4)
        try:
            connection.request('GET', path, headers={'Cookie': 'wsid=' + sid})
            response = connection.getresponse()
            raw = response.read().decode()
            return response.status, raw
        finally:
            connection.close()

    def test_only_py_creates_missing_config_data_and_backup(self):
        self.assertFalse(self.config.exists())
        self.assertFalse(self.db.exists())
        self.start(existing_config=False)
        self.assertTrue(self.config.is_file())
        self.assertTrue(self.db.is_file())
        self.assertEqual(set(env_values(self.config)), set(SCHEMA))
        self.assertEqual(env_values(self.config)['PANEL_PASSWORD'], PASSWORD)
        _, rows = self.data()
        self.assertIn('accounts', rows)
        self.assertIn('web_sessions', rows)
        self.assertEqual(rows['accounts'], [])
        backup = self.config.parent / '.emoskata.py.last-good'
        self.assertEqual(backup.read_bytes(), self.entry.read_bytes())
        self.process.terminate()
        self.assertEqual(self.process.wait(timeout=10), 0)

    def test_one_file_updates_all_modules_pages_and_docs_preserving_accounts(self):
        self.start(image('v1'))
        self.seed()
        sid = self.seed_web_login()
        config_before = self.config.read_bytes()
        columns, before = self.data()
        expected_names = {'updates', 'envconfig', 'runtime_info', 'emos', 'accounts', 'webui', 'bot', 'app'}
        self.assertEqual(self.versions(), {name: 'v1' for name in expected_names})
        self.assertIn('version:v1', self.request('/admin')[2])
        self.assertIn('// version:v1', self.dashboard(sid)[1])
        self.restart(image('v2'), 'v2')
        self.assertEqual(self.versions(), {name: 'v2' for name in expected_names})
        self.assertIn('version:v2', self.request('/admin')[2])
        self.assertIn('// version:v2', self.dashboard(sid)[1])
        self.assertEqual(self.request('/api/runtime')[2]['token_guide_doc'], 'version:v2')
        self.assertEqual(self.config.read_bytes(), config_before)
        self.assertEqual(self.data(columns)[1], before)
        self.assertEqual(self.request('/api/config')[0], 200)
        self.assertEqual(self.dashboard(sid, '/api/web/me')[0], 200)
        self.assertEqual([p.name for p in self.runtime.glob('*.py')], ['emoskata.py'])
        self.assertFalse((self.runtime / 'accounts.py').exists())
        self.assertEqual((self.config.parent / '.emoskata.py.last-good').read_bytes(), self.entry.read_bytes())

    def test_new_schema_creates_missing_fields_not_resetting_old_values(self):
        self.start(image('v1'))
        self.seed()
        sid = self.seed_web_login()
        config_before = env_values(self.config)
        columns, before = self.data()
        self.restart(image('v2', expand=True), 'v2')
        config_after = env_values(self.config)
        self.assertEqual({key: config_after[key] for key in config_before}, config_before)
        self.assertEqual(config_after['UPGRADE_TEST_OPTION'], 'fixture-added')
        self.assertEqual(self.data(columns)[1], before)
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute('SELECT upgrade_test_column FROM accounts ORDER BY id').fetchall(), [(7,), (7,)])
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM upgrade_test_items').fetchone()[0], 0)
        self.assertEqual(self.request('/api/config')[2]['upgrade_test_option'], 'fixture-added')
        self.assertEqual(self.dashboard(sid, '/api/web/me')[0], 200)
        expanded_before = self.config.read_bytes()
        current_columns, expanded_rows = self.data()
        self.restart(image('v2', expand=True), 'v2')
        self.assertEqual(self.config.read_bytes(), expanded_before)
        self.assertEqual(self.data(current_columns)[1], expanded_rows)

    def test_failed_new_schema_restores_config_accounts_and_existing_sessions(self):
        self.start(image('v1'))
        self.seed()
        sid = self.seed_web_login()
        config_before = self.config.read_bytes()
        columns, before = self.data()
        original_source = self.entry.read_bytes()
        self.restart(image('broken', fail=True), 'v1')
        self.assertEqual(self.entry.read_bytes(), original_source)
        self.assertEqual(self.config.read_bytes(), config_before)
        self.assertEqual(self.data(columns)[1], before)
        all_columns, _ = self.data()
        self.assertNotIn('failed_test_items', all_columns)
        self.assertNotIn('FAILED_TEST_OPTION', env_values(self.config))
        self.assertEqual(self.request('/api/config')[0], 200)
        self.assertEqual(self.dashboard(sid, '/api/web/me')[0], 200)

    def test_existing_py_siblings_are_not_dependencies_or_extracted_update_targets(self):
        # 真实部署只需入口；留在旁边的旧维护文件既不执行，也不由更新器覆盖。
        stale = {name: ('raise RuntimeError("unused sibling:' + name + '")\n').encode()
                 for name in ('accounts.py', 'webui.py', 'bot.py', 'envconfig.py', 'emos.py', 'updates.py')}
        for name, source in stale.items():
            (self.runtime / name).write_bytes(source)
        self.entry.write_bytes(image('v1'))
        self.config.parent.mkdir()
        self.config.write_text(serialize_env(self.config_values()))
        self.config.chmod(0o600)
        environment = dict(os.environ, EMOS_ENV_FILE=str(self.config))
        self.log = (self.root / 'process.log').open('wb')
        self.process = subprocess.Popen([sys.executable, '-I', '-S', '-u', str(self.entry)], cwd=self.root,
                                        env=environment, stdin=subprocess.DEVNULL,
                                        stdout=self.log, stderr=subprocess.STDOUT)
        self.wait(lambda: self.request('/api/health', authenticated=False)[0] == 200)
        status, headers, _ = self.request('/api/login', {'password': PASSWORD}, authenticated=False)
        self.assertEqual(status, 200)
        self.cookie = headers['Set-Cookie'].split(';')[0]
        self.restart(image('v2'), 'v2')
        self.assertEqual({name: (self.runtime / name).read_bytes() for name in stale}, stale)
        self.assertEqual(set(self.versions().values()), {'v2'})
