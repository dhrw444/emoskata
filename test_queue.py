"""Bot 与网页队列的去重、暂停、排空和停止回归。

仅使用临时目录、伪凭证和本地服务；不把替身成功视为生产授权或容器验收。
Runtime: Python 3.10+ / 标准库。
Test: python3 -B -m unittest test_queue
"""
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from test_support import fixture_app
from accounts import DomainError
from bot import _Jobs


class QueueTests(unittest.TestCase):
    def test_pausing_keeps_workers_drains_queued_jobs_and_resumes(self):
        pool=_Jobs(1,8)
        entered,release=threading.Event(),threading.Event()
        values=[]
        def first():
            entered.set()
            release.wait(2)
            values.append('first')
        self.assertTrue(pool.submit('one',first))
        self.assertTrue(entered.wait(1))
        self.assertTrue(pool.submit('two',lambda:values.append('second')))
        pool.pause()
        self.assertFalse(pool.submit('three',lambda:None))
        worker=pool.threads[0]
        release.set()
        self.assertTrue(pool.join(2))
        self.assertEqual(values,['first','second'])
        self.assertTrue(worker.is_alive())
        self.assertEqual(pool.keys,set())
        pool.resume()
        self.assertTrue(pool.submit('three',lambda:values.append('third')))
        self.assertTrue(pool.join(2))
        self.assertEqual(values,['first','second','third'])
        self.assertIs(pool.threads[0],worker)
        pool.stop()
        worker.join(2)
        self.assertFalse(worker.is_alive())

    def test_web_stop_between_dequeue_and_dispatch_prevents_run(self):
        with tempfile.TemporaryDirectory(prefix='emos-queue-stop-') as directory:
            app=fixture_app(self,directory,'queue_stop_fixture')
            app.init_db()
            self.addCleanup(app.close_idle_db)
            service=app.Store().accounts
            service.begin_login(101, 'eA')
            account=service.finish_login(101,'11_synthetic_A',{'user_id':'eA','telegram_user_id':101})
            web=app.personal_web()
            self.addCleanup(web.stop)
            entered,release=threading.Event(),threading.Event()
            original=web.queue.get_nowait
            calls=[]
            def dequeue():
                result=original()
                entered.set()
                release.wait(2)
                return result
            with patch.object(web.queue,'get_nowait',side_effect=dequeue), patch.object(web,'run_user',side_effect=lambda *a,**k:calls.append(a)):
                result=web.start_sign(101,{'account_id':account['id'],'request_id':'fixture_stop_queue_01'})
                self.assertTrue(entered.wait(1))
                web.stopping.set()
                release.set()
                web.queue.join()
                if web.worker:
                    web.worker.join(2)
                self.assertEqual(calls,[])
                self.assertEqual(web.jobs[result['job_id']]['status'],'failed')
                self.assertEqual(web.pending,{})

    def test_failed_job_request_is_idempotent_actor_scoped_and_expires(self):
        with tempfile.TemporaryDirectory(prefix='emos-idempotence-') as directory:
            app=fixture_app(self,directory,'idempotence_fixture')
            app.init_db()
            self.addCleanup(app.close_idle_db)
            service=app.Store().accounts
            accounts={}
            for actor in (101,202):
                service.begin_login(actor, 'e' + str(actor))
                accounts[actor]=service.finish_login(actor,'11_fake_'+str(actor),{'user_id':'e'+str(actor),'telegram_user_id':actor})
            web=app.personal_web()
            self.addCleanup(web.stop)
            key='fixture_idempotent_key_01'
            with patch.object(web,'run_user',side_effect=DomainError('fixture failure')) as action:
                one=web.start_sign(101,{'account_id':accounts[101]['id'],'request_id':key})
                web.queue.join()
                two=web.start_sign(101,{'account_id':accounts[101]['id'],'request_id':key})
                self.assertEqual(one,two)
                self.assertEqual(action.call_count,1)
                three=web.start_sign(202,{'account_id':accounts[202]['id'],'request_id':key})
                web.queue.join()
                self.assertNotEqual(one['job_id'],three['job_id'])
                self.assertEqual(action.call_count,2)
                with web.lock:
                    web.jobs[one['job_id']]['expires']=time.monotonic()-1
                four=web.start_sign(101,{'account_id':accounts[101]['id'],'request_id':key})
                web.queue.join()
                self.assertNotEqual(four['job_id'],one['job_id'])
                self.assertEqual(action.call_count,3)


if __name__ == '__main__':
    unittest.main()
