"""Scan a brand reference website for its real imagery and colors, to anchor every cover to the brand.

Only public pages and images of the reference website itself are fetched (plus the share image the
site declares), within robots.txt, size, and count limits. Logos, icons, and small graphics are skipped.
"""
import hashlib
import os
import re
import tempfile
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlsplit
from urllib.robotparser import RobotFileParser

from .ai import AIError
from .covers import raster_info
from .crawler import USER_AGENT, fetch_public, normalize_url

# Graphics that are not brand imagery, plus images covers must never imitate: real people and before/after results.
SKIP = re.compile(r'(logo|icon|favicon|sprite|avatar|badge|flag|emoji|placeholder|spinner|arrow|pattern|texture'
                  r'|team|staff|doctor|headshot|portrait|testimonial|before|after)', re.I)
EXTENSIONS = ('.png', '.jpg', '.jpeg', '.webp')
MAX_PAGES, MAX_DOWNLOADS, KEEP = 4, 20, 8


class _Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.images, self.share, self.pages = [], [], []

    def handle_starttag(self, tag, attrs):
        a = {k: v or '' for k, v in attrs}
        if tag == 'meta' and (a.get('property') or a.get('name', '')).lower() in ('og:image', 'twitter:image'):
            self.share.append(a.get('content', ''))
        if tag in ('img', 'source'):
            srcset = a.get('srcset') or a.get('data-srcset')
            if srcset:
                # The last srcset candidate is conventionally the largest.
                self.images.append(srcset.split(',')[-1].strip().split(' ')[0])
            for key in ('src', 'data-src'):
                if a.get(key):
                    self.images.append(a[key])
        for url in re.findall(r'url\(["\']?([^"\')]+)', a.get('style', '')):
            self.images.append(url)
        if tag == 'a' and a.get('href'):
            self.pages.append(a['href'])


def _image_url(page, raw):
    url = urljoin(page, raw.strip())
    parts = urlsplit(url)
    if parts.path == '/_next/image':
        # Next.js image optimizer: use the original asset it points to.
        inner = parse_qs(parts.query).get('url', [''])[0]
        url = urljoin(page, inner) if inner else ''
    return url


def _hosts(host):
    bare = host.removeprefix('www.')
    return {host, bare, 'www.' + bare}


def scan(url, data_dir, client_id, progress=None):
    """Download the reference website's main images and return a guide record (no AI yet)."""
    url = normalize_url(url)
    host = urlsplit(url).hostname or ''
    hosts = _hosts(host)
    robots = RobotFileParser()
    try:
        robots.parse(fetch_public(urljoin(url, '/robots.txt'), 200000, hosts)['text'].splitlines())
    except ValueError:
        robots.parse([])
    home = fetch_public(url, allowed_hosts=hosts)
    parser = _Links()
    parser.feed(home['text'])
    pages = [home['url']]
    for href in parser.pages:
        target = urljoin(home['url'], href).split('#')[0]
        if (urlsplit(target).hostname in hosts and target not in pages and not target.lower().endswith(EXTENSIONS + ('.pdf', '.svg'))
                and len(pages) < MAX_PAGES and robots.can_fetch(USER_AGENT, target)):
            pages.append(target)
    share_hosts = set()
    for index, page in enumerate(pages):
        if index:
            try:
                sub = _Links()
                sub.feed(fetch_public(page, allowed_hosts=hosts)['text'])
                parser.images += sub.images
                parser.share += sub.share
            except ValueError:
                continue
    # Page order reflects prominence (hero images first). Share images come last: they often carry logos or text.
    found = [_image_url(url, raw) for raw in parser.images]
    for raw in parser.share:
        # The page's declared share image is part of its brand, even on a sibling domain (e.g. the production domain).
        share = _image_url(url, raw)
        if share:
            share_hosts.add(urlsplit(share).hostname)
            found.append(share)
    root = Path(data_dir).resolve() / 'brand' / client_id
    root.mkdir(parents=True, exist_ok=True)
    kept, seen, downloads = [], set(), 0
    for image_url in dict.fromkeys(u for u in found if u):
        path = urlsplit(image_url).path
        if SKIP.search(path) or (os.path.splitext(path)[1] and not path.lower().endswith(EXTENSIONS)):
            continue
        if urlsplit(image_url).hostname not in hosts | share_hosts:
            continue  # Only the brand's own images; third-party hosts are never contacted.
        if len(kept) >= KEEP or downloads >= MAX_DOWNLOADS:
            break
        if not robots.can_fetch(USER_AGENT, image_url):
            continue
        downloads += 1
        if progress:
            progress('Reading brand images from %s (%d)' % (host, downloads))
        try:
            data = fetch_public(image_url, 5_000_000, hosts | share_hosts, image=True)['data']
            kind, width, height = raster_info(data)
        except (ValueError, AIError, OSError):
            continue
        digest = hashlib.sha256(data).hexdigest()
        # Skip small graphics and thin banners; keep photos and artwork that show the brand's imagery.
        if digest in seen or min(width, height) < 320 or width * height < 150_000 or max(width, height) / min(width, height) > 3.2:
            continue
        seen.add(digest)
        name = digest[:16] + '.' + ('jpg' if kind == 'jpeg' else kind)
        target = root / name
        if not target.exists():
            with tempfile.NamedTemporaryFile(dir=str(root), prefix='.img-', delete=False) as handle:
                handle.write(data)
            os.replace(handle.name, target)
        kept.append({'file': name, 'sha256': digest, 'source': image_url[:1000], 'width': width, 'height': height, 'format': kind})
    colors = []
    try:
        from .brand import extract_brand
        colors = extract_brand(url).get('colors', [])
    except (ValueError, OSError):
        pass
    return {'url': url, 'pages': pages, 'images': kept, 'css_colors': colors}


def image_path(data_dir, client_id, item):
    """Resolve one saved guide image, verifying it is inside the brand folder and unchanged."""
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,100}', str(client_id)) or not re.fullmatch(r'[0-9a-f]{16}\.(?:png|jpg|webp)', str(item.get('file', ''))):
        return None
    root = Path(data_dir).resolve() / 'brand' / client_id
    path = root / item['file']
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 5_000_000:
            return None
        if hashlib.sha256(path.read_bytes()).hexdigest() != item.get('sha256'):
            return None
    except OSError:
        return None
    return path
