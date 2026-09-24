"""Bounded public-web inspection. Retrieved content is always untrusted data."""
import collections
import http.client
import ipaddress
import re
import socket
import ssl
import time
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from urllib.parse import quote, urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

USER_AGENT = 'RankMe/1.0 (website content inspection)'


def normalize_url(url):
    if any(ord(c) < 32 or ord(c) == 127 for c in str(url)):
        raise ValueError('Invalid URL characters.')
    url = str(url).strip()
    if '://' not in url:
        url = 'https://' + url
    p = urlsplit(url)
    if p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password:
        raise ValueError('Use a public HTTP or HTTPS URL without credentials.')
    host = p.hostname.encode('idna').decode('ascii').lower().rstrip('.')
    if host == 'localhost' or host.endswith(('.localhost', '.local', '.internal')) or '%' in host:
        raise ValueError('Local addresses are not allowed.')
    try:
        if not _public_address(ipaddress.ip_address(host)):
            raise ValueError('Private addresses are not allowed.')
    except ValueError as exc:
        if 'not allowed' in str(exc):
            raise
    port = p.port
    if port not in (None, 80, 443):
        raise ValueError('Only standard web ports are allowed.')
    netloc = '[' + host + ']' if ':' in host else host
    if port and port != (443 if p.scheme == 'https' else 80):
        netloc += ':' + str(port)
    path = p.path or '/'
    if any(ord(c) < 32 for c in url):
        raise ValueError('Invalid URL characters.')
    return urlunsplit((p.scheme, netloc, quote(path, safe='/%:@!$&\'()*+,;=-._~'),
                       quote(p.query, safe='%/:?@!$&\'()*+,;=-._~'), ''))


def _public_address(address):
    # Transition addresses can route to embedded private IPv4 destinations.
    if not address.is_global or address.is_multicast or address.is_reserved:
        return False
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return _public_address(address.ipv4_mapped)
        if address.sixtofour is not None or address.teredo is not None:
            return False
        if address in ipaddress.ip_network('64:ff9b::/96') or address in ipaddress.ip_network('64:ff9b:1::/48'):
            return False
    return True


def _addresses(host, port):
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not addresses:
        raise ValueError('Website address could not be resolved.')
    for item in addresses:
        address = ipaddress.ip_address(item[4][0])
        if not _public_address(address):
            raise ValueError('Website resolves to a non-public address.')
    return addresses


IMAGE_TYPES = ('image/png', 'image/jpeg', 'image/webp')


