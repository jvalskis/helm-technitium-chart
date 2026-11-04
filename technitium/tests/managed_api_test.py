# MIT License
# Copyright (c) 2026 Shane & Contributors

import http.client
import io
import json
from pathlib import Path
import runpy
import sys
import unittest
from unittest import mock
import urllib.error
import urllib.parse

FILES = Path(__file__).resolve().parents[1] / 'files'
sys.path.insert(0, str(FILES))
import managed_api as api
import managed_scripts_test as fixtures


def response(value):
    return io.BytesIO(json.dumps(value).encode())


class ClientTests(unittest.TestCase):
    def test_transport_failures_are_normalized(self):
        for error in (http.client.IncompleteRead(b'{', 20), http.client.RemoteDisconnected(),
                      ConnectionResetError(), TimeoutError(), urllib.error.URLError('offline')):
            with self.subTest(error=type(error).__name__), mock.patch.object(api, 'open_request', side_effect=error):
                with self.assertRaises(api.TransportError):
                    api.request_json('https://synthetic.test/')

    def test_https_failure_never_posts_password_over_http(self):
        client = api.TechnitiumClient('10.0.0.1', 53443, 'test', 'synthetic')
        with mock.patch.object(api, 'open_request', side_effect=urllib.error.URLError('offline')) as send:
            with self.assertRaises(api.TransportError):
                client.state(api.Deadline(10))
        send.assert_called_once()
        self.assertTrue(send.call_args.args[0].full_url.startswith('https://'))

    def test_login_rejection_is_permanent_and_does_not_retry(self):
        client = api.TechnitiumClient('10.0.0.1', 53443, 'test', 'synthetic')
        with mock.patch.object(api, 'open_request', return_value=response({'status': 'error'})) as send:
            deadline = api.Deadline(900)
            with self.assertRaises(api.AuthenticationError):
                deadline.retry(lambda: client.state(deadline))
        send.assert_called_once()

    def test_sessions_are_reused_and_refreshed_only_after_expiry(self):
        client = api.TechnitiumClient('10.0.0.1', 53443, 'test', 'synthetic')
        data = {'clusterInitialized': False, 'dnsServerDomain': 'node'}
        with mock.patch.object(api, 'open_request', side_effect=[
            response({'status': 'ok', 'token': 'first'}),
            response({'status': 'ok', 'response': data}),
            response({'status': 'invalid-token'}),
            response({'status': 'ok', 'token': 'second'}),
            response({'status': 'ok', 'response': data}),
            response({'status': 'ok', 'response': data}),
        ]) as send:
            for _ in range(3):
                self.assertFalse(client.state(api.Deadline(10)).initialized)
        self.assertEqual(send.call_count, 6)
        self.assertEqual(sum('/api/user/login' in c.args[0].full_url for c in send.call_args_list), 2)

    def test_dedicated_token_never_uses_password_login(self):
        client = api.TechnitiumClient('10.0.0.1', 53443, 'test', token='synthetic-token')
        with mock.patch.object(api, 'open_request', return_value=response({'status': 'invalid-token'})) as send:
            with self.assertRaises(api.AuthenticationError):
                client.state(api.Deadline(10))
        send.assert_called_once()
        self.assertEqual(send.call_args.args[0].get_header('Authorization'), 'Bearer synthetic-token')
        self.assertNotIn('/login', send.call_args.args[0].full_url)

    def test_forbidden_request_does_not_create_another_session(self):
        client = api.TechnitiumClient('10.0.0.1', 53443, 'test', 'synthetic')
        client.token = 'cached-session'
        with mock.patch.object(api, 'open_request', side_effect=urllib.error.HTTPError('url', 403, 'denied', None, None)) as send:
            with self.assertRaises(api.AuthenticationError):
                client.state(api.Deadline(10))
        send.assert_called_once()

    def test_redirects_are_rejected_before_forwarding_credentials(self):
        request = urllib.request.Request('https://synthetic.test/', headers={'Authorization': 'Bearer synthetic'})
        with self.assertRaises(api.ApiError):
            api.NoRedirect().redirect_request(request, None, 302, '', {}, 'http://synthetic.test/')

    def test_one_deadline_bounds_retries(self):
        now = [0.0]
        def sleep(seconds):
            now[0] += seconds
        with mock.patch.object(api.time, 'monotonic', side_effect=lambda: now[0]), \
             mock.patch.object(api.time, 'sleep', side_effect=sleep):
            deadline = api.Deadline(10)
            operation = mock.Mock(side_effect=api.TransportError('offline'))
            with self.assertRaises(api.DeadlineExceeded):
                deadline.retry(operation)
        self.assertEqual(now[0], 10)
        self.assertEqual(operation.call_count, 4)

    def test_scripts_are_safe_to_import_without_environment_or_network(self):
        with mock.patch.dict(api.os.environ, {}, clear=True), \
             mock.patch.object(api, 'open_request', side_effect=AssertionError('unexpected network')):
            for name in ('managed-guard.py', 'managed-cluster.py', 'managed-primary-controller.py'):
                self.assertIn('main', runpy.run_path(str(FILES / name)))


