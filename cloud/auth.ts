import { createHash, createHmac, randomBytes, randomUUID, timingSafeEqual } from 'node:crypto';
import type { Pool, PoolClient } from 'pg';
import {
  generateAuthenticationOptions, generateRegistrationOptions,
  verifyAuthenticationResponse, verifyRegistrationResponse,
  type WebAuthnCredential,
} from '@simplewebauthn/server';
import type { AuthenticationResponseJSON, RegistrationResponseJSON } from '@simplewebauthn/server';
import { withTransaction } from './db.js';

const PRE_COOKIE = '__Host-rankme_pre';
const SESSION_COOKIE = '__Host-rankme_session';
const CHALLENGE_MS = 5 * 60_000;
const IDLE_MS = 30 * 60_000;
const ABSOLUTE_MS = 8 * 60 * 60_000;
const STEP_UP_MS = 5 * 60_000;
const MAX_BODY = 64 * 1024;

export class AuthError extends Error {
  constructor(public status = 403) { super('Authentication failed'); }
}

export type AuthSession = {
  id: string;
  ownerId: string;
  credentialId: string;
  csrf: string;
  absoluteExpiresAt: Date;
  idleExpiresAt: Date;
};

export function json(value: unknown, status = 200, headers?: HeadersInit): Response {
  const out = new Headers(headers);
  out.set('Content-Type', 'application/json; charset=utf-8');
  out.set('Cache-Control', 'no-store');
  out.set('X-Content-Type-Options', 'nosniff');
  return new Response(JSON.stringify(value), { status, headers: out });
}

export function authErrorResponse(error: unknown): Response {
  return json({ error: 'Request could not be completed' }, error instanceof AuthError ? error.status : 500);
}

function hash(value: string): string { return createHash('sha256').update(value).digest('hex'); }
function safeEqual(a: string, b: string): boolean {
  const aa = Buffer.from(a), bb = Buffer.from(b);
  return aa.length === bb.length && timingSafeEqual(aa, bb);
}
function randomToken(): string { return randomBytes(32).toString('base64url'); }
function cookie(request: Request, name: string): string | null {
  const part = request.headers.get('cookie')?.split(';').map(item => item.trim()).find(item => item.startsWith(`${name}=`));
  if (!part) return null;
  try { return decodeURIComponent(part.slice(name.length + 1)); } catch { throw new AuthError(400); }
}
function setCookie(name: string, value: string, maxAge: number): string {
  return `${name}=${encodeURIComponent(value)}; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=${maxAge}`;
}
function requireOrigin(request: Request, origin: string): void {
  if (request.headers.get('origin') !== origin) throw new AuthError(403);
  const site = request.headers.get('sec-fetch-site');
  if (site && site !== 'same-origin') throw new AuthError(403);
}
async function readBody(request: Request): Promise<Record<string, unknown>> {
  if (!request.headers.get('content-type')?.toLowerCase().startsWith('application/json')) throw new AuthError(415);
  const declared = Number(request.headers.get('content-length') || 0);
  if (declared > MAX_BODY) throw new AuthError(413);
  const reader = request.body?.getReader();
  if (!reader) return {};
  const chunks: Uint8Array[] = [];
  let size = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    size += value.length;
    if (size > MAX_BODY) { await reader.cancel(); throw new AuthError(413); }
    chunks.push(value);
  }
  let value: unknown;
  try { value = JSON.parse(Buffer.concat(chunks).toString('utf8')); } catch { throw new AuthError(400); }
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new AuthError(400);
  return value as Record<string, unknown>;
}

function rejectCrossOriginCeremony(clientData: unknown): void {
  if (typeof clientData !== 'string') throw new AuthError(400);
  let decoded: unknown;
  try { decoded = JSON.parse(Buffer.from(clientData, 'base64url').toString('utf8')); }
  catch { throw new AuthError(403); }
  if (!decoded || typeof decoded !== 'object' || (decoded as Record<string, unknown>).crossOrigin === true ||
      (decoded as Record<string, unknown>).topOrigin !== undefined) throw new AuthError(403);
}

type Config = { publicOrigin?: string; cookieSecret?: string };
type ChallengeKind = 'enroll' | 'login' | 'stepup' | 'add' | 'invite';
type Challenge = { challenge: string; session_id: string | null; bootstrap_hash: string | null; invite_hash: string | null; name: string | null };