def fetch_public(url, max_bytes=1500000, allowed_hosts=None, image=False):
    """Fetch HTML/text (or, with image=True, raw PNG/JPEG/WebP bytes) using DNS-pinned connections and validated redirects."""
    url = normalize_url(url)
    if not isinstance(max_bytes, int) or not 1 <= max_bytes <= 10000000:
        raise ValueError('Invalid website response size limit.')
    deadline = time.monotonic() + 45
    for _ in range(6):
        p = urlsplit(url)
        if allowed_hosts is not None and p.hostname not in allowed_hosts:
            raise ValueError('Redirect leaves the inspected website.')
        if time.monotonic() >= deadline:
            raise ValueError('Website request exceeded its time limit.')
        addresses = _addresses(p.hostname, p.port or (443 if p.scheme == 'https' else 80))
        sock = None
        try:
            # Connect to the validated IP directly: a second DNS lookup cannot rebind it.
            last_error = None
            for family, kind, proto, _, address in addresses[:4]:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ValueError('Website request exceeded its time limit.')
                try:
                    sock = socket.socket(family, kind, proto)
                    sock.settimeout(min(10, remaining))
                    sock.connect(address)
                    if p.scheme == 'https':
                        sock = ssl.create_default_context().wrap_socket(sock, server_hostname=p.hostname)
                    break
                except OSError as exc:
                    last_error = exc
                    sock.close()
                    sock = None
            if sock is None:
                raise ValueError('Could not connect to the public website: %s' % str(last_error)[:120])
            connection = http.client.HTTPConnection(p.hostname, timeout=15)
            connection.sock = sock
            path = urlunsplit(('', '', p.path or '/', p.query, ''))
            connection.request('GET', path, headers={'Host': p.netloc, 'User-Agent': USER_AGENT,
                                                      'Accept': ', '.join(IMAGE_TYPES) if image else 'text/html,text/plain,application/xml',
                                                      'Accept-Encoding': 'identity'})
            response = connection.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader('Location')
                if not location:
                    raise ValueError('Redirect without destination.')
                url = normalize_url(urljoin(url, location))
                continue
            if response.status >= 400:
                raise ValueError('Website returned HTTP %s.' % response.status)
            content_type = response.getheader('Content-Type', '')
            if image and content_type.split(';')[0].strip().lower() not in IMAGE_TYPES:
                raise ValueError('Unsupported image type.')
            if not image and content_type and not any(t in content_type.lower() for t in ('text/', 'xml', 'json')):
                raise ValueError('Unsupported website content type.')
            chunks, size = [], 0
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ValueError('Website request exceeded its time limit.')
                sock.settimeout(min(15, remaining))
                chunk = response.read1(min(65536, max_bytes + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > max_bytes:
                    raise ValueError('Website response exceeds size limit.')
            data = b''.join(chunks)
            if image:
                return {'url': url, 'data': data, 'content_type': content_type, 'status': response.status}
            charset = re.search(r'charset=["\']?([^;\s"\']+)', content_type)
            encoding = charset.group(1) if charset else 'utf-8'
            try:
                text = data.decode(encoding, errors='replace')
            except LookupError:
                text = data.decode('utf-8', errors='replace')
            return {'url': url, 'text': text, 'content_type': content_type, 'status': response.status}
        finally:
            if sock:
                sock.close()
    raise ValueError('Too many website redirects.')


class _Page(HTMLParser):
    def __init__(self, base):
        super().__init__(convert_charrefs=True)
        self.base, self.parts, self.links, self.title_parts = base, [], [], []
        self.description = ''
        self.hidden = 0
        self.in_title = False
        self.canonical, self.robots, self.headings = '', [], []
        self.heading = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ('script', 'style', 'noscript', 'svg'):
            self.hidden += 1
        if tag == 'title':
            self.in_title = True
        if tag == 'meta' and a.get('name', '').lower() == 'description':
            self.description = a.get('content', '')[:1000]
        if tag == 'meta' and a.get('name', '').lower() in ('robots', 'googlebot'):
            self.robots.append(a.get('content', '')[:1000])
        if tag == 'link' and 'canonical' in a.get('rel', '').lower().split() and a.get('href'):
            try:
                self.canonical = normalize_url(urljoin(self.base, a['href']))
            except (ValueError, UnicodeError):
                pass
        if tag in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6') and len(self.headings) < 100:
            self.heading = {'level': int(tag[1]), 'text': ''}
            self.headings.append(self.heading)
        if tag == 'a' and a.get('href'):
            try:
                link = normalize_url(urljoin(self.base, a['href']))
                if link not in self.links:
                    self.links.append(link)
            except (ValueError, UnicodeError):
                pass

    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'noscript', 'svg'):
            self.hidden = max(0, self.hidden - 1)
        if tag == 'title':
            self.in_title = False
        if tag in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6'):
            self.heading = None

    def handle_data(self, data):
        if self.in_title:
            self.title_parts.append(data)
        if not self.hidden and data.strip():
            self.parts.append(data.strip())
            if self.heading is not None:
                self.heading['text'] = (self.heading['text'] + ' ' + data.strip()).strip()[:1000]


def crawl_site(url, max_pages=24, progress=None, _canonical_hops=0):
    url = normalize_url(url)
    max_pages = max(1, min(60, int(max_pages)))
    pages, issues, visited = [], [], set()
    crawl_deadline = time.monotonic() + 300
    host = urlsplit(url).hostname
    robots = RobotFileParser()
    robots_url = urljoin(url, '/robots.txt')
    try:
        robots_result = fetch_public(robots_url, 200000, {host, 'www.' + host.removeprefix('www.'), host.removeprefix('www.')})
        robots.parse(robots_result['text'].splitlines())
    except Exception as exc:
        # Missing robots is common; explicit denials and network errors are treated conservatively.
        if 'HTTP 404' in str(exc):
            robots.parse([])
        else:
            issues.append('Robots could not be checked: %s' % str(exc)[:180])
            return {'url': url, 'pages': [], 'issues': issues,
                    'summary': {'pages_crawled': 0, 'limited': True}}
    queue = collections.deque([url])
    # Read a bounded sitemap index to discover pages absent from navigation.
    sitemap_queue = list(robots.site_maps() or [urljoin(url, '/sitemap.xml')])[:3]
    sitemap_seen = set()
    while sitemap_queue and len(sitemap_seen) < 3:
        sitemap = sitemap_queue.pop(0)
        try:
            sitemap = normalize_url(sitemap)
            if sitemap in sitemap_seen or urlsplit(sitemap).hostname != host:
                continue
            sitemap_seen.add(sitemap)
            if not robots.can_fetch(USER_AGENT, sitemap):
                continue
            result = fetch_public(sitemap, 500000, {host})
            if urlsplit(result['url']).hostname != host:
                continue
            xml = ET.fromstring(result['text'])
            locations = [element.text.strip() for element in xml.iter()
                         if element.tag.rsplit('}', 1)[-1] == 'loc' and element.text][:500]
            is_index = xml.tag.rsplit('}', 1)[-1] == 'sitemapindex'
            for location in locations:
                candidate = normalize_url(location)
                if urlsplit(candidate).hostname != host:
                    continue
                if is_index:
                    sitemap_queue.append(candidate)
                elif not urlsplit(candidate).query:
                    queue.append(candidate)
        except (ValueError, OSError, ET.ParseError, http.client.HTTPException):
            pass  # A sitemap is optional; navigation remains a valid discovery path.
    attempts = 0
    while queue and len(pages) < max_pages and attempts < max_pages * 3:
        if time.monotonic() >= crawl_deadline:
            issues.append('Inspection reached its five-minute time limit; remaining pages were skipped.')
            break
        current = queue.popleft()
        if current in visited:
            continue
        visited.add(current)
        if not robots.can_fetch(USER_AGENT, current):
            issues.append('Robots disallows: ' + current)
            continue
        attempts += 1
        if progress:
            progress('Inspecting %s' % current)
        try:
            result = fetch_public(current, 1500000, {host, 'www.' + host.removeprefix('www.'), host.removeprefix('www.')})
            if urlsplit(result['url']).hostname != host:
                # Allow canonical www redirects for initial page only.
                new_host = urlsplit(result['url']).hostname
                if not pages and new_host.removeprefix('www.') == host.removeprefix('www.'):
                    if _canonical_hops >= 2:
                        issues.append('Website canonical redirects did not stabilize.')
                        break
                    host = new_host
                    return crawl_site(result['url'], max_pages, progress, _canonical_hops + 1)
                issues.append('Skipped external redirect: ' + result['url'])
                continue
            parser = _Page(result['url'])
            parser.feed(result['text'])
            pages.append({'url': result['url'], 'title': ' '.join(parser.title_parts)[:500],
                          'description': parser.description, 'text': '\n'.join(parser.parts)[:24000],
                          'links': parser.links[:200], 'status': result.get('status', 200),
                          'canonical': parser.canonical, 'robots': parser.robots[:20],
                          'headings': parser.headings})
            candidates = [link for link in parser.links if urlsplit(link).hostname == host
                          and not urlsplit(link).query and not re.search(
                              r'\.(pdf|jpg|png|zip|mp4|css|js|webp|gif|svg)$', urlsplit(link).path, re.I)]
            candidates.sort(key=lambda link: (not any(word in urlsplit(link).path.lower()
                            for word in ('about', 'service', 'product', 'contact', 'blog', 'pricing')), len(link)))
            queue.extend(candidates[:100])
            queue = collections.deque(sorted(set(queue) - visited, key=lambda link: (
                not any(word in urlsplit(link).path.lower() for word in ('about', 'service', 'product', 'contact', 'pricing')),
                len(urlsplit(link).path.strip('/').split('/')), len(link))))
        except Exception as exc:
            issues.append('%s: %s' % (current, str(exc)[:180]))
    return {'url': url, 'pages': pages, 'issues': issues[:100],
            'summary': {'pages_crawled': len(pages), 'limited': bool(queue),
                        'note': 'Public HTML only; JavaScript-rendered or private content may be missing.'}}
