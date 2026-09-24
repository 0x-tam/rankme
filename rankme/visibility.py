"""Bounded, deterministic visibility triage from observations; never performs writes/network IO.

Scores are editorial prioritization heuristics, not search-engine scores or forecasts.
All source text is untrusted data. Recommendations require contextual review before edits.
"""
from collections import Counter, defaultdict
import hashlib
import math
import re
from urllib.parse import urlsplit, urlunsplit

_LIMIT = 100
_STOP = set('the and for with from this that your our how what when why are can into about guide a an of to in on is'.split())


def _text(value, limit=500):
    return ' '.join(str(value or '').split())[:limit]


def _url(value):
    try:
        p = urlsplit(str(value or ''))
        if p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password:
            return ''
        return urlunsplit((p.scheme, p.netloc.lower(), p.path or '/', p.query, ''))[:2000]
    except ValueError:
        return ''


def _num(value):
    try:
        number = float(value)
        return number if math.isfinite(number) and number >= 0 else None
    except (TypeError, ValueError):
        return None


def _rows(value, limit=5000):
    return [row for row in value[:limit] if isinstance(row, dict)] if isinstance(value, list) else []


def _tokens(value):
    return set(re.findall(r'[^\W_]{3,}', _text(value, 3000).casefold())) - _STOP


def _same_site(url, host):
    return bool(url) and (urlsplit(url).hostname or '').removeprefix('www.') == host


def _capabilities():
    specs = [
        ('Keyword Research', 'partial', 'Observed Search Console queries, page vocabulary and public customer-question research with checked source quotations; external volumes require a provider.'),
        ('Search Console', 'implemented', 'Authorized current/prior query, page and joint query/page imports; availability depends on the connected property and successful sync.'),
        ('AI Mentions', 'integration_required', 'Requires dated prompt/model/locale observations from configured answer providers.'),
        ('Citation Gaps', 'integration_required', 'Public citation-source research is available; actual AI citation gaps require dated answer-provider responses and citation URLs.'),
        ('Opportunity Scoring', 'implemented', 'Deterministic impact/confidence/effort heuristic with evidence and explanations.'),
        ('Topical Authority', 'partial', 'Lexical coverage clusters from crawled titles; not a search-engine authority score.'),
        ('Content Briefs', 'partial', 'Evidence-backed outlines and source requirements; verified public research can create planned articles. Publication remains gated by the configured adapter and quality checks.'),
        ('LLM Structure', 'partial', 'Checks observed heading structure when available; no AI ranking promises.'),
        ('Internal Links', 'partial', 'Suggests relevant missing edges between observed pages; does not modify the site.'),
        ('Cannibalization', 'partial', 'Joint query/page overlap or lexical review candidates; overlap alone does not prove harm.'),
        ('Content Refresh', 'partial', 'Prioritizes observed comparable-window click declines; no automatic overwrite.'),
        ('Technical Audits', 'partial', 'Metadata and available status/indexability evidence; crawl coverage is bounded.'),
        ('Link Outreach', 'partial', 'Existing researched prospects and drafts; sending requires an authorized mail integration.'),
        ('Link Exchange', 'partial', 'Public editorial-partnership research and action briefs are available; relationship verification and authorized sending remain pending. No automated reciprocal schemes.'),
        ('SEO Reporting', 'implemented', 'Aggregates observed issues, opportunities, source timestamps and coverage limitations.'),
        ('Recover Lost Links', 'partial', 'Flags previously seen hyperlinks now missing; failed checks are not treated as losses.'),
        ('Unlinked Mentions', 'partial', 'Public mention research checks supporting quotations; absence of a hyperlink and outreach suitability require separate verification. No automated sending.'),
        ('Free Tools', 'partial', 'Research identifies source-supported tool ideas and prepares action briefs; tool implementation, testing and a publishing adapter remain required.'),
        ('Listicle Outreach', 'partial', 'Public listicle research checks source quotations and prepares action briefs; fit, omissions and an authorized sending integration remain required.'),
        ('Competitor Links', 'partial', 'Public competitor-link research and action briefs are available; complete backlink coverage and actual link verification require additional evidence.'),
        ('Broken Link Building', 'partial', 'Public broken-reference candidates and action briefs are available; failed target checks and replacement suitability must be verified before outreach.'),
        ('Fix Outdated Guides', 'partial', 'Traffic-decline analysis and source-checked public guide research suggest reviews; fact freshness must be verified. Owned published articles can receive separate refresh drafts.'),
        ('Statistics Page', 'partial', 'Source-checked statistics-page opportunities can create planned articles; figures, methodology, dates and publication quality checks remain required.'),
        ('Reporter Outreach', 'partial', 'Public reporter-request research checks supporting quotations and prepares action briefs; deadlines, verified expertise and authorized sending remain required.'),
        ('Infographics', 'partial', 'Source-supported infographic ideas and action briefs are available; validated data, accessible graphic production and publishing remain pending.'),
        ('Links', 'partial', 'Observed crawl edges and backlink status inventory; not a complete web link index.'),
    ]
    result = [dict(id=index, name=name, status=status, detail=detail)
            for index, (name, status, detail) in enumerate(specs, 1)]
    for capability in result:
        if capability['id'] in (13, 14, 19, 24):
            capability.update(status='out_of_scope', detail='Outreach excluded by request. No new outreach research, drafts, or sending in visibility automation.')
    return result


