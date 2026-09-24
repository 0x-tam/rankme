"""Conservative publishing adapters for local, code-backed websites."""
import hashlib
import html
import json
import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse


class PublishError(ValueError):
    pass


def _run(argv, cwd, timeout=300, env=None):
    # Child processes can print credentials in either stream or command arguments.
    # Persist only executable name and exit code, never raw subprocess output.
    try:
        result = subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        raise PublishError("Command timed out after %s seconds." % timeout) from None
    except OSError:
        raise PublishError("Unable to start the configured command; check its executable and project directory.") from None
    if result.returncode:
        raise PublishError("Command %s failed with exit code %s. Inspect the command locally for details." % (Path(argv[0]).name, result.returncode))
    return result.stdout.rstrip()


def _argv(value):
    if not value:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            raise PublishError('Commands must be JSON argument arrays, for example ["npm", "run", "build"].')
    if not isinstance(value, list) or not value or not all(isinstance(x, str) and x and '\0' not in x for x in value):
        raise PublishError("Invalid command argument array.")
    return value


def _project(connection):
    value = connection.get('project_path', '')
    if not value:
        raise PublishError('Choose a local website project first.')
    root = Path(value).expanduser().resolve()
    if not root.is_dir():
        raise PublishError('Website project directory does not exist.')
    return root


def _inside(root, path):
    resolved = path.resolve()
    if resolved != root and root not in resolved.parents:
        raise PublishError('Content path must remain inside the website project.')
    return resolved


def inspect_project(path):
    root = Path(path).expanduser()
    result = dict(exists=root.is_dir(), git=False, framework='unknown', suggested_content_dir='content/blog', message='Project does not exist.')
    if not root.is_dir():
        return result
    try:
        result['git'] = _run(['git', 'rev-parse', '--is-inside-work-tree'], root) == 'true'
    except (PublishError, OSError):
        pass
    try:
        package = json.loads((root / 'package.json').read_text())
        dependencies = dict(package.get('dependencies', {}), **package.get('devDependencies', {}))
        for package_name, framework, folder in [('astro','Astro','src/content/blog'),('next','Next.js','content/blog'),('@sveltejs/kit','SvelteKit','content/blog'),('gatsby','Gatsby','content/blog')]:
            if package_name in dependencies:
                result.update(framework=framework, suggested_content_dir=folder)
                break
    except (OSError, ValueError, TypeError):
        pass
    result['message'] = 'Confirm that the website reads articles from the selected content directory. Exporting files alone does not create a blog.'
    return result


def validate_connection(connection):
    errors = []
    try:
        root = _project(connection)
        folder = connection.get('content_dir') or 'content/blog'
        if Path(folder).is_absolute() or '..' in Path(folder).parts or '.git' in Path(folder).parts:
            raise PublishError('Use a project-relative content directory without parent traversal.')
        _inside(root, root / folder)
        image_folder = connection.get('image_dir') or 'public/images/articles'
        if Path(image_folder).is_absolute() or '..' in Path(image_folder).parts or '.git' in Path(image_folder).parts:
            raise PublishError('Use a project-relative image directory without parent traversal.')
        _inside(root, root / image_folder)
        prefix = connection.get('image_url_prefix') or '/images/articles'
        if not re.fullmatch(r'/(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-]+/?', prefix):
            raise PublishError('Image URL prefix must be a root-relative path, such as /images/articles.')
        if connection.get('format', 'md') not in ('md', 'mdx', 'json'):
            raise PublishError('Supported article formats: md, mdx, json.')
        if connection.get('mode', 'export') not in ('export', 'git'):
            raise PublishError('Supported publishing modes: export, git.')
        _argv(connection.get('build_command'))
        _argv(connection.get('deploy_command'))
        template = connection.get('public_url_template', '')
        if template and (urlparse(template).scheme not in ('http', 'https') or not urlparse(template).hostname or urlparse(template).username is not None or '{slug}' not in template):
            raise PublishError('Public URL template must be an HTTP(S) URL containing {slug}.')
        if connection.get('mode') == 'git':
            if not connection.get('branch') or str(connection['branch']).startswith('-'):
                raise PublishError('Git publishing requires an explicit branch.')
            _run(['git', 'check-ref-format', '--branch', connection['branch']], root)
            if _run(['git', 'rev-parse', '--show-toplevel'], root) != str(root):
                raise PublishError('Choose the repository root as the project directory.')
            # Vercel builds each push itself and keeps the previous production deployment
            # live if that build fails, so a local build step is optional there.
            if not _argv(connection.get('build_command')) and connection.get('provider') != 'vercel':
                raise PublishError('Git publishing requires a build validation command.')
            remote = connection.get('remote', 'origin')
            if not re.fullmatch(r'[A-Za-z0-9_.-]+', remote) or remote.startswith('-'):
                raise PublishError('Invalid git remote name.')
    except (PublishError, OSError, subprocess.SubprocessError) as exc:
        errors.append(str(exc))
    return {'ok': not errors, 'errors': errors, 'message': '; '.join(errors) if errors else 'Connection configuration is valid.'}


