import assert from 'node:assert/strict';
import { createHash, generateKeyPairSync, randomBytes } from 'node:crypto';
import { test } from 'node:test';
import { Pool } from 'pg';
import { createAuthService } from '../cloud/auth.js';
import { issuePasskeyInvite } from '../cloud/admin.js';

const origin = 'http://localhost:18789';
const databaseUrl = process.env.DATABASE_URL;
if (!databaseUrl?.includes('ep-lively-poetry-b25ggp71') || process.env.RANKME_TEST_BRANCH !== 'br-restless-tooth-b2uw88lk')
  throw new Error('Invite integration test requires the explicit isolated Neon branch guard');

const pool = new Pool({ connectionString: databaseUrl, max: 5 });
const auth = createAuthService(pool, { publicOrigin: origin, cookieSecret: process.env.RANKME_COOKIE_SECRET });
const b64 = (value: Uint8Array | Buffer) => Buffer.from(value).toString('base64url');
const sha = (value: string | Buffer) => createHash('sha256').update(value).digest('hex');
function cbor(value: unknown): Buffer {
  const head = (major: number, size: number): Buffer => size < 24 ? Buffer.from([(major << 5) | size]) :
    size < 256 ? Buffer.from([(major << 5) | 24, size]) : Buffer.from([(major << 5) | 25, size >> 8, size & 255]);
  if (typeof value === 'number') return value >= 0 ? head(0, value) : head(1, -1 - value);
  if (typeof value === 'string') return Buffer.concat([head(3, Buffer.byteLength(value)), Buffer.from(value)]);
  if (Buffer.isBuffer(value)) return Buffer.concat([head(2, value.length), value]);
  if (value instanceof Map) return Buffer.concat([head(5, value.size), ...[...value].flatMap(([key, item]) => [cbor(key), cbor(item)])]);
  throw Error('Unsupported CBOR value');
}
function authenticator() {
  const { publicKey } = generateKeyPairSync('ec', { namedCurve: 'prime256v1' });
  const jwk = publicKey.export({ format: 'jwk' });
  const rawId = randomBytes(32), id = b64(rawId);
  const cose = cbor(new Map<unknown, unknown>([[1, 2], [3, -7], [-1, 1], [-2, Buffer.from(jwk.x!, 'base64url')], [-3, Buffer.from(jwk.y!, 'base64url')]]));
  return { id, registration(challenge: string, flags = 0x45, crossOrigin = false) {
    const client = Buffer.from(JSON.stringify({ type: 'webauthn.create', challenge, origin, ...(crossOrigin ? {crossOrigin:true} : {}) }));
    const counter = Buffer.alloc(4);
    const authData = Buffer.concat([createHash('sha256').update('localhost').digest(), Buffer.from([flags]), counter,
      Buffer.alloc(16), Buffer.from([0, rawId.length]), rawId, cose]);
    const attestation = cbor(new Map<unknown, unknown>([['fmt', 'none'], ['attStmt', new Map()], ['authData', authData]]));
    return { id, rawId:id, type:'public-key', response:{clientDataJSON:b64(client),attestationObject:b64(attestation),transports:['internal']},clientExtensionResults:{} };
  }};
}
type Jar = Map<string,string>;
function cookies(response: Response, jar: Jar) {
  for (const item of response.headers.getSetCookie()) {
    const [pair] = item.split(';'), at = pair.indexOf('=');
    if (at < 0) continue;
    const key = pair.slice(0, at), value = pair.slice(at + 1);
    if (value) jar.set(key, value); else jar.delete(key);
  }
}
async function call(path: string, jar: Jar, csrf = '', method = 'GET', body?: unknown, requestOrigin = origin): Promise<Response> {
  const headers: Record<string,string> = { Cookie: [...jar].map(([key,value]) => `${key}=${value}`).join('; ') };
  if (method !== 'GET') Object.assign(headers, { Origin: requestOrigin, 'Content-Type':'application/json', 'X-RankMe-CSRF':csrf });
  const request = new Request(origin + path, { method, headers, ...(method !== 'GET' ? { body:JSON.stringify(body ?? {}) } : {}) });
  const response = await auth.handle(request);
  assert(response);
  cookies(response, jar);
  return response;
}

