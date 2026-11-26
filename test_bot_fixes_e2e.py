"""验证 Bot 侧：.env 优先、官方换绑、签到不阻塞其他命令。"""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_support import fixture_app
from test_runtime import FakeTG
import bot


class BotFixTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='emos-botfix-')
        self.addCleanup(self.tmp.cleanup)
        self.app = fixture_app(self, Path(self.tmp.name) / 'cfg', 'botfix', single_owner=True)
        self.app.init_db()
        self.addCleanup(self.app.close_idle_db)
        self.service = self.app.Store().accounts

    def _bot(self, profile=None):
        tg = FakeTG()
        factory = None
        if profile is not None:
            class FakeClient:
                def __init__(self, token, base=''):
                    self.token = token
                    self.timeout = 20
                def profile(self):
                    return dict(profile)
            factory = FakeClient
        instance = bot.Bot('fixture:bot', self.app.Store(), tg=tg, synchronous=True,
                           client_factory=factory)
        instance._accept_me(tg.get_me())
        self.addCleanup(instance.stop)
        return instance, tg

    def _urls(self, tg):
        return [b['url'] for _, _, kb in tg.messages
                for row in kb or [] for b in row if 'url' in b]

    def test_env_id_wins_over_cached_primary(self):
        self.service.begin_login(101, 'eCached')
        self.service.finish_login(101, '11_cached_token', {'user_id': 'eCached',
                                                           'telegram_user_id': 101})
        self.app.CONFIG.update({'EMOS_USER_ID': 'eFromEnv'})
        instance, tg = self._bot()
        with patch.object(self.app._shutdown, 'wait', return_value=False):
            instance._handle({'update_id': 1, 'message': {'message_id': 1,
                'from': {'id': 101}, 'chat': {'id': 101, 'type': 'private'}, 'text': '/login'}})
        urls = self._urls(tg)
        self.assertTrue(any('link_eFromEnv-' in u for u in urls), urls)
        self.assertFalse(any('link_eCached-' in u for u in urls), urls)

    def test_official_rebinding_moves_primary_keeping_data(self):
        self.service.begin_login(101, 'eOld')
        self.service.finish_login(101, '11_old_token', {'user_id': 'eOld',
                                                        'telegram_user_id': 101, '_carrot': 55})
        self.app.CONFIG.update({'EMOS_USER_ID': 'eNew'})
        instance, tg = self._bot(profile={'user_id': 'eNew', 'telegram_user_id': 101,
                                          '_display': '新账号', '_carrot': 77})
        with patch.object(self.app._shutdown, 'wait', return_value=False):
            instance._handle({'update_id': 2, 'message': {'message_id': 2,
                'from': {'id': 101}, 'chat': {'id': 101, 'type': 'private'}, 'text': '/login'}})
            self.assertTrue(any('link_eNew-' in u for u in self._urls(tg)))
            instance._handle({'update_id': 3, 'message': {'message_id': 3,
                'from': {'id': 101}, 'chat': {'id': 101, 'type': 'private'},
                'text': '/start emosLinkAgree-11_new_token'}})
        account = self.service.primary_account(101)
        self.assertEqual(account['emos_id'], 'eNew')
        self.assertEqual(len(self.service.owned(101)), 1)

    def test_sign_job_does_not_block_other_commands(self):
        self.service.begin_login(101, 'eA')
        self.service.finish_login(101, '11_sign_token', {'user_id': 'eA',
                                                         'telegram_user_id': 101})
        instance, tg = self._bot()
        started = []
        import threading
        release = threading.Event()

        def slow_run(*a, **k):
            started.append(1)
            release.wait(5)
            return []
        instance.store.run_user = slow_run
        with patch.object(self.app._shutdown, 'wait', return_value=False):
            instance._jobs.synchronous = False
            instance._handle({'update_id': 10, 'message': {'message_id': 10,
                'from': {'id': 101}, 'chat': {'id': 101, 'type': 'private'}, 'text': '/sign'}})
            deadline = __import__('time').monotonic() + 5
            while not started and __import__('time').monotonic() < deadline:
                __import__('time').sleep(0.05)
            self.assertTrue(started, 'sign job did not start')
            # 签到还在跑时，菜单命令必须仍然可用。
            instance._handle({'update_id': 11, 'message': {'message_id': 11,
                'from': {'id': 101}, 'chat': {'id': 101, 'type': 'private'}, 'text': '/accounts'}})
            texts = [t for _, t, _ in tg.messages]
            self.assertFalse(any('队列已满' in t or '已有操作在处理' in t for t in texts), texts)
            release.set()
            instance.join_jobs(5)


if __name__ == '__main__':
    unittest.main()