def _validate_article(article):
    if not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', article.get('slug', '')) or len(article['slug']) > 150:
        raise PublishError('Article slug must contain lowercase letters, numbers and single hyphens only.')
    for key in ('id', 'title', 'body'):
        if not isinstance(article.get(key), str) or not article[key].strip():
            raise PublishError('Article requires a nonempty %s.' % key)
    # Markdown renderers often allow raw HTML. Generated content must remain inert.
    decoded = html.unescape(article['body'])
    compact = re.sub(r'[\x00-\x20\x7f]', '', decoded)
    if (re.search(r'<\s*/?\s*[A-Za-z!]', decoded, re.I)
            or re.search(r'(?:javascript|vbscript|data):', compact, re.I)):
        raise PublishError('Raw HTML and unsafe link schemes are not permitted in generated articles.')


def _hash(article):
    fields = {k: article.get(k) for k in ('id','title','slug','description','body','sources')}
    if article.get('cover'):
        fields['cover'] = {key: article['cover'].get(key) for key in ('url', 'alt', 'sha256')}
    return hashlib.sha256(json.dumps(fields, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def export_markdown(article):
    _validate_article(article)
    metadata = {'title': article['title'], 'description': article.get('description', ''), 'slug': article['slug'], 'date': article.get('published_at') or article.get('created_at') or datetime.now(timezone.utc).date().isoformat(), 'rankme_id': article['id'], 'rankme_hash': _hash(article)}
    if article.get('cover', {}).get('url'):
        metadata.update(image=article['cover']['url'], imageAlt=article['cover'].get('alt', ''))
    return '---\n' + '\n'.join('%s: %s' % (key, json.dumps(value, ensure_ascii=False)) for key, value in metadata.items()) + '\n---\n\n' + article['body'].strip() + '\n'


def _identity(text, format):
    if format == 'json':
        try:
            data = json.loads(text)
            return data.get('rankme_id'), data.get('rankme_hash')
        except ValueError:
            return None, None
    if not text.startswith('---\n'):
        return None, None
    header = text.split('\n---\n', 1)[0]
    values = []
    for key in ('rankme_id', 'rankme_hash'):
        match = re.search(r'^' + key + r': (.+)$', header, re.M)
        try:
            values.append(json.loads(match.group(1)) if match else None)
        except ValueError:
            values.append(None)
    return tuple(values)


def _atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix='.rankme-', dir=str(path.parent))
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(data.encode('utf-8') if isinstance(data, str) else data)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _connection_hash(connection):
    config = dict(connection, project_path=str(_project(connection)))
    return hashlib.sha256(json.dumps(config, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _normalized_date(text):
    return re.sub(r'^date: .+$', 'date: <preserved>', text, count=1, flags=re.M)


def _json_article(article):
    public = {key: article[key] for key in ('id', 'title', 'slug', 'description', 'body', 'sources', 'claims',
              'internal_links', 'created_at', 'published_at', 'cover') if key in article}
    if article.get('cover', {}).get('url'):
        public.update(image=article['cover']['url'], imageAlt=article['cover'].get('alt', ''))
    return dict(public, rankme_id=article['id'], rankme_hash=_hash(article))


def refresh_baseline(client, original):
    """Capture only a verified RankMe artifact, never adopt arbitrary/manual files."""
    connection = client.get('connection') or {}
    checked = validate_connection(connection)
    if not checked['ok']:
        raise PublishError(checked['message'])
    _validate_article(original)
    root = _project(connection)
    format = connection.get('format', 'md')
    raw_path = root / connection.get('content_dir', 'content/blog') / (original['slug'] + '.' + format)
    path = _inside(root, raw_path)
    if raw_path.is_symlink() or not path.is_file():
        raise PublishError('Refresh source must be an existing nonsymlink article.')
    if path.stat().st_size > 4000000:
        raise PublishError('Refresh source exceeds the size limit.')
    raw = path.read_bytes()
    text = raw.decode('utf-8')
    result = original.get('publish_result') or {}
    prior_baseline = original.get('refresh_baseline') or {}
    publication_id = result.get('publication_id') or prior_baseline.get('publication_id') or original['id']
    if _identity(text, format)[0] != publication_id:
        raise PublishError('Refresh source ownership does not match this article.')
    digest = hashlib.sha256(raw).hexdigest()
    if result.get('file_sha256'):
        if digest != result['file_sha256']:
            raise PublishError('Article has manual changes; review before refreshing it.')
    else:
        public = dict(original, id=publication_id)
        if original.get('cover'):
            public['cover'] = {key: original['cover'].get(key) for key in ('url', 'alt', 'sha256')}
            public['cover']['url'] = result.get('image_url') or public['cover'].get('url')
            if original['cover'].get('format'):
                public['cover']['format'] = original['cover']['format']
        if format == 'json':
            stored = json.loads(text)
            expected = _json_article(public)
            for key in ('created_at', 'published_at'):
                stored.pop(key, None)
                expected.pop(key, None)
            if stored != expected:
                raise PublishError('Article has manual changes; review before refreshing it.')
        elif _normalized_date(text) != _normalized_date(export_markdown(public)):
            raise PublishError('Article has manual changes; review before refreshing it.')
    date = original.get('published_at') or original.get('created_at') or ''
    created_at, published_at = original.get('created_at'), original.get('published_at')
    if format != 'json':
        match = re.search(r'^date: (.+)$', text.split('\n---\n', 1)[0], re.M)
        if match:
            date = json.loads(match.group(1))
    else:
        stored = json.loads(text)
        created_at, published_at = stored.get('created_at'), stored.get('published_at')
        date = published_at or created_at or ''
    return {'source_article_id': original['id'], 'publication_id': publication_id,
            'slug': original['slug'], 'file_sha256': digest,
            'connection_sha256': _connection_hash(connection), 'publication_date': date,
            'created_at': created_at, 'published_at': published_at}


def _prepare_cover(article, root, storage, connection, data_dir):
    cover = article.get('cover')
    if not cover:
        return None
    if not data_dir:
        raise PublishError('A configured data directory is required to publish a cover.')
    if not isinstance(cover, dict) or cover.get('review', {}).get('passed') is not True or cover.get('review', {}).get('issues'):
        raise PublishError('Cover requires a successful review before publication.')
    alt = cover.get('alt', '')
    if not isinstance(alt, str) or not alt.strip() or len(alt) > 1000:
        raise PublishError('Cover requires descriptive alternative text.')
    covers = _inside(storage, storage / 'covers')
    original = Path(cover.get('path', ''))
    if not original.is_absolute() or original.is_symlink():
        raise PublishError('Cover source must be an absolute, nonsymlink file in the covers directory.')
    source = _inside(covers, original)
    if not source.is_file() or source.stat().st_size > 20_000_000:
        raise PublishError('Cover source is missing or exceeds 20 MB.')
    data = source.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if cover.get('sha256') != digest:
        raise PublishError('Cover changed after review; generate or review it again.')
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        extension = 'png'
    elif data.startswith(b'\xff\xd8\xff'):
        extension = 'jpg'
    elif data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        extension = 'webp'
    else:
        raise PublishError('Cover must be a PNG, JPEG, or WebP image.')
    name = '%s-%s-%s.%s' % (article['slug'], hashlib.sha256(article['id'].encode()).hexdigest()[:10], digest[:16], extension)
    raw_target = root / (connection.get('image_dir') or 'public/images/articles') / name
    target = _inside(root, raw_target)
    if raw_target.is_symlink():
        raise PublishError('Cover target cannot be a symbolic link.')
    ownership = storage / 'publishing-assets' / (hashlib.sha256(str(target).encode()).hexdigest() + '.json')
    owner = {'article_id': article['id'], 'sha256': digest, 'path': str(target)}
    known = False
    if ownership.exists():
        try:
            known = json.loads(ownership.read_text()) == owner
        except (OSError, ValueError):
            pass
        if not known:
            raise PublishError('Cover ownership record does not match this article.')
    previous = target.read_bytes() if target.exists() else None
    if previous is not None and (not known or hashlib.sha256(previous).hexdigest() != digest):
        raise PublishError('An unrelated or modified file occupies the cover path.')
    url = (connection.get('image_url_prefix') or '/images/articles').rstrip('/') + '/' + name
    return {'path': target, 'data': data, 'previous': previous, 'ownership': ownership, 'owner': owner,
            'public': {'url': url, 'alt': alt.strip(), 'sha256': digest, 'format': extension}}


def verify_live(url, slug_or_title):
    if not url:
        return {'ok': False, 'status': 'pending', 'message': 'No public URL template is configured.'}
    try:
        from .crawler import fetch_public
        result = fetch_public(url)
        if isinstance(result, dict):
            body = result.get('text') or result.get('html') or result.get('body') or ''
            status = result.get('status', result.get('status_code', 200))
        else:
            body, status = str(result), 200
        visible = html.unescape(re.sub(r'<[^>]+>', ' ', body))
        visible = ' '.join(visible.split()).casefold()
        expected = ' '.join(slug_or_title.split()).casefold()
        ok = int(status) == 200 and bool(expected) and expected in visible
        return {'ok': ok, 'status': status, 'message': 'Live article verified.' if ok else 'Deployment is not yet verifiable at the public URL.'}
    except Exception as exc:
        return {'ok': False, 'status': 'pending', 'message': 'Live verification pending; the public page could not be fetched safely.'}


def publish_article(client, article, progress=None, data_dir=None):
    connection = client.get('connection') or {}
    check = validate_connection(connection)
    if not check['ok']:
        raise PublishError(check['message'])
    _validate_article(article)
    root = _project(connection)
    format = connection.get('format', 'md')
    baseline = None
    if article.get('refresh_of'):
        baseline = article.get('refresh_baseline')
        if (not isinstance(baseline, dict) or baseline.get('source_article_id') != article['refresh_of']
                or baseline.get('slug') != article['slug'] or not isinstance(baseline.get('publication_id'), str)
                or not baseline['publication_id'] or not re.fullmatch(r'[a-f0-9]{64}', str(baseline.get('file_sha256', '')))
                or baseline.get('connection_sha256') != _connection_hash(connection)):
            raise PublishError('Refresh baseline or publishing configuration changed; prepare a new refresh.')
        article = dict(article, id=baseline['publication_id'])
        if baseline.get('created_at'):
            article['created_at'] = baseline['created_at']
        if baseline.get('publication_date'):
            article['published_at'] = baseline['publication_date']
    if format == 'mdx' and (re.search(r'[{}<>]', article['body']) or re.search(r'^\s*(?:import|export)\s', article['body'], re.M)):
        raise PublishError('MDX articles cannot contain expressions, JSX, imports or exports. Use Markdown for code examples.')
    path = _inside(root, root / connection.get('content_dir', 'content/blog') / (article['slug'] + '.' + format))
    if path.is_symlink() or (root / connection.get('content_dir', 'content/blog') / (article['slug'] + '.' + format)).is_symlink():
        raise PublishError('Article targets cannot be symbolic links.')
    relative = str(path.relative_to(root))
    storage = Path(data_dir).expanduser().resolve() if data_dir else Path(__file__).resolve().parent.parent / 'data'
    asset = _prepare_cover(article, root, storage, connection, data_dir)
    if asset:
        article = dict(article, cover=asset['public'])
    asset_relative = str(asset['path'].relative_to(root)) if asset else None
    targets = [relative] + ([asset_relative] if asset else [])
    checkpoint_key = hashlib.sha256(json.dumps({'root': str(root), 'path': relative, 'article': _hash(article), 'mode': connection.get('mode', 'export'), 'branch': connection.get('branch'), 'remote': connection.get('remote', 'origin'), 'deploy': _argv(connection.get('deploy_command'))}, sort_keys=True).encode()).hexdigest()
    checkpoint_path = storage / 'publishing' / (checkpoint_key + '.json')
    checkpoint = {}
    if checkpoint_path.exists():
        try:
            checkpoint = json.loads(checkpoint_path.read_text(encoding='utf-8'))
        except (ValueError, OSError):
            raise PublishError('Publication checkpoint is unreadable; inspect it before retrying.')

    def save_checkpoint(**values):
        checkpoint.update(values)
        _atomic_write(checkpoint_path, json.dumps(checkpoint, sort_keys=True))

    previous = path.read_text(encoding='utf-8') if path.exists() else None
    identity = _identity(previous, format) if previous is not None else (None, None)
    if previous is not None and identity[0] != article['id']:
        raise PublishError('An unrelated file already occupies the article path.')
    same = identity == (article['id'], _hash(article))
    if baseline and not same:
        if previous is None or hashlib.sha256(path.read_bytes()).hexdigest() != baseline['file_sha256']:
            raise PublishError('Refresh source changed or disappeared after preparation; preserve the existing file.')
    if same and format == 'json' and _hash(json.loads(previous)) != _hash(article):
        raise PublishError('Article has manual changes; review before replacing it.')
    data = export_markdown(article)
    if baseline and same and format != 'json' and previous != data:
        raise PublishError('Refreshed article has manual changes; preserve the existing file.')
    if same and format != 'json':
        # Preserve original publication date across retries, but reject other edits.
        normalize = lambda text: re.sub(r'^date: .+$', 'date: <preserved>', text, count=1, flags=re.M)
        if normalize(previous) != normalize(data):
            raise PublishError('Article has manual changes; review before replacing it.')
    if format == 'json':
        data = json.dumps(_json_article(article), ensure_ascii=False, indent=2) + '\n'
        if baseline and same and json.loads(previous) != json.loads(data):
            raise PublishError('Refreshed article has manual changes; preserve the existing file.')
    git_mode = connection.get('mode') == 'git'
    commit = None
    recovering = False
    if git_mode:
        if _run(['git', 'branch', '--show-current'], root) != connection['branch']:
            raise PublishError('Current git branch does not match the configured branch.')
        dirty = _run(['git', 'status', '--porcelain', '-z', '--untracked-files=all'], root)
        if dirty:
            entries = [entry for entry in dirty.split('\0') if entry]
            matches = {relative: same}
            if asset:
                matches[asset_relative] = asset['previous'] == asset['data']
            recovering = bool(entries) and all(entry[:2] in ('??', 'A ', 'AM', ' M', 'M ', 'MM') and matches.get(entry[3:], False) for entry in entries)
            if not recovering:
                raise PublishError('Git publishing requires a clean repository; preserve or commit existing work first.')
    needs_write = not same or bool(asset and asset['previous'] is None)
    if needs_write or recovering or (not git_mode and connection.get('build_command')):
        try:
            if not same:
                if previous is not None:
                    backup = storage / 'revisions' / hashlib.sha256(str(path).encode()).hexdigest() / (hashlib.sha256(previous.encode()).hexdigest() + '.' + format)
                    _atomic_write(backup, previous)
                _atomic_write(path, data)
            if asset and asset['previous'] is None:
                # Record ownership before the atomic write so an interrupted copy can resume safely.
                _atomic_write(asset['ownership'], json.dumps(asset['owner'], sort_keys=True))
                _atomic_write(asset['path'], asset['data'])
            if progress:
                progress('Validating article build')
            build = _argv(connection.get('build_command'))
            if build:
                _run(build, root)
            expected_text = previous if same else data
            if path.read_text(encoding='utf-8') != expected_text or (asset and asset['path'].read_bytes() != asset['data']):
                raise PublishError('Build modified the generated article or cover; review the build command before retrying.')
            if git_mode:
                # Build scripts must not mutate tracked project files.
                changes = _run(['git', 'diff', '--name-only'], root).splitlines()
                if any(name not in targets for name in changes):
                    raise PublishError('Build changed other tracked files; review the repository before retrying.')
                _run(['git', 'add', '--'] + targets, root)
                _run(['git', 'commit', '-m', 'Publish article: ' + article['title'], '--'] + targets, root)
        except Exception:
            if git_mode:
                try:
                    _run(['git', 'restore', '--staged', '--'] + targets, root)
                except (PublishError, OSError):
                    pass
            if asset:
                if asset['previous'] is None:
                    asset['path'].unlink(missing_ok=True)
                else:
                    _atomic_write(asset['path'], asset['previous'])
            if previous is None:
                path.unlink(missing_ok=True)
            else:
                _atomic_write(path, previous)
            raise
    if git_mode:
        commit = _run(['git', 'rev-parse', 'HEAD'], root)
        if checkpoint.get('commit') and checkpoint['commit'] != commit:
            raise PublishError('Repository HEAD changed after publication began; inspect it before retrying deployment.')
        save_checkpoint(commit=commit)
    result = {'status': 'exported', 'path': str(path), 'live_url': None, 'commit': commit,
              'file_sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'publication_id': article['id'],
              'message': 'Article saved locally. Deployment is not enabled.'}
    if asset:
        result.update(image_path=str(asset['path']), image_url=asset['public']['url'])
    # auto_publish lets scheduled runs go live unattended; deploy_on_publish lets an
    # explicit Publish click push while scheduled runs still wait for approval.
    if not (connection.get('auto_publish') or connection.get('deploy_on_publish')):
        return result
    deploy = _argv(connection.get('deploy_command'))
    if git_mode and not checkpoint.get('pushed'):
        # Repeating an interrupted Git push is safe; the same commit is reused.
        _run(['git', 'push', connection.get('remote', 'origin'), 'HEAD:refs/heads/' + connection['branch']], root)
        save_checkpoint(pushed=True)
    if deploy and not checkpoint.get('deployed'):
        if checkpoint.get('deployment_started'):
            raise PublishError('Deployment outcome is unknown after an interruption. Verify externally before clearing its publication checkpoint.')
        save_checkpoint(deployment_started=True)
        try:
            _run(deploy, root)
        except Exception:
            # An explicit failure can be retried; configured commands must be idempotent.
            save_checkpoint(deployment_started=False, deployment_failed=True)
            raise
        save_checkpoint(deployed=True, deployment_started=False)
    if not git_mode and not deploy:
        return result
    url = connection.get('public_url_template', '').replace('{slug}', article['slug'])
    if connection.get('provider') == 'vercel':
        # The push only starts a Vercel build; the verify job follows that deployment.
        result.update(status='verification_pending', live_url=url or None,
                      message='Pushed to GitHub. Vercel is building the site; RankMe checks the live page when the build finishes.')
        return result
    verification = verify_live(url, article['title'])
    result.update(status='published' if verification['ok'] else 'verification_pending', live_url=url or None, message=verification['message'])
    return result
