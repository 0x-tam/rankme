import tempfile
import unittest
from unittest.mock import patch

from rankme.google_data import GoogleData, GoogleDataError, validate_goal


GOAL = {'event_name': 'generate_lead', 'landing_page': 'https://example.com/services?plan=business', 'goal_type': 'lead'}
PERIOD = {'start': '2026-01-01', 'end': '2026-01-28'}


class ConversionMeasurementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.google = GoogleData(self.temp.name)

    def sync(self, goal=GOAL, property_id='123', api=None):
        with patch.object(self.google, 'status', return_value={'search_console': False}), \
             patch.object(self.google, '_analytics', return_value={'organic_sessions': 100, 'key_events': 20}), \
             patch.object(self.google, '_api', side_effect=api or (lambda *args: {'rows': [{'metricValues': [{'value': '0'}]}]})):
            return self.google.sync('https://example.com/', property_id, goal)

    def test_sessions_not_filtered_to_event_and_exact_landing_query(self):
        requests = []
        def api(url, scope, body):
            requests.append(body)
            metric = body['metrics'][0]['name']
            return {'rows': [{'metricValues': [{'value': '100' if metric == 'sessions' else '7'}]}]}
        result = self.sync(api=api)
        self.assertEqual(len(requests), 4)
        for body in requests:
            filters = {f['filter']['fieldName']: f['filter']['stringFilter'] for f in body['dimensionFilter']['andGroup']['expressions']}
            self.assertEqual(filters['landingPagePlusQueryString']['value'], '/services?plan=business')
            self.assertTrue(filters['landingPagePlusQueryString']['caseSensitive'])
            self.assertEqual(filters['sessionDefaultChannelGroup']['value'], 'Organic Search')
            self.assertEqual(filters['hostName']['value'], 'example.com')
            if body['metrics'][0]['name'] == 'eventCount':
                self.assertEqual(filters['eventName']['value'], 'generate_lead')
            else:
                self.assertNotIn('eventName', filters)
            self.assertEqual(body['limit'], '1')
        measurement = result['conversion_measurement']
        self.assertEqual(measurement['status'], 'measured')
        self.assertEqual(measurement['tracking_status'], 'observed')
        self.assertEqual(measurement['current']['conversions'], 7)
        self.assertEqual(measurement['current']['organic_sessions'], 100)
        self.assertEqual(result['analytics']['current']['key_events'], 20)
        self.assertEqual(measurement['current']['landing_page'], GOAL['landing_page'])

    def test_zero_is_measured_not_missing_and_does_not_claim_tracking_broken(self):
        for response in ({'rows': []}, {'rows': [{'metricValues': [{'value': '0'}]}]}):
            result = self.sync(api=lambda *args: response)['conversion_measurement']
            self.assertEqual(result['status'], 'measured')
            self.assertEqual(result['current']['conversions'], 0)
            self.assertEqual(result['tracking_status'], 'not_observed')
            self.assertIn('verify instrumentation separately', result['note'])

    def test_unconfigured_goal_does_not_request_additional_reports(self):
        with patch.object(self.google, 'status', return_value={'search_console': False}), \
             patch.object(self.google, '_analytics', return_value={'organic_sessions': 0}), \
             patch.object(self.google, '_api') as api:
            result = self.google.sync('https://example.com/', '123')
        api.assert_not_called()
        self.assertEqual(result['conversion_measurement']['status'], 'unconfigured')
        self.assertIsNone(result['conversion_measurement']['current'])

    def test_conversion_error_preserves_sitewide_metrics_without_fake_zeros(self):
        def fail(*args):
            raise GoogleDataError('Missing permission')
        result = self.sync(api=fail)
        self.assertEqual(result['analytics']['current']['organic_sessions'], 100)
        self.assertEqual(result['conversion_measurement']['status'], 'unavailable')
        self.assertIsNone(result['conversion_measurement']['current'])
        self.assertEqual(result['conversion_measurement']['issues'], ['Missing permission'])

    def test_partial_period_failure_does_not_publish_incomplete_comparison(self):
        calls = []
        def api(*args):
            calls.append(args)
            if len(calls) == 3:
                raise GoogleDataError('Quota')
            return {'rows': [{'metricValues': [{'value': '7'}]}]}
        result = self.sync(api=api)['conversion_measurement']
        self.assertEqual(result['status'], 'unavailable')
        self.assertIsNone(result['current'])
        self.assertIsNone(result['previous'])

    def test_missing_ga_property_reported_with_search_console_preserved(self):
        with patch.object(self.google, 'status', return_value={'search_console': True}), \
             patch.object(self.google, '_search', return_value=([], False)):
            result = self.google.sync('https://example.com/', '', GOAL)
        self.assertEqual(result['conversion_measurement']['status'], 'unavailable')
        self.assertIn('GA4 property', result['conversion_measurement']['issues'][0])
        self.assertIsNotNone(result['search_console'])

    def test_goal_validation_rejects_unsafe_foreign_unbounded_inputs(self):
        invalid = [{**GOAL, 'event_name': 'x' * 41}, {**GOAL, 'event_name': 'event\nname'},
                   {**GOAL, 'event_name': '0event'}, {**GOAL, 'goal_type': 'anything'},
                   {**GOAL, 'landing_page': 'https://example.com.evil.test/'},
                   {**GOAL, 'landing_page': 'https://user:secret@example.com/'},
                   {**GOAL, 'landing_page': 'http://127.0.0.1/'},
                   {**GOAL, 'landing_page': 'https://example.com/#fragment'},
                   {**GOAL, 'landing_page': 'https://example.com/' + 'x' * 2048},
                   {**GOAL, 'landing_page': '/relative'}, {**GOAL, 'extra': 'injection'}, []]
        for goal in invalid:
            with self.subTest(goal=goal), self.assertRaises(GoogleDataError):
                validate_goal(goal, 'https://example.com/')
        self.assertEqual(validate_goal(GOAL, 'sc-domain:example.com'), GOAL)
        self.assertIsNone(validate_goal(None, 'https://example.com/'))

    def test_malformed_metrics_are_unavailable_not_zero(self):
        for value in ('NaN', 'Infinity', '-1', 'not-a-number'):
            with self.subTest(value=value):
                result = self.sync(api=lambda *args: {'rows': [{'metricValues': [{'value': value}]}]})
                self.assertEqual(result['conversion_measurement']['status'], 'unavailable')
                self.assertIsNone(result['conversion_measurement']['current'])


if __name__ == '__main__':
    unittest.main()
