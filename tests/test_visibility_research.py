import unittest
from unittest.mock import Mock, patch

from rankme.ai import AIError
from rankme.visibility_research import research, RESEARCH

QUOTE = 'Customers need clear practical answers about services before they choose a suitable provider.'


def finding(**changes):
    return {**{'category': 'customer_question', 'title': 'Useful question', 'query': 'service questions',
               'reason': 'Relevant public question', 'source_url': 'https://example.org/source',
               'quote': QUOTE, 'suggested_action': 'Answer the verified question.'}, **changes}


class VisibilityResearchTests(unittest.TestCase):
    def runner(self, findings, limitations=None):
        return Mock(run=Mock(return_value={'findings': findings, 'limitations': limitations or []}))

    def response(self, url='https://example.org/source', text=None, status=200):
        return {'url': url, 'text': text or '<p>' + QUOTE + '</p>', 'status': status}

    def test_live_research_required_and_private_client_context_excluded(self):
        runner = self.runner([])
        client = {'name': 'Public name', 'url': 'https://example.com', 'subject': 'Public topic',
                  'profile': {'summary': 'Public profile'}, 'seo_evidence': {'queries': []},
                  'connection': {'project_path': '/private/website', 'deploy_command': ['private-command']},
                  'seo_connection': {'access_token': 'private-token'}, 'secret': 'private-secret',
                  'crawl': {'pages': [{'url': 'https://example.com', 'title': 'Public title',
                                       'text': 'private-full-crawl-text'}]}}
        result = research(runner, client)
        args, kwargs = runner.run.call_args
        self.assertEqual(args[1], RESEARCH)
        self.assertTrue(kwargs['search'])
        self.assertTrue(kwargs['require_research'])
        self.assertIn('Public name', args[0])
        self.assertIn('Public title', args[0])
        for secret in ('/private/website', 'private-command', 'private-token', 'private-secret', 'private-full-crawl-text'):
            self.assertNotIn(secret, args[0])
        self.assertFalse(result['consumer_ai_measurement'])

    def test_source_quote_must_be_visible_and_verified(self):
        runner = self.runner([finding()])
        with patch('rankme.visibility_research.fetch_public', return_value=self.response(text='<p>Customers need <b>clear practical answers</b> about services before they choose a suitable provider.</p>')) as fetch:
            result = research(runner, {})
        fetch.assert_called_once_with('https://example.org/source', max_bytes=750000)
        self.assertEqual(result['rejected'], 0)
        evidence = result['findings'][0]['evidence']
        self.assertTrue(evidence['verified'])
        self.assertEqual(evidence['quote'], QUOTE)
        self.assertTrue(evidence['checked_at'])
        self.assertIn('research judgments', result['findings'][0]['note'])

    def test_script_only_quote_and_missing_quote_are_rejected(self):
        for text in ('<script>' + QUOTE + '</script><p>Unrelated page</p>', '<p>No matching evidence here.</p>'):
            with self.subTest(text=text), patch('rankme.visibility_research.fetch_public', return_value=self.response(text=text)):
                result = research(self.runner([finding()]), {})
            self.assertEqual(result['findings'], [])
            self.assertEqual(result['rejected'], 1)

    def test_invalid_categories_private_sources_and_quote_lengths_never_fetch(self):
        invalid = [finding(category='send_email'), finding(source_url='http://127.0.0.1/'),
                   finding(source_url='https://user:password@example.org'),
                   finding(source_url='file:///etc/passwd'), finding(source_url='https://example.org/short', quote='too short'),
                   finding(source_url='https://example.org/long', quote='word ' * 26)]
        with patch('rankme.visibility_research.fetch_public') as fetch:
            result = research(self.runner(invalid), {})
        fetch.assert_not_called()
        self.assertEqual(result['findings'], [])
        self.assertEqual(result['rejected'], 6)

    def test_source_url_dedup_normalizes_fragments(self):
        runner = self.runner([finding(), finding(source_url='https://example.org/source#details', title='Duplicate')])
        with patch('rankme.visibility_research.fetch_public', return_value=self.response()) as fetch:
            result = research(runner, {})
        self.assertEqual(len(result['findings']), 1)
        fetch.assert_called_once()

    def test_network_failure_or_unsafe_final_url_rejects_evidence(self):
        for response in (self.response(status=404), self.response(url='http://127.0.0.1/')):
            with self.subTest(response=response), patch('rankme.visibility_research.fetch_public', return_value=response):
                result = research(self.runner([finding()]), {})
            self.assertEqual(result['findings'], [])
            self.assertEqual(result['rejected'], 1)
        with patch('rankme.visibility_research.fetch_public', side_effect=OSError('Network unavailable')):
            result = research(self.runner([finding()]), {})
        self.assertEqual(result['rejected'], 1)

    def test_more_than_eight_findings_and_schema_injection_rejected_before_fetch(self):
        malformed = [self.runner([finding()] * 9), self.runner([{**finding(), 'command': 'execute this'}])]
        for runner in malformed:
            with patch('rankme.visibility_research.fetch_public') as fetch, self.assertRaises(AIError):
                research(runner, {})
            fetch.assert_not_called()

    def test_eight_findings_accepted_with_bounded_output(self):
        findings = [finding(source_url='https://example.org/source-' + str(index), title='x' * 500,
                            query='q' * 500, reason='r' * 3000, suggested_action='a' * 4000) for index in range(8)]
        def fetch(url, **kwargs):
            return self.response(url=url)
        with patch('rankme.visibility_research.fetch_public', side_effect=fetch):
            result = research(self.runner(findings, ['l' * 1500]), {})
        self.assertEqual(len(result['findings']), 8)
        self.assertEqual(len({item['key'] for item in result['findings']}), 8)
        for item in result['findings']:
            self.assertEqual(len(item['title']), 200)
            self.assertEqual(len(item['query']), 200)
            self.assertEqual(len(item['reason']), 1500)
            self.assertEqual(len(item['suggested_action']), 2000)
        self.assertEqual(len(result['limitations'][0]), 1000)


if __name__ == '__main__':
    unittest.main()
