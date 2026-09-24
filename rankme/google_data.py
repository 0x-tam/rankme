"""Read-only Google Search Console and GA4 integration for the local desktop app.

OAuth: https://developers.google.com/identity/protocols/oauth2/native-app
Search: https://developers.google.com/webmaster-tools/v1/searchanalytics/query
GA4: https://developers.google.com/analytics/devguides/reporting/data/v1/basics
"""
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, HTTPRedirectHandler, build_opener

SEARCH_SCOPE = 'https://www.googleapis.com/auth/webmasters.readonly'
ANALYTICS_SCOPE = 'https://www.googleapis.com/auth/analytics.readonly'
SCOPES = (SEARCH_SCOPE, ANALYTICS_SCOPE)
TOKEN_URL = 'https://oauth2.googleapis.com/token'
AUTH_URL = 'https://accounts.google.com/o/oauth2/v2/auth'
GOOGLE_HOSTS = {'oauth2.googleapis.com', 'www.googleapis.com', 'analyticsadmin.googleapis.com', 'analyticsdata.googleapis.com'}
_LOCKS = {}
_LOCKS_GUARD = threading.Lock()


class GoogleDataError(RuntimeError):
    def __init__(self, message, retryable=False, status=0):
        super().__init__(message)
        self.retryable = retryable
        self.status = status


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _http(url, method='GET', payload=None, token='', form=False):
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or parsed.hostname not in GOOGLE_HOSTS or parsed.username or parsed.password:
        raise GoogleDataError('Invalid Google service endpoint.')
    headers = {'Accept': 'application/json'}
    data = None
    if payload is not None:
        data = (urlencode(payload) if form else json.dumps(payload)).encode('utf-8')
        headers['Content-Type'] = 'application/x-www-form-urlencoded' if form else 'application/json'
    if token:
        headers['Authorization'] = 'Bearer ' + token
    try:
        with build_opener(_NoRedirect()).open(Request(url, data=data, headers=headers, method=method), timeout=20) as response:
            raw = response.read(4000001)
            if len(raw) > 4000000:
                raise GoogleDataError('Google response exceeded the local size limit.')
            result = json.loads(raw.decode('utf-8')) if raw else {}
            if not isinstance(result, dict):
                raise GoogleDataError('Google returned an unexpected response.')
            return result
    except HTTPError as exc:
        # Never include response bodies, tokens, request URLs, or authorization codes in errors.
        code = exc.code
        if code in (400, 401):
            raise GoogleDataError('Google authorization or request was rejected. Reconnect if access expired.', status=code)
        if code == 403:
            raise GoogleDataError('Google denied access. Check granted scopes, property access, and enabled Google APIs.', status=code)
        if code == 404:
            raise GoogleDataError('Google property was not found or is not accessible.', status=code)
        raise GoogleDataError('Google service is temporarily unavailable. Try again later.', retryable=code == 429 or code >= 500, status=code)
    except (URLError, OSError, TimeoutError):
        raise GoogleDataError('Could not reach Google. Check the internet connection and retry.', retryable=True)
    except (UnicodeError, ValueError):
        raise GoogleDataError('Google returned an unreadable response.', retryable=True)


def _redirect(uri):
    p = urlsplit(str(uri))
    try:
        port = p.port
    except ValueError:
        port = None
    if p.scheme != 'http' or p.hostname not in ('127.0.0.1', '::1', 'localhost') or not port or p.username or p.password or p.query or p.fragment:
        raise GoogleDataError('Google sign-in requires an HTTP loopback callback URL with a port.')
    if any(ord(c) < 32 for c in str(uri)):
        raise GoogleDataError('Invalid Google callback URL.')
    return str(uri)


def _number(value):
    try:
        result = float(value)
        return result if result == result and abs(result) != float('inf') else 0.0
    except (TypeError, ValueError):
        return 0.0


