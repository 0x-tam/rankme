import { AuthError, createAuthService, authErrorResponse } from './auth';
import { getPool } from './db';
import { chatseoSnapshotRead, enqueueCommand, getCommand, getSnapshot, handleWorker } from './bridge';
import { BookingError, bookingHistory, confirmBooking, exportBookingData, issueReference, issueTestReference, listBookings, lookupReference, publicReferenceCors, updateBooking } from './bookings';

const PUBLIC_ORIGIN = 'https://getrankme.vercel.app';
const auth = createAuthService(getPool(), { publicOrigin: process.env.RANKME_PUBLIC_ORIGIN ?? PUBLIC_ORIGIN });
const apiHeaders = {
  'Cache-Control': 'no-store, private',
  'Content-Security-Policy': "default-src 'none'; frame-ancestors 'none'",
  'Referrer-Policy': 'no-referrer',
  'X-Content-Type-Options': 'nosniff',
  'X-Frame-Options': 'DENY',
};

function secure(response: Response): Response {
  const headers = new Headers(response.headers);
  for (const [key, value] of Object.entries(apiHeaders)) headers.set(key, value);
  return new Response(response.body, { status: response.status, statusText: response.statusText, headers });
}
function json(value: unknown, status = 200): Response {
  return secure(Response.json(value, { status }));
}
function unavailable(message = 'This action is available only in the local RankMe workspace.'): Response {
  return json({ error: message }, 409);
}
function file(name: string, content: string, type: string): Response {
  const headers = new Headers(apiHeaders);
  headers.set('Content-Type', type);
  headers.set('Content-Disposition', `attachment; filename="${name}"`);
  return new Response(content, { headers });
}
function snapshotState(snapshot: Record<string, unknown>): Record<string, unknown> {
  const bridge = snapshot.bridge && typeof snapshot.bridge === 'object' ? snapshot.bridge : { online: false, lastSeen: null, pendingCommands: 0 };
  return {
    ...snapshot,
    clients: Array.isArray(snapshot.clients) ? snapshot.clients : [],
    articles: Array.isArray(snapshot.articles) ? snapshot.articles : [],
    jobs: Array.isArray(snapshot.jobs) ? snapshot.jobs : [],
    events: Array.isArray(snapshot.events) ? snapshot.events : [],
    settings: snapshot.settings && typeof snapshot.settings === 'object' ? snapshot.settings : {},
    status: snapshot.status && typeof snapshot.status === 'object' ? snapshot.status : {},
    bridge,
  };
}
async function readJson(request: Request, maxBytes = 2_000_000): Promise<unknown> {
  const declared = Number(request.headers.get('content-length') || '0');
  if (declared > maxBytes) throw new RangeError('Request is too large.');
  const reader = request.body?.getReader();
  if (!reader) return {};
  const pieces: Uint8Array[] = [];
  let length = 0;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    length += value.byteLength;
    if (length > maxBytes) {
      await reader.cancel();
      throw new RangeError('Request is too large.');
    }
    pieces.push(value);
  }
  const body = new Uint8Array(length);
  let offset = 0;
  for (const piece of pieces) { body.set(piece, offset); offset += piece.byteLength; }
  if (!length) return {};
  try { return JSON.parse(new TextDecoder().decode(body)); }
  catch { throw new SyntaxError('Expected a JSON request body.'); }
}

