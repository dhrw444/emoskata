"""Client contract regression tests. Run: python3 -B -m unittest -v test_client.
Every request uses a localhost fixture or an injected transport; never official APIs.
"""
import io
import json
import urllib.error
import unittest
from unittest.mock import patch

from emos import BASES, EmosClient, EmosError, clamp_sign_time, pick_content, valid_base
from test_runtime import Fixture, serve


class ClientTests(unittest.TestCase):
    def test_time_clamp(self):
        for value, expected in [('00:00', '01:00'), ('0:5', '01:05'), ('08:00', '08:00'),
                                ('23:59', '23:59'), ('abc', '08:00'), ('99:99', '23:59')]:
            with self.subTest(value=value):
                self.assertEqual(clamp_sign_time(value), expected)

    def test_content_validation(self):
        self.assertEqual(pick_content(' 自定义 '), '自定义')
        self.assertTrue(pick_content())
        with self.assertRaises(EmosError):
            pick_content('x' * 11)

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

    def test_generic_429_rate_and_429_already_are_distinct(self):
        with serve() as (server, base):
            server.modes['eFixture'] = 'rate'
            result = EmosClient('11_fixture', base).sign_once()
            self.assertEqual((result['status'], result['retry_after']), ('rate', 600))
            server.modes['eFixture'] = 'already429'
            self.assertEqual(EmosClient('11_fixture', base).sign_once()['status'], 'already')

    def test_409_already_is_preserved(self):
        class Conflict(Fixture):
            def do_PUT(self):
                self.raw(409, {'message': 'already'})
        with serve(Conflict) as (_, base):
            self.assertEqual(EmosClient('11_fixture', base).sign_once()['status'], 'already')

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
                return len(calls) <= 2
            c.before_request = guard
            self.assertEqual(c.sign_once()['status'], 'revoked')
            self.assertEqual(server.signed, set())
            self.assertTrue(all(method != 'PUT' for method, _, _ in server.hits))

    def test_bad_profile_account_stops_before_sign_check(self):
        with serve() as (server, base):
            c = EmosClient('11_fixture', base)
            c.expected_user_id = 'eOther'
            self.assertEqual(c.sign_once()['status'], 'invalid')
            self.assertEqual(len(server.hits), 1)


if __name__ == '__main__':
    unittest.main()
