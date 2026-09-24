from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from rankme import measurement
from rankme.experiments import validate_goal
from rankme.google_data import GoogleDataError
from rankme.server import Application


class MeasurementSecurityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = Application(Path(self.temp.name))
        self.store, self.engine = self.app.store, self.app.engine
        self.addCleanup(self.store.close)
        self.today = datetime.now(timezone.utc).date()
        self.goal = validate_goal({'event_name': 'generate_lead', 'landing_page': 'https://example.com/service', 'goal_type': 'lead'}, 'https://example.com/')
        self.client = self.store.put('clients', {'id': 'client', 'url': 'https://example.com/', 'name': 'Example',
            'confirmed': True, 'status': 'ready', 'profile': {'summary': 'Example'}, 'conversion_goal': self.goal,
            'seo_connection': {'site_url': 'https://example.com/', 'ga4_property': '123'}})
        self.metric = {'start': (self.today - timedelta(days=30)).isoformat(), 'end': (self.today - timedelta(days=3)).isoformat(),
            'landing_page': self.goal['landing_page'], 'event_name': 'generate_lead', 'organic_sessions': 100, 'conversions': 10}
        self.snapshot = {'ga4_property': '123', 'site_url': 'https://example.com/',
            'conversion_measurement': {'status': 'measured', 'goal': {k: self.goal[k] for k in ('event_name', 'landing_page', 'goal_type')},
            'current': self.metric, 'previous': self.metric}}
        measurement.save_snapshot(self.store, 'seo', 'client', self.snapshot, 'initial')

    def start(self):
        return measurement.start(self.engine, 'client', self.goal['landing_page'], 'A clearer CTA helps', 'Changed CTA copy')

    def test_ledger_rejects_updates_and_overwrites(self):
        row = measurement.observation('client', 'test', {'value': 1}, 'fixed')
        first = self.store.put('measurements', row)
        overwritten = self.store.put('measurements', {**row, 'snapshot': {'value': 2}})
        self.assertEqual(first, overwritten)
        self.store.put_many([('measurements', {**row, 'snapshot': {'value': 3}})])
        self.assertEqual(self.store.get('measurements', row['id']), first)
        with self.assertRaises(ValueError):
            self.store.update('measurements', row['id'], snapshot={'value': 4})

    def test_goal_and_property_mismatch_cannot_reuse_old_measurement(self):
        changed = {**self.client, 'conversion_goal': {**self.goal, 'event_name': 'purchase'}}
        self.assertEqual(measurement.matching_measurement(changed, self.snapshot)['status'], 'unavailable')
        changed = {**self.client, 'seo_connection': {'ga4_property': '999'}}
        self.assertEqual(measurement.matching_measurement(changed, self.snapshot)['status'], 'unavailable')
        self.assertEqual(measurement.matching_measurement(self.client, self.snapshot)['status'], 'measured')

    def test_failed_sync_preserves_history_and_frozen_baseline(self):
        experiment = self.start()
        before = self.store.all('measurements')
        job = self.engine.queue('seo-sync', 'client')
        with patch.object(self.engine.google, 'sync', side_effect=GoogleDataError('Unavailable', retryable=True)):
            self.engine.execute(job)
        self.assertEqual(self.store.get('experiments', experiment['id'])['baseline'], self.metric)
        self.assertEqual(self.store.all('measurements'), before)
        self.assertEqual(self.store.get('seo', 'client')['conversion_measurement']['current'], self.metric)
        self.assertEqual(self.store.get('jobs', job['id'])['status'], 'failed')

    def test_active_experiment_prevents_goal_property_changes_and_duplicate_start(self):
        self.start()
        for change in ({'conversion_goal': {}}, {'seo_connection': {'site_url': 'https://example.com/', 'ga4_property': '999'}}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.app.update_client('client', change)
        with self.assertRaises(ValueError):
            self.start()
        self.assertEqual(len(self.store.all('experiments')), 1)

    def test_active_experiment_holds_refresh_queue(self):
        self.start()
        self.store.put('opportunities', {'id': 'refresh', 'client_id': 'client', 'status': 'open',
             'url': self.goal['landing_page'], 'execution_mode': 'refresh', 'kind': 'refresh'})
        from rankme.autopilot import queue_action
        with self.assertRaises(ValueError):
            queue_action(self.engine, 'refresh')
        self.assertEqual(self.store.all('jobs'), [])

    def test_active_experiment_blocks_already_prepared_refresh_publication(self):
        from rankme.engine import content_digest, profile_digest
        self.start()
        self.store.put('articles', {'id': 'original', 'client_id': 'client', 'status': 'published',
             'publish_result': {'live_url': self.goal['landing_page']}})
        draft = {'id': 'draft', 'client_id': 'client', 'refresh_of': 'original', 'title': 'Reviewed revision',
                 'slug': 'service', 'body': 'Reviewed content', 'review': {'passed': True},
                 'reviewed_profile': profile_digest(self.client)}
        draft['reviewed_digest'] = content_digest(draft)
        self.store.put('articles', draft)
        with patch.object(self.engine, 'cover_valid', return_value=True), patch('rankme.publisher.publish_article') as publish:
            with self.assertRaisesRegex(ValueError, 'active experiment'):
                self.engine.publish(self.client, draft, lambda message: None)
        publish.assert_not_called()

    def test_missing_measurement_does_not_become_zero_or_mutate_baseline(self):
        experiment = self.start()
        failed = {**self.snapshot, 'conversion_measurement': {'status': 'unavailable', 'goal': self.snapshot['conversion_measurement']['goal'], 'current': None}}
        measurement.evaluate_all(self.engine, 'client', failed, 'failed-measurement')
        actual = self.store.get('experiments', experiment['id'])
        self.assertEqual(actual['status'], 'observing')
        self.assertEqual(actual['baseline'], self.metric)
        self.assertNotIn('current_conversions', actual['evaluation'])

    def test_goal_defaults_are_not_passed_as_google_measurement_fields(self):
        job = self.engine.queue('seo-sync', 'client')
        with patch.object(self.engine.google, 'sync', return_value=self.snapshot) as sync:
            self.engine.execute(job)
        self.assertEqual(set(sync.call_args.kwargs['goal_config']), {'event_name', 'landing_page', 'goal_type'})
        self.assertEqual(self.store.get('jobs', job['id'])['status'], 'completed')

    def test_snapshot_retry_cannot_desynchronize_latest_and_immutable_evidence(self):
        different = {**self.snapshot, 'ga4_property': '999'}
        record = measurement.save_snapshot(self.store, 'seo', 'client', different, 'initial')
        self.assertEqual(record['snapshot'], self.snapshot)
        self.assertEqual(self.store.get('seo', 'client')['ga4_property'], '123')

    def test_frozen_experiment_property_cannot_be_reinterpreted_after_import(self):
        experiment = self.start()
        self.store.update('clients', 'client', seo_connection={'site_url': 'https://example.com/', 'ga4_property': '999'})
        current = {**self.metric, 'start': (self.today + timedelta(days=1)).isoformat(),
                   'end': (self.today + timedelta(days=28)).isoformat(), 'conversions': 90}
        incoming = {**self.snapshot, 'ga4_property': '999',
                    'conversion_measurement': {**self.snapshot['conversion_measurement'], 'current': current}}
        with patch('rankme.measurement.now', return_value=(self.today + timedelta(days=31)).isoformat()):
            measurement.evaluate_all(self.engine, 'client', incoming, 'different-property')
        row = self.store.get('experiments', experiment['id'])
        self.assertEqual(row['status'], 'observing')
        self.assertEqual(row['baseline'], self.metric)
        self.assertNotIn('relative_change', row['evaluation'])

    def test_guard_and_experiment_page_identity_agree(self):
        from rankme.experiments import _page
        urls = ['https://example.com/service', 'https://www.example.com/service',
                'https://example.com/service/', 'https://example.com:443/service',
                'http://example.com/service', 'https://example.com/service?intent=other']
        for left in urls:
            for right in urls:
                with self.subTest(left=left, right=right):
                    self.assertEqual(measurement.page_identity(left) == measurement.page_identity(right),
                                     _page(left) == _page(right))


if __name__ == '__main__':
    unittest.main()
