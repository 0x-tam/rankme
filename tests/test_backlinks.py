import unittest
from unittest.mock import Mock, patch

from rankme.backlinks import discover_prospects, verify_backlink


class BacklinkTests(unittest.TestCase):
    def page(self, text, **fields):
        return dict({'url': 'https://publisher.example/resources', 'status': 200, 'content_type': 'text/html', 'text': text}, **fields)

    def test_actual_href_and_rel_not_text_mentions(self):
        page = self.page('client.example <a href="https://evilclient.example">Wrong</a><a href="https://www.client.example/guide" rel="nofollow sponsored UGC">Useful guide</a>')
        with patch('rankme.backlinks.fetch_public', return_value=page):
            result = verify_backlink(page['url'], 'https://client.example')
        self.assertEqual(result['status'], 'live')
        self.assertEqual(len(result['links']), 1)
        self.assertEqual(result['links'][0]['rel'], ['nofollow', 'sponsored', 'ugc'])
        self.assertEqual(result['links'][0]['anchor'], 'Useful guide')
        self.assertTrue(result['links'][0]['nofollow'])
        self.assertIn('+00:00', result['checked_at'])

    def test_mentions_scripts_and_spoofed_hosts_are_missing(self):
        page = self.page('<script>"<a href=\"https://client.example\">Fake</a>"</script><a href="https://client.example.evil.example">Fake</a> client.example')
        with patch('rankme.backlinks.fetch_public', return_value=page):
            result = verify_backlink(page['url'], 'https://client.example')
        self.assertEqual(result['status'], 'missing')
        self.assertEqual(result['links'], [])

    def test_base_and_protocol_relative_hrefs(self):
        page = self.page('<base href="https://client.example/"><a href="guides">Guide</a><a href="//client.example/about">About</a>')
        with patch('rankme.backlinks.fetch_public', return_value=page):
            result = verify_backlink(page['url'], 'https://client.example')
        self.assertEqual(len(result['links']), 2)
        self.assertEqual(result['links'][0]['url'], 'https://client.example/guides')

    def test_private_urls_and_credentials_are_not_fetched(self):
        with patch('rankme.backlinks.fetch_public') as fetch:
            for source in ('http://127.0.0.1/x', 'https://user:SECRET@publisher.example/'):
                result = verify_backlink(source, 'https://client.example')
                self.assertEqual(result['status'], 'error')
                self.assertNotIn('SECRET', str(result))
            fetch.assert_not_called()

    def test_unavailable_or_internal_source_is_error(self):
        with patch('rankme.backlinks.fetch_public', return_value=self.page('No page', status=404)):
            self.assertEqual(verify_backlink('https://publisher.example', 'https://client.example')['status'], 'error')
        with patch('rankme.backlinks.fetch_public') as fetch:
            self.assertEqual(verify_backlink('https://client.example/about', 'https://client.example')['status'], 'error')
            fetch.assert_not_called()

    def prospect(self, **fields):
        return dict({'name': 'Publisher', 'source_url': 'https://publisher.example/resources', 'reason': 'Offers useful industry resources.',
                     'contact_url': 'https://publisher.example/contact', 'draft': 'Hello, our guide may help your readers. [Sender name]',
                     'evidence_quote': 'We curate useful industry resources for our readers.'}, **fields)

    def test_discovery_verifies_evidence_and_keeps_draft_only(self):
        runner = Mock()
        runner.run.return_value = {'prospects': [self.prospect()]}
        page = self.page('<p>We curate useful industry resources for our readers.</p><a href="/contact">Contact</a>')
        client = {'url': 'https://client.example', 'name': 'Client', 'profile': {}, 'connection': {'password': 'PRIVATE_SECRET'}}
        with patch('rankme.backlinks.fetch_public', return_value=page):
            result = discover_prospects(runner, client)
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0]['evidence']['verified'])
        self.assertEqual(result[0]['status'], 'prospect')
        self.assertEqual(result[0]['contact_url'], 'https://publisher.example/contact')
        self.assertIn('[Sender name]', result[0]['draft'])
        self.assertNotIn('PRIVATE_SECRET', runner.run.call_args.args[0])
        self.assertTrue(runner.run.call_args.kwargs['require_research'])

    def test_unverified_evidence_and_contact_are_flagged(self):
        runner = Mock()
        runner.run.return_value = {'prospects': [self.prospect()]}
        with patch('rankme.backlinks.fetch_public', return_value=self.page('Different page content')):
            result = discover_prospects(runner, {'url': 'https://client.example'})
        self.assertEqual(result[0]['status'], 'needs_review')
        self.assertFalse(result[0]['evidence']['verified'])
        self.assertEqual(result[0]['contact_url'], '')

    def test_discovery_deduplicates_and_bounds_fetches(self):
        runner = Mock()
        runner.run.return_value = {'prospects': [self.prospect()] * 20}
        with patch('rankme.backlinks.fetch_public', return_value=self.page('We curate useful industry resources for our readers.')) as fetch:
            result = discover_prospects(runner, {'url': 'https://client.example'})
        self.assertEqual(len(result), 1)
        self.assertEqual(fetch.call_count, 1)

    def test_failed_discovery_page_never_becomes_verified(self):
        runner = Mock()
        runner.run.return_value = {'prospects': [self.prospect()]}
        with patch('rankme.backlinks.fetch_public', side_effect=ValueError('Blocked private redirect')):
            self.assertEqual(discover_prospects(runner, {'url': 'https://client.example'}), [])


if __name__ == '__main__':
    unittest.main()
