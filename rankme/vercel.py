"""Read-only Vercel account link and RankMe-managed copies of Git-connected websites.

RankMe never changes anything in Vercel. It lists projects, reads each project's
linked GitHub repository and production branch, and watches production
deployments after RankMe pushes an article commit. Pushing uses this Mac's
existing Git credentials, so no GitHub token is stored.

REST API: https://vercel.com/docs/rest-api
"""
import json
import os
import re
import shutil
import stat
import tempfile
import threading
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .publisher import PublishError, _run, inspect_project

API = 'https://api.vercel.com'
SAFE_NAME = re.compile(r'[A-Za-z0-9_.-]{1,100}')
SAFE_ID = re.compile(r'[A-Za-z0-9_-]{1,100}')
# Deployment states from the Vercel API, mapped to what RankMe tells the user.
BUILDING = ('QUEUED', 'INITIALIZING', 'BUILDING')
FAILED = ('ERROR', 'CANCELED')
# Folders that a site's blog commonly reads Markdown/MDX articles from.
CONTENT_DIRS = ('content/blog', 'src/content/blog', 'content/articles', 'src/content/articles', 'content/posts',
                'src/content/posts', 'posts', 'src/posts', 'blog', 'src/blog')
# Build-time prerendering packages: without one, a Vite single-page app serves an
# empty HTML shell, so crawlers and RankMe's live check cannot see article text.
PRERENDER_PACKAGES = ('vite-plugin-prerender', 'vite-ssg', 'vike', 'vite-plugin-ssr', '@prerenderer/rollup-plugin',
                      'vite-prerender-plugin', 'react-snap', '@tanstack/start', '@react-router/dev')
_LOCKS = {}
_LOCKS_GUARD = threading.Lock()


