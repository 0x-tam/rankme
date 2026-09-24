"""Per-website cover style: rendering modes, a fixed house style, and approved style references."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from rankme.brand import normalize_brand
from rankme.covers import (_run_image_job, GENERATED, cover_prompt, generate_cover, house_style, reference_paths,
                           review_prompt)
from rankme.engine import brand_digest
from rankme.server import Application
from tests.test_covers import Runner, png

EON = {'name': 'EON Coatings', 'profile': {'audience': 'Homeowners and facility managers'},
       'image_brand': {'colors': ['#F2A900', '#1B3A5C'], 'style': '', 'mode': 'photo', 'mood': 'Bright and inviting',
                       'subjects': 'Show the damaged floor or wall being fixed', 'avoid': 'Dark warehouses'}}
LUMIDENT = {'name': 'Lumident', 'image_brand': {'colors': ['#CA2126'], 'style': 'Ivory tooth forms'}}
ARTICLE = {'title': 'Fixing cracked garage floors', 'body': 'How epoxy coatings repair cracks.'}


class StyleSettingsTests(unittest.TestCase):
    def test_default_mode_is_omitted_so_existing_brands_keep_their_digest(self):
        brand = normalize_brand({'colors': ['#ca2126'], 'style': 'Ivory', 'mode': 'illustration_3d'})
        self.assertEqual(brand, {'colors': ['#CA2126'], 'style': 'Ivory'})
        client = {'image_brand': brand, 'profile': {'audience': 'A', 'tone': 'B'}}
        legacy = hashlib.sha256(json.dumps({'colors': ['#CA2126'], 'style': 'Ivory', 'audience': 'A', 'tone': 'B'}, sort_keys=True).encode()).hexdigest()
        self.assertEqual(brand_digest(client), legacy)

    def test_photo_mode_and_direction_are_kept_and_change_the_digest(self):
        brand = normalize_brand({**EON['image_brand'], 'avoid': '  Dark\n warehouses  ', 'references': [{'x': 1}], 'style_spec': 'forged'})
        self.assertEqual(brand['mode'], 'photo')
        self.assertEqual(brand['avoid'], 'Dark warehouses')
        self.assertNotIn('references', brand)
        self.assertNotIn('style_spec', brand)
        self.assertNotEqual(brand_digest({'image_brand': brand}), brand_digest({'image_brand': {'colors': brand['colors'], 'style': ''}}))

    def test_unknown_mode_is_rejected(self):
        with self.assertRaises(ValueError):
            normalize_brand({'colors': [], 'mode': 'watercolor'})


class PromptTests(unittest.TestCase):
    def test_photo_prompt_is_realistic_and_names_ai_tells(self):
        prompt = cover_prompt(EON, ARTICLE)
        self.assertIn('realistic editorial photograph', prompt)
        self.assertIn('waxy or plastic surfaces', prompt)
        self.assertIn('Show the damaged floor or wall being fixed', prompt)
        self.assertIn('Bright and inviting', prompt)
        self.assertNotIn('3D editorial illustration', prompt)
        self.assertIn('professional photographer', review_prompt(EON, prompt))

    def test_3d_prompt_stays_3d(self):
        prompt = cover_prompt(LUMIDENT, ARTICLE)
        self.assertIn('polished 3D editorial illustration', prompt)
        self.assertNotIn('realistic editorial photograph', prompt)
        self.assertIn('3D rendering is required', review_prompt(LUMIDENT, prompt))

    def test_house_style_is_identical_across_articles_and_includes_learned_spec(self):
        client = {**EON, 'image_brand': {**EON['image_brand'], 'style_spec': 'Soft window light from the left.'}}
        self.assertIn('Soft window light from the left.', house_style(client))
        first, second = cover_prompt(client, ARTICLE), cover_prompt(client, {'title': 'Rust on railings'})
        self.assertTrue(first.startswith(house_style(client)) and second.startswith(house_style(client)))

    def test_references_are_described_as_style_only(self):
        prompt = cover_prompt(EON, ARTICLE, references=2)
        self.assertIn('do not copy their subjects', prompt)
        self.assertIn('style references', review_prompt(EON, prompt, references=2))
        self.assertNotIn('style references', review_prompt(EON, prompt, references=0))


class ReferenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data = Path(self.temp.name).resolve()

    def tearDown(self):
        self.temp.cleanup()

    def cover(self, article_id):
        folder = self.data / 'covers' / article_id
        folder.mkdir(parents=True)
        staged = png(folder / 'staged.png')
        digest = hashlib.sha256(staged.read_bytes()).hexdigest()
        path = folder / ('cover-' + digest[:12] + '.png')
        staged.rename(path)
        return {'article_id': article_id, 'sha256': digest}, path

    def test_only_verified_covers_inside_the_data_directory_are_used(self):
        good, path = self.cover('a1')
        tampered, tampered_path = self.cover('a2')
        tampered_path.write_bytes(b'changed')
        refs = [good, tampered, {'article_id': '../a1', 'sha256': good['sha256']}, {'article_id': 'a3', 'sha256': 'x'}]
        self.assertEqual(reference_paths({'image_brand': {'references': refs}}, self.data), [path])

    def test_at_most_three_references(self):
        refs = [self.cover('a%d' % i)[0] for i in range(5)]
        self.assertEqual(len(reference_paths({'image_brand': {'references': refs}}, self.data)), 3)

    def test_generation_and_review_both_receive_the_references(self):
        ref, path = self.cover('old')
        client = {**EON, 'image_brand': {**EON['image_brand'], 'references': [ref]}}
        calls = []

        def job(runner, prompt, schema, work_dir, image=None, references=()):
            calls.append((image, list(references), prompt))
            if image:
                return {'passed': True, 'issues': [], 'summary': 'Consistent'}, ''
            return {'image_path': str(png(Path(work_dir) / 'image.png')), 'alt': 'Epoxy floor repair'}, ''
        with patch('rankme.covers._run_image_job', side_effect=job):
            cover = generate_cover(Runner(self.temp.name), client, {'id': 'new', 'title': 'Floors'}, self.data)
        self.assertEqual([c[1] for c in calls], [[path], [path]])
        self.assertIn('Match their rendering', calls[0][2])
        self.assertEqual(cover['mode'], 'photo')
        self.assertEqual(cover['references'], ['old'])

    def test_cli_attaches_every_reference_and_can_still_generate(self):
        ref = png(self.data / 'ref.png')
        captured = {}

        def run(command, **kwargs):
            captured['command'] = command
            Path(command[command.index('--output-last-message') + 1]).write_text(json.dumps({'image_path': '', 'alt': ''}))
            return type('Result', (), {'returncode': 0})()
        with patch('rankme.cancel.run', side_effect=run):
            _run_image_job(Runner(self.temp.name), 'Generate', GENERATED, self.temp.name, references=[ref, ref])
        self.assertEqual(captured['command'].count('-i'), 2)
        self.assertIn('features.image_generation=true', captured['command'])


class ReferenceWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = Application(Path(self.temp.name))
        self.store, self.engine = self.app.store, self.app.engine
        self.engine.runner_factory = lambda: Mock(work_dir=Path(self.temp.name))
        self.store.put('clients', {'id': 'c1', 'name': 'EON', 'url': 'https://eon.example', 'status': 'ready', 'confirmed': True,
                                   'profile': {'summary': 's'}, 'image_brand': dict(EON['image_brand']), 'connection': {}})
        folder = Path(self.temp.name) / 'covers' / 'a1'
        folder.mkdir(parents=True)
        image = png(folder / 'staged.png')
        self.digest = hashlib.sha256(image.read_bytes()).hexdigest()
        path = folder / ('cover-' + self.digest[:12] + '.png')
        image.rename(path)
        self.article = self.store.put('articles', {'id': 'a1', 'client_id': 'c1', 'title': 'Floors', 'status': 'ready',
            'cover': {'path': str(path), 'sha256': self.digest, 'mode': 'photo', 'review': {'passed': True, 'issues': []}}})

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def use(self, **cover):
        article = self.store.update('articles', 'a1', cover={**self.article['cover'], **cover})
        with patch.object(self.engine, 'cover_valid', return_value=True):
            return self.app.dispatch('POST', '/api/articles/a1/style-reference', {'use': True})

    def test_approved_cover_becomes_a_reference_and_queues_style_learning(self):
        brand = self.use()['image_brand']
        self.assertEqual(brand['references'], [{'article_id': 'a1', 'sha256': self.digest}])
        job = [j for j in self.store.all('jobs') if j['kind'] == 'style'][0]
        with patch('rankme.covers.describe_style', return_value='Bright daylight, shallow depth of field.'):
            self.engine.execute(job)
        self.assertEqual(self.store.get('clients', 'c1')['image_brand']['style_spec'], 'Bright daylight, shallow depth of field.')

    def test_unreviewed_or_other_mode_covers_are_refused(self):
        with self.assertRaisesRegex(ValueError, 'passed review'):
            self.use(review={'passed': False, 'issues': ['x']})
        with self.assertRaisesRegex(ValueError, 'different style'):
            self.use(mode='illustration_3d')

    def test_changing_mode_drops_references_but_editing_direction_keeps_them(self):
        self.store.update('clients', 'c1', image_brand={**EON['image_brand'], 'references': [{'article_id': 'a1', 'sha256': self.digest}], 'style_spec': 'Spec'})
        kept = self.app.update_client('c1', {'image_brand': {**EON['image_brand'], 'mood': 'Warmer'}})['image_brand']
        self.assertEqual(kept['style_spec'], 'Spec')
        dropped = self.app.update_client('c1', {'image_brand': {**EON['image_brand'], 'mode': 'illustration_3d'}})['image_brand']
        self.assertNotIn('references', dropped)
        self.assertNotIn('style_spec', dropped)

    def test_style_learning_failure_does_not_block_the_weekly_schedule(self):
        self.store.put('jobs', {'id': 'j1', 'kind': 'style', 'client_id': 'c1', 'status': 'failed'})
        self.store.update('clients', 'c1', automation=True, subject='Floors', status='active', next_run='2020-01-01T00:00:00+00:00')
        self.store.put('articles', {'id': 'p1', 'client_id': 'c1', 'title': 'Next', 'status': 'planned', 'scheduled_at': '2020-01-01T00:00:00+00:00'})
        self.engine.schedule_tick()
        self.assertTrue([j for j in self.store.all('jobs') if j['kind'] == 'generate' and j['status'] == 'queued'])


if __name__ == '__main__':
    unittest.main()
