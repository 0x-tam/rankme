"""Evidence-preserving SEO audit and portable reports.

The audit is one JSON-compatible record. Absence in a bounded crawl or Search
Console export is never represented as a measured zero or proof of absence.
No semantic decision, publication, or remote write is performed here.
"""

from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import io
import json
import math
import os
import re
import ssl
import stat
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener, HTTPSHandler, HTTPRedirectHandler, ProxyHandler
import xml.etree.ElementTree as ET
import zipfile

from .crawler import normalize_url, fetch_public, _addresses


GSC_SOURCE = 'https://search.google.com/search-console/performance/search-analytics'
PSI_SOURCE = 'https://pagespeed.web.dev/'
CTR_REVIEW_HEURISTIC = .02  # Editorial triage only; never a calibrated baseline.
MAX_ROWS = 100
_STOP = set('a an and are for from how in into is of on or our the this to what when where why with your'.split())


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and result >= 0 else None


def _text(value, limit=500):
    return ' '.join(str(value or '').split())[:limit]


def _public_url(value):
    try:
        if not value:
            return ''
        # Apply the same public-address and credential-query policy used for
        # AI Overview evidence before this URL can enter reports or AI briefs.
        from .ai_overviews import _public_url as safe_observation_url
        return safe_observation_url(normalize_url(value))
    except (ValueError, UnicodeError):
        return ''


def _site_url(value, host):
    url = _public_url(value)
    if not url:
        return ''
    domain = (urlsplit(url).hostname or '').removeprefix('www.')
    return url if domain == host else ''


def _rows(value, limit=5000):
    return [row for row in value[:limit] if isinstance(row, dict)] if isinstance(value, list) else []


def _tokens(value):
    return set(re.findall(r'[^\W_]{3,}', _text(value, 1000).casefold())) - _STOP


def _id(kind, *parts):
    data = '\n'.join([kind, *(_text(part, 2000).casefold() for part in parts)])
    return kind + '-' + hashlib.sha256(data.encode()).hexdigest()[:20]


def _evidence(source, source_url, observed_at, method, measurements=None, caveats=None):
    return {'source': source, 'source_url': source_url, 'observed_at': observed_at,
            'method': method, 'measurements': measurements or {}, 'caveats': caveats or []}


def _action(client_id, kind, title, query, url, evidence, priority, *, existing_url='', status='needs_verification'):
    return {'id': _id('action', client_id, kind, query, url), 'kind': kind,
            'title': title, 'query': query, 'url': url, 'existing_url': existing_url,
            'priority': priority, 'status': status, 'evidence': evidence,
            'automation_eligible': False}


def validate_experience_entry(entry):
    """Retain provenance and permit brief facts only after explicit user confirmation."""
    if not isinstance(entry, dict):
        raise ValueError('Experience entry must be an object')
    facts = []
    raw_facts = entry.get('facts')
    if isinstance(raw_facts, list):
        facts = [_text(fact, 1000) for fact in raw_facts if isinstance(fact, str) and _text(fact)]
    source = entry.get('source') if isinstance(entry.get('source'), dict) else {}
    source_type = _text(source.get('type'), 80)
    source_url = _public_url(source.get('url'))
    invalid_source_url = bool(source.get('url')) and not source_url
    if invalid_source_url:
        source_type = 'rejected_url'
    confirmed = entry.get('user_confirmed') is True and bool(facts) and not invalid_source_url
    # Source verification has its own state; user confirmation never implies it.
    source_verified = entry.get('source_verification_status') == 'verified' and bool(source_url)
    verification = 'user_confirmed' if confirmed else 'unverified'
    return {'id': _text(entry.get('id'), 120) or _id('experience', *facts),
            'label': _text(entry.get('label'), 200), 'facts': facts[:30],
            'source': {'type': source_type, 'url': source_url,
                       'reference': _text(source.get('reference'), 250)},
            'user_confirmed': confirmed,
            'confirmed_at': _text(entry.get('confirmed_at'), 80) if confirmed else None,
            'verification_status': verification,
            'source_verification_status': 'verified' if source_verified else 'unverified',
            'brief_eligible': confirmed}


def validate_experience_claim(claim, source_url=None, client_url=None):
    """Normalize a claim for user review without confirming or source-verifying it."""
    if isinstance(claim, str):
        claim = {'facts': [claim]}
    if not isinstance(claim, dict):
        raise ValueError('Experience claim must be text or an object')
    normalized = dict(claim)
    if source_url is not None:
        normalized['source'] = {'type': 'public_url', 'url': source_url}
    source = normalized.get('source') or {}
    if source.get('url') and not _public_url(source.get('url')):
        raise ValueError('Experience source URL must be public HTTP(S)')
    if client_url and not _public_url(client_url):
        raise ValueError('Client URL must be public HTTP(S)')
    result = validate_experience_entry(normalized)
    if not result['facts']:
        raise ValueError('Experience claim needs at least one concrete fact')
    # New submissions cannot assert their own verification or approval flags.
    result.update(user_confirmed=False, verification_status='unverified', source_verification_status='unverified',
                  brief_eligible=False, confirmed_at=None)
    return result


