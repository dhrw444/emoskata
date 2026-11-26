"""多 TG 用户：配置解析、权限、调度与通知按人隔离。

只用临时库与合成账号；不代表真实官方授权。
Test: python3 -B -m unittest test_multi_owner
"""
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from envconfig import configured_owners, owner_permitted, validate_env, SCHEMA
from test_support import fixture_app


class ConfigTests(unittest.TestCase):
    def base(self):
        values = {key: field.default for key, field in SCHEMA.items()}
        values['PANEL_PASSWORD'] = 'fixture_password'
        return values

    def test_multiple_ids_parse_dedupe_and_sort(self):
        self.assertEqual(configured_owners({'tg_allowed_user_ids': '101'}),
                         (101,))
        self.assertEqual(configured_owners({'tg_allowed_user_ids': '202, 101;202'}),
                         (101, 202))
        self.assertEqual(configured_owners({'tg_allowed_user_ids': ''}), ())
        # 任一项非法则整体失效，不能部分放行。
        self.assertEqual(configured_owners({'tg_allowed_user_ids': '101,abc'}), ())

    def test_saved_value_is_normalized(self):
        ok = validate_env(dict(self.base(), TG_ALLOWED_USER_IDS='202, 101 202;303'))
        self.assertEqual(ok['TG_ALLOWED_USER_IDS'], '101,202,303')

    def test_permission_is_membership(self):
        cfg = {'tg_allowed_user_ids': '101,202'}
        self.assertTrue(owner_permitted(cfg, 101))
        self.assertTrue(owner_permitted(cfg, 202))
        self.assertFalse(owner_permitted(cfg, 303))
        # 留空是公开模式：任何合法 TG 私聊身份都放行。
        self.assertTrue(owner_permitted({'tg_allowed_user_ids': ''}, 101))
        self.assertFalse(owner_permitted({'tg_allowed_user_ids': 'bad'}, 101))


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='emos-multi-owner-')
        self.addCleanup(self.tmp.cleanup)
        self.app = fixture_app(self, self.tmp.name, 'multi_owner_app', single_owner=True)
        self.app.init_db()
        self.addCleanup(self.app.close_idle_db)

    def seed(self, tg_id, emos, token):
        with self.app.write_db() as conn:
            conn.execute('INSERT INTO tg_users(tg_id,chat_id) VALUES(?,?)', (tg_id, tg_id))
            cur = conn.execute(
                'INSERT INTO accounts(name,token,emos_id,owner_tg_id,auto_sign,sign_time) '
                "VALUES(?,?,?,?,1,'01:00')", (emos, token, emos, tg_id))
            account_id = cur.lastrowid
            conn.execute('UPDATE tg_users SET primary_account_id=?,active_account_id=? '
                         'WHERE tg_id=?', (account_id, account_id, tg_id))
        return account_id

    def test_scheduler_covers_every_primary_owner(self):
        self.seed(101, 'eA', '11_fixture_a')
        self.seed(202, 'eB', '22_fixture_b')
        seen = []
        def fake_run(cfg, ids, trigger='manual', **kwargs):
            seen.append(sorted(ids))
            return []
        self.app.CONFIG.update({'TG_ALLOWED_USER_IDS': '101,202'})
        later = datetime(2026, 11, 9, 9, 0, tzinfo=timezone.utc)
        with patch.object(self.app, 'cfg_all', return_value=dict(
                self.app.CONFIG.app_config(), tg_allowed_user_ids='101,202')), \
             patch.object(self.app, '_run_ids', side_effect=fake_run), \
             patch.object(self.app, 'now_local', return_value=later):
            self.app.scheduler_tick()
        self.assertEqual(len(seen), 1)
        self.assertEqual(len(seen[0]), 2)

    def test_owner_without_primary_does_not_block_others(self):
        self.seed(202, 'eB', '22_fixture_b')
        seen = []
        def fake_run(cfg, ids, trigger='manual', **kwargs):
            seen.append(sorted(ids))
            return []
        later = datetime(2026, 11, 9, 9, 0, tzinfo=timezone.utc)
        with patch.object(self.app, 'cfg_all', return_value=dict(
                self.app.CONFIG.app_config(), tg_allowed_user_ids='101,202')), \
             patch.object(self.app, '_run_ids', side_effect=fake_run), \
             patch.object(self.app, 'now_local', return_value=later):
            self.app.scheduler_tick()
        # 101 没有主号，只调度 202，且不能整体返回空。
        self.assertEqual(len(seen), 1)
        self.assertEqual(len(seen[0]), 1)


if __name__ == '__main__':
    unittest.main()
