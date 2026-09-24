import json
import hashlib
import base64
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rankme.publisher import PublishError, export_markdown, publish_article, validate_connection, verify_live


class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'site'
        self.root.mkdir()
        self.data_dir = Path(self.temp.name) / 'data'
        self.article = {'id': 'article-1', 'slug': 'first-post', 'title': 'First post', 'description': 'Useful advice', 'body': 'Evidence-backed advice.\n\n[Source](https://example.com)'}
        self.client = {'connection': {'project_path': str(self.root), 'content_dir': 'content/blog', 'format': 'md', 'mode': 'export'}}

    def publish(self, article, **kwargs):
        kwargs.setdefault('data_dir', self.data_dir)
        return publish_article(self.client, article, **kwargs)

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args):
        return subprocess.check_output(['git'] + list(args), cwd=self.root, stderr=subprocess.DEVNULL, text=True).strip()

    def repository(self):
        self.git('init', '-b', 'main')
        self.git('config', 'user.name', 'RankMe Test')
        self.git('config', 'user.email', 'rankme@example.test')
        (self.root / 'README').write_text('test')
        self.git('add', 'README')
        self.git('commit', '-m', 'Initial')
        self.client['connection'].update(mode='git', branch='main', build_command=[sys.executable, '-c', 'pass'])

    def test_export_is_idempotent_and_not_publish(self):
        first = self.publish(self.article)
        before = Path(first['path']).stat().st_mtime_ns
        second = self.publish(self.article)
        self.assertEqual(second['status'], 'exported')
        self.assertEqual(Path(second['path']).stat().st_mtime_ns, before)
        self.assertIn('rankme_id: "article-1"', Path(first['path']).read_text())

    def test_unsafe_slug_and_mdx(self):
        with self.assertRaises(PublishError):
            self.publish(dict(self.article, slug='../oops'))
        self.client['connection']['format'] = 'mdx'
        with self.assertRaises(PublishError):
            self.publish(dict(self.article, body='{process.exit()}'))
        with self.assertRaises(PublishError):
            export_markdown(dict(self.article, body='<script>alert(1)</script>'))

    def test_unrelated_existing_file(self):
        folder = self.root / 'content/blog'
        folder.mkdir(parents=True)
        (folder / 'first-post.md').write_text('Human article')
        with self.assertRaises(PublishError):
            self.publish(self.article)
        self.assertEqual((folder / 'first-post.md').read_text(), 'Human article')

    def test_symlink_escape_and_traversal(self):
        (self.root / 'outside').symlink_to(self.root.parent, target_is_directory=True)
        self.client['connection']['content_dir'] = 'outside'
        self.assertFalse(validate_connection(self.client['connection'])['ok'])
        self.client['connection']['content_dir'] = '../escape'
        self.assertFalse(validate_connection(self.client['connection'])['ok'])

    def test_build_failure_restores_file(self):
        self.repository()
        self.client['connection']['build_command'] = [sys.executable, '-c', 'raise SystemExit(1)']
        with self.assertRaises(PublishError):
            self.publish(self.article)
        self.assertFalse((self.root / 'content/blog/first-post.md').exists())
        self.assertEqual(self.git('status', '--porcelain'), '')
        self.assertEqual(self.git('rev-list', '--count', 'HEAD'), '1')

    def test_dirty_repo_refused(self):
        self.repository()
        (self.root / 'other').write_text('User work')
        with self.assertRaises(PublishError):
            self.publish(self.article)
        self.assertEqual((self.root / 'other').read_text(), 'User work')

    def test_failed_push_retry_retains_single_commit(self):
        self.repository()
        self.client['connection']['auto_publish'] = True
        with self.assertRaises(PublishError):
            self.publish(self.article)
        self.assertEqual(self.git('rev-list', '--count', 'HEAD'), '2')
        remote = self.root.parent / (self.root.name + '-remote.git')
        try:
            subprocess.check_call(['git', 'init', '--bare', str(remote)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.git('remote', 'add', 'origin', str(remote))
            with patch('rankme.publisher.verify_live', return_value={'ok': False, 'message': 'Pending'}):
                result = self.publish(self.article)
            self.assertEqual(result['status'], 'verification_pending')
            self.assertEqual(self.git('rev-list', '--count', 'HEAD'), '2')
        finally:
            import shutil
            shutil.rmtree(remote, ignore_errors=True)

    def test_json_export_and_manual_edit(self):
        self.client['connection']['format'] = 'json'
        result = self.publish(self.article)
        self.assertEqual(json.loads(Path(result['path']).read_text())['rankme_id'], self.article['id'])
        self.client['connection']['format'] = 'md'
        result = self.publish(self.article)
        path = Path(result['path'])
        path.write_text(path.read_text() + 'Manual edit')
        with self.assertRaises(PublishError):
            self.publish(self.article)

    def test_crash_after_file_write_resumes_single_commit(self):
        self.repository()
        path = self.root / 'content/blog/first-post.md'
        path.parent.mkdir(parents=True)
        path.write_text(export_markdown(self.article))
        result = self.publish(self.article)
        self.assertEqual(result['status'], 'exported')
        self.assertEqual(self.git('rev-list', '--count', 'HEAD'), '2')
        self.publish(self.article)
        self.assertEqual(self.git('rev-list', '--count', 'HEAD'), '2')
        self.assertEqual(self.git('status', '--porcelain'), '')

    def test_crash_after_staging_resumes_single_commit(self):
        self.repository()
        path = self.root / 'content/blog/first-post.md'
        path.parent.mkdir(parents=True)
        path.write_text(export_markdown(self.article))
        self.git('add', 'content/blog/first-post.md')
        self.publish(self.article)
        self.assertEqual(self.git('rev-list', '--count', 'HEAD'), '2')
        self.assertEqual(self.git('status', '--porcelain'), '')

    def test_crash_recovery_does_not_allow_other_dirty_files(self):
        self.repository()
        path = self.root / 'content/blog/first-post.md'
        path.parent.mkdir(parents=True)
        path.write_text(export_markdown(self.article))
        (self.root / 'README').write_text('User edit')
        with self.assertRaises(PublishError):
            self.publish(self.article)
        self.assertEqual(self.git('rev-list', '--count', 'HEAD'), '1')
        self.assertEqual((self.root / 'README').read_text(), 'User edit')

    def test_same_article_manual_frontmatter_edit_refused(self):
        result = self.publish(self.article)
        path = Path(result['path'])
        path.write_text(path.read_text().replace('title: "First post"', 'title: "Human title"'))
        with self.assertRaises(PublishError):
            self.publish(self.article)

    def test_live_verification_decodes_visible_title(self):
        with patch('rankme.crawler.fetch_public', return_value={'text': '<h1>Coffee &amp; <em>Tea</em></h1>', 'status': 200}):
            self.assertTrue(verify_live('https://example.com/blog/coffee', 'Coffee & Tea')['ok'])
            self.assertFalse(verify_live('https://example.com/blog/coffee', 'Other topic')['ok'])
            self.assertFalse(verify_live('https://example.com/blog/coffee', '')['ok'])
        with patch('rankme.crawler.fetch_public', return_value={'text': 'Coffee & Tea', 'status': 404}):
            self.assertFalse(verify_live('https://example.com/blog/coffee', 'Coffee & Tea')['ok'])

    def test_command_credentials_never_leak_in_errors(self):
        secret = 'TOP_SECRET_CREDENTIAL'
        self.client['connection']['build_command'] = [sys.executable, '-c', 'import sys; print("https://user:' + secret + '@example.com"); sys.exit(1)']
        with self.assertRaises(PublishError) as caught:
            self.publish(self.article)
        self.assertNotIn(secret, str(caught.exception))
        self.assertNotIn('user:', str(caught.exception))
        with patch('rankme.crawler.fetch_public', side_effect=ValueError('https://user:' + secret + '@example.com')):
            result = verify_live('https://example.com/post', 'First post')
        self.assertNotIn(secret, result['message'])

    def test_credential_url_template_refused(self):
        self.client['connection']['public_url_template'] = 'https://user:password@example.com/{slug}'
        result = validate_connection(self.client['connection'])
        self.assertFalse(result['ok'])
        self.assertNotIn('password', result['message'])

    def test_revisions_follow_configured_data_directory(self):
        data_dir = self.root / 'app-data'
        result = self.publish(self.article, data_dir=data_dir)
        original = Path(result['path']).read_text()
        self.publish(dict(self.article, body='Updated article body.'), data_dir=data_dir)
        revisions = list((data_dir / 'revisions').glob('*/*.md'))
        self.assertEqual(len(revisions), 1)
        self.assertEqual(revisions[0].read_text(), original)

    def test_completed_deployment_is_not_repeated(self):
        counter = self.root / 'deployments'
        self.client['connection'].update(auto_publish=True, deploy_command=[sys.executable, '-c', 'from pathlib import Path; p=Path("deployments"); p.write_text(p.read_text()+"x" if p.exists() else "x")'])
        with patch('rankme.publisher.verify_live', return_value={'ok': True, 'message': 'Verified'}):
            first = self.publish(self.article)
            second = self.publish(self.article)
        self.assertEqual(first['status'], 'published')
        self.assertEqual(second['status'], 'published')
        self.assertEqual(counter.read_text(), 'x')

    def test_interrupted_deployment_blocks_unsafe_repeat(self):
        from rankme import publisher
        real_run = publisher._run
        self.client['connection'].update(auto_publish=True, deploy_command=[sys.executable, '-c', 'from pathlib import Path; Path("deployed").write_text("yes")'])

        class SimulatedCrash(BaseException):
            pass

        def run_then_crash(argv, cwd, timeout=300):
            result = real_run(argv, cwd, timeout)
            if argv == self.client['connection']['deploy_command']:
                raise SimulatedCrash()
            return result

        with patch('rankme.publisher._run', side_effect=run_then_crash):
            with self.assertRaises(SimulatedCrash):
                self.publish(self.article)
        self.assertEqual((self.root / 'deployed').read_text(), 'yes')
        with self.assertRaisesRegex(PublishError, 'outcome is unknown'):
            self.publish(self.article)

    def test_published_git_retry_skips_push(self):
        from rankme import publisher
        self.repository()
        remote = self.root.parent / 'remote.git'
        subprocess.check_call(['git', 'init', '--bare', str(remote)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.git('remote', 'add', 'origin', str(remote))
        self.client['connection']['auto_publish'] = True
        with patch('rankme.publisher.verify_live', return_value={'ok': True, 'message': 'Verified'}):
            self.publish(self.article)
            with patch('rankme.publisher._run', wraps=publisher._run) as runner:
                self.publish(self.article)
            self.assertFalse(any(call.args[0][:2] == ['git', 'push'] for call in runner.call_args_list))
        self.assertEqual(self.git('rev-list', '--count', 'HEAD'), '2')

    def add_cover(self):
        source = self.data_dir / 'covers' / self.article['id'] / 'cover.png'
        source.parent.mkdir(parents=True)
        binary = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a1x8AAAAASUVORK5CYII=')
        source.write_bytes(binary)
        self.article['cover'] = {'path': str(source), 'alt': 'A realistic article illustration', 'prompt': 'Private generation prompt', 'sha256': hashlib.sha256(binary).hexdigest(), 'review': {'passed': True, 'issues': []}, 'format': 'png'}
        return source, binary

    def test_cover_export_and_json_do_not_leak_local_paths(self):
        source, binary = self.add_cover()
        result = self.publish(self.article)
        text = Path(result['path']).read_text()
        self.assertIn('image: "/images/articles/', text)
        self.assertIn('imageAlt: "A realistic article illustration"', text)
        self.assertEqual(Path(result['image_path']).read_bytes(), binary)
        self.assertNotIn(str(source), text)
        self.client['connection']['format'] = 'json'
        result = self.publish(self.article)
        data = json.loads(Path(result['path']).read_text())
        self.assertEqual(data['cover']['url'], result['image_url'])
        self.assertNotIn('path', data['cover'])
        self.assertNotIn('prompt', data['cover'])
        self.assertNotIn(str(source), json.dumps(data))

    def test_cover_git_commits_exactly_two_files_and_reuses_commit(self):
        self.repository()
        self.add_cover()
        first = self.publish(self.article)
        files = self.git('show', '--format=', '--name-only', 'HEAD').splitlines()
        self.assertEqual(sorted(files), sorted([str(Path(first['path']).relative_to(self.root.resolve())), str(Path(first['image_path']).relative_to(self.root.resolve()))]))
        second = self.publish(self.article)
        self.assertEqual(first['commit'], second['commit'])
        self.assertEqual(self.git('rev-list', '--count', 'HEAD'), '2')

    def test_cover_failed_build_rolls_back_both_files(self):
        self.repository()
        self.add_cover()
        self.client['connection']['build_command'] = [sys.executable, '-c', 'raise SystemExit(1)']
        with self.assertRaises(PublishError):
            self.publish(self.article)
        self.assertFalse((self.root / 'content/blog/first-post.md').exists())
        self.assertEqual(list((self.root / 'public/images/articles').glob('*.png')), [])
        self.assertEqual(self.git('status', '--porcelain'), '')

    def test_failed_cover_revision_preserves_original(self):
        self.repository()
        source, binary = self.add_cover()
        original = self.publish(self.article)
        original_text = Path(original['path']).read_text()
        updated = binary + b'new-image-version'
        source.write_bytes(updated)
        self.article['cover']['sha256'] = hashlib.sha256(updated).hexdigest()
        self.client['connection']['build_command'] = [sys.executable, '-c', 'raise SystemExit(1)']
        with self.assertRaises(PublishError):
            self.publish(self.article)
        self.assertEqual(Path(original['path']).read_text(), original_text)
        self.assertEqual(Path(original['image_path']).read_bytes(), binary)
        self.assertEqual(len(list((self.root / 'public/images/articles').glob('*.png'))), 1)
        self.assertEqual(self.git('status', '--porcelain'), '')

    def test_cover_sources_and_destinations_are_guarded(self):
        source, binary = self.add_cover()
        self.article['cover']['sha256'] = '0' * 64
        with self.assertRaisesRegex(PublishError, 'changed after review'):
            self.publish(self.article)
        self.article['cover']['sha256'] = hashlib.sha256(binary).hexdigest()
        self.article['cover']['review']['passed'] = False
        with self.assertRaisesRegex(PublishError, 'successful review'):
            self.publish(self.article)
        self.article['cover']['review']['passed'] = True
        outside = self.root / 'outside.png'
        outside.write_bytes(binary)
        self.article['cover']['path'] = str(outside)
        with self.assertRaises(PublishError):
            self.publish(self.article)
        self.article['cover']['path'] = str(source)
        self.client['connection']['image_dir'] = '../escape'
        with self.assertRaises(PublishError):
            self.publish(self.article)
        self.client['connection']['image_dir'] = 'public/images/articles'
        result = self.publish(self.article)
        for record in (self.data_dir / 'publishing-assets').glob('*.json'):
            record.unlink()
        with self.assertRaisesRegex(PublishError, 'unrelated or modified'):
            self.publish(self.article)
        self.assertEqual(Path(result['image_path']).read_bytes(), binary)

    def test_cover_partial_crash_resumes_with_single_commit(self):
        from rankme import publisher
        self.repository()
        self.add_cover()
        real_write = publisher._atomic_write
        class SimulatedCrash(BaseException):
            pass
        def crash_before_copy(path, data):
            if isinstance(data, bytes):
                raise SimulatedCrash()
            real_write(path, data)
        with patch('rankme.publisher._atomic_write', side_effect=crash_before_copy):
            with self.assertRaises(SimulatedCrash):
                self.publish(self.article)
        result = self.publish(self.article)
        self.assertTrue(Path(result['image_path']).is_file())
        self.assertEqual(self.git('rev-list', '--count', 'HEAD'), '2')
        self.assertEqual(self.git('status', '--porcelain'), '')

    def test_cover_staged_crash_resumes_both_files(self):
        from rankme import publisher
        self.repository()
        self.add_cover()
        real_run = publisher._run
        class SimulatedCrash(BaseException):
            pass
        def crash_before_commit(argv, cwd, timeout=300):
            if argv[:2] == ['git', 'commit']:
                raise SimulatedCrash()
            return real_run(argv, cwd, timeout)
        with patch('rankme.publisher._run', side_effect=crash_before_commit):
            with self.assertRaises(SimulatedCrash):
                self.publish(self.article)
        self.publish(self.article)
        self.assertEqual(self.git('rev-list', '--count', 'HEAD'), '2')
        self.assertEqual(len(self.git('show', '--format=', '--name-only', 'HEAD').splitlines()), 2)
        self.assertEqual(self.git('status', '--porcelain'), '')

    def test_cover_build_mutation_is_not_committed(self):
        self.repository()
        self.add_cover()
        self.client['connection']['build_command'] = [sys.executable, '-c', 'from pathlib import Path; next(Path("public/images/articles").glob("*.png")).write_bytes(b"changed")']
        with self.assertRaisesRegex(PublishError, 'Build modified'):
            self.publish(self.article)
        self.assertEqual(self.git('rev-list', '--count', 'HEAD'), '1')
        self.assertEqual(self.git('status', '--porcelain'), '')


if __name__ == '__main__':
    unittest.main()
