"""Persistence and scheduling boundaries for visibility follow-up work."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from rankme.engine import Engine

from rankme import autopilot
from rankme.server import Application


class AutopilotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = Application(Path(self.tmp.name))
        self.engine, self.store = self.app.engine, self.app.store
        self.client = self.store.put('clients', {
            'id': 'one', 'name': 'Solar', 'url': 'https://solar.example/', 'confirmed': True,
            'profile': {'summary': 'Solar advice'}, 'subject': 'Solar', 'status': 'ready',
            'visibility_settings': {**autopilot.DEFAULTS, 'auto_plan': True},
            'crawl_observed_at': datetime.now(timezone.utc).isoformat(),
            'crawl': {'pages': [{'url': 'https://solar.example/', 'title': '', 'description': '', 'links': []}]}})

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def opportunity(self, **fields):
        return self.store.put('opportunities', dict({
            'id': 'opportunity', 'client_id': 'one', 'title': 'Solar maintenance checklist',
            'kind': 'customer_question', 'query': 'solar maintenance checklist', 'status': 'open',
            'execution_mode': 'article', 'score': 70, 'url': 'https://publisher.example/question',
            'evidence': {'quote': 'How should solar panels be maintained?'}}, **fields))

    def test_rebuild_preserves_dispositions_and_isolates_client_ids(self):
        first = autopilot.rebuild(self.engine, 'one')
        ident = first['opportunities'][0]['id']
        self.store.update('opportunities', ident, status='dismissed')
        second = autopilot.rebuild(self.engine, 'one')
        match = next(row for row in second['opportunities'] if row['id'] == ident)
        self.assertEqual(match['status'], 'dismissed')
        self.assertFalse(match['executable'])
        self.store.put('clients', {**self.client, 'id': 'two'})
        other = autopilot.rebuild(self.engine, 'two')
        self.assertTrue(set(row['id'] for row in first['opportunities']).isdisjoint(row['id'] for row in other['opportunities']))
        self.assertEqual(self.store.get('opportunities', ident)['client_id'], 'one')

    def test_disappearing_opportunity_resolves_then_reopens(self):
        first = autopilot.rebuild(self.engine, 'one')['opportunities']
        ident = next(row['id'] for row in first if row['kind'] == 'missing_title')
        crawl = {'pages': [{'url': 'https://solar.example/', 'title': 'Solar', 'description': 'Solar advice', 'links': []}]}
        self.store.update('clients', 'one', crawl=crawl)
        autopilot.rebuild(self.engine, 'one')
        self.assertEqual(self.store.get('opportunities', ident)['status'], 'resolved')
        self.store.update('clients', 'one', crawl=self.client['crawl'])
        autopilot.rebuild(self.engine, 'one')
        self.assertEqual(self.store.get('opportunities', ident)['status'], 'open')

    def test_queue_prevents_duplicate_dispatch(self):
        item = self.opportunity()
        with patch.object(self.engine, 'queue', wraps=self.engine.queue) as queued:
            autopilot.queue_action(self.engine, item['id'])
            with self.assertRaises(ValueError):
                autopilot.queue_action(self.engine, item['id'])
        self.assertEqual(queued.call_count, 1)
        self.assertEqual(self.store.get('opportunities', item['id'])['status'], 'queued')

    def test_prepare_content_is_idempotent_and_does_not_publish(self):
        item = self.opportunity(status='queued')
        job = {'opportunity_id': item['id'], 'client_id': 'one'}
        autopilot.execute_action(self.engine, job, Mock())
        autopilot.execute_action(self.engine, job, Mock())
        articles = self.store.all('articles')
        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0]['status'], 'planned')
        self.assertEqual(articles[0]['body'], '')
        self.assertEqual(len(self.store.all('tasks')), 1)
        self.assertEqual(self.store.get('opportunities', item['id'])['status'], 'completed')

    def test_refresh_preserves_original_published_article(self):
        original = self.store.put('articles', {'id': 'original', 'client_id': 'one', 'title': 'Solar guide',
            'slug': 'solar-guide', 'body': 'Original body', 'status': 'published',
            'publish_result': {'live_url': 'https://solar.example/solar-guide'}})
        item = self.opportunity(execution_mode='refresh', kind='refresh', source_article_id='original')
        with patch('rankme.publisher.refresh_baseline', return_value={'sha256': 'observed'}, create=True):
            autopilot.execute_action(self.engine, {'opportunity_id': item['id'], 'client_id': 'one'}, Mock())
        self.assertEqual(self.store.get('articles', 'original'), original)
        draft = next(row for row in self.store.all('articles') if row['id'] != 'original')
        self.assertEqual(draft['status'], 'planned')
        self.assertEqual(draft['refresh_of'], 'original')
        self.assertEqual(draft['slug'], original['slug'])
        self.assertIn('Original body', draft['angle'])

    def test_cross_client_execute_and_refresh_are_rejected_atomically(self):
        item = self.opportunity()
        with self.assertRaises(ValueError):
            autopilot.execute_action(self.engine, {'opportunity_id': item['id'], 'client_id': 'other'}, Mock())
        self.store.put('articles', {'id': 'foreign', 'client_id': 'other', 'status': 'published'})
        self.store.update('opportunities', item['id'], execution_mode='refresh', source_article_id='foreign')
        with self.assertRaises(ValueError):
            autopilot.execute_action(self.engine, {'opportunity_id': item['id'], 'client_id': 'one'}, Mock())
        self.assertEqual(self.store.all('tasks'), [])
        self.assertEqual(len(self.store.all('articles')), 1)

    def test_schedule_limits_actions_including_failures(self):
        self.opportunity()
        with patch.object(self.engine, 'queue', wraps=self.engine.queue):
            autopilot.schedule(self.engine)
            job = self.store.all('jobs')[0]
            self.assertEqual(job['kind'], 'visibility-action')
            self.store.update('jobs', job['id'], status='failed')
            self.store.update('opportunities', 'opportunity', status='failed')
            self.opportunity(id='second')
            autopilot.schedule(self.engine)
        self.assertEqual(len(self.store.all('jobs')), 1)

    def test_global_and_client_pause_stop_schedule(self):
        self.opportunity()
        with patch.object(self.engine, 'queue', wraps=self.engine.queue):
            self.store.settings({'paused': True})
            autopilot.schedule(self.engine)
            self.store.settings({'paused': False})
            self.store.update('clients', 'one', status='paused')
            autopilot.schedule(self.engine)
        self.assertEqual(self.store.all('jobs'), [])

    def test_disabled_automatic_planning_rechecked_at_execution(self):
        item = self.opportunity(status='queued')
        self.store.update('clients', 'one', visibility_settings=autopilot.DEFAULTS)
        with self.assertRaises(ValueError):
            autopilot.execute_action(self.engine, {'opportunity_id': item['id'], 'client_id': 'one', 'scheduled': True}, Mock())
        self.assertEqual(self.store.all('articles'), [])

    def test_failed_audit_records_attempt_and_preserves_evidence(self):
        before = autopilot.rebuild(self.engine, 'one')
        past = (datetime.now(timezone.utc) - timedelta(days=9)).isoformat()
        self.store.update('clients', 'one', visibility_attempt_at=past, visibility_settings=autopilot.DEFAULTS)
        job = {'kind': 'visibility-audit', 'client_id': 'one', 'scheduled': True}
        with patch('rankme.crawler.crawl_site', return_value={'pages': []}):
            with self.assertRaises(ValueError):
                autopilot.execute(self.engine, job, Mock())
        self.assertNotEqual(self.store.get('clients', 'one')['visibility_attempt_at'], past)
        self.assertEqual(self.store.get('visibility', 'one')['opportunities'], before['opportunities'])
        with patch.object(self.engine, 'queue', wraps=self.engine.queue):
            autopilot.schedule(self.engine)
        self.assertEqual(self.store.all('jobs'), [])

    def test_atomic_put_many_rolls_back_on_serialization_failure(self):
        with self.assertRaises(TypeError):
            self.store.put_many([('tasks', {'id': 'first', 'body': 'valid'}),
                                 ('opportunities', {'id': 'second', 'invalid': object()})])
        self.assertEqual(self.store.all('tasks'), [])
        self.assertEqual(self.store.all('opportunities'), [])

    def test_execute_endpoint_and_queued_state_survive_restart(self):
        item = self.opportunity()
        job = self.app.dispatch('POST', '/api/opportunities/' + item['id'] + '/execute', {})
        self.assertEqual(self.store.get('opportunities', item['id'])['status'], 'queued')
        with self.assertRaises(ValueError):
            self.app.dispatch('POST', '/api/opportunities/' + item['id'] + '/execute', {})
        with self.assertRaises(ValueError):
            self.app.dispatch('PATCH', '/api/opportunities/' + item['id'], {'status': 'dismissed'})
        # Reconstruct both app and SQLite connection to exercise persisted jobs.
        self.store.close()
        self.app = Application(Path(self.tmp.name))
        self.engine, self.store = self.app.engine, self.app.store
        resumed = self.store.get('jobs', job['id'])
        self.engine.execute(resumed)
        self.assertEqual(self.store.get('jobs', job['id'])['status'], 'completed')
        self.assertEqual(len(self.store.all('articles')), 1)
        # Crash after dependent writes but before job completion must remain idempotent.
        self.store.update('jobs', job['id'], status='running')
        restarted = Engine(self.store, Path(self.tmp.name))
        restarted.execute(self.store.get('jobs', job['id']))
        self.assertEqual(len(self.store.all('articles')), 1)
        self.assertEqual(len(self.store.all('tasks')), 1)
        self.assertEqual(self.store.get('jobs', job['id'])['status'], 'completed')

    def test_audit_endpoint_rebuilds_report_and_exports_client_scope(self):
        self.store.put('clients', {**self.client, 'id': 'two'})
        self.opportunity(id='foreign', client_id='two')
        with patch('rankme.crawler.crawl_site', return_value=self.client['crawl']):
            job = self.app.dispatch('POST', '/api/clients/one/visibility-audit', {})
            self.engine.execute(job)
        self.assertEqual(self.store.get('jobs', job['id'])['status'], 'completed')
        result = self.app.dispatch('GET', '/api/clients/one/visibility-report', {})
        self.assertTrue(result['opportunities'])
        self.assertTrue(all(row['client_id'] == 'one' for row in result['opportunities']))

    def test_failed_action_retry_preserves_opportunity_id(self):
        item = self.opportunity()
        job = autopilot.queue_action(self.engine, item['id'])
        with patch('rankme.autopilot.execute_action', side_effect=ValueError('Temporary failure')):
            self.engine.execute(job)
        self.assertEqual(self.store.get('jobs', job['id'])['status'], 'failed')
        retried = self.engine.retry(job['id'])
        self.assertEqual(retried['opportunity_id'], item['id'])
        self.engine.execute(retried)
        self.assertEqual(len(self.store.all('articles')), 1)
        self.assertEqual(self.store.get('opportunities', item['id'])['status'], 'completed')

    def test_probe_uses_subject_when_search_console_snapshot_is_null(self):
        self.store.put('seo', {'id': 'one', 'client_id': 'one', 'search_console': None,
                               'analytics': {'current': {'organic_sessions': 10}}})
        job = self.engine.queue('visibility-probe', 'one')
        with patch.object(self.engine, 'runner', return_value=Mock()), patch('rankme.answer_probes.probe',
                return_value={'records': [], 'provider': 'Codex web research'}) as probe:
            self.engine.execute(job)
        self.assertEqual(self.store.get('jobs', job['id'])['status'], 'completed')
        self.assertEqual(probe.call_args.args[2], ['Solar'])

    def test_queued_scheduled_generation_rechecks_client_pause(self):
        self.store.put('articles', {'id': 'planned', 'client_id': 'one', 'title': 'Solar guide',
                                   'status': 'planned', 'body': ''})
        job = self.engine.queue('generate', 'one', 'planned', scheduled=True)
        self.store.update('clients', 'one', automation=False, status='paused')
        with patch.object(self.engine, 'runner', side_effect=AssertionError('Paused generation called runner')) as runner:
            self.engine.execute(job)
        runner.assert_not_called()
        self.assertEqual(self.store.get('articles', 'planned')['status'], 'planned')

    def test_configuration_rejects_unknown_and_unsafe_types(self):
        for value in ({'max_actions': True}, {'max_actions': 6}, {'interval_days': 0},
                      {'auto_plan': 'yes'}, {'send_outreach': True}):
            with self.assertRaises(ValueError):
                autopilot.settings(value)

    def test_refresh_url_ownership_preserves_query_identity(self):
        self.store.put('articles', {'id': 'page-a', 'client_id': 'one', 'status': 'published',
            'publish_result': {'live_url': 'https://solar.example/?page=a'}})
        self.assertIsNone(autopilot.owned_article(self.engine, self.client, 'https://solar.example/?page=b'))
        self.assertEqual(autopilot.owned_article(self.engine, self.client, 'https://solar.example/?page=a')['id'], 'page-a')

    def test_outreach_excluded_but_monitoring_remains(self):
        item = self.opportunity(kind='editorial_prospect')
        with self.assertRaisesRegex(ValueError, 'Outreach'):
            autopilot.queue_action(self.engine, item['id'])
        report = autopilot.rebuild(self.engine, 'one')
        excluded = {cap['id'] for cap in report['capabilities'] if cap['status'] == 'out_of_scope'}
        self.assertEqual(excluded, {13, 14, 19, 24})
        self.assertIn('link_opportunities', report)

    def test_recurring_refresh_uses_new_revision_and_new_data_window(self):
        self.store.put('articles', {'id': 'original', 'client_id': 'one', 'status': 'published',
            'publish_result': {'live_url': 'https://solar.example/guide'}})
        observed = {'opportunities': [{'id': 'stable-decline', 'kind': 'refresh', 'title': 'Decline',
            'url': 'https://solar.example/guide', 'score': 60,
            'evidence': {'periods': {'current': {'end': '2026-06-30'}}}}],
            'coverage': {'pages': 1}, 'technical_findings': [], 'capabilities': [], 'missing_data': []}
        import copy
        with patch('rankme.visibility.analyze', side_effect=lambda *a: copy.deepcopy(observed)):
            first = autopilot.rebuild(self.engine, 'one')['opportunities'][0]
            self.store.update('opportunities', first['id'], status='completed')
            self.store.update('articles', 'original', status='refreshed')
            self.store.put('articles', {'id': 'revision', 'client_id': 'one', 'status': 'published',
                'refresh_of': 'original', 'published_at': '2026-07-05T00:00:00+00:00',
                'publish_result': {'live_url': 'https://solar.example/guide'}})
            self.assertEqual(autopilot.rebuild(self.engine, 'one')['opportunities'], [])
            observed['opportunities'][0]['evidence']['periods']['current']['end'] = '2026-08-05'
            next_item = autopilot.rebuild(self.engine, 'one')['opportunities'][0]
            self.assertNotEqual(next_item['id'], first['id'])
            self.assertEqual(next_item['source_article_id'], 'revision')
            self.assertEqual(next_item['status'], 'open')


if __name__ == '__main__':
    unittest.main()
