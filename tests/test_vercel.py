import io
import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from rankme import vercel
from rankme.publisher import publish_article, validate_connection
from rankme.vercel import Vercel, VercelError, _project, readiness, sync_site

TOKEN = 'abcdefghijklmnopqrstuvwx'


def git(cwd, *args):
    return subprocess.check_output(['git'] + list(args), cwd=cwd, stderr=subprocess.DEVNULL, text=True).strip()


class ProjectTests(unittest.TestCase):
    def test_project_reads_github_link_and_prefers_custom_domains(self):
        raw = {'id': 'prj_1', 'name': 'site', 'framework': 'vite',
               'link': {'type': 'github', 'org': '0x-tam', 'repo': 'k9-site', 'productionBranch': 'main'},
               'targets': {'production': {'alias': ['k9-site.vercel.app', 'k9academy.me']}}}
        project = _project(raw, 'team_1')
        self.assertEqual(project['repo'], '0x-tam/k9-site')
        self.assertEqual(project['domains'][0], 'k9academy.me')
        self.assertEqual(project['team_id'], 'team_1')

    def test_project_without_github_or_with_unsafe_names_has_no_repo(self):
        self.assertEqual(_project({'id': 'p', 'link': {'type': 'gitlab', 'org': 'a', 'repo': 'b'}}, '')['repo'], '')
        self.assertEqual(_project({'id': 'p', 'link': {'type': 'github', 'org': '../x', 'repo': 'b'}}, '')['repo'], '')
        self.assertEqual(_project({'id': 'p', 'link': {'type': 'github', 'org': 'a', 'repo': 'b', 'productionBranch': '-x;rm'}}, '')['branch'], 'main')

    def test_repo_url_only_builds_github_https_urls(self):
        self.assertEqual(vercel.repo_url('0x-tam/site'), 'https://github.com/0x-tam/site.git')
        for bad in ('', 'a', 'a/b/c', '../a/b', 'a/b c'):
            with self.assertRaises(VercelError):
                vercel.repo_url(bad)


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.account = Vercel(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def fake_get(self, path, token, params=None):
        self.assertEqual(token, TOKEN)
        if path == '/v2/user':
            return {'user': {'username': 'tam'}}
        if path == '/v2/teams':
            return {'teams': [{'id': 'team_1', 'name': 'Priv'}, {'id': 'bad id!', 'name': 'x'}]}
        if path == '/v9/projects':
            name = 'personal' if not params.get('teamId') else 'team-site'
            return {'projects': [{'id': 'prj_' + name, 'name': name, 'link': {'type': 'github', 'org': 'o', 'repo': name}}]}
        if path == '/v6/deployments':
            return {'deployments': [{'uid': 'dpl_1', 'readyState': 'BUILDING', 'meta': {'githubCommitSha': 'a' * 40},
                                     'inspectorUrl': 'https://vercel.com/team/site/dpl_1'},
                                    {'uid': 'dpl_0', 'readyState': 'READY', 'meta': {'githubCommitSha': 'b' * 40},
                                     'inspectorUrl': 'https://evil.example/'}]}
        raise AssertionError(path)

    def test_connect_stores_token_privately_and_status_never_returns_it(self):
        with patch.object(vercel, '_get', self.fake_get):
            status = self.account.connect(TOKEN)
        self.assertTrue(status['connected'])
        self.assertEqual(status['user'], 'tam')
        self.assertEqual([t['id'] for t in status['teams']], ['team_1'])
        self.assertNotIn(TOKEN, json.dumps(status))
        mode = stat.S_IMODE(os.stat(self.account.path).st_mode)
        self.assertEqual(mode, 0o600)

    def test_connect_rejects_malformed_tokens_without_network(self):
        with patch.object(vercel, '_get', side_effect=AssertionError('no network')):
            for bad in ('', 'short', 'x' * 15, 'has space in the token here'):
                with self.assertRaises(VercelError):
                    self.account.connect(bad)

    def test_projects_cover_personal_and_team_scopes(self):
        with patch.object(vercel, '_get', self.fake_get):
            self.account.connect(TOKEN)
            names = [p['name'] for p in self.account.projects()['projects']]
        self.assertEqual(names, ['personal', 'team-site'])

    def test_deployment_matches_commit_and_drops_foreign_inspector_links(self):
        with patch.object(vercel, '_get', self.fake_get):
            self.account.connect(TOKEN)
            building = self.account.deployment('team_1', 'prj_1', 'a' * 40)
            ready = self.account.deployment('team_1', 'prj_1', 'b' * 12)
            missing = self.account.deployment('team_1', 'prj_1', 'c' * 40)
        self.assertEqual(building['state'], 'BUILDING')
        self.assertEqual(building['inspector_url'], 'https://vercel.com/team/site/dpl_1')
        self.assertEqual(ready['state'], 'READY')
        self.assertEqual(ready['inspector_url'], '')
        self.assertIsNone(missing)
        self.assertIsNone(self.account.deployment('team_1', 'prj_1', 'not-a-sha'))

    def test_disconnect_removes_the_token(self):
        with patch.object(vercel, '_get', self.fake_get):
            self.account.connect(TOKEN)
        self.assertFalse(self.account.disconnect()['connected'])
        self.assertFalse(self.account.path.exists())
        with self.assertRaises(VercelError):
            self.account.projects()

    def test_http_errors_never_echo_the_token(self):
        error = HTTPError('https://api.vercel.com/v2/user', 401, 'Unauthorized', {}, io.BytesIO(TOKEN.encode()))
        with patch('rankme.vercel.build_opener') as opener:
            opener.return_value.open.side_effect = error
            with self.assertRaises(VercelError) as caught:
                vercel._get('/v2/user', TOKEN)
        self.assertNotIn(TOKEN, str(caught.exception))


class SiteCopyTests(unittest.TestCase):
    """RankMe's own repository copy, with a local bare repository standing in for GitHub."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.remote = base / 'remote.git'
        subprocess.check_call(['git', 'init', '--bare', '-b', 'main', str(self.remote)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.work = base / 'work'
        subprocess.check_call(['git', 'clone', str(self.remote), str(self.work)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for key, value in (('user.name', 'RankMe Test'), ('user.email', 'rankme@example.test')):
            git(self.work, 'config', key, value)
        (self.work / 'package.json').write_text(json.dumps({'dependencies': {'vite': '5', 'react-router-dom': '6'}}))
        git(self.work, 'add', '.')
        git(self.work, 'commit', '-m', 'Initial')
        git(self.work, 'push', 'origin', 'HEAD:main')
        self.data = base / 'data'
        self.patch = patch.object(vercel, 'repo_url', lambda repo: str(self.remote))
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def test_sync_clones_then_fast_forwards(self):
        path = sync_site(self.data, 'client-1', 'o/site', 'main')
        self.assertEqual(path, (self.data / 'sites' / 'client-1').resolve())
        (self.work / 'extra.txt').write_text('new')
        git(self.work, 'add', '.')
        git(self.work, 'commit', '-m', 'Remote change')
        git(self.work, 'push', 'origin', 'HEAD:main')
        sync_site(self.data, 'client-1', 'o/site', 'main')
        self.assertTrue((path / 'extra.txt').exists())

    def test_sync_rejects_unsafe_identifiers(self):
        for client_id, branch in (('../x', 'main'), ('c', '-main'), ('c', 'a b')):
            with self.assertRaises(VercelError):
                sync_site(self.data, client_id, 'o/site', branch)

    def test_readiness_flags_missing_blog_and_single_page_app(self):
        path = sync_site(self.data, 'client-1', 'o/site', 'main')
        result = readiness(path)
        self.assertFalse(result['ready'])
        self.assertEqual(result['framework'], 'Vite')
        self.assertEqual([c['ok'] for c in result['checks']], [False, False])
        (path / 'content' / 'blog').mkdir(parents=True)
        (path / 'package.json').write_text(json.dumps({'dependencies': {'next': '15'}}))
        result = readiness(path)
        self.assertTrue(result['ready'])
        self.assertEqual(result['content_dir'], 'content/blog')

    def test_vercel_publish_pushes_article_without_local_build_and_waits_for_deploy(self):
        path = sync_site(self.data, 'client-1', 'o/site', 'main')
        for key, value in (('user.name', 'RankMe Test'), ('user.email', 'rankme@example.test')):
            git(path, 'config', key, value)
        connection = {'provider': 'vercel', 'mode': 'git', 'project_path': str(path), 'branch': 'main', 'remote': 'origin',
                      'content_dir': 'content/blog', 'format': 'md', 'build_command': [], 'deploy_command': [],
                      'public_url_template': 'https://example.com/blog/{slug}', 'deploy_on_publish': True, 'auto_publish': False}
        self.assertTrue(validate_connection(connection)['ok'])
        self.assertFalse(validate_connection({**connection, 'provider': ''})['ok'])
        article = {'id': 'article-1', 'slug': 'first-post', 'title': 'First post', 'description': 'Advice', 'body': 'Useful advice.'}
        with patch('rankme.publisher.verify_live', side_effect=AssertionError('live check waits for the deployment')):
            result = publish_article({'connection': connection}, article, data_dir=self.data)
        self.assertEqual(result['status'], 'verification_pending')
        self.assertEqual(result['live_url'], 'https://example.com/blog/first-post')
        self.assertEqual(git(self.remote, 'rev-parse', 'main'), result['commit'])
        self.assertIn('content/blog/first-post.md', git(self.remote, 'show', '--name-only', '--format=', 'main'))

    def test_publish_without_approval_flags_only_exports(self):
        path = sync_site(self.data, 'client-1', 'o/site', 'main')
        for key, value in (('user.name', 'RankMe Test'), ('user.email', 'rankme@example.test')):
            git(path, 'config', key, value)
        before = git(self.remote, 'rev-parse', 'main')
        connection = {'provider': 'vercel', 'mode': 'git', 'project_path': str(path), 'branch': 'main', 'content_dir': 'content/blog',
                      'format': 'md', 'build_command': [], 'deploy_command': [], 'public_url_template': 'https://example.com/blog/{slug}'}
        article = {'id': 'article-1', 'slug': 'first-post', 'title': 'First post', 'description': 'Advice', 'body': 'Useful advice.'}
        result = publish_article({'connection': connection}, article, data_dir=self.data)
        self.assertEqual(result['status'], 'exported')
        self.assertEqual(git(self.remote, 'rev-parse', 'main'), before)


if __name__ == '__main__':
    unittest.main()
