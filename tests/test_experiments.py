import unittest
from copy import deepcopy

from rankme.experiments import (enrich_opportunities, ensure_available, evaluate,
                                start_experiment, validate_goal)


class ExperimentTests(unittest.TestCase):
    site = 'https://site.example/'
    goal = {'event_name': 'booking_confirmed', 'landing_page': 'https://site.example/book', 'goal_type': 'booking'}

    def metric(self, start='2026-01-01', end='2026-01-14', sessions=200, events=20, **extra):
        return {'start': start, 'end': end, 'landing_page': self.goal['landing_page'],
                'event_name': self.goal['event_name'], 'organic_sessions': sessions, 'conversions': events,
                'metadata': {'organic_sessions': {'timeZone': 'UTC'}, 'conversions': {'timeZone': 'UTC'}}, **extra}

    def experiment(self, baseline=None):
        return start_experiment('one', self.goal['landing_page'], 'Clarifying the CTA may increase booking events',
                                'Changed only CTA label', baseline or self.metric(), [], self.goal, '2026-01-15T10:00:00+00:00')

    def post(self, **kwargs):
        return self.metric(start='2026-01-16', end='2026-01-29', **kwargs)

    def test_goal_validation_bounds_and_same_site(self):
        result = validate_goal(self.goal, self.site)
        self.assertEqual(result['minimum_days'], 14)
        self.assertEqual(validate_goal({}, self.site), {})
        for config in ({**self.goal, 'landing_page': 'https://site.example.attacker.test/book'},
                       {**self.goal, 'landing_page': 'https://user:pass@site.example/book'},
                       {**self.goal, 'landing_page': self.goal['landing_page'] + '#form'},
                       {**self.goal, 'landing_page': self.goal['landing_page'] + '?' + 'x' * 2048},
                       {**self.goal, 'event_name': 'invalid event'}, {**self.goal, 'goal_type': 'views'},
                       {**self.goal, 'minimum_days': True}, {**self.goal, 'minimum_days': 29}, {**self.goal, 'min_sessions': 0},
                       {**self.goal, 'unknown': 1}):
            with self.assertRaises(ValueError):
                validate_goal(config, self.site)

    def test_exact_page_one_active_and_tenant_isolation(self):
        record = self.experiment()
        with self.assertRaises(ValueError):
            ensure_available([record], 'one', self.goal['landing_page'])
        ensure_available([record], 'one', self.goal['landing_page'] + '/')
        ensure_available([record], 'one', 'https://www.site.example/book')
        ensure_available([record], 'two', self.goal['landing_page'])
        ensure_available([record], 'one', self.goal['landing_page'] + '?variant=b')
        ensure_available([{**record, 'status': 'improved'}], 'one', self.goal['landing_page'])

    def test_start_snapshots_baseline_and_rejects_missing_or_overlapping(self):
        baseline = self.metric()
        record = self.experiment(baseline)
        baseline['conversions'] = 999
        self.assertEqual(record['baseline']['conversions'], 20)
        for baseline in (self.metric(end='2026-01-15'), self.metric(conversions=None), self.metric(organic_sessions=float('nan'))):
            with self.assertRaises(ValueError):
                self.experiment(baseline)

    def test_positive_and_negative_effects_are_descriptive(self):
        for events, status in ((40, 'improved'), (0, 'declined')):
            result = evaluate(self.experiment(), self.post(events=events), '2026-01-30')
            self.assertEqual(result['status'], status)
            self.assertIn('not a unique-session conversion rate', result['evaluation']['note'])
            self.assertEqual(result['evaluation']['current_events_per_session'], events / 200)
        # Event occurrences can legitimately exceed session counts.
        result = evaluate(self.experiment(), self.post(events=400), '2026-01-30')
        self.assertEqual(result['evaluation']['current_events_per_session'], 2)

    def test_missing_is_never_zero_and_waits_until_terminal_horizon(self):
        original = self.experiment()
        for observation in (None, self.post(conversions=None), self.post(organic_sessions=None),
                            {'status': 'unavailable', 'current': self.post()}):
            result = evaluate(original, observation, '2026-01-30')
            self.assertEqual(result['status'], 'observing')
            self.assertNotIn('relative_change', result['evaluation'])
            self.assertEqual(evaluate(original, observation, '2026-05-01')['status'], 'inconclusive')
        self.assertNotIn('completed_at', original)

    def test_low_samples_and_no_effect_remain_observing(self):
        self.assertEqual(evaluate(self.experiment(), self.post(sessions=5, events=1), '2026-01-30')['status'], 'observing')
        self.assertEqual(evaluate(self.experiment(), self.post(events=21), '2026-01-30')['status'], 'observing')
        self.assertEqual(evaluate(self.experiment(), self.post(events=21), '2026-05-01')['status'], 'inconclusive')
        self.assertEqual(evaluate(self.experiment(self.metric(events=0)), self.post(events=30), '2026-01-30')['status'], 'observing')

    def test_incompatible_windows_goal_or_future_evidence_cannot_conclude(self):
        for observation in (self.metric(start='2026-01-15', end='2026-01-28', events=40),
                            self.post(landing_page='https://www.site.example/book', events=40),
                            self.post(landing_page='https://site.example/book/', events=40),
                            self.post(landing_page='https://site.example/book?variant=a', events=40),
                            self.metric(start='2026-01-10', end='2026-01-23', events=40),
                            self.metric(start='2026-01-16', end='2026-01-28', events=40),
                            self.metric(start='2026-02-01', end='2026-02-14', events=40),
                            self.post(event_name='different_event', events=40),
                            self.post(landing_page='https://site.example/other', events=40)):
            self.assertEqual(evaluate(self.experiment(), observation, '2026-01-30')['status'], 'observing')

    def test_thresholded_sampled_and_stale_evidence_cannot_conclude(self):
        for metadata in ({'organic_sessions': {'subjectToThresholding': True}},
                         {'conversions': {'samplingMetadatas': [{'samplesReadCount': '10'}]}},
                         {'dataLossFromOtherRow': True}):
            result = evaluate(self.experiment(), self.post(events=40, metadata=metadata), '2026-01-30')
            self.assertEqual(result['status'], 'observing')
            self.assertIn('data quality', result['evaluation']['reason'])
        self.assertEqual(evaluate(self.experiment(), self.post(events=40), '2026-03-01')['status'], 'observing')

    def test_default_ports_match_but_effects_beyond_time_budget_do_not(self):
        with self.assertRaises(ValueError):
            ensure_available([self.experiment()], 'one', 'https://site.example:443/book')
        observation = self.metric(start='2026-04-10', end='2026-04-23', events=40)
        result = evaluate(self.experiment(), observation, '2026-04-24')
        self.assertEqual(result['status'], 'inconclusive')
        self.assertIn('90-day', result['evaluation']['reason'])

    def test_change_day_uses_property_timezone_and_rejects_timezone_drift(self):
        metadata = {'organic_sessions': {'timeZone': 'Asia/Tokyo'}, 'conversions': {'timeZone': 'Asia/Tokyo'}}
        baseline = self.metric(metadata=metadata)
        record = start_experiment('one', self.goal['landing_page'], 'Hypothesis', 'CTA label',
                                  baseline, [], self.goal, '2026-01-15T22:00:00+00:00')
        early = self.metric(start='2026-01-16', end='2026-01-29', events=40, metadata=metadata)
        self.assertEqual(evaluate(record, early, '2026-01-30')['status'], 'observing')
        safe = self.metric(start='2026-01-17', end='2026-01-30', events=40, metadata=metadata)
        self.assertEqual(evaluate(record, safe, '2026-01-31')['status'], 'improved')
        self.assertEqual(evaluate(record, self.post(events=40), '2026-01-31')['status'], 'observing')
        conflicting = self.post(events=40, metadata={'organic_sessions': {'timeZone': 'UTC'},
                                                   'conversions': {'timeZone': 'Asia/Tokyo'}})
        self.assertEqual(evaluate(record, conflicting, '2026-01-31')['status'], 'observing')

    def test_missing_timezone_excludes_utc_change_day_and_following_day(self):
        baseline = self.metric(metadata={})
        record = self.experiment(baseline)
        early = self.post(events=40, metadata={})
        self.assertEqual(evaluate(record, early, '2026-01-30')['status'], 'observing')
        safe = self.metric(start='2026-01-17', end='2026-01-30', events=40, metadata={})
        self.assertEqual(evaluate(record, safe, '2026-01-31')['status'], 'improved')
        record['started_at'] = '2026-01-15'
        self.assertEqual(evaluate(record, early, '2026-01-30')['status'], 'improved')

    def test_wrapper_supported_and_completed_outcome_is_immutable(self):
        measurement = {'status': 'measured', 'current': self.post(events=40)}
        result = evaluate(self.experiment(), measurement, '2026-01-30')
        self.assertEqual(result['status'], 'improved')
        self.assertEqual(evaluate(result, self.post(events=0), '2026-02-01'), result)

    def test_scoring_uses_only_real_exact_goal_page_evidence_and_is_idempotent(self):
        rows = [{'id': 'one', 'url': self.goal['landing_page'], 'score': 40},
                {'id': 'two', 'url': 'https://site.example/other', 'score': 40},
                {'id': 'three', 'url': '', 'score': 40}]
        snapshot = deepcopy(rows)
        measurement = {'status': 'measured', 'current': self.post()}
        enriched = enrich_opportunities(rows, self.goal, measurement)
        self.assertEqual(rows, snapshot)
        self.assertEqual([r['score'] for r in enriched], [50, 40, 40])
        self.assertEqual(enrich_opportunities(enriched, self.goal, measurement), enriched)
        self.assertIn('Heuristic', enriched[0]['conversion_score_reason'])
        self.assertEqual(enrich_opportunities(rows, self.goal, {'status': 'unavailable'}), rows)
        self.assertEqual(enrich_opportunities(rows, self.goal, {'status': 'measured', 'current': self.post(conversions=None)}), rows)


if __name__ == '__main__':
    unittest.main()
