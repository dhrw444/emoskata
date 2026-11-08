"""配置优先级与升级补项回归。

只使用临时配置和 SQLite，验证已有值、显式默认值及清空凭据不被迁移覆盖。
Test: python3 -B -m unittest test_config_upgrade
"""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from envconfig import ConfigStore, EnvConfigError, SCHEMA
from test_support import fixture_app


class ConfigUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='emos-config-upgrade-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = fixture_app(self, self.root / 'config', 'config_upgrade_app', single_owner=True)
        self.app.init_db()
        self.addCleanup(self.app.close_idle_db)

    def partial_config(self, absent):
        values, _ = self.app.CONFIG.snapshot()
        path = self.root / 'partial.env'
        path.write_text('# existing deployment config\n' + ''.join(
            key + '=' + json.dumps(value) + '\n'
            for key, value in values.items() if key not in absent))
        path.chmod(0o600)
        return ConfigStore(path, create=True), {key: value for key, value in values.items() if key not in absent}

    def legacy_values(self):
        self.app.set_setting('sign_time', '07:30')
        self.app.set_setting('gap_seconds', '12')
        self.app.set_setting('tg_bot_token', '123456:synthetic_legacy_token')

    def test_explicit_defaults_and_cleared_token_are_preserved(self):
        self.app.CONFIG.update({'SIGN_TIME': '08:00', 'GAP_SECONDS': '8'},
                               clear_secrets=['TG_BOT_TOKEN'])
        before = self.app.CONFIG.path.read_bytes()
        self.legacy_values()
        state = self.app.import_legacy_config()
        self.assertEqual(state['imported'], [])
        self.assertEqual(self.app.CONFIG.path.read_bytes(), before)
        values, _ = self.app.CONFIG.snapshot()
        self.assertEqual((values['SIGN_TIME'], values['GAP_SECONDS'], values['TG_BOT_TOKEN']),
                         ('08:00', '8', ''))
        for key in ('SIGN_TIME', 'GAP_SECONDS', 'TG_BOT_TOKEN'):
            self.assertIn(key, state['untouched'])

    def test_saved_default_on_previously_missing_field_counts_as_explicit(self):
        self.app.CONFIG, _ = self.partial_config({'SIGN_TIME', 'TG_BOT_TOKEN'})
        self.assertNotIn('SIGN_TIME', self.app.CONFIG.provided_keys)
        self.app.CONFIG.update({'SIGN_TIME': '08:00'}, clear_secrets=['TG_BOT_TOKEN'])
        before = self.app.CONFIG.path.read_bytes()
        self.legacy_values()
        state = self.app.import_legacy_config()
        self.assertEqual(state['imported'], [])
        self.assertEqual(self.app.CONFIG.path.read_bytes(), before)
        self.assertIn('SIGN_TIME', self.app.CONFIG.provided_keys)
        self.assertIn('TG_BOT_TOKEN', self.app.CONFIG.provided_keys)

    def test_missing_schema_fields_are_added_without_changing_existing_values(self):
        missing = {'EMOS_AUTH_BOT', 'PUBLIC_BASE'}
        config, existing = self.partial_config(missing)
        values, _ = config.snapshot()
        self.assertEqual(set(values), set(SCHEMA))
        self.assertEqual({key: values[key] for key in existing}, existing)
        self.assertEqual({key: values[key] for key in missing},
                         {key: SCHEMA[key].default for key in missing})
        self.assertEqual(config.path.stat().st_mode & 0o777, 0o600)
        before = config.path.read_bytes()
        with patch.dict('os.environ', {'PANEL_PASSWORD': 'different_fixture_password',
                                      'SIGN_TIME': '09:30', 'TG_ALLOWED_USER_IDS': '999'}):
            reloaded = ConfigStore(config.path, create=True)
        self.assertEqual(reloaded.snapshot()[0], values)
        self.assertEqual(reloaded.path.read_bytes(), before)

    def test_first_creation_seeds_explicit_default_without_legacy_override(self):
        path = self.root / 'new' / '.env'
        with patch.dict('os.environ', {'PANEL_PASSWORD': 'seed_fixture_password',
                                      'SIGN_TIME': '08:00', 'GAP_SECONDS': '8',
                                      'TG_BOT_TOKEN': '', 'TG_ALLOWED_USER_IDS': '101'}):
            config = ConfigStore(path, create=True)
        self.assertTrue(config.created)
        self.assertIn('SIGN_TIME', config.provided_keys)
        self.assertIn('TG_BOT_TOKEN', config.provided_keys)
        self.app.CONFIG = config
        self.legacy_values()
        before = path.read_bytes()
        self.assertEqual(self.app.import_legacy_config()['imported'], [])
        self.assertEqual(path.read_bytes(), before)

    def test_failed_update_does_not_write_or_mark_missing_field_as_provided(self):
        config, _ = self.partial_config({'SIGN_TIME'})
        before = config.path.read_bytes()
        with self.assertRaises(EnvConfigError):
            config.update({'SIGN_TIME': 'not-a-time'})
        self.assertEqual(config.path.read_bytes(), before)
        self.assertNotIn('SIGN_TIME', config.provided_keys)
