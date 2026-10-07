import io
import json
import os
from pathlib import Path
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile
from unittest.mock import patch

from rankme.evidence_audit import (build_audit, fetch_page_speed, parse_page_speed,
                                   render_report, enrich_actions_with_jev,
                                   validate_experience_claim,
                                   validate_experience_entry, validate_summary)


class EvidenceAuditTests(unittest.TestCase):
    client = {'id': 'one', 'url': 'https://example.com/'}

    def seo(self, query_pages=None, limited=False):
        return {'client_id': 'one', 'updated_at': '2026-08-28T12:00:00Z',
                'periods': {'current': {'start': '2026-08-01', 'end': '2026-08-28'}},
                'search_console': {'limited': limited, 'current': {
                    'queries': [
                        {'query': 'solar maintenance', 'clicks': 4, 'impressions': 400, 'position': 8},
                        {'query': 'battery warranty', 'clicks': 1, 'impressions': 200, 'position': 12},
                        {'query': 'solar cost', 'clicks': 0, 'impressions': 300, 'position': 10}],
                    'query_pages': query_pages if query_pages is not None else []}}}

    def test_existing_landing_page_takes_priority_over_gap(self):
        crawl = {'pages': [{'url': 'https://example.com/solar-care',
                            'title': 'Solar maintenance', 'description': 'How to care for panels',
                            'links': []}], 'summary': {'limited': True}}
        joint = [{'query': 'solar maintenance', 'page': 'https://example.com/solar-care',
                  'clicks': 4, 'impressions': 350, 'position': 8}]
        audit = build_audit(self.client, crawl, self.seo(joint, limited=True))
        self.assertEqual([a['kind'] for a in audit['actions']],
                         ['gsc_leak', 'gsc_gap_candidate', 'gsc_gap_candidate'])
        leader = audit['actions'][0]
        self.assertEqual(leader['existing_url'], 'https://example.com/solar-care')
        self.assertEqual(leader['evidence']['measurements']['impressions'], 400)
        self.assertEqual(leader['evidence']['joint_query_page_rows'][0]['impressions'], 350)
        self.assertEqual(leader['status'], 'needs_verification')
        self.assertFalse(leader['automation_eligible'])
        self.assertIn('truncated', ' '.join(audit['missing_data']))
        self.assertIn('not market search volume', ' '.join(leader['evidence']['caveats']))
        self.assertEqual(leader['id'], build_audit(self.client, crawl, self.seo(joint))['actions'][0]['id'])
        json.dumps(audit, allow_nan=False)

    def test_title_overlap_routes_to_existing_coverage(self):
        crawl = {'pages': [{'url': 'https://example.com/battery', 'title': 'Battery warranty guide'}],
                 'summary': {'limited': False}}
        audit = build_audit(self.client, crawl, self.seo())
        row = next(a for a in audit['actions'] if a['query'] == 'battery warranty')
        self.assertEqual(row['kind'], 'gsc_coverage_review')
        self.assertEqual(row['existing_url'], 'https://example.com/battery')

    def test_missing_joint_rows_do_not_become_a_content_gap(self):
        seo = self.seo()
        del seo['search_console']['current']['query_pages']
        audit = build_audit(self.client, [], seo)
        self.assertTrue(all(row['kind'] == 'gsc_attribution_review' for row in audit['actions']))

    def test_technical_checks_and_unknown_fields(self):
        crawl = {'pages': [
            {'url': 'https://example.com/a', 'title': 'Same', 'description': 'Same',
             'status': 404, 'robots': ['noindex, follow'], 'canonical': 'https://example.com/b',
             'anchors': [{'text': ''}], 'schema': [], 'links': []},
            {'url': 'https://example.com/b', 'title': 'Same', 'description': 'Same', 'links': []}],
            'summary': {'limited': False}}
        audit = build_audit(self.client, crawl)
        kinds = {item['kind'] for item in audit['technical_findings']}
        self.assertTrue({'http_error', 'noindex', 'canonical_mismatch', 'duplicate_title',
                         'duplicate_description', 'empty_link_text', 'missing_schema'} <= kinds)
        self.assertIn('Search Console data unavailable', ' '.join(audit['missing_data']))
        self.assertFalse(audit['actions'])
        self.assertNotIn('field_lcp_ms', audit['page_speed']['measurements'])

    def test_pagespeed_measured_only_and_public_endpoint(self):
        payload = {'lighthouseResult': {'categories': {'performance': {'score': .85}},
                                       'audits': {'largest-contentful-paint': {'numericValue': 2100}}},
                   'loadingExperience': {'metrics': {'INTERACTION_TO_NEXT_PAINT': {'percentile': 220}}}}
        seen = []
        def fetch(url, **kwargs):
            seen.append((url, kwargs))
            return {'text': json.dumps(payload)}
        with patch('rankme.evidence_audit._addresses', return_value=[('public',)]):
            result = fetch_page_speed('https://example.com/', fetcher=fetch)
        self.assertEqual(result['measurements']['lab_performance_score'], 85)
        self.assertEqual(result['measurements']['field_inp_ms'], 220)
        self.assertNotIn('field_cls', result['measurements'])
        self.assertEqual(seen[0][1]['allowed_hosts'], {'www.googleapis.com'})
        with self.assertRaises(ValueError):
            fetch_page_speed('http://127.0.0.1/', fetcher=fetch)
        self.assertEqual(parse_page_speed({})['status'], 'unavailable')

    def test_experience_requires_explicit_confirmation(self):
        row = validate_experience_claim('Cut replacement time by two days',
                                        'https://example.com/case-study', self.client['url'])
        self.assertEqual(row['verification_status'], 'unverified')
        self.assertFalse(row['brief_eligible'])
        confirmed = validate_experience_entry({**row, 'user_confirmed': True})
        self.assertTrue(confirmed['brief_eligible'])
        self.assertEqual(confirmed['source_verification_status'], 'unverified')
        audit = build_audit(self.client, [], experience=[row, {**row, 'user_confirmed': True}])
        self.assertEqual(len(audit['verified_brief_facts']), 1)
        with self.assertRaises(ValueError):
            validate_experience_claim('Fact', 'http://localhost/private', self.client['url'])

    def test_summary_and_export_safety(self):
        seo = self.seo([{'query': 'solar maintenance', 'page': 'https://example.com/a',
                         'clicks': 4, 'impressions': 400, 'position': 8}])
        audit = build_audit(self.client, {'pages': []}, seo)
        action = audit['actions'][0]
        self.assertTrue(validate_summary({'action_ids': [action['id']], 'numeric_claims': [
            {'action_id': action['id'], 'metric': 'impressions', 'value': 400}],
            'text': 'Review observed demand.'}, audit))
        for summary in ({'action_ids': ['unknown']},
                        {'text': 'Expected traffic 1000'},
                        {'numeric_claims': [{'action_id': action['id'], 'metric': 'impressions', 'value': 1000}]}):
            with self.assertRaises(ValueError):
                validate_summary(summary, audit)
        audit['actions'][0]['title'] = '=HYPERLINK("https://evil.example")'
        markdown = render_report(audit, 'md')
        pdf = render_report(audit, 'pdf')
        xlsx = render_report(audit, 'xlsx')
        self.assertIn(action['id'].encode(), markdown)
        self.assertTrue(pdf.startswith(b'%PDF-1.4'))
        self.assertIn(b'%%EOF', pdf[-20:])
        with zipfile.ZipFile(io.BytesIO(xlsx)) as book:
            root = ET.fromstring(book.read('xl/worksheets/sheet1.xml'))
            content = ''.join(root.itertext())
            self.assertIn("'=HYPERLINK", content)
            self.assertIn(action['id'], content)
            self.assertNotIn('<f>', book.read('xl/worksheets/sheet1.xml').decode())
        with self.assertRaises(ValueError):
            render_report(audit, 'csv')

    def test_opt_in_jev_uses_typed_bounded_questions_and_fails_closed(self):
        audit = build_audit(self.client, [], self.seo())
        disabled = enrich_actions_with_jev(audit)
        self.assertEqual(disabled['semantic_assessment']['status'], 'disabled')
        self.assertNotIn('semantic_judgment', audit['actions'][0])
        calls = []
        def evaluate(state, questions):
            calls.append((state, questions))
            answers = {}
            for index in range(len(state['actions'])):
                answers[f'intent_{index}'] = {'type': 'choice', 'choice': 'informational', 'confidence': .8,
                    'probabilities': {'informational': .84, 'commercial': .04,
                                      'transactional': .04, 'navigational': .04, 'unclear': .04}}
                answers[f'evidence_quality_{index}'] = {'type': 'choice', 'choice': 'limited', 'confidence': .7,
                    'probabilities': {'adequate_for_review': .1, 'limited': .8, 'insufficient': .1}}
            return {'model': 'jev-test', 'answers': answers}
        result = enrich_actions_with_jev(audit, enabled=True, evaluator=evaluate, max_actions=2)
        self.assertEqual(len(calls), 1)
        self.assertEqual(set(calls[0][1]), {'intent_0', 'evidence_quality_0', 'intent_1', 'evidence_quality_1'})
        self.assertEqual(result['actions'][0]['semantic_judgment']['intent']['choice'], 'informational')
        self.assertEqual(result['actions'][0]['status'], audit['actions'][0]['status'])
        self.assertFalse(result['actions'][0]['automation_eligible'])
        self.assertEqual(result['actions'][2]['semantic_judgment']['status'], 'unassessed')
        failure = enrich_actions_with_jev(audit, enabled=True, evaluator=lambda *_: {'answers': {}}, max_actions=1)
        self.assertEqual(failure['actions'][0]['semantic_judgment']['status'], 'unavailable')
        self.assertNotIn('intent', failure['actions'][0]['semantic_judgment'])

    def test_crawl_parser_collects_anchor_and_json_ld_observations(self):
        from rankme.crawler import _Page
        parser = _Page('https://example.com/')
        parser.feed('<a href="/service"><img alt="Service"> details</a>'
                    '<script type="application/ld+json">{"@type":"Service"}</script>'
                    '<script type="application/ld+json">{broken}</script>')
        self.assertEqual(parser.anchors[0]['text'], 'Service details')
        self.assertEqual(parser.schema[0], {'valid_json': True, 'types': ['Service']})
        self.assertEqual(parser.schema[1]['valid_json'], False)

    def test_fresh_crawl_timestamp_and_credential_file_rules(self):
        from rankme.evidence_audit import _typesafe_key
        client = {**self.client, 'crawl_observed_at': '2026-01-01T00:00:00Z'}
        audit = build_audit(client, {'pages': [], 'observed_at': '2026-08-01T00:00:00Z'})
        self.assertEqual(audit['sources']['crawl']['observed_at'], '2026-08-01T00:00:00Z')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / '.config' / 'rankme'
            config.mkdir(parents=True)
            target = config / 'typesafe.env'
            target.write_text("export TYPESAFE_API_KEY='test_only_token'\n")
            target.chmod(0o600)
            with patch.dict(os.environ, {'TYPESAFE_API_KEY': ''}), patch('rankme.evidence_audit.Path.home', return_value=root):
                self.assertEqual(_typesafe_key(), 'test_only_token')
                target.chmod(0o644)
                with self.assertRaises(ValueError):
                    _typesafe_key()
                target.chmod(0o600)
                link = root / 'linked'
                os.link(target, link)
                with self.assertRaises(ValueError):
                    _typesafe_key()


if __name__ == '__main__':
    unittest.main()
