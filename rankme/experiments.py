"""Pure conversion-goal and controlled-change evaluation helpers.

Selected-event occurrences per organic landing session are a descriptive ratio,
not a unique-session conversion rate. Outcomes never establish causality or
statistical significance. Callers persist returned records and serialize starts.
"""
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import hashlib
import math
import re
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .crawler import normalize_url

DEFAULTS = {'minimum_days': 14, 'min_sessions': 100, 'min_conversions': 5}
MAX_DAYS = 90
EFFECT_THRESHOLD = .20
ACTIVE = {'observing'}
NOTE = ('Selected-event occurrences per organic landing session; repeated events are possible. '
        'Observed association only, not a unique-session conversion rate, significance test or causal estimate. '
        'Seasonality, tracking changes and other site changes may explain differences.')


def _day(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise ValueError('A valid ISO observation date is required')
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).date() if 'T' in value else date.fromisoformat(value)
    except ValueError:
        raise ValueError('A valid ISO observation date is required')


def _zone(row):
    metadata = (row.get('metadata') or {}) if isinstance(row, dict) else {}
    if not isinstance(metadata, dict):
        return ''
    values = [metadata.get('timeZone')]
    nested = [metadata.get(key, {}) for key in ('organic_sessions', 'conversions')]
    nested_zones = [item.get('timeZone') if isinstance(item, dict) else None for item in nested]
    if any(nested_zones) and not all(nested_zones):
        raise ValueError('Measurement timezone metadata is missing for one metric')
    for key in ('organic_sessions', 'conversions'):
        if isinstance(metadata.get(key), dict):
            values.append(metadata[key].get('timeZone'))
    names = {value for value in values if isinstance(value, str) and value}
    if len(names) > 1:
        raise ValueError('Measurement metrics have conflicting property timezones')
    if not names:
        return ''
    name = names.pop()
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError('Measurement property timezone is unrecognized') from None
    return name


def _local_day(value, zone):
    instant = value if isinstance(value, datetime) else None
    if isinstance(value, str) and 'T' in value:
        instant = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if instant is not None and instant.tzinfo is not None:
        return instant.astimezone(ZoneInfo(zone) if zone else timezone.utc).date()
    return _day(value)


def _timestamp(value):
    return isinstance(value, datetime) or isinstance(value, str) and 'T' in value


def _page(value):
    # Validate public URL syntax, then preserve the exact observation identity.
    # GA dimensions distinguish host, trailing slash and query; do not collapse them.
    normalize_url(value)
    parsed = urlsplit(value)
    scheme, port = parsed.scheme.lower(), parsed.port
    if (scheme == 'https' and port == 443) or (scheme == 'http' and port == 80):
        port = None
    return (scheme, (parsed.hostname or '').lower(), port, parsed.path or '/', parsed.query)


def _count(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) and value >= 0 else None


def validate_goal(config, site_url):
    """Validate a single named business event and same-site destination.

    An empty dictionary/None disables the goal. Minimum days is capped at 28
    because the current connector supplies fixed 28-day windows. Counts alone cannot prove event
    instrumentation or CTA readiness; those require independent validation.
    """
    if config is None or config == {}:
        return {}
    allowed = {'event_name', 'landing_page', 'goal_type', *DEFAULTS}
    if not isinstance(config, dict) or set(config) - allowed:
        raise ValueError('Unknown conversion goal settings')
    event = config.get('event_name')
    if not isinstance(event, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,39}', event):
        raise ValueError('Use a named event of 1–40 letters, digits or underscores, starting with a letter')
    if config.get('goal_type') not in {'signup', 'purchase', 'booking', 'lead'}:
        raise ValueError('Choose signup, purchase, booking or lead')
    landing = config.get('landing_page')
    if not isinstance(landing, str) or len(landing) > 2048 or not landing.startswith(('https://', 'http://')):
        raise ValueError('The goal landing page must be an absolute HTTP(S) URL')
    if urlsplit(landing).fragment:
        raise ValueError('The goal landing page must not include a fragment')
    landing = normalize_url(landing)
    if _page(landing)[1] != _page(site_url)[1]:
        raise ValueError('The goal landing page must belong to this website')
    result = {**DEFAULTS, **config, 'landing_page': landing}
    for key, minimum, maximum in (('minimum_days', 7, 28), ('min_sessions', 10, 1000000), ('min_conversions', 1, 100000)):
        value = result[key]
        if type(value) is not int or not minimum <= value <= maximum:
            raise ValueError(key + ' is outside the allowed range')
    return result