def analyze(client, pages, seo=None, backlinks=None, articles=None):
    """Analyze one site's crawl + GoogleData.sync schema + stored backlink/article rows.

    Accept a crawl dictionary or its page list. Optional current.query_pages rows have
    query/page/clicks/impressions/position fields; independent aggregates never join.
    No wall-clock reads make identical observations produce identical reports.
    """
    client = client if isinstance(client, dict) else {}
    seo = seo if isinstance(seo, dict) else {}
    crawl = pages if isinstance(pages, dict) else {'pages': pages}
    host = (urlsplit(_url(client.get('url'))).hostname or '').removeprefix('www.')
    records = {}
    for row in _rows(crawl.get('pages'), 60):
        url = _url(row.get('url'))
        if _same_site(url, host):
            records[url] = row
    links = [row for row in _rows(backlinks, 500) if row.get('client_id', client.get('id')) == client.get('id')]
    drafts = [row for row in _rows(articles, 500) if row.get('client_id', client.get('id')) == client.get('id')]
    search = seo.get('search_console') or {}
    # A caller must not accidentally mix another customer's private query data.
    if seo.get('client_id', client.get('id')) != client.get('id'):
        search = {}
        seo = {}
    current = search.get('current') or {}
    previous = search.get('previous') or {}
    queries = _rows(current.get('queries'))
    report = {key: [] for key in ('opportunities', 'technical_findings', 'keyword_opportunities',
        'topical_clusters', 'cannibalization', 'internal_links', 'refresh_candidates', 'briefs', 'link_opportunities', 'missing_data')}
    report.update(schema_version=1, capabilities=_capabilities(), coverage={
        'pages': len(records), 'queries': len(queries), 'backlinks': len(links), 'articles': len(drafts),
        'crawl_limited': bool(crawl.get('summary', {}).get('limited', True)),
        'search_limited': bool(search.get('limited', False)), 'seo_updated_at': seo.get('updated_at'),
        'periods': seo.get('periods', {}), 'note': 'Sampled public HTML and available observations only; absence is not proof of absence.'})

    def opportunity(kind, title, url, evidence, impact=3, confidence=0.7, effort=2):
        # A bounded priority, explicitly not predicted traffic or probability of ranking.
        score = round(100 * (impact / 5) * confidence / (1 + (effort - 1) * .25))
        identity = kind + '\n' + url + '\n' + title
        item = dict(id=hashlib.sha256(identity.encode()).hexdigest()[:20], kind=kind, title=title,
                    url=url, score=score, confidence=confidence, action='review', evidence=evidence,
                    score_explanation=f'Impact {impact}/5 × evidence confidence {confidence:g}, discounted by effort {effort}/5. Editorial heuristic; not expected traffic.')
        report['opportunities'].append(item)
        return item

    if not records:
        report['missing_data'].append('No usable same-site crawl pages; run website inspection.')
    if not search:
        report['missing_data'].append('Search Console observations unavailable; connect and sync an authorized property.')
    if not current.get('query_pages'):
        report['missing_data'].append('Joint query/page observations unavailable; separate query and page totals cannot establish competing URLs.')
    report['missing_data'].extend([
        'AI mentions and citation gaps are unmeasured without provider response observations.',
        'External keyword volumes, competitor backlinks and brand mentions require evidence providers.',
        'Canonical, robots, headings, status and schema checks apply only where crawl fields are available.',
    ])
    titles = defaultdict(list)
    vocabulary = {}
    inbound = Counter()
    for url, row in records.items():
        vocabulary[url] = _tokens(row.get('title'))
        if row.get('title'):
            titles[_text(row['title']).casefold()].append(url)
        observed_links = row.get('links') if isinstance(row.get('links'), list) else []
        for target in set(_url(link) for link in observed_links[:200] if isinstance(link, str)):
            if target in records and target != url:
                inbound[target] += 1
        checks = []
        for field in ('title', 'description'):
            if not _text(row.get(field)):
                checks.append(('missing_' + field, 'Missing ' + field, 2))
        status = _num(row.get('status'))
        if status is not None and status >= 400:
            checks.append(('http_error', 'Observed HTTP error', 5))
        if row.get('noindex') is True or any('noindex' in str(rule).lower().split(',') or 'noindex' in str(rule).lower().split() for rule in (row.get('robots') or [])):
            checks.append(('noindex', 'Check whether observed noindex is intentional', 4))
        if 'headings' in row and not row['headings']:
            checks.append(('headings', 'No headings observed; review answer structure', 2))
        canonical = _url(row.get('canonical'))
        if canonical and not _same_site(canonical, host):
            checks.append(('external_canonical', 'Review canonical pointing to another website', 4))
        for kind, title, impact in checks:
            evidence = {'source': 'crawl', 'url': url, 'observed': {key: row.get(key) for key in ('title', 'description', 'status', 'noindex', 'robots', 'headings', 'canonical') if key in row}}
            item = opportunity(kind, title, url, evidence, impact, .95, 1)
            report['technical_findings'].append(item)
    for title, urls in sorted(titles.items()):
        if len(urls) > 1:
            report['technical_findings'].append(opportunity('duplicate_title', 'Duplicate observed title', urls[0], {'title': title, 'urls': urls}, 3, .95, 2))
    for url in records:
        if inbound[url] == 0 and urlsplit(url).path != '/':
            report['technical_findings'].append(opportunity('possible_orphan', 'No incoming links in the crawl sample', url,
                {'source': 'crawl', 'sample_pages': len(records), 'note': 'Not proof of an orphan across the complete website.'}, 2, .5, 2))

    by_token = defaultdict(list)
    for url, tokens in vocabulary.items():
        for token in sorted(tokens):
            by_token[token].append(url)
    for token, urls in sorted(by_token.items(), key=lambda pair: (-len(pair[1]), pair[0])):
        if len(urls) >= 2:
            report['topical_clusters'].append({'topic': token, 'urls': sorted(urls), 'basis': 'Shared title vocabulary; lexical coverage heuristic.'})
    urls = sorted(records)
    for index, source in enumerate(urls):
        source_links = {_url(link) for link in (records[source].get('links') or [])[:200] if isinstance(link, str)}
        for target in urls[index + 1:]:
            shared = vocabulary[source] & vocabulary[target]
            union = vocabulary[source] | vocabulary[target]
            if len(shared) >= 2:
                if target not in source_links:
                    report['internal_links'].append(opportunity('internal_link', 'Review a contextual internal link', source,
                        {'source': 'crawl', 'target_url': target, 'shared_terms': sorted(shared), 'destination_observed': True}, 2, .6, 2))
                if union and len(shared) / len(union) >= .65:
                    report['cannibalization'].append(opportunity('topic_overlap', 'Review similar page intent before creating more content', source,
                        {'urls': [source, target], 'shared_terms': sorted(shared), 'basis': 'Lexical candidate only; competing rankings are not established.'}, 3, .45, 3))

    for row in queries:
        query = _text(row.get('query'))
        impressions, clicks, position = (_num(row.get(key)) for key in ('impressions', 'clicks', 'position'))
        ctr = clicks / impressions if impressions and clicks is not None else _num(row.get('ctr'))
        if not query or impressions is None or impressions < 100 or position is None or not 4 <= position <= 20 or ctr is None or ctr >= .02:
            continue
        evidence = {'source': 'search_console', 'query': query, 'impressions': impressions, 'clicks': clicks,
                    'ctr': ctr, 'position': position, 'period': seo.get('periods', {}).get('current'), 'updated_at': seo.get('updated_at')}
        item = opportunity('query_optimization', 'Review intent and snippet for “' + query + '”', '', evidence, 4, .85, 2)
        report['keyword_opportunities'].append(item)
        related = [url for url in urls if len(_tokens(query) & vocabulary[url]) >= 2][:5]
        report['briefs'].append({'title': 'Answer: ' + query, 'keyword': query, 'intent': 'Validate against search results', 'angle': 'Resolve the observed query with verified business expertise', 'cta_url': _url(client.get('url')), 'query': query, 'opportunity_id': item['id'], 'evidence': evidence,
            'existing_candidates': related, 'recommendation': 'Review existing coverage before commissioning a new page.',
            'outline': ['Direct answer to the observed query', 'Practical steps and original examples', 'Limitations and alternatives', 'Sources and next steps'],
            'requirements': ['Verify search intent with current results.', 'Use verified business facts and cite factual claims.', 'Do not invent statistics, quotes or customer results.', 'Add contextual links only after checking relevance.'],
            'source_requirements': 'Original evidence and dated authoritative sources are required before publication.'})

    grouped = defaultdict(dict)
    for row in _rows(current.get('query_pages')):
        query, url = _text(row.get('query')), _url(row.get('page'))
        if query and _same_site(url, host) and (_num(row.get('impressions')) or 0) > 0:
            grouped[query][url] = {'page': url, **{key: _num(row.get(key)) for key in ('clicks', 'impressions', 'position')}}
    for query, observed in sorted(grouped.items()):
        if len(observed) >= 2:
            report['cannibalization'].append(opportunity('query_overlap', 'Multiple URLs observed for “' + query + '”', '',
                {'query': query, 'rows': list(observed.values())[:10], 'source': 'search_console_query_page',
                 'note': 'Overlap is observed; harm and matching intent still require review.'}, 4, .8, 3))

    old = {_url(row.get('page')): row for row in _rows(previous.get('pages'))}
    periods = seo.get('periods') or {}
    # Supplied sync windows are equal length; custom imports must prove comparable periods.
    comparable = False
    try:
        from datetime import date
        cur, prev = periods['current'], periods['previous']
        current_days = (date.fromisoformat(cur['end']) - date.fromisoformat(cur['start'])).days
        previous_days = (date.fromisoformat(prev['end']) - date.fromisoformat(prev['start'])).days
        comparable = current_days >= 0 and current_days == previous_days and cur['start'] > prev['end']
    except (KeyError, TypeError, ValueError):
        pass
    if not comparable:
        report['missing_data'].append('Comparable nonoverlapping search periods unavailable; decline scoring skipped.')
    for row in _rows(current.get('pages')) if comparable else []:
        url = _url(row.get('page'))
        before, after = _num(old.get(url, {}).get('clicks')), _num(row.get('clicks'))
        if _same_site(url, host) and before is not None and before >= 10 and after is not None and after < before * .8:
            report['refresh_candidates'].append(opportunity('refresh', 'Investigate click decline before refreshing content', url,
                {'source': 'search_console', 'previous_clicks': before, 'current_clicks': after,
                 'change_percent': round((after / before - 1) * 100, 1), 'periods': periods,
                 'note': 'Check seasonality, indexing, intent and site changes; correlation does not establish cause.'}, 4, .8, 3))
    for link in links:
        verification = link.get('verification') or {}
        source = _url(link.get('source_url'))
        if not source:
            continue
        if verification.get('status') == 'missing' and link.get('last_seen_at'):
            report['link_opportunities'].append(opportunity('lost_link', 'Verify and reclaim a previously observed link', source,
                {'source': 'backlink_check', 'last_seen_at': link['last_seen_at'], 'checked_at': verification.get('checked_at'),
                 'target_url': _url(link.get('target_url')), 'note': 'Missing from fetched HTML; verify context before contacting the publisher.'}, 4, .85, 2))
        elif link.get('evidence', {}).get('verified') and link.get('status') == 'prospect':
            report['link_opportunities'].append(opportunity('editorial_prospect', 'Review verified editorial prospect', source,
                {'source': 'backlink_research', 'quote': _text(link['evidence'].get('quote'), 500),
                 'checked_at': link['evidence'].get('checked_at'), 'note': 'Draft only; authorized sending integration required.'}, 3, .7, 3))
    for key, value in report.items():
        if isinstance(value, list) and key != 'capabilities':
            if key == 'opportunities':
                value.sort(key=lambda item: (-item['score'], item['id']))
            report[key] = value[:_LIMIT]
    return report
