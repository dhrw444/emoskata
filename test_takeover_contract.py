"""回归只读校验无副作用、纯 Bot 保存和显式配置来源持久化。

仅用隔离目录、合成身份或 localhost；正式授权和真实生产资源不在此验收。
Test: python3 -B -m unittest test_takeover_contract"""
import ast
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from build_single import build
from envconfig import (ConfigStore, EnvConfigError, parse_env, render_env_example,
                       serialize_env)
from test_support import fixture_app, write_env


class TakeoverContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='emos-takeover-contract-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def run_check(self, root, config, source=None):
        entry = root / 'emoskata.py'
        entry.write_text(build(write=False) if source is None else source, encoding='utf-8')
        environment = dict(os.environ, EMOS_ENV_FILE=str(config), PYTHONDONTWRITEBYTECODE='1')
        for key in ('EMOS_UPDATE_ACTIVE', 'EMOS_STARTUP_PROBE', 'EMOS_PROBE_REUSE_BIND', 'PYTHONPATH'):
            environment.pop(key, None)
        return subprocess.run([sys.executable, '-I', '-S', '-B', str(entry), '--check-config'],
                              cwd=root, env=environment, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=30)

    def test_check_config_never_creates_data_directory_or_runtime_snapshot(self):
        root = self.root / 'read-only'
        config = write_env(root / '.env', DATA_DIR='./absent-data')
        before = config.read_bytes()
        result = self.run_check(root, config)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn('配置通过校验', result.stdout)
        self.assertEqual({path.name for path in root.iterdir()}, {'.env', 'emoskata.py'})
        self.assertEqual(config.read_bytes(), before)
        self.assertNotIn('fixture_password', result.stdout + result.stderr)

    def test_check_config_missing_file_creates_nothing(self):
        root = self.root / 'absent-config'
        root.mkdir()
        config = root / 'missing' / '.env'
        result = self.run_check(root, config)
        self.assertEqual(result.returncode, 2, result.stderr + result.stdout)
        self.assertIn('.env.example', result.stdout)
        self.assertEqual({path.name for path in root.iterdir()}, {'emoskata.py'})

    def test_check_config_does_not_execute_update_or_application_modules(self):
        root = self.root / 'isolated-check'
        config = write_env(root / '.env')
        source = build(write=False)
        tree = ast.parse(source)
        assignment = next(node for node in tree.body if isinstance(node, ast.Assign)
                          and any(isinstance(target, ast.Name) and target.id == '_SOURCES'
                                  for target in node.targets))
        modules = ast.literal_eval(assignment.value)
        modules['updates'] = "raise RuntimeError('check must not recover updates')\n"
        modules['app'] = "raise RuntimeError('check must not initialize runtime')\n"
        lines = source.splitlines(keepends=True)
        lines[assignment.lineno - 1:assignment.end_lineno] = ['_SOURCES = ' + repr(modules) + '\n']
        result = self.run_check(root, config, ''.join(lines))
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

    def test_bot_only_empty_password_can_be_saved_and_reloaded(self):
        config = write_env(self.root / 'bot' / '.env', TG_BOT_TOKEN='123456:synthetic_bot_token',
                           TG_ALLOWED_USER_IDS='101', HTTP_ENABLED='0')
        values = parse_env(config.read_text())
        values['PANEL_PASSWORD'] = ''
        config.write_text(serialize_env(values))
        store = ConfigStore(config)
        outcome = store.update({'DATA_DIR': './new-data'})
        self.assertEqual(outcome['changed'], ['DATA_DIR'])
        reloaded = ConfigStore(config).snapshot()[0]
        self.assertEqual(reloaded['DATA_DIR'], './new-data')
        self.assertEqual(reloaded['PANEL_PASSWORD'], '')
        before = config.read_bytes()
        with self.assertRaises(EnvConfigError):
            store.update({'HTTP_ENABLED': '1'})
        self.assertEqual(config.read_bytes(), before)
        store.update({'HTTP_ENABLED': '1', 'PANEL_PASSWORD': 'new_synthetic_password'})
        self.assertEqual(ConfigStore(config).snapshot()[0]['HTTP_ENABLED'], '1')

    def test_explicitly_saved_missing_default_survives_reload_and_legacy_import(self):
        app = fixture_app(self, self.root / 'legacy', 'takeover_legacy_fixture', single_owner=True)
        self.addCleanup(app.close_idle_db)
        app.init_db()
        values, _ = app.CONFIG.snapshot()
        values.pop('SIGN_TIME')
        app.CONFIG.path.write_text(''.join('%s=%r\n' % item for item in values.items()))
        app.CONFIG = ConfigStore(app.CONFIG.path)
        self.assertNotIn('SIGN_TIME', app.CONFIG.provided_keys)
        original = app.CONFIG.path.read_bytes()
        outcome = app.CONFIG.update({'SIGN_TIME': '08:00'})
        self.assertEqual(outcome['changed'], [])
        self.assertNotEqual(app.CONFIG.path.read_bytes(), original)
        app.CONFIG = ConfigStore(app.CONFIG.path)
        app.set_setting('sign_time', '07:30')
        before = app.CONFIG.path.read_bytes()
        self.assertNotIn('SIGN_TIME', app.import_legacy_config()['imported'])
        self.assertEqual(app.CONFIG.path.read_bytes(), before)
        self.assertEqual(app.cfg_all()['sign_time'], '08:00')

    def test_reloaded_provided_keys_follow_external_file_changes(self):
        path = write_env(self.root / 'external' / '.env')
        store = ConfigStore(path)
        values, _ = store.snapshot()
        values.pop('SIGN_TIME')
        path.write_text(''.join('%s=%r\n' % item for item in values.items()))
        store.snapshot(force=True)
        self.assertNotIn('SIGN_TIME', store.provided_keys)
        path.write_text(path.read_text() + 'SIGN_TIME="08:00"\n')
        values, _ = store.snapshot(force=True)
        self.assertEqual(values['SIGN_TIME'], '08:00')
        self.assertIn('SIGN_TIME', store.provided_keys)

    def test_checked_in_template_matches_schema_generator(self):
        template = Path(__file__).with_name('.env.example').read_text(encoding='utf-8')
        self.assertEqual(template, render_env_example())


if __name__ == '__main__':
    unittest.main()
