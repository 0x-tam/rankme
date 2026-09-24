"""Linking a website to a Vercel project and following its deployment through the live check."""
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rankme.server import Application


class VercelWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = Application(self.root / 'data')
        self.store, self.engine = self.app.store, self.app.engine
        self.store.put('clients', {'id': 'client1', 'name': 'K9 Academy', 'url': 'https://k9academy.me', 'status': 'ready',
                                   'confirmed': True, 'automation': False, 'profile': {'summary': 'Dog training'},
                                   'subject': 'Training', 'connection': {'mode': 'export', 'auto_publish': False}})
        self.repo = self.root / 'site-copy'
        self.repo.mkdir()
        subprocess.check_call(['git', 'init', '-b', 'main'], cwd=self.repo, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.project = {'id': 'prj_1', 'name': 'k9-site', 'team_id': 'team_1', 'framework': 'vite', 'repo': '0x-tam/k9-site',
                        'branch': 'main', 'domains': ['k9academy.me', 'www.k9academy.me', 'k9-site.vercel.app'], 'git_provider': 'github'}

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def link(self, **data):
        with patch.object(self.engine.vercel, 'project', return_value=self.project), \
                patch('rankme.vercel.github_access', return_value={'ok': True, 'message': ''}), \
                patch('rankme.vercel.sync_site', return_value=self.repo):
            return self.app.dispatch('POST', '/api/clients/client1/vercel', {'team_id': 'team_1', 'project_id': 'prj_1', **data})

    def test_link_configures_git_publishing_that_waits_for_approval(self):
        connection = self.link()['connection']
        self.assertEqual(connection['provider'], 'vercel')
        self.assertEqual(connection['mode'], 'git')
        self.assertEqual(connection['project_path'], str(self.repo))
        self.assertEqual(connection['public_url_template'], 'https://k9academy.me/blog/{slug}')
        self.assertTrue(connection['deploy_on_publish'])
        self.assertFalse(connection['auto_publish'])
        self.assertEqual(connection['vercel']['repo'], '0x-tam/k9-site')
        self.assertFalse(connection['vercel']['readiness']['ready'])

    def test_link_accepts_only_the_project_domains_and_safe_blog_paths(self):
        self.assertEqual(self.link(domain='evil.example')['connection']['vercel']['domain'], 'k9academy.me')
        self.assertEqual(self.link(domain='www.k9academy.me', blog_path='/resources/articles/')['connection']['public_url_template'],
                         'https://www.k9academy.me/resources/articles/{slug}')
        for bad in ('/../x', '/Blog Posts', '/x?y=1'):
            with self.assertRaises(ValueError):
                self.link(blog_path=bad)

    def test_link_requires_a_github_linked_project(self):
        self.project['repo'] = ''
        with self.assertRaisesRegex(ValueError, 'not linked to a GitHub repository'):
            self.link()

    def test_generic_connection_edits_cannot_forge_or_drop_the_vercel_link(self):
        self.app.dispatch('PATCH', '/api/clients/client1', {'connection': {'provider': 'vercel', 'deploy_on_publish': True, 'vercel': {'repo': 'x/y'}}})
        self.assertNotIn('provider', self.store.get('clients', 'client1')['connection'])
        self.link()
        self.app.dispatch('PATCH', '/api/clients/client1', {'connection': {'auto_publish': True, 'provider': 'export', 'vercel': {}}})
        connection = self.store.get('clients', 'client1')['connection']
        self.assertTrue(connection['auto_publish'])
        self.assertEqual(connection['provider'], 'vercel')
        self.assertEqual(connection['vercel']['project_id'], 'prj_1')

    def test_unlink_returns_to_local_export(self):
        self.link()
        connection = self.app.dispatch('POST', '/api/clients/client1/vercel/unlink', {})['connection']
        self.assertEqual(connection['mode'], 'export')
        self.assertNotIn('provider', connection)

    def verify_with(self, deployment, live=None):
        self.link()
        self.store.put('articles', {'id': 'article1', 'client_id': 'client1', 'title': 'Recall training', 'slug': 'recall-training',
                                    'status': 'verification_pending', 'publish_started': True,
                                    'publish_result': {'commit': 'a' * 40, 'live_url': 'https://k9academy.me/blog/recall-training'}})
        job = self.engine.queue('verify', 'client1', 'article1')
        with patch.object(self.engine.vercel, 'deployment', return_value=deployment), \
                patch('rankme.publisher.verify_live', return_value=live or {'ok': False, 'message': 'not yet'}) as check:
            self.engine.execute(job)
        return self.store.get('articles', 'article1'), check, self.store.get('jobs', job['id'])

    def test_verify_waits_while_vercel_builds(self):
        article, check, job = self.verify_with({'id': 'dpl_1', 'state': 'BUILDING', 'inspector_url': ''})
        self.assertEqual(article['status'], 'verification_pending')
        self.assertEqual(article['publish_result']['deployment']['state'], 'BUILDING')
        self.assertIn('building', article['publish_result']['message'])
        self.assertEqual(job['status'], 'completed')
        check.assert_not_called()

    def test_verify_reports_a_failed_vercel_build_and_stops_rechecking(self):
        article, check, _ = self.verify_with({'id': 'dpl_1', 'state': 'ERROR', 'inspector_url': 'https://vercel.com/x'})
        self.assertIn('build failed', article['publish_result']['message'])
        check.assert_not_called()
        self.engine.schedule_tick()
        self.assertFalse([j for j in self.store.all('jobs') if j['status'] == 'queued' and j['kind'] == 'verify'])

    def test_verify_checks_the_live_page_once_the_deployment_is_ready(self):
        article, check, _ = self.verify_with({'id': 'dpl_1', 'state': 'READY', 'inspector_url': ''}, live={'ok': True, 'message': 'Live article verified.'})
        check.assert_called_once()
        self.assertEqual(article['status'], 'published')
        self.assertEqual(article['publish_result']['deployment']['state'], 'READY')


if __name__ == '__main__':
    unittest.main()
