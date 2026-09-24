"""Stopping queued/running work, dismissing failures, and deleting articles and covers."""
import subprocess
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from rankme import cancel
from rankme.server import Application


class CancelRunTests(unittest.TestCase):
    def tearDown(self):
        cancel.reset()

    def test_stop_kills_a_running_child_quickly(self):
        timer = threading.Timer(0.5, cancel.request)
        timer.start()
        started = time.monotonic()
        with self.assertRaises(cancel.JobCancelled):
            cancel.run(['sleep', '30'], timeout=60)
        self.assertLess(time.monotonic() - started, 5)

    def test_protected_sections_refuse_stop_requests(self):
        with cancel.protected():
            self.assertFalse(cancel.request())
            cancel.check()  # does not raise
        self.assertTrue(cancel.request())
        with self.assertRaises(cancel.JobCancelled):
            cancel.check()

    def test_normal_runs_and_timeouts_behave_like_subprocess_run(self):
        self.assertEqual(cancel.run(['true']).returncode, 0)
        self.assertEqual(cancel.run(['false']).returncode, 1)
        with self.assertRaises(subprocess.TimeoutExpired):
            cancel.run(['sleep', '5'], timeout=0.3)


class StopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.app = Application(self.root)
        self.store, self.engine = self.app.store, self.app.engine
        self.engine.runner_factory = lambda: Mock()
        past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        self.store.put('clients', {'id': 'c1', 'name': 'Site', 'url': 'https://site.example', 'status': 'active', 'confirmed': True,
                                   'automation': True, 'subject': 'Floors', 'profile': {'summary': 's'}, 'next_run': past, 'connection': {}})
        self.store.put('articles', {'id': 'a1', 'client_id': 'c1', 'title': 'Floors', 'status': 'planned', 'scheduled_at': past, 'body': ''})

    def tearDown(self):
        cancel.reset()
        self.store.close()
        self.temp.cleanup()

    def test_stopping_queued_scheduled_work_moves_the_schedule_to_next_week(self):
        self.engine.schedule_tick()
        job = [j for j in self.store.all('jobs') if j['kind'] == 'generate'][0]
        self.app.dispatch('POST', '/api/jobs/%s/stop' % job['id'], {})
        self.assertEqual(self.store.get('jobs', job['id'])['status'], 'cancelled')
        self.assertGreater(datetime.fromisoformat(self.store.get('clients', 'c1')['next_run']), datetime.now(timezone.utc))
        self.engine.schedule_tick()
        self.assertFalse([j for j in self.store.all('jobs') if j['status'] == 'queued'])

    def run_stopped_generate(self, body=''):
        self.store.update('articles', 'a1', body=body)
        job = self.engine.queue('generate', 'c1', 'a1')

        def write(runner, client, article, existing, feedback='', progress=None):
            self.engine.stop(job['id'])  # the user presses Stop while Codex is writing
            progress('Still writing')
            raise AssertionError('progress should have stopped the job')
        with patch('rankme.ai.write_article', side_effect=write):
            self.engine.execute(job)
        return self.store.get('jobs', job['id']), self.store.get('articles', 'a1')

    def test_stopping_a_running_article_returns_it_to_the_plan(self):
        job, article = self.run_stopped_generate()
        self.assertEqual((job['status'], job['stage']), ('cancelled', 'Stopped by you'))
        self.assertEqual(article['status'], 'planned')
        self.assertFalse(article.get('error'))

    def test_stopping_a_rewrite_keeps_the_existing_draft_for_review(self):
        _, article = self.run_stopped_generate(body='Existing draft')
        self.assertEqual(article['status'], 'held')
        self.assertEqual(article['body'], 'Existing draft')

    def test_stopped_jobs_do_not_block_the_weekly_schedule(self):
        self.run_stopped_generate()
        self.store.update('clients', 'c1', next_run=(datetime.now(timezone.utc) - timedelta(days=1)).isoformat())
        self.engine.schedule_tick()
        self.assertTrue([j for j in self.store.all('jobs') if j['kind'] == 'generate' and j['status'] == 'queued'])

    def test_publishing_cannot_be_stopped_midway(self):
        job = self.store.put('jobs', {'client_id': 'c1', 'article_id': 'a1', 'kind': 'publish', 'status': 'running'})
        with self.assertRaisesRegex(ValueError, 'cannot be stopped midway'):
            self.engine.stop(job['id'])
        generate = self.store.put('jobs', {'client_id': 'c1', 'article_id': 'a1', 'kind': 'generate', 'status': 'running'})
        with cancel.protected():  # an automatic publish inside a generate job
            with self.assertRaisesRegex(ValueError, 'cannot be stopped safely'):
                self.engine.stop(generate['id'])

    def test_stopping_an_inspection_restores_the_website(self):
        self.store.update('clients', 'c1', status='inspecting', confirmed=False, profile={})
        job = self.store.put('jobs', {'client_id': 'c1', 'kind': 'inspect', 'status': 'queued'})
        self.engine.stop(job['id'])
        self.assertEqual(self.store.get('clients', 'c1')['status'], 'new')

    def test_finished_jobs_cannot_be_stopped_and_failures_can_be_dismissed(self):
        failed = self.store.put('jobs', {'client_id': 'c1', 'article_id': 'a1', 'kind': 'generate', 'status': 'failed'})
        with self.assertRaises(ValueError):
            self.engine.stop(failed['id'])
        self.engine.schedule_tick()
        self.assertFalse([j for j in self.store.all('jobs') if j['status'] == 'queued'])  # the failure blocks the schedule
        self.app.dispatch('POST', '/api/jobs/%s/dismiss' % failed['id'], {})
        self.engine.schedule_tick()
        self.assertTrue([j for j in self.store.all('jobs') if j['status'] == 'queued'])


class DeleteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.app = Application(self.root)
        self.store = self.app.store
        self.store.put('clients', {'id': 'c1', 'name': 'Site', 'url': 'https://site.example', 'status': 'ready', 'connection': {},
                                   'image_brand': {'colors': [], 'references': [{'article_id': 'a1', 'sha256': 'f' * 64}], 'style_spec': 'Spec'}})
        for ident, status in (('a1', 'ready'), ('a2', 'published')):
            self.store.put('articles', {'id': ident, 'client_id': 'c1', 'title': ident, 'status': status,
                                        'cover': {'path': str(self.root / 'covers' / ident / 'cover.png')}})
            for kind in ('covers', 'revisions'):
                folder = self.root / kind / ident
                folder.mkdir(parents=True)
                (folder / 'file').write_text('x')

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_delete_article_removes_its_record_files_and_reference(self):
        self.app.dispatch('POST', '/api/articles/a1/remove', {})
        with self.assertRaises(KeyError):
            self.store.get('articles', 'a1')
        self.assertFalse((self.root / 'covers' / 'a1').exists())
        self.assertFalse((self.root / 'revisions' / 'a1').exists())
        self.assertTrue((self.root / 'covers' / 'a2').exists())
        brand = self.store.get('clients', 'c1')['image_brand']
        self.assertEqual(brand['references'], [])
        self.assertNotIn('style_spec', brand)

    def test_published_articles_can_be_deleted_and_the_log_says_they_stay_live(self):
        self.app.dispatch('POST', '/api/articles/a2/remove', {})
        self.assertTrue(any('stays live' in e['message'] for e in self.store.all('events')))

    def test_delete_refuses_running_work_and_refresh_parents(self):
        self.store.put('jobs', {'client_id': 'c1', 'article_id': 'a1', 'kind': 'cover', 'status': 'running'})
        with self.assertRaisesRegex(ValueError, 'Stop this article'):
            self.app.dispatch('POST', '/api/articles/a1/remove', {})
        self.store.put('articles', {'id': 'a3', 'client_id': 'c1', 'title': 'Refresh', 'status': 'planned', 'refresh_of': 'a2'})
        with self.assertRaisesRegex(ValueError, 'refresh draft'):
            self.app.dispatch('POST', '/api/articles/a2/remove', {})

    def test_delete_cover_holds_the_article_and_keeps_published_covers(self):
        article = self.app.dispatch('POST', '/api/articles/a1/remove-cover', {})
        self.assertIsNone(article['cover'])
        self.assertEqual(article['status'], 'held')
        self.assertFalse((self.root / 'covers' / 'a1').exists())
        with self.assertRaisesRegex(ValueError, 'Published covers'):
            self.app.dispatch('POST', '/api/articles/a2/remove-cover', {})
        self.assertTrue((self.root / 'covers' / 'a2').exists())


if __name__ == '__main__':
    unittest.main()
