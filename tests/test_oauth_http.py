"""Exercise the real HTTP handler's OAuth exception and secret boundaries offline."""
import io
import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import urlencode

from rankme.server import Application, handler_for


class OAuthHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = Application(self.temp.name)
        self.google = Mock()
        self.google.status.return_value = {'configured': True, 'connected': False}
        self.google.configure.return_value = {'configured': True, 'connected': False}
        self.app.engine.google = self.google
        self.app.oauth_redirect = 'http://127.0.0.1:8787/api/google/callback'

    def tearDown(self):
        self.app.store.close()
        self.temp.cleanup()

    def request(self, method, path, headers=None, payload=None):
        handler = object.__new__(handler_for(self.app))
        handler.server = SimpleNamespace(server_port=8787)
        handler.path = path
        body = json.dumps(payload or {}).encode()
        handler.headers = {'Host': '127.0.0.1:8787', 'Content-Length': str(len(body)), **(headers or {})}
        handler.rfile = io.BytesIO(body)
        handler.send = Mock()
        handler.handle_request(method)
        return handler.send.call_args.args

    def callback_path(self, state='one-use-state', code='secret-code+with/slash'):
        return '/api/google/callback?' + urlencode({'code': code, 'state': state})

    def test_google_crosssite_get_callback_passes_state_and_code(self):
        response = self.request('GET', self.callback_path(), {'Sec-Fetch-Site': 'cross-site', 'Origin': 'https://accounts.google.com'})
        self.assertEqual(response[0], 200)
        self.google.callback.assert_called_once_with('secret-code+with/slash', 'one-use-state', self.app.oauth_redirect)
        self.assertNotIn('secret-code', response[1])
        self.assertNotIn('one-use-state', response[1])

    def test_callback_requires_exact_loopback_host_and_port(self):
        for host in ('evil.test:8787', 'localhost:8787', '127.0.0.1:8788', '127.0.0.1:8787.evil.test'):
            with self.subTest(host=host):
                self.assertEqual(self.request('GET', self.callback_path(), {'Host': host, 'Sec-Fetch-Site': 'cross-site'})[0], 403)
        self.google.callback.assert_not_called()

    def test_failed_state_validation_returns_generic_error_without_secrets(self):
        def validate(code, state, redirect):
            if state != 'expected-state':
                raise ValueError('Sensitive failure: code=' + code + ' state=' + state)
            return {'connected': True}
        self.google.callback.side_effect = validate
        result = self.request('GET', self.callback_path(state='wrong-secret-state'), {'Sec-Fetch-Site': 'cross-site'})
        self.assertEqual(result[0], 400)
        self.assertNotIn('wrong-secret-state', result[1])
        self.assertNotIn('secret-code', result[1])
        self.assertNotIn('Sensitive failure', result[1])

    def test_regular_crosssite_apis_remain_blocked(self):
        for path in ('/api/session', '/api/state', '/api/google/properties', '/api/backup'):
            with self.subTest(path=path):
                result = self.request('GET', path, {'Sec-Fetch-Site': 'cross-site'})
                self.assertEqual(result[0], 403)
        self.google.properties.assert_not_called()

    def test_callback_crosssite_exception_is_get_only(self):
        result = self.request('POST', self.callback_path(), {'Sec-Fetch-Site': 'cross-site',
            'X-RankMe-Token': self.app.token, 'Content-Type': 'application/json'})
        self.assertEqual(result[0], 403)
        self.google.callback.assert_not_called()

    def test_google_configure_requires_token_and_same_origin(self):
        payload = {'client_id': 'desktop.apps.googleusercontent.com', 'client_secret': 'PRIVATE_CLIENT_SECRET'}
        self.assertEqual(self.request('POST', '/api/google/configure', {'Content-Type': 'application/json'}, payload)[0], 403)
        self.assertEqual(self.request('POST', '/api/google/configure', {'Content-Type': 'application/json', 'X-RankMe-Token': 'wrong'}, payload)[0], 403)
        auth = {'Content-Type': 'application/json', 'X-RankMe-Token': self.app.token}
        self.assertEqual(self.request('POST', '/api/google/configure', {**auth, 'Origin': 'https://evil.test'}, payload)[0], 403)
        self.google.configure.assert_not_called()
        result = self.request('POST', '/api/google/configure', auth, payload)
        self.assertEqual(result[0], 200)
        self.google.configure.assert_called_once_with(payload['client_id'], payload['client_secret'])
        self.assertNotIn('PRIVATE_CLIENT_SECRET', json.dumps(result))


if __name__ == '__main__':
    unittest.main()
