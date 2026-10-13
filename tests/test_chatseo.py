"""ChatSEO security, accounting, protocol and import regressions, entirely offline."""
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
import uuid
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from rankme.chatseo import ChatSEO, ChatSEOError, _http, _remaining, MCP, TOKEN, REGISTER
from rankme.server import Application
from tests.test_oauth_http import OAuthHTTPTests


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = Application(self.temp.name)
        self.addCleanup(self.app.store.close)
        self.p = self.app.engine.chatseo
        self.client = self.app.store.put('clients', {'name': 'Example', 'url': 'https://example.com', 'chatseo_site_id': 'site-1',
            'chatseo_settings': {'enabled': True, 'daily_request_limit': 2, 'minimum_credits': 1}})
        self.p._save({'access_token': 'PRIVATE_ACCESS', 'refresh_token': 'PRIVATE_REFRESH', 'expires_at': time.time() + 3600, 'client_id': 'public'})
        self.app.store.settings({'chatseo_cache': {'sites': [{'id': 'site-1', 'name': 'Example', 'domain': 'example.com'}],
            'credits': {'remaining': 30}, 'capabilities': ['send_message', 'get_conversation_messages']}})
        self.p.tools = {name: {'inputSchema': {'properties': props}} for name, props in {
            'send_message': {'message': {}, 'siteId': {}}, 'get_conversation_messages': {'conversationId': {}},
            'list_artifacts': {'conversationId': {}}, 'get_artifact': {'id': {}}}.items()}

    def data(self, message='Audit the site exactly as written.\nKeep this text.'):
        return {'message': message, 'credit_acknowledged': True, 'request_id': str(uuid.uuid4())}

    def pending(self, data=None):
        with patch.object(self.p, 'sync', return_value=self.p.status()), patch('rankme.chatseo.threading.Thread'):
            return self.p.send(self.client['id'], data or self.data())

    def test_credit_fixture_real_provider_and_bad_values(self):
        self.assertEqual(_remaining({'subscriptionCredits': 30, 'subscriptionCreditsUsed': 0,
            'subscriptionCreditsRemaining': 30, 'topUpCreditsRemaining': 4, 'bonusCreditsRemaining': 2, 'canGenerate': True}), 36)
        self.assertIsNone(_remaining({'subscriptionCreditsRemaining': float('nan')}))
        self.assertIsNone(_remaining({'remaining': True}))
        self.assertIsNone(_remaining({'remaining': -1}))

    def test_private_file_mode_and_no_credentials_in_backup_or_status(self):
        self.assertEqual(self.p.path.stat().st_mode & 0o777, 0o600)
        snapshot = json.dumps({'status': self.p.status(), 'backup': self.app.store.backup()})
        self.assertNotIn('PRIVATE_', snapshot)
        self.p.path.chmod(0o644)
        self.assertFalse(self.p.status()['connected'])
        with self.assertRaises(ChatSEOError):
            self.p._save({})

    def test_symlink_and_hardlink_rejected(self):
        other = Path(self.temp.name) / 'other'
        self.p.path.rename(other)
        self.p.path.symlink_to(other)
        with self.assertRaises(ChatSEOError):
            self.p._read()
        self.p.path.unlink()
        os.link(other, self.p.path)
        with self.assertRaises(ChatSEOError):
            self.p._read()

    def test_oauth_pkce_one_use_and_only_fixed_redirect(self):
        self.p.disconnect()
        with patch('rankme.chatseo._http', return_value=({'client_id': 'public'}, '')) as http:
            url = self.p.begin(self.app.chatseo_redirect)
        http.assert_called_once()
        self.assertEqual(http.call_args.args[0], REGISTER)
        query = parse_qs(urlsplit(url).query)
        self.assertEqual(query['code_challenge_method'], ['S256'])
        self.assertNotIn(self.p.pending['verifier'], url)
        with self.assertRaises(ChatSEOError), patch('rankme.chatseo._http') as http:
            self.p.callback('code', 'wrong', self.app.chatseo_redirect)
        http.assert_not_called()
        with patch('rankme.chatseo._http', return_value=({'access_token': 'new', 'expires_in': 3600}, '')) as http:
            self.p.callback('code', query['state'][0], self.app.chatseo_redirect)
        self.assertEqual(http.call_args.args[0], TOKEN)
        with self.assertRaises(ChatSEOError):
            self.p.callback('code', query['state'][0], self.app.chatseo_redirect)
        for uri in ('https://evil.test/api/chatseo/callback', 'http://localhost:8787/api/chatseo/callback?x=1', 'http://localhost:8787/api/google/callback'):
            with self.assertRaises(ChatSEOError):
                self.p.begin(uri)

    def test_http_endpoint_redirect_and_response_bounds(self):
        with patch('rankme.chatseo.build_opener') as opener:
            for url in ('http://127.0.0.1', MCP + '?x=1', 'https://api.chatseo.app.evil/mcp'):
                with self.assertRaises(ChatSEOError):
                    _http(url, {})
            opener.assert_not_called()
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read1.return_value = b'x' * 2000001
        with patch('rankme.chatseo.build_opener') as opener:
            opener.return_value.open.return_value = response
            with self.assertRaisesRegex(ChatSEOError, 'size limit'):
                _http(MCP, {})

    def test_paid_request_exact_text_durable_dedupe_and_conflicts(self):
        data = self.data()
        run = self.pending(data)
        with patch.object(self.p, 'sync') as sync:
            same = self.p.send(self.client['id'], data)
            self.assertEqual(same['id'], run['id'])
            sync.assert_not_called()
        with patch.object(self.p, 'call', return_value={'conversationId': 'conv-1'}) as call:
            self.p._submit(run['id'])
            self.p._submit(run['id'])
        call.assert_called_once_with('send_message', {'message': data['message'], 'siteId': 'site-1'})
        with self.assertRaises(ChatSEOError):
            self.p.send(self.client['id'], {**data, 'message': 'changed'})

    def test_uncertain_send_restart_and_resolution_never_resend(self):
        run = self.pending()
        with patch.object(self.p, 'call', side_effect=ChatSEOError('timeout')) as call:
            self.p._submit(run['id'])
            self.p._submit(run['id'])
        call.assert_called_once()
        self.assertEqual(self.app.store.get('chatseo_runs', run['id'])['status'], 'uncertain')
        with self.assertRaises(ChatSEOError):
            self.p.resolve(self.client['id'], run['id'], False)
        resolved = self.p.resolve(self.client['id'], run['id'], True)
        self.assertEqual(resolved['status'], 'acknowledged')
        second = self.pending()
        recovered = ChatSEO(self.temp.name, self.app.store)
        self.assertEqual(self.app.store.get('chatseo_runs', second['id'])['status'], 'uncertain')
        self.assertEqual(len(self.app.store.all('chatseo_runs')), 2)

    def test_paid_defaults_acknowledgment_minimum_and_daily_cap(self):
        self.app.store.update('clients', self.client['id'], chatseo_settings={})
        with self.assertRaisesRegex(ChatSEOError, 'Enable'):
            self.pending()
        self.app.store.update('clients', self.client['id'], chatseo_settings={'enabled': True, 'daily_request_limit': 1, 'minimum_credits': 31})
        with self.assertRaisesRegex(ChatSEOError, 'balance'):
            self.pending()
        self.app.store.update('clients', self.client['id'], chatseo_settings={'enabled': True, 'daily_request_limit': 1, 'minimum_credits': 1})
        with self.assertRaises(ChatSEOError):
            self.pending({**self.data(), 'credit_acknowledged': False})
        run = self.pending()
        self.app.store.update('chatseo_runs', run['id'], status='acknowledged')
        with self.assertRaisesRegex(ChatSEOError, 'daily'):
            self.pending()

    def test_runtime_schema_mismatch_fails_closed(self):
        with patch.object(self.p, '_rpc') as rpc:
            with self.assertRaises(ChatSEOError):
                self.p.call('get_artifact', {'artifactId': 'a'})
            with self.assertRaises(ChatSEOError):
                self.p.call('shopify_publish_article', {})
            rpc.assert_not_called()

    def test_poll_import_membership_and_draft_review(self):
        run = self.pending()
        self.app.store.update('chatseo_runs', run['id'], status='processing', conversation_id='conv-1')
        def call(name, args):
            if name == 'get_conversation_messages':
                return {'status': 'complete', 'messages': [{'role': 'assistant', 'content': 'Finished.'}]}
            if name == 'list_artifacts':
                return {'artifacts': [{'id': 'artifact-1', 'title': 'SEO article', 'type': 'article'}]}
            if name == 'get_artifact':
                self.assertEqual(args, {'id': 'artifact-1'})
                return {'id': 'artifact-1', 'conversationId': 'conv-1', 'title': 'SEO article', 'content': '# Draft\nNeeds factual review.'}
            self.fail(name)
        with patch.object(self.p, 'call', side_effect=call), patch.object(self.p, 'sync', return_value=self.p.status()):
            polled = self.p.poll(self.client['id'], run['id'])
            self.assertEqual(polled['status'], 'complete')
            with self.assertRaises(ChatSEOError):
                self.p.import_artifact(self.client['id'], run['id'], 'foreign')
            article = self.p.import_artifact(self.client['id'], run['id'], 'artifact-1')
            again = self.p.import_artifact(self.client['id'], run['id'], 'artifact-1')
        self.assertEqual(article['id'], again['id'])
        self.assertEqual(article['status'], 'held')
        self.assertFalse(article['review']['passed'])
        self.assertNotIn('reviewed_digest', article)
        other = self.app.store.put('clients', {'chatseo_site_id': 'site-1'})
        with self.assertRaises(ChatSEOError):
            self.p.poll(other['id'], run['id'])

    def test_new_oauth_grant_drops_previous_account_refresh(self):
        self.p.pending = {'state': 's', 'verifier': 'v', 'expires': time.time() + 60, 'redirect': self.app.chatseo_redirect}
        with patch('rankme.chatseo._http', return_value=({'access_token': 'ACCOUNT_B_ACCESS', 'expires_in': 3600}, '')):
            self.p.callback('code', 's', self.app.chatseo_redirect)
        self.assertNotIn('refresh_token', self.p._read())
        saved = self.p._read()
        saved['expires_at'] = 0
        self.p._save(saved)
        with patch('rankme.chatseo._http') as http, self.assertRaises(ChatSEOError):
            self.p._token()
        http.assert_not_called()

    def test_mismatched_site_or_artifact_response_fails_closed(self):
        self.p.tools['list_site_actions'] = {'inputSchema': {'properties': {'siteId': {}}}}
        with patch.object(self.p, 'sync', return_value=self.p.status()), patch.object(self.p, 'call', return_value={
                'actions': [{'id': 'good', 'siteId': 'site-1'}, {'id': 'bad', 'siteId': 'other'}]}):
            report = self.p.sync_client(self.client['id'])
        self.assertEqual(report['actions'], [])
        run = self.pending()
        self.app.store.update('chatseo_runs', run['id'], status='complete', conversation_id='conv-1', artifacts=[{'id': 'artifact-1'}])
        for artifact in ({'id': 'other', 'content': 'bad'}, {'id': 'artifact-1', 'conversationId': 'other', 'content': 'bad'}):
            with patch.object(self.p, 'sync', return_value=self.p.status()), patch.object(self.p, 'call', side_effect=[
                    {'artifacts': [{'id': 'artifact-1'}]}, artifact]), self.assertRaises(ChatSEOError):
                self.p.import_artifact(self.client['id'], run['id'], 'artifact-1')
        self.assertEqual(self.app.store.all('articles'), [])

    def test_snapshot_bound_and_remapping_drops_old_site_data(self):
        for index in range(60):
            self.app.store.put('chatseo_runs', {'client_id': self.client['id'], 'site_id': 'site-1',
                'message': '測' * 8000, 'reply': '測' * 100000, 'status': 'complete', 'artifacts': []})
        snapshot = self.p.snapshot()
        self.assertLess(len(json.dumps(snapshot).encode()), 500000)
        self.assertLessEqual(len(snapshot['chatseo_runs']), 20)
        self.app.store.put('chatseo_reports', {'id': self.client['id'], 'client_id': self.client['id'], 'site_id': 'site-1'})
        self.app.store.update('clients', self.client['id'], chatseo_site_id='changed')
        result = self.app.dispatch('GET', '/api/clients/' + self.client['id'] + '/chatseo', {})
        self.assertEqual(result, {'report': None, 'runs': []})

    def test_full_cached_response_read_is_bound_and_works_disconnected(self):
        run = self.pending()
        body = 'Full response. ' * 5000
        self.app.store.update('chatseo_runs', run['id'], status='complete', reply=body)
        self.p.disconnect()
        with patch('rankme.chatseo._http') as network:
            result = self.app.dispatch('POST', '/api/clients/' + self.client['id'] + '/chatseo-read', {'run_id': run['id']})
        network.assert_not_called()
        self.assertEqual(result['reply'], body)
        self.app.store.update('clients', self.client['id'], chatseo_site_id='other')
        with self.assertRaises(ChatSEOError):
            self.p.read_run(self.client['id'], run['id'])

    def test_mcp_initialization_discovers_runtime_allowlist(self):
        self.p.tools = {}
        def http(url, payload, **kwargs):
            self.assertEqual(url, MCP)
            if payload['method'] == 'notifications/initialized':
                return {}, ''
            result = {'tools': [{'name': 'send_message', 'inputSchema': {'properties': {'message': {}, 'siteId': {}}}},
                                {'name': 'delete_site', 'inputSchema': {}}]} if payload['method'] == 'tools/list' else {}
            return {'jsonrpc': '2.0', 'id': payload['id'], 'result': result}, 'session'
        with patch('rankme.chatseo._http', side_effect=http) as request:
            self.assertEqual(self.p.discover(), ['send_message'])
        self.assertEqual([c.args[1]['method'] for c in request.call_args_list], ['initialize', 'notifications/initialized', 'tools/list'])

    def test_unavailable_capability_is_explicit(self):
        with patch.object(self.p, 'sync', return_value=self.p.status()):
            report = self.p.sync_client(self.client['id'])
        self.assertEqual(report['actions'], [])
        self.assertIn('list_site_actions', report['unavailable'])
        self.assertIn('list_conversations', report['unavailable'])


