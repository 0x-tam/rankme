"""Durable observations and the integration boundary for page experiments."""
import hashlib
from datetime import datetime, timezone
from urllib.parse import urlsplit

from .store import now, uid


def observation(client_id, source, snapshot, key=None, observed_at=None):
    identity = client_id + ':' + source + ':' + (key or uid())
    return {'id': hashlib.sha256(identity.encode()).hexdigest()[:32],
            'client_id': client_id, 'source': source, 'observed_at': observed_at or now(),
            'snapshot': snapshot}


def save_snapshot(store, table, client_id, snapshot, key=None):
    """Save the latest view and immutable evidence in one database transaction."""
    record = observation(client_id, table, snapshot, key)
    with store.lock:
        try:
            return store.get('measurements', record['id'])
        except KeyError:
            pass
        saved = store.put_many([(table, {**snapshot, 'id': client_id, 'client_id': client_id}),
                                ('measurements', record)])
        return saved[1]


def backfill(store):
    # Old versions kept only one snapshot. Preserve that one, without inventing history.
    for table in ('seo', 'research', 'answer_probes'):
        for row in store.all(table):
            key = 'legacy:' + row['id']
            ident = observation(row.get('client_id', row['id']), table, {}, key)['id']
            try:
                store.get('measurements', ident)
            except KeyError:
                # A new app installation has no legacy data; only backfill a snapshot
                # when no history for this client/source exists.
                if not any(x['source'] == table for x in store.history(row.get('client_id', row['id']))):
                    store.put('measurements', observation(row.get('client_id', row['id']), table, row,
                        key, row.get('observed_at') or row.get('updated_at')))


def page_identity(url):
    p = urlsplit(url or '')
    port = p.port
    if port == (443 if p.scheme == 'https' else 80):
        port = None
    return (p.scheme.lower(), (p.hostname or '').lower(), port,
            p.path or '/', p.query)


def active_for(store, client_id, page_url=None):
    return [r for r in store.all('experiments') if r['client_id'] == client_id
            and r.get('status') == 'observing' and
            (page_url is None or page_identity(r['page_url']) == page_identity(page_url))]


def matching_measurement(client, seo):
    measured = seo.get('conversion_measurement') or {}
    goal = client.get('conversion_goal') or {}
    recorded = measured.get('goal') or {}
    if not goal or any(recorded.get(k) != goal.get(k) for k in ('event_name', 'landing_page', 'goal_type')):
        return {'status': 'unconfigured' if not goal else 'unavailable',
                'issues': ['Sync Google after configuring or changing the conversion goal.']}
    expected = str(client.get('seo_connection', {}).get('ga4_property', '')).removeprefix('properties/')
    actual = str(seo.get('ga4_property', '')).removeprefix('properties/')
    if not expected or expected != actual:
        return {'status': 'unavailable', 'issues': ['Sync the selected GA4 property to measure this goal.']}
    if measured.get('status') == 'measured':
        try:
            age = (datetime.now(timezone.utc).date() - datetime.fromisoformat(measured['current']['end']).date()).days
        except (KeyError, TypeError, ValueError):
            age = 999
        if not 0 <= age <= 7:
            return {**measured, 'status': 'unavailable', 'issues': ['The goal evidence is stale or undated. Sync Google before using it.']}
    return measured