def ensure_available(existing, client_id, page_url):
    """Raise if an observing experiment already owns this client's exact page."""
    page = _page(page_url)
    for row in existing:
        if row.get('client_id') != client_id or row.get('status') not in ACTIVE:
            continue
        if _page(row.get('page_url', '')) == page:
            raise ValueError('This page already has an active experiment; finish or close it first')


def _metric(row, goal):
    if not isinstance(row, dict):
        return None, 'Measurement is unavailable'
    try:
        if _page(row.get('landing_page', '')) != _page(goal['landing_page']) or row.get('event_name') != goal['event_name']:
            return None, 'Measurement belongs to a different landing page or named event'
        start, end = _day(row.get('start')), _day(row.get('end'))
    except (ValueError, KeyError, TypeError):
        return None, 'Measurement dates or page are missing or invalid'
    metadata = row.get('metadata') or {}
    if isinstance(metadata, dict):
        checks = [metadata] + [value for value in metadata.values() if isinstance(value, dict)]
        if any(info.get('subjectToThresholding') or info.get('dataLossFromOtherRow') or info.get('samplingMetadatas') for info in checks):
            return None, 'Provider reports thresholding, sampling or row loss; inspect data quality before comparing'
    sessions, events = _count(row.get('organic_sessions')), _count(row.get('conversions'))
    if end < start or sessions is None or events is None:
        return None, 'Measurement dates or counts are missing or invalid'
    if sessions == 0 and events > 0:
        return None, 'Positive event counts with zero sessions are inconsistent'
    return {'start': start, 'end': end, 'days': (end - start).days + 1,
            'sessions': sessions, 'events': events, 'ratio': events / sessions if sessions else None}, ''


def start_experiment(client_id, page_url, hypothesis, change, baseline, existing, goal, started_at):
    """Snapshot the pre-change evidence. The caller has already made the one change.

    Snapshot dates must end before the change date. The returned observing record
    is intentionally independent of current provider state and mutable settings.
    """
    goal = validate_goal(goal, page_url)
    if not goal or _page(page_url) != _page(goal['landing_page']):
        raise ValueError('Configure a conversion goal for this exact landing page first')
    if not isinstance(client_id, str) or not client_id:
        raise ValueError('An owning client is required')
    for value in (hypothesis, change):
        if not isinstance(value, str) or not value.strip() or len(value) > 4000:
            raise ValueError('Provide a hypothesis and one concrete change, each up to 4000 characters')
    started = _local_day(started_at, _zone(baseline))
    metric, error = _metric(baseline, goal)
    if error:
        raise ValueError('Cannot start without a measured baseline: ' + error)
    if metric['end'] >= started:
        raise ValueError('The baseline must end before the change date')
    if (started - metric['end']).days > 7:
        raise ValueError('The baseline is stale; sync a window ending within seven days before the change')
    ensure_available(existing, client_id, page_url)
    timestamp = started_at.isoformat() if isinstance(started_at, (date, datetime)) else started_at
    identity = hashlib.sha256((client_id + '\n' + repr(_page(page_url)) + '\n' + timestamp).encode()).hexdigest()[:32]
    return {'id': identity, 'client_id': client_id, 'page_url': normalize_url(page_url),
            'hypothesis': hypothesis.strip(), 'change': change.strip(), 'started_at': timestamp,
            'status': 'observing', 'goal': deepcopy(goal), 'baseline': deepcopy(baseline),
            'evaluation': {'status': 'observing', 'reason': 'Waiting for an eligible post-change observation window', 'note': NOTE}}