def parse_page_speed(payload, *, target_url='', observed_at=None):
    """Extract actual PageSpeed and CrUX measurements, preserving missing values."""
    if not isinstance(payload, dict):
        return {'status': 'unavailable', 'source_url': PSI_SOURCE,
                'observed_at': observed_at, 'measurements': {}, 'missing_data': ['PageSpeed response unavailable.']}
    lighthouse = payload.get('lighthouseResult') or {}
    audits = lighthouse.get('audits') or {}
    categories = lighthouse.get('categories') or {}
    performance = categories.get('performance') or {}
    measured = {}
    score = _number(performance.get('score'))
    if score is not None and score <= 1:
        measured['lab_performance_score'] = round(score * 100, 1)
    for field, audit_key in [('lab_lcp_ms', 'largest-contentful-paint'),
                             ('lab_fcp_ms', 'first-contentful-paint'),
                             ('lab_cls', 'cumulative-layout-shift'),
                             ('lab_tbt_ms', 'total-blocking-time')]:
        value = _number((audits.get(audit_key) or {}).get('numericValue'))
        if value is not None:
            measured[field] = value
    field_data = payload.get('loadingExperience') or {}
    metrics = field_data.get('metrics') or {}
    for field, name in [('field_lcp_ms', 'LARGEST_CONTENTFUL_PAINT_MS'),
                        ('field_cls', 'CUMULATIVE_LAYOUT_SHIFT_SCORE'),
                        ('field_inp_ms', 'INTERACTION_TO_NEXT_PAINT')]:
        value = _number((metrics.get(name) or {}).get('percentile'))
        if value is not None:
            measured[field] = value / 100 if field == 'field_cls' else value
    missing = []
    if not any(key.startswith('field_') for key in measured):
        missing.append('CrUX field data unavailable for this URL; no real-user values inferred.')
    if not any(key.startswith('lab_') for key in measured):
        missing.append('PageSpeed lab measurements unavailable.')
    return {'status': 'measured' if measured else 'unavailable', 'source': 'Google PageSpeed Insights',
            'source_url': PSI_SOURCE, 'target_url': _public_url(target_url),
            'observed_at': observed_at or _text(payload.get('analysisUTCTimestamp'), 80) or None,
            'method': 'PageSpeed Insights API response; lab and CrUX field data are distinct.',
            'measurements': measured, 'missing_data': missing}


def fetch_page_speed(target_url, *, fetcher=fetch_public):
    """One bounded public PageSpeed GET; the provider's unavailable fields stay unknown."""
    target = _public_url(target_url)
    if not target:
        raise ValueError('A public HTTP(S) target URL is required')
    parsed = urlsplit(target)
    _addresses(parsed.hostname, parsed.port or (443 if parsed.scheme == 'https' else 80))
    endpoint = 'https://www.googleapis.com/pagespeedonline/v5/runPagespeed?' + urlencode(
        {'url': target, 'strategy': 'mobile', 'category': 'performance'})
    response = fetcher(endpoint, max_bytes=5_000_000, allowed_hosts={'www.googleapis.com'})
    payload = json.loads(response['text'])
    return parse_page_speed(payload, target_url=target,
                            observed_at=datetime.now(timezone.utc).isoformat())


