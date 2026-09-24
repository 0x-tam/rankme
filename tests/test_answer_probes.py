import unittest
from unittest.mock import Mock, patch

from rankme.answer_probes import ANSWER, probe
from rankme.ai import AIError


class AnswerProbeTests(unittest.TestCase):
    client = {'url': 'https://target.example/', 'name': 'Bright Solar', 'profile': {'secret': 'private-profile'},
              'connection': {'access_token': 'private-token'}}

    def runner(self, answer='General solar advice.', citations=None):
        return Mock(run=Mock(return_value={'answer': answer, 'citations': citations or []}))

    def test_prompt_is_question_only_no_target_context(self):
        runner = self.runner()
        result = probe(runner, self.client, ['How should panels be maintained?'])
        prompt = runner.run.call_args.args[0]
        for secret in ('target.example', 'Bright Solar', 'private-profile', 'private-token'):
            self.assertNotIn(secret, prompt)
        self.assertIn('How should panels be maintained?', prompt)
        self.assertEqual(runner.run.call_args.args[1], ANSWER)
        self.assertTrue(runner.run.call_args.kwargs['require_research'])
        self.assertTrue(runner.run.call_args.kwargs['search'])
        self.assertFalse(result['consumer_ai_measurement'])
        self.assertEqual(result['records'][0]['provider'], 'Codex web research')

    def test_actual_answer_mentions_brand_with_word_boundaries(self):
        for answer, expected in [('Try Bright Solar for guidance.', True), ('BRIGHT SOLAR is one provider.', True),
                                 ('Bright Solarium works.', False), ('NotBright Solar is another.', False)]:
            with self.subTest(answer=answer):
                result = probe(self.runner(answer), self.client, ['Panel advice?'])
                self.assertEqual(result['records'][0]['brand_mentioned'], expected)
        client = {**self.client, 'name': 'Solar+Co'}
        self.assertTrue(probe(self.runner('Solar+Co offers help.'), client, ['Advice?'])['records'][0]['brand_mentioned'])

    def test_verified_target_citation_and_no_gap(self):
        citations = [{'url': 'https://www.target.example/guide', 'title': 'Guide'},
                     {'url': 'https://publisher.example/advice', 'title': 'Other'}]
        def fetch(url, **kwargs):
            return {'url': url, 'status': 200, 'text': 'Public page'}
        with patch('rankme.answer_probes.fetch_public', side_effect=fetch):
            record = probe(self.runner(citations=citations), self.client, ['Advice?'])['records'][0]
        self.assertTrue(record['target_cited'])
        self.assertEqual(record['citation_gaps'], [])
        self.assertTrue(all(source['verified'] for source in record['citations']))

    def test_external_verified_sources_form_gaps_without_target(self):
        citations = [{'url': 'https://target.example.evil.example/guide', 'title': 'Spoof'},
                     {'url': 'https://publisher.example/article', 'title': 'Unavailable'}]
        def fetch(url, **kwargs):
            return {'url': url, 'status': 200 if 'evil' in url else 404, 'text': ''}
        with patch('rankme.answer_probes.fetch_public', side_effect=fetch):
            record = probe(self.runner(citations=citations), self.client, ['Advice?'])['records'][0]
        self.assertFalse(record['target_cited'])
        self.assertEqual(len(record['citation_gaps']), 1)
        self.assertFalse(record['citations'][1]['verified'])

    def test_unsafe_urls_do_not_fetch_and_fetch_errors_are_not_citations(self):
        citations = [{'url': 'http://127.0.0.1/admin', 'title': 'Private'},
                     {'url': 'javascript:alert(1)', 'title': 'Script'},
                     {'url': 'https://target.example/guide', 'title': 'Failed'}]
        with patch('rankme.answer_probes.fetch_public', side_effect=ValueError('Unsafe DNS')) as fetch:
            record = probe(self.runner(citations=citations), self.client, ['Advice?'])['records'][0]
        self.assertEqual(fetch.call_count, 1)
        self.assertFalse(record['target_cited'])
        self.assertEqual(record['citation_gaps'], [])

    def test_bounds_deduplication_and_invalid_inputs(self):
        runner = self.runner()
        self.assertEqual(len(probe(runner, self.client, ['One?', 'One?', 'Two?'])['records']), 2)
        self.assertEqual(runner.run.call_count, 2)
        for queries in ([], ['a'] * 4, ['x' * 1001], [42], 'question'):
            with self.assertRaises(ValueError):
                probe(runner, self.client, queries)
        excessive = self.runner(citations=[{'url': 'https://publisher.example/', 'title': ''}] * 6)
        with self.assertRaises(AIError):
            probe(excessive, self.client, ['Question?'])

    def test_redirect_is_classified_by_verified_destination(self):
        with patch('rankme.answer_probes.fetch_public', return_value={'url': 'https://publisher.example/', 'status': 200}):
            record = probe(self.runner(citations=[{'url': 'https://target.example/redirect', 'title': 'Redirect'}]),
                           self.client, ['Advice?'])['records'][0]
        self.assertFalse(record['target_cited'])
        self.assertEqual(len(record['citation_gaps']), 1)


if __name__ == '__main__':
    unittest.main()