class HookRegressions(unittest.TestCase):
    def run_hook(self, fake, extra=None):
        fixtures.ManagedScriptsTests().run_script('managed-cluster.py', fake, extra)

    def test_totp_is_sent_to_login_and_join(self):
        fake = fixtures.FakeApis(count=1, initialized=True)
        fake.expected_totp = '123456'
        self.run_hook(fake, {'PRIMARY_TOTP': '123456'})
        self.assertTrue(fake.login_forms)
        self.assertTrue(all(form['totp'] == ['123456'] for form in fake.login_forms))
        self.assertEqual(fake.ready, [True, True])

    def test_join_certificate_validation_follows_explicit_tls_setting(self):
        for setting, expected in (({}, 'false'), ({'VERIFY_TLS': 'false'}, 'true')):
            fake = fixtures.FakeApis(count=1, initialized=True)
            self.run_hook(fake, setting)
            self.assertEqual(fake.join_forms[0]['ignoreCertificateErrors'], [expected])

    def test_lost_join_responses_inspect_state_instead_of_repeating_mutation(self):
        for error in (http.client.RemoteDisconnected(), http.client.IncompleteRead(b'{', 20)):
            fake = fixtures.FakeApis(count=1, initialized=True)
            original = fake.urlopen
            def lose_response(request, **kwargs):
                result = original(request, **kwargs)
                if urllib.parse.urlparse(request.full_url).path.endswith('/initJoin'):
                    raise error
                return result
            fake.urlopen = lose_response
            with self.subTest(error=type(error).__name__):
                self.run_hook(fake)
                self.assertEqual(fake.ready, [True, True])
                self.assertEqual(sum(path.endswith('/initJoin') for _, path, _ in fake.calls), 1)

    def test_lost_init_response_inspects_state(self):
        fake = fixtures.FakeApis(count=0)
        original = fake.urlopen
        def lose_response(request, **kwargs):
            result = original(request, **kwargs)
            if urllib.parse.urlparse(request.full_url).path.endswith('/init'):
                raise http.client.RemoteDisconnected()
            return result
        fake.urlopen = lose_response
        self.run_hook(fake, {'ALLOW_INITIALIZE': 'true'})
        self.assertEqual(fake.ready, [True])

    def test_http_override_is_explicit_and_only_applies_to_ordinal_zero(self):
        fake = fixtures.FakeApis(count=1, initialized=True)
        original, schemes = fake.urlopen, []
        def record(request, **kwargs):
            url = urllib.parse.urlparse(request.full_url)
            schemes.append((url.hostname, url.scheme))
            return original(request, **kwargs)
        fake.urlopen = record
        self.run_hook(fake, {'BOOTSTRAP_HTTP': 'true'})
        self.assertEqual({scheme for ip, scheme in schemes if ip == fake.ips[0]}, {'http'})
        self.assertEqual({scheme for ip, scheme in schemes if ip == fake.ips[1]}, {'https'})


if __name__ == '__main__':
    unittest.main()
