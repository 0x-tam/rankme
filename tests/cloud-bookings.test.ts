import assert from 'node:assert/strict';
import { createHash, createHmac, randomBytes, randomUUID } from 'node:crypto';
import { test } from 'node:test';
import { getPool } from '../cloud/db';
import { BookingError, bookingHistory, confirmBooking, exportBookingData, issueReference, issueTestReference, listBookings, lookupReference, publicReferenceCors, updateBooking } from '../cloud/bookings';

const expectedBranch = 'br-odd-firefly-b2gozz9f';
const expectedEndpoint = 'ep-curly-thunder-b2hq3m9l';
if (process.env.RANKME_TEST_BRANCH !== expectedBranch || !process.env.DATABASE_URL?.includes(expectedEndpoint))
  throw new Error('Booking integration tests require the explicit isolated Neon branch guard.');

const ownerId = randomUUID();
const request = (key = randomUUID(), origin = 'https://getrankme.vercel.app') =>
  new Request('https://getrankme.vercel.app/api/bookings', { method: 'POST', headers: { Origin: origin, 'Idempotency-Key': key } });

test('Lumident confirmations keep source linkage, test isolation, history and idempotency', async () => {
  const pool = getPool();
  const before = await pool.query('SELECT count(*)::int AS count FROM rankme_cloud.owner');
  assert.equal(before.rows[0].count, 0, 'isolated test branch must have no owner');
  await pool.query('INSERT INTO rankme_cloud.owner(id,user_handle) VALUES($1,$2)', [ownerId, Buffer.from(randomUUID())]);
  try {
    assert.throws(() => publicReferenceCors(new Request('https://getrankme.vercel.app/api/public/booking-reference', { headers: { Origin: 'https://evil.example' } })), BookingError);
    const publicRequest = new Request('https://getrankme.vercel.app/api/public/booking-reference', { method: 'POST', headers: { Origin: 'https://lumident-lb.com' } });
    assert.equal(publicReferenceCors(publicRequest).get('access-control-allow-origin'), 'https://lumident-lb.com');
    await assert.rejects(issueReference(publicRequest, { department: 'pediatric', source: 'google_search', patient: 'private' }), { status: 400 });
    const unknownSource = await issueReference(publicRequest, { department: 'adultsCosmetics', source: 'claimed_campaign' });
    assert.equal((await lookupReference(ownerId, unknownSource.reference)).source, 'unknown');
    const { reference } = await issueReference(publicRequest, { department: 'pediatric', source: 'google_search' });
    const lead = await lookupReference(ownerId, reference);
    assert.equal(lead.source, 'google_search');
    assert.equal(lead.department, 'pediatric');
    await assert.rejects(lookupReference(randomUUID(), reference), { status: 404 });
    await assert.rejects(lookupReference(ownerId, 'LM-000000000000'), { status: 404 });
    await assert.rejects(confirmBooking(ownerId, request(), { reference, department: 'orthodontics' }), { status: 409 });
    const key = randomUUID();
    const [a, b] = await Promise.all([
      confirmBooking(ownerId, request(key), { reference, department: 'pediatric' }),
      confirmBooking(ownerId, request(key), { reference, department: 'pediatric' }),
    ]);
    assert.equal(a.id, b.id);
    assert.equal(a.source, 'google_search');
    await assert.rejects(confirmBooking(ownerId, request(key), { department: 'pediatric' }), { status: 409 });
    await assert.rejects(confirmBooking(ownerId, request(), { reference, department: 'pediatric' }), { status: 409 });
    const unlinked = await confirmBooking(ownerId, request(), { department: 'adultsCosmetics' });
    assert.equal(unlinked.source, 'unknown');
    const manualKey = randomUUID();
    const [manualA, manualB] = await Promise.all([
      confirmBooking(ownerId, request(manualKey), { department: 'pediatric' }),
      confirmBooking(ownerId, request(manualKey), { department: 'pediatric' }),
    ]);
    assert.equal(manualA.id, manualB.id);
    await assert.rejects(updateBooking(randomUUID(), a.id, { status: 'cancelled' }), { status: 404 });
    await assert.rejects(bookingHistory(randomUUID(), a.id), { status: 404 });
    const testRef = await issueTestReference(ownerId, { department: 'orthodontics' });
    await assert.rejects(confirmBooking(ownerId, request(), { reference: testRef.reference, department: 'orthodontics' }), { status: 409 });
    const testBooking = await confirmBooking(ownerId, request(), { reference: testRef.reference, department: 'orthodontics', test_mode: true });
    assert.equal(testBooking.test_mode, true);
    const expired = await issueTestReference(ownerId, { department: 'pediatric' });
    await pool.query('UPDATE rankme_cloud.booking_references SET expires_at=now()-interval \'1 second\' WHERE code=$1', [expired.reference]);
    await assert.rejects(confirmBooking(ownerId, request(), { reference: expired.reference, department: 'pediatric', test_mode: true }), { status: 404 });
    const beforeChange = await listBookings(ownerId);
    assert.equal(beforeChange.totals.reduce((sum: number, row: { count: number }) => sum + row.count, 0), 3);
    await updateBooking(ownerId, a.id, { status: 'cancelled' });
    await updateBooking(ownerId, a.id, { status: 'confirmed', department: 'orthodontics' });
    const history = await bookingHistory(ownerId, a.id);
    assert.deepEqual(history.events.map((event: { action: string }) => event.action), ['confirmed', 'cancelled', 'reconfirmed']);
    const exported = await exportBookingData(ownerId);
    assert.equal(exported.bookings.length, 4);
    assert.equal(exported.booking_events.length, 6);
    assert.equal(JSON.stringify(exported).includes('private'), false);

    // Exercise the real HTTP router with a synthetic valid session. WebAuthn
    // ceremony validation is covered by the dedicated auth integration suite.
    const secret = randomBytes(48).toString('base64url');
    process.env.RANKME_COOKIE_SECRET = secret;
    const token = randomBytes(32).toString('base64url');
    const csrf = createHmac('sha256', secret).update(`csrf:${token}`).digest('base64url');
    const hash = (value: string) => createHash('sha256').update(value).digest('hex');
    const credentialId = randomUUID();
    await pool.query(`INSERT INTO rankme_cloud.credentials(id,owner_id,public_key) VALUES($1,$2,$3)`, [credentialId, ownerId, Buffer.alloc(65)]);
    await pool.query(`INSERT INTO rankme_cloud.sessions(id,owner_id,credential_id,token_hash,csrf_hash,absolute_expires_at,idle_expires_at)
      VALUES($1,$2,$3,$4,$5,now()+interval '1 hour',now()+interval '30 minutes')`,
      [randomUUID(), ownerId, credentialId, hash(token), hash(csrf)]);
    const router = (await import('../cloud/index')).default;
    const route = (path: string, method = 'GET', body?: unknown, headers: Record<string,string> = {}) =>
      router.fetch(new Request(`https://getrankme.vercel.app${path}`, { method, headers: { Cookie: `__Host-rankme_session=${token}`, ...headers },
        ...(method === 'GET' ? {} : { body: JSON.stringify(body ?? {}) }) }));
    assert.equal((await router.fetch(new Request('https://getrankme.vercel.app/api/bookings'))).status, 401);
    assert.equal((await route('/api/bookings', 'POST', { department: 'pediatric' }, { Origin: 'https://evil.example', 'Content-Type': 'application/json', 'X-RankMe-CSRF': csrf, 'Idempotency-Key': randomUUID() })).status, 403);
    assert.equal((await route('/api/bookings', 'POST', { department: 'pediatric' }, { Origin: 'https://getrankme.vercel.app', 'Content-Type': 'application/json', 'Idempotency-Key': randomUUID() })).status, 403);
    assert.equal((await route('/api/bookings', 'POST', { department: 'pediatric', patient: 'private' }, { Origin: 'https://getrankme.vercel.app', 'Content-Type': 'application/json', 'X-RankMe-CSRF': csrf, 'Idempotency-Key': randomUUID() })).status, 400);
    assert.equal((await route('/api/bookings', 'POST', { department: 'pediatric', junk: 'x'.repeat(600) }, { Origin: 'https://getrankme.vercel.app', 'Content-Type': 'application/json', 'X-RankMe-CSRF': csrf, 'Idempotency-Key': randomUUID() })).status, 400);
    const valid = await route('/api/bookings', 'POST', { department: 'pediatric', test_mode: true }, { Origin: 'https://getrankme.vercel.app', 'Content-Type': 'application/json', 'X-RankMe-CSRF': csrf, 'Idempotency-Key': randomUUID() });
    assert.equal(valid.status, 201);
    assert.equal((await valid.json()).test_mode, true);
    const exportResponse = await route('/api/bookings/export');
    assert.equal(exportResponse.status, 200);
    assert.equal(exportResponse.headers.get('cache-control'), 'no-store, private');
  } finally {
    await pool.query('DELETE FROM rankme_cloud.booking_events WHERE owner_id=$1', [ownerId]);
    await pool.query('DELETE FROM rankme_cloud.bookings WHERE owner_id=$1', [ownerId]);
    await pool.query('DELETE FROM rankme_cloud.booking_references WHERE owner_id=$1', [ownerId]);
    await pool.query('DELETE FROM rankme_cloud.owner WHERE id=$1', [ownerId]);
    await pool.end();
  }
});
