import assert from 'node:assert/strict';
import { createHash, generateKeyPairSync, randomBytes, sign } from 'node:crypto';
import { test } from 'node:test';
import { Pool } from 'pg';
import { createAuthService } from '../cloud/auth.js';
import { issueBootstrap } from '../cloud/admin.js';

const origin = 'http://localhost:18789';
const branchEndpoint = 'ep-lively-poetry-b25ggp71';
const databaseUrl = process.env.DATABASE_URL;
if (!databaseUrl || !databaseUrl.includes(branchEndpoint) || process.env.RANKME_TEST_BRANCH !== 'br-restless-tooth-b2uw88lk')
  throw new Error('Cloud auth integration tests require the explicit isolated branch guard');

const pool = new Pool({ connectionString: databaseUrl, max: 5 });
const auth = createAuthService(pool, { publicOrigin: origin, cookieSecret: process.env.RANKME_COOKIE_SECRET });
const b64 = (value: Uint8Array | Buffer) => Buffer.from(value).toString('base64url');
const sha = (value: string | Buffer) => createHash('sha256').update(value).digest();
const u32 = (value: number) => { const result = Buffer.alloc(4); result.writeUInt32BE(value); return result; };
function cbor(value: unknown): Buffer {
  const head = (major: number, size: number): Buffer => size < 24 ? Buffer.from([(major << 5) | size]) :
    size < 256 ? Buffer.from([(major << 5) | 24, size]) : Buffer.from([(major << 5) | 25, size >> 8, size & 255]);
  if (typeof value === 'number') return value >= 0 ? head(0, value) : head(1, -1 - value);
  if (typeof value === 'string') return Buffer.concat([head(3, Buffer.byteLength(value)), Buffer.from(value)]);
  if (Buffer.isBuffer(value)) return Buffer.concat([head(2, value.length), value]);
  if (value instanceof Map) return Buffer.concat([head(5, value.size), ...[...value].flatMap(([key, item]) => [cbor(key), cbor(item)])]);
  if (value && typeof value === 'object') return cbor(new Map(Object.entries(value)));
  throw new Error('Unsupported CBOR test value');
}

function authenticator() {
  const { privateKey, publicKey } = generateKeyPairSync('ec', { namedCurve: 'prime256v1' });
  const jwk = publicKey.export({ format: 'jwk' });
  const credentialId = randomBytes(32);
  const id = b64(credentialId);
  const rpHash = sha('localhost');
  const cose = cbor(new Map<unknown, unknown>([[1, 2], [3, -7], [-1, 1], [-2, Buffer.from(jwk.x!, 'base64url')], [-3, Buffer.from(jwk.y!, 'base64url')]]));
  const baseData = (flags: number, counter: number) => Buffer.concat([rpHash, Buffer.from([flags]), u32(counter)]);
  return {
    id,
    registration(challenge: string) {
      const client = Buffer.from(JSON.stringify({ type: 'webauthn.create', challenge, origin }));
      const authData = Buffer.concat([baseData(0x45, 0), Buffer.alloc(16), Buffer.from([0, credentialId.length]), credentialId, cose]);
      const attestation = cbor(new Map<unknown, unknown>([['fmt', 'none'], ['attStmt', new Map()], ['authData', authData]]));
      return { id, rawId: id, type: 'public-key', response: { clientDataJSON: b64(client), attestationObject: b64(attestation), transports: ['internal'] }, clientExtensionResults: {} };
    },
    assertion(challenge: string, counter: number, userHandle: string | null = null) {
      const client = Buffer.from(JSON.stringify({ type: 'webauthn.get', challenge, origin }));
      const authData = baseData(0x05, counter);
      const signature = sign('sha256', Buffer.concat([authData, sha(client)]), privateKey);
      return { id, rawId: id, type: 'public-key', response: { clientDataJSON: b64(client), authenticatorData: b64(authData), signature: b64(signature), userHandle }, clientExtensionResults: {} };
    },
  };
}

function parseCookies(response: Response, jar: Map<string, string>): void {
  for (const value of response.headers.getSetCookie()) {
    const [pair] = value.split(';');
    const at = pair.indexOf('=');
    if (at < 0) continue;
    const name = pair.slice(0, at), token = pair.slice(at + 1);
    if (token) jar.set(name, token); else jar.delete(name);
  }
}
function call(path: string, jar: Map<string, string>, csrf = '', method = 'GET', body?: unknown, overrideOrigin = origin) {
  const headers: Record<string, string> = { Cookie: [...jar].map(([name, value]) => `${name}=${value}`).join('; ') };
  if (method !== 'GET') { headers.Origin = overrideOrigin; headers['Content-Type'] = 'application/json'; headers['X-RankMe-CSRF'] = csrf; }
  const request = new Request(`${origin}${path}`, { method, headers, ...(method !== 'GET' ? { body: JSON.stringify(body ?? {}) } : {}) });
  return auth.handle(request).then(response => { assert(response); parseCookies(response, jar); return response; });
}

