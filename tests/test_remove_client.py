"""Removing a website deletes only its own RankMe records and files, and only after explicit confirmation."""
import tempfile
import unittest
from pathlib import Path

from rankme.server import Application


class RemoveClientTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.app = Application(self.root)
        self.store = self.app.store
        for ident, url in (('gone', 'https://www.eoncoatings.com'), ('kept', 'https://lumident-lb.com')):
            self.store.put('clients', {'id': ident, 'name': ident, 'url': url, 'status': 'ready'})
            self.store.put('articles', {'id': 'art-' + ident, 'client_id': ident, 'title': 'T', 'status': 'published'})
            self.store.put('seo', {'id': ident, 'client_id': ident})
            self.store.put('visibility', {'id': ident, 'client_id': ident})
            self.store.put('opportunities', {'client_id': ident, 'title': 'O'})
            self.store.put('measurements', {'client_id': ident, 'source': 'seo'})
            self.store.event('Something happened', ident)
            for folder in (('covers', 'art-' + ident), ('revisions', 'art-' + ident), ('brand', ident), ('sites', ident)):
                path = self.root.joinpath(*folder)
                path.mkdir(parents=True)
                (path / 'file').write_text('x')

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def remove(self, confirm):
        return self.app.dispatch('POST', '/api/clients/gone/remove', {'confirm': confirm})

    def test_requires_typing_the_website_address(self):
        for wrong in ('', 'gone', 'lumident-lb.com', 'eoncoatings'):
            with self.assertRaisesRegex(ValueError, 'Type eoncoatings.com'):
                self.remove(wrong)
        self.assertTrue(self.store.get('clients', 'gone'))

    def test_removes_only_that_websites_records_and_files(self):
        self.assertEqual(self.remove(' https://www.EONcoatings.com/ ')['removed'], 'gone')
        for table in ('clients', 'articles', 'seo', 'visibility', 'opportunities', 'measurements', 'events'):
            owners = {r.get('client_id', r['id']) for r in self.store.all(table)}
            self.assertNotIn('gone', owners, table)
            if table != 'events':
                self.assertIn('kept', owners, table)
        for folder in (('covers', 'art-gone'), ('revisions', 'art-gone'), ('brand', 'gone'), ('sites', 'gone')):
            self.assertFalse(self.root.joinpath(*folder).exists(), folder)
        for folder in (('covers', 'art-kept'), ('brand', 'kept'), ('sites', 'kept')):
            self.assertTrue(self.root.joinpath(*folder).exists(), folder)
        self.assertTrue(any('Removed website gone' in e['message'] for e in self.store.all('events')))

    def test_refuses_while_a_job_is_running(self):
        self.store.put('jobs', {'client_id': 'gone', 'kind': 'generate', 'status': 'running'})
        with self.assertRaisesRegex(ValueError, 'current job'):
            self.remove('eoncoatings.com')
        self.assertTrue(self.store.get('clients', 'gone'))

    def test_symlinked_folders_are_not_followed_out_of_the_data_directory(self):
        outside = Path(tempfile.mkdtemp()).resolve()
        (outside / 'keep.txt').write_text('precious')
        brand = self.root / 'brand' / 'gone'
        for child in brand.iterdir():
            child.unlink()
        brand.rmdir()
        brand.symlink_to(outside, target_is_directory=True)
        self.remove('eoncoatings.com')
        self.assertTrue((outside / 'keep.txt').exists())

    def test_the_cloud_workspace_can_remove_only_with_a_typed_confirmation(self):
        from rankme.cloud_bridge import allowed_command
        path = '/api/clients/' + 'a' * 32 + '/remove'
        self.assertTrue(allowed_command('POST', path, {'confirm': 'eoncoatings.com'}))
        for body in ({}, {'confirm': ''}, {'confirm': 7}, {'confirm': 'x.com', 'extra': 1}):
            self.assertFalse(allowed_command('POST', path, body))

    def test_unknown_website_is_not_found(self):
        with self.assertRaises(KeyError):
            self.app.dispatch('POST', '/api/clients/missing/remove', {'confirm': 'x.com'})


if __name__ == '__main__':
    unittest.main()