export function createAuthService(pool: Pool, config: Config = {}) {
  const publicOrigin = config.publicOrigin ?? process.env.RANKME_PUBLIC_ORIGIN ?? 'https://getrankme.vercel.app';
  const parsed = new URL(publicOrigin);
  if (parsed.origin !== publicOrigin || (parsed.protocol !== 'https:' && !(parsed.protocol === 'http:' && parsed.hostname === 'localhost')))
    throw new Error('RANKME_PUBLIC_ORIGIN must be an exact HTTPS origin (or explicit localhost development origin)');
  const rpID = parsed.hostname;
  const secret = config.cookieSecret ?? process.env.RANKME_COOKIE_SECRET;
  if (!secret || Buffer.byteLength(secret) < 32) throw new Error('RANKME_COOKIE_SECRET must contain at least 32 bytes');
  const csrfFor = (token: string) => createHmac('sha256', secret).update(`csrf:${token}`).digest('base64url');
  let maintenanceCalls = 0;
  const browserHash = (request: Request): string => {
    const token = cookie(request, PRE_COOKIE);
    if (!token || !/^[A-Za-z0-9_-]{43}$/.test(token)) throw new AuthError(403);
    return hash(token);
  };
  const anonymousCsrf = (request: Request): void => {
    const token = cookie(request, PRE_COOKIE);
    if (!token || !safeEqual(request.headers.get('x-rankme-csrf') ?? '', csrfFor(token))) throw new AuthError(403);
  };
  const mutation = (request: Request): void => { requireOrigin(request, publicOrigin); anonymousCsrf(request); };

  async function rate(scope: string, bucket: string, limit: number, seconds = 60): Promise<void> {
    if (++maintenanceCalls % 100 === 0) {
      await pool.query(`DELETE FROM rankme_cloud.challenges WHERE id IN
        (SELECT id FROM rankme_cloud.challenges WHERE expires_at<now() ORDER BY expires_at LIMIT 100)`);
      await pool.query(`DELETE FROM rankme_cloud.sessions WHERE id IN
        (SELECT id FROM rankme_cloud.sessions WHERE absolute_expires_at<now() OR idle_expires_at<now() LIMIT 100)`);
      await pool.query(`DELETE FROM rankme_cloud.rate_limits WHERE (scope,bucket) IN
        (SELECT scope,bucket FROM rankme_cloud.rate_limits WHERE window_start<now()-interval '1 day' LIMIT 100)`);
    }
    const result = await pool.query(`INSERT INTO rankme_cloud.rate_limits(scope,bucket,window_start,attempts)
      VALUES($1,$2,now(),1) ON CONFLICT(scope,bucket) DO UPDATE SET
      window_start=CASE WHEN rankme_cloud.rate_limits.window_start < now()-($3::int * interval '1 second') THEN now() ELSE rankme_cloud.rate_limits.window_start END,
      attempts=CASE WHEN rankme_cloud.rate_limits.window_start < now()-($3::int * interval '1 second') THEN 1 ELSE rankme_cloud.rate_limits.attempts+1 END
      RETURNING attempts`, [scope, bucket, seconds]);
    if (result.rows[0].attempts > limit) throw new AuthError(429);
  }

  async function requireSession(request: Request, isMutation = false): Promise<AuthSession | null> {
    if (isMutation) requireOrigin(request, publicOrigin);
    const token = cookie(request, SESSION_COOKIE);
    if (!token || !/^[A-Za-z0-9_-]{43}$/.test(token)) return null;
    const found = await pool.query(`SELECT id,owner_id,credential_id,csrf_hash,absolute_expires_at,idle_expires_at
      FROM rankme_cloud.sessions WHERE token_hash=$1 AND absolute_expires_at>now() AND idle_expires_at>now()`, [hash(token)]);
    const row = found.rows[0];
    if (!row) return null;
    const csrf = csrfFor(token);
    if (!safeEqual(row.csrf_hash, hash(csrf))) return null;
    if (isMutation && !safeEqual(request.headers.get('x-rankme-csrf') ?? '', csrf)) throw new AuthError(403);
    return { id: row.id, ownerId: row.owner_id, credentialId: row.credential_id, csrf,
      absoluteExpiresAt: row.absolute_expires_at, idleExpiresAt: row.idle_expires_at };
  }

  async function issueSession(client: PoolClient, ownerId: string, credentialId: string): Promise<{ token: string; csrf: string }> {
    const token = randomToken(), csrf = csrfFor(token);
    await client.query(`INSERT INTO rankme_cloud.sessions(id,owner_id,credential_id,token_hash,csrf_hash,absolute_expires_at,idle_expires_at)
      VALUES($1,$2,$3,$4,$5,now()+interval '8 hours',now()+interval '30 minutes')`,
      [randomUUID(), ownerId, credentialId, hash(token), hash(csrf)]);
    return { token, csrf };
  }

  async function saveChallenge(kind: ChallengeKind, challenge: string, request: Request, sessionId?: string, bootstrapHash?: string, name?: string, inviteHash?: string): Promise<void> {
    const browser = browserHash(request);
    await withTransaction(async client => {
      await client.query('DELETE FROM rankme_cloud.challenges WHERE browser_hash=$1 AND kind=$2', [browser, kind]);
      await client.query(`INSERT INTO rankme_cloud.challenges(id,kind,challenge,browser_hash,session_id,bootstrap_hash,name,invite_hash,expires_at)
        VALUES($1,$2,$3,$4,$5,$6,$7,$8,now()+interval '5 minutes')`,
        [randomUUID(), kind, challenge, browser, sessionId ?? null, bootstrapHash ?? null, name ?? null, inviteHash ?? null]);
    }, pool);
  }
  async function consumeChallenge(kind: ChallengeKind, request: Request, sessionId?: string): Promise<Challenge> {
    const browser = browserHash(request);
    const result = await pool.query(`DELETE FROM rankme_cloud.challenges WHERE id IN
      (SELECT id FROM rankme_cloud.challenges WHERE kind=$1 AND browser_hash=$2
       AND session_id IS NOT DISTINCT FROM $3::uuid ORDER BY created_at DESC LIMIT 1)
      RETURNING challenge,session_id,bootstrap_hash,invite_hash,name,expires_at`, [kind, browser, sessionId ?? null]);
    const row = result.rows[0];
    if (!row || new Date(row.expires_at).getTime() <= Date.now()) throw new AuthError(403);
    return row;
  }

  async function status(request: Request): Promise<Response> {
    const existing = cookie(request, PRE_COOKIE);
    const pre = existing && /^[A-Za-z0-9_-]{43}$/.test(existing) ? existing : randomToken();
    const session = await requireSession(request);
    const enrolled = (await pool.query('SELECT EXISTS(SELECT 1 FROM rankme_cloud.owner) AS enrolled')).rows[0].enrolled;
    const response = json({ enrolled, authenticated: Boolean(session), csrf: session?.csrf ?? csrfFor(pre), preCsrf: csrfFor(pre) });
    if (pre !== existing) response.headers.append('Set-Cookie', setCookie(PRE_COOKIE, pre, 8 * 3600));
    return response;
  }

  async function registrationOptions(request: Request, mode: 'enroll' | 'add' | 'invite'): Promise<Response> {
    const add = mode === 'add', invited = mode === 'invite';
    let ownerId: string, userHandle: Uint8Array<ArrayBufferLike>, name = 'Passkey', bootstrapHash: string | undefined, inviteHash: string | undefined;
    let session: AuthSession | null = null;
    if (add) {
      session = await requireSession(request, true);
      if (!session) throw new AuthError(401);
      const active = await pool.query('SELECT 1 FROM rankme_cloud.sessions WHERE id=$1 AND step_up_at>now()-interval \'5 minutes\'', [session.id]);
      if (!active.rowCount) throw new AuthError(403);
      const count = await pool.query('SELECT count(*)::int AS count FROM rankme_cloud.credentials WHERE owner_id=$1', [session.ownerId]);
      if (count.rows[0].count >= 10) throw new AuthError(409);
      const body = await readBody(request);
      name = typeof body.name === 'string' ? body.name.trim().slice(0, 80) || 'Passkey' : 'Passkey';
      const owner = await pool.query('SELECT user_handle FROM rankme_cloud.owner WHERE id=$1', [session.ownerId]);
      if (!owner.rowCount) throw new AuthError(401);
      ownerId = session.ownerId;
      userHandle = Uint8Array.from(owner.rows[0].user_handle);
    } else if (invited) {
      mutation(request);
      const body = await readBody(request);
      if (typeof body.invite_secret !== 'string' || !/^[A-Za-z0-9_-]{43}$/.test(body.invite_secret)) throw new AuthError(403);
      inviteHash = hash(body.invite_secret);
      await rate('invite', inviteHash, 10, 600);
      const result = await pool.query(`SELECT o.id,o.user_handle FROM rankme_cloud.passkey_invites i
        JOIN rankme_cloud.owner o ON o.id=i.owner_id WHERE i.secret_hash=$1 AND i.expires_at>now()`, [inviteHash]);
      if (result.rowCount !== 1) throw new AuthError(403);
      ownerId = result.rows[0].id;
      userHandle = Uint8Array.from(result.rows[0].user_handle);
      const count = await pool.query('SELECT count(*)::int AS count FROM rankme_cloud.credentials WHERE owner_id=$1', [ownerId]);
      if (count.rows[0].count >= 10) throw new AuthError(409);
      name = typeof body.name === 'string' ? body.name.trim().slice(0, 80) || 'Passkey' : 'Passkey';
    } else {
      mutation(request);
      const body = await readBody(request);
      if (typeof body.bootstrap_secret !== 'string' || !/^[A-Za-z0-9_-]{32,128}$/.test(body.bootstrap_secret)) throw new AuthError(403);
      bootstrapHash = hash(body.bootstrap_secret);
      await rate('bootstrap', 'global', 10, 600);
      const bootstrap = await pool.query('SELECT secret_hash FROM rankme_cloud.bootstrap WHERE singleton=true AND expires_at>now()');
      if (!bootstrap.rowCount || !safeEqual(bootstrap.rows[0].secret_hash, bootstrapHash)) throw new AuthError(403);
      const ownerExists = await pool.query('SELECT 1 FROM rankme_cloud.owner LIMIT 1');
      if (ownerExists.rowCount) throw new AuthError(409);
      ownerId = randomUUID();
      userHandle = randomBytes(32);
    }
    const creds = add || invited ? await pool.query('SELECT id,transports FROM rankme_cloud.credentials WHERE owner_id=$1', [ownerId]) : { rows: [] };
    const publicKey = await generateRegistrationOptions({ rpName: 'RankMe', rpID, userName: 'owner', userDisplayName: 'RankMe owner',
      userID: Uint8Array.from(userHandle) as Uint8Array<ArrayBuffer>, attestationType: 'none', timeout: CHALLENGE_MS,
      authenticatorSelection: { residentKey: 'required', requireResidentKey: true, userVerification: 'required',
        ...(invited ? { authenticatorAttachment: 'platform' as const } : {}) },
      excludeCredentials: creds.rows.map(row => ({ id: row.id, transports: row.transports })) });
    await saveChallenge(mode, publicKey.challenge, request, session?.id, bootstrapHash,
      add || invited ? name : Buffer.from(userHandle).toString('base64url'), inviteHash);
    return json({ publicKey });
  }

  async function registrationVerify(request: Request, mode: 'enroll' | 'add' | 'invite'): Promise<Response> {
    const add = mode === 'add', invited = mode === 'invite';
    const session = add ? await requireSession(request, true) : null;
    if (add && !session) throw new AuthError(401);
    if (!add) mutation(request);
    const body = await readBody(request);
    const challenge = await consumeChallenge(mode, request, session?.id);
    if (invited && (typeof body.invite_secret !== 'string' || !/^[A-Za-z0-9_-]{43}$/.test(body.invite_secret) ||
      !challenge.invite_hash || !safeEqual(hash(body.invite_secret), challenge.invite_hash))) throw new AuthError(403);
    if (!body.credential || typeof body.credential !== 'object') throw new AuthError(400);
    rejectCrossOriginCeremony((body.credential as RegistrationResponseJSON).response?.clientDataJSON);
    let verified;
    try { verified = await verifyRegistrationResponse({ response: body.credential as RegistrationResponseJSON,
      expectedChallenge: challenge.challenge, expectedOrigin: publicOrigin, expectedRPID: rpID,
      requireUserVerification: true }); } catch { throw new AuthError(403); }
    if (!verified.verified || !verified.registrationInfo?.userVerified) throw new AuthError(403);
    const credential = verified.registrationInfo.credential;
    const transports = Array.isArray((body.credential as RegistrationResponseJSON).response?.transports)
      ? (body.credential as RegistrationResponseJSON).response.transports!.filter(value => typeof value === 'string') : [];
    if (add) {
      await withTransaction(async client => {
        const stepped = await client.query(`UPDATE rankme_cloud.sessions SET step_up_at=NULL WHERE id=$1
          AND step_up_at>now()-interval '5 minutes' AND absolute_expires_at>now() AND idle_expires_at>now()
          RETURNING owner_id`, [session!.id]);
        if (!stepped.rowCount) throw new AuthError(403);
        const owner = await client.query('SELECT id FROM rankme_cloud.owner WHERE id=$1 FOR UPDATE', [session!.ownerId]);
        if (!owner.rowCount) throw new AuthError(401);
        const count = await client.query('SELECT count(*)::int AS count FROM rankme_cloud.credentials WHERE owner_id=$1', [session!.ownerId]);
        if (count.rows[0].count >= 10) throw new AuthError(409);
        await client.query(`INSERT INTO rankme_cloud.credentials(id,owner_id,name,public_key,counter,transports)
          VALUES($1,$2,$3,$4,$5,$6)`, [credential.id, session!.ownerId, challenge.name ?? 'Passkey',
          Buffer.from(credential.publicKey), credential.counter, transports]);
      }, pool);
      return json({ id: credential.id, name: challenge.name ?? 'Passkey' });
    }
    if (invited) {
      const issued = await withTransaction(async client => {
        await client.query('SELECT pg_advisory_xact_lock($1)', [72149304]);
        const invite = await client.query(`DELETE FROM rankme_cloud.passkey_invites WHERE secret_hash=$1 AND expires_at>now()
          RETURNING owner_id`, [challenge.invite_hash]);
        if (invite.rowCount !== 1) throw new AuthError(403);
        const ownerId = invite.rows[0].owner_id;
        const owner = await client.query('SELECT id FROM rankme_cloud.owner WHERE id=$1 FOR UPDATE', [ownerId]);
        if (!owner.rowCount) throw new AuthError(403);
        const count = await client.query('SELECT count(*)::int AS count FROM rankme_cloud.credentials WHERE owner_id=$1', [ownerId]);
        if (count.rows[0].count >= 10) throw new AuthError(409);
        await client.query(`INSERT INTO rankme_cloud.credentials(id,owner_id,name,public_key,counter,transports)
          VALUES($1,$2,$3,$4,$5,$6)`, [credential.id, ownerId, challenge.name ?? 'Passkey',
          Buffer.from(credential.publicKey), credential.counter, transports]);
        await client.query('DELETE FROM rankme_cloud.challenges WHERE kind=$1 AND invite_hash=$2', ['invite', challenge.invite_hash]);
        return issueSession(client, ownerId, credential.id);
      }, pool);
      const result = json({ verified: true, csrf: issued.csrf, id: credential.id });
      result.headers.append('Set-Cookie', setCookie(SESSION_COOKIE, issued.token, 8 * 3600));
      return result;
    }
    const issued = await withTransaction(async client => {
      await client.query('SELECT pg_advisory_xact_lock($1)', [72149302]);
      const owner = await client.query('SELECT 1 FROM rankme_cloud.owner LIMIT 1');
      if (owner.rowCount) throw new AuthError(409);
      const bootstrap = await client.query(`DELETE FROM rankme_cloud.bootstrap WHERE singleton=true AND secret_hash=$1
        AND expires_at>now() RETURNING secret_hash`, [challenge.bootstrap_hash]);
      if (!bootstrap.rowCount) throw new AuthError(403);
      const ownerId = randomUUID();
      await client.query('INSERT INTO rankme_cloud.owner(id,user_handle) VALUES($1,$2)',
        [ownerId, Buffer.from(challenge.name!, 'base64url')]);
      await client.query(`INSERT INTO rankme_cloud.credentials(id,owner_id,public_key,counter,transports)
        VALUES($1,$2,$3,$4,$5)`, [credential.id, ownerId, Buffer.from(credential.publicKey), credential.counter, transports]);
      return issueSession(client, ownerId, credential.id);
    }, pool);
    const response = json({ verified: true, csrf: issued.csrf });
    response.headers.append('Set-Cookie', setCookie(SESSION_COOKIE, issued.token, 8 * 3600));
    return response;
  }

  async function authenticationOptions(request: Request, kind: 'login' | 'stepup'): Promise<Response> {
    let session: AuthSession | null = null;
    if (kind === 'stepup') { session = await requireSession(request, true); if (!session) throw new AuthError(401); }
    else mutation(request);
    const owner = await pool.query('SELECT id FROM rankme_cloud.owner LIMIT 1');
    if (!owner.rowCount) throw new AuthError(403);
    const creds = await pool.query('SELECT id,transports FROM rankme_cloud.credentials WHERE owner_id=$1', [owner.rows[0].id]);
    const publicKey = await generateAuthenticationOptions({ rpID, timeout: CHALLENGE_MS,
      userVerification: 'required', allowCredentials: creds.rows.map(row => ({ id: row.id, transports: row.transports })) });
    await saveChallenge(kind, publicKey.challenge, request, session?.id);
    return json({ publicKey });
  }

  async function authenticationVerify(request: Request, kind: 'login' | 'stepup'): Promise<Response> {
    const session = kind === 'stepup' ? await requireSession(request, true) : null;
    if (kind === 'stepup' && !session) throw new AuthError(401);
    if (kind === 'login') mutation(request);
    const body = await readBody(request);
    const challenge = await consumeChallenge(kind, request, session?.id);
    const response = body.credential as AuthenticationResponseJSON | undefined;
    if (!response || typeof response.id !== 'string') throw new AuthError(400);
    rejectCrossOriginCeremony(response.response?.clientDataJSON);
    const found = await pool.query(`SELECT c.id,c.owner_id,c.public_key,c.counter,c.transports,o.user_handle
      FROM rankme_cloud.credentials c JOIN rankme_cloud.owner o ON o.id=c.owner_id WHERE c.id=$1`, [response.id]);
    const row = found.rows[0];
    if (!row || (session && session.ownerId !== row.owner_id)) throw new AuthError(403);
    const userHandle = response.response?.userHandle;
    if (userHandle && !safeEqual(userHandle, Buffer.from(row.user_handle).toString('base64url'))) throw new AuthError(403);
    const credential: WebAuthnCredential = { id: row.id, publicKey: new Uint8Array(row.public_key),
      counter: Number(row.counter), transports: row.transports };
    let verified;
    try { verified = await verifyAuthenticationResponse({ response, expectedChallenge: challenge.challenge,
      expectedOrigin: publicOrigin, expectedRPID: rpID, credential, requireUserVerification: true }); }
    catch { throw new AuthError(403); }
    if (!verified.verified || !verified.authenticationInfo.userVerified) throw new AuthError(403);
    const issued = await withTransaction(async client => {
      const update = await client.query(`UPDATE rankme_cloud.credentials SET counter=$2,last_used_at=now()
        WHERE id=$1 AND counter=$3 RETURNING owner_id`, [row.id, verified.authenticationInfo.newCounter, row.counter]);
      if (!update.rowCount) throw new AuthError(409);
      if (session) {
        const active = await client.query(`UPDATE rankme_cloud.sessions SET step_up_at=now() WHERE id=$1
          AND absolute_expires_at>now() AND idle_expires_at>now() RETURNING id`, [session.id]);
        if (!active.rowCount) throw new AuthError(401);
        return null;
      }
      return issueSession(client, row.owner_id, row.id);
    }, pool);
    const result = json({ verified: true, ...(issued ? { csrf: issued.csrf } : {}) });
    if (issued) result.headers.append('Set-Cookie', setCookie(SESSION_COOKIE, issued.token, 8 * 3600));
    return result;
  }

  async function credentials(request: Request): Promise<Response> {
    const session = await requireSession(request);
    if (!session) throw new AuthError(401);
    const result = await pool.query(`SELECT id,name,created_at,last_used_at FROM rankme_cloud.credentials
      WHERE owner_id=$1 ORDER BY created_at,id`, [session.ownerId]);
    return json({ credentials: result.rows });
  }

  async function removeCredential(request: Request, id: string): Promise<Response> {
    const session = await requireSession(request, true);
    if (!session) throw new AuthError(401);
    await readBody(request);
    await withTransaction(async client => {
      await client.query('SELECT pg_advisory_xact_lock($1)', [72149303]);
      const stepped = await client.query(`UPDATE rankme_cloud.sessions SET step_up_at=NULL WHERE id=$1
        AND step_up_at>now()-interval '5 minutes' AND absolute_expires_at>now() AND idle_expires_at>now()
        RETURNING id`, [session.id]);
      if (!stepped.rowCount) throw new AuthError(403);
      const count = await client.query('SELECT count(*)::int AS count FROM rankme_cloud.credentials WHERE owner_id=$1', [session.ownerId]);
      if (count.rows[0].count <= 1) throw new AuthError(409);
      const removed = await client.query('DELETE FROM rankme_cloud.credentials WHERE id=$1 AND owner_id=$2 RETURNING id', [id, session.ownerId]);
      if (!removed.rowCount) throw new AuthError(404);
      await client.query('DELETE FROM rankme_cloud.sessions WHERE credential_id=$1', [id]);
    }, pool);
    return json({ removed: true });
  }

  async function handle(request: Request): Promise<Response | null> {
    const path = new URL(request.url).pathname;
    if (path !== '/api/session' && !path.startsWith('/api/auth/')) return null;
    try {
      if (request.method !== 'GET') {
        await rate('global', 'auth', 120);
        await rate('browser', cookie(request, PRE_COOKIE) ? hash(cookie(request, PRE_COOKIE)!) : 'missing', 30);
      }
      if (path === '/api/auth/status' && request.method === 'GET') return await status(request);
      if (path === '/api/session' && request.method === 'GET') {
        const session = await requireSession(request);
        if (!session) throw new AuthError(401);
        return json({ csrf: session.csrf, token: session.csrf });
      }
      if (path === '/api/auth/enroll/options' && request.method === 'POST') return await registrationOptions(request, 'enroll');
      if (path === '/api/auth/enroll/verify' && request.method === 'POST') return await registrationVerify(request, 'enroll');
      if (path === '/api/auth/invite/options' && request.method === 'POST') return await registrationOptions(request, 'invite');
      if (path === '/api/auth/invite/verify' && request.method === 'POST') return await registrationVerify(request, 'invite');
      if (path === '/api/auth/login/options' && request.method === 'POST') return await authenticationOptions(request, 'login');
      if (path === '/api/auth/login/verify' && request.method === 'POST') return await authenticationVerify(request, 'login');
      if (path === '/api/auth/step-up/options' && request.method === 'POST') return await authenticationOptions(request, 'stepup');
      if (path === '/api/auth/step-up/verify' && request.method === 'POST') return await authenticationVerify(request, 'stepup');
      if (path === '/api/auth/credentials' && request.method === 'GET') return await credentials(request);
      if (path === '/api/auth/credentials/options' && request.method === 'POST') return await registrationOptions(request, 'add');
      if (path === '/api/auth/credentials/verify' && request.method === 'POST') return await registrationVerify(request, 'add');
      if (path.startsWith('/api/auth/credentials/') && request.method === 'DELETE')
        return await removeCredential(request, decodeURIComponent(path.slice('/api/auth/credentials/'.length)));
      if (path === '/api/auth/logout' && request.method === 'POST') {
        const session = await requireSession(request, true);
        if (session) await pool.query('DELETE FROM rankme_cloud.sessions WHERE id=$1', [session.id]);
        else { requireOrigin(request, publicOrigin); anonymousCsrf(request); }
        const result = json({ ok: true });
        result.headers.append('Set-Cookie', setCookie(SESSION_COOKIE, '', 0));
        return result;
      }
      if (path === '/api/auth/touch' && request.method === 'POST') {
        const session = await requireSession(request, true);
        if (!session) throw new AuthError(401);
        const touched = await pool.query(`UPDATE rankme_cloud.sessions SET last_active_at=now(),idle_expires_at=LEAST(absolute_expires_at,now()+interval '30 minutes')
          WHERE id=$1 AND absolute_expires_at>now() AND idle_expires_at>now()`, [session.id]);
        if (!touched.rowCount) throw new AuthError(401);
        return json({ ok: true });
      }
      return json({ error: 'Not found' }, 404);
    } catch (error) { return authErrorResponse(error); }
  }
  return { handle, requireSession };
}
