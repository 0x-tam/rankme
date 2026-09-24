import unittest
from unittest.mock import patch
from rankme.brand import extract_brand, normalize_brand


class BrandTests(unittest.TestCase):
    def test_normalizes_and_bounds_colors(self):
        self.assertEqual(normalize_brand({'colors':['#abc','#AABBCC'],'style':'Natural light'})['colors'],['#AABBCC'])
        with self.assertRaises(ValueError):
            normalize_brand({'colors':['red']})
        with self.assertRaises(ValueError):
            normalize_brand({'colors':['#123456']*7})

    def test_extracts_first_light_theme_hsl_and_ignores_external_css(self):
        calls=[]
        def fetch(url,**kwargs):
            calls.append(url)
            if url.endswith('main.css'):
                return {'url':url,'text':':root{--primary:358 72% 46%;--primary-foreground:0 0% 100%;--accent:#efe;} .dark{--primary:#112233;}'}
            return {'url':url,'text':'<link rel="stylesheet" href="/main.css"><link rel="stylesheet" href="https://other.test/x.css">'}
        with patch('rankme.brand.fetch_public',side_effect=fetch):
            brand=extract_brand('https://example.com/')
        self.assertEqual(brand['colors'],['#CA2126','#EEFFEE'])
        self.assertEqual(len(calls),2)
        self.assertEqual(brand['evidence'][0]['source'],'https://example.com/main.css')

    def test_no_palette_is_not_invented(self):
        with patch('rankme.brand.fetch_public',return_value={'url':'https://example.com/','text':'<p>No styles</p>'}):
            self.assertEqual(extract_brand('https://example.com/')['colors'],[])
