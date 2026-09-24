"""Offline regression tests for persistence, workflow gates and local HTTP safety."""
import io
import base64
import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from rankme.engine import Engine, content_digest, profile_digest, brand_digest, parse_date
from rankme.server import Application, handler_for
from rankme.store import Store


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = Application(self.root)
        self.store = self.app.store
        self.engine = self.app.engine
        self.engine.runner_factory = lambda: Mock()
        self.client = self.store.put('clients', {
            'id': 'client1', 'name': 'Test Business', 'url': 'https://example.com',
            'status': 'active', 'confirmed': True, 'automation': True,
            'profile': {'summary': 'A test business'}, 'subject': 'Useful advice',
            'next_run': (datetime.now(timezone.utc) - timedelta(days=35)).isoformat(),
            'connection': {'auto_publish': False}, 'image_brand': {'colors': ['#245B78'], 'style': 'Natural photography'},
        })
        self.article = self.store.put('articles', {
            'id': 'article1', 'client_id': 'client1', 'title': 'Useful advice',
            'slug': 'useful-advice', 'description': 'Advice for clients',
            'body': 'A draft with evidence.', 'sources': [], 'claims': [],
            'status': 'planned', 'subject': 'Useful advice', 'scheduled_at': self.client['next_run'],
        })

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def generated_cover(self, runner=None, client=None, article=None, data_dir=None, **kwargs):
        """Offline image generator boundary; validation still checks a real file and digests."""
        article = article or self.store.get('articles', 'article1')
        folder = self.root / 'covers' / article['id']
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / 'cover.png'
        image = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jhWQAAAAASUVORK5CYII=')
        path.write_bytes(image)
        return {'path': str(path.resolve()), 'format': 'png', 'sha256': hashlib.sha256(image).hexdigest(),
                'alt': 'An editorial cover fixture', 'review': {'passed': True, 'issues': [], 'summary': 'Approved'}}

    def reviewed(self, with_cover=True):
        article = self.store.get('articles', 'article1')
        current_client = self.store.get('clients', 'client1')
        fields = {'status': 'ready', 'review': {'passed': True, 'issues': []},
                  'reviewed_digest': content_digest(article), 'reviewed_profile': profile_digest(current_client)}
        if with_cover:
            cover = self.generated_cover(article=article)
            cover.update(reviewed_article_digest=content_digest(article), reviewed_brand_digest=brand_digest(current_client))
            fields['cover'] = cover
        return self.store.update('articles', 'article1', **fields)

    def test_persistence_round_trip_and_backup(self):
        self.store.settings({'paused': True, 'model': 'test-model'})
        self.store.event('Saved event', 'client1')
        other = Store(self.store.path)
        try:
            self.assertEqual(other.get('clients', 'client1')['profile'], self.client['profile'])
            self.assertTrue(other.settings()['paused'])
            backup = other.backup()
            self.assertEqual(backup['format'], 'rankme-backup-v1')
            self.assertEqual(len(backup['articles']), 1)
            self.assertEqual(backup['events'][0]['message'], 'Saved event')
        finally:
            other.close()

    def test_store_rejects_unknown_collection(self):
        with self.assertRaises(ValueError):
            self.store.all('clients; DROP TABLE clients')
        self.assertEqual(self.store.get('clients', 'client1')['name'], 'Test Business')

    def test_confirmation_required_for_planning_and_automation(self):
        self.store.update('clients', 'client1', confirmed=False, automation=False)
        with self.assertRaisesRegex(ValueError, 'Confirm'):
            self.app.dispatch('POST', '/api/clients/client1/plan', {'subject': 'Advice'})
        with self.assertRaisesRegex(ValueError, 'Confirm'):
            self.app.update_client('client1', {'automation': True})
        self.assertFalse(self.store.all('jobs'))

    def test_unconfirmed_generation_never_calls_ai(self):
        self.store.update('clients', 'client1', confirmed=False)
        job = self.engine.queue('generate', 'client1', 'article1')
        with patch('rankme.ai.write_article') as write:
            self.engine.execute(job)
        write.assert_not_called()
        self.assertEqual(self.store.get('jobs', job['id'])['status'], 'failed')

    def test_profile_edit_requires_reconfirmation(self):
        changed = self.app.update_client('client1', {'profile': {'summary': 'Changed facts'}})
        self.assertFalse(changed['confirmed'])
        self.engine.schedule_tick()
        self.assertFalse(self.store.all('jobs'))

    def test_queue_serializes_per_client_and_validates_ownership(self):
        self.engine.queue('generate', 'client1', 'article1')
        with self.assertRaisesRegex(ValueError, 'queued or running'):
            self.engine.queue('inspect', 'client1')
        self.store.put('clients', {**self.client, 'id': 'client2'})
        with self.assertRaisesRegex(ValueError, 'does not belong'):
            self.engine.queue('generate', 'client2', 'article1')
        self.assertEqual(len(self.store.all('jobs')), 1)

    def test_pause_allowed_while_other_configuration_is_locked(self):
        self.engine.queue('inspect', 'client1')
        changed = self.app.update_client('client1', {'automation': False})
        self.assertFalse(changed['automation'])
        with self.assertRaisesRegex(ValueError, 'Wait'):
            self.app.update_client('client1', {'subject': 'Changed subject'})

    def test_schedule_catches_up_only_once_and_advances_to_future(self):
        for i in range(2, 5):
            self.store.put('articles', {**self.article, 'id': f'article{i}',
                                       'scheduled_at': (parse_date(self.client['next_run']) + timedelta(days=i * 7)).isoformat()})
        self.engine.schedule_tick()
        self.engine.schedule_tick()
        jobs = self.store.all('jobs')
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]['article_id'], 'article1')
        self.assertTrue(jobs[0]['scheduled'])
        self.store.update('jobs', jobs[0]['id'], status='completed')
        self.store.update('articles', 'article1', status='ready')
        self.engine.advance('client1')
        due = parse_date(self.store.get('clients', 'client1')['next_run'])
        current = datetime.now(timezone.utc)
        self.assertGreater(due, current)
        self.assertLessEqual(due, current + timedelta(days=7))
        self.engine.schedule_tick()
        self.assertEqual(len(self.store.all('jobs')), 1)

    def test_global_pause_and_failed_jobs_block_schedule(self):
        self.store.settings({'paused': True})
        self.engine.schedule_tick()
        self.assertFalse(self.store.all('jobs'))
        self.store.settings({'paused': False})
        job = self.engine.queue('generate', 'client1', 'article1')
        self.store.update('jobs', job['id'], status='failed')
        self.engine.schedule_tick()
        self.assertEqual(len(self.store.all('jobs')), 1)

    def test_planning_deduplicates_topics_and_spaces_weeks(self):
        topics = {'articles': [{'title': 'Useful advice'}, {'title': 'New topic'}, {'title': ' new topic '}, {'title': 'Another topic'}]}
        job = self.engine.queue('plan', 'client1')
        with patch('rankme.ai.plan_articles', return_value=topics):
            self.engine.execute(job)
        articles = self.engine.articles('client1')
        self.assertEqual(len(articles), 3)
        dates = [parse_date(a['scheduled_at']) for a in articles]
        self.assertEqual(dates[1] - dates[0], timedelta(days=7))
        self.assertEqual(dates[2] - dates[1], timedelta(days=7))

    def test_generation_records_review_digests_without_publishing(self):
        job = self.engine.queue('generate', 'client1', 'article1')
        with patch('rankme.ai.write_article', return_value={'body': 'New supported draft'}), \
             patch('rankme.ai.review_article', return_value={'passed': True, 'issues': [], 'score': 90}), \
             patch('rankme.engine.check_article', return_value={'passed': True, 'issues': []}), \
             patch('rankme.covers.generate_cover', side_effect=self.generated_cover), \
             patch('rankme.publisher.publish_article') as publish:
            self.engine.execute(job)
        article = self.store.get('articles', 'article1')
        self.assertEqual(article['status'], 'ready')
        self.assertEqual(article['reviewed_digest'], content_digest(article))
        self.assertEqual(article['reviewed_profile'], profile_digest(self.client))
        publish.assert_not_called()
        self.assertTrue(list((self.root / 'revisions' / 'article1').glob('*.json')))

    def test_failed_review_repairs_once_then_holds_without_publishing(self):
        self.store.update('clients', 'client1', connection={'auto_publish': True})
        job = self.engine.queue('generate', 'client1', 'article1')
        with patch('rankme.ai.write_article', return_value={'body': 'Unsupported draft'}) as write, \
             patch('rankme.ai.review_article', return_value={'passed': False, 'issues': ['Unsupported claim']}), \
             patch('rankme.engine.check_article', return_value={'passed': True, 'issues': []}), \
             patch('rankme.publisher.publish_article') as publish:
            self.engine.execute(job)
        self.assertEqual(write.call_count, 2)
        self.assertEqual(self.store.get('articles', 'article1')['status'], 'held')
        publish.assert_not_called()

    def test_article_edit_invalidates_review_and_prevents_publication(self):
        self.reviewed()
        article = self.app.dispatch('PATCH', '/api/articles/article1', {'body': 'Edited claim'})
        self.assertEqual(article['status'], 'held')
        self.assertEqual(article['reviewed_digest'], '')
        with patch('rankme.publisher.publish_article') as publish:
            with self.assertRaisesRegex(ValueError, 'successful review'):
                self.engine.publish(self.client, article, lambda _: None)
        publish.assert_not_called()

    def test_changed_claims_or_profile_cannot_reuse_review(self):
        article = self.reviewed()
        with self.assertRaisesRegex(ValueError, 'successful review'):
            self.engine.publish(self.client, {**article, 'claims': ['new claim']}, lambda _: None)
        changed_client = {**self.client, 'profile': {'summary': 'New business'}}
        with self.assertRaisesRegex(ValueError, 'Business profile changed'):
            self.engine.publish(changed_client, article, lambda _: None)

    def test_publish_rechecks_structure_and_exports_without_project(self):
        article = self.reviewed()
        with patch('rankme.engine.check_article', return_value={'passed': False, 'issues': ['Unsafe link']}):
            with self.assertRaisesRegex(ValueError, 'Publication checks failed'):
                self.engine.publish(self.client, article, lambda _: None)
        with patch('rankme.engine.check_article', return_value={'passed': True, 'issues': []}), \
             patch('rankme.publisher.publish_article', return_value={'status': 'exported', 'path': 'article.md'}) as publish:
            self.engine.publish(self.client, article, lambda _: None)
        connection = publish.call_args.args[0]['connection']
        self.assertEqual(connection['mode'], 'export')
        self.assertEqual(connection['deploy_command'], [])
        self.assertTrue(str(connection['project_path']).startswith(str(self.root.resolve())))
        self.assertEqual(self.store.get('articles', 'article1')['status'], 'exported')

    def test_interrupted_generation_retries_publication_not_writing(self):
        self.store.update('articles', 'article1', publish_started=True, status='error')
        old = self.store.put('jobs', {'kind': 'generate', 'client_id': 'client1', 'article_id': 'article1', 'status': 'failed', 'scheduled': True})
        retry = self.engine.retry(old['id'])
        self.assertEqual(retry['kind'], 'publish')
        self.assertTrue(retry['scheduled'])
        self.assertEqual(self.store.get('jobs', old['id'])['status'], 'retried')
        with self.assertRaisesRegex(ValueError, 'Only failed'):
            self.engine.retry(old['id'])

    def test_verification_retry_is_bounded(self):
        self.store.update('clients', 'client1', next_run=(datetime.now(timezone.utc)+timedelta(days=1)).isoformat())
        self.store.update('articles', 'article1', status='verification_pending', verification_attempts=12,
                          last_verified_at=(datetime.now(timezone.utc)-timedelta(minutes=10)).isoformat())
        self.engine.schedule_tick()
        self.assertFalse(self.store.all('jobs'))
        self.store.update('articles', 'article1', verification_attempts=11)
        self.engine.schedule_tick()
        self.assertEqual(self.store.all('jobs')[0]['kind'], 'verify')

    def test_future_article_date_is_respected_when_client_is_overdue(self):
        future = (datetime.now(timezone.utc) + timedelta(days=14)).isoformat()
        self.store.update('articles', 'article1', scheduled_at=future)
        self.engine.schedule_tick()
        self.assertFalse(self.store.all('jobs'))

    def test_new_subject_supersedes_old_plan_only_after_success(self):
        self.store.update('clients', 'client1', subject='New direction')
        job = self.engine.queue('plan', 'client1')
        with patch('rankme.ai.plan_articles', return_value={'articles': [{'title': 'A fresh topic'}]}):
            self.engine.execute(job)
        self.assertEqual(self.store.get('articles', 'article1')['status'], 'superseded')
        planned = [a for a in self.store.all('articles') if a['status'] == 'planned']
        self.assertEqual(len(planned), 1)
        self.assertEqual(planned[0]['subject'], 'New direction')

    def test_client_pause_during_review_prevents_auto_publish(self):
        self.store.update('clients', 'client1', connection={'auto_publish': True})
        job = self.engine.queue('generate', 'client1', 'article1')
        def review(*args, **kwargs):
            self.app.update_client('client1', {'automation': False})
            return {'passed': True, 'issues': []}
        with patch('rankme.ai.write_article', return_value={'body': 'A supported draft'}), \
             patch('rankme.ai.review_article', side_effect=review), \
             patch('rankme.engine.check_article', return_value={'passed': True, 'issues': []}), \
             patch('rankme.covers.generate_cover', side_effect=self.generated_cover), \
             patch('rankme.publisher.publish_article') as publish:
            self.engine.execute(job)
        publish.assert_not_called()
        self.assertEqual(self.store.get('articles', 'article1')['status'], 'ready')

    def test_failed_publication_cannot_be_edited_or_regenerated(self):
        self.store.update('articles', 'article1', publish_started=True, status='error')
        with self.assertRaisesRegex(ValueError, 'preserved'):
            self.app.dispatch('PATCH', '/api/articles/article1', {'body': 'Changed after publication'})
        job = self.engine.queue('generate', 'client1', 'article1')
        with patch('rankme.ai.write_article') as write:
            self.engine.execute(job)
        write.assert_not_called()
        self.assertEqual(self.store.get('jobs', job['id'])['status'], 'failed')

    def test_manual_retry_clears_stale_failure_and_unblocks_future_schedule(self):
        old = self.store.put('jobs', {'kind': 'generate', 'client_id': 'client1',
                                      'article_id': 'article1', 'status': 'failed'})
        replacement = self.engine.queue('generate', 'client1', 'article1')
        self.assertEqual(self.store.get('jobs', old['id'])['status'], 'retried')
        self.store.update('jobs', replacement['id'], status='completed')
        self.store.update('articles', 'article1', status='ready')
        self.store.put('articles', {**self.article, 'id': 'article2', 'title': 'Next topic'})
        self.engine.schedule_tick()
        queued = [job for job in self.store.all('jobs') if job['status'] == 'queued']
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0]['article_id'], 'article2')
        self.assertTrue(queued[0]['scheduled'])

    def test_manual_retry_preserves_unrelated_failures(self):
        cases = [('verify', 'client1', 'article1'), ('generate', 'client1', 'article2'),
                 ('generate', 'client2', 'article1')]
        old = [self.store.put('jobs', {'kind': kind, 'client_id': client_id,
                                     'article_id': article_id, 'status': 'failed'})
               for kind, client_id, article_id in cases]
        self.engine.queue('generate', 'client1', 'article1')
        for job in old:
            self.assertEqual(self.store.get('jobs', job['id'])['status'], 'failed')

    def test_manual_publication_recovers_failed_generate_without_rewriting(self):
        self.reviewed()
        self.store.update('articles', 'article1', status='error', publish_started=True)
        old = self.store.put('jobs', {'kind': 'generate', 'client_id': 'client1',
                                      'article_id': 'article1', 'status': 'failed'})
        job = self.engine.queue('publish', 'client1', 'article1')
        self.assertEqual(self.store.get('jobs', old['id'])['status'], 'retried')
        with patch('rankme.engine.check_article', return_value={'passed': True, 'issues': []}), \
             patch('rankme.publisher.publish_article', return_value={'status': 'exported', 'path': 'recovered.md'}) as publish, \
             patch('rankme.ai.write_article') as write:
            self.engine.execute(job)
        publish.assert_called_once()
        write.assert_not_called()
        self.assertEqual(self.store.get('jobs', job['id'])['status'], 'completed')
        self.assertEqual(self.store.get('articles', 'article1')['status'], 'exported')

    def test_schedule_change_shifts_planned_only_in_existing_order(self):
        old_date = parse_date(self.article['scheduled_at'])
        for index in (2, 3):
            self.store.put('articles', {**self.article, 'id': f'article{index}',
                                       'scheduled_at': (old_date + timedelta(days=index * 9)).isoformat()})
        preserved = {}
        for index, status in enumerate(('ready', 'held', 'published', 'superseded'), start=4):
            article = self.store.put('articles', {**self.article, 'id': f'article{index}', 'status': status})
            preserved[article['id']] = article['scheduled_at']
        due = (datetime.now(timezone.utc) + timedelta(days=2)).replace(microsecond=0)
        self.app.update_client('client1', {'next_run': due.isoformat()})
        for index, ident in enumerate(('article1', 'article2', 'article3')):
            self.assertEqual(parse_date(self.store.get('articles', ident)['scheduled_at']), due + timedelta(days=index * 7))
        for ident, unchanged_date in preserved.items():
            self.assertEqual(self.store.get('articles', ident)['scheduled_at'], unchanged_date)
        self.assertEqual(parse_date(self.store.get('clients', 'client1')['next_run']), due)

    def test_publish_refuses_missing_cover(self):
        article = self.reviewed(with_cover=False)
        with patch('rankme.publisher.publish_article') as publish:
            with self.assertRaisesRegex(ValueError, 'reviewed cover'):
                self.engine.publish(self.client, article, lambda _: None)
        publish.assert_not_called()

    def test_cover_bytes_or_article_edits_invalidate_binding(self):
        article = self.reviewed()
        self.assertTrue(self.engine.cover_valid(self.client, article))
        edited = self.app.dispatch('PATCH', '/api/articles/article1', {'body': 'A changed article'})
        self.assertFalse(self.engine.cover_valid(self.client, edited))
        article = self.reviewed()
        Path(article['cover']['path']).write_bytes(b'changed image bytes')
        self.assertFalse(self.engine.cover_valid(self.client, article))
        with self.assertRaisesRegex(ValueError, 'reviewed cover'):
            self.engine.publish(self.client, article, lambda _: None)

    def test_brand_change_invalidates_cover_without_changing_text_review(self):
        article = self.reviewed()
        changed = self.app.update_client('client1', {'image_brand': {'colors': ['#123456'], 'style': 'Natural light'}})
        self.assertEqual(profile_digest(changed), article['reviewed_profile'])
        self.assertFalse(self.engine.cover_valid(changed, article))
        with self.assertRaisesRegex(ValueError, 'reviewed cover'):
            self.engine.publish(changed, article, lambda _: None)

    def test_generation_creates_cover_only_after_successful_text_review(self):
        order = []
        def reviewed(*args, **kwargs):
            order.append('text review')
            return {'passed': True, 'issues': []}
        def generate_cover(*args, **kwargs):
            order.append('cover generation')
            self.assertEqual(self.store.get('articles', 'article1')['reviewed_digest'], content_digest(args[2]))
            return self.generated_cover(*args, **kwargs)
        job = self.engine.queue('generate', 'client1', 'article1')
        with patch('rankme.ai.write_article', return_value={'body': 'Supported text'}), \
             patch('rankme.ai.review_article', side_effect=reviewed), \
             patch('rankme.engine.check_article', return_value={'passed': True, 'issues': []}), \
             patch('rankme.covers.generate_cover', side_effect=generate_cover) as cover:
            self.engine.execute(job)
        self.assertEqual(order, ['text review', 'cover generation'])
        cover.assert_called_once()
        article = self.store.get('articles', 'article1')
        self.assertEqual(article['status'], 'ready')
        self.assertTrue(self.engine.cover_valid(self.client, article))

    def test_detected_palette_is_used_for_immediate_auto_publish(self):
        self.store.update('clients', 'client1', image_brand={}, connection={'auto_publish': True})
        job = self.engine.queue('generate', 'client1', 'article1')
        with patch('rankme.brand.extract_brand', return_value={'colors': ['#245B78'], 'style': ''}), \
             patch('rankme.ai.write_article', return_value={'body': 'Supported text'}), \
             patch('rankme.ai.review_article', return_value={'passed': True, 'issues': []}), \
             patch('rankme.engine.check_article', return_value={'passed': True, 'issues': []}), \
             patch('rankme.covers.generate_cover', side_effect=self.generated_cover), \
             patch('rankme.publisher.publish_article', return_value={'status': 'exported', 'path': 'cover-post.md'}) as publish:
            self.engine.execute(job)
        publish.assert_called_once()
        self.assertEqual(self.store.get('articles', 'article1')['status'], 'exported')
        self.assertEqual(self.store.get('jobs', job['id'])['status'], 'completed')

    def test_failed_text_review_never_generates_cover(self):
        job = self.engine.queue('generate', 'client1', 'article1')
        with patch('rankme.ai.write_article', return_value={'body': 'Unsupported text'}), \
             patch('rankme.ai.review_article', return_value={'passed': False, 'issues': ['Unsupported']}), \
             patch('rankme.engine.check_article', return_value={'passed': True, 'issues': []}), \
             patch('rankme.covers.generate_cover') as cover:
            self.engine.execute(job)
        cover.assert_not_called()
        self.assertEqual(self.store.get('articles', 'article1')['status'], 'held')

    def test_cover_generation_failure_retries_cover_without_rewriting(self):
        job = self.engine.queue('generate', 'client1', 'article1')
        with patch('rankme.ai.write_article', return_value={'body': 'Supported text'}) as write, \
             patch('rankme.ai.review_article', return_value={'passed': True, 'issues': []}), \
             patch('rankme.engine.check_article', return_value={'passed': True, 'issues': []}), \
             patch('rankme.covers.generate_cover', side_effect=RuntimeError('Image tool unavailable')):
            self.engine.execute(job)
        write.assert_called_once()
        self.assertEqual(self.store.get('jobs', job['id'])['status'], 'failed')
        retry = self.engine.retry(job['id'])
        self.assertEqual(retry['kind'], 'cover')
        self.assertTrue(retry['resume_generation'])
        with patch('rankme.covers.generate_cover', side_effect=self.generated_cover) as cover, \
             patch('rankme.ai.write_article') as write_again:
            self.engine.execute(retry)
        cover.assert_called_once()
        write_again.assert_not_called()
        self.assertEqual(self.store.get('articles', 'article1')['status'], 'ready')

    def test_failed_visual_review_holds_article_and_prevents_publish(self):
        article = self.reviewed(with_cover=False)
        failed = self.generated_cover(article=article)
        failed['review'] = {'passed': False, 'issues': ['Unnatural hands'], 'summary': 'Needs revision'}
        with patch('rankme.covers.generate_cover', return_value=failed):
            result = self.engine.prepare_cover(self.client, article, lambda _: None)
        self.assertEqual(result['status'], 'held')
        self.assertFalse(self.engine.cover_valid(self.client, result))
        with self.assertRaisesRegex(ValueError, 'reviewed cover'):
            self.engine.publish(self.client, result, lambda _: None)

    def test_cover_http_rejects_external_paths_symlinks_and_changed_bytes(self):
        article = self.reviewed()
        self.store.put('articles', {**article, 'id': 'abc123'})
        self.assertEqual(self.request('GET', '/api/articles/abc123/cover').args[0], 200)
        outside = self.root / 'private.txt'
        outside.write_bytes(b'private content')
        cover = {**article['cover'], 'path': str(outside), 'sha256': hashlib.sha256(outside.read_bytes()).hexdigest()}
        self.store.update('articles', 'abc123', cover=cover)
        self.assertEqual(self.request('GET', '/api/articles/abc123/cover').args[0], 404)
        link = self.root / 'covers' / 'escape.png'
        link.symlink_to(outside)
        self.store.update('articles', 'abc123', cover={**cover, 'path': str(link)})
        self.assertEqual(self.request('GET', '/api/articles/abc123/cover').args[0], 404)
        self.store.update('articles', 'abc123', cover=article['cover'])
        Path(article['cover']['path']).write_bytes(b'tampered')
        self.assertEqual(self.request('GET', '/api/articles/abc123/cover').args[0], 400)

    def test_image_brand_validation_is_separate_from_business_profile(self):
        result = self.app.update_client('client1', {'image_brand': {'colors': ['#aabbcc', '#AABBCC'], 'style': 'Natural photography'}})
        self.assertEqual(result['image_brand']['colors'], ['#AABBCC'])
        self.assertEqual(result['profile'], self.client['profile'])
        for value in ('invalid', {'colors': 'red'}, {'colors': ['red']}, {'colors': ['#123456'] * 7}):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.app.update_client('client1', {'image_brand': value})

    def request(self, method, path, headers=None, body=b'{}'):
        """Exercise the actual handler without opening a socket."""
        cls = handler_for(self.app)
        handler = object.__new__(cls)
        handler.server = SimpleNamespace(server_port=8787)
        handler.path = path
        handler.headers = {'Host': '127.0.0.1:8787', **(headers or {})}
        handler.rfile = io.BytesIO(body)
        handler.send = Mock()
        handler.handle_request(method)
        return handler.send.call_args

    def test_http_rejects_foreign_hosts_origins_and_cross_site_reads(self):
        for headers in ({'Host': 'evil.test:8787'}, {'Origin': 'https://evil.test'}, {'Sec-Fetch-Site': 'cross-site'}):
            with self.subTest(headers=headers):
                self.assertEqual(self.request('GET', '/api/session', headers).args[0], 403)
        self.assertEqual(self.request('GET', '/api/session').args[0], 200)

    def test_http_requires_token_json_and_bounded_body(self):
        self.assertEqual(self.request('PATCH', '/api/settings').args[0], 403)
        auth = {'X-RankMe-Token': self.app.token}
        self.assertEqual(self.request('PATCH', '/api/settings', auth).args[0], 415)
        auth['Content-Type'] = 'application/json'
        self.assertEqual(self.request('PATCH', '/api/settings', {**auth, 'Content-Length': '2000001'}).args[0], 413)
        payload = json.dumps({'paused': True}).encode()
        result = self.request('PATCH', '/api/settings', {**auth, 'Content-Length': str(len(payload))}, payload)
        self.assertEqual(result.args[0], 200)
        self.assertTrue(self.store.settings()['paused'])

    def test_http_blocks_static_path_traversal(self):
        for path in ('/../rankme/store.py', '/static/../../data/rankme.sqlite3', '/data/rankme.sqlite3'):
            self.assertEqual(self.request('GET', path).args[0], 404)

    def test_timezone_required(self):
        with self.assertRaisesRegex(ValueError, 'timezone'):
            parse_date('2026-09-22T09:00:00')


if __name__ == '__main__':
    unittest.main()
