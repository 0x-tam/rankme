"""Optional ChatSEO OAuth/MCP provider; private credentials never enter the Store.

Discovery verified 2026-10-14 against chatseo.app/llms-full.txt and the public
OpenID configuration. Only documented, fixed ChatSEO endpoints are contacted.
"""
import base64
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
import threading
import time
import uuid
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, HTTPRedirectHandler, build_opener

from .store import now, uid

MCP = 'https://api.chatseo.app/mcp'
AUTH = 'https://api.chatseo.app/auth/oauth2/authorize'
TOKEN = 'https://api.chatseo.app/auth/oauth2/token'
REGISTER = 'https://api.chatseo.app/auth/oauth2/register'
ENDPOINTS = {MCP, TOKEN, REGISTER}
READ_TOOLS = {'list_sites', 'get_credits', 'list_site_actions', 'list_conversations',
              'get_conversation_messages', 'list_artifacts', 'get_artifact'}
TOOLS = READ_TOOLS | {'send_message'}
ID = re.compile(r'[A-Za-z0-9_-]{1,200}')
MAX_RESPONSE = 2_000_000


class ChatSEOError(ValueError):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _http(url, payload, token='', form=False, session=''):
    if url not in ENDPOINTS:
        raise ChatSEOError('Untrusted ChatSEO endpoint.')
    headers = {'Accept': 'application/json, text/event-stream', 'Content-Type':
               'application/x-www-form-urlencoded' if form else 'application/json'}
    for key, value in (('Authorization', 'Bearer ' + token if token else ''), ('Mcp-Session-Id', session)):
        if value:
            if not isinstance(value, str) or len(value) > 16000 or any(ord(c) < 32 or ord(c) == 127 for c in value):
                raise ChatSEOError('Invalid ChatSEO connection. Reconnect.')
            headers[key] = value
    if url == MCP:
        headers['MCP-Protocol-Version'] = '2025-06-18'
    body = (urlencode(payload) if form else json.dumps(payload)).encode()
    try:
        with build_opener(_NoRedirect()).open(Request(url, data=body, headers=headers), timeout=30) as response:
            deadline, chunks, size = time.monotonic() + 35, [], 0
            while True:
                if time.monotonic() > deadline:
                    raise ChatSEOError('ChatSEO response exceeded the time limit. Submitted work may still be running.')
                chunk = response.read1(min(65536, MAX_RESPONSE + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > MAX_RESPONSE:
                    raise ChatSEOError('ChatSEO response exceeded the size limit.')
            raw = b''.join(chunks)
            if len(raw) > MAX_RESPONSE:
                raise ChatSEOError('ChatSEO response exceeded the size limit.')
            text = raw.decode('utf-8')
            if 'text/event-stream' in response.headers.get('Content-Type', ''):
                # Bounded stream: pick only the JSON-RPC response for this request.
                values = [json.loads(line[5:].strip()) for line in text.splitlines() if line.startswith('data:') and line[5:].strip()]
                result = next((v for v in reversed(values) if isinstance(v, dict) and v.get('id') == payload.get('id')), {})
            else:
                result = json.loads(text) if text else {}
            if not isinstance(result, dict):
                raise ChatSEOError('ChatSEO returned an unexpected response.')
            return result, response.headers.get('Mcp-Session-Id', '')
    except HTTPError as exc:
        if exc.code in (401, 403):
            raise ChatSEOError('ChatSEO access was denied. Reconnect and check your plan and organization.') from None
        raise ChatSEOError('ChatSEO request failed. The provider may still have accepted submitted work.') from None
    except (URLError, OSError, TimeoutError, UnicodeError, json.JSONDecodeError):
        raise ChatSEOError('ChatSEO could not be reached or returned an unreadable response. Submitted work may still be running.') from None


def _text(value, limit=2000):
    return value[:limit] if isinstance(value, str) else ''


def _ident(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise ChatSEOError('Invalid ChatSEO identifier.')
    return value


def _result(value):
    if value.get('isError'):
        raise ChatSEOError('ChatSEO tool is unavailable or rejected this request. Check your organization and plan.')
    if isinstance(value.get('structuredContent'), dict):
        return value['structuredContent']
    parts = value.get('content', [])
    texts = [p.get('text', '') for p in parts if isinstance(p, dict) and p.get('type') == 'text']
    for text in texts:
        try:
            decoded = json.loads(text)
            if isinstance(decoded, (dict, list)):
                return decoded
        except (ValueError, TypeError):
            pass
    return {'text': '\n'.join(texts)[:100000]}


def _rows(value, key):
    if isinstance(value, list):
        return [x for x in value[:100] if isinstance(x, dict)]
    if isinstance(value, dict):
        for name in (key, 'items', 'data'):
            if isinstance(value.get(name), list):
                return [x for x in value[name][:100] if isinstance(x, dict)]
            if isinstance(value.get(name), dict):
                return _rows(value[name], key)
    return []


def _remaining(value):
    if not isinstance(value, dict):
        return None
    components = ('subscriptionCreditsRemaining', 'topUpCreditsRemaining', 'bonusCreditsRemaining')
    if any(key in value for key in components):
        numbers = [value.get(key, 0) for key in components]
        if all(type(n) in (int, float) and math.isfinite(n) and n >= 0 for n in numbers):
            return sum(numbers)
        return None
    for key in ('remaining', 'remainingCredits', 'credits', 'balance', 'creditsRemaining'):
        number = value.get(key)
        if type(number) in (int, float) and math.isfinite(number) and number >= 0:
            return number
        if isinstance(number, dict):
            found = _remaining(number)
            if found is not None:
                return found
    return None


class ChatSEO:
    def __init__(self, data_dir, store):
        self.root, self.store = Path(data_dir), store
        self.path = self.root / 'chatseo-secrets.json'
        self.lock = threading.RLock()
        self.tools = {}
        self.session = ''
        self.initialized = False
        self.sequence = 0
        self.pending = None
        # A process crash must never repeat a request whose acceptance is unknown.
        for run in store.all('chatseo_runs'):
            if run.get('status') in ('pending', 'sending'):
                store.update('chatseo_runs', run['id'], status='uncertain',
                    error='RankMe stopped during submission. Check ChatSEO before making another request; this request will not be sent again.')

    def _read(self):
        try:
            descriptor = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return {}
        except OSError:
            raise ChatSEOError('ChatSEO credentials must be a private regular file.') from None
        try:
            with os.fdopen(descriptor, 'r') as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > 100000:
                    raise ChatSEOError('ChatSEO credentials must be an owner-only regular file.')
                result = json.load(stream)
                if not isinstance(result, dict):
                    raise ValueError()
                return result
        except (OSError, ValueError):
            raise ChatSEOError('ChatSEO credentials could not be read securely. Reconnect.') from None

    def _save(self, data):
        self._read()  # Reject existing symlinks, hardlinks and unsafe permissions.
        with tempfile.NamedTemporaryFile('w', dir=self.root, prefix='.chatseo-', delete=False) as stream:
            filename = stream.name
            os.fchmod(stream.fileno(), 0o600)
            json.dump(data, stream)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.replace(filename, self.path)
        finally:
            if os.path.exists(filename):
                os.unlink(filename)

    def status(self):
        try:
            connected = bool(self._read().get('access_token'))
            error = ''
        except ChatSEOError as exc:
            connected, error = False, str(exc)
        cached = self.store.settings().get('chatseo_cache', {})
        return {'connected': connected, 'connection_required': not connected,
            'local_connection_only': True, 'capabilities': cached.get('capabilities', []) if connected else [],
            'sites': cached.get('sites', []) if connected else [], 'credits': cached.get('credits', {'remaining': None}) if connected else {'remaining': None},
            'last_sync': cached.get('last_sync', ''), 'error': error or cached.get('error', '')}

    def snapshot(self, client_id=None):
        """Bound provider contribution to the shared/cloud snapshot, preserving full local records."""
        clients = {c['id']: c.get('chatseo_site_id') for c in self.store.all('clients')}
        reports, runs, used = [], [], 0
        for table, target in (('chatseo_runs', runs), ('chatseo_reports', reports)):
            rows = sorted(self.store.all(table), key=lambda r: r.get('created_at', ''), reverse=True)
            for row in rows:
                if client_id and row['client_id'] != client_id:
                    continue
                if row.get('site_id') != clients.get(row['client_id']):
                    continue
                if len(target) >= 20:
                    break
                projected = dict(row)
                if table == 'chatseo_runs':
                    projected['reply'] = _text(row.get('reply'), 4000)
                    projected['reply_truncated'] = len(row.get('reply', '')) > 4000
                    projected['artifacts'] = row.get('artifacts', [])[:20]
                else:
                    projected['actions'] = [{**a, 'description': _text(a.get('description'), 500)} for a in row.get('actions', [])[:20]]
                    projected['conversations'] = row.get('conversations', [])[:20]
                    projected['artifacts'] = row.get('artifacts', [])[:20]
                size = len(json.dumps(projected).encode())
                if used + size > 450000:
                    continue
                target.append(projected)
                used += size
        return {'chatseo': self.status(), 'chatseo_reports': reports, 'chatseo_runs': runs}

    def begin(self, redirect):
        parsed = urlsplit(redirect)
        if parsed.scheme != 'http' or parsed.hostname != 'localhost' or not parsed.port or parsed.path != '/api/chatseo/callback' or parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise ChatSEOError('ChatSEO must be connected in RankMe on the worker Mac.')
        with self.lock:
            saved = self._read()
            if not saved.get('client_id') or saved.get('redirect_uri') != redirect:
                registration, _ = _http(REGISTER, {'client_name': 'RankMe', 'redirect_uris': [redirect],
                    'grant_types': ['authorization_code', 'refresh_token'], 'response_types': ['code'],
                    'token_endpoint_auth_method': 'none'})
                if not isinstance(registration.get('client_id'), str) or not registration['client_id']:
                    raise ChatSEOError('ChatSEO did not register this application.')
                saved = {'client_id': registration['client_id'], 'redirect_uri': redirect}
                self._save(saved)
            state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
            self.pending = {'state': state, 'verifier': verifier, 'expires': time.time() + 600, 'redirect': redirect}
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
            return AUTH + '?' + urlencode({'client_id': saved['client_id'], 'redirect_uri': redirect,
                'response_type': 'code', 'scope': 'offline_access', 'resource': MCP,
                'state': state, 'code_challenge': challenge, 'code_challenge_method': 'S256'})

    def callback(self, code, state, redirect):
        with self.lock:
            pending = self.pending
            if not pending or not isinstance(state, str) or not secrets.compare_digest(pending['state'], state) or time.time() > pending['expires'] or redirect != pending['redirect']:
                raise ChatSEOError('ChatSEO sign-in expired or did not match this session.')
            self.pending = None
            if not isinstance(code, str) or not 1 <= len(code) <= 8192:
                raise ChatSEOError('ChatSEO authorization code is missing.')
            saved = self._read()
            tokens, _ = _http(TOKEN, {'grant_type': 'authorization_code', 'code': code,
                'client_id': saved['client_id'], 'redirect_uri': redirect,
                'code_verifier': pending['verifier'], 'resource': MCP}, form=True)
            saved.pop('refresh_token', None)
            self._save_tokens(saved, tokens)
            self.initialized, self.session, self.tools = False, '', {}
            self.store.settings({'chatseo_cache': {}})
            return self.status()

    def _save_tokens(self, saved, tokens):
        access = tokens.get('access_token')
        if not isinstance(access, str) or not access or len(access) > 16000 or any(ord(c) < 33 for c in access):
            raise ChatSEOError('ChatSEO did not return a valid access grant.')
        expires = tokens.get('expires_in', 3600)
        if type(expires) not in (int, float) or not math.isfinite(expires) or expires < 0:
            raise ChatSEOError('ChatSEO returned an invalid access expiry.')
        saved.update(access_token=access, expires_at=time.time() + min(expires, 86400))
        if isinstance(tokens.get('refresh_token'), str):
            saved['refresh_token'] = tokens['refresh_token']
        self._save(saved)

    def _token(self):
        saved = self._read()
        if not saved.get('access_token'):
            raise ChatSEOError('Connect ChatSEO on the worker Mac first.')
        if saved.get('expires_at', 0) < time.time() + 60:
            if not saved.get('refresh_token'):
                raise ChatSEOError('ChatSEO access expired. Reconnect on the worker Mac.')
            tokens, _ = _http(TOKEN, {'grant_type': 'refresh_token', 'refresh_token': saved['refresh_token'],
                'client_id': saved['client_id'], 'resource': MCP}, form=True)
            self._save_tokens(saved, tokens)
            return tokens['access_token']
        return saved['access_token']

    def disconnect(self):
        with self.lock:
            self._read()
            self.path.unlink(missing_ok=True)
            self.pending, self.tools, self.initialized, self.session = None, {}, False, ''
            self.store.settings({'chatseo_cache': {}})
            return self.status()

    def _rpc(self, method, params=None, notification=False):
        self.sequence += 1
        payload = {'jsonrpc': '2.0', 'method': method}
        if not notification:
            payload['id'] = self.sequence
        if params is not None:
            payload['params'] = params
        response, session = _http(MCP, payload, token=self._token(), session=self.session)
        if session:
            self.session = session
        if notification:
            return {}
        if response.get('id') != self.sequence or 'error' in response or not isinstance(response.get('result'), dict):
            raise ChatSEOError('ChatSEO protocol request was rejected or unsupported.')
        return response['result']

    def discover(self):
        with self.lock:
            if not self.initialized:
                self._rpc('initialize', {'protocolVersion': '2025-06-18', 'capabilities': {},
                    'clientInfo': {'name': 'RankMe', 'version': '1.0.0'}})
                self._rpc('notifications/initialized', notification=True)
                self.initialized = True
            found, cursor = {}, None
            for _ in range(5):
                result = self._rpc('tools/list', {'cursor': cursor} if cursor else {})
                for tool in result.get('tools', []):
                    if isinstance(tool, dict) and tool.get('name') in TOOLS:
                        found[tool['name']] = tool
                cursor = result.get('nextCursor')
                if not cursor:
                    break
            self.tools = found
            return sorted(found)

    def call(self, name, arguments):
        with self.lock:
            if not self.tools:
                self.discover()
            if name not in TOOLS or name not in self.tools:
                raise ChatSEOError('This ChatSEO capability is unavailable for this connection.')
            schema = self.tools[name].get('inputSchema') or {}
            props = schema.get('properties', {})
            if any(key not in props for key in arguments) or any(key not in arguments for key in schema.get('required', [])):
                raise ChatSEOError('ChatSEO changed its tool contract. This operation is unavailable until supported.')
            return _result(self._rpc('tools/call', {'name': name, 'arguments': arguments}))

    def sync(self):
        with self.lock:
            capabilities = self.discover()
            sites = []
            for row in _rows(self.call('list_sites', {}), 'sites'):
                if ID.fullmatch(str(row.get('id', ''))):
                    sites.append({'id': row['id'], 'domain': _text(row.get('domain', row.get('url', '')), 500),
                                  'name': _text(row.get('name', row.get('domain', '')), 200)})
            credits = self.call('get_credits', {})
            self.store.settings({'chatseo_cache': {'sites': sites, 'credits': {'remaining': _remaining(credits)},
                'capabilities': capabilities, 'last_sync': now(), 'error': ''}})
            return self.status()

    def mapped(self, client_id, refresh=False):
        client = self.store.get('clients', client_id)
        site = client.get('chatseo_site_id', '')
        state = self.sync() if refresh else self.status()
        if not state['connected'] or not site or site not in {s['id'] for s in state['sites']}:
            raise ChatSEOError('Connect ChatSEO and choose an accessible site first.')
        return client, site

    def map_site(self, client_id, site_id):
        with self.lock:
            _ident(site_id)
            state = self.sync()
            if site_id not in {s['id'] for s in state['sites']}:
                raise ChatSEOError('This site does not belong to the connected ChatSEO account.')
            self.store.get('clients', client_id)
            if any(r['client_id'] == client_id and r['status'] in ('pending', 'sending', 'processing', 'uncertain') for r in self.store.all('chatseo_runs')):
                raise ChatSEOError('Resolve the current ChatSEO request before changing its site.')
            return self.store.update('clients', client_id, chatseo_site_id=site_id)

    def settings(self, client_id, data):
        if set(data) != {'enabled', 'daily_request_limit', 'minimum_credits'} or type(data['enabled']) is not bool or type(data['daily_request_limit']) is not int or not 1 <= data['daily_request_limit'] <= 10 or type(data['minimum_credits']) is not int or not 1 <= data['minimum_credits'] <= 100000:
            raise ChatSEOError('Choose enabled, 1–10 requests per day, and a minimum credit balance of 1–100000.')
        return self.store.update('clients', client_id, chatseo_settings=data)

    def sync_client(self, client_id):
        with self.lock:
            _, site = self.mapped(client_id, refresh=True)
            result = {'id': client_id, 'client_id': client_id, 'site_id': site, 'source': 'chatseo',
                      'actions': [], 'conversations': [], 'artifacts': [], 'unavailable': [], 'synced_at': now(), 'error': ''}
            for tool, key in (('list_site_actions', 'actions'), ('list_conversations', 'conversations')):
                schema = self.tools.get(tool, {}).get('inputSchema', {})
                # Never display organization-wide conversations as belonging to this site.
                supports_site = 'siteId' in schema.get('properties', {})
                if tool not in self.tools or not supports_site:
                    result['unavailable'].append(tool)
                    continue
                try:
                    rows = _rows(self.call(tool, {'siteId': site}), key)
                    for row in rows:
                        if row.get('siteId', row.get('site_id', site)) != site:
                            raise ChatSEOError('ChatSEO returned data for a different site.')
                        if not ID.fullmatch(str(row.get('id', ''))):
                            continue
                        item = {'id': row['id'], 'title': _text(row.get('title', row.get('name', '')), 500)}
                        if key == 'actions':
                            item.update(description=_text(row.get('description', '')), status=_text(row.get('status', ''), 50), source='chatseo')
                        result[key].append(item)
                except ChatSEOError:
                    result[key] = []
                    result['unavailable'].append(tool)
            return self.store.put('chatseo_reports', result)

    def send(self, client_id, data):
        if set(data) != {'message', 'credit_acknowledged', 'request_id'} or data.get('credit_acknowledged') is not True or not isinstance(data.get('message'), str) or not 1 <= len(data['message'].strip()) <= 8000 or len(data['message']) > 8000:
            raise ChatSEOError('Enter a request and explicitly acknowledge its unknown credit cost.')
        try:
            request_id = str(uuid.UUID(data['request_id']))
        except (ValueError, TypeError, AttributeError):
            raise ChatSEOError('A unique request ID is required.') from None
        with self.lock:
            for run in self.store.all('chatseo_runs'):
                if run.get('request_id') == request_id:
                    if run['client_id'] != client_id or run['message'] != data['message']:
                        raise ChatSEOError('This request ID was already used for different work.')
                    return run
            client, site = self.mapped(client_id, refresh=True)
            config = client.get('chatseo_settings', {})
            if config.get('enabled') is not True:
                raise ChatSEOError('Enable paid ChatSEO requests for this website first.')
            if 'send_message' not in self.tools or 'get_conversation_messages' not in self.tools:
                raise ChatSEOError('ChatSEO generation and recovery capabilities are unavailable.')
            previous = [r for r in self.store.all('chatseo_runs') if r['client_id'] == client_id]
            if any(r['status'] in ('pending', 'sending', 'processing', 'uncertain') for r in previous):
                raise ChatSEOError('Resolve the existing ChatSEO request before starting another.')
            if sum(r['created_at'][:10] == now()[:10] for r in previous) >= config.get('daily_request_limit', 1):
                raise ChatSEOError('This website reached its daily ChatSEO request limit (UTC).')
            balance = self.status()['credits']['remaining']
            if balance is None or balance < config.get('minimum_credits', 1):
                raise ChatSEOError('ChatSEO credit balance is unknown or below your minimum.')
            run = self.store.put('chatseo_runs', {'id': uid(), 'client_id': client_id, 'site_id': site,
                'request_id': request_id, 'message': data['message'], 'status': 'pending', 'conversation_id': '',
                'reply': '', 'artifacts': [], 'error': ''})
            threading.Thread(target=self._submit, args=(run['id'],), daemon=True, name='rankme-chatseo-submit').start()
            return run

    def _submit(self, run_id):
        with self.lock:
            run = self.store.get('chatseo_runs', run_id)
            if run['status'] != 'pending':
                return
            self.store.update('chatseo_runs', run_id, status='sending')
            try:
                # Exact owner-written text; never add hidden instructions or a rewrite.
                result = self.call('send_message', {'message': run['message'], 'siteId': run['site_id']})
                conversation = result.get('conversationId') if isinstance(result, dict) else None
                _ident(conversation)
                self.store.update('chatseo_runs', run_id, status='processing', conversation_id=conversation)
            except Exception:
                self.store.update('chatseo_runs', run_id, status='uncertain',
                    error='Submission outcome is unknown. Check ChatSEO directly; RankMe will never automatically send this request again.')

    def _run(self, client_id, run_id):
        run = self.store.get('chatseo_runs', run_id)
        _, site = self.mapped(client_id)
        if run['client_id'] != client_id or run['site_id'] != site:
            raise ChatSEOError('This request does not belong to the selected website and site.')
        return run

    def poll(self, client_id, run_id):
        with self.lock:
            run = self._run(client_id, run_id)
            if not run.get('conversation_id') or run['status'] not in ('processing', 'complete'):
                raise ChatSEOError('There is no recoverable conversation ID. Check ChatSEO directly.')
            if time.time() - run.get('last_poll_time', 0) < 10:
                return run
            self.store.update('chatseo_runs', run_id, last_poll_time=time.time())
            self.mapped(client_id, refresh=True)
            result = self.call('get_conversation_messages', {'conversationId': run['conversation_id']})
            if not isinstance(result, dict):
                raise ChatSEOError('ChatSEO returned an unsupported conversation response.')
            status = result.get('status')
            if status not in ('processing', 'complete', 'failed', 'error'):
                raise ChatSEOError('ChatSEO returned an unknown conversation status. Check the provider directly.')
            messages = _rows(result, 'messages')
            reply = '\n\n'.join(_text(m.get('content', m.get('text', '')), 100000) for m in messages if m.get('role') == 'assistant')[:100000]
            updates = {'reply': reply, 'error': '', 'status': 'complete' if status == 'complete' else 'processing'}
            if status in ('failed', 'error'):
                updates.update(status='failed', error='ChatSEO reported that this request failed. No automatic retry was attempted.')
            artifacts = []
            if status == 'complete' and 'list_artifacts' in self.tools:
                for item in _rows(self.call('list_artifacts', {'conversationId': run['conversation_id']}), 'artifacts'):
                    if ID.fullmatch(str(item.get('id', ''))):
                        artifacts.append({'id': item['id'], 'title': _text(item.get('title', ''), 500),
                                          'type': _text(item.get('type', item.get('kind', '')), 100)})
            updates['artifacts'] = artifacts
            return self.store.update('chatseo_runs', run_id, **updates)

    def read_run(self, client_id, run_id):
        """Read a stored response without credentials or provider network access."""
        run = self.store.get('chatseo_runs', _ident(run_id))
        client = self.store.get('clients', client_id)
        if run['client_id'] != client_id or run['site_id'] != client.get('chatseo_site_id'):
            raise ChatSEOError('This request does not belong to the selected website and site.')
        return run

    def resolve(self, client_id, run_id, checked_provider):
        with self.lock:
            run = self._run(client_id, run_id)
            if checked_provider is not True or run['status'] != 'uncertain':
                raise ChatSEOError('Check the uncertain request in ChatSEO before acknowledging it.')
            return self.store.update('chatseo_runs', run_id, status='acknowledged',
                error='Owner checked ChatSEO and acknowledged this uncertain request. It was not resent.')

    def import_artifact(self, client_id, run_id, artifact_id):
        with self.lock:
            run = self._run(client_id, run_id)
            _ident(artifact_id)
            if run['status'] != 'complete' or artifact_id not in {a['id'] for a in run.get('artifacts', [])}:
                raise ChatSEOError('Choose an artifact from this website’s completed ChatSEO request.')
            self.mapped(client_id, refresh=True)
            # Refresh membership using the known conversation. Never trust browser-supplied content.
            rows = _rows(self.call('list_artifacts', {'conversationId': run['conversation_id']}), 'artifacts')
            if artifact_id not in {str(a.get('id')) for a in rows}:
                raise ChatSEOError('This artifact does not belong to the recorded conversation.')
            result = self.call('get_artifact', {'id': artifact_id})
            if not isinstance(result, dict):
                raise ChatSEOError('Unsupported artifact response.')
            item = result.get('artifact', result)
            if not isinstance(item, dict):
                raise ChatSEOError('Unsupported artifact response.')
            if item.get('id', artifact_id) != artifact_id or item.get('conversationId', item.get('conversation_id', run['conversation_id'])) != run['conversation_id']:
                raise ChatSEOError('Artifact conversation mismatch.')
            body = _text(item.get('content', ''), 100000)
            if not body.strip():
                raise ChatSEOError('This artifact does not contain text that can be imported as a draft.')
            for article in self.store.all('articles'):
                if article.get('chatseo_artifact_id') == artifact_id and article['client_id'] == client_id:
                    return article
            title = _text(item.get('title', ''), 200) or 'ChatSEO draft'
            slug = re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')[:80] or 'chatseo-draft'
            return self.store.put('articles', {'client_id': client_id, 'title': title, 'slug': slug + '-' + uid()[:8],
                'description': '', 'body': body, 'status': 'held', 'scheduled_at': now(), 'sources': [], 'claims': [],
                'review': {'passed': False, 'issues': ['Imported from ChatSEO. Review factual claims and sources before publishing.']},
                'source': 'chatseo', 'chatseo_artifact_id': artifact_id, 'chatseo_run_id': run_id,
                'error': 'Imported as an unreviewed draft. Run review before publishing.'})
