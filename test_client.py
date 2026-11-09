"""HTTP JSON 契约、身份、余额与非幂等请求回归。

仅用隔离目录、合成身份或 localhost；正式授权和真实生产资源不在此验收。
Test: python3 -B -m unittest test_client"""
import unittest
from unittest.mock import patch

from emos import BASES, CONTENTS, EmosClient, EmosError, clamp_sign_time, pick_content, valid_base
from test_runtime import Fixture, serve


class ClientTests(unittest.TestCase):
    def test_time_clamp(self):
        for value, expected in [('00:00', '01:00'), ('0:5', '01:05'), ('08:00', '08:00'),
                                ('23:59', '23:59'), ('abc', '08:00'), ('99:99', '23:59')]:
            with self.subTest(value=value):
                self.assertEqual(clamp_sign_time(value), expected)

    def test_content_validation(self):
        self.assertEqual(pick_content(' 自定义 '), '自定义')
        self.assertEqual(pick_content(), '')
        self.assertTrue(pick_content(mode='random'))
        self.assertEqual(pick_content('x' * 11), 'x' * 11)
        with self.assertRaises(EmosError):
            pick_content('x' * 257)

    def test_custom_endpoint_never_appends_public_hosts(self):
        self.assertEqual(EmosClient('11_fixture', 'https://custom.example').bases,
                         ['https://custom.example'])
        self.assertEqual(EmosClient('11_fixture').bases, list(BASES))
        self.assertEqual(EmosClient('11_fixture', BASES[0]).bases, list(BASES))

    def test_invalid_endpoints_and_tokens_fail_before_network(self):
        for endpoint in ('emos.best', 'file:///tmp/data', 'https://user:pw@host',
                         'https://host/?key=secret', 'http://'):
            with self.subTest(endpoint=endpoint), self.assertRaises(EmosError):
                valid_base(endpoint)
        for token in ('', 'x', '11_\nabc', '11_中文'):
            with self.subTest(token=token), self.assertRaises(EmosError):
                EmosClient(token)

    def test_real_success_shape_and_existing_sign_skip(self):
        with serve() as (server, base):
            c = EmosClient('11_fixture', base)
            result = c.sign_once()
            self.assertEqual((result['status'], result['point'], result['days'], result['rank']),
                             ('signed', 3, 2, 9))
            server.hits.clear()
            self.assertEqual(c.sign_once()['status'], 'already')
            self.assertTrue(all(method != 'PUT' for method, _, _ in server.hits))

    def test_flat_and_wrapped_profiles_both_require_official_id(self):
        class Wrapped(Fixture):
            def do_GET(self):
                self.raw(200, {'data': {'user_id': 'eWrapped', 'username': 'Wrapped'}})
        with serve(Wrapped) as (_, base):
            self.assertEqual(EmosClient('11_fixture', base).profile()['_user_id'], 'eWrapped')
        with serve() as (_, base):
            self.assertEqual(EmosClient('11_fixture', base).profile()['_user_id'], 'eFixture')
        class Missing(Fixture):
            def do_GET(self):
                self.raw(200, {'username': 'Not a proof'})
        with serve(Missing) as (_, base), self.assertRaises(EmosError):
            EmosClient('11_fixture', base).profile()

    def test_cf_html_403_and_html200_do_not_claim_invalid_or_success(self):
        with serve() as (server, base):
            for mode in ('cf', 'html'):
                server.modes['eFixture'] = mode
                with self.subTest(mode=mode):
                    self.assertEqual(EmosClient('11_fixture', base).sign_once()['status'], 'blocked')

    def test_401_invalid_is_distinct_from_forbidden(self):
        with serve() as (server, base):
            server.modes['eFixture'] = 'invalid'
            self.assertEqual(EmosClient('11_fixture', base).sign_once()['status'], 'invalid')
        class Forbidden(Fixture):
            def do_GET(self):
                self.raw(403, {'message': 'Forbidden'})
        with serve(Forbidden) as (_, base):
            self.assertEqual(EmosClient('11_fixture', base).sign_once()['status'], 'blocked')

    def test_empty_sign_object_is_not_treated_as_signed(self):
        # sign={} 不作为已签到证据；当前策略继续提交，重复签到由官方响应归一。
        with serve() as (server, base):
            server.modes['eFixture'] = 'empty_sign'
            result = EmosClient('11_fixture', base).sign_once()
            self.assertEqual(result['status'], 'signed')
            self.assertEqual(len([hit for hit in server.hits if hit[0] == 'PUT']), 1)

    def test_sign_object_with_content_skips_put(self):
        with serve() as (server, base):
            server.signed.add('eFixture')
            result = EmosClient('11_fixture', base).sign_once()
            self.assertEqual(result['status'], 'already')
            self.assertEqual([hit for hit in server.hits if hit[0] == 'PUT'], [])

    def test_sign_is_never_resent_to_the_failover_host(self):
        # 签到不是幂等请求：网络中断时请求可能已经到达官方并签到成功，
        # 换域名重发就是二次签到尝试（官方明确禁止接口轮询）。
        attempts = []
        def fake_once(method, path, endpoint, params=None):
            attempts.append((method, path, endpoint))
            raise EmosError('网络连接失败，稍后自动重试。', network=True)
        client = EmosClient('11_fixture', BASES[0])
        with patch.object(client, '_once', side_effect=fake_once), \
             patch.object(client, 'profile', return_value={'_user_id': 'eX', '_display': 'x',
                                                          'sign': None, 'telegram_user_id': None}):
            self.assertEqual(client.sign_once()['status'], 'network')
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0][2], BASES[0])

    def test_gateway_side_effect_never_resends_sign_to_failover(self):
        for status, body, content_type in (
            (403, {'message': 'Forbidden'}, 'application/json'),
            (403, '<html>gateway denied after write</html>', 'text/html'),
            (200, '<html>gateway rewrote upstream reply</html>', 'text/html'),
        ):
            writes = []
            class AppliedThenBlocked(Fixture):
                def do_PUT(self):
                    self.server.signed.add('eFixture')
                    writes.append('primary-applied')
                    self.raw(status, body, content_type)
            class Failover(Fixture):
                def do_PUT(self):
                    writes.append('backup-applied')
                    super().do_PUT()
            with self.subTest(status=status, content_type=content_type):
                with serve(AppliedThenBlocked) as (primary, first), serve(Failover) as (_, backup):
                    client = EmosClient('11_fixture', first)
                    client.bases = [first, backup]
                    result = client.sign_once()
                    self.assertEqual(writes, ['primary-applied'], (writes, result))
                    self.assertEqual(primary.signed, {'eFixture'})
                    self.assertEqual(result['status'], 'blocked')

    def test_mutating_methods_do_not_fail_over_on_blocked_result(self):
        for method in ('PUT', 'POST', 'DELETE'):
            attempts = []
            client = EmosClient('11_fixture', BASES[0])
            def blocked(method, path, endpoint, params=None):
                attempts.append(endpoint)
                raise EmosError('synthetic blocked after write', kind='blocked')
            with self.subTest(method=method), patch.object(client, '_once', side_effect=blocked):
                with self.assertRaises(EmosError):
                    client.call(method, '/api/user/sign')
                self.assertEqual(attempts, [BASES[0]])

    def test_get_profile_still_fails_over(self):
        attempts = []
        def fake_once(method, path, endpoint, params=None):
            attempts.append(endpoint)
            if endpoint == BASES[0]:
                raise EmosError('网络连接失败，稍后自动重试。', network=True)
            return 200, {'user_id': 'eX'}
        client = EmosClient('11_fixture', BASES[0])
        with patch.object(client, '_once', side_effect=fake_once):
            self.assertEqual(client.profile()['_user_id'], 'eX')
        self.assertEqual(attempts, [BASES[0], BASES[1]])

    def test_generic_429_rate_and_429_already_are_distinct(self):
        # 官方样例是 429 + {"message":"重复签到"}；带句号/空白/近义写法也必须判成已签，
        # 空 message 仍按限流处理。
        for message, expected in [('重复签到', 'already'), ('重复签到。', 'already'),
                                  ('重复签到 ', 'already'), ('重复签到\u3000', 'already'),
                                  ('您今天已经签到过了', 'already'), ('', 'rate'),
                                  ('Too Many Requests', 'rate')]:
            with self.subTest(message=message), serve() as (server, base):
                server.modes['eFixture'] = 'custom'
                server.custom = (429, {'message': message})
                self.assertEqual(EmosClient('11_fixture', base).sign_once()['status'], expected)
        with serve() as (server, base):
            server.modes['eFixture'] = 'rate'
            result = EmosClient('11_fixture', base).sign_once()
            self.assertEqual((result['status'], result['retry_after']), ('rate', 600))
            server.modes['eFixture'] = 'already429'
            self.assertEqual(EmosClient('11_fixture', base).sign_once()['status'], 'already')

    def test_409_without_explicit_daily_sign_proof_is_not_success(self):
        class Conflict(Fixture):
            def do_PUT(self):
                self.raw(409, {'message': 'conflicting request'})
        with serve(Conflict) as (_, base):
            self.assertEqual(EmosClient('11_fixture', base).sign_once()['status'], 'failed')

    def test_200_already_message_is_not_counted_as_reward(self):
        class Already(Fixture):
            def do_PUT(self):
                self.raw(200, {'message': '您今天已经签到过了'})
        with serve(Already) as (_, base):
            result = EmosClient('11_fixture', base).sign_once()
            self.assertEqual((result['status'], result['point']), ('already', None))

    def test_malformed_success_and_string_boolean_are_rejected(self):
        with serve() as (server, base):
            server.modes['eFixture'] = 'malformed'
            self.assertEqual(EmosClient('11_fixture', base).sign_once()['status'], 'failed')
        class WrongBool(Fixture):
            def do_GET(self):
                self.raw(200, {'is_sign': 'false', 'user_id': 'eFixture'})
        with serve(WrongBool) as (_, base), self.assertRaises(EmosError):
            EmosClient('11_fixture', base).check()

    def test_put_network_exception_stays_classified(self):
        with serve() as (_, base):
            c = EmosClient('11_fixture', base)
            original = c._once
            def failing(method, path, endpoint, params=None):
                if method == 'PUT':
                    raise EmosError('Synthetic outage', network=True)
                return original(method, path, endpoint, params)
            with patch.object(c, '_once', side_effect=failing):
                self.assertEqual(c.sign_once()['status'], 'network')

    def test_official_failover_prefers_reachable_host_and_custom_does_not(self):
        attempts = []
        def failing(method, path, endpoint, params=None):
            attempts.append(endpoint)
            if endpoint == BASES[0] or endpoint == 'https://custom.example':
                raise EmosError('Synthetic blocked', kind='blocked')
            return 200, {'user_id': 'eBackup'}
        c = EmosClient('11_fixture', BASES[0])
        with patch.object(c, '_once', side_effect=failing):
            self.assertEqual(c.profile()['_user_id'], 'eBackup')
            c.profile()
        self.assertEqual(attempts, [BASES[0], BASES[1], BASES[1]])
        custom = EmosClient('11_fixture', 'https://custom.example')
        attempts.clear()
        with patch.object(custom, '_once', side_effect=failing), self.assertRaises(EmosError):
            custom.profile()
        self.assertEqual(attempts, ['https://custom.example'])

    def test_redirect_never_sends_bearer_to_other_origin(self):
        seen = []
        class Sink(Fixture):
            def do_GET(self):
                seen.append(self.headers.get('Authorization'))
                self.raw(200, {'user_id': 'eSink'})
        with serve(Sink) as (_, sink):
            class Redirect(Fixture):
                def do_GET(self):
                    self.raw(302, {}, headers={'Location': sink + '/api/user'})
            with serve(Redirect) as (_, base):
                self.assertEqual(EmosClient('11_fixture', base).sign_once()['status'], 'blocked')
        self.assertEqual(seen, [])

    def test_auth_guard_can_revoke_between_requests(self):
        with serve() as (server, base):
            c = EmosClient('11_fixture', base)
            calls = []
            def guard():
                calls.append(1)
                return len(calls) <= 1
            c.before_request = guard
            self.assertEqual(c.sign_once()['status'], 'revoked')
            self.assertEqual(server.signed, set())
            self.assertTrue(all(method != 'PUT' for method, _, _ in server.hits))

    def test_login_is_sign_true_does_not_skip_daily_sign_in(self):
        with serve() as (server, base):
            c = EmosClient('11_fixture', base)
            logged_in, body = c.check()
            self.assertTrue(logged_in)
            self.assertIsNone(c.profile()['sign'])
            result = c.sign_once()
            self.assertEqual(result['status'], 'signed')
            self.assertEqual(server.signed, {'eFixture'})
            self.assertEqual([path for method, path, _ in server.hits if method == 'PUT'], ['/api/user/sign'])

    def test_official_profile_sign_record_skips_without_extra_requests(self):
        with serve() as (server, base):
            server.signed.add('eFixture')
            result = EmosClient('11_fixture', base).sign_once()
            self.assertEqual(result['status'], 'already')
            self.assertEqual(server.hits, [('GET', '/api/user', 'eFixture')])

    def test_missing_profile_daily_sign_field_fails_closed(self):
        class MissingSign(Fixture):
            def do_GET(self):
                self.raw(200, {'user_id': 'eFixture', 'username': 'fixture'})
        with serve(MissingSign) as (_, base):
            self.assertEqual(EmosClient('11_fixture', base).sign_once()['status'], 'failed')

    def test_bad_profile_account_stops_before_sign_check(self):
        with serve() as (server, base):
            c = EmosClient('11_fixture', base)
            c.expected_user_id = 'eOther'
            self.assertEqual(c.sign_once()['status'], 'invalid')
            self.assertEqual(len(server.hits), 1)

    def test_normal_mode_sends_no_content_parameter(self):
        with serve() as (server, base):
            self.assertEqual(EmosClient('11_fixture', base).sign_once()['status'], 'signed')
            self.assertEqual(server.puts, [('/api/user/sign', b'')])

    def test_random_mode_sends_content_from_whitelist(self):
        with serve() as (server, base):
            result = EmosClient('11_fixture', base).sign_once(mode='random')
            self.assertEqual(result['status'], 'signed')
            (path, body), = server.puts
            self.assertTrue(path.startswith('/api/user/sign?content='), path)
            from urllib.parse import parse_qs, urlsplit
            value = parse_qs(urlsplit(path).query)['content'][0]
            self.assertIn(value, CONTENTS)
            self.assertEqual(body, b'')

    def test_explicit_content_is_sent_verbatim_in_query(self):
        with serve() as (server, base):
            EmosClient('11_fixture', base).sign_once(' 今天也在 ')
            (path, _), = server.puts
            from urllib.parse import parse_qs, urlsplit
            self.assertEqual(parse_qs(urlsplit(path).query)['content'][0], '今天也在')

    def test_bad_mode_and_control_characters_fail_before_any_request(self):
        with serve() as (server, base):
            for mode in ('random2', ''):
                with self.subTest(mode=mode):
                    self.assertEqual(EmosClient('11_fixture', base).sign_once(mode=mode)['status'],
                                     'config')
            self.assertEqual(EmosClient('11_fixture', base).sign_once('a\nb')['status'], 'config')
            self.assertEqual(server.hits, [])


if __name__ == '__main__':
    unittest.main()