def build_audit(client, crawl, seo=None, articles=None, *, page_speed=None, experience=None):
    """Build a canonical, bounded, evidence-based audit for one client.

    Inputs match the current client crawl and GoogleData.sync records. Candidate
    gaps remain verification tasks: Search Console row limits and crawl sampling
    prevent proving that no suitable page exists.
    """
    client = client if isinstance(client, dict) else {}
    crawl = crawl if isinstance(crawl, dict) else {'pages': crawl}
    crawl_summary = crawl.get('summary') if isinstance(crawl.get('summary'), dict) else {}
    crawl_issues = crawl.get('issues') if isinstance(crawl.get('issues'), list) else []
    seo = seo if isinstance(seo, dict) and seo.get('client_id', client.get('id')) == client.get('id') else {}
    client_id = _text(client.get('id'), 120)
    homepage = _public_url(client.get('url'))
    host = (urlsplit(homepage).hostname or '').removeprefix('www.')
    raw_pages = _rows(crawl.get('pages'), 60)
    pages = {_site_url(row.get('url'), host): row for row in raw_pages if _site_url(row.get('url'), host)}
    search = seo.get('search_console') or {}
    current = search.get('current') or {}
    gsc_updated = seo.get('updated_at')
    crawl_updated = crawl.get('observed_at') or client.get('crawl_observed_at')
    audit_observed_at = crawl.get('observed_at') or datetime.now(timezone.utc).isoformat()
    period = (seo.get('periods') or {}).get('current')
    limited = bool(search.get('limited'))
    missing = []
    if not pages:
        missing.append('No same-site crawl pages available; page checks are unassessed.')
    if not search:
        missing.append('Search Console data unavailable; query demand and ranking are unassessed.')
    if not isinstance(current.get('query_pages'), list):
        missing.append('Joint query/page rows unavailable; landing-page attribution is unassessed.')
    if limited:
        missing.append('Search Console top-row import was truncated; absent query/page rows do not prove absence.')
    if crawl_summary.get('limited', True):
        missing.append('Website crawl is sampled or truncated; absent pages, links, and schema are not proven absent.')

    technical = []
    def finding(kind, title, url, measurements, caveats=None):
        technical.append({'id': _id('finding', client_id, kind, url), 'kind': kind,
                          'title': title, 'url': url, 'status': 'observed',
                          'evidence': _evidence('website_crawl', url, crawl_updated,
                                                'Deterministic check of fetched HTML/crawl row.', measurements, caveats)})

    titles = defaultdict(list)
    descriptions = defaultdict(list)
    inbound = defaultdict(int)
    for url, row in sorted(pages.items()):
        title = _text(row.get('title'))
        description = _text(row.get('description'))
        if title:
            titles[title.casefold()].append(url)
        else:
            finding('missing_title', 'No title observed', url, {'title': ''})
        if description:
            descriptions[description.casefold()].append(url)
        else:
            finding('missing_description', 'No meta description observed', url, {'description': ''})
        status = _number(row.get('status'))
        if status is not None and status >= 400:
            finding('http_error', 'Observed HTTP error', url, {'http_status': int(status)})
        robots = row.get('robots') or []
        robots_text = ' '.join(robots if isinstance(robots, list) else [str(robots)]).lower()
        if row.get('noindex') is True or re.search(r'\bnoindex\b', robots_text):
            finding('noindex', 'Noindex instruction observed', url,
                    {'noindex': True, 'robots': robots})
        canonical = _public_url(row.get('canonical'))
        if canonical and canonical != url:
            finding('canonical_mismatch', 'Canonical points to another URL', url,
                    {'canonical_url': canonical}, ['Confirm whether this is intentional.'])
        if 'headings' in row and not row['headings']:
            finding('missing_headings', 'No headings observed', url, {'headings': []})
        if 'schema' in row and not row['schema']:
            finding('missing_schema', 'No JSON-LD observed', url, {'schema': []},
                    ['Other structured-data formats may be present; JSON-LD is optional.'])
        if isinstance(row.get('schema'), list) and any(
                isinstance(item, dict) and item.get('valid_json') is False for item in row['schema']):
            finding('invalid_json_ld', 'Malformed JSON-LD observed', url,
                    {'invalid_blocks': sum(1 for item in row['schema'] if isinstance(item, dict)
                                           and item.get('valid_json') is False)})
        if 'anchors' in row and isinstance(row['anchors'], list):
            empty = sum(1 for anchor in row['anchors'][:500] if isinstance(anchor, dict) and
                        not _text(anchor.get('text')) and not _text(anchor.get('aria_label')))
            if empty:
                finding('empty_link_text', 'Links without observed accessible text', url,
                        {'empty_anchor_count': empty})
        for target in set(_site_url(value, host) for value in row.get('links', [])[:500]
                          if isinstance(value, str)):
            if target in pages and target != url:
                inbound[target] += 1
    for field, grouped in [('title', titles), ('description', descriptions)]:
        for value, urls in sorted(grouped.items()):
            if len(urls) > 1:
                finding('duplicate_' + field, 'Duplicate observed ' + field, urls[0],
                        {field: value, 'urls': sorted(urls)})
    for url in sorted(pages):
        if inbound[url] == 0 and urlsplit(url).path != '/':
            finding('no_sampled_inlinks', 'No incoming links in crawl sample', url,
                    {'sampled_pages': len(pages)}, ['A bounded crawl cannot establish an orphan page.'])
    if pages and not any('schema' in row for row in pages.values()):
        missing.append('Structured-data checks unavailable because crawl rows contain no schema observations.')
    if pages and not any('anchors' in row for row in pages.values()):
        missing.append('Anchor-text checks unavailable because crawl rows contain link URLs only.')
    if pages and not any('status' in row for row in pages.values()):
        missing.append('HTTP status checks unavailable because crawl rows contain no status observations.')
    for issue in crawl_issues[:100]:
        if not isinstance(issue, str):
            continue
        match = re.fullmatch(r'(https?://\S+): Website returned HTTP (\d{3})\.', issue.strip())
        if match:
            url = _site_url(match.group(1), host)
            if url:
                finding('http_error', 'Observed HTTP error during crawl', url,
                        {'http_status': int(match.group(2))})

    joint = defaultdict(dict)
    for row in _rows(current.get('query_pages')):
        query = _text(row.get('query'), 300)
        url = _site_url(row.get('page'), host)
        impressions = _number(row.get('impressions'))
        if query and url and impressions is not None and impressions > 0:
            candidate = {'page': url, 'impressions': impressions,
                         'clicks': _number(row.get('clicks')), 'position': _number(row.get('position'))}
            existing = joint[query.casefold()].get(url)
            if existing is None or impressions > existing['impressions']:
                joint[query.casefold()][url] = candidate
    actions = []
    for row in _rows(current.get('queries')):
        query = _text(row.get('query'), 300)
        impressions = _number(row.get('impressions'))
        clicks = _number(row.get('clicks'))
        position = _number(row.get('position'))
        if not query or impressions is None or impressions < 100 or clicks is None or position is None:
            continue
        ctr = clicks / impressions
        if position < 4 or position > 20 or ctr >= CTR_REVIEW_HEURISTIC:
            continue
        observed = sorted(joint.get(query.casefold(), {}).values(), key=lambda x: (-x['impressions'], x['page']))
        vocabulary = _tokens(query)
        lexical = [url for url, page in pages.items() if vocabulary and
                   len(vocabulary & _tokens(page.get('title'))) >= min(2, len(vocabulary))]
        base = _evidence('google_search_console', GSC_SOURCE, gsc_updated,
                         'Observed current-period query metrics; CTR below an editorial 2% review heuristic, not a calibrated baseline.',
                         {'query': query, 'impressions': impressions, 'clicks': clicks,
                          'ctr': ctr, 'average_position': position, 'period': period,
                          'ctr_review_heuristic': CTR_REVIEW_HEURISTIC},
                         ['Impressions measure this property in this period, not market search volume.',
                          'Average position is an observed aggregate, not a guaranteed rank.',
                          'Search Console may omit anonymized or lower-ranked rows.'])
        if observed:
            leader = observed[0]
            evidence = {**base, 'joint_query_page_rows': observed[:10],
                        'landing_page_title': _text((pages.get(leader['page']) or {}).get('title'), 300),
                        'landing_page_description': _text((pages.get(leader['page']) or {}).get('description'), 500),
                        'method': 'Query/page evidence identifies an existing landing page; compare its snippet and intent before new content.'}
            kind = 'gsc_overlap_review' if len(observed) > 1 else 'gsc_leak'
            title = ('Review competing observed pages' if len(observed) > 1 else 'Improve existing search result') + ' for “' + query + '”'
            actions.append(_action(client_id, kind, title, query, leader['page'], evidence, 1,
                                   existing_url=leader['page']))
        elif lexical:
            url = sorted(lexical)[0]
            evidence = {**base, 'existing_page_candidates': sorted(lexical)[:10],
                        'landing_page_title': _text((pages.get(url) or {}).get('title'), 300),
                        'landing_page_description': _text((pages.get(url) or {}).get('description'), 500),
                        'method': 'Crawl title overlap suggests an existing page; verify intent and joint query/page data.'}
            actions.append(_action(client_id, 'gsc_coverage_review',
                                   'Review existing coverage for “' + query + '”', query, url, evidence, 2,
                                   existing_url=url))
        elif isinstance(current.get('query_pages'), list):
            evidence = {**base, 'method': 'Observed query demand, with no aligned page identified in available joint rows or sampled titles.',
                        'caveats': base['caveats'] + ['Candidate gap only; verify the complete site and current results before creating a page.']}
            actions.append(_action(client_id, 'gsc_gap_candidate',
                                   'Verify possible content gap for “' + query + '”', query, '', evidence, 3))
        else:
            evidence = {**base, 'method': 'Query is observed but joint query/page attribution is unavailable.',
                        'caveats': base['caveats'] + ['No content-gap classification is possible without landing-page evidence.']}
            actions.append(_action(client_id, 'gsc_attribution_review',
                                   'Check landing page for “' + query + '”', query, '', evidence, 2))
    actions.sort(key=lambda row: (row['priority'], -row['evidence']['measurements']['impressions'], row['id']))

    experience_rows = [validate_experience_entry(item) for item in _rows(experience, 100)]
    facts = [{'experience_id': item['id'], 'fact': fact, 'source': item['source']}
             for item in experience_rows if item['brief_eligible'] for fact in item['facts']]
    speed = (page_speed if isinstance(page_speed, dict) and 'measurements' in page_speed else
             parse_page_speed(page_speed, target_url=homepage))
    if speed.get('status') != 'measured':
        missing.append('PageSpeed and CrUX metrics unavailable; performance is unassessed.')
    missing.extend(speed.get('missing_data') or [])
    return {'schema_version': 2, 'client_id': client_id, 'site_url': homepage,
            'observed_at': audit_observed_at, 'sources': {
                'crawl': {'observed_at': crawl_updated, 'pages': len(pages),
                          'sampled_or_truncated': bool(crawl_summary.get('limited', True))},
                'search_console': {'source_url': GSC_SOURCE, 'observed_at': gsc_updated,
                                   'period': period, 'query_rows': len(_rows(current.get('queries'))),
                                   'query_page_rows': len(_rows(current.get('query_pages'))),
                                   'sampled_or_truncated': limited}},
            'coverage': {'pages_observed': len(pages), 'queries_observed': len(_rows(current.get('queries')))},
            'crawl_issues': [_text(item, 500) for item in crawl_issues[:100] if isinstance(item, str)],
            'actions': actions[:MAX_ROWS], 'technical_findings': technical[:MAX_ROWS],
            'page_speed': speed, 'experience_library': experience_rows,
            'verified_brief_facts': facts[:100], 'missing_data': list(dict.fromkeys(missing)),
            'summary': {'actions': min(len(actions), MAX_ROWS),
                        'technical_findings': min(len(technical), MAX_ROWS),
                        'pages_observed': len(pages), 'queries_observed': len(_rows(current.get('queries')))}}


