import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

from rankme.publisher import PublishError, export_markdown, publish_article, refresh_baseline


class RefreshPublisherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'site'
        self.root.mkdir()
        self.storage = Path(self.temp.name) / 'data'
        self.client = {'connection': {'project_path': str(self.root), 'content_dir': 'posts', 'mode': 'export', 'format': 'md'}}
        self.original = {'id': 'original', 'slug': 'useful-guide', 'title': 'Useful guide',
                         'body': 'Original supported content.', 'description': 'A useful description',
                         'created_at': '2025-01-01T00:00:00+00:00'}

    def publish(self, article):
        return publish_article(self.client, article, data_dir=self.storage)

    def draft(self, legacy=False):
        result = self.publish(self.original)
        self.original['publish_result'] = result if not legacy else {'path': result['path']}
        baseline = refresh_baseline(self.client, self.original)
        draft = dict(self.original, id='revision-1', body='Refreshed supported content.',
                     created_at='2026-01-01T00:00:00+00:00', refresh_of='original', refresh_baseline=baseline)
        draft.pop('publish_result', None)
        return draft, Path(result['path'])

    def test_refresh_preserves_identity_slug_date_and_backups(self):
        draft, path = self.draft()
        previous = path.read_bytes()
        result = self.publish(draft)
        self.assertEqual(result['publication_id'], 'original')
        self.assertEqual(result['file_sha256'], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertIn('rankme_id: "original"', path.read_text())
        self.assertIn('date: "2025-01-01T00:00:00+00:00"', path.read_text())
        self.assertIn('Refreshed supported content.', path.read_text())
        self.assertEqual(list((self.storage / 'revisions').glob('*/*.md'))[0].read_bytes(), previous)
        before = path.stat().st_mtime_ns
        self.publish(draft)
        self.assertEqual(path.stat().st_mtime_ns, before)

    def test_manual_change_after_baseline_never_overwritten(self):
        draft, path = self.draft()
        path.write_text(path.read_text() + '\nHuman changes')
        expected = path.read_bytes()
        with self.assertRaises(PublishError):
            self.publish(draft)
        self.assertEqual(path.read_bytes(), expected)

    def test_manual_change_before_baseline_detected_with_and_without_hash(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                result = self.publish(self.original)
                self.original['publish_result'] = result if not legacy else {}
                path = Path(result['path'])
                pristine = path.read_text()
                path.write_text(pristine + '\nManual content')
                with self.assertRaises(PublishError):
                    refresh_baseline(self.client, self.original)
                path.write_text(pristine)

    def test_changed_connection_or_slug_rejected(self):
        draft, path = self.draft()
        for setting, value in (('content_dir', 'elsewhere'), ('format', 'json'), ('auto_publish', True),
                               ('deploy_command', [sys.executable, '-c', 'pass'])):
            connection = copy.deepcopy(self.client['connection'])
            self.client['connection'][setting] = value
            with self.subTest(setting=setting), self.assertRaises(PublishError):
                self.publish(draft)
            self.client['connection'] = connection
        with self.assertRaises(PublishError):
            self.publish(dict(draft, slug='other-path'))
        self.assertIn('Original supported content.', path.read_text())

    def test_baseline_path_is_never_used_as_write_destination(self):
        draft, path = self.draft()
        victim = self.root / 'human.md'
        victim.write_text('Human work')
        draft['refresh_baseline']['path'] = str(victim)
        self.publish(draft)
        self.assertEqual(victim.read_text(), 'Human work')
        self.assertIn('Refreshed supported content.', path.read_text())

    def test_missing_or_unrelated_file_never_recreated(self):
        draft, path = self.draft()
        path.unlink()
        with self.assertRaises(PublishError):
            self.publish(draft)
        self.assertFalse(path.exists())
        path.write_text('Unrelated article')
        with self.assertRaises(PublishError):
            self.publish(draft)
        self.assertEqual(path.read_text(), 'Unrelated article')

    def test_future_refresh_chain_keeps_original_publication_identity(self):
        draft, path = self.draft()
        draft['publish_result'] = self.publish(draft)
        second = dict(draft, id='revision-2', body='Second refresh.', refresh_of=draft['id'],
                      refresh_baseline=refresh_baseline(self.client, draft))
        second.pop('publish_result')
        self.assertEqual(second['refresh_baseline']['publication_id'], 'original')
        result = self.publish(second)
        self.assertEqual(result['publication_id'], 'original')
        self.assertIn('rankme_id: "original"', path.read_text())
        self.assertIn('Second refresh.', path.read_text())

    def test_legacy_baseline_and_json_refresh_retry(self):
        self.client['connection']['format'] = 'json'
        draft, path = self.draft(legacy=True)
        result = self.publish(draft)
        self.assertEqual(json.loads(path.read_text())['rankme_id'], 'original')
        before = path.read_bytes()
        self.publish(draft)
        self.assertEqual(path.read_bytes(), before)
        data = json.loads(path.read_text())
        data['claims'] = [{'claim': 'A human update'}]
        path.write_text(json.dumps(data))
        with self.assertRaises(PublishError):
            self.publish(draft)

    def test_legacy_json_additional_manual_fields_rejected(self):
        self.client['connection']['format'] = 'json'
        result = self.publish(self.original)
        path = Path(result['path'])
        data = json.loads(path.read_text())
        data['custom_human_field'] = 'Preserve me'
        path.write_text(json.dumps(data))
        with self.assertRaises(PublishError):
            refresh_baseline(self.client, self.original)

    def test_failed_refresh_build_restores_original(self):
        self.client['connection']['build_command'] = [sys.executable, '-c', 'from pathlib import Path; import sys; sys.exit(1 if "Refreshed" in Path("posts/useful-guide.md").read_text() else 0)']
        draft, path = self.draft()
        previous = path.read_bytes()
        with self.assertRaises(PublishError):
            self.publish(draft)
        self.assertEqual(path.read_bytes(), previous)

    def test_missing_baseline_and_wrong_source_rejected(self):
        draft, path = self.draft()
        for broken in (None, {}, {**draft['refresh_baseline'], 'source_article_id': 'someone-else'}):
            with self.subTest(baseline=broken), self.assertRaises(PublishError):
                self.publish(dict(draft, refresh_baseline=broken))

    def test_competing_refresh_cannot_overwrite_newer_revision(self):
        draft, path = self.draft()
        stale = dict(draft, id='competing-revision', body='Competing content.')
        self.publish(draft)
        expected = path.read_bytes()
        with self.assertRaises(PublishError):
            self.publish(stale)
        self.assertEqual(path.read_bytes(), expected)

    def test_refresh_with_new_reviewed_cover_keeps_identity_and_retries(self):
        def cover(ident, payload):
            source = self.storage / 'covers' / ident / 'cover.png'
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(payload)
            return {'path': str(source), 'alt': 'Useful guide illustration', 'format': 'png',
                    'sha256': hashlib.sha256(payload).hexdigest(), 'review': {'passed': True, 'issues': []}}
        self.original['cover'] = cover('original', b'\x89PNG\r\n\x1a\noriginal')
        draft, path = self.draft()
        old_image = Path(self.original['publish_result']['image_path'])
        draft['cover'] = cover('revision-1', b'\x89PNG\r\n\x1a\nrefreshed')
        result = self.publish(draft)
        self.assertNotEqual(result['image_path'], str(old_image))
        self.assertTrue(old_image.exists())
        self.assertIn(result['image_url'], path.read_text())
        self.assertEqual(self.publish(draft)['file_sha256'], result['file_sha256'])
        draft['publish_result'] = result
        next_baseline = refresh_baseline(self.client, draft)
        self.assertEqual(next_baseline['publication_id'], 'original')
        self.assertEqual(next_baseline['file_sha256'], result['file_sha256'])

    def test_symlink_source_and_changed_retry_date_rejected(self):
        draft, path = self.draft()
        self.publish(draft)
        path.write_text(path.read_text().replace('2025-01-01T00:00:00+00:00', '2024-01-01T00:00:00+00:00'))
        with self.assertRaises(PublishError):
            self.publish(draft)
        copy_path = self.root / 'other.md'
        path.rename(copy_path)
        path.symlink_to(copy_path)
        with self.assertRaises(PublishError):
            refresh_baseline(self.client, self.original)
        with self.assertRaises(PublishError):
            self.publish(draft)

    def test_entity_encoded_and_control_obfuscated_schemes_rejected(self):
        for body in ('[click](jav&#x61;script:alert(1))', '[click](java&#9;script:alert(1))',
                     '[click](vb&#115;cript:evil)', '[click](d&#x61;ta:text/html,test)',
                     '&lt;script&gt;alert(1)&lt;/script&gt;'):
            with self.subTest(body=body), self.assertRaises(PublishError):
                export_markdown(dict(self.original, body=body))


if __name__ == '__main__':
    unittest.main()
