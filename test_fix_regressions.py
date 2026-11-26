"""本轮修复的针对性回归：env 优先、删除键不回写、余额刷新不签到。"""
from pathlib import Path
import tempfile
import unittest

from envconfig import ConfigStore, serialize_env, template_values
from test_support import fixture_app
import bot
from test_runtime import FakeTG, serve


class ConfigUpgradeTests(unittest.TestCase):
    def test_deleted_key_is_not_written_back(self):
        with tempfile.TemporaryDirectory(prefix='emos-del-key-') as folder:
            path = Path(folder) / '.env'
            values = template_values(password='deleted_key_fixture')
            values['EMOS_USER_ID'] = 'eProvider'
            path.write_text(serialize_env(values))
            path.chmod(0o600)
            lines = [line for line in path.read_text().splitlines(keepends=True)
                     if not line.strip().startswith('EMOS_USER_ID=')]
            path.write_text(''.join(lines))
            self.assertNotIn('EMOS_USER_ID', path.read_text())
            store = ConfigStore(path)
            self.assertEqual(store.app_config().get('emos_user_id'), '')
            # 关键：启动不能把删掉的键按默认值写回文件。
            self.assertNotIn('EMOS_USER_ID', path.read_text())


class BalanceRefreshTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='emos-balance-')
        self.addCleanup(self.tmp.cleanup)
        self.app = fixture_app(self, self.tmp.name, 'balance_fixture', single_owner=True)
        self.app.init_db()
        self.addCleanup(self.app.close_idle_db)
        self.service = self.app.Store().accounts

    def test_refresh_reads_balances_without_signing(self):
        with serve() as (server, base):
            self.service.begin_login(101, 'eA')
            self.service.finish_login(101, '11_balA_fixture', {'user_id': 'eA', 'telegram_user_id': 101,
                                                       '_carrot': 10}, base)
            # 额外账号走 token 添加，官方绑定可以是别的 TG。
            self.service.login_with_token(101, '11_balB_fixture',
                                          {'user_id': 'eB', 'telegram_user_id': 202,
                                           '_carrot': 20}, base)
            server.identities['11_balA_fixture'] = {'user_id': 'eA', 'telegram_user_id': 101, 'carrot': 111}
            server.identities['11_balB_fixture'] = {'user_id': 'eB', 'telegram_user_id': 202, 'carrot': 222}
            server.hits.clear()
            rows = self.app.refresh_balances(101, include_links=True)
            self.assertEqual(sorted(r['balance'] for r in rows), [111, 222])
            # 余额刷新绝不能发送签到写请求。
            self.assertFalse([hit for hit in server.hits if hit[0] == 'PUT'], server.hits)
            self.assertEqual(server.signed, set())
            stored = {row['emos_id']: row['carrot'] for row in self.service.owned(101)}
            self.assertEqual(stored, {'eA': 111, 'eB': 222})


class EnvPriorityTests(unittest.TestCase):
    def test_env_emos_id_overrides_cached_primary(self):
        with tempfile.TemporaryDirectory(prefix='emos-env-pri-') as folder:
            app = fixture_app(self, Path(folder) / 'cfg', 'env_priority_fixture', single_owner=True)
            app.init_db()
            self.addCleanup(app.close_idle_db)
            app.Store().accounts.begin_login(101, 'eOldPrimary')
            app.Store().accounts.finish_login(101, '11_old_fixture', {'user_id': 'eOldPrimary',
                                                             'telegram_user_id': 101})
            # .env 改成新 ID 后，/login 必须使用 .env，而不是缓存里的旧主号。
            app.CONFIG.update({'EMOS_USER_ID': 'eFromEnv'})
            tg = FakeTG()
            instance = bot.Bot('fixture:bot', app.Store(), tg=tg, synchronous=True)
            instance._accept_me(tg.get_me())
            self.addCleanup(instance.stop)
            instance._handle({'update_id': 1, 'message': {'message_id': 1,
                'from': {'id': 101}, 'chat': {'id': 101, 'type': 'private'}, 'text': '/login'}})
            urls = [b['url'] for _, _, keyboard in tg.messages
                    for row in keyboard or [] for b in row if 'url' in b]
            self.assertTrue(any('link_eFromEnv-' in u for u in urls), urls)
            self.assertFalse(any('link_eOldPrimary-' in u for u in urls), urls)


if __name__ == '__main__':
    unittest.main()