_INTENT = {
    'informational': 'The searcher primarily wants an explanation, instructions, or facts.',
    'commercial': 'The searcher compares providers, products, or options before buying.',
    'transactional': 'The searcher appears ready to buy, book, download, or contact.',
    'navigational': 'The searcher seeks a particular known site, brand, or page.',
    'unclear': 'The short query does not reveal a reliable dominant intent.',
}
_EVIDENCE_QUALITY = {
    'adequate_for_review': 'Supplied query and page context is specific enough for a human to review page fit.',
    'limited': 'Some useful context exists but intent or page fit needs more evidence.',
    'insufficient': 'Context is too sparse to judge intent or page fit responsibly.',
}


def _typesafe_key():
    key = os.environ.get('TYPESAFE_API_KEY')
    if key:
        return key
    if not hasattr(os, 'O_NOFOLLOW'):
        raise ValueError('Secure TypeSafe credential loading unavailable on this platform')
    path = Path.home() / '.config' / 'rankme' / 'typesafe.env'
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, 'O_CLOEXEC', 0)
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
                info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > 4096):
            raise ValueError('TypeSafe credential file must be an owner-only regular file')
        content = os.read(descriptor, 4097)
        if len(content) > 4096:
            raise ValueError('TypeSafe credential file is too large')
    finally:
        os.close(descriptor)
    for line in content.decode('utf-8').splitlines():
        match = re.fullmatch(r'\s*(?:export\s+)?TYPESAFE_API_KEY\s*=\s*(.*?)\s*', line)
        if not match:
            continue
        value = match.group(1)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        if re.fullmatch(r'[A-Za-z0-9._-]+', value):
            return value
    raise ValueError('TypeSafe credential unavailable')


def _call_jev(state, questions):
    key = _typesafe_key()
    body = json.dumps({'model': 'jev-latest', 'state': state, 'questions': questions},
                      ensure_ascii=False, allow_nan=False).encode('utf-8')
    request = Request('https://api.typesafe.ai/v1/systemone', data=body,
                      headers={'Authorization': 'Bearer ' + key,
                               'Content-Type': 'application/json'}, method='POST')
    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, request, fp, code, msg, headers, newurl):
            raise ValueError('TypeSafe redirect refused')
    opener = build_opener(ProxyHandler({}), HTTPSHandler(context=ssl.create_default_context()), NoRedirect())
    with opener.open(request, timeout=12) as response:
        if urlsplit(response.geturl()).netloc != 'api.typesafe.ai':
            raise ValueError('Unexpected TypeSafe response origin')
        raw = response.read(200001)
        if len(raw) > 200000:
            raise ValueError('TypeSafe response too large')
        return json.loads(raw)


def _validated_choice(answers, key, options):
    answer = answers.get(key) if isinstance(answers, dict) else None
    if not isinstance(answer, dict) or answer.get('type') != 'choice' or answer.get('choice') not in options:
        raise ValueError('Invalid TypeSafe choice response')
    confidence = _number(answer.get('confidence'))
    probabilities = answer.get('probabilities')
    if confidence is None or confidence > 1 or not isinstance(probabilities, dict) or set(probabilities) != set(options):
        raise ValueError('Invalid TypeSafe confidence or probability distribution')
    numbers = {name: _number(probabilities[name]) for name in options}
    if any(value is None or value > 1 for value in numbers.values()) or abs(sum(numbers.values()) - 1) > .02:
        raise ValueError('Invalid TypeSafe probability distribution')
    return {'choice': answer['choice'], 'confidence': confidence, 'probabilities': numbers}


