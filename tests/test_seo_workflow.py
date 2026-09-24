"""Integration checks for data jobs, persistence, and configuration boundaries."""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch
from rankme.server import Application


class DataWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = Application(Path(self.tmp.name))
        self.store, self.engine = self.app.store, self.app.engine
        self.engine.google = Mock()
        self.engine.google.status.return_value = {'configured': True, 'connected': True}
        self.client = self.store.put('clients', {'id': 'client', 'url': 'https://example.com', 'name': 'Example',
            'profile': {'summary': 'Example'}, 'confirmed': True, 'automation': False, 'status': 'ready',
            'subject': 'Advice', 'seo_connection': {'site_url': 'sc-domain:example.com'}})

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_sync_and_backup_include_metrics_not_credentials(self):
        self.engine.google.sync.return_value = {'search_console': {'current': {'totals': {'clicks': 20}}}}
        job = self.app.dispatch('POST', '/api/clients/client/seo-sync', {})
        self.engine.execute(job)
        self.assertEqual(self.store.get('jobs', job['id'])['status'], 'completed')
        self.assertEqual(self.store.get('seo', 'client')['search_console']['current']['totals']['clicks'], 20)
        self.assertIn('seo', self.store.backup())
        self.assertNotIn('google', self.store.backup())

    def test_sync_failure_preserves_previous_metrics(self):
        self.store.put('seo', {'id': 'client', 'client_id': 'client', 'marker': 'previous'})
        self.engine.google.sync.side_effect = ValueError('Reconnect Google')
        self.engine.execute(self.engine.queue('seo-sync', 'client'))
        self.assertEqual(self.store.get('seo', 'client')['marker'], 'previous')
        self.assertEqual(self.store.get('clients', 'client')['status'], 'ready')
        self.assertEqual(self.store.get('clients', 'client')['seo_error'], 'Reconnect Google')

    def test_daily_sync_throttled_after_failure(self):
        self.engine.google.sync.side_effect = ValueError('Reconnect Google')
        self.engine.data_schedule_tick()
        jobs = self.store.all('jobs')
        self.engine.execute(jobs[0])
        self.engine.data_schedule_tick()
        self.assertEqual(len(self.store.all('jobs')), 1)

    def test_pause_prevents_background_data_jobs(self):
        self.store.settings({'paused': True})
        self.engine.data_schedule_tick()
        self.assertEqual(self.store.all('jobs'), [])

    def test_configuration_validation(self):
        for bad in ('https://user:secret@example.com', 'file:///tmp/local'):
            with self.assertRaises(ValueError):
                self.app.update_client('client', {'seo_connection': {'site_url': bad}})
        with self.assertRaises(ValueError):
            self.app.update_client('client', {'seo_connection': {'ga4_property': '../secret'}})

    def test_backlink_research_deduplicates_and_never_sends(self):
        prospect = {'source_url': 'https://directory.example/listing', 'draft': 'Hello', 'status': 'prospect'}
        with patch('rankme.backlinks.discover_prospects', return_value=[prospect, prospect]):
            self.engine.execute(self.engine.queue('backlinks-discover', 'client'))
        links = self.store.all('backlinks')
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]['status'], 'prospect')
        self.assertEqual(links[0]['draft'], 'Hello')

    def test_retry_retains_link_id(self):
        link = self.store.put('backlinks', {'client_id': 'client', 'source_url': 'https://directory.example/listing'})
        job = self.engine.queue('backlink-check', 'client', backlink_id=link['id'])
        self.store.update('jobs', job['id'], status='failed')
        retried = self.engine.retry(job['id'])
        self.assertEqual(retried['backlink_id'], link['id'])

    def test_different_link_does_not_clear_failed_check(self):
        failed = self.engine.queue('backlink-check', 'client', backlink_id='first')
        self.store.update('jobs', failed['id'], status='failed')
        self.engine.queue('backlink-check', 'client', backlink_id='second')
        self.assertEqual(self.store.get('jobs', failed['id'])['status'], 'failed')

    def test_link_monitoring_independent_of_google_auto_sync(self):
        self.store.update('clients', 'client', seo_connection={'auto_sync': False})
        self.store.put('backlinks', {'client_id': 'client', 'source_url': 'https://directory.example/listing',
            'created_at': (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()})
        self.engine.data_schedule_tick()
        self.assertEqual(self.store.all('jobs')[0]['kind'], 'backlink-check')

    def test_live_link_becomes_missing_without_erasing_history(self):
        link = self.store.put('backlinks', {'client_id': 'client', 'source_url': 'https://directory.example/listing',
            'status': 'live', 'first_seen_at': '2026-01-01T00:00:00+00:00'})
        with patch('rankme.backlinks.verify_backlink', return_value={'status': 'missing', 'live': False}):
            self.engine.execute(self.engine.queue('backlink-check', 'client', backlink_id=link['id']))
        result = self.store.get('backlinks', link['id'])
        self.assertEqual(result['status'], 'prospect')
        self.assertEqual(result['first_seen_at'], link['first_seen_at'])
        self.assertEqual(result['verification']['status'], 'missing')

    def test_link_check_cannot_access_other_client(self):
        link = self.store.put('backlinks', {'client_id': 'other', 'source_url': 'https://directory.example/listing'})
        self.engine.execute(self.engine.queue('backlink-check', 'client', backlink_id=link['id']))
        self.assertEqual(self.store.all('jobs')[0]['status'], 'failed')
        self.assertNotIn('verification', self.store.get('backlinks', link['id']))


if __name__ == '__main__':
    unittest.main()
