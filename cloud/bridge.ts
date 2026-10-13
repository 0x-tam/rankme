// Durable cloud queue and bearer-only outbound worker protocol.
import { createHash, randomUUID, timingSafeEqual } from 'node:crypto';
import { isDeepStrictEqual } from 'node:util';
import { getPool, withTransaction } from './db.js';

const ID = '[a-f0-9]{32}';
const MAX_REQUEST = 2_000_000;
const SECRET_KEY = /secret|token|password|api.?key|authorization|credential|private.?key|codex.?path|project.?path|build.?command|deploy.?command|local.?path/i;
const INLINE_SECRET = /\b(?:client[_-]?secret|refresh[_-]?token|access[_-]?token|api[_-]?key|authorization|password|private[_-]?key)\b\s*[:=]\s*[^\s,;]+/gi;
const LOCAL_PATH = /(^|\s)\/(?:Users|home|private|var|tmp)\/[^\s,;]+/g;

type CommandStatus = 'queued' | 'leased' | 'succeeded' | 'failed' | 'needs_review';
type Command = { id: string; method: string; path: string; body: Record<string, unknown>; status: CommandStatus; result: unknown; created_at: string; updated_at: string };

function object(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function fieldsOnly(body: Record<string, unknown>, allowed: string[], required = false): boolean {
  const fields = Object.keys(body);
  return (!required || fields.length > 0) && fields.every(field => allowed.includes(field));
}

function hasSensitiveKey(value: unknown): boolean {
  if (Array.isArray(value)) return value.some(hasSensitiveKey);
  return object(value) && Object.entries(value).some(([key, item]) => SECRET_KEY.test(key) || hasSensitiveKey(item));
}

export function allowedCommand(method: string, path: string, value: unknown): boolean {
  if (!object(value) || JSON.stringify(value).length > 100_000 || hasSensitiveKey(value)) return false;
  const body = value;
  if (method === 'POST' && ['/api/chatseo/sync', '/api/chatseo/disconnect'].includes(path)) return Object.keys(body).length === 0;
  const chatseo = new RegExp(`^/api/clients/${ID}/chatseo-(map|sync|settings|send|poll|import|resolve|read)$`).exec(path);
  if (method === 'POST' && chatseo) {
    const exact = (fields: string[]) => Object.keys(body).length === fields.length && fieldsOnly(body, fields);
    const reference = (value: unknown) => typeof value === 'string' && /^[A-Za-z0-9_-]{1,200}$/.test(value);
    const runId = (value: unknown) => typeof value === 'string' && new RegExp(`^${ID}$`).test(value);
    switch (chatseo[1]) {
      case 'sync': return exact([]);
      case 'map': return exact(['site_id']) && reference(body.site_id);
      case 'settings': return exact(['enabled', 'daily_request_limit', 'minimum_credits'])
        && typeof body.enabled === 'boolean'
        && Number.isInteger(body.daily_request_limit) && Number(body.daily_request_limit) >= 1 && Number(body.daily_request_limit) <= 10
        && Number.isInteger(body.minimum_credits) && Number(body.minimum_credits) >= 1 && Number(body.minimum_credits) <= 100000;
      case 'send': return exact(['message', 'credit_acknowledged', 'request_id'])
        && typeof body.message === 'string' && body.message.trim().length > 0 && body.message.length <= 8000
        && body.credit_acknowledged === true && typeof body.request_id === 'string'
        && /^[a-fA-F0-9]{8}-(?:[a-fA-F0-9]{4}-){3}[a-fA-F0-9]{12}$/.test(body.request_id);
      case 'resolve': return exact(['run_id', 'checked_provider']) && runId(body.run_id) && body.checked_provider === true;
      case 'read':
      case 'poll': return exact(['run_id']) && runId(body.run_id);
      case 'import': return exact(['run_id', 'artifact_id']) && runId(body.run_id) && reference(body.artifact_id);
    }
  }
  if (method === 'POST' && path === '/api/clients') return fieldsOnly(body, ['url', 'name']) && typeof body.url === 'string';
  if (method === 'PATCH' && new RegExp(`^/api/clients/${ID}$`).test(path)) {
    if (!fieldsOnly(body, ['name', 'subject', 'profile', 'confirmed', 'automation', 'next_run', 'seo_connection', 'visibility_settings', 'image_brand', 'conversion_goal', 'aio_settings', 'growth_settings'], true)) return false;
    if (body.profile !== undefined && (!object(body.profile) || Object.keys(body.profile).some(key => SECRET_KEY.test(key)))) return false;
    if (body.seo_connection !== undefined && (!object(body.seo_connection) || !fieldsOnly(body.seo_connection, ['site_url', 'ga4_property', 'auto_sync']))) return false;
    if (body.visibility_settings !== undefined && (!object(body.visibility_settings) || !fieldsOnly(body.visibility_settings, ['enabled', 'interval_days', 'max_actions', 'auto_plan', 'auto_refresh', 'auto_research', 'auto_probe']))) return false;
    if (body.aio_settings !== undefined) {
      if (!object(body.aio_settings) || !fieldsOnly(body.aio_settings, ['enabled', 'interval_days', 'queries', 'country', 'language', 'location', 'device'])) return false;
      const s = body.aio_settings;
      if (s.enabled !== undefined && typeof s.enabled !== 'boolean') return false;
      if (s.interval_days !== undefined && (!Number.isInteger(s.interval_days) || Number(s.interval_days) < 1 || Number(s.interval_days) > 30)) return false;
      if (s.queries !== undefined && (!Array.isArray(s.queries) || s.queries.length > 10 || s.queries.some(q => typeof q !== 'string' || q.trim().length < 2 || q.length > 200))) return false;
      if (s.country !== undefined && (typeof s.country !== 'string' || !/^[a-zA-Z]{2}$/.test(s.country))) return false;
      if (s.language !== undefined && (typeof s.language !== 'string' || !/^[a-zA-Z]{2}$/.test(s.language))) return false;
      if (s.location !== undefined && (typeof s.location !== 'string' || s.location.length > 100)) return false;
      if (s.device !== undefined && !['desktop', 'mobile', 'tablet'].includes(String(s.device))) return false;
    }
    if (body.growth_settings !== undefined) {
      if (!object(body.growth_settings) || !fieldsOnly(body.growth_settings, ['enabled', 'interval_days', 'jev_enabled'])) return false;
      const s = body.growth_settings;
      if (s.enabled !== undefined && typeof s.enabled !== 'boolean') return false;
      if (s.jev_enabled !== undefined && typeof s.jev_enabled !== 'boolean') return false;
      if (s.interval_days !== undefined && (!Number.isInteger(s.interval_days) || Number(s.interval_days) < 1 || Number(s.interval_days) > 30)) return false;
    }
    return true;
  }
  if (method === 'PATCH' && new RegExp(`^/api/experience/${ID}/edit$`).test(path)) return fieldsOnly(body, ['claim', 'source_url', 'source_note', 'confirmed'], true)
    && (body.claim === undefined || typeof body.claim === 'string' && body.claim.length <= 2000)
    && (body.source_url === undefined || typeof body.source_url === 'string' && body.source_url.length <= 2000)
    && (body.source_note === undefined || typeof body.source_note === 'string' && body.source_note.length <= 1000)
    && (body.confirmed === undefined || typeof body.confirmed === 'boolean');
  if (method === 'PATCH' && new RegExp(`^/api/articles/${ID}$`).test(path)) return fieldsOnly(body, ['body', 'title', 'description', 'scheduled_at'], true);
  if (method === 'PATCH' && new RegExp(`^/api/backlinks/${ID}$`).test(path)) return fieldsOnly(body, ['status', 'notes'], true);
  if (method === 'PATCH' && new RegExp(`^/api/opportunities/${ID}$`).test(path)) return Object.keys(body).length === 1 && ['open', 'dismissed'].includes(String(body.status));
  if (method === 'PATCH' && path === '/api/settings') return fieldsOnly(body, ['paused', 'model', 'max_pages'], true);
  if (method !== 'POST') return false;
  // Removing a website needs the typed address; the Mac checks it again before deleting anything.
  if (new RegExp(`^/api/clients/${ID}/remove$`).test(path)) {
    return Object.keys(body).length === 1 && typeof body.confirm === 'string' && body.confirm.length > 0 && body.confirm.length <= 300;
  }
  const routes: [RegExp, string[]][] = [
    [new RegExp(`^/api/clients/${ID}/(?:inspect|run|visibility-audit|visibility-research|visibility-probe|seo-sync|backlinks-discover|aio-collect|evidence-audit)$`), []],
    [new RegExp(`^/api/experience/${ID}/remove$`), []],
    [new RegExp(`^/api/clients/${ID}/plan$`), ['subject']],
    [new RegExp(`^/api/clients/${ID}/backlinks$`), ['source_url', 'notes']],
    [new RegExp(`^/api/clients/${ID}/experiments$`), ['page_url', 'hypothesis', 'change']],
    [new RegExp(`^/api/articles/${ID}/(?:generate|review|cover|publish|verify|remove|remove-cover)$`), []],
    [new RegExp(`^/api/backlinks/${ID}/check$`), []],
    [new RegExp(`^/api/opportunities/${ID}/execute$`), []],
    [new RegExp(`^/api/experiments/${ID}/cancel$`), []],
    [new RegExp(`^/api/jobs/${ID}/(?:retry|stop|dismiss)$`), []],
  ];
  if (new RegExp(`^/api/clients/${ID}/aio-import$`).test(path)) {
    return Object.keys(body).length === 1 && object(body.observation)
      && fieldsOnly(body.observation, ['query', 'country', 'language', 'location', 'device', 'observed_at', 'status', 'answer', 'citations', 'evidence_url', 'evidence_note']);
  }
  if (new RegExp(`^/api/clients/${ID}/experience$`).test(path)) {
    return fieldsOnly(body, ['claim', 'source_url', 'source_note', 'confirmed'])
      && typeof body.claim === 'string' && body.claim.trim().length >= 10 && body.claim.length <= 2000
      && (body.source_url === undefined || typeof body.source_url === 'string' && body.source_url.length <= 2000)
      && (body.source_note === undefined || typeof body.source_note === 'string' && body.source_note.length <= 1000)
      && (body.confirmed === undefined || typeof body.confirmed === 'boolean');
  }
  if (new RegExp(`^/api/clients/${ID}/evidence-report$`).test(path)) {
    return Object.keys(body).length === 1 && ['md', 'pdf', 'xlsx'].includes(String(body.format));
  }
  if (new RegExp(`^/api/clients/${ID}/aio-history$`).test(path)) {
    return Object.keys(body).length === 1 && typeof body.query === 'string' && body.query.trim().length >= 2 && body.query.length <= 200;
  }
  return routes.some(([route, fields]) => route.test(path) && fieldsOnly(body, fields));
}

// The enrolled owner's snapshot proves references before durable queueing; the
// worker checks the current provider account/site binding again before execution.
export function validateChatseoReferences(path: string, body: Record<string, unknown>, snapshot: Record<string, unknown>): void {
  const match = new RegExp(`^/api/clients/(${ID})/chatseo-(map|sync|settings|send|poll|import|resolve|read)$`).exec(path);
  if (!match) return;
  const rows = (value: unknown): Record<string, unknown>[] => Array.isArray(value) ? value.filter(object) : [];
  const client = rows(snapshot.clients).find(item => item.id === match[1]);
  if (!client) throw new RangeError('Client not found in the owner snapshot. Sync the worker first.');
  if (match[2] === 'map') {
    const sites = object(snapshot.chatseo) ? rows(snapshot.chatseo.sites) : [];
    if (!sites.some(site => site.id === body.site_id)) throw new RangeError('ChatSEO site is not in the connected account. Sync sites first.');
  }
  if (['poll', 'import', 'resolve', 'read'].includes(match[2])) {
    const run = rows(snapshot.chatseo_runs).find(item => item.id === body.run_id && item.client_id === client.id && item.site_id === client.chatseo_site_id);
    if (!run) throw new RangeError('ChatSEO request does not belong to this client and linked site.');
    if (match[2] === 'import' && !rows(run.artifacts).some(item => item.id === body.artifact_id)) throw new RangeError('ChatSEO artifact does not belong to this request.');
  }
}

export function chatseoSnapshotRead(path: string, snapshot: Record<string, unknown>): Record<string, unknown> | null {
  const status = object(snapshot.chatseo) ? snapshot.chatseo : { connected: false, connection_required: true, local_connection_only: true, sites: [], capabilities: [], credits: { remaining: null } };
  if (path === '/api/chatseo/status') return status;
  if (path === '/api/chatseo/sites') return { sites: Array.isArray(status.sites) ? status.sites : [] };
  const match = new RegExp(`^/api/clients/(${ID})/chatseo$`).exec(path);
  if (!match) return null;
  const rows = (value: unknown): Record<string, unknown>[] => Array.isArray(value) ? value.filter(object) : [];
  const client = rows(snapshot.clients).find(item => item.id === match[1]);
  if (!client) throw new RangeError('Client not found in the owner snapshot.');
  return {
    report: rows(snapshot.chatseo_reports).find(item => item.client_id === client.id && item.site_id === client.chatseo_site_id) ?? null,
    runs: rows(snapshot.chatseo_runs).filter(item => item.client_id === client.id && item.site_id === client.chatseo_site_id),
  };
}

function clean(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(clean);
  if (object(value)) {
    const result: Record<string, unknown> = {};
    for (const [key, item] of Object.entries(value)) {
      if (SECRET_KEY.test(key) || key === 'publish_result') continue;
      result[key] = clean(item);
    }
    if (object(result.connection)) {
      const source = result.connection;
      result.connection = Object.fromEntries(['mode', 'format', 'auto_publish', 'content_dir', 'public_url_template']
        .filter(key => key in source).map(key => [key, source[key]]));
    }
    return result;
  }
  if (typeof value === 'string') return value.replace(INLINE_SECRET, '[redacted]').replace(LOCAL_PATH, '$1[local path]').slice(0, 500_000);
  return value;
}

async function readJson(request: Request): Promise<Record<string, unknown>> {
  const reader = request.body?.getReader();
  if (!reader) throw new RangeError('JSON body required');
  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > MAX_REQUEST) throw new RangeError('Request too large');
      chunks.push(value);
    }
  } finally {
    reader.releaseLock();
  }
  let parsed: unknown;
  try { parsed = JSON.parse(new TextDecoder().decode(Buffer.concat(chunks))); }
  catch { throw new RangeError('Invalid JSON'); }
  if (!object(parsed)) throw new RangeError('JSON object required');
  return parsed;
}