def enrich_actions_with_jev(audit, *, enabled=False, evaluator=None, max_actions=5):
    """Optional bounded semantic review; never changes deterministic actions or enables execution.

    The caller must pass enabled=True for remote requests. An evaluator may be
    injected in tests. Failures become explicit unavailable status, not verdicts.
    """
    result = deepcopy(audit)
    actions = result.get('actions', [])
    for item in actions:
        item['semantic_judgment'] = {'status': 'unassessed', 'reason': 'Not selected for semantic review.'}
    if not enabled:
        result['semantic_assessment'] = {'status': 'disabled', 'assessed_actions': 0,
                                         'method': 'TypeSafe Jev Choice judgments; opt-in only.'}
        return result
    if type(max_actions) is not int or not 1 <= max_actions <= 10:
        raise ValueError('max_actions must be between 1 and 10')
    evaluate = evaluator or _call_jev
    selected = actions[:max_actions]
    state = {'actions': []}
    questions = {}
    for index, item in enumerate(selected):
        evidence = item.get('evidence') or {}
        state['actions'].append({'query': _text(item.get('query'), 300),
                                 'observed_page_title': _text(evidence.get('landing_page_title'), 300),
                                 'observed_page_description': _text(evidence.get('landing_page_description'), 500),
                                 'joint_page_observed': bool(evidence.get('joint_query_page_rows')),
                                 'source_timestamp_present': bool(evidence.get('observed_at')),
                                 'source_caveats': [_text(value, 200) for value in (evidence.get('caveats') or [])[:5]]})
        questions[f'intent_{index}'] = {
            'type': 'choice',
            'instructions': f'What dominant search intent is conveyed by `actions[{index}].query`? Choose unclear when context is insufficient. Do not infer demand or ranking.',
            'criteria': _INTENT}
        questions[f'evidence_quality_{index}'] = {
            'type': 'choice',
            'instructions': f'How sufficient are `actions[{index}]` query and observed page context to assess page fit? A missing page or truncated evidence cannot establish a content gap.',
            'criteria': _EVIDENCE_QUALITY}
    try:
        response = evaluate(state, questions) if selected else {'answers': {}, 'model': None}
        if not isinstance(response, dict) or not isinstance(response.get('answers'), dict):
            raise ValueError('Invalid TypeSafe response')
    except Exception:
        for item in selected:
            item['semantic_judgment'] = {'status': 'unavailable',
                                         'reason': 'TypeSafe request unavailable; no verdict inferred.'}
        result['semantic_assessment'] = {'status': 'unavailable', 'assessed_actions': 0,
                                         'method': 'TypeSafe Jev Choice judgments; opt-in only.'}
        return result
    assessed = 0
    for index, item in enumerate(selected):
        try:
            intent = _validated_choice(response['answers'], f'intent_{index}', _INTENT)
            quality = _validated_choice(response['answers'], f'evidence_quality_{index}', _EVIDENCE_QUALITY)
            item['semantic_judgment'] = {'status': 'assessed', 'model': _text(response.get('model'), 80),
                                         'intent': intent, 'evidence_quality': quality,
                                         'method': 'Two independent Jev Choice judgments on bounded query/page context; review aid only.'}
            assessed += 1
        except Exception:
            item['semantic_judgment'] = {'status': 'unavailable',
                                         'reason': 'TypeSafe answer missing or invalid; no verdict inferred.'}
    result['semantic_assessment'] = {'status': ('assessed' if assessed == len(selected) else
                                               'partial' if assessed else 'unavailable'),
                                     'assessed_actions': assessed,
                                     'method': 'TypeSafe Jev Choice judgments; opt-in only.'}
    return result


def validate_summary(summary, audit):
    """Reject unsupported action references or numeric claims in generated summaries."""
    if not isinstance(summary, dict) or not isinstance(audit, dict):
        raise ValueError('Summary and audit must be objects')
    known = {action['id'] for action in audit.get('actions', [])}
    ids = summary.get('action_ids', [])
    if not isinstance(ids, list) or any(not isinstance(item, str) or item not in known for item in ids):
        raise ValueError('Summary references an unknown action ID')
    claims = summary.get('numeric_claims', [])
    if not isinstance(claims, list):
        raise ValueError('Numeric claims must be a list')
    prose = summary.get('text', '')
    if not isinstance(prose, str) or len(prose) > 5000:
        raise ValueError('Summary text must be a short string')
    if re.search(r'\d', prose):
        raise ValueError('Numeric prose must use validated numeric_claims')
    for claim in claims:
        if not isinstance(claim, dict) or claim.get('action_id') not in known:
            raise ValueError('Numeric claim references an unknown action ID')
        action = next(item for item in audit['actions'] if item['id'] == claim['action_id'])
        key = claim.get('metric')
        measured = (action.get('evidence') or {}).get('measurements') or {}
        if (key not in measured or isinstance(measured[key], (dict, list)) or
                isinstance(claim.get('value'), bool) or claim.get('value') != measured[key]):
            raise ValueError('Numeric claim lacks matching measured evidence')
    return True


def _safe_cell(value):
    text = ''.join(ch for ch in _text(value, 32000) if ch in '\t\n\r' or ord(ch) >= 32)
    return "'" + text if text.lstrip().startswith(('=', '+', '-', '@')) or text.startswith(('\t', '\r', '\n')) else text


def _audit_lines(audit):
    return json.dumps(audit, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2).splitlines()


def _markdown(audit):
    lines = ['# RankMe evidence audit', '', 'Site: ' + _text(audit.get('site_url')), '',
             'Observed at: ' + _text(audit.get('observed_at') or 'unknown'), '',
             '## Actions', '']
    for action in audit.get('actions', []):
        lines += ['### ' + action['title'], '', 'ID: `' + action['id'] + '`',
                  'Status: ' + action['status'], '', '```json',
                  json.dumps(action, ensure_ascii=False, allow_nan=False, indent=2), '```', '']
    lines += ['## Technical findings', '', '```json',
              json.dumps(audit.get('technical_findings', []), ensure_ascii=False, allow_nan=False, indent=2),
              '```', '', '## PageSpeed and CrUX', '', '```json',
              json.dumps(audit.get('page_speed', {}), ensure_ascii=False, allow_nan=False, indent=2),
              '```', '', '## Missing data', '']
    lines += ['- ' + _text(item, 2000) for item in audit.get('missing_data', [])]
    lines += ['', '## Canonical audit record', '', '```json', *_audit_lines(audit), '```', '']
    return '\n'.join(lines).encode('utf-8')


