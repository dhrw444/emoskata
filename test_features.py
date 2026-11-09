"""配置、官方身份、并发状态与关闭共享入口回归。

仅用隔离目录、合成身份或 localhost；正式授权和真实生产资源不在此验收。
Test: python3 -B -m unittest test_features"""
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from envconfig import ConfigStore, EnvConfigError
from test_support import fixture_app, configure
from test_runtime import serve, Fixture, FakeTG
from accounts import DomainError
import bot
import webui
from emos import EmosClient


class FeatureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='emos-feature-')
        self.addCleanup(self.tmp.cleanup)
        self.app = fixture_app(self, self.tmp.name, 'feature_fixture_app')
        self.app.init_db()
        self.app.init_password()
        self.addCleanup(self.app.close_idle_db)
        self.service = self.app.Store().accounts
        self.web = self.app.personal_web()
        self.addCleanup(self.web.stop)
        self.ids = {}
        for actor, label in ((101, 'A'), (202, 'B'), (303, 'C')):
            self.service.begin_login(actor, 'e' + label)
            self.ids[actor] = self.service.finish_login(actor, '11_fixture_' + label,
                {'user_id': 'e' + label, 'telegram_user_id': actor, 'username': label})['id']

    def pending(self):
        code = self.service.issue_code(202)
        return self.web.action(101, 'link', {'account_id': self.ids[101],
                                           'target_emos_id': 'eB', 'code': code['code']})

    def review(self, actor, approve):
        return self.web.action(actor, 'review', {'account_id': self.ids[actor],
            'source_id': self.ids[101], 'target_id': self.ids[202], 'approve': approve})

    def test_real_official_client_uses_each_targets_own_token_after_confirmation(self):
        with serve() as (server, base):
            for actor, label in ((101,'A'), (202,'B'), (303,'C')):
                server.identities['11_fixture_'+label] = {'user_id':'e'+label,'telegram_user_id':actor}
            with self.app.write_db() as conn:
                conn.execute('UPDATE accounts SET base_url=?', (base,))
            with patch.object(self.app._shutdown, 'wait', return_value=False):
                self.assertTrue(self.pending()['pending'])
                self.app.run_user(101)
                self.assertEqual(server.signed, {'eA'})
                self.assertFalse(any(who=='eB' for _,_,who in server.hits))
                server.signed.clear()
                server.hits.clear()
                self.review(202, True)
                rows = self.app.run_user(101)
                self.assertEqual(server.signed, {'eA','eB'})
                self.assertEqual([(method,path,who) for method,path,who in server.hits],
                    [('GET','/api/user','eA'),('PUT','/api/user/sign','eA'),
                     ('GET','/api/user','eB'),('PUT','/api/user/sign','eB')])
                self.assertEqual([row['emos_id'] for row in rows], ['eA','eB'])
                self.service.unlink(202, self.ids[101], self.ids[202])
                server.signed.clear()
                server.hits.clear()
                self.app.run_user(101)
                self.assertEqual(server.signed, {'eA'})
                self.assertFalse(any(who=='eB' for _,_,who in server.hits))

    def test_old_shared_callbacks_cannot_be_used_by_owner_bot(self):
        self.pending()
        original = self.app.cfg_all
        self.app.cfg_all = lambda: dict(original(), tg_allowed_user_ids='101')
        tg = FakeTG()
        instance = bot.Bot('123456:fixture_bot', self.app.Store(), tg=tg, synchronous=True)
        instance._accept_me(tg.get_me())
        self.addCleanup(instance.stop)
        root, target = self.ids[101], self.ids[202]
        for index, data in enumerate(('links:%d:0' % root,
                'review:%d:%d:%d:1' % (root, root, target),
                'withdraw:%d:%d:%d' % (root, root, target)), 1):
            instance._handle({'update_id': index, 'callback_query': {'id': 'closed%d' % index,
                'from': {'id': 101}, 'message': {'message_id': index,
                    'chat': {'id': 101, 'type': 'private'}}, 'data': data}})
        self.assertTrue(any('跨 TG 共享' in text for _, text, _ in tg.messages))
        with self.app.db() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM link_requests').fetchone()[0], 1)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM account_links').fetchone()[0], 0)

    def test_pending_expiry_replay_ownership_and_bool_checks(self):
        self.pending()
        with self.app.write_db() as conn:
            conn.execute('UPDATE link_requests SET source_owner=303')
        with self.assertRaises(DomainError):
            self.review(202,True)
        self.service.withdraw_link(101,self.ids[101],self.ids[202])
        self.pending()
        for value in (1, 'true', None):
            with self.assertRaises(DomainError):
                self.review(202,value)
        with self.app.write_db() as conn:
            conn.execute('UPDATE link_requests SET expires_at=1')
        with self.assertRaises(DomainError):
            self.review(202,True)
        self.assertFalse(self.service.can_run(101,self.ids[101],self.ids[202]))

    def test_web_session_duration_is_real_db_expiry_and_applies_to_existing(self):
        configure(self.app, web_base_url='http://127.0.0.1:8080', web_session_hours=2)
        with patch.object(webui.time,'time',return_value=1000):
            code = self.web.issue_url(101).split('#code=',1)[1]
            login = self.web.exchange(code)
        with self.app.db() as conn:
            row=conn.execute('SELECT created,expires_at FROM web_sessions').fetchone()
        self.assertEqual(tuple(row),(1000,8200))
        configure(self.app, web_session_hours=1)
        with patch.object(webui.time,'time',return_value=4600):
            self.assertIsNone(self.web.authenticate(login['sid']))

    def test_sqlite_leases_reuse_connections_rollback_and_close(self):
        self.app.close_idle_db()
        original=sqlite3.connect
        with patch.object(self.app.sqlite3,'connect',wraps=original) as calls:
            for _ in range(20):
                self.service.active(101)
                self.service.visible(101)
            self.assertEqual(calls.call_count,1)
        with self.assertRaises(RuntimeError):
            with self.app.write_db() as conn:
                conn.execute('UPDATE tg_users SET notify_success=0 WHERE tg_id=101')
                raise RuntimeError('synthetic rollback')
        self.assertEqual(self.service.user(101)['notify_success'],1)
        with self.app.db() as conn:
            self.assertEqual(conn.execute('PRAGMA foreign_keys').fetchone()[0],1)
        self.assertEqual(self.app._db_borrowed,0)
        self.assertLessEqual(len(self.app._db_pool),3)
        self.app.close_idle_db()
        self.assertEqual(self.app._db_pool,[])

    def test_cancelled_restart_resumes_bot_web_and_retains_service(self):
        self.web.begin_drain()
        self.app._draining.set()
        with self.assertRaises(DomainError):
            self.web.start_sign(101,{'account_id':self.ids[101],'request_id':'fixture_restart_0001'})
        with patch.object(self.app,'_start_core_threads'):
            self.app._cancel_restart('fixture timeout','timeout')
        self.assertFalse(self.app._shutdown.is_set())
        self.assertFalse(self.app._draining.is_set())
        self.assertTrue(self.web.accepting)
        instance=bot.Bot('123456:fixture_bot',self.app.Store(),tg=FakeTG(),synchronous=True)
        self.addCleanup(instance.stop)
        instance.begin_drain()
        self.assertFalse(instance._jobs.submit(101,lambda:None))
        instance.resume()
        self.assertTrue(instance._jobs.submit(101,lambda:None))

    def test_complete_env_adoption_atomic_rejection_and_mode(self):
        path=Path(self.tmp.name)/'legacy' /'.env'
        path.parent.mkdir()
        path.write_text('PANEL_PASSWORD="fixture_password"\nTG_BOT_TOKEN=""\n')
        path.chmod(0o600)
        # 程序只读：缺项补内存，不改文件，也不替部署者改权限。
        config=ConfigStore(path)
        self.assertEqual(path.stat().st_mode & 0o777,0o600)
        from envconfig import SCHEMA
        self.assertEqual(set(config.snapshot()[0]),set(SCHEMA))
        before=path.read_bytes()
        for value in ('x\u0085x','x\u2028x','x\u2029x'):
            with self.assertRaises(EnvConfigError):
                config.update({'PUBLIC_BASE':value})
            self.assertEqual(path.read_bytes(),before)
        self.assertEqual(config.snapshot()[0]['PANEL_PASSWORD'],'fixture_password')

    def test_business_rejected_sign_sends_one_put_and_limits_not_success(self):
        class Rejected(Fixture):
            def do_PUT(self):
                self.server.hits.append(('PUT',self.path.split('?')[0],'eFixture'))
                self.raw(400,{'message':'bad content'})
        with serve(Rejected) as (server,base):
            self.assertEqual(EmosClient('11_fixture',base).sign_once()['status'],'rejected')
            self.assertEqual(len([x for x in server.hits if x[0]=='PUT']),1)
        class ContextLimit(Fixture):
            def do_PUT(self):
                self.raw(429,{'message':'请求过于频繁，今日已签到请勿重复请求'})
        with serve(ContextLimit) as (_,base):
            self.assertEqual(EmosClient('11_fixture',base).sign_once()['status'],'rate')
        with serve() as (server,base):
            server.modes['eFixture']='already429'
            self.assertEqual(EmosClient('11_fixture',base).sign_once()['status'],'already')
        self.assertEqual(EmosClient('0d2f1a64').token,'0d2f1a64')


class OwnerProofTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='emos-owner-proof-')
        self.addCleanup(self.tmp.cleanup)
        self.app=fixture_app(self,self.tmp.name,'owner_proof_app',single_owner=True)
        self.app.init_db()
        self.addCleanup(self.app.close_idle_db)
        self.accounts=self.app.Store().accounts

    def test_valid_bearer_without_official_tg_binding_never_proves_person(self):
        self.accounts.begin_login(101, 'eA')
        for missing in ({}, {'telegram_user_id':None}, {'telegram_user_id':''}):
            with self.assertRaisesRegex(DomainError,'绑定'):
                self.accounts.finish_login(101,'11_synthetic_A',dict(user_id='eA',**missing))
        self.assertEqual(self.accounts.owned(101),[])
        self.assertTrue(self.accounts.login_pending(101))

    def test_unbound_official_callback_has_real_binding_guide_not_success(self):
        original = self.app.cfg_all
        self.app.cfg_all = lambda: dict(original(), tg_allowed_user_ids='101')
        with serve() as (server,base):
            server.identities['11_fixture_A']={'user_id':'eA','telegram_user_id':None,
                                              'telegram_bind_url':'https://t.me/emos_test_bot?start=bind_fixture'}
            configure(self.app,base_url=base)
            tg=FakeTG()
            instance=bot.Bot('123456:fixture_bot',self.app.Store(),tg=tg,synchronous=True)
            instance._accept_me(tg.get_me())
            self.addCleanup(instance.stop)
            def message(uid,text):
                instance._handle({'update_id':uid,'message':{'message_id':uid,'from':{'id':101},
                                  'chat':{'id':101,'type':'private'},'text':text}})
            message(1,'/login eA')
            message(2,'/start emosLinkAgree-11_fixture_A')
            self.assertEqual(self.accounts.owned(101),[])
            self.assertTrue(any('绑定' in text for chat,text,_ in tg.messages if chat==101))
            # 官网入口跟随 .env 的 EMOS_BASE_URL，不写死站点地址。
            links = [button.get('url') for _, _, keyboard in tg.messages
                     for row in keyboard or [] for button in row]
            self.assertTrue(any(url and url.rstrip('/').count('/') >= 2 and url.endswith('/')
                                for url in links), links)
            self.assertFalse(any('✅ 已登录' in text for _,text,_ in tg.messages))
            server.identities['11_fixture_A']['telegram_user_id']='101'
            message(3,'/login eA')
            message(4,'/start emosLinkAgree-11_fixture_A')
            self.assertEqual(self.accounts.active(101)['emos_id'],'eA')

    def test_official_primary_binding_change_prevents_put_and_disables_until_relogin(self):
        original = self.app.cfg_all
        self.app.cfg_all = lambda: dict(original(), tg_allowed_user_ids='101')
        with serve() as (server,base):
            self.accounts.begin_login(101, 'eA')
            self.accounts.finish_login(101,'11_fixture_A',{'user_id':'eA','telegram_user_id':101},base)
            server.identities['11_fixture_A']={'user_id':'eA','telegram_user_id':202}
            result=self.app.run_user(101)
            self.assertEqual([row['status'] for row in result],['invalid'])
            self.assertFalse(any(method=='PUT' for method,_,_ in server.hits))
            self.assertEqual(self.accounts.active(101)['enabled'],0)


if __name__ == '__main__':
    unittest.main()