function json(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), { status, headers: { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' } });
}

function authorized(request: Request): boolean {
  const expected = process.env.RANKME_WORKER_TOKEN;
  const header = request.headers.get('authorization');
  if (!expected || expected.length < 32 || !header?.startsWith('Bearer ')) return false;
  const candidate = header.slice(7);
  const left = createHash('sha256').update(expected).digest();
  const right = createHash('sha256').update(candidate).digest();
  return timingSafeEqual(left, right);
}

export async function enqueueCommand(ownerId: string, method: string, path: string, body: unknown, requestKey?: string): Promise<Command> {
  if (!allowedCommand(method, path, body)) throw new RangeError('Unsupported cloud command or fields');
  if (requestKey !== undefined && !/^[a-zA-Z0-9_-]{8,128}$/.test(requestKey)) throw new RangeError('Invalid idempotency key');
  return withTransaction(async client => {
    const owner = await client.query('SELECT id FROM rankme_cloud.owner WHERE id=$1 FOR SHARE', [ownerId]);
    if (!owner.rowCount) throw new RangeError('Owner enrollment required');
    if (new RegExp(`^/api/clients/${ID}/chatseo-`).test(path)) {
      const snapshot = await client.query('SELECT state FROM rankme_cloud.snapshot WHERE singleton=true');
      validateChatseoReferences(path, body as Record<string, unknown>, object(snapshot.rows[0]?.state) ? snapshot.rows[0].state : {});
    }
    const id = randomUUID().replaceAll('-', '');
    const inserted = await client.query(
      `INSERT INTO rankme_cloud.commands(id,owner_id,request_key,method,path,body)
       VALUES ($1,$2,$3,$4,$5,$6::jsonb)
       ON CONFLICT (owner_id,request_key) DO NOTHING
       RETURNING replace(id::text,'-','') AS id,method,path,body,status,result,created_at,updated_at`,
      [id, ownerId, requestKey ?? null, method, path, JSON.stringify(body)]);
    if (inserted.rows[0]) return inserted.rows[0] as Command;
    const existing = await client.query(
      `SELECT replace(id::text,'-','') AS id,method,path,body,status,result,created_at,updated_at
       FROM rankme_cloud.commands WHERE owner_id=$1 AND request_key=$2`, [ownerId, requestKey]);
    const row = existing.rows[0] as Command | undefined;
    if (!row || row.method !== method || row.path !== path || !isDeepStrictEqual(row.body, body)) throw new RangeError('Idempotency key used for another command');
    return row;
  });
}

export async function getCommand(ownerId: string, id: string): Promise<Command | null> {
  if (!new RegExp(`^(?:${ID}|[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12})$`).test(id)) return null;
  const result = await getPool().query(
    `SELECT replace(id::text,'-','') AS id,method,path,body,status,result,created_at,updated_at
     FROM rankme_cloud.commands WHERE owner_id=$1 AND id=$2`, [ownerId, id]);
  return (result.rows[0] as Command | undefined) ?? null;
}

export async function getSnapshot(ownerId: string): Promise<Record<string, unknown>> {
  const result = await getPool().query(
    `SELECT
       (SELECT state FROM rankme_cloud.snapshot WHERE singleton=true) AS state,
       (SELECT last_seen FROM rankme_cloud.snapshot WHERE singleton=true) AS last_seen,
       (SELECT count(*)::int FROM rankme_cloud.commands c WHERE c.owner_id=$1 AND c.status IN ('queued','leased')) AS pending`, [ownerId]);
  const row = result.rows[0] as { state: unknown; last_seen: Date | null; pending: number } | undefined;
  const state = row && object(row.state) ? row.state : {};
  const lastSeen = row?.last_seen?.toISOString() ?? null;
  return { ...state, bridge: { lastSeen, online: !!row?.last_seen && Date.now() - row.last_seen.getTime() < 60_000, pendingCommands: row?.pending ?? 0 } };
}

export async function handleWorker(request: Request): Promise<Response> {
  if (!authorized(request)) return json({ error: 'Unauthorized' }, 401);
  const path = new URL(request.url).pathname;
  if (request.method !== 'POST' || !['/api/worker/poll', '/api/worker/result'].includes(path)) return json({ error: 'Endpoint not found' }, 404);
  try {
    const body = await readJson(request);
    if (path === '/api/worker/poll') {
      if (!object(body.snapshot) || JSON.stringify(body.snapshot).length > MAX_REQUEST) throw new RangeError('Invalid snapshot');
      return json(await withTransaction(async client => {
        await client.query(
          `INSERT INTO rankme_cloud.snapshot(singleton,state,last_seen) VALUES(true,$1::jsonb,now())
           ON CONFLICT(singleton) DO UPDATE SET state=EXCLUDED.state,last_seen=now()`, [JSON.stringify(clean(body.snapshot))]);
        const owner = await client.query('SELECT id FROM rankme_cloud.owner LIMIT 1');
        if (!owner.rows[0]) return { owner_enrolled: false, command: null };
        await client.query(
          `UPDATE rankme_cloud.commands SET status='needs_review',updated_at=now(),
             result=COALESCE(result,'{"message":"Worker claim expired; review locally before retrying."}'::jsonb)
           WHERE owner_id=$1 AND status='leased' AND leased_at < now() - interval '2 minutes'`, [owner.rows[0].id]);
        const claimed = await client.query(
          `WITH picked AS (SELECT id FROM rankme_cloud.commands
            WHERE owner_id=$1 AND status='queued' ORDER BY created_at,id LIMIT 1 FOR UPDATE SKIP LOCKED)
           UPDATE rankme_cloud.commands c SET status='leased',leased_at=now(),updated_at=now()
           FROM picked WHERE c.id=picked.id
           RETURNING replace(c.id::text,'-','') AS id,c.method,c.path,c.body`, [owner.rows[0].id]);
        return { owner_enrolled: true, command: claimed.rows[0] ?? null };
      }));
    }
    const id = body.id;
    const status = body.status;
    if (typeof id !== 'string' || !new RegExp(`^${ID}$`).test(id) || !['succeeded', 'failed', 'needs_review'].includes(String(status)) || !object(body.result)) throw new RangeError('Invalid worker result');
    if (JSON.stringify(body.result).length > 500_000) throw new RangeError('Worker result too large');
    const updated = await getPool().query(
      `UPDATE rankme_cloud.commands SET status=$2,result=$3::jsonb,updated_at=now()
       WHERE id=$1 AND status IN ('leased','needs_review')
       RETURNING id`, [id, status, JSON.stringify(clean(body.result))]);
    if (!updated.rowCount) {
      const previous = await getPool().query('SELECT status FROM rankme_cloud.commands WHERE id=$1', [id]);
      if (!previous.rowCount || previous.rows[0].status !== status) return json({ error: 'Unknown or completed command' }, 409);
    }
    return json({ ok: true });
  } catch (error) {
    return json({ error: error instanceof RangeError ? error.message : 'Bridge request failed' }, error instanceof RangeError ? 400 : 500);
  }
}
