"""回归部署配置读取、内存补项、原子保存与旧库迁移。

仅用隔离目录、合成身份或 localhost；正式授权和真实生产资源不在此验收。
Test: python3 -B -m unittest test_config_upgrade"""
import json
from pathlib import Path
import tempfile
import unittest

from envconfig import (ConfigStore, EnvConfigError, SCHEMA, complete_values,
                       missing_config_message, parse_env, serialize_env, template_values)
from test_support import fixture_app, write_env

class ConfigUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='emos-config-upgrade-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = fixture_app(self, self.root / 'config', 'config_upgrade_app', single_owner=True)
        self.app.init_db()
        self.addCleanup(self.app.close_idle_db)

    def partial_config(self, absent):
        """写一份缺若干键的 .env，模拟部署者只填了关心的项。"""
        values, _ = self.app.CONFIG.snapshot()
        path = self.root / 'partial.env'
        path.write_text('# existing deployment config\n' + ''.join(
            key + '=' + json.dumps(value) + '\n'
            for key, value in values.items() if key not in absent))
        path.chmod(0o600)
        return ConfigStore(path), {key: value for key, value in values.items() if key not in absent}

    def legacy_values(self):
        self.app.set_setting('sign_time', '07:30')
        self.app.set_setting('gap_seconds', '12')
        self.app.set_setting('tg_bot_token', '123456:synthetic_legacy_token')

    def test_missing_file_is_an_error_and_is_not_created(self):
        # 程序绝不代建配置：报错要能照着做，且目录里不会多出文件。
        path = self.root / 'absent' / '.env'
        with self.assertRaises(EnvConfigError) as caught:
            ConfigStore(path)
        self.assertIn('.env.example', str(caught.exception))
        self.assertFalse(path.exists())
        self.assertFalse(path.parent.exists())

    def test_directory_in_place_of_file_is_rejected(self):
        path = self.root / 'weird' / '.env'
        path.mkdir(parents=True)
        with self.assertRaises(EnvConfigError):
            ConfigStore(path)

    def test_missing_keys_are_completed_in_memory_only(self):
        # 缺项按 schema 默认值补进内存，文件一个字节都不改。
        missing = {'EMOS_AUTH_BOT', 'PUBLIC_BASE'}
        config, existing = self.partial_config(missing)
        before = config.path.read_bytes()
        values, revision = config.snapshot()
        self.assertEqual(set(values), set(SCHEMA))
        self.assertEqual({key: values[key] for key in existing}, existing)
        self.assertEqual({key: values[key] for key in missing},
                         {key: SCHEMA[key].default for key in missing})
        self.assertEqual(config.path.read_bytes(), before)
        self.assertEqual(config.provided_keys, set(existing))
        self.assertEqual(revision, __import__('hashlib').sha256(before).hexdigest())
        # 重复加载也不该写盘。
        self.assertEqual(ConfigStore(config.path).snapshot()[0], values)
        self.assertEqual(config.path.read_bytes(), before)

    def test_env_process_values_never_override_the_file(self):
        # .env 是唯一运行来源；进程环境同名值只在没有文件时才可能被用到（现在也不会）。
        config, _ = self.partial_config({'SIGN_TIME'})
        before = config.path.read_bytes()
        import os
        from unittest.mock import patch
        with patch.dict(os.environ, {'PANEL_PASSWORD': 'different_fixture_password',
                                    'SIGN_TIME': '09:30', 'TG_ALLOWED_USER_IDS': '999'}):
            reloaded = ConfigStore(config.path)
        self.assertEqual(reloaded.snapshot()[0]['SIGN_TIME'], SCHEMA['SIGN_TIME'].default)
        self.assertEqual(reloaded.path.read_bytes(), before)

    def test_web_save_still_writes_the_file_and_persists(self):
        # 网页保存是唯一的写路径：改一个值必须落盘，并且能原样读回。
        path = self.root / 'save' / '.env'
        write_env(path, 'fixture_password', SIGN_TIME='08:00')
        config = ConfigStore(path)
        before_revision = config.snapshot()[1]
        outcome = config.update({'SIGN_TIME': '09:15', 'GAP_SECONDS': '15'},
                                revision=before_revision)
        self.assertEqual(sorted(outcome['changed']), ['GAP_SECONDS', 'SIGN_TIME'])
        self.assertEqual(outcome['restart_required'], [])
        on_disk = parse_env(path.read_text(encoding='utf-8'))
        self.assertEqual((on_disk['SIGN_TIME'], on_disk['GAP_SECONDS']), ('09:15', '15'))
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertNotEqual(outcome['revision'], before_revision)
        self.assertEqual(ConfigStore(path).snapshot()[0]['SIGN_TIME'], '09:15')

    def test_web_save_adds_a_key_missing_from_the_file(self):
        # 部署者手写的 .env 可以不全；网页保存时应把改到的键写进去，其余保持原样。
        path = self.root / 'sparse' / '.env'
        write_env(path, 'fixture_password')
        kept = [line for line in path.read_text(encoding='utf-8').splitlines()
                if not line.startswith('PUBLIC_BASE=')]
        path.write_text('\n'.join(kept) + '\n', encoding='utf-8')
        config = ConfigStore(path)
        self.assertNotIn('PUBLIC_BASE', config.provided_keys)
        config.update({'PUBLIC_BASE': 'https://panel.example.com'})
        self.assertEqual(parse_env(path.read_text(encoding='utf-8'))['PUBLIC_BASE'],
                         'https://panel.example.com')
        self.assertEqual(config.snapshot()[0]['PUBLIC_BASE'], 'https://panel.example.com')

    def test_web_save_rejects_stale_revision_without_touching_the_file(self):
        path = self.root / 'stale' / '.env'
        write_env(path, 'fixture_password')
        config = ConfigStore(path)
        stale = config.snapshot()[1]
        config.update({'GAP_SECONDS': '9'})
        before = path.read_bytes()
        with self.assertRaises(EnvConfigError):
            config.update({'SIGN_TIME': '10:00'}, revision=stale)
        self.assertEqual(path.read_bytes(), before)

    def test_web_save_keeps_secrets_when_blank_and_clears_only_on_request(self):
        path = self.root / 'secret' / '.env'
        write_env(path, 'fixture_password', TG_BOT_TOKEN='123456:fixture_bot_token')
        config = ConfigStore(path)
        config.update({'TG_BOT_TOKEN': ''})
        self.assertEqual(config.snapshot()[0]['TG_BOT_TOKEN'], '123456:fixture_bot_token')
        config.update({}, clear_secrets=['TG_BOT_TOKEN'])
        self.assertEqual(config.snapshot()[0]['TG_BOT_TOKEN'], '')
        self.assertEqual(parse_env(path.read_text(encoding='utf-8'))['TG_BOT_TOKEN'], '')

    def test_web_save_is_atomic_on_invalid_value(self):
        path = self.root / 'invalid' / '.env'
        write_env(path, 'fixture_password')
        config = ConfigStore(path)
        before = path.read_bytes()
        with self.assertRaises(EnvConfigError):
            config.update({'SIGN_TIME': 'not-a-time'})
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(config.provided_keys, set(SCHEMA))

    def test_panel_password_is_required_when_the_panel_is_enabled(self):
        # 开网页就必须有密码；关网页（纯 Bot）允许空密码。
        path = self.root / 'nopw' / '.env'
        path.parent.mkdir(parents=True)
        path.write_text('HTTP_ENABLED="1"\nPANEL_PASSWORD=""\n', encoding='utf-8')
        path.chmod(0o600)
        with self.assertRaises(EnvConfigError):
            ConfigStore(path)
        path.write_text('HTTP_ENABLED="0"\nPANEL_PASSWORD=""\n'
                        'TG_BOT_TOKEN="123456:fixture_bot_token"\n'
                        'TG_ALLOWED_USER_IDS="101"\n', encoding='utf-8')
        values = ConfigStore(path).snapshot()[0]
        self.assertEqual(values['PANEL_PASSWORD'], '')

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

    def test_failed_update_does_not_write_or_mark_missing_field_as_provided(self):
        config, _ = self.partial_config({'SIGN_TIME'})
        before = config.path.read_bytes()
        with self.assertRaises(EnvConfigError):
            config.update({'SIGN_TIME': 'not-a-time'})
        self.assertEqual(config.path.read_bytes(), before)
        self.assertNotIn('SIGN_TIME', config.provided_keys)

    def test_complete_values_matches_serialize_round_trip(self):
        # 内存补项和序列化必须互相对得上，否则网页保存会把默认值写歪。
        values = template_values('fixture_password')
        self.assertEqual(complete_values(parse_env(serialize_env(values))), values)
        self.assertEqual(missing_config_message('/x/.env').count('.env.example'), 1)

if __name__ == '__main__':
    unittest.main()