class VercelError(ValueError):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _get(path, token, params=None):
    if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,200}', token):
        raise VercelError('The saved Vercel token is invalid. Connect Vercel again.')
    query = {k: v for k, v in (params or {}).items() if v not in (None, '')}
    url = API + path + ('?' + urlencode(query) if query else '')
    request = Request(url, headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/json'}, method='GET')
    try:
        with build_opener(_NoRedirect()).open(request, timeout=20) as response:
            raw = response.read(5000001)
    except HTTPError as exc:
        # Never include response bodies, tokens, or request URLs in errors.
        if exc.code in (401, 403):
            raise VercelError('Vercel rejected the token. Create a new token and connect again.') from None
        if exc.code == 404:
            raise VercelError('The Vercel project was not found or this token cannot access it.') from None
        raise VercelError('Vercel is temporarily unavailable. Try again later.') from None
    except (URLError, OSError, TimeoutError):
        raise VercelError('Could not reach Vercel. Check the internet connection and retry.') from None
    if len(raw) > 5000000:
        raise VercelError('Vercel response exceeded the local size limit.')
    try:
        result = json.loads(raw.decode('utf-8'))
    except (UnicodeError, ValueError):
        raise VercelError('Vercel returned an unreadable response.') from None
    if not isinstance(result, dict):
        raise VercelError('Vercel returned an unexpected response.')
    return result


def _project(raw, team_id):
    link = raw.get('link') if isinstance(raw.get('link'), dict) else {}
    org, repo = str(link.get('org') or ''), str(link.get('repo') or '')
    github = link.get('type') == 'github' and SAFE_NAME.fullmatch(org) and SAFE_NAME.fullmatch(repo)
    branch = str(link.get('productionBranch') or 'main')
    production = ((raw.get('targets') or {}).get('production') or {}) if isinstance(raw.get('targets'), dict) else {}
    aliases = [a for a in production.get('alias') or [] if isinstance(a, str)]
    custom = [a for a in aliases if not a.endswith('.vercel.app')]
    return {'id': str(raw.get('id', '')), 'name': str(raw.get('name', '')), 'team_id': team_id or '',
            'framework': str(raw.get('framework') or ''), 'repo': f'{org}/{repo}' if github else '',
            'branch': branch if re.fullmatch(r'[A-Za-z0-9._/-]{1,200}', branch) else 'main',
            'domains': custom + [a for a in aliases if a not in custom],
            'git_provider': str(link.get('type') or '')}


class Vercel:
    def __init__(self, data_dir):
        self.root = Path(data_dir).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / 'vercel-secrets.json'
        with _LOCKS_GUARD:
            self.lock = _LOCKS.setdefault(str(self.path), threading.RLock())

    def _read(self):
        if self.path.is_symlink():
            raise VercelError('Vercel credentials file must not be a symbolic link.')
        if not self.path.exists():
            return {}
        try:
            descriptor = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, 'r', encoding='utf-8') as handle:
                metadata = os.fstat(handle.fileno())
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_size > 100000:
                    raise VercelError('Vercel credentials must be a small, regular, unshared file.')
                os.fchmod(handle.fileno(), 0o600)
                data = json.loads(handle.read(100001))
            if not isinstance(data, dict) or not isinstance(data.get('token', ''), str):
                raise ValueError()
            return data
        except (OSError, ValueError):
            raise VercelError('Vercel credentials could not be read. Connect Vercel again.') from None

    def _save(self, value):
        if self.path.is_symlink():
            raise VercelError('Vercel credentials file must not be a symbolic link.')
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=str(self.root), prefix='.vercel-', delete=False) as handle:
            name = handle.name
            os.fchmod(handle.fileno(), 0o600)
            json.dump(value, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, self.path)

    def _token(self):
        with self.lock:
            token = self._read().get('token')
        if not token:
            raise VercelError('Connect your Vercel account in Settings first.')
        return token

    def connect(self, token):
        token = str(token or '').strip()
        if not re.fullmatch(r'[A-Za-z0-9_-]{16,200}', token):
            raise VercelError('Paste a Vercel access token from vercel.com/account/tokens.')
        user = _get('/v2/user', token).get('user') or {}
        teams = [{'id': str(t.get('id')), 'name': str(t.get('name') or t.get('slug') or 'Team')}
                 for t in _get('/v2/teams', token, {'limit': 100}).get('teams') or [] if SAFE_ID.fullmatch(str(t.get('id', '')))]
        with self.lock:
            self._save({'token': token, 'user': str(user.get('username') or user.get('email') or 'Vercel user')[:200], 'teams': teams})
        return self.status()

    def disconnect(self):
        with self.lock:
            if self.path.exists() and not self.path.is_symlink():
                self.path.unlink()
        return {**self.status(), 'message': 'Vercel disconnected in RankMe. Delete the token at vercel.com/account/tokens to revoke it completely.'}

    def status(self):
        with self.lock:
            try:
                data = self._read()
            except VercelError as exc:
                return {'connected': False, 'message': str(exc)}
        if not data.get('token'):
            return {'connected': False, 'message': 'Connect Vercel to choose each website’s project.'}
        return {'connected': True, 'user': data.get('user', ''), 'teams': data.get('teams', []),
                'message': 'Vercel is connected with read-only use: RankMe lists projects and watches deployments.'}

    def projects(self):
        token = self._token()
        scopes = [''] + [t['id'] for t in self.status().get('teams', [])]
        found = []
        for team_id in scopes:
            for raw in _get('/v9/projects', token, {'limit': 100, 'teamId': team_id}).get('projects') or []:
                if isinstance(raw, dict):
                    found.append(_project(raw, team_id))
        unique = {p['id']: p for p in found if SAFE_ID.fullmatch(p['id'])}
        return {'projects': sorted(unique.values(), key=lambda p: p['name'].lower())}

    def project(self, team_id, project_id):
        if not SAFE_ID.fullmatch(str(project_id)) or (team_id and not SAFE_ID.fullmatch(str(team_id))):
            raise VercelError('Choose a valid Vercel project.')
        token = self._token()
        project = _project(_get('/v9/projects/' + project_id, token, {'teamId': team_id}), team_id)
        domains = _get('/v9/projects/' + project_id + '/domains', token, {'teamId': team_id}).get('domains') or []
        # Prefer a verified custom domain that serves content rather than redirecting.
        serving = [d.get('name') for d in domains if isinstance(d, dict) and d.get('verified', True) and not d.get('redirect')
                   and isinstance(d.get('name'), str) and not d['name'].endswith('.vercel.app')]
        if serving:
            project['domains'] = serving + [d for d in project['domains'] if d not in serving]
        return project

    def deployment(self, team_id, project_id, commit):
        """Return the production deployment built from one commit, if Vercel has started it."""
        if not re.fullmatch(r'[0-9a-f]{7,64}', str(commit or '')):
            return None
        items = _get('/v6/deployments', self._token(), {'projectId': project_id, 'teamId': team_id, 'target': 'production', 'limit': 20}).get('deployments') or []
        for item in items:
            meta = item.get('meta') if isinstance(item, dict) and isinstance(item.get('meta'), dict) else {}
            sha = str(meta.get('githubCommitSha') or '')
            if sha and (sha.startswith(commit) or commit.startswith(sha)):
                state = str(item.get('readyState') or item.get('state') or 'QUEUED').upper()
                inspector = item.get('inspectorUrl') if str(item.get('inspectorUrl', '')).startswith('https://vercel.com/') else ''
                return {'id': str(item.get('uid') or item.get('id') or ''), 'state': state, 'inspector_url': inspector}
        return None