def _pdf_basic(audit):
    # Dependency-free fallback. Escapes preserve Unicode codepoints visibly.
    lines = ['RankMe evidence audit', 'Site: ' + _text(audit.get('site_url')),
             'Observed: ' + _text(audit.get('observed_at') or 'unknown'), ''] + _audit_lines(audit)
    wrapped = []
    for line in lines:
        line = line.encode('ascii', 'backslashreplace').decode('ascii')
        while len(line) > 95:
            wrapped.append(line[:95])
            line = line[95:]
        wrapped.append(line)
    pages = [wrapped[i:i + 58] for i in range(0, len(wrapped), 58)] or [[]]
    objects = [None, None, b'<< /Type /Font /Subtype /Type1 /BaseFont /Courier >>']
    page_ids = []
    for page in pages:
        stream_lines = [b'BT /F1 9 Tf 35 805 Td 11 TL']
        for line in page:
            raw = line.encode('ascii').replace(b'\\', b'\\\\').replace(b'(', b'\\(').replace(b')', b'\\)')
            stream_lines.append(b'(' + raw + b') Tj T*')
        stream_lines.append(b'ET')
        stream = b'\n'.join(stream_lines)
        content_id = len(objects) + 1
        objects.append(b'<< /Length %d >>\nstream\n' % len(stream) + stream + b'\nendstream')
        page_id = len(objects) + 1
        objects.append(b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 842] /Resources << /Font << /F1 3 0 R >> >> /Contents %d 0 R >>' % content_id)
        page_ids.append(page_id)
    objects[0] = b'<< /Type /Catalog /Pages 2 0 R >>'
    objects[1] = b'<< /Type /Pages /Count %d /Kids [%s] >>' % (len(page_ids), b' '.join(('%d 0 R' % i).encode() for i in page_ids))
    out = bytearray(b'%PDF-1.4\n%\xe2\xe3\xcf\xd3\n')
    offsets = [0]
    for index, body in enumerate(objects, 1):
        offsets.append(len(out))
        out.extend(('%d 0 obj\n' % index).encode() + body + b'\nendobj\n')
    start = len(out)
    out.extend(('xref\n0 %d\n0000000000 65535 f \n' % (len(objects) + 1)).encode())
    for offset in offsets[1:]:
        out.extend(('%010d 00000 n \n' % offset).encode())
    out.extend(('trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF' % (len(objects) + 1, start)).encode())
    return bytes(out)


def _pdf(audit):
    try:
        from reportlab.lib.colors import HexColor
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.pdfgen import canvas
    except ImportError:
        return _pdf_basic(audit)
    font_path = next((path for path in (
        '/System/Library/Fonts/Supplemental/Arial Unicode.ttf',
        '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
        '/usr/share/fonts/dejavu/DejaVuSans.ttf') if Path(path).is_file()), None)
    if font_path:
        try:
            pdfmetrics.registerFont(TTFont('RankMeUnicode', font_path))
            body_font = 'RankMeUnicode'
            glyphs = pdfmetrics.getFont(body_font).face.charWidths
        except Exception:
            body_font, glyphs = 'Helvetica', None
    else:
        body_font, glyphs = 'Helvetica', None

    def display(value):
        content = str(value)
        if glyphs is None:
            return content.encode('ascii', 'backslashreplace').decode('ascii')
        return ''.join(ch if ord(ch) in glyphs or ch in '\n\t' else
                       ch.encode('ascii', 'backslashreplace').decode('ascii') for ch in content)

    buffer = io.BytesIO()
    page = canvas.Canvas(buffer, pagesize=A4, pageCompression=1)
    page.setTitle('RankMe evidence audit')
    width, height = A4
    left, right = 48, width - 48
    y, number = height - 54, 1
    navy, teal, gray = HexColor('#153047'), HexColor('#087e81'), HexColor('#596876')

    def footer():
        page.setStrokeColor(HexColor('#d8e0e5'))
        page.line(left, 37, right, 37)
        page.setFillColor(gray)
        page.setFont('Helvetica', 8)
        page.drawString(left, 24, 'RankMe | observed evidence and review candidates')
        page.drawRightString(right, 24, 'Page ' + str(number))

    def next_page():
        nonlocal y, number
        footer()
        page.showPage()
        number += 1
        y = height - 54

    def write(value, *, size=9, color=navy, space=3, indent=0, font=None):
        nonlocal y
        face = font or body_font
        limit = right - left - indent
        for paragraph in display(value).split('\n'):
            words = paragraph.split(' ')
            lines, current = [], ''
            for word in words:
                candidate = (current + ' ' + word).strip() if current else word
                if pdfmetrics.stringWidth(candidate, face, size) <= limit:
                    current = candidate
                else:
                    if current:
                        lines.append(current)
                        current = ''
                    if pdfmetrics.stringWidth(word, face, size) <= limit:
                        current = word
                    else:
                        piece = ''
                        for char in word:
                            if pdfmetrics.stringWidth(piece + char, face, size) > limit and piece:
                                lines.append(piece)
                                piece = ''
                            piece += char
                        current = piece
            lines.append(current)
            for line in lines:
                if y < 58:
                    next_page()
                page.setFillColor(color)
                page.setFont(face, size)
                page.drawString(left + indent, y, line)
                y -= size + space

    def section(title):
        nonlocal y
        y -= 10
        if y < 95:
            next_page()
        page.setStrokeColor(HexColor('#c5e2e2'))
        page.line(left, y + 12, right, y + 12)
        write(title, size=13, color=teal, space=6, font='Helvetica-Bold')

    write('RankMe evidence audit', size=20, color=navy, space=8, font='Helvetica-Bold')
    write('Site: ' + _text(audit.get('site_url')), size=10)
    write('Observed at: ' + _text(audit.get('observed_at') or 'unknown'), size=9, color=gray)
    summary = audit.get('summary') or {}
    write('Actions: %s   Technical findings: %s   Pages observed: %s   Queries observed: %s' % (
        summary.get('actions', 0), summary.get('technical_findings', 0),
        summary.get('pages_observed', 0), summary.get('queries_observed', 0)), size=9)
    write('Actions are review candidates. Search Console figures measure this property and period; they do not predict traffic.',
          size=9, color=gray)

    section('Priority actions')
    if not audit.get('actions'):
        write('No evidence-backed actions in the available observations.', color=gray)
    for index, item in enumerate(audit.get('actions', []), 1):
        if y < 125:
            next_page()
        write('%s. %s' % (index, item.get('title', '')), size=10, color=navy)
        write('ID: %s | %s | %s' % (item.get('id'), item.get('kind'), item.get('status')),
              size=8, color=gray, indent=10)
        if item.get('existing_url'):
            write('Existing page: ' + item['existing_url'], size=8, indent=10)
        metrics = (item.get('evidence') or {}).get('measurements') or {}
        write('GSC: %s impressions, %s clicks, %.2f%% CTR, average position %s' % (
            metrics.get('impressions', 'unknown'), metrics.get('clicks', 'unknown'),
            100 * (metrics.get('ctr') or 0), metrics.get('average_position', 'unknown')),
            size=8, indent=10)
        evidence = item.get('evidence') or {}
        write('Source: %s | observed %s' % (evidence.get('source_url'), evidence.get('observed_at') or 'unknown'),
              size=8, color=gray, indent=10)
        write('Method: ' + _text(evidence.get('method'), 800), size=8, indent=10)
        for caveat in (evidence.get('caveats') or [])[:5]:
            write('Caveat: ' + _text(caveat, 500), size=8, color=gray, indent=10)
        y -= 6

    section('Technical findings')
    if not audit.get('technical_findings'):
        write('No technical findings in the available crawl sample.', color=gray)
    for item in audit.get('technical_findings', []):
        write(item.get('title', '') + ' - ' + item.get('url', ''), size=9)
        write('Observed: ' + json.dumps((item.get('evidence') or {}).get('measurements') or {},
                                        ensure_ascii=False, sort_keys=True), size=8, color=gray, indent=10)

    section('PageSpeed and CrUX')
    speed = audit.get('page_speed') or {}
    write('Status: ' + _text(speed.get('status') or 'unavailable'))
    for name, value in sorted((speed.get('measurements') or {}).items()):
        write(name.replace('_', ' ') + ': ' + str(value), size=9, indent=10)
    for note in speed.get('missing_data') or []:
        write(_text(note), size=8, color=gray, indent=10)

    section('Experience facts')
    facts = audit.get('verified_brief_facts') or []
    if not facts:
        write('No user-confirmed experience facts are available for briefs.', color=gray)
    for item in facts:
        write(_text(item.get('fact'), 1000), size=9)
        write('Source: ' + _text((item.get('source') or {}).get('url') or (item.get('source') or {}).get('reference')),
              size=8, color=gray, indent=10)

    section('Missing data and limitations')
    for note in audit.get('missing_data') or []:
        write('- ' + _text(note, 1000), size=8, color=gray)
    section('Canonical audit record')
    write('The following JSON preserves every field used by the report.', size=8, color=gray)
    for line in _audit_lines(audit):
        write(line, size=6.5, space=1.5, color=gray)
    footer()
    page.save()
    return buffer.getvalue()