test('single owner, WebAuthn proof, browser binding, expiry, counter, and session policies', async () => {
  const jar = new Map<string, string>();
  const other = new Map<string, string>();
  try {
    const empty = await pool.query('SELECT count(*)::int AS count FROM rankme_cloud.owner');
    assert.equal(empty.rows[0].count, 0, 'test branch must have no owner');
    let response = await call('/api/auth/status', jar);
    assert.equal(response.status, 200);
    let status = await response.json();
    assert.equal(status.enrolled, false);
    const csrf = status.csrf;
    const secret = await issueBootstrap(pool);
    response = await call('/api/auth/enroll/options', jar, csrf, 'POST', { bootstrap_secret: secret }, 'https://evil.example');
    assert.equal(response.status, 403);
    response = await call('/api/auth/enroll/options', jar, csrf, 'POST', { bootstrap_secret: secret });
    assert.equal(response.status, 200);
    let options = (await response.json()).publicKey;
    assert.equal(options.authenticatorSelection.userVerification, 'required');
    assert.equal(options.authenticatorSelection.residentKey, 'required');
    const key = authenticator();
    response = await call('/api/auth/enroll/verify', other, csrf, 'POST', { credential: key.registration(options.challenge) });
    assert.equal(response.status, 403, 'challenge is browser bound');
    const crossOrigin = key.registration(options.challenge);
    const client = JSON.parse(Buffer.from(crossOrigin.response.clientDataJSON, 'base64url').toString('utf8'));
    client.crossOrigin = true;
    crossOrigin.response.clientDataJSON = b64(Buffer.from(JSON.stringify(client)));
    response = await call('/api/auth/enroll/verify', jar, csrf, 'POST', { credential: crossOrigin });
    assert.equal(response.status, 403, 'cross-origin ceremony is rejected');
    response = await call('/api/auth/enroll/options', jar, csrf, 'POST', { bootstrap_secret: secret });
    options = (await response.json()).publicKey;
    await pool.query("UPDATE rankme_cloud.challenges SET expires_at=now()-interval '1 second' WHERE kind='enroll'");
    response = await call('/api/auth/enroll/verify', jar, csrf, 'POST', { credential: key.registration(options.challenge) });
    assert.equal(response.status, 403, 'expired ceremony is rejected');
    response = await call('/api/auth/enroll/options', jar, csrf, 'POST', { bootstrap_secret: secret });
    options = (await response.json()).publicKey;
    response = await call('/api/auth/enroll/verify', jar, csrf, 'POST', { credential: key.registration(options.challenge) });
    assert.equal(response.status, 200);
    status = await response.json();
    const sessionCsrf = status.csrf;
    response = await call('/api/auth/enroll/options', jar, csrf, 'POST', { bootstrap_secret: secret });
    assert.equal(response.status, 403, 'used bootstrap secret cannot enroll again');
    assert(jar.has('__Host-rankme_session'));
    response = await call('/api/session', jar);
    assert.equal(response.status, 200);
    response = await call('/api/auth/credentials', jar);
    assert.equal((await response.json()).credentials.length, 1);
    response = await call(`/api/auth/credentials/${encodeURIComponent(key.id)}`, jar, sessionCsrf, 'DELETE', {});
    assert.equal(response.status, 403, 'removal requires step-up');
    response = await call('/api/auth/logout', jar, sessionCsrf, 'POST', {});
    assert.equal(response.status, 200);
    response = await call('/api/session', jar);
    assert.equal(response.status, 401);
    response = await call('/api/auth/login/options', jar, csrf, 'POST', {});
    assert.equal(response.status, 200);
    const loginChallenge = (await response.json()).publicKey.challenge;
    const assertion = key.assertion(loginChallenge, 1);
    const [first, replay] = await Promise.all([
      call('/api/auth/login/verify', jar, csrf, 'POST', { credential: assertion }),
      call('/api/auth/login/verify', other, csrf, 'POST', { credential: assertion }),
    ]);
    assert.equal(first.status, 200);
    assert.equal(replay.status, 403);
    const secondCsrf = (await first.json()).csrf;
    response = await call('/api/auth/login/options', jar, csrf, 'POST', {});
    assert.equal(response.status, 200);
    response = await call('/api/auth/login/verify', jar, csrf, 'POST', { credential: key.assertion((await response.json()).publicKey.challenge, 1) });
    assert.equal(response.status, 403, 'signature counter replay is rejected');
    const beforeRead = await pool.query('SELECT idle_expires_at FROM rankme_cloud.sessions WHERE token_hash=$1', [sha(jar.get('__Host-rankme_session')!).toString('hex')]);
    response = await call('/api/session', jar);
    assert.equal(response.status, 200);
    const afterRead = await pool.query('SELECT idle_expires_at FROM rankme_cloud.sessions WHERE token_hash=$1', [sha(jar.get('__Host-rankme_session')!).toString('hex')]);
    assert.equal(afterRead.rows[0].idle_expires_at.getTime(), beforeRead.rows[0].idle_expires_at.getTime(), 'passive GET does not extend idle expiry');
    response = await call('/api/auth/step-up/options', jar, secondCsrf, 'POST', {});
    assert.equal(response.status, 200);
    response = await call('/api/auth/step-up/verify', jar, secondCsrf, 'POST', { credential: key.assertion((await response.json()).publicKey.challenge, 2) });
    assert.equal(response.status, 200);
    response = await call(`/api/auth/credentials/${encodeURIComponent(key.id)}`, jar, secondCsrf, 'DELETE', {});
    assert.equal(response.status, 409, 'last credential guard');
    await pool.query("UPDATE rankme_cloud.sessions SET idle_expires_at=now()-interval '1 second'");
    response = await call('/api/session', jar);
    assert.equal(response.status, 401, 'idle expiry is enforced');
  } finally {
    await pool.query('DELETE FROM rankme_cloud.owner');
    await pool.query('DELETE FROM rankme_cloud.bootstrap');
    await pool.query('DELETE FROM rankme_cloud.challenges');
    await pool.query('DELETE FROM rankme_cloud.rate_limits');
    await pool.end();
  }
});