test('operator invitation adds to the existing owner, rotates, expires, and cannot replay', async () => {
  const jar: Jar = new Map(), other: Jar = new Map();
  const key = authenticator();
  const hashes: string[] = [];
  let originalIds: string[] = [];
  try {
    const owners = await pool.query('SELECT id,user_handle FROM rankme_cloud.owner');
    assert.equal(owners.rowCount, 1, 'test branch must already have exactly one owner');
    const ownerId = owners.rows[0].id;
    originalIds = (await pool.query('SELECT id FROM rankme_cloud.credentials WHERE owner_id=$1 ORDER BY id', [ownerId])).rows.map(row => row.id);
    assert(originalIds.length > 0 && originalIds.length < 10);
    let response = await call('/api/auth/status', jar);
    const csrf = (await response.json()).csrf;
    response = await call('/api/auth/status', other);
    const otherCsrf = (await response.json()).csrf;
    assert.equal((await call('/api/session', jar)).status, 401, 'invite alone gives no workspace session');
    const first = await issuePasskeyInvite(pool);
    hashes.push(sha(first));
    response = await call('/api/auth/invite/options', jar, csrf, 'POST', {invite_secret:first}, 'https://evil.example');
    assert.equal(response.status, 403);
    response = await call('/api/auth/invite/options', jar, csrf, 'POST', {invite_secret:first});
    assert.equal(response.status, 200);
    let options = (await response.json()).publicKey;
    assert.equal(options.authenticatorSelection.authenticatorAttachment, 'platform');
    assert.equal(options.authenticatorSelection.userVerification, 'required');
    assert.equal(options.user.id, b64(owners.rows[0].user_handle));
    assert.equal(options.excludeCredentials.length, originalIds.length);
    response = await call('/api/auth/invite/verify', other, otherCsrf, 'POST', {invite_secret:first,credential:key.registration(options.challenge)});
    assert.equal(response.status, 403, 'challenge is browser bound');
    response = await call('/api/auth/invite/verify', jar, csrf, 'POST', {invite_secret:'B'.repeat(43),credential:key.registration(options.challenge)});
    assert.equal(response.status, 403, 'wrong secret cannot complete a challenge');
    response = await call('/api/auth/invite/options', jar, csrf, 'POST', {invite_secret:first});
    options = (await response.json()).publicKey;
    response = await call('/api/auth/invite/verify', jar, csrf, 'POST', {invite_secret:first,credential:key.registration(options.challenge, 0x45, true)});
    assert.equal(response.status, 403, 'cross-origin ceremony is rejected');
    response = await call('/api/auth/invite/options', jar, csrf, 'POST', {invite_secret:first});
    options = (await response.json()).publicKey;
    response = await call('/api/auth/invite/verify', jar, csrf, 'POST', {invite_secret:first,credential:key.registration(options.challenge, 0x41)});
    assert.equal(response.status, 403, 'registration without user verification is rejected');
    response = await call('/api/auth/invite/options', jar, csrf, 'POST', {invite_secret:first});
    options = (await response.json()).publicKey;
    await pool.query("UPDATE rankme_cloud.passkey_invites SET expires_at=now()-interval '1 second' WHERE secret_hash=$1", [sha(first)]);
    response = await call('/api/auth/invite/verify', jar, csrf, 'POST', {invite_secret:first,credential:key.registration(options.challenge)});
    assert.equal(response.status, 403, 'expired invitation is rejected at commit');
    response = await call('/api/auth/invite/options', jar, csrf, 'POST', {invite_secret:first});
    assert.equal(response.status, 403, 'expired invitation cannot start another challenge');
    const second = await issuePasskeyInvite(pool);
    hashes.push(sha(second));
    response = await call('/api/auth/invite/options', jar, csrf, 'POST', {invite_secret:second});
    options = (await response.json()).publicKey;
    const third = await issuePasskeyInvite(pool);
    hashes.push(sha(third));
    response = await call('/api/auth/invite/verify', jar, csrf, 'POST', {invite_secret:second,credential:key.registration(options.challenge)});
    assert.equal(response.status, 403, 'reissue invalidates the earlier challenge');
    response = await call('/api/auth/invite/options', jar, csrf, 'POST', {invite_secret:second});
    assert.equal(response.status, 403, 'reissued token is revoked');
    response = await call('/api/auth/invite/options', jar, csrf, 'POST', {invite_secret:third,name:'Phone'});
    assert.equal(response.status, 200);
    options = (await response.json()).publicKey;
    const registration = key.registration(options.challenge);
    const [accepted, replay] = await Promise.all([
      call('/api/auth/invite/verify', jar, csrf, 'POST', {invite_secret:third,credential:registration}),
      call('/api/auth/invite/verify', jar, csrf, 'POST', {invite_secret:third,credential:registration}),
    ]);
    assert.deepEqual([accepted.status,replay.status].sort(), [200,403]);
    assert(jar.has('__Host-rankme_session'));
    assert.equal((await call('/api/session', jar)).status, 200);
    response = await call('/api/auth/invite/options', other, otherCsrf, 'POST', {invite_secret:third});
    assert.equal(response.status, 403, 'consumed token cannot start a ceremony');
    const after = (await pool.query('SELECT id FROM rankme_cloud.credentials WHERE owner_id=$1 ORDER BY id', [ownerId])).rows.map(row => row.id);
    assert.deepEqual(after.filter(id => id !== key.id), originalIds, 'all existing credentials remain');
    assert.equal(after.filter(id => id === key.id).length, 1);
  } finally {
    await pool.query('DELETE FROM rankme_cloud.credentials WHERE id=$1', [key.id]);
    await pool.query('DELETE FROM rankme_cloud.challenges WHERE invite_hash=ANY($1::text[])', [hashes]);
    await pool.query('DELETE FROM rankme_cloud.passkey_invites WHERE secret_hash=ANY($1::text[])', [hashes]);
    await pool.end();
  }
});
