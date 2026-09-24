import base64
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from rankme.google_data import GoogleData, GoogleDataError, SCOPES, SEARCH_SCOPE, ANALYTICS_SCOPE, _http, _redirect

CLIENT='123-example.apps.googleusercontent.com'

class GoogleTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.google=GoogleData(self.temp.name)
    def connected(self):
        self.google.configure(CLIENT,'private-client-secret')
        data=self.google._read();data.update(access_token='access-secret',refresh_token='refresh-secret',expires_at=time.time()+3600,scope=' '.join(SCOPES));self.google._save(data)
    def test_configuration_private_and_safe_status(self):
        result=self.google.configure(CLIENT,'secret-value')
        self.assertTrue(result['configured']);self.assertFalse(result['connected'])
        self.assertNotIn('secret-value',json.dumps(result))
        self.assertEqual(os.stat(self.google.path).st_mode&0o777,0o600)
        with self.assertRaises(GoogleDataError):self.google.configure('invalid')
    def test_oauth_pkce_state_and_single_use_callback(self):
        self.google.configure(CLIENT)
        url=self.google.begin('http://127.0.0.1:8787/google/callback');q=parse_qs(urlsplit(url).query)
        pending=self.google._read()['pending']
        expected=base64.urlsafe_b64encode(hashlib.sha256(pending['verifier'].encode()).digest()).decode().rstrip('=')
        self.assertEqual(q['code_challenge'],[expected]);self.assertEqual(q['code_challenge_method'],['S256'])
        self.assertEqual(set(q['scope'][0].split()),set(SCOPES))
        with patch('rankme.google_data._http',return_value={'access_token':'access','refresh_token':'refresh','scope':SEARCH_SCOPE,'expires_in':3600}) as http:
            result=self.google.callback('authorization-code',q['state'][0],'http://127.0.0.1:8787/google/callback')
            self.assertEqual(http.call_args.args[2]['code_verifier'],pending['verifier'])
        self.assertTrue(result['search_console']);self.assertFalse(result['analytics'])
        with self.assertRaises(GoogleDataError):self.google.callback('authorization-code',q['state'][0],'http://127.0.0.1:8787/google/callback')
    def test_bad_or_expired_state_never_calls_token_endpoint(self):
        self.google.configure(CLIENT);self.google.begin('http://127.0.0.1:8787/callback')
        data=self.google._read();state=data['pending']['state'];data['pending']['created_at']=time.time()-700;self.google._save(data)
        with patch('rankme.google_data._http') as http:
            with self.assertRaises(GoogleDataError):self.google.callback('code','wrong','http://127.0.0.1:8787/callback')
            with self.assertRaises(GoogleDataError):self.google.callback('code',state,'http://127.0.0.1:8787/callback')
            http.assert_not_called()
    def test_redirect_is_loopback_only(self):
        for url in ['https://evil.com/callback','http://127.0.0.1/callback','http://127.0.0.1:8787/callback?x=1','http://user@127.0.0.1:8787/callback']:
            with self.assertRaises(GoogleDataError):_redirect(url)
    def test_secret_symlink_refused(self):
        target=Path(self.temp.name)/'other';target.write_text('{}');self.google.path.symlink_to(target)
        with self.assertRaises(GoogleDataError):self.google.configure(CLIENT,'secret')
        self.assertEqual(target.read_text(),'{}')
    def test_refresh_token_preserved_and_revoked_state_cleared(self):
        self.connected();data=self.google._read();data['expires_at']=0;self.google._save(data)
        with patch('rankme.google_data._http',return_value={'access_token':'new-access','expires_in':3600}):
            self.assertEqual(self.google._token(SEARCH_SCOPE),'new-access')
        self.assertEqual(self.google._read()['refresh_token'],'refresh-secret')
        data=self.google._read();data['expires_at']=0;self.google._save(data)
        with patch('rankme.google_data._http',side_effect=GoogleDataError('Rejected',status=400)):
            with self.assertRaises(GoogleDataError):self.google._token(SEARCH_SCOPE)
        self.assertFalse(self.google.status()['connected'])
    def test_disconnect_removes_local_tokens_even_when_revoke_fails(self):
        self.connected()
        with patch('rankme.google_data._http',side_effect=GoogleDataError('Network')):result=self.google.disconnect()
        self.assertFalse(result['connected']);self.assertTrue(result['configured'])
        self.assertNotIn('refresh_token',self.google._read());self.assertIn('Remote revocation',result['message'])
    def test_property_pagination_is_bounded(self):
        self.connected()
        def api(url,scope,payload=None):
            if scope==SEARCH_SCOPE:return {'siteEntry':[{'siteUrl':'https://example.com/','permissionLevel':'siteOwner'}]}
            return {'accountSummaries':[{'displayName':'Account','propertySummaries':[{'property':'properties/123','displayName':'Site'}]}],'nextPageToken':'repeated'}
        with patch.object(self.google,'_api',side_effect=api) as api_mock:result=self.google.properties()
        self.assertEqual(api_mock.call_count,3);self.assertTrue(result['issues']);self.assertEqual(result['analytics'][0]['property'],'properties/123')
    def test_search_pagination_limit(self):
        batch=[{'keys':['keyword'],'clicks':1,'impressions':10,'ctr':.1,'position':8}]*1000
        with patch.object(self.google,'_api',return_value={'rows':batch}) as api:
            rows,limited=self.google._search('https://example.com/',{'start':'2026-01-01','end':'2026-01-28'},'query')
        self.assertEqual(len(rows),5000);self.assertTrue(limited);self.assertEqual(api.call_count,5)
    def test_sync_totals_are_not_sum_of_query_rows(self):
        self.connected()
        def api(url,scope,payload=None):
            if scope==ANALYTICS_SCOPE:
                self.assertEqual(payload['dimensionFilter']['filter']['stringFilter']['value'],'Organic Search')
                return {'rows':[{'metricValues':[{'value':'50'},{'value':'3'}]}]}
            if payload.get('dimensions'):
                return {'rows':[{'keys':['topic'],'clicks':1,'impressions':200,'ctr':.005,'position':8}]}
            return {'rows':[{'clicks':100,'impressions':1000,'ctr':.1,'position':7}]}
        with patch.object(self.google,'_api',side_effect=api):result=self.google.sync('https://example.com/','properties/123')
        self.assertEqual(result['search_console']['current']['totals']['clicks'],100)
        self.assertEqual(result['analytics']['current']['organic_sessions'],50)
        self.assertEqual(result['analytics']['current']['key_events'],3)
        self.assertTrue(result['recommendations']);self.assertNotIn('access-secret',json.dumps(result))
        from datetime import date
        for period in result['periods'].values():self.assertEqual((date.fromisoformat(period['end'])-date.fromisoformat(period['start'])).days,27)
    def test_api_error_never_echoes_secret_response(self):
        error=HTTPError('https://oauth2.googleapis.com/token?secret-token',401,'secret-body',{},None)
        with patch('rankme.google_data.build_opener') as opener:
            opener.return_value.open.side_effect=error
            with self.assertRaises(GoogleDataError) as result:_http('https://oauth2.googleapis.com/token',token='secret-token')
        self.assertNotIn('secret',str(result.exception))
    def test_malformed_credentials_do_not_break_status(self):
        self.google.path.write_text('{broken')
        result=self.google.status()
        self.assertFalse(result['configured']);self.assertFalse(result['connected'])
        with self.assertRaises(GoogleDataError):self.google.begin('http://127.0.0.1:8787/callback')
    def test_total_sync_failure_raises_instead_of_returning_empty_snapshot(self):
        self.connected()
        with patch.object(self.google,'_api',side_effect=GoogleDataError('Service unavailable')):
            with self.assertRaisesRegex(GoogleDataError,'previous data was preserved'):
                self.google.sync('https://example.com/','123')
    def test_api_endpoint_restricted(self):
        with self.assertRaises(GoogleDataError):_http('https://evil.example/token',token='secret')
    def test_invalid_ga_property_rejected(self):
        with self.assertRaises(GoogleDataError):self.google.sync('https://example.com/','../../other')

if __name__=='__main__':unittest.main()