def _git(argv, cwd, timeout=180):
    # Never prompt for credentials in a background worker; the Mac's credential helper answers or the call fails.
    return _run(['git'] + argv, cwd, timeout=timeout, env={**os.environ, 'GIT_TERMINAL_PROMPT': '0', 'GCM_INTERACTIVE': 'never'})


def repo_url(repo):
    org, _, name = str(repo).partition('/')
    if not (SAFE_NAME.fullmatch(org) and SAFE_NAME.fullmatch(name)):
        raise VercelError('This Vercel project is not linked to a GitHub repository.')
    return f'https://github.com/{org}/{name}.git'


def site_dir(data_dir, client_id):
    if not SAFE_ID.fullmatch(str(client_id)):
        raise VercelError('Invalid website identifier.')
    return Path(data_dir).resolve() / 'sites' / client_id


def github_access(repo):
    """Check that this Mac's Git credentials can read the repository (push rights are checked on publish)."""
    try:
        _git(['ls-remote', '--heads', repo_url(repo)], Path.home(), timeout=45)
        return {'ok': True, 'message': 'This Mac can reach the GitHub repository.'}
    except PublishError:
        return {'ok': False, 'message': 'This Mac cannot access the GitHub repository. Run “gh auth login” in Terminal, then check again.'}


def sync_site(data_dir, client_id, repo, branch):
    """Create or fast-forward RankMe's own clean copy of the website repository."""
    path, url = site_dir(data_dir, client_id), repo_url(repo)
    if not re.fullmatch(r'[A-Za-z0-9._/-]{1,200}', branch) or branch.startswith('-'):
        raise VercelError('Invalid production branch.')
    if path.exists() and not (path / '.git').is_dir():
        raise VercelError('RankMe’s copy of this website is damaged. Disconnect and connect the project again.')
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            _git(['clone', '--branch', branch, '--single-branch', '--', url, str(path)], path.parent, timeout=300)
        except PublishError:
            shutil.rmtree(path, ignore_errors=True)
            raise VercelError('Could not copy the GitHub repository. Check that this Mac is signed in to GitHub (gh auth login).') from None
        return path
    if _git(['remote', 'get-url', 'origin'], path) != url:
        raise VercelError('RankMe’s copy points at a different repository. Disconnect and connect the project again.')
    if _git(['status', '--porcelain'], path):
        # Leftovers from an interrupted publish are resolved by the publisher's own recovery.
        return path
    _git(['fetch', 'origin', branch], path)
    if _git(['branch', '--show-current'], path) != branch:
        _git(['checkout', branch], path)
    ahead = _git(['rev-list', '--count', f'origin/{branch}..HEAD'], path)
    if ahead == '0':
        _git(['merge', '--ff-only', f'origin/{branch}'], path)
    return path


def readiness(path):
    """Describe whether the site can show RankMe articles as real, crawlable pages."""
    project = inspect_project(path)
    content_dir = next((d for d in CONTENT_DIRS if (Path(path) / d).is_dir()), '')
    try:
        package = json.loads((Path(path) / 'package.json').read_text())
        dependencies = {**package.get('dependencies', {}), **package.get('devDependencies', {})}
    except (OSError, ValueError, AttributeError):
        dependencies = {}
    framework = project.get('framework', 'unknown')
    if 'vite' in dependencies and framework == 'unknown':
        framework = 'Vite'
    prerendered = framework in ('Next.js', 'Astro', 'SvelteKit', 'Gatsby') or any(p in dependencies for p in PRERENDER_PACKAGES)
    checks = [{'key': 'blog', 'ok': bool(content_dir),
               'label': f'Blog folder found: {content_dir}' if content_dir else 'No blog section found in the site code',
               'detail': '' if content_dir else 'Articles would be saved to the repository but not shown on the website until the site has a blog section that reads them.'},
              {'key': 'crawlable', 'ok': prerendered,
               'label': f'{framework} builds real HTML pages' if prerendered else f'{framework} single-page app without prerendering',
               'detail': '' if prerendered else 'Pages are blank until JavaScript runs, so search engines, AI crawlers, and RankMe’s live check see little or nothing. The blog section needs build-time prerendering.'}]
    return {'framework': framework, 'content_dir': content_dir, 'checks': checks, 'ready': all(c['ok'] for c in checks)}
