"""Brand website guide (scan + analysis) and the one-distinct-image-per-article guardrail."""
import hashlib
import io
import struct
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from rankme.ai import AIError
from rankme.brand_guide import image_path, scan
from rankme.covers import analyze_brand, fingerprint, generate_cover, guide_paths, raster_info, too_similar
from rankme.server import Application, handler_for
from tests.test_covers import Runner, png


def picture(path, width=1200, height=800):
    return png(path, width, height)


def _shaded(path, width, height, shade):
    import zlib
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff)
    # A left-to-right gradient whose direction depends on `shade`, so fingerprints differ between images.
    row = bytes(v for x in range(width) for v in [((x * 255 // width) if shade > 0x88 else (255 - x * 255 // width))] * 3)
    raw = b''.join(b'\0' + row for _ in range(height))
    Path(path).write_bytes(b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
                           + chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b''))
    return Path(path)


class RasterTests(unittest.TestCase):
    def test_webp_dimensions_are_read(self):
        vp8x = b'RIFF' + b'\0' * 4 + b'WEBPVP8X' + b'\0' * 8 + (1199).to_bytes(3, 'little') + (799).to_bytes(3, 'little')
        self.assertEqual(raster_info(vp8x), ('webp', 1200, 800))
        with self.assertRaises(AIError):
            raster_info(b'<svg></svg>')


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data = Path(self.temp.name).resolve()
        self.images = {name: picture(self.data / (name + '.png'), *size).read_bytes() for name, size in {
            'hero': (1600, 900), 'room': (1200, 800), 'logo': (1200, 800), 'team': (1200, 800), 'tiny': (200, 200),
            'banner': (3000, 400), 'share': (1200, 630), 'blocked': (1200, 800)}.items()}
        self.fetched = []
        home = ('<meta property="og:image" content="https://www.brand.example/share.png">'
                '<img src="/img/logo.png"><img src="/img/hero.png"><img srcset="/img/tiny.png 200w, /img/room.png 1200w">'
                '<img src="/_next/image?url=%2Fimg%2Fbanner.png&w=3840"><img src="/team/team.png"><img src="/private/blocked.png">'
                '<img src="https://cdn.other.example/img/room.png"><a href="/services">Services</a><a href="https://other.example/">x</a>')

        def fetch(url, max_bytes=0, allowed_hosts=None, image=False):
            self.fetched.append(url)
            host = url.split('/')[2]
            if allowed_hosts is not None and host not in allowed_hosts:
                raise ValueError('Redirect leaves the inspected website.')
            path = url.split(host, 1)[1]
            if path == '/robots.txt':
                return {'text': 'User-agent: *\nDisallow: /private/'}
            if not image:
                return {'url': url, 'text': home if path == '/' else '<img src="/img/room.png">'}
            name = path.rsplit('/', 1)[1].split('.')[0]
            return {'data': self.images[name]}
        self.patches = [patch('rankme.brand_guide.fetch_public', side_effect=fetch),
                        patch('rankme.brand.extract_brand', return_value={'colors': ['#F2A900']})]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in self.patches:
            item.stop()
        self.temp.cleanup()

    def test_keeps_brand_imagery_and_skips_logos_people_small_banners_robots_and_other_sites(self):
        guide = scan('https://brand.example/', self.data, 'client1')
        sources = [item['source'].split('/', 3)[-1] for item in guide['images']]
        self.assertEqual(sources, ['img/hero.png', 'img/room.png', 'share.png'])  # page order, share image last
        self.assertEqual(guide['css_colors'], ['#F2A900'])
        self.assertEqual(guide['pages'], ['https://brand.example/', 'https://brand.example/services'])
        self.assertFalse([u for u in self.fetched if 'logo' in u or 'team' in u or 'blocked' in u or 'other.example' in u])
        self.assertTrue(any('/img/banner.png' in u for u in self.fetched))  # Next.js optimizer URL unwrapped, then rejected as a banner
        for item in guide['images']:
            self.assertEqual(image_path(self.data, 'client1', item).read_bytes(), self.images[item['source'].rsplit('/', 1)[1][:-4]])

    def test_saved_images_are_verified_before_use(self):
        item = scan('https://brand.example/', self.data, 'client1')['images'][0]
        self.assertIsNone(image_path(self.data, '../client1', item))
        self.assertIsNone(image_path(self.data, 'client1', {**item, 'file': '../../x.png'}))
        (self.data / 'brand' / 'client1' / item['file']).write_bytes(b'tampered')
        self.assertIsNone(image_path(self.data, 'client1', item))


class AnalysisTests(unittest.TestCase):
    def test_analysis_is_sanitized_and_mode_aware(self):
        with tempfile.TemporaryDirectory() as temp:
            images = [png(Path(temp) / ('%d.png' % i)) for i in range(3)]
            captured = {}

            def job(runner, prompt, schema, work_dir, image=None, references=()):
                captured.update(prompt=prompt, images=[image, *references])
                return {'summary': 'Bright interiors.', 'style_spec': 'Daylight   interiors', 'mood': 'Bright', 'subjects': 'Rooms',
                        'avoid': 'Gloom', 'colors': ['#f2a900', 'teal', '#1B3A5C'], 'reference_images': [1, 7, -1, 0]}, ''
            with patch('rankme.covers._run_image_job', side_effect=job):
                result = analyze_brand(Runner(temp), {'image_brand': {'mode': 'photo'}}, images, ['#F2A900'], 'https://brand.example/')
        self.assertIn('Realistic photography', captured['prompt'])
        self.assertIn('never choose images containing logos, text, identifiable people', captured['prompt'])
        self.assertEqual(len(captured['images']), 3)
        self.assertEqual(result['colors'], ['#F2A900', '#1B3A5C'])
        self.assertEqual(result['reference_images'], [1, 0])
        self.assertEqual(result['style_spec'], 'Daylight interiors')

    def test_no_images_fails_clearly(self):
        with self.assertRaisesRegex(AIError, 'No usable brand images'):
            analyze_brand(Runner('/tmp'), {}, [], [], 'https://brand.example/')


class DistinctCoverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data = Path(self.temp.name).resolve()

    def tearDown(self):
        self.temp.cleanup()

    def test_fingerprint_matches_copies_and_separates_different_images(self):
        first = _shaded(self.data / 'a.png', 1200, 800, 0x90)
        copy = _shaded(self.data / 'b.png', 1536, 1024, 0x90)
        other = _shaded(self.data / 'c.png', 1200, 800, 0x10)
        self.assertIsNotNone(fingerprint(first))
        self.assertTrue(too_similar(fingerprint(copy), [{'fingerprint': fingerprint(first), 'title': 'A'}]))
        self.assertIsNone(too_similar(fingerprint(other), [{'fingerprint': fingerprint(first), 'title': 'A'}]))

    def test_near_duplicate_is_regenerated_then_refused_without_review(self):
        earlier = _shaded(self.data / 'earlier.png', 1536, 1024, 0x90)
        previous = [{'title': 'Old article', 'alt': 'Blue floor', 'fingerprint': fingerprint(earlier), 'path': str(earlier)}]
        calls = []

        def job(runner, prompt, schema, work_dir, image=None, references=()):
            calls.append(image)
            return {'image_path': str(_shaded(Path(work_dir) / 'image.png', 1536, 1024, 0x90)), 'alt': 'Blue floor again'}, ''
        with patch('rankme.covers._run_image_job', side_effect=job):
            with self.assertRaisesRegex(AIError, 'nearly duplicated'):
                generate_cover(Runner(self.temp.name), {}, {'id': 'new', 'title': 'New'}, self.data, previous=previous)
        self.assertEqual(calls, [None, None])  # two generations, no review spent on duplicates

    def test_distinct_cover_is_reviewed_against_guide_references_and_earlier_covers(self):
        earlier = _shaded(self.data / 'earlier.png', 1536, 1024, 0x10)
        site_image = self.data / 'brand' / 'c1' / 'site.png'
        site_image.parent.mkdir(parents=True)
        png(site_image)
        digest = hashlib.sha256(site_image.read_bytes()).hexdigest()
        stored = site_image.with_name(digest[:16] + '.png')
        site_image.rename(stored)
        client = {'id': 'c1', 'image_brand': {'mode': 'photo', 'site': {'images': [{'file': stored.name, 'sha256': digest}],
                                                                        'analysis': {'reference_images': [0], 'style_spec': 'Warm daylight'}}}}
        self.assertEqual(guide_paths(client, self.data), [stored])
        previous = [{'title': 'Old article', 'alt': 'Cracked garage floor', 'fingerprint': fingerprint(earlier), 'path': str(earlier)}]
        calls = []

        def job(runner, prompt, schema, work_dir, image=None, references=()):
            calls.append((image, list(references), prompt))
            if image:
                return {'passed': True, 'issues': [], 'summary': 'Distinct'}, ''
            return {'image_path': str(_shaded(Path(work_dir) / 'image.png', 1536, 1024, 0x90)), 'alt': 'Coated clinic floor'}, ''
        with patch('rankme.covers._run_image_job', side_effect=job):
            cover = generate_cover(Runner(self.temp.name), client, {'id': 'new', 'title': 'Clinic floors'}, self.data, previous=previous)
        generation, review = calls
        self.assertEqual(generation[1], [stored])
        self.assertIn('Old article: Cracked garage floor', generation[2])
        self.assertIn('Binding brand guide', generation[2])
        self.assertEqual(review[1], [stored, earlier])
        self.assertIn('repeats or closely resembles any earlier cover', review[2])
        self.assertTrue(cover['fingerprint'])


class GuideWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = Application(Path(self.temp.name))
        self.store, self.engine = self.app.store, self.app.engine
        self.engine.runner_factory = lambda: Mock(work_dir=Path(self.temp.name))
        self.session = self.app.auth.new_session('test-credential')
        self.store.put('clients', {'id': 'c1', 'name': 'EON', 'url': 'https://eoncoatings.vercel.app', 'status': 'ready', 'confirmed': True,
                                   'profile': {'summary': 's'}, 'connection': {},
                                   'image_brand': {'colors': ['#123456'], 'style': '', 'mode': 'photo', 'mood': 'My own mood'}})

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def run_guide(self, url=None):
        job = self.app.dispatch('POST', '/api/clients/c1/brand-guide', {'url': url} if url else {})
        image = png(Path(self.temp.name) / 'img.png')
        digest = hashlib.sha256(image.read_bytes()).hexdigest()
        folder = Path(self.temp.name).resolve() / 'brand' / 'c1'
        folder.mkdir(parents=True, exist_ok=True)
        image.rename(folder / (digest[:16] + '.png'))
        site = {'url': url or 'https://eoncoatings.vercel.app/', 'pages': [], 'css_colors': ['#174DDE'],
                'images': [{'file': digest[:16] + '.png', 'sha256': digest, 'source': 'x', 'width': 1536, 'height': 1024, 'format': 'png'}]}
        analysis = {'summary': 'Bright rooms', 'style_spec': 'Daylight', 'mood': 'Bright', 'subjects': 'Rooms', 'avoid': 'Gloom',
                    'colors': ['#174DDE'], 'reference_images': [0]}
        with patch('rankme.brand_guide.scan', return_value=site), patch('rankme.covers.analyze_brand', return_value=analysis):
            self.engine.execute(job)
        return self.store.get('clients', 'c1')['image_brand'], digest

    def test_guide_fills_only_empty_fields_and_survives_style_edits(self):
        brand, _ = self.run_guide('https://eoncoatings.vercel.app')
        self.assertEqual(brand['site']['analysis']['style_spec'], 'Daylight')
        self.assertEqual(brand['mood'], 'My own mood')
        self.assertEqual(brand['subjects'], 'Rooms')
        self.assertEqual(brand['colors'], ['#123456'])
        edited = self.app.update_client('c1', {'image_brand': {'colors': ['#654321'], 'mode': 'photo'}})['image_brand']
        self.assertEqual(edited['site']['url'], brand['site']['url'])

    def test_reference_site_must_be_a_public_web_address(self):
        for bad in ('ftp://x.example', 'https://user:pw@x.example', 'not a url'):
            with self.assertRaises(ValueError):
                self.app.dispatch('POST', '/api/clients/c1/brand-guide', {'url': bad})

    def test_a_new_website_starts_a_fresh_guide(self):
        self.run_guide('https://eoncoatings.vercel.app')
        self.app.dispatch('POST', '/api/clients/c1/brand-guide', {'url': 'https://eoncoatings.com'})
        site = self.store.get('clients', 'c1')['image_brand']['site']
        self.assertEqual(site, {'url': 'https://eoncoatings.com/'})

    def request(self, path):
        handler = object.__new__(handler_for(self.app))
        handler.server = SimpleNamespace(server_port=8787)
        handler.path = path
        handler.headers = {'Host': 'localhost:8787', 'Cookie': '__Host-rankme-session=' + self.session}
        handler.rfile = io.BytesIO(b'{}')
        handler.send = Mock()
        handler.handle_request('GET')
        return handler.send.call_args.args

    def test_brand_images_are_served_only_when_listed_and_unchanged(self):
        brand, digest = self.run_guide()
        name = digest[:16] + '.png'
        status, body, mime = self.request('/api/clients/c1/brand-images/' + name)[:3]
        self.assertEqual((status, mime), (200, 'image/png'))
        self.assertEqual(self.request('/api/clients/c1/brand-images/' + 'f' * 16 + '.png')[0], 404)
        (Path(self.temp.name).resolve() / 'brand' / 'c1' / name).write_bytes(b'changed')
        self.assertEqual(self.request('/api/clients/c1/brand-images/' + name)[0], 404)

    def test_earlier_covers_exclude_the_current_article_and_carry_fingerprints(self):
        folder = Path(self.temp.name) / 'covers' / 'a1'
        folder.mkdir(parents=True)
        image = _shaded(folder / 'cover.png', 1536, 1024, 0x90)
        self.store.put('articles', {'id': 'a1', 'client_id': 'c1', 'title': 'Old', 'status': 'published', 'cover': {'path': str(image), 'alt': 'Floor'}})
        self.store.put('articles', {'id': 'a2', 'client_id': 'c1', 'title': 'New', 'status': 'planned'})
        earlier = self.engine.earlier_covers(self.store.get('clients', 'c1'), {'id': 'a2'})
        self.assertEqual([(e['title'], e['alt']) for e in earlier], [('Old', 'Floor')])
        self.assertTrue(earlier[0]['fingerprint'])
        self.assertEqual(self.store.get('articles', 'a1')['cover']['fingerprint'], earlier[0]['fingerprint'])
        self.assertEqual(self.engine.earlier_covers(self.store.get('clients', 'c1'), {'id': 'a1'}), [])


if __name__ == '__main__':
    unittest.main()
