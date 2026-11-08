"""应用、账号、Bot 和客户端的本地 HTTP 集成回归。

夹具只使用临时或演练数据；本地通过不代表真实官方授权。
Test: python3 -B -m unittest test_runtime。"""
import contextlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest

import envconfig
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
import bot
from test_support import configure


class Fixture(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def raw(self, status, body, content_type='application/json', headers=None):
        data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def identity(self):
        token = (self.headers.get('Authorization') or '').removeprefix('Bearer ')
        return self.server.identities.get(token, {'user_id': 'eFixture', 'username': 'Fixture'})

    def do_GET(self):
        profile = self.identity()
        self.server.hits.append(('GET', self.path, profile['user_id']))
        mode = self.server.modes.get(profile['user_id'], '')
        if mode == 'cf':
            return self.raw(403, '<html>Cloudflare blocked</html>', 'text/html')
        if mode == 'html':
            return self.raw(200, '<html>Gateway login</html>', 'text/html')
        if mode == 'invalid':
            return self.raw(401, {'message': 'Unauthenticated.'})
        if self.path == '/api/user':
            result = dict(profile)
            if mode == 'empty_sign':
                result['sign'] = {}
            elif mode == 'no_sign':
                result.pop('sign', None)
            else:
                result['sign'] = ({'earn_point': 3, 'continuous_days': 2, 'sign_index': 9}
                                  if profile['user_id'] in self.server.signed else None)
            hook = self.server.check_hook
            if hook:
                hook(profile['user_id'])
            return self.raw(200, result)
        if self.path == '/api/sign/check':
            return self.raw(200, {'is_sign': True, 'user_id': profile['user_id']})
        return self.raw(404, {})

    def do_PUT(self):
        profile = self.identity()
        self.server.hits.append(('PUT', self.path.split('?')[0], profile['user_id']))
        self.server.puts.append((self.path, self.rfile.read(int(self.headers.get('Content-Length') or 0))))
        mode = self.server.modes.get(profile['user_id'], '')
        if mode == 'rate':
            return self.raw(429, {'message': 'Too Many Requests'}, headers={'Retry-After': '600'})
        if mode == 'already429':
            return self.raw(429, {'message': '重复签到'})
        if mode == 'custom':
            status, body = self.server.custom
            return self.raw(status, body)
        if mode == 'boom':
            return self.raw(500, {'message': 'Synthetic outage'})
        if mode == 'malformed':
            return self.raw(200, {'message': 'ok'})
        self.server.signed.add(profile['user_id'])
        return self.raw(200, {'earn_point': 3, 'continuous_days': 2, 'sign_index': 9})


@contextlib.contextmanager
def serve(handler=Fixture):
    server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
    server.identities, server.modes = {}, {}
    server.signed, server.hits, server.check_hook = set(), [], None
    server.puts = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, 'http://127.0.0.1:%d' % server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


class FakeTG:
    def __init__(self):
        self.messages, self.deleted = [], []
    def enabled(self):
        return True
    def get_me(self):
        return {'id': 777, 'is_bot': True, 'username': 'fixture_emos_bot'}
    def send(self, chat, text, keyboard=None):
        self.messages.append((chat, text, keyboard))
        return {'ok': True}
    def edit(self, chat, mid, text, keyboard=None):
        return self.send(chat, text, keyboard)
    def delete(self, chat, mid):
        self.deleted.append((chat, mid))
        return {'ok': True}
    def answer(self, *args):
        return {'ok': True}


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='emos-runtime-test-')
        self.addCleanup(self.temp.cleanup)
        from test_support import fixture_app, configure
        self.app = fixture_app(self, self.temp.name, 'runtime_fixture_app')
        self.app.init_db()
        configure(self.app, tg_bot_token='123456:fixture_bot')
        self.service = self.app.Store().accounts
        fast = patch.object(self.app._shutdown, 'wait', return_value=False)
        fast.start()
        self.addCleanup(fast.stop)
        self.hour = 9
        clock = patch.object(self.app, 'now_local', side_effect=lambda cfg:
                             datetime(2026, 11, 7, self.hour, 0, tzinfo=timezone.utc))
        clock.start()
        self.addCleanup(clock.stop)
        self.tokens = {101: '11_fixture_A', 202: '11_fixture_B', 303: '11_fixture_C'}

    def login(self, actor, official, base):
        self.service.begin_login(actor, official)
        return self.service.finish_login(actor, self.tokens[actor],
            {'user_id': official, 'username': official, 'telegram_user_id': actor}, base)

    def pair(self, base):
        a, b = self.login(101, 'eA', base), self.login(202, 'eB', base)
        code = self.service.issue_code(202)
        self.service.redeem(101, 'eB', code['code'])
        self.assertFalse(self.service.can_run(101, a['id'], b['id']))
        self.service.review_link(202, a['id'], b['id'], True, expected_root=b['id'])
        return a, b

    def seed_http(self, server):
        for actor, official in ((101, 'eA'), (202, 'eB'), (303, 'eC')):
            server.identities[self.tokens[actor]] = {'user_id': official, 'username': official,
                                                     'telegram_user_id': actor}

    def dispatch(self, instance, actor, text=None, data=None):
        self.serial = getattr(self, 'serial', 0) + 1
        message = {'from': {'id': actor}, 'chat': {'id': actor, 'type': 'private'},
                   'message_id': self.serial, 'text': text or 'menu'}
        update = {'update_id': self.serial}
        if data:
            update['callback_query'] = {'id': 'cb%d' % self.serial, 'from': {'id': actor},
                                        'message': message, 'data': data}
        else:
            update['message'] = message
        instance._handle(update)
        self.assertTrue(instance.join_jobs(5))

    def test_actual_bot_primary_then_tokens_sign_all_owner_accounts(self):
        original = self.app.cfg_all
        self.app.cfg_all = lambda: dict(original(), tg_allowed_user_ids='101')
        with serve() as (server, base):
            self.seed_http(server)
            server.identities[self.tokens[202]]['telegram_user_id'] = 101
            server.identities[self.tokens[303]]['telegram_user_id'] = 101
            configure(self.app, base_url=base)
            tg = FakeTG()
            instance = bot.Bot('fixture:bot', self.app.Store(), tg=tg, synchronous=True)
            instance._accept_me(tg.get_me())
            self.dispatch(instance, 101, '/login eA')
            self.assertEqual(tg.messages[-1][2][0][0]['url'],
                             'https://t.me/emospg_bot?start=link_eA-fixture_emos_bot')
            self.dispatch(instance, 101, '/start emosLinkAgree-' + self.tokens[101])
            self.assertIsNotNone(self.service.primary_account(101))
            self.dispatch(instance, 101, '/token ' + self.tokens[202])
            self.dispatch(instance, 101, '/token ' + self.tokens[303])
            self.assertEqual(len(self.service.owned(101)), 3)
            self.dispatch(instance, 101, '/sign')
            self.assertEqual(server.signed, {'eA', 'eB', 'eC'})
            before = list(server.hits)
            self.dispatch(instance, 202, '/sign')
            self.assertEqual(server.hits, before)
            visible = '\n'.join(text for _, text, _ in tg.messages)
            for token in self.tokens.values():
                self.assertNotIn(token, visible)
            self.assertTrue(tg.deleted)
            instance.stop()

    def test_new_bot_identity_does_not_inherit_another_bot_offset(self):
        self.app.set_setting('tg_update_offset', '999999')
        self.app.set_setting('tg_update_offset_777', '12')
        self.app.set_setting('tg_update_offset_778', '3')
        instance = bot.Bot('fixture:bot', self.app.Store(), tg=FakeTG(), synchronous=True)
        instance._accept_me({'id': 777, 'is_bot': True, 'username': 'fixture_emos_bot'})
        self.assertEqual(instance._offset, 12)
        instance._accept_me({'id': 778, 'is_bot': True, 'username': 'other_emos_bot'})
        self.assertEqual(instance._offset, 3)
        instance.stop()

    def test_only_direct_grants_not_reverse_or_transitive(self):
        with serve() as (server, base):
            self.seed_http(server)
            a, b = self.pair(base)
            c = self.login(303, 'eC', base)
            code = self.service.issue_code(303)
            self.service.redeem(202, 'eC', code['code'])
            self.service.review_link(303, b['id'], c['id'], True, expected_root=c['id'])
            rows = self.app.run_user(101)
            self.assertEqual([row['id'] for row in rows], [a['id'], b['id']])
            self.assertEqual(server.signed, {'eA', 'eB'})
            self.assertFalse(self.service.can_run(101, a['id'], c['id']))
            self.assertFalse(self.service.can_run(202, b['id'], a['id']))

    def test_revocation_during_account_gap_prevents_target_http(self):
        with serve() as (server, base):
            self.seed_http(server)
            a, b = self.pair(base)
            def revoked(seconds):
                self.service.unlink(202, a['id'], b['id'])
                return False
            with patch.object(self.app._shutdown, 'wait', side_effect=revoked):
                rows = self.app.run_user(101)
            self.assertEqual([row['id'] for row in rows], [a['id']])
            self.assertTrue(all(uid != 'eB' for _, _, uid in server.hits))

    def test_revocation_between_check_and_put_prevents_write(self):
        with serve() as (server, base):
            self.seed_http(server)
            a, b = self.pair(base)
            def revoke(uid):
                if uid == 'eB':
                    self.service.unlink(202, a['id'], b['id'])
            server.check_hook = revoke
            rows = self.app.run_user(101)
            self.assertNotIn('eB', server.signed)
            self.assertTrue(all(not (method == 'PUT' and uid == 'eB') for method, _, uid in server.hits))
            self.assertEqual([row['id'] for row in rows], [a['id']])

    def test_missed_minute_catches_up_without_second_tick_duplicates(self):
        with serve() as (server, base):
            self.seed_http(server)
            a = self.login(101, 'eA', base)
            self.hour = 10
            self.assertEqual(len(self.app.scheduler_tick()), 1)
            self.assertEqual(self.app.scheduler_tick(), [])
            self.assertEqual(len([hit for hit in server.hits if hit[0] == 'PUT']), 1)
            self.assertEqual(self.app._account_record(a['id'])['last_day'], '2026-11-07')

    def test_auto_sign_honours_each_account_sign_mode(self):
        with serve() as (server, base):
            self.seed_http(server)
            a = self.login(101, 'eA', base)
            self.service.set_account(101, expected_root=a['id'], sign_mode='random')
            self.hour = 10
            self.assertEqual(len(self.app.scheduler_tick()), 1)
            (path, body), = server.puts
            self.assertIn('content=', path)
            self.assertEqual(body, b'')

    def test_auto_sign_normal_mode_has_no_content_parameter(self):
        with serve() as (server, base):
            self.seed_http(server)
            self.login(101, 'eA', base)
            self.hour = 10
            self.assertEqual(len(self.app.scheduler_tick()), 1)
            self.assertEqual(server.puts, [('/api/user/sign', b'')])

    def test_scheduler_runs_all_owned_not_only_active_selection(self):
        with serve() as (server, base):
            self.seed_http(server)
            self.login(101, 'eA', base)
            self.service.begin_login(101, 'eA2')
            self.service.finish_login(101, '11_fixture_A2', {'user_id': 'eA2', 'telegram_user_id': 101}, base)
            server.identities['11_fixture_A2'] = {'user_id': 'eA2', 'username': 'Second', 'telegram_user_id': 101}
            self.assertEqual(len(self.app.scheduler_tick()), 2)
            self.assertEqual(server.signed, {'eA', 'eA2'})

    def test_failed_link_target_retries_after_source_succeeded(self):
        with serve() as (server, base):
            self.seed_http(server)
            a, b = self.pair(base)
            self.service.set_account(202, auto_sign=False)
            server.modes['eB'] = 'boom'
            rows = self.app.scheduler_tick()
            self.assertEqual([row['status'] for row in rows], ['signed', 'failed'])
            self.assertEqual(self.app.scheduler_tick(), [])
            self.assertGreater(self.app._account_record(b['id'])['next_run_at'], int(time.time()))
            with self.app.write_db() as conn:
                conn.execute('UPDATE accounts SET next_run_at=1 WHERE id=?', (b['id'],))
            server.modes.clear()
            rows = self.app.scheduler_tick()
            self.assertEqual([row['id'] for row in rows], [b['id']])
            self.assertEqual(server.signed, {'eA', 'eB'})

    def test_revoking_link_stops_retry_when_target_auto_disabled(self):
        with serve() as (server, base):
            self.seed_http(server)
            a, b = self.pair(base)
            self.service.set_account(202, auto_sign=False)
            server.modes['eB'] = 'boom'
            self.app.scheduler_tick()
            self.service.unlink(202, a['id'], b['id'])
            with self.app.write_db() as conn:
                conn.execute('UPDATE accounts SET next_run_at=1 WHERE id=?', (b['id'],))
            before = len(server.hits)
            self.assertEqual(self.app.scheduler_tick(), [])
            self.assertEqual(len(server.hits), before)

    def test_network_skipped_accounts_all_receive_retry_and_recover(self):
        a, b = self.pair('http://127.0.0.1:1')
        rows = self.app.scheduler_tick()
        self.assertEqual([row['status'] for row in rows], ['network', 'network'])
        for aid in (a['id'], b['id']):
            state = self.app._account_record(aid)
            self.assertEqual(state['retry_count'], 1)
            self.assertGreater(state['next_run_at'], int(time.time()))
            self.assertEqual(state['last_day'], '')
        self.assertEqual(self.app.scheduler_tick(), [])
        with serve() as (server, base):
            self.seed_http(server)
            with self.app.write_db() as conn:
                conn.execute('UPDATE accounts SET base_url=?,next_run_at=1', (base,))
            self.assertEqual([row['status'] for row in self.app.scheduler_tick()], ['signed', 'signed'])

    def test_retry_exhaustion_has_daily_cap_not_hot_loop(self):
        self.login(101, 'eA', 'http://127.0.0.1:1')
        for count in range(1, 5):
            rows = self.app.scheduler_tick()
            self.assertEqual(len(rows), 1)
            self.assertEqual(self.app._account_record(1)['retry_count'], count)
            with self.app.write_db() as conn:
                conn.execute('UPDATE accounts SET next_run_at=1')
        self.assertEqual(self.app.scheduler_tick(), [])
        with self.app.write_db() as conn:
            conn.execute("UPDATE accounts SET retry_day='2026-11-06'")
        self.assertEqual(len(self.app.scheduler_tick()), 1)
        self.assertEqual(self.app._account_record(1)['retry_count'], 1)

    def test_cf_html_does_not_disable_account_and_200_html_not_success(self):
        with serve() as (server, base):
            self.seed_http(server)
            a = self.login(101, 'eA', base)
            for mode in ('cf', 'html', 'malformed'):
                server.modes['eA'] = mode
                rows = self.app.run_user(101)
                self.assertNotIn(rows[0]['status'], ('signed', 'already', 'invalid'))
                self.assertEqual(self.app._account_record(a['id'])['enabled'], 1)
                self.assertEqual(self.app._account_record(a['id'])['last_day'], '')

    def test_429_limits_are_not_forged_success(self):
        with serve() as (server, base):
            self.seed_http(server)
            a = self.login(101, 'eA', base)
            server.modes['eA'] = 'rate'
            rows = self.app.run_user(101)
            self.assertEqual(rows[0]['status'], 'rate')
            self.assertGreaterEqual(self.app._account_record(a['id'])['next_run_at'], int(time.time()) + 599)
            server.modes['eA'] = 'already429'
            self.assertEqual(self.app.run_user(101)[0]['status'], 'already')

    def test_401_disables_and_relogin_restores_without_losing_history(self):
        with serve() as (server, base):
            self.seed_http(server)
            a = self.login(101, 'eA', base)
            server.modes['eA'] = 'invalid'
            self.assertEqual(self.app.run_user(101)[0]['status'], 'invalid')
            self.assertEqual(self.app._account_record(a['id'])['enabled'], 0)
            self.login(101, 'eA', base)
            self.assertEqual(self.app._account_record(a['id'])['enabled'], 1)
            self.assertEqual(len(self.service.logs(101)), 1)

    def test_wrong_profile_account_cannot_sign_or_create_false_result(self):
        with serve() as (server, base):
            self.seed_http(server)
            self.login(101, 'eWrong', base)
            rows = self.app.run_user(101)
            self.assertEqual(rows[0]['status'], 'invalid')
            self.assertEqual(server.signed, set())

    def test_queued_notification_rechecks_revoked_recipient(self):
        a, b = self.pair('http://127.0.0.1:1')
        row = {'id': b['id'], 'emos_id': 'eB', 'name': 'B', 'status': 'signed', 'message': 'ok', 'carrot': 3}
        self.app._queue_result(row, self.app.cfg_all())
        tasks = []
        while not self.app._notifications.empty():
            tasks.append(self.app._notifications.get_nowait())
        self.service.unlink(202, a['id'], b['id'])
        sent = []
        with patch.object(bot.TG, 'send', side_effect=lambda chat, text: sent.append(chat)):
            outcomes = [self.app._deliver_notification(task) for task in tasks]
        self.assertEqual(sent, [202])
        self.assertEqual(sorted(outcomes), [False, True])

    def test_notification_preferences_are_per_user_and_not_transitive(self):
        a, b = self.pair('http://127.0.0.1:1')
        c = self.login(303, 'eC', 'http://127.0.0.1:1')
        issued = self.service.issue_code(303)
        self.service.redeem(202, 'eC', issued['code'])
        self.service.review_link(303, b['id'], c['id'], True, expected_root=c['id'])
        self.service.set_preferences(202, notify_success=False)
        self.app._queue_result({'id': c['id'], 'emos_id': 'eC', 'name': 'C', 'status': 'signed',
                                'message': 'ok', 'carrot': 1}, self.app.cfg_all())
        task = self.app._notifications.get_nowait()
        self.assertEqual(task[1], 303)
        self.assertTrue(self.app._notifications.empty())

    def test_write_transaction_rolls_back_on_error(self):
        with self.assertRaises(RuntimeError):
            with self.app.write_db() as conn:
                conn.execute("INSERT INTO settings VALUES('must_rollback','x')")
                raise RuntimeError('fixture abort')
        self.assertIsNone(self.app.get_setting('must_rollback'))

    def test_new_env_file_password_invalidates_old_cookie(self):
        self.app.init_password()
        session = self.app.create_session('127.0.0.1', 'fixture')
        self.assertTrue(self.app.session_ok(session))
        configure(self.app, panel_password='fixture_password_rotated')
        self.app.init_password()
        self.assertFalse(self.app.verify_password('fixture_password'))
        self.assertTrue(self.app.verify_password('fixture_password_rotated'))
        self.assertFalse(self.app.session_ok(session))

    def test_cancel_during_http_login_prevents_account_save(self):
        entered, release = threading.Event(), threading.Event()
        configure(self.app, base_url='https://example.invalid')
        original = self.app.cfg_all
        self.app.cfg_all = lambda: dict(original(), tg_allowed_user_ids='101')
        tg = FakeTG()
        class WaitingClient:
            def __init__(self, *args):
                pass
            def profile(self):
                entered.set()
                release.wait(3)
                return {'user_id': 'eCancelled', 'telegram_user_id': 101}
        instance = bot.Bot('fixture:bot', self.app.Store(), tg=tg, client_factory=WaitingClient,
                           synchronous=False, workers=2)
        instance._accept_me(tg.get_me())
        try:
            self.dispatch(instance, 101, '/login eCancelled')
            instance._handle({'update_id': 2, 'message': {'message_id': 2,
                             'from': {'id': 101}, 'chat': {'id': 101, 'type': 'private'},
                             'text': '/start emosLinkAgree-11_fixture_pending'}})
            self.assertTrue(entered.wait(1))
            instance._handle({'update_id': 3, 'message': {'message_id': 3,
                             'from': {'id': 101}, 'chat': {'id': 101, 'type': 'private'},
                             'text': '/cancel'}})
            self.assertFalse(self.service.login_pending(101))
            release.set()
            self.assertTrue(instance.join_jobs(5))
            self.assertEqual(self.service.owned(101), [])
        finally:
            release.set()
            instance.stop(wait=True, timeout=5)

    def test_bad_gap_configuration_does_not_keep_global_lock(self):
        self.login(101, 'eA', 'http://127.0.0.1:1')
        bad = dict(self.app.cfg_all(), gap_seconds='not-an-integer')
        with self.assertRaises(ValueError):
            self.app._run_ids(bad, [1])
        self.assertFalse(self.app._run_lock.locked())

    def test_expected_root_blocks_accidental_switch_before_dispatch(self):
        with serve() as (server, base):
            self.seed_http(server)
            a = self.login(101, 'eA', base)
            self.service.begin_login(101, 'eSecond')
            self.service.finish_login(101, '11_second_root', {'user_id': 'eSecond', 'telegram_user_id': 101}, base)
            with self.assertRaises(self.app.DomainError):
                self.app.run_user(101, expected_root=a['id'])
            self.assertEqual(server.hits, [])

    def test_logs_are_capped_per_account(self):
        with serve() as (server, base):
            self.seed_http(server)
            a = self.login(101, 'eA', base)
            cfg = self.app.cfg_all()
            account = self.app._account_record(a['id'])
            for index in range(self.app.LOG_KEEP + 5):
                self.app._store_result(account, {'status': 'failed', 'message': 'x%d' % index,
                                                 'point': None}, cfg, 'auto')
            with self.app.db() as conn:
                count = conn.execute('SELECT COUNT(*) c FROM logs WHERE account_id=?',
                                     (a['id'],)).fetchone()['c']
            self.assertEqual(count, self.app.LOG_KEEP)

    def test_timezone_last_day_is_not_host_clock(self):
        os.environ['TZ'] = 'UTC'
        if hasattr(time, 'tzset'):
            time.tzset()
        with serve() as (server, base):
            self.seed_http(server)
            a = self.login(101, 'eA', base)
            with patch.object(self.app, 'now_local', return_value=datetime(2026, 11, 8, 1, 0, tzinfo=timezone.utc)):
                self.app.run_user(101)
            self.assertEqual(self.app._account_record(a['id'])['last_day'], '2026-11-08')

    def test_duplicate_official_id_does_not_block_startup(self):
        # 老库里有重复 emos_id 时，启动必须能继续：保留最早一行，其余置空。
        with self.app.write_db() as conn:
            conn.execute('DROP INDEX ux_accounts_emos_id')
            conn.execute("INSERT INTO accounts(name,token,emos_id) VALUES('Old A','11_legacyA','dup')")
            conn.execute("INSERT INTO accounts(name,token,emos_id) VALUES('Old B','11_legacyB','dup')")
        self.app.init_db()
        with self.app.db() as conn:
            rows = conn.execute('SELECT id,emos_id,token FROM accounts ORDER BY id').fetchall()
            self.assertEqual([row['emos_id'] for row in rows], ['dup', None])
            self.assertEqual([row['token'] for row in rows], ['11_legacyA', '11_legacyB'])
            self.assertEqual(conn.execute(
                'SELECT COUNT(*) FROM accounts WHERE owner_tg_id IS NOT NULL').fetchone()[0], 0)
            self.assertIn('ux_accounts_emos_id', [row[1] for row in
                                                  conn.execute('PRAGMA index_list(accounts)')])

    def test_first_boot_inherits_explicit_env_values(self):
        # 首次建配置必须继承显式环境值，此后不再作为覆盖来源。
        from envconfig import _seed_from_environ, template_values
        seeded = _seed_from_environ({'TG_ALLOWED_USER_IDS': '101',
                                     'PORT': '9000', 'NOT_A_KEY': 'x'})
        self.assertEqual(seeded, {'TG_ALLOWED_USER_IDS': '101', 'PORT': '9000'})
        values = template_values(**seeded)
        self.assertEqual(values['TG_ALLOWED_USER_IDS'], '101')
        self.assertEqual(values['PORT'], '9000')
        # 空值不等于显式配置，不能被继承成空串以外的东西
        self.assertNotIn('TG_BOT_TOKEN', seeded)
        with self.assertRaises(envconfig.EnvConfigError):
            template_values(NOT_A_KEY='x')

    def test_allowlist_config_rejects_malformed_ids(self):
        # 单个 owner 的非法配置在保存时拒绝。
        from envconfig import SCHEMA, validate_env
        base = {key: field.default for key, field in SCHEMA.items()}
        base['PANEL_PASSWORD'] = 'abcdefgh'
        for bad in ('0', '-5', 'abc', '101;202', '101,202', '101 202', '9999999999999999999999'):
            with self.assertRaises(envconfig.EnvConfigError, msg=bad):
                validate_env(dict(base, TG_ALLOWED_USER_IDS=bad))
        ok = validate_env(dict(base, TG_ALLOWED_USER_IDS='101'))
        self.assertEqual(ok['TG_ALLOWED_USER_IDS'], '101')

    def test_every_env_setting_is_reachable_from_the_web_panel(self):
        # 配置 schema 与网页映射逐项对照，防止遗漏新增字段。
        from envconfig import CFG_TO_ENV, SCHEMA
        mapped_env = set(CFG_TO_ENV.values())
        unknown = sorted(env for env in mapped_env if env not in SCHEMA)
        self.assertEqual(unknown, [], 'CFG_TO_ENV 指向了 SCHEMA 里不存在的环境变量')
        self.assertIn('TG_ALLOWED_USER_IDS', mapped_env)
        for env in mapped_env:
            self.assertIn(env, SCHEMA)

    def test_legacy_settings_table_moves_into_env_file(self):
        # 模拟旧 settings 配置迁移到 .env。
        # 当前值为默认值时才满足此迁移条件。
        self.app.CONFIG.update({}, clear_secrets=['TG_BOT_TOKEN'])
        configure(self.app, sign_time='08:00', gap_seconds='8')
        self.app.set_setting('tg_bot_token', '123456789:legacyfixture')
        self.app.set_setting('sign_time', '07:30')
        self.app.set_setting('gap_seconds', '12')
        state = self.app.import_legacy_config()
        for key in ('TG_BOT_TOKEN', 'SIGN_TIME', 'GAP_SECONDS'):
            self.assertIn(key, state['imported'])
        env, _ = self.app.CONFIG.snapshot()
        self.assertEqual(env['TG_BOT_TOKEN'], '123456789:legacyfixture')
        self.assertEqual(env['SIGN_TIME'], '07:30')
        self.assertEqual(env['GAP_SECONDS'], '12')
        # 迁移只能跑一次：之后再跑不应改动 .env
        before = self.app.CONFIG.snapshot()[1]
        again = self.app.import_legacy_config()
        self.assertEqual((again['imported'], again['rejected']), ([], []))
        self.assertEqual(self.app.CONFIG.snapshot()[1], before)

    def test_legacy_import_never_overrides_values_already_in_env(self):
        # 本用例填入非默认值，验证旧 settings 不覆盖该值。
        configure(self.app, tg_bot_token='999999:fromenvfile')
        self.app.set_setting('tg_bot_token', '123456789:stalelegacy')
        state = self.app.import_legacy_config()
        self.assertNotIn('TG_BOT_TOKEN', state['imported'])
        self.assertIn('TG_BOT_TOKEN', state['untouched'])
        self.assertEqual(self.app.CONFIG.snapshot()[0]['TG_BOT_TOKEN'], '999999:fromenvfile')

    def test_legacy_import_skips_invalid_value_without_blocking_startup(self):
        # 旧库里可能是旧格式的 Token。写不进 .env 就明确拒绝，不能让启动失败。
        self.app.CONFIG.update({}, clear_secrets=['TG_BOT_TOKEN'])
        self.app.set_setting('tg_bot_token', 'not-a-valid-token')
        state = self.app.import_legacy_config()
        self.assertIn('TG_BOT_TOKEN', state['rejected'])
        self.assertEqual(self.app.cfg_all()['tg_bot_token'],
                         self.app.CONFIG.snapshot()[0]['TG_BOT_TOKEN'])


if __name__ == '__main__':
    unittest.main()