def _xlsx(audit):
    def sheet(rows, *, header_row=1, widths=None, freeze=True):
        root = ET.Element('worksheet', xmlns='http://schemas.openxmlformats.org/spreadsheetml/2006/main')
        if freeze:
            views = ET.SubElement(root, 'sheetViews')
            ET.SubElement(ET.SubElement(views, 'sheetView', workbookViewId='0'), 'pane',
                          ySplit=str(header_row), topLeftCell='A' + str(header_row + 1),
                          activePane='bottomLeft', state='frozen')
        if widths:
            cols = ET.SubElement(root, 'cols')
            for index, width in enumerate(widths, 1):
                ET.SubElement(cols, 'col', min=str(index), max=str(index), width=str(width), customWidth='1')
        data = ET.SubElement(root, 'sheetData')
        for index, row in enumerate(rows, 1):
            element = ET.SubElement(data, 'row', r=str(index))
            for col, value in enumerate(row):
                attributes = {'r': chr(65 + col) + str(index)}
                if index == header_row:
                    attributes['s'] = '1'
                if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
                    cell = ET.SubElement(element, 'c', attributes)
                    ET.SubElement(cell, 'v').text = str(value)
                else:
                    attributes['t'] = 'inlineStr'
                    cell = ET.SubElement(element, 'c', attributes)
                    ET.SubElement(ET.SubElement(cell, 'is'), 't').text = _safe_cell(value)
        if rows and header_row <= len(rows):
            end = chr(64 + max(len(row) for row in rows))
            ET.SubElement(root, 'autoFilter', ref=f'A{header_row}:{end}{len(rows)}')
        return ET.tostring(root, encoding='utf-8', xml_declaration=True)
    action_rows = [
        ['RankMe evidence audit - action tracker'],
        ['Site', audit.get('site_url')], ['Observed at', audit.get('observed_at') or 'unknown'], [],
        ['ID', 'Priority', 'Kind', 'Query', 'Existing URL', 'Proposed review', 'Status',
         'Impressions', 'Clicks', 'CTR', 'Avg position', 'Evidence observed', 'Source URL',
         'Method', 'Caveats', 'Owner', 'Review decision', 'Next step']]
    for item in audit.get('actions', []):
        evidence = item.get('evidence') or {}
        metrics = evidence.get('measurements') or {}
        action_rows.append([item['id'], item.get('priority'), item['kind'], item['query'],
                            item['existing_url'], item['title'], item['status'],
                            metrics.get('impressions'), metrics.get('clicks'), metrics.get('ctr'),
                            metrics.get('average_position'), evidence.get('observed_at'),
                            evidence.get('source_url'), evidence.get('method'),
                            ' | '.join(evidence.get('caveats') or []), '', '', ''])
    finding_rows = [['ID', 'Kind', 'Finding', 'URL', 'Observed at', 'Measurements', 'Caveats']]
    for item in audit.get('technical_findings', []):
        evidence = item.get('evidence') or {}
        finding_rows.append([item['id'], item['kind'], item['title'], item['url'],
                             evidence.get('observed_at'),
                             json.dumps(evidence.get('measurements') or {}, ensure_ascii=False, allow_nan=False),
                             ' | '.join(evidence.get('caveats') or [])])
    speed = audit.get('page_speed') or {}
    metric_rows = [['Measure', 'Value', 'Status or note', 'Source URL'],
                   ['PageSpeed / CrUX status', '', speed.get('status'), speed.get('source_url')]]
    for name, value in sorted((speed.get('measurements') or {}).items()):
        metric_rows.append([name, value, '', speed.get('source_url')])
    for item in audit.get('missing_data') or []:
        metric_rows.append(['Missing data', '', item, ''])
    raw = json.dumps(audit, ensure_ascii=False, allow_nan=False, sort_keys=True)
    raw_rows = [['Canonical audit JSON, sequential chunks']] + [[raw[i:i + 30000]] for i in range(0, len(raw), 30000)]
    sheets = [('Actions', action_rows, 5, [30, 10, 22, 24, 42, 48, 20, 16, 12, 12, 14, 22, 38, 55, 55, 18, 20, 32]),
              ('Technical findings', finding_rows, 1, [30, 25, 45, 42, 22, 55, 55]),
              ('Measurements', metric_rows, 1, [32, 18, 75, 40]),
              ('Audit JSON', raw_rows, 1, [100])]
    content_types = ET.Element('Types', xmlns='http://schemas.openxmlformats.org/package/2006/content-types')
    ET.SubElement(content_types, 'Default', Extension='rels', ContentType='application/vnd.openxmlformats-package.relationships+xml')
    ET.SubElement(content_types, 'Default', Extension='xml', ContentType='application/xml')
    ET.SubElement(content_types, 'Override', PartName='/xl/workbook.xml', ContentType='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml')
    ET.SubElement(content_types, 'Override', PartName='/xl/styles.xml', ContentType='application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml')
    for i in range(1, len(sheets) + 1):
        ET.SubElement(content_types, 'Override', PartName=f'/xl/worksheets/sheet{i}.xml', ContentType='application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml')
    workbook = ET.Element('workbook', {'xmlns': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main',
                                       'xmlns:r': 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'})
    names = ET.SubElement(workbook, 'sheets')
    for i, (name, _, _, _) in enumerate(sheets, 1):
        ET.SubElement(names, 'sheet', {'name': name, 'sheetId': str(i), 'r:id': f'rId{i}'})
    rels = ET.Element('Relationships', xmlns='http://schemas.openxmlformats.org/package/2006/relationships')
    for i in range(1, len(sheets) + 1):
        ET.SubElement(rels, 'Relationship', Id=f'rId{i}', Type='http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet', Target=f'worksheets/sheet{i}.xml')
    ET.SubElement(rels, 'Relationship', Id=f'rId{len(sheets) + 1}', Type='http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles', Target='styles.xml')
    root_rels = ET.Element('Relationships', xmlns='http://schemas.openxmlformats.org/package/2006/relationships')
    ET.SubElement(root_rels, 'Relationship', Id='rId1', Type='http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument', Target='xl/workbook.xml')
    styles = ET.Element('styleSheet', xmlns='http://schemas.openxmlformats.org/spreadsheetml/2006/main')
    fonts = ET.SubElement(styles, 'fonts', count='2')
    ET.SubElement(ET.SubElement(fonts, 'font'), 'sz', val='11')
    header_font = ET.SubElement(fonts, 'font')
    ET.SubElement(header_font, 'b')
    ET.SubElement(header_font, 'color', rgb='FFFFFFFF')
    ET.SubElement(header_font, 'sz', val='11')
    fills = ET.SubElement(styles, 'fills', count='3')
    ET.SubElement(ET.SubElement(fills, 'fill'), 'patternFill', patternType='none')
    ET.SubElement(ET.SubElement(fills, 'fill'), 'patternFill', patternType='gray125')
    ET.SubElement(ET.SubElement(fills, 'fill'), 'patternFill', patternType='solid').append(
        ET.Element('fgColor', rgb='FF153047'))
    borders = ET.SubElement(styles, 'borders', count='1')
    ET.SubElement(borders, 'border')
    cell_style_xfs = ET.SubElement(styles, 'cellStyleXfs', count='1')
    ET.SubElement(cell_style_xfs, 'xf', numFmtId='0', fontId='0', fillId='0', borderId='0')
    cell_xfs = ET.SubElement(styles, 'cellXfs', count='2')
    ET.SubElement(cell_xfs, 'xf', numFmtId='0', fontId='0', fillId='0', borderId='0', xfId='0')
    ET.SubElement(cell_xfs, 'xf', numFmtId='0', fontId='1', fillId='2', borderId='0', xfId='0', applyFill='1', applyFont='1')
    cell_styles = ET.SubElement(styles, 'cellStyles', count='1')
    ET.SubElement(cell_styles, 'cellStyle', name='Normal', xfId='0', builtinId='0')
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as book:
        book.writestr('[Content_Types].xml', ET.tostring(content_types, encoding='utf-8', xml_declaration=True))
        book.writestr('_rels/.rels', ET.tostring(root_rels, encoding='utf-8', xml_declaration=True))
        book.writestr('xl/workbook.xml', ET.tostring(workbook, encoding='utf-8', xml_declaration=True))
        book.writestr('xl/_rels/workbook.xml.rels', ET.tostring(rels, encoding='utf-8', xml_declaration=True))
        book.writestr('xl/styles.xml', ET.tostring(styles, encoding='utf-8', xml_declaration=True))
        for i, (_, rows, header_row, widths) in enumerate(sheets, 1):
            book.writestr(f'xl/worksheets/sheet{i}.xml', sheet(rows, header_row=header_row, widths=widths))
    return buffer.getvalue()


def render_report(audit, format):
    """Export the same canonical audit as Markdown, PDF, or XLSX bytes."""
    json.dumps(audit, ensure_ascii=False, allow_nan=False)
    if format in ('md', 'markdown'):
        return _markdown(audit)
    if format == 'pdf':
        return _pdf(audit)
    if format == 'xlsx':
        return _xlsx(audit)
    raise ValueError('Unsupported audit format')
