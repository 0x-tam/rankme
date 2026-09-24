import json
import unittest

from rankme.visibility import analyze


class VisibilityTests(unittest.TestCase):
    client = {'id': 'one', 'url': 'https://business.example/'}
    periods = {'current': {'start': '2026-08-01', 'end': '2026-08-28'},
               'previous': {'start': '2026-07-04', 'end': '2026-07-31'}}

    def page(self, path, title='Solar panel maintenance', **extra):
        return dict(url=self.client['url'] + path, title=title, description='Practical advice', links=[], **extra)

    def seo(self, **current):
        return {'periods': self.periods, 'search_console': {'current': current, 'previous': {}}}

    def test_empty_data_is_truthful_and_deterministic(self):
        first = analyze(self.client, [])
        self.assertEqual(first, analyze(self.client, []))
        self.assertEqual(first['opportunities'], [])
        self.assertEqual(len(first['capabilities']), 26)
        self.assertEqual(first['capabilities'][2]['status'], 'integration_required')
        self.assertTrue(first['missing_data'])
        json.dumps(first, allow_nan=False)

    def test_query_opportunity_brief_and_stable_identity(self):
        row = {'query': 'solar panel maintenance', 'impressions': 2000, 'clicks': 10, 'position': 8}
        data = self.seo(queries=[row])
        result = analyze(self.client, [self.page('care')], data)
        opportunity = result['keyword_opportunities'][0]
        self.assertEqual(opportunity['evidence']['ctr'], .005)
        self.assertIn('not expected traffic', opportunity['score_explanation'])
        self.assertEqual(result['briefs'][0]['existing_candidates'], ['https://business.example/care'])
        data['updated_at'] = '2026-09-01'
        self.assertEqual(opportunity['id'], analyze(self.client, [], data)['keyword_opportunities'][0]['id'])

    def test_no_join_of_independent_query_and_page_aggregates(self):
        data = self.seo(queries=[{'query': 'solar', 'impressions': 500}], pages=[{'page': self.client['url'] + 'a', 'impressions': 500}])
        self.assertEqual(analyze(self.client, [], data)['cannibalization'], [])
        data['search_console']['current']['query_pages'] = [
            {'query': 'solar maintenance', 'page': self.client['url'] + path, 'impressions': 100, 'position': 8}
            for path in ('a', 'b')]
        result = analyze(self.client, [], data)
        self.assertEqual(result['cannibalization'][0]['kind'], 'query_overlap')
        self.assertIn('harm', result['cannibalization'][0]['evidence']['note'])

    def test_crawl_metadata_internal_edges_and_lexical_overlap(self):
        pages = [self.page('a'), self.page('b')]
        pages[0]['description'] = ''
        result = analyze(self.client, pages)
        self.assertTrue(any(row['kind'] == 'missing_description' for row in result['technical_findings']))
        self.assertEqual(result['internal_links'][0]['evidence']['target_url'], pages[1]['url'])
        self.assertEqual(result['cannibalization'][0]['kind'], 'topic_overlap')
        self.assertTrue(result['topical_clusters'])
        pages[0]['links'] = [pages[1]['url']]
        self.assertEqual(analyze(self.client, pages)['internal_links'], [])

    def test_lost_links_require_last_seen_and_successful_missing_check(self):
        records = [{'source_url': 'https://publisher.example/article', 'client_id': 'one',
                    'last_seen_at': '2026-08-01', 'verification': {'status': 'missing', 'checked_at': '2026-08-03'}}]
        result = analyze(self.client, [], backlinks=records)
        self.assertEqual(result['link_opportunities'][0]['kind'], 'lost_link')
        records[0]['verification']['status'] = 'error'
        self.assertEqual(analyze(self.client, [], backlinks=records)['link_opportunities'], [])
        records[0]['verification']['status'] = 'missing'
        del records[0]['last_seen_at']
        self.assertEqual(analyze(self.client, [], backlinks=records)['link_opportunities'], [])

    def test_decline_requires_comparable_periods(self):
        url = self.client['url'] + 'care'
        data = self.seo(pages=[{'page': url, 'clicks': 5}])
        data['search_console']['previous'] = {'pages': [{'page': url, 'clicks': 50}]}
        self.assertEqual(analyze(self.client, [], data)['refresh_candidates'][0]['evidence']['change_percent'], -90)
        data['periods'] = {}
        self.assertEqual(analyze(self.client, [], data)['refresh_candidates'], [])

    def test_missing_metrics_never_become_zero_ctr(self):
        data = self.seo(queries=[{'query': 'solar panels', 'impressions': 500, 'position': 5},
                               {'query': 'solar cost', 'impressions': float('nan'), 'clicks': 0, 'position': 5}])
        self.assertEqual(analyze(self.client, [], data)['keyword_opportunities'], [])

    def test_tenant_filter_and_safe_link_schemes(self):
        data = self.seo(queries=[{'query': 'private data', 'impressions': 500, 'clicks': 0, 'position': 8}])
        data['client_id'] = 'other'
        records = [{'client_id': 'other', 'source_url': 'https://publisher.example/', 'last_seen_at': '2026-08-01', 'verification': {'status': 'missing'}}]
        result = analyze(self.client, [{'url': 'javascript:alert(1)'}, {'url': 'https://other.example/'}], data, records)
        self.assertEqual(result['opportunities'], [])

    def test_bounded_output(self):
        data = self.seo(queries=[{'query': 'query ' + str(i), 'impressions': 500, 'clicks': 0, 'position': 8} for i in range(6000)])
        result = analyze(self.client, [], data)
        for value in result.values():
            if isinstance(value, list):
                self.assertLessEqual(len(value), 100)


if __name__ == '__main__':
    unittest.main()
