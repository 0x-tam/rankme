import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from rankme.ai import CodexRunner
from rankme.crawler import normalize_url, _addresses, _Page, crawl_site, fetch_public
from rankme.google_data import GoogleData, GoogleDataError, _http


class SecurityHardeningTests(unittest.TestCase):
    def test_special_destinations_and_controls_rejected(self):
        for url in ('http://224.0.0.1/', 'http://[ff02::1]/',
                    'http://[2002:7f00:1::]/', 'http://[64:ff9b::7f00:1]/',
                    'http://[::ffff:127.0.0.1]/', '\nhttps://example.com',
                    'https://exam\tple.com', 'https://example.com/\x7f'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                normalize_url(url)

    def test_dns_transition_addresses_rejected(self):
        addresses = [(socket.AF_INET6, socket.SOCK_STREAM, 6, '', ('2002:7f00:1::', 443, 0, 0))]
        with patch('rankme.crawler.socket.getaddrinfo', return_value=addresses):
            with self.assertRaises(ValueError):
                _addresses('example.com', 443)

    def test_invalid_read_limits_fail_before_network(self):
        with patch('rankme.crawler._addresses') as resolve:
            for limit in (0, -1, 10000001, '100'):
                with self.assertRaises(ValueError):
                    fetch_public('https://example.com', limit)
            resolve.assert_not_called()

    def test_alternating_canonical_redirects_terminate(self):
        def fetch(url, *args):
            if url.endswith('robots.txt'):
                return {'url': url, 'text': 'User-agent: *\nDisallow:'}
            if url.endswith('sitemap.xml'):
                return {'url': url, 'text': '<urlset/>'}
            new = 'https://example.com/' if 'www.' in url else 'https://www.example.com/'
            return {'url': new, 'text': '<title>Example</title>'}
        with patch('rankme.crawler.fetch_public', side_effect=fetch) as fetcher:
            result = crawl_site('https://example.com')
        self.assertLessEqual(fetcher.call_count, 9)
        self.assertIn('canonical redirects', ' '.join(result['issues']))

    def test_subprocess_env_excludes_unrelated_secrets_and_injection(self):
        with tempfile.TemporaryDirectory() as root:
            runner = CodexRunner(root)
            with patch.dict(os.environ, {'PATH': '/bin', 'HOME': root, 'CODEX_HOME': root,
                    'AWS_SECRET_ACCESS_KEY': 'secret', 'GH_TOKEN': 'secret',
                    'GOOGLE_APPLICATION_CREDENTIALS': '/credentials', 'SSH_AUTH_SOCK': '/ssh',
                    'NODE_OPTIONS': '--require=/tmp/inject.js', 'PYTHONPATH': '/tmp/inject',
                    'OPENAI_API_KEY': 'secret'}, clear=True):
                self.assertEqual(runner._env(), {'PATH': '/bin', 'HOME': root, 'CODEX_HOME': root})

    def test_google_endpoint_and_header_validation_precedes_network(self):
        with patch('rankme.google_data.build_opener') as opener:
            for url in ('https://oauth2.googleapis.com:444/token',
                        'https://oauth2.googleapis.com:bad/token',
                        'https://oauth2.googleapis.com/token#fragment',
                        '\nhttps://oauth2.googleapis.com/token'):
                with self.assertRaises(GoogleDataError):
                    _http(url, token='secret')
            with self.assertRaises(GoogleDataError):
                _http('https://oauth2.googleapis.com/token', token='secret\r\nX-Injected: value')
            opener.assert_not_called()

    def test_credentials_reject_hardlinks_and_fifos(self):
        with tempfile.TemporaryDirectory() as root:
            google = GoogleData(root)
            other = Path(root) / 'other'
            other.write_text(json.dumps({'client_id': 'test'}))
            os.link(other, google.path)
            with self.assertRaises(GoogleDataError):
                google._read()
            google.path.unlink()
            os.mkfifo(google.path)
            with self.assertRaises(GoogleDataError):
                google._read()

    def test_crawler_observed_metadata(self):
        parser = _Page('https://example.com/page')
        parser.feed('<link rel="canonical" href="/canonical"><meta name="robots" content="noindex,follow">'
                    '<h1>Main <em>heading</em></h1><h2>Section</h2>')
        self.assertEqual(parser.canonical, 'https://example.com/canonical')
        self.assertEqual(parser.robots, ['noindex,follow'])
        self.assertEqual(parser.headings, [{'level': 1, 'text': 'Main heading'}, {'level': 2, 'text': 'Section'}])
        parser.feed('<link rel="canonical" href="http://127.0.0.1/">')
        self.assertEqual(parser.canonical, 'https://example.com/canonical')

    def test_google_joint_query_page_rows_and_aggregation(self):
        with tempfile.TemporaryDirectory() as root:
            google = GoogleData(root)
            response = {'rows': [{'keys': ['same query', 'https://example.com/a'], 'clicks': 2},
                                 {'keys': ['same query', 'https://example.com/b'], 'clicks': 3}]}
            with patch.object(google, '_api', return_value=response) as api:
                rows, limited = google._search('https://example.com/', {'start': '2026-01-01', 'end': '2026-01-28'}, ('query', 'page'))
            request = api.call_args.args[2]
            self.assertEqual(request['dimensions'], ['query', 'page'])
            self.assertEqual(request['aggregationType'], 'auto')
            self.assertFalse(limited)
            self.assertEqual([row['page'] for row in rows], ['https://example.com/a', 'https://example.com/b'])
            self.assertEqual([row['query'] for row in rows], ['same query', 'same query'])

    def test_joint_evidence_failure_preserves_successful_sync(self):
        with tempfile.TemporaryDirectory() as root:
            google = GoogleData(root)
            def search(site, period, dimension=None):
                if isinstance(dimension, tuple):
                    raise GoogleDataError('Quota')
                return ([{'clicks': 10, 'impressions': 20, 'ctr': .5, 'position': 1}] if not dimension else []), False
            with patch.object(google, 'status', return_value={'search_console': True}), patch.object(google, '_search', side_effect=search):
                result = google.sync('https://example.com/')
            self.assertEqual(result['search_console']['current']['totals']['clicks'], 10)
            self.assertEqual(result['search_console']['current']['query_pages'], [])
            self.assertEqual(len(result['issues']), 2)


if __name__ == '__main__':
    unittest.main()
