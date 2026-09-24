"""Conservative, source-backed color suggestions from public website styles."""
import re
import colorsys
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse
from .crawler import fetch_public


class Styles(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'link' and 'stylesheet' in attrs.get('rel', '').lower():
            self.links.append(attrs.get('href', ''))


# How a website's covers are rendered. Every cover for one website uses the same mode.
COVER_MODES = ('illustration_3d', 'photo')
# Plain-language art direction the user writes once per website; reused verbatim for every cover.
DIRECTION_FIELDS = {'mood': 400, 'subjects': 800, 'avoid': 800}


def normalize_brand(value):
    """Validate user-editable cover style. Learned fields (references, style_spec) are managed by the server."""
    if not isinstance(value, dict):
        raise ValueError('Image brand must contain colors and a style direction')
    mode = value.get('mode') or 'illustration_3d'
    if mode not in COVER_MODES:
        raise ValueError('Choose 3D illustration or realistic photography for covers')
    colors = value.get('colors', [])
    if not isinstance(colors, list) or len(colors) > 6:
        raise ValueError('Choose up to six brand colors')
    result = []
    for color in colors:
        if isinstance(color, str) and re.fullmatch(r'#[0-9a-fA-F]{3}', color.strip()):
            color = '#' + ''.join(c * 2 for c in color.strip()[1:])
        if not isinstance(color, str) or not re.fullmatch(r'#[0-9a-fA-F]{6}', color.strip()):
            raise ValueError('Use six-digit hex colors, such as #245B78')
        color = color.strip().upper()
        if color not in result:
            result.append(color)
    brand = {'colors': result, 'style': str(value.get('style', ''))[:1600]}
    # Omit defaults so existing brands keep their identity (and their reviewed-cover digests).
    if mode != 'illustration_3d':
        brand['mode'] = mode
    for key, limit in DIRECTION_FIELDS.items():
        text = ' '.join(str(value.get(key) or '').split())[:limit]
        if text:
            brand[key] = text
    return brand


def extract_brand(url):
    host = urlparse(url).hostname
    page = fetch_public(url, allowed_hosts={host})
    parser = Styles()
    parser.feed(page['text'])
    documents = [(page['url'], page['text'])]
    for href in parser.links[:4]:
        target = urljoin(page['url'], href)
        if urlparse(target).hostname != host:
            continue
        try:
            doc = fetch_public(target, max_bytes=500000, allowed_hosts={host})
            documents.append((doc['url'], doc['text']))
        except (ValueError, OSError):
            continue
    candidates = []
    for source, text in documents:
        # Prefer the first (normally light-theme) value for each semantic token.
        seen = set()
        for name, value in re.findall(r'(--[\w-]*(?:brand|primary|accent|secondary)[\w-]*)\s*:\s*([^;}]+)', text, re.I):
            if name in seen or 'foreground' in name or name.startswith('--tw-'):
                continue
            seen.add(name)
            color = value.strip()
            if re.fullmatch(r'#[0-9a-fA-F]{3}', color):
                color = '#' + ''.join(char * 2 for char in color[1:])
            hsl = re.fullmatch(r'(?:hsl\()?([\d.]+)[ ,]+([\d.]+)%[ ,]+([\d.]+)%(?:\))?', color)
            if hsl:
                hue, saturation, light = map(float, hsl.groups())
                if not (0 <= saturation <= 100 and 0 <= light <= 100):
                    continue
                rgb = colorsys.hls_to_rgb((hue % 360) / 360, light / 100, saturation / 100)
                color = '#' + ''.join('%02X' % round(v * 255) for v in rgb)
            if not re.fullmatch(r'#[0-9a-fA-F]{6}', color):
                continue
            color = color.upper()
            if color in ('#FFFFFF', '#000000'):
                continue
            if color not in [c['color'] for c in candidates]:
                candidates.append({'color': color, 'token': name, 'source': source})
    return {'colors': [c['color'] for c in candidates[:4]], 'style': '', 'evidence': candidates[:4],
            'note': 'Suggested from public CSS design tokens; review these colors before generating covers.'}