def evaluate(experiment, current, as_of):
    """Return an updated copy; never reinterpret a completed experiment.

    current accepts a measured landing-page row, or conversion_measurement wrapper.
    Complete comparable windows and sufficient sessions/baseline events are needed
    for directional outcomes. Low samples/no clear change remain observing until
    day 90, then conclude inconclusive. No missing count is interpreted as zero.
    """
    result = deepcopy(experiment)
    if result.get('status') not in ACTIVE:
        return result
    today, started = _day(as_of), _day(result['started_at'])
    if today < started:
        raise ValueError('Evaluation cannot precede the change')
    elapsed = (today - started).days
    status = 'inconclusive' if elapsed >= MAX_DAYS else 'observing'
    evaluation = {'status': status, 'evaluated_at': str(as_of), 'days_since_change': elapsed, 'note': NOTE}

    def finish(reason, **fields):
        evaluation.update(reason=reason, **fields)
        result.update(status=evaluation['status'], evaluation=evaluation)
        if evaluation['status'] != 'observing':
            result['completed_at'] = str(as_of)
        return result

    goal = result['goal']
    if isinstance(current, dict) and 'current' in current:
        if current.get('status') != 'measured':
            return finish('Selected-event measurement is unavailable')
        current = current['current']
    baseline, error = _metric(result.get('baseline'), goal)
    if error:
        return finish('Baseline evidence is invalid: ' + error)
    observation, error = _metric(current, goal)
    if error:
        return finish(error)
    try:
        zone = _zone(result.get('baseline'))
        if zone != _zone(current):
            return finish('Baseline and current property timezones differ or timezone metadata is missing')
        started = _local_day(result['started_at'], zone)
        today = _local_day(as_of, zone)
    except ValueError as exc:
        return finish(str(exc))
    # Without a property timezone, the UTC following day can still contain the
    # local change. Date-only inputs explicitly represent measurement dates.
    change_cutoff = started + timedelta(days=1 if not zone and _timestamp(result['started_at']) else 0)
    elapsed = (today - started).days
    evaluation.update(status='inconclusive' if elapsed >= MAX_DAYS else 'observing', days_since_change=elapsed)
    evaluation.update(change_day=started.isoformat(), property_timezone=zone or None,
                      first_eligible_observation_day=(change_cutoff + timedelta(days=1)).isoformat())
    if observation['end'] > today:
        return finish('Observation contains future dates')
    if observation['end'] > started + timedelta(days=MAX_DAYS):
        return finish('The observation extends beyond the 90-day experiment horizon; no directional conclusion')
    if (today - observation['end']).days > 7:
        return finish('Post-change evidence is stale; sync a window ending within the past seven days')
    if observation['start'] <= change_cutoff or observation['start'] <= baseline['end']:
        return finish('The observation window includes the change day or earlier days; wait for a wholly post-change window')
    if observation['days'] != baseline['days']:
        return finish('Baseline and post-change windows must have equal duration')
    if min(observation['days'], baseline['days']) < goal.get('minimum_days', DEFAULTS['minimum_days']):
        return finish('The observation windows are shorter than the configured minimum')
    if min(observation['sessions'], baseline['sessions']) < goal.get('min_sessions', DEFAULTS['min_sessions']):
        return finish('Insufficient organic landing-page sessions in one or both windows')
    if baseline['events'] < goal.get('min_conversions', DEFAULTS['min_conversions']):
        return finish('Insufficient baseline selected-event occurrences for a stable descriptive comparison')
    change = observation['ratio'] / baseline['ratio'] - 1
    fields = {'baseline_events_per_session': baseline['ratio'], 'current_events_per_session': observation['ratio'],
              'relative_change': change, 'baseline_sessions': baseline['sessions'], 'current_sessions': observation['sessions'],
              'baseline_conversions': baseline['events'], 'current_conversions': observation['events'],
              'current': deepcopy(current), 'threshold': EFFECT_THRESHOLD}
    if abs(change) + 1e-12 < EFFECT_THRESHOLD:
        return finish('No clear descriptive change beyond the 20% effect threshold', **fields)
    evaluation['status'] = 'improved' if change > 0 else 'declined'
    return finish('Observed event occurrences per session increased' if change > 0 else 'Observed event occurrences per session decreased', **fields)


def enrich_opportunities(opportunities, goal, conversion_measurement):
    """Add modest, explainable conversion-evidence priority on the exact goal URL.

    This is evidence of a relevant measurable business outcome, not expected ROI.
    Missing/failed measurements and unrelated pages receive no score adjustment.
    """
    rows = deepcopy(opportunities)
    if not goal or not isinstance(conversion_measurement, dict) or conversion_measurement.get('status') != 'measured':
        return rows
    current = conversion_measurement.get('current')
    metric, error = _metric(current, goal)
    if error or metric['sessions'] == 0:
        return rows
    for item in rows:
        try:
            url = item.get('url')
            if not url or _page(url) != _page(goal['landing_page']):
                continue
        except ValueError:
            continue
        score = _count(item.get('score'))
        if score is None:
            continue
        # Repeated enrichment replaces this component rather than compounding it.
        base = item.get('conversion_base_score', score)
        bonus = 10 if metric['events'] > 0 else 3
        item['conversion_base_score'] = base
        item['score'] = min(100, base + bonus)
        item['conversion_evidence'] = {**deepcopy(current), 'events_per_session': metric['ratio'], 'note': NOTE}
        item['conversion_score_reason'] = (f'+{bonus} priority points: exact configured goal landing page has '
            + ('observed selected-event occurrences' if metric['events'] > 0 else 'measured organic sessions but zero selected-event occurrences; check tracking and CTA readiness')
            + '. Heuristic relevance boost, not revenue prediction.')
    return rows
