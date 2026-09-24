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
        if not ipaddress.ip_address(host).is_global:
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


def _addresses(host, port):
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not addresses:
        raise ValueError('Website address could not be resolved.')
    for item in addresses:
        address = ipaddress.ip_address(item[4][0])
        if not address.is_global or address.is_multicast:
            raise ValueError('Website resolves to a non-public address.')
    return addresses


def fetch_public(url, max_bytes=1500000, allowed_hosts=None):
    """Fetch HTML/text using DNS-pinned connections and validated redirects."""
    url = normalize_url(url)
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
                                                      'Accept': 'text/html,text/plain,application/xml',
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
            if content_type and not any(t in content_type.lower() for t in ('text/', 'xml', 'json')):
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

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ('script', 'style', 'noscript', 'svg'):
            self.hidden += 1
        if tag == 'title':
            self.in_title = True
        if tag == 'meta' and a.get('name', '').lower() == 'description':
            self.description = a.get('content', '')[:1000]
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

    def handle_data(self, data):
        if self.in_title:
            self.title_parts.append(data)
        if not self.hidden and data.strip():
            self.parts.append(data.strip())


def crawl_site(url, max_pages=24, progress=None):
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
                    host = new_host
                    return crawl_site(result['url'], max_pages, progress)
                issues.append('Skipped external redirect: ' + result['url'])
                continue
            parser = _Page(result['url'])
            parser.feed(result['text'])
            pages.append({'url': result['url'], 'title': ' '.join(parser.title_parts)[:500],
                          'description': parser.description, 'text': '\n'.join(parser.parts)[:24000],
                          'links': parser.links[:200]})
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