def start(engine, client_id, page_url, hypothesis, change, article_id=None, captured=None):
    from .experiments import start_experiment
    with engine.guard:
        store = engine.store
        client = store.get('clients', client_id)
        try:
            seo = store.get('seo', client_id)
        except KeyError:
            seo = {}
        started_at = now()
        if captured:
            seo = captured['snapshot']
            started_at = captured['started_at']
        # Captured evidence is checked relative to its actual publication time below.
        if captured:
            measured = seo.get('conversion_measurement') or {}
            if any((measured.get('goal') or {}).get(k) != (client.get('conversion_goal') or {}).get(k)
                   for k in ('event_name', 'landing_page', 'goal_type')) or str(seo.get('ga4_property', '')).removeprefix('properties/') != str(client.get('seo_connection', {}).get('ga4_property', '')).removeprefix('properties/'):
                measured = {}
        else:
            measured = matching_measurement(client, seo)
        if measured.get('status') != 'measured':
            raise ValueError('Sync a configured conversion goal before starting an experiment')
        baseline = measured.get('current') or {}
        # A stale measurement is not an adequate baseline for a new change.
        try:
            end = datetime.fromisoformat(baseline['end']).date()
            age = (datetime.fromisoformat(started_at).date() - end).days
        except (KeyError, TypeError, ValueError):
            age = 999
        if not 0 <= age <= 7:
            raise ValueError('Sync a recent baseline before starting an experiment')
        record = start_experiment(client_id, page_url, hypothesis, change, baseline,
                                  store.all('experiments'), client.get('conversion_goal'), started_at)
        record.setdefault('id', uid())
        record['ga4_property'] = seo.get('ga4_property')
        if article_id:
            record['article_id'] = article_id
        store.put_many([('experiments', record), ('measurements', observation(client_id, 'experiment', record,
            'start:' + record['id']))])
        from .autopilot import rebuild
        rebuild(engine, client_id)
        return store.get('experiments', record['id'])


def cancel(engine, ident):
    with engine.guard:
        row = engine.store.get('experiments', ident)
        if row.get('status') != 'observing':
            raise ValueError('Only an observing experiment can be cancelled')
        row.update(status='cancelled', finished_at=now(), interpretation='Cancelled by the operator; no conclusion.')
        engine.store.put_many([('experiments', row), ('measurements', observation(row['client_id'],
            'experiment', row, 'cancel:' + ident))])
        from .autopilot import rebuild
        rebuild(engine, row['client_id'])
        return row


def evaluate_all(engine, client_id, snapshot, observation_id):
    from .experiments import evaluate
    with engine.guard:
        client = engine.store.get('clients', client_id)
        measured = matching_measurement(client, snapshot)
        current = measured.get('current') if measured.get('status') == 'measured' else {}
        for row in active_for(engine.store, client_id):
            same_property = str(row.get('ga4_property', '')).removeprefix('properties/') == str(snapshot.get('ga4_property', '')).removeprefix('properties/')
            updated = evaluate(row, (current or {}) if same_property else {}, now())
            updated['measurement_id'] = observation_id
            engine.store.put_many([('experiments', updated), ('measurements', observation(client_id,
                'experiment', updated, 'evaluate:' + row['id'] + ':' + observation_id))])


def record_publication(engine, client, article, result):
    """Record a change once; begin observation only after verified publication."""
    store = engine.store
    key = article['id'] + ':' + str(result.get('status'))
    store.put('measurements', observation(client['id'], 'change', {
        'article_id': article['id'], 'refresh_of': article.get('refresh_of'),
        'title': article.get('title'), 'live_url': result.get('live_url'),
        'status': result.get('status'), 'file_sha256': result.get('file_sha256'),
        'note': 'Publication observation; this does not establish a performance effect.'}, key))
    if result.get('status') != 'published' or not article.get('refresh_of'):
        return
    if any(r.get('article_id') == article['id'] for r in store.all('experiments')):
        return
    goal = client.get('conversion_goal') or {}
    if not goal or page_identity(goal.get('landing_page')) != page_identity(result.get('live_url')):
        return
    try:
        start(engine, client['id'], result['live_url'], 'Refresh improves the selected business event per organic session.',
              'Published reviewed refresh: ' + article.get('title', ''), article_id=article['id'],
              captured=article.get('change_observation'))
    except ValueError as exc:
        store.event('Refresh published, but conversion experiment could not start: ' + str(exc), client['id'], 'warning')
        store.put('measurements', observation(client['id'], 'experiment_unmeasured',
            {'article_id': article['id'], 'reason': str(exc)}, article['id']))
