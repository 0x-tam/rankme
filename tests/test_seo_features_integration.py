import base64
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from rankme.cloud_bridge import allowed_command, sanitize_snapshot
from rankme.evidence_audit import build_audit
from rankme.seo_features import confirmed_experience
from rankme.server import Application


class SeoFeaturesIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = Application(self.temp.name)
        self.addCleanup(self.app.store.close)
        self.client = self.app.store.put('clients', {'url': 'https://example.com', 'name': 'Example',
            'confirmed': True, 'profile': {'name': 'Example'}, 'status': 'ready'})
        self.other = self.app.store.put('clients', {'url': 'https://another.example', 'name': 'Other'})

    def test_experience_needs_confirmation_and_edits_reset_it(self):
        path = f"/api/clients/{self.client['id']}/experience"
        row = self.app.dispatch('POST', path, {'claim': 'We completed thirty service visits last month.',
            'source_note': 'Owner checked the service log', 'confirmed': True})
        self.assertTrue(row['brief_eligible'])
        self.assertEqual(len(confirmed_experience(self.app.store, self.client['id'])), 1)
        self.assertEqual(confirmed_experience(self.app.store, self.other['id']), [])
        edited = self.app.dispatch('PATCH', f"/api/experience/{row['id']}/edit",
            {'claim': 'We completed forty service visits last month.', 'confirmed': True})
        self.assertFalse(edited['user_confirmed'])
        self.assertFalse(edited['brief_eligible'])
        self.assertEqual(confirmed_experience(self.app.store, self.client['id']), [])

    def test_experience_library_is_bounded_per_client(self):
        path = f"/api/clients/{self.client['id']}/experience"
        for index in range(20):
            self.app.dispatch('POST', path, {'claim': f'We completed service visit number {index} last month.',
                'source_note': 'Owner checked the service log', 'confirmed': False})
        with self.assertRaisesRegex(ValueError, 'up to 20'):
            self.app.dispatch('POST', path, {'claim': 'We completed another service visit last month.',
                'source_note': 'Owner checked the service log', 'confirmed': False})
        self.assertEqual(len([r for r in self.app.store.all('experience_claims') if r['client_id'] == self.client['id']]), 20)

    def test_signed_or_private_experience_source_cannot_enter_article_context(self):
        from rankme.evidence_audit import validate_experience_entry
        path = f"/api/clients/{self.client['id']}/experience"
        for source in ('https://example.com/file?X-Amz-Signature=secret',
                       'https://example.com/file?key=secret',
                       'https://127.0.0.1/private'):
            with self.subTest(source=source), self.assertRaises(ValueError):
                self.app.dispatch('POST', path, {'claim': 'We serviced this roof last month.',
                    'source_url': source, 'confirmed': True})
        stale = validate_experience_entry({'facts': ['We serviced this roof last month.'],
            'source': {'type': 'public_url', 'url': 'https://example.com/file?sig=secret'},
            'user_confirmed': True})
        self.assertFalse(stale['brief_eligible'])
        self.assertEqual(stale['source']['type'], 'rejected_url')

    def test_audit_download_is_bounded_and_binary_exact(self):
        audit = build_audit(self.client, {'pages': [{'url': 'https://example.com/', 'title': 'Example'}]})
        self.app.store.put('evidence_audits', {'id': self.client['id'], 'client_id': self.client['id'], 'audit': audit})
        for fmt in ('md', 'pdf', 'xlsx'):
            payload = b'\x00RankMe-report-\xff-' + fmt.encode()
            with patch('rankme.evidence_audit.render_report', return_value=payload):
                result = self.app.dispatch('POST', f"/api/clients/{self.client['id']}/evidence-report", {'format': fmt})
            self.assertEqual(base64.b64decode(result['base64']), payload)
            self.assertLess(len(result['base64']), 400_000)
            self.assertEqual(sanitize_snapshot(result)['base64'], result['base64'])
        with self.assertRaises(KeyError):
            self.app.dispatch('POST', f"/api/clients/{self.other['id']}/evidence-report", {'format': 'pdf'})

    def test_audit_date_tracks_fresh_crawl_not_older_search_data(self):
        crawl_time = '2026-10-07T12:00:00Z'
        search_time = '2026-09-01T12:00:00Z'
        audit = build_audit(self.client, {'observed_at': crawl_time,
            'pages': [{'url': 'https://example.com/', 'title': 'Example'}]},
            seo={'client_id': self.client['id'], 'updated_at': search_time,
                 'search_console': {'current': {'queries': []}}})
        self.assertEqual(audit['observed_at'], crawl_time)
        self.assertEqual(audit['sources']['crawl']['observed_at'], crawl_time)
        self.assertEqual(audit['sources']['search_console']['observed_at'], search_time)

    def test_failed_observation_cleanup_leaves_client_available_for_retry(self):
        from rankme.ai_overviews import AIOCollector
        with patch.object(AIOCollector, 'delete_client', side_effect=OSError('history database unavailable')):
            with self.assertRaisesRegex(OSError, 'history database unavailable'):
                self.app.dispatch('POST', f"/api/clients/{self.client['id']}/remove", {'confirm': 'example.com'})
        self.assertEqual(self.app.store.get('clients', self.client['id'])['url'], 'https://example.com')

    def test_provider_configuration_stays_off_and_cloud_commands_are_narrow(self):
        ident = self.client['id']
        self.assertEqual(self.app.engine.state()['aio_status']['cost'], 'free')
        config = {'enabled': False, 'interval_days': 1, 'queries': ['solar maintenance'],
                  'country': 'us', 'language': 'en', 'location': '', 'device': 'desktop'}
        self.app.dispatch('PATCH', f'/api/clients/{ident}', {'aio_settings': config})
        summary = self.app.engine.state()['aio_summaries'][0]['summary']
        self.assertEqual(summary['sample_days'], 0)
        self.assertIsNone(summary['appearance_rate'])
        history = self.app.dispatch('POST', f'/api/clients/{ident}/aio-history', {'query': 'solar maintenance'})
        self.assertIn(b'"observations": []', base64.b64decode(history['base64']))
        with self.assertRaises(ValueError):
            self.app.dispatch('POST', f"/api/clients/{self.other['id']}/aio-history", {'query': 'solar maintenance'})
        for action in ('aio-collect', 'evidence-audit'):
            self.assertTrue(allowed_command('POST', f'/api/clients/{ident}/{action}', {}))
            self.assertFalse(allowed_command('POST', f'/api/clients/{ident}/{action}', {'force': True}))
        self.assertTrue(allowed_command('POST', f'/api/clients/{ident}/evidence-report', {'format': 'pdf'}))
        self.assertTrue(allowed_command('POST', f'/api/clients/{ident}/aio-history', {'query': 'solar maintenance'}))
        self.assertFalse(allowed_command('POST', f'/api/clients/{ident}/evidence-report', {'format': 'pdf', 'path': '/tmp'}))
        self.assertFalse(allowed_command('PATCH', f'/api/clients/{ident}', {'aio_settings': {'api_key': 'secret'}}))

    def test_seven_observed_days_create_a_source_review_task(self):
        from rankme.ai_overviews import AIOCollector
        from rankme.autopilot import rebuild, queue_action
        ident = self.client['id']
        config = {'enabled': False, 'interval_days': 1, 'queries': ['solar maintenance'],
                  'country': 'us', 'language': 'en', 'location': '', 'device': 'desktop'}
        self.app.dispatch('PATCH', f'/api/clients/{ident}', {'aio_settings': config})
        self.app.store.update('clients', ident, crawl={'pages': [{'url': 'https://example.com/solar',
            'title': 'Solar maintenance basics', 'text': 'Basic care'}]})
        collector = AIOCollector(self.temp.name)
        for offset in range(6, -1, -1):
            observed_at = (datetime.now(timezone.utc) - timedelta(days=offset)).replace(microsecond=0).isoformat()
            collector.import_observation(ident, {'query': 'solar maintenance', 'country': 'us', 'language': 'en',
                'location': '', 'device': 'desktop', 'observed_at': observed_at, 'status': 'present',
                'answer': 'Clean panels safely and inspect connections.',
                'citations': [{'url': 'https://www.example.org/solar-guide', 'title': 'Solar guide'}],
                'evidence_note': 'Owner recorded a Google Search result'})
        report = rebuild(self.app.engine, ident)
        item = next(o for o in self.app.store.all('opportunities') if o.get('kind') == 'aio_cited_source_review')
        self.assertEqual(item['execution_mode'], 'task')
        self.assertTrue(item['executable'])
        job = queue_action(self.app.engine, item['id'])
        with patch('rankme.crawler.crawl_site', return_value={'pages': [{'url': 'https://www.example.org/solar-guide',
                'title': 'Detailed solar care', 'description': 'Inspect and clean panels',
                'headings': [{'text': 'Inspection'}], 'text': 'Inspect wiring; clean safely.'}]}):
            self.app.engine.execute(job)
        self.assertEqual(self.app.store.get('jobs', job['id'])['status'], 'completed')
        task = self.app.store.get('tasks', self.app.store.get('opportunities', item['id'])['task_id'])
        self.assertIn('Detailed solar care', task['body'])
        self.assertIn('Solar maintenance basics', task['body'])


if __name__ == '__main__':
    unittest.main()
