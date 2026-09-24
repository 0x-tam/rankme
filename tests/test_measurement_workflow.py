"""Evidence retention and controlled-change integration with the real application."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from rankme.server import Application
from rankme import measurement, autopilot
from rankme.experiments import validate_goal


class MeasurementWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = Application(Path(self.temp.name))
        self.store, self.engine = self.app.store, self.app.engine
        self.goal = validate_goal({'event_name': 'booking', 'goal_type': 'booking',
                                   'landing_page': 'https://example.com/book'}, 'https://example.com/')
        self.client = self.store.put('clients', {'id': 'one', 'url': 'https://example.com/',
            'name': 'Example', 'confirmed': True, 'status': 'ready', 'subject': 'Advice',
            'conversion_goal': self.goal, 'seo_connection': {'site_url': 'sc-domain:example.com', 'ga4_property': '123'}})
        self.engine.google = Mock()
        self.engine.google.status.return_value = {}

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def snapshot(self, sessions=200, events=20):
        end = datetime.now(timezone.utc).date() - timedelta(days=3)
        return {'ga4_property': '123', 'conversion_measurement': {'status': 'measured', 'goal': self.goal,
            'current': {'start': (end-timedelta(days=27)).isoformat(), 'end': end.isoformat(),
                        'organic_sessions': sessions, 'conversions': events, 'event_name': 'booking',
                        'landing_page': self.goal['landing_page']}}}

    def start(self):
        measurement.save_snapshot(self.store, 'seo', 'one', self.snapshot())
        return self.app.dispatch('POST', '/api/clients/one/experiments', {
            'page_url': self.goal['landing_page'], 'hypothesis': 'Clearer CTA leads to more booking events',
            'change': 'Updated the booking introduction'})

    def test_sync_preserves_each_snapshot_and_evaluates_without_mutating_baseline(self):
        row = self.start()
        self.engine.google.sync.return_value = self.snapshot(250, 30)
        self.engine.execute(self.engine.queue('seo-sync', 'one'))
        current = self.store.get('experiments', row['id'])
        self.assertEqual(current['baseline']['organic_sessions'], 200)
        self.assertEqual(current['status'], 'observing')
        self.assertIn('change day', current['evaluation']['reason'])
        self.assertEqual(len([r for r in self.store.history('one') if r['source'] == 'seo']), 2)
        self.assertEqual(set(self.engine.google.sync.call_args.kwargs['goal_config']), {'goal_type', 'landing_page', 'event_name'})

    def test_append_only_and_duplicate_job_snapshot_stays_consistent(self):
        first = measurement.save_snapshot(self.store, 'seo', 'one', self.snapshot(), 'same-job')
        repeated = measurement.save_snapshot(self.store, 'seo', 'one', self.snapshot(1, 0), 'same-job')
        self.assertEqual(first, repeated)
        self.assertEqual(self.store.get('seo', 'one')['conversion_measurement']['current']['organic_sessions'], 200)
        with self.assertRaises(ValueError):
            self.store.update('measurements', first['id'], snapshot={})
        self.assertEqual(self.store.put('measurements', {**first, 'snapshot': {}}), first)
        self.assertEqual(self.store.put_many([('measurements', {**first, 'snapshot': {}})])[0], first)

    def test_active_experiment_blocks_goal_properties_and_repeat_page_work(self):
        row = self.start()
        for change in ({'conversion_goal': {}}, {'seo_connection': {'ga4_property': '456'}}):
            with self.assertRaisesRegex(ValueError, 'active experiments'):
                self.app.update_client('one', change)
        with self.assertRaisesRegex(ValueError, 'active experiment'):
            self.app.dispatch('POST', '/api/clients/one/experiments', {
                'page_url': self.goal['landing_page'], 'hypothesis': 'Second change', 'change': 'Changed CTA again'})
        self.store.put('opportunities', {'id': 'refresh', 'client_id': 'one', 'execution_mode': 'refresh',
            'status': 'open', 'url': self.goal['landing_page'], 'kind': 'refresh'})
        with self.assertRaisesRegex(ValueError, 'active experiment'):
            autopilot.queue_action(self.engine, 'refresh')
        self.assertEqual(self.store.all('jobs'), [])
        self.app.dispatch('POST', '/api/experiments/' + row['id'] + '/cancel', {})
        self.app.update_client('one', {'conversion_goal': {}})
        self.assertEqual(self.store.get('experiments', row['id'])['status'], 'cancelled')
        self.assertTrue(self.store.history('one'))

    def test_missing_or_wrong_property_baseline_never_starts(self):
        with self.assertRaisesRegex(ValueError, 'Sync'):
            measurement.start(self.engine, 'one', self.goal['landing_page'], 'Test', 'Changed title')
        value = self.snapshot()
        value['ga4_property'] = '456'
        measurement.save_snapshot(self.store, 'seo', 'one', value)
        with self.assertRaisesRegex(ValueError, 'Sync'):
            measurement.start(self.engine, 'one', self.goal['landing_page'], 'Test', 'Changed title')

    def test_refresh_records_one_experiment_and_captured_baseline_after_verification(self):
        snapshot = self.snapshot()
        article = {'id': 'draft', 'refresh_of': 'original', 'title': 'Booking', 'change_observation': {
            'started_at': datetime.now(timezone.utc).isoformat(), 'snapshot': snapshot}}
        pending = {'status': 'verification_pending', 'live_url': self.goal['landing_page']}
        measurement.record_publication(self.engine, self.client, article, pending)
        self.assertEqual(self.store.all('experiments'), [])
        measurement.save_snapshot(self.store, 'seo', 'one', self.snapshot(500, 40))
        result = {**pending, 'status': 'published'}
        measurement.record_publication(self.engine, self.client, article, result)
        measurement.record_publication(self.engine, self.client, article, result)
        experiments = self.store.all('experiments')
        self.assertEqual(len(experiments), 1)
        self.assertEqual(experiments[0]['baseline']['organic_sessions'], 200)
        self.assertEqual(len([r for r in self.store.history('one') if r['source'] == 'change']), 2)

    def test_state_bounded_history_full_export_and_restart_persistence(self):
        for i in range(24):
            measurement.save_snapshot(self.store, 'seo', 'one', self.snapshot(events=i))
        measurement.save_snapshot(self.store, 'seo', 'two', self.snapshot())
        state = self.engine.state()
        self.assertEqual(len(state['measurements']), 20)
        self.assertEqual(state['measurement_counts']['one'], 24)
        exported = self.app.dispatch('GET', '/api/clients/one/measurement-history', {})
        self.assertEqual(len(exported['measurements']), 24)
        self.assertTrue(all(r['client_id'] == 'one' for r in exported['measurements']))
        self.store.close()
        self.app = Application(Path(self.temp.name))
        self.store = self.app.store
        self.assertEqual(len(self.store.history('one')), 24)

    def test_zero_events_do_not_imply_failed_tracking_or_page(self):
        measurement.save_snapshot(self.store, 'seo', 'one', self.snapshot(events=0))
        row = measurement.start(self.engine, 'one', self.goal['landing_page'], 'Test', 'Changed title')
        self.assertEqual(row['status'], 'observing')
        self.assertEqual(row['baseline']['conversions'], 0)


if __name__ == '__main__':
    unittest.main()