class GoogleData:
    def __init__(self, data_dir):
        self.root = Path(data_dir).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / 'google-secrets.json'
        with _LOCKS_GUARD:
            self.lock = _LOCKS.setdefault(str(self.path), threading.RLock())

    def _read(self):
        if self.path.is_symlink():
            raise GoogleDataError('Google credentials file must not be a symbolic link.')
        if not self.path.exists():
            return {}
        try:
            if self.path.stat().st_size > 100000:
                raise GoogleDataError('Google credentials file is too large.')
            os.chmod(self.path, 0o600)
            data = json.loads(self.path.read_text(encoding='utf-8'))
            if not isinstance(data, dict):
                raise ValueError()
            if any(key in data and not isinstance(data[key], str) for key in ('client_id', 'client_secret', 'access_token', 'refresh_token', 'scope')):
                raise ValueError()
            if 'expires_at' in data and not isinstance(data['expires_at'], (int, float)):
                raise ValueError()
            if 'pending' in data and not isinstance(data['pending'], dict):
                raise ValueError()
            return data
        except (OSError, ValueError):
            raise GoogleDataError('Google credentials could not be read. Reconfigure the Google connection.')

    def _save(self, value):
        if self.path.is_symlink():
            raise GoogleDataError('Google credentials file must not be a symbolic link.')
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=str(self.root), prefix='.google-', delete=False) as handle:
            name = handle.name
            os.fchmod(handle.fileno(), 0o600)
            json.dump(value, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, self.path)
        os.chmod(self.path, 0o600)

    def configure(self, client_id, client_secret=''):
        client_id = str(client_id).strip()
        if not re.fullmatch(r'[A-Za-z0-9_-]+\.apps\.googleusercontent\.com', client_id):
            raise GoogleDataError('Enter a valid Google Desktop OAuth client ID.')
        if len(str(client_secret)) > 3000 or any(ord(c) < 32 for c in str(client_secret)):
            raise GoogleDataError('Invalid Google OAuth client secret.')
        with self.lock:
            try:
                data = self._read()
            except GoogleDataError:
                data = {}  # Explicit reconfiguration can repair malformed local credentials.
            if data.get('client_id') != client_id:
                data = {}
            data.update(client_id=client_id, client_secret=str(client_secret).strip())
            data.pop('pending', None)
            self._save(data)
            return self.status()

    def status(self):
        with self.lock:
            try:
                data = self._read()
            except GoogleDataError:
                return {'configured': False, 'connected': False, 'search_console': False, 'analytics': False,
                        'message': 'Google credentials could not be read. Reconfigure the Google connection.'}
            connected = bool(data.get('refresh_token') or data.get('access_token') and data.get('expires_at', 0) > time.time())
            scopes = set(str(data.get('scope', '')).split())
            message = 'Google is connected with read-only access.' if connected else 'Connect your Google account.'
            if not data.get('client_id'):
                message = 'Configure a Google Desktop OAuth client first.'
            elif connected and not data.get('refresh_token'):
                message = 'Connected for this session. Reconnect to enable offline refresh.'
            return {'configured': bool(data.get('client_id')), 'connected': connected,
                    'search_console': connected and SEARCH_SCOPE in scopes,
                    'analytics': connected and ANALYTICS_SCOPE in scopes, 'message': message}

    def begin(self, redirect_uri):
        redirect_uri = _redirect(redirect_uri)
        with self.lock:
            data = self._read()
            if not data.get('client_id'):
                raise GoogleDataError('Configure a Google Desktop OAuth client first.')
            verifier = secrets.token_urlsafe(64)
            state = secrets.token_urlsafe(32)
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode('ascii')).digest()).decode('ascii').rstrip('=')
            data['pending'] = {'state': state, 'verifier': verifier, 'redirect_uri': redirect_uri, 'created_at': time.time()}
            self._save(data)
            return AUTH_URL + '?' + urlencode({'client_id': data['client_id'], 'redirect_uri': redirect_uri,
                'response_type': 'code', 'scope': ' '.join(SCOPES), 'state': state, 'code_challenge': challenge,
                'code_challenge_method': 'S256', 'access_type': 'offline', 'prompt': 'consent'})

    def callback(self, code, state, redirect_uri):
        redirect_uri = _redirect(redirect_uri)
        with self.lock:
            data = self._read()
            pending = data.get('pending', {})
            if not pending or not secrets.compare_digest(str(state).encode('utf-8'), str(pending.get('state', '')).encode('utf-8')) or pending.get('redirect_uri') != redirect_uri:
                raise GoogleDataError('Google sign-in state did not match. Start sign-in again.')
            data.pop('pending', None)
            self._save(data)  # Consume the state before exchanging the one-time code.
            if time.time() - _number(pending.get('created_at', 0)) > 600:
                raise GoogleDataError('Google sign-in expired. Start sign-in again.')
            if not code or len(str(code)) > 10000:
                raise GoogleDataError('Google did not provide a valid authorization code.')
            payload = {'client_id': data['client_id'], 'code': str(code), 'code_verifier': pending['verifier'],
                       'redirect_uri': redirect_uri, 'grant_type': 'authorization_code'}
            if data.get('client_secret'):
                payload['client_secret'] = data['client_secret']
            tokens = _http(TOKEN_URL, 'POST', payload, form=True)
            self._store_tokens(data, tokens, initial=True)
            return self.status()

    def _store_tokens(self, data, tokens, initial=False):
        if not isinstance(tokens.get('access_token'), str) or not tokens['access_token']:
            raise GoogleDataError('Google did not return an access token. Reconnect.')
        data['access_token'] = tokens['access_token']
        data['expires_at'] = time.time() + max(0, min(86400, _number(tokens.get('expires_in', 3600))))
        if tokens.get('refresh_token'):
            data['refresh_token'] = tokens['refresh_token']
        elif initial:
            data.pop('refresh_token', None)  # Never mix tokens from two different accounts.
        data['scope'] = tokens.get('scope', ' '.join(SCOPES) if initial else data.get('scope', ''))
        self._save(data)

    def _token(self, scope):
        with self.lock:
            data = self._read()
            if scope not in set(str(data.get('scope', '')).split()):
                raise GoogleDataError('Required Google read-only permission was not granted. Reconnect and grant access.')
            if data.get('access_token') and data.get('expires_at', 0) > time.time() + 60:
                return data['access_token']
            if not data.get('refresh_token'):
                raise GoogleDataError('Google connection expired. Reconnect your account.')
            payload = {'client_id': data['client_id'], 'refresh_token': data['refresh_token'], 'grant_type': 'refresh_token'}
            if data.get('client_secret'):
                payload['client_secret'] = data['client_secret']
            try:
                tokens = _http(TOKEN_URL, 'POST', payload, form=True)
            except GoogleDataError as exc:
                if exc.status in (400, 401):
                    for key in ('access_token', 'refresh_token', 'expires_at', 'scope'):
                        data.pop(key, None)
                    self._save(data)
                raise
            self._store_tokens(data, tokens)
            return data['access_token']

    def disconnect(self):
        with self.lock:
            data = self._read()
            token = data.get('refresh_token') or data.get('access_token')
            self._save({key: data[key] for key in ('client_id', 'client_secret') if key in data})
            message = ''
            if token:
                try:
                    _http('https://oauth2.googleapis.com/revoke', 'POST', {'token': token}, form=True)
                except GoogleDataError:
                    message = 'Disconnected locally. Remote revocation could not be confirmed; remove access in your Google account if needed.'
            result = self.status()
            if message:
                result['message'] = message
            return result

    def _api(self, url, scope, payload=None):
        token = self._token(scope)
        try:
            return _http(url, 'POST' if payload is not None else 'GET', payload, token=token)
        except GoogleDataError as exc:
            if exc.status != 401:
                raise
            with self.lock:
                data = self._read()
                data['expires_at'] = 0
                self._save(data)
            return _http(url, 'POST' if payload is not None else 'GET', payload, token=self._token(scope))

    def properties(self):
        result = {'search_console': [], 'analytics': [], 'issues': []}
        status = self.status()
        if status['search_console']:
            try:
                response = self._api('https://www.googleapis.com/webmasters/v3/sites', SEARCH_SCOPE)
                result['search_console'] = [{key: item.get(key, '') for key in ('siteUrl', 'permissionLevel')}
                                            for item in response.get('siteEntry', [])[:2000]]
            except GoogleDataError as exc:
                result['issues'].append(str(exc))
        if status['analytics']:
            token, seen = '', set()
            try:
                for _ in range(10):
                    url = 'https://analyticsadmin.googleapis.com/v1beta/accountSummaries?' + urlencode({'pageSize': 200, 'pageToken': token})
                    response = self._api(url, ANALYTICS_SCOPE)
                    for account in response.get('accountSummaries', []):
                        for prop in account.get('propertySummaries', []):
                            result['analytics'].append({'property': prop.get('property', ''), 'displayName': prop.get('displayName', ''),
                                                        'account': account.get('displayName', account.get('account', ''))})
                    token = response.get('nextPageToken', '')
                    if not token:
                        break
                    if token in seen:
                        result['issues'].append('Google Analytics returned a repeated pagination token; listing was stopped.')
                        break
                    seen.add(token)
                else:
                    result['issues'].append('Google Analytics property listing reached its pagination limit.')
            except GoogleDataError as exc:
                result['issues'].append(str(exc))
        return result

    def _search(self, site, period, dimension=None):
        endpoint = 'https://www.googleapis.com/webmasters/v3/sites/' + quote(site, safe='') + '/searchAnalytics/query'
        request = {'startDate': period['start'], 'endDate': period['end'], 'type': 'web', 'dataState': 'final',
                   'aggregationType': 'auto' if dimension == 'page' else 'byProperty', 'rowLimit': 1000, 'startRow': 0}
        if dimension:
            request['dimensions'] = [dimension]
        rows = []
        for index in range(5 if dimension else 1):
            request['startRow'] = index * 1000
            response = self._api(endpoint, SEARCH_SCOPE, request)
            batch = response.get('rows', [])
            for row in batch[:1000]:
                item = {key: _number(row.get(key, 0)) for key in ('clicks', 'impressions', 'ctr', 'position')}
                if dimension:
                    item[dimension] = str((row.get('keys') or [''])[0])
                rows.append(item)
            if len(batch) < 1000:
                return rows, False
        return rows, bool(dimension and len(rows) >= 5000)

    def _analytics(self, property_id, period):
        response = self._api('https://analyticsdata.googleapis.com/v1beta/properties/' + property_id + ':runReport', ANALYTICS_SCOPE,
            {'dateRanges': [{'startDate': period['start'], 'endDate': period['end']}],
             'metrics': [{'name': 'sessions'}, {'name': 'keyEvents'}],
             'dimensionFilter': {'filter': {'fieldName': 'sessionDefaultChannelGroup',
                                           'stringFilter': {'matchType': 'EXACT', 'value': 'Organic Search'}}},
             'limit': '1', 'returnPropertyQuota': True})
        values = ((response.get('rows') or [{}])[0]).get('metricValues', [])
        return {'organic_sessions': _number(values[0].get('value')) if len(values) > 0 else 0,
                'key_events': _number(values[1].get('value')) if len(values) > 1 else 0,
                'metadata': response.get('metadata', {}),
                'note': 'Key events recorded in Organic Search sessions, not proof that content caused the conversion.'}

    def sync(self, site_url, ga4_property=''):
        site_url = str(site_url).strip()
        if site_url.startswith('sc-domain:'):
            if not re.fullmatch(r'sc-domain:[A-Za-z0-9.-]+', site_url):
                raise GoogleDataError('Invalid Search Console domain property.')
        else:
            p = urlsplit(site_url)
            if p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password or p.query or p.fragment:
                raise GoogleDataError('Select a valid Search Console URL-prefix or domain property.')
        property_id = str(ga4_property).removeprefix('properties/').strip()
        if property_id and not re.fullmatch(r'\d{1,30}', property_id):
            raise GoogleDataError('GA4 property ID must be numeric.')
        today = datetime.now(timezone.utc).date()
        end = today - timedelta(days=3)
        start = end - timedelta(days=27)
        prior_end = start - timedelta(days=1)
        periods = {'current': {'start': start.isoformat(), 'end': end.isoformat()},
                   'previous': {'start': (prior_end - timedelta(days=27)).isoformat(), 'end': prior_end.isoformat()}}
        result = {'site_url': site_url, 'ga4_property': property_id, 'updated_at': datetime.now(timezone.utc).isoformat(),
                  'periods': periods, 'search_console': None, 'analytics': None, 'recommendations': [], 'issues': [],
                  'notes': ['Windows end three days ago to reduce incomplete-data effects; Google may still revise data.',
                            'Search Console uses Pacific dates; GA4 uses the property time zone. Cross-product totals are not directly comparable.',
                            'Query and page lists are top rows, may omit anonymized queries, and must not be summed as site totals.']}
        if self.status()['search_console']:
            search = {'limited': False}
            try:
                for name, period in periods.items():
                    totals, _ = self._search(site_url, period)
                    queries, q_limit = self._search(site_url, period, 'query')
                    pages, p_limit = self._search(site_url, period, 'page')
                    search[name] = {'totals': totals[0] if totals else {'clicks': 0, 'impressions': 0, 'ctr': 0, 'position': 0},
                                    'queries': queries, 'pages': pages}
                    search['limited'] = search['limited'] or q_limit or p_limit
                result['search_console'] = search
            except GoogleDataError as exc:
                result['issues'].append(str(exc))
        else:
            result['issues'].append('Search Console is not connected with read-only permission.')
        if property_id:
            try:
                result['analytics'] = {name: self._analytics(property_id, period) for name, period in periods.items()}
            except GoogleDataError as exc:
                result['issues'].append(str(exc))
        if result['search_console'] is None and result['analytics'] is None:
            raise GoogleDataError('Google sync failed; previous data was preserved. ' + ' '.join(result['issues'][:2]), retryable=True)
        search = result['search_console']
        if search:
            current, previous = search['current']['totals'], search['previous']['totals']
            if previous['clicks'] >= 10 and current['clicks'] < previous['clicks'] * .8:
                result['recommendations'].append('Investigate the click decline against previous-period queries and pages. Check seasonality and site changes before rewriting content.')
            for row in search['current']['queries']:
                if row['impressions'] >= 100 and 4 <= row['position'] <= 20 and row['ctr'] < .02:
                    result['recommendations'].append('Review intent and relevant page coverage for query “%s” (%s impressions, average position %.1f); this is an observed opportunity, not a ranking guarantee.' %
                                                       (row['query'], int(row['impressions']), row['position']))
                    if len(result['recommendations']) >= 6:
                        break
            if current['impressions'] == 0:
                result['recommendations'].append('No search impressions were returned. Confirm the selected property and date range, then check indexing before drawing content conclusions.')
        return result
