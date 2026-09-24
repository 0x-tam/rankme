import json
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from rankme.crawler import normalize_url, _addresses, _Page, crawl_site, fetch_public
from rankme.ai import AIError, CodexRunner, _validate, REVIEW, inspect_business, _client_context, _existing_context, review_article

class CrawlTests(unittest.TestCase):
    def test_normalization(self):
        self.assertEqual(normalize_url('Example.com/about#x'), 'https://example.com/about')
        self.assertEqual(normalize_url('https://example.com/über'), 'https://example.com/%C3%BCber')
        for url in ['http://127.0.0.1', 'https://user:pass@example.com', 'http://[::1]', 'http://192.168.1.5', 'http://localhost', 'http://example.com:22', 'ftp://example.com']:
            with self.assertRaises(ValueError, msg=url): normalize_url(url)
    def test_all_dns_addresses_must_be_public(self):
        addresses = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443)), (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('10.0.0.1', 443))]
        with patch('rankme.crawler.socket.getaddrinfo', return_value=addresses):
            with self.assertRaises(ValueError): _addresses('example.com', 443)
    def test_private_redirect_blocked(self):
        response = type('Response', (), {'status': 302, 'getheader': lambda self, key: 'http://127.0.0.1/private'})()
        addresses = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 80))]
        with patch('rankme.crawler._addresses', return_value=addresses), patch('rankme.crawler.socket.socket'), patch('rankme.crawler.http.client.HTTPConnection') as connection:
            connection.return_value.getresponse.return_value = response
            with self.assertRaises(ValueError): fetch_public('http://example.com')

    def test_response_size_is_bounded(self):
        class Response:
            status = 200
            def getheader(self, name, default=''): return 'text/html'
            def read1(self, size): return b'x' * size
        addresses = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 80))]
        with patch('rankme.crawler._addresses', return_value=addresses), patch('rankme.crawler.socket.socket') as sock, patch('rankme.crawler.http.client.HTTPConnection') as connection:
            connection.return_value.getresponse.return_value = Response()
            with self.assertRaisesRegex(ValueError, 'size limit'):
                fetch_public('http://example.com', max_bytes=100)
            sock.return_value.connect.assert_called_once_with(('93.184.216.34', 80))

    def test_external_redirect_blocked_before_fetch(self):
        response = type('Response', (), {'status': 302, 'getheader': lambda self, key: 'http://other.example/page'})()
        addresses = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 80))]
        with patch('rankme.crawler._addresses', return_value=addresses) as resolve, patch('rankme.crawler.socket.socket'), patch('rankme.crawler.http.client.HTTPConnection') as connection:
            connection.return_value.getresponse.return_value = response
            with self.assertRaisesRegex(ValueError, 'leaves'):
                fetch_public('http://example.com', allowed_hosts={'example.com'})
            self.assertEqual(resolve.call_count, 1)

    def test_sitemap_discovers_unlinked_pages(self):
        def fetch(url, *args):
            if url.endswith('robots.txt'): return {'url': url, 'text': 'User-agent: *\nDisallow:'}
            if url.endswith('sitemap.xml'): return {'url': url, 'text': '<urlset><url><loc>https://example.com/services</loc></url></urlset>'}
            return {'url': url, 'text': '<title>Acme</title><p>Business page</p>'}
        with patch('rankme.crawler.fetch_public', side_effect=fetch): result = crawl_site('https://example.com', 2)
        self.assertEqual([p['url'] for p in result['pages']], ['https://example.com/', 'https://example.com/services'])

    def test_html_extraction(self):
        parser = _Page('https://example.com/')
        parser.feed('<title>Example</title><script>secret()</script><meta name="description" content="Test"><h1>Hello</h1><a href="/about">About</a>')
        self.assertNotIn('secret()', parser.parts)
        self.assertEqual(parser.links, ['https://example.com/about'])
        self.assertEqual(parser.description, 'Test')
    def test_robots_and_host_bounds(self):
        calls = []
        def fetch(url, *args):
            calls.append(url)
            if url.endswith('robots.txt'): return {'url': url, 'text': 'User-agent: *\nDisallow: /private'}
            return {'url': url, 'text': '<title>Acme</title><a href="/private">Private</a><a href="https://other.example/">Other</a><a href="/about">About</a>'}
        with patch('rankme.crawler.fetch_public', side_effect=fetch): result = crawl_site('https://example.com', 4)
        self.assertEqual(len(result['pages']), 2)
        self.assertNotIn('https://example.com/private', calls)
        self.assertNotIn('https://other.example/', calls)
    def test_robots_network_failure_stops_crawl(self):
        with patch('rankme.crawler.fetch_public', side_effect=ValueError('timeout')):
            self.assertEqual(crawl_site('https://example.com')['pages'], [])