class ChatSEOOAuthHTTPTests(OAuthHTTPTests):
    # Inherit the real HTTP harness, plus its existing Google regressions.
    def test_chatseo_crosssite_callback_bound_to_session_and_provider(self):
        provider = Mock()
        self.app.engine.chatseo = provider
        self.app.auth.begin_oauth(self.session_key, 'chatseo:state')
        path = '/api/chatseo/callback?code=PRIVATE_CODE&state=state'
        bad = self.request('GET', path, {'Cookie': '', 'Sec-Fetch-Site': 'cross-site'})
        self.assertEqual(bad[0], 400)
        provider.callback.assert_not_called()
        good = self.request('GET', path, {'Sec-Fetch-Site': 'cross-site'})
        self.assertEqual(good[0], 200)
        self.assertNotIn('PRIVATE_CODE', good[1])
        provider.callback.assert_called_once_with('PRIVATE_CODE', 'state', self.app.chatseo_redirect)
        self.assertEqual(self.request('GET', path, {'Sec-Fetch-Site': 'cross-site'})[0], 400)

    def test_chatseo_mutation_requires_owner_csrf_and_origin(self):
        provider = Mock()
        self.app.engine.chatseo = provider
        for path in ('/api/chatseo/connect', '/api/chatseo/disconnect', '/api/chatseo/sync'):
            self.assertEqual(self.request('POST', path, {'Content-Type': 'application/json'})[0], 403)
        provider.begin.assert_not_called()
        provider.disconnect.assert_not_called()
        provider.sync.assert_not_called()