async function route(request: Request): Promise<Response> {
  const url = new URL(request.url);
  const path = url.pathname;
  if (!path.startsWith('/api/')) return json({ error: 'Not found.' }, 404);

  if (path === '/api/public/booking-reference') {
    const cors = publicReferenceCors(request);
    if (request.method === 'OPTIONS') return secure(new Response(null, { status: 204, headers: cors }));
    if (request.method !== 'POST') return json({ error: 'Method not allowed.' }, 405);
    if (!request.headers.get('content-type')?.toLowerCase().startsWith('application/json')) return json({ error: 'JSON content type required.' }, 415);
    const result = json(await issueReference(request, await readJson(request, 256)), 201);
    for (const [key, value] of cors) result.headers.set(key, value);
    return result;
  }

  if (path.startsWith('/api/worker/')) return secure(await handleWorker(request));

  const handled = await auth.handle(request);
  if (handled) return secure(handled);

  const mutation = !['GET', 'HEAD'].includes(request.method);
  const session = await auth.requireSession(request, mutation);
  if (!session) return json({ error: 'Unlock the workspace first.' }, 401);

  if (path === '/api/bookings' && request.method === 'GET') return json(await listBookings(session.ownerId));
  if (path === '/api/bookings/export' && request.method === 'GET')
    return file('lumident-bookings.json', JSON.stringify({ format: 'rankme-lumident-bookings-v1', exported_at: new Date().toISOString(), ...await exportBookingData(session.ownerId) }), 'application/json; charset=utf-8');
  if (path === '/api/bookings/test-reference' && request.method === 'POST') {
    if (!request.headers.get('content-type')?.toLowerCase().startsWith('application/json')) return json({ error: 'JSON content type required.' }, 415);
    return json(await issueTestReference(session.ownerId, await readJson(request, 256)), 201);
  }
  const reference = /^\/api\/bookings\/references\/(LM-[A-F0-9]{12})$/.exec(path);
  if (reference && request.method === 'GET') return json(await lookupReference(session.ownerId, reference[1]));
  const history = /^\/api\/bookings\/([a-f0-9-]{36})\/history$/.exec(path);
  if (history && request.method === 'GET') return json(await bookingHistory(session.ownerId, history[1]));
  if (path === '/api/bookings' && request.method === 'POST') {
    if (!request.headers.get('content-type')?.toLowerCase().startsWith('application/json')) return json({ error: 'JSON content type required.' }, 415);
    return json(await confirmBooking(session.ownerId, request, await readJson(request, 512)), 201);
  }
  const booking = /^\/api\/bookings\/([a-f0-9-]{36})$/.exec(path);
  if (booking && request.method === 'PATCH') {
    if (!request.headers.get('content-type')?.toLowerCase().startsWith('application/json')) return json({ error: 'JSON content type required.' }, 415);
    return json(await updateBooking(session.ownerId, booking[1], await readJson(request, 512)));
  }

  if (request.method === 'GET' && path === '/api/state') {
    return json(snapshotState((await getSnapshot(session.ownerId)) as Record<string, unknown>));
  }
  if (request.method === 'GET' && /^\/api\/commands\/(?:[a-f0-9]{32}|[a-f0-9-]{36})$/.test(path)) {
    const command = await getCommand(session.ownerId, path.slice('/api/commands/'.length));
    return command ? json(command) : json({ error: 'Command not found.' }, 404);
  }

  if (request.method === 'GET') {
    const snapshot = snapshotState((await getSnapshot(session.ownerId)) as Record<string, unknown>);
    const clients = snapshot.clients as Record<string, unknown>[];
    const articles = snapshot.articles as Record<string, unknown>[];
    if (path === '/api/backup') return file('rankme-cloud-backup.json', JSON.stringify({ format: 'rankme-cloud-snapshot-v2', exported_at: new Date().toISOString(), last_synced_at: (snapshot.bridge as Record<string, unknown>).lastSeen, state: snapshot, lumident_bookings: await exportBookingData(session.ownerId) }), 'application/json; charset=utf-8');
    const article = /^\/api\/articles\/([a-f0-9]+)\/download$/.exec(path);
    if (article) {
      const item = articles.find(row => row.id === article[1]);
      return item && typeof item.body === 'string'
        ? file(`rankme-article-${article[1]}.md`, item.body, 'text/markdown; charset=utf-8')
        : json({ error: 'Article content is unavailable in the cloud snapshot.' }, 404);
    }
    if (/^\/api\/articles\/[a-f0-9]+\/cover$/.test(path)) return unavailable('Cover downloads are available in the local workspace.');
    const measurements = /^\/api\/clients\/([a-f0-9]+)\/measurement-history$/.exec(path);
    if (measurements) {
      const item = clients.find(row => row.id === measurements[1]);
      return item ? file(`rankme-measurements-${measurements[1]}.json`, JSON.stringify({ measurements: (snapshot.measurements as Record<string, unknown>[] || []).filter(row => row.client_id === measurements[1]), experiments: (snapshot.experiments as Record<string, unknown>[] || []).filter(row => row.client_id === measurements[1]) }), 'application/json; charset=utf-8') : json({ error: 'Client not found.' }, 404);
    }
    const report = /^\/api\/clients\/([a-f0-9]+)\/visibility-report$/.exec(path);
    if (report) {
      const item = clients.find(row => row.id === report[1]);
      if (!item) return json({ error: 'Client not found.' }, 404);
      const visibility = (snapshot.visibility as Record<string, unknown>[] || []).find(row => row.client_id === report[1]) || {};
      return file(`rankme-visibility-${report[1]}.json`, JSON.stringify({ ...visibility, opportunities: (snapshot.opportunities as Record<string, unknown>[] || []).filter(row => row.client_id === report[1]), tasks: (snapshot.tasks as Record<string, unknown>[] || []).filter(row => row.client_id === report[1]), measurements: (snapshot.measurements as Record<string, unknown>[] || []).filter(row => row.client_id === report[1]), experiments: (snapshot.experiments as Record<string, unknown>[] || []).filter(row => row.client_id === report[1]) }), 'application/json; charset=utf-8');
    }
    const chatseo = chatseoSnapshotRead(path, snapshot);
    if (chatseo) return json(chatseo);
    if (path === '/api/google/properties') return unavailable('Google account setup and property selection run on your Mac.');
    return json({ error: 'Not found.' }, 404);
  }

  if (!['POST', 'PATCH', 'DELETE'].includes(request.method)) return json({ error: 'Method not allowed.' }, 405);
  if (path.startsWith('/api/chatseo/') && !['/api/chatseo/sync', '/api/chatseo/disconnect'].includes(path)) return unavailable('Connect ChatSEO in the local RankMe workspace.');
  if (path.startsWith('/api/google/') || path === '/api/connection/check' || path === '/api/settings/check') return unavailable();
  if (!request.headers.get('content-type')?.toLowerCase().startsWith('application/json')) return json({ error: 'JSON content type required.' }, 415);
  const body = await readJson(request);
  if (path === '/api/settings' && request.method === 'PATCH') {
    if (!body || typeof body !== 'object' || Array.isArray(body) || Object.keys(body).some(key => !['paused', 'model', 'max_pages'].includes(key))) {
      return json({ error: 'Only pause, model, and page limit can be changed here. Configure local paths on your Mac.' }, 400);
    }
  }
  const requestKey = request.headers.get('idempotency-key') || undefined;
  const command = await enqueueCommand(session.ownerId, request.method, path, body, requestKey);
  return json(command, 202);
}

export default {
  async fetch(request: Request): Promise<Response> {
    try { return await route(request); }
    catch (error) {
      if (error instanceof RangeError) return json({ error: error.message }, 400);
      if (error instanceof SyntaxError) return json({ error: error.message }, 400);
      if (error instanceof AuthError) return secure(authErrorResponse(error));
      if (error instanceof BookingError) return json({ error: error.message }, error.status);
      console.error('RankMe cloud request failed', error instanceof Error ? error.name : 'unknown');
      return json({ error: 'The workspace is temporarily unavailable.' }, 503);
    }
  },
};