class AITests(unittest.TestCase):
    def test_strict_schema(self):
        _validate({'passed': True, 'score': 95, 'issues': [], 'summary': 'Good'}, REVIEW)
        for value in [{'passed': True, 'score': 101, 'issues': [], 'summary': 'Good'}, {'passed': True, 'score': True, 'issues': [], 'summary': 'Good'}, {'passed': True}]:
            with self.assertRaises(AIError): _validate(value, REVIEW)
    def test_empty_inspection_rejected(self):
        with self.assertRaises(AIError): inspect_business(None, {'pages': []})
    def test_runner_uses_auth_no_api_and_structured_output(self):
        with tempfile.TemporaryDirectory() as temp:
            runner = CodexRunner(temp)
            runner.status = lambda: {'authenticated': True}
            captured = {}
            def run(args, **kwargs):
                captured.update(args=args, kwargs=kwargs)
                Path(args[args.index('--output-last-message') + 1]).write_text(json.dumps({'passed': True, 'score': 85, 'issues': [], 'summary': 'Checked'}))
                return type('Result', (), {'returncode': 0})()
            with patch('rankme.cancel.run', side_effect=run), patch.dict('os.environ', {'OPENAI_API_KEY': 'must-not-leak', 'CODEX_API_KEY': 'also-must-not-leak'}): result = runner.run('Review', REVIEW)
            self.assertTrue(result['passed'])
            self.assertIn('--ignore-user-config', captured['args'])
            self.assertIn('read-only', captured['args'])
            self.assertNotIn('OPENAI_API_KEY', captured['kwargs']['env'])
            self.assertNotIn('CODEX_API_KEY', captured['kwargs']['env'])
            self.assertNotIn('--ignore-rules', captured['args'])
            self.assertFalse(captured['kwargs'].get('shell', False))
    def test_required_web_research_cannot_be_skipped(self):
        with tempfile.TemporaryDirectory() as temp:
            runner = CodexRunner(temp)
            runner.status = lambda: {'authenticated': True}
            def run(args, **kwargs):
                Path(args[args.index('--output-last-message') + 1]).write_text(json.dumps({'passed': True, 'score': 90, 'issues': [], 'summary': 'Claims researched'}))
                return type('Result', (), {'returncode': 0})()
            with patch('rankme.cancel.run', side_effect=run):
                with self.assertRaisesRegex(AIError, 'without a recorded web research'):
                    runner.run('Review', REVIEW, require_research=True)

    def test_recorded_web_research_is_accepted(self):
        with tempfile.TemporaryDirectory() as temp:
            runner = CodexRunner(temp)
            runner.status = lambda: {'authenticated': True}
            def run(args, **kwargs):
                kwargs['stdout'].write(json.dumps({'type': 'item.completed', 'item': {'type': 'web_search'}}) + '\n')
                kwargs['stdout'].flush()
                Path(args[args.index('--output-last-message') + 1]).write_text(json.dumps({'passed': True, 'score': 90, 'issues': [], 'summary': 'Checked'}))
                return type('Result', (), {'returncode': 0})()
            with patch('rankme.cancel.run', side_effect=run):
                self.assertTrue(runner.run('Review', REVIEW, require_research=True)['passed'])

    def test_private_configuration_not_sent_to_ai(self):
        context = _client_context({'name': 'Acme', 'url': 'https://example.com', 'profile': {'summary': 'Company'},
            'publishing': {'token': 'secret', 'project_dir': '/private/client'},
            'crawl': {'pages': [{'url': 'https://example.com', 'title': 'Home', 'text': 'Large text'}]}})
        self.assertNotIn('secret', json.dumps(context))
        self.assertNotIn('Large text', json.dumps(context))
        self.assertEqual(context['name'], 'Acme')

    def test_history_is_bounded(self):
        context = _existing_context([{'body': 'x' * 9000}] * 300)
        self.assertEqual(len(context), 150)
        self.assertEqual(len(context[0]['body']), 1200)

    def test_review_cannot_pass_with_issues(self):
        class Runner:
            def run(self, *args, **kwargs):
                return {'passed': True, 'score': 99, 'issues': ['Unsupported claim'], 'summary': 'Fix'}
        self.assertFalse(review_article(Runner(), {}, {})['passed'])

    def test_rate_limit_is_retryable(self):
        with tempfile.TemporaryDirectory() as temp:
            runner = CodexRunner(temp)
            runner.status = lambda: {'authenticated': True}
            def run(args, **kwargs):
                kwargs['stdout'].write('usage limit reached'); kwargs['stdout'].flush()
                return type('Result', (), {'returncode': 1})()
            with patch('rankme.cancel.run', side_effect=run):
                with self.assertRaises(AIError) as result: runner.run('Review', REVIEW)
            self.assertTrue(result.exception.retryable)

if __name__ == '__main__': unittest.main()
