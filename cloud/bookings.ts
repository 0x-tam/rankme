import { randomBytes, randomUUID } from 'node:crypto';
import { getPool, withTransaction } from './db';

const WEBSITE_ORIGIN = 'https://lumident-lb.com';
const departments = ['adultsCosmetics', 'pediatric', 'orthodontics'] as const;
type Department = typeof departments[number];
type Source = 'google_search' | 'google_maps' | 'unknown';
export class BookingError extends Error {
  constructor(public status: number, message: string) { super(message); }
}
const record = (value: unknown): Record<string, unknown> => {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new BookingError(400, 'Invalid booking request.');
  return value as Record<string, unknown>;
};
const only = (body: Record<string, unknown>, keys: string[]) => {
  if (Object.keys(body).some(key => !keys.includes(key))) throw new BookingError(400, 'Unexpected booking fields.');
};
const department = (value: unknown): Department => {
  if (!departments.includes(value as Department)) throw new BookingError(400, 'Choose a department.');
  return value as Department;
};
const referenceCode = (value: unknown): string => {
  if (typeof value !== 'string' || !/^LM-[A-F0-9]{12}$/.test(value)) throw new BookingError(400, 'Enter a valid booking reference.');
  return value;
};
const requestKey = (request: Request): string => {
  const value = request.headers.get('idempotency-key');
  if (!value || !/^[a-f0-9-]{36}$/i.test(value)) throw new BookingError(400, 'A unique request key is required.');
  return value;
};

export function publicReferenceCors(request: Request): Headers {
  const headers = new Headers({ 'Access-Control-Allow-Origin': WEBSITE_ORIGIN, 'Access-Control-Allow-Methods': 'POST, OPTIONS', 'Access-Control-Allow-Headers': 'Content-Type', 'Access-Control-Max-Age': '600', Vary: 'Origin' });
  if (request.headers.get('origin') !== WEBSITE_ORIGIN) throw new BookingError(403, 'This request is unavailable.');
  return headers;
}

async function issueRate(scope: string, bucket: string, limit: number, seconds: number): Promise<void> {
  const result = await getPool().query(`INSERT INTO rankme_cloud.rate_limits(scope,bucket,window_start,attempts)
    VALUES($1,$2,now(),1) ON CONFLICT(scope,bucket) DO UPDATE SET
    window_start=CASE WHEN rankme_cloud.rate_limits.window_start < now()-($3::int * interval '1 second') THEN now() ELSE rankme_cloud.rate_limits.window_start END,
    attempts=CASE WHEN rankme_cloud.rate_limits.window_start < now()-($3::int * interval '1 second') THEN 1 ELSE rankme_cloud.rate_limits.attempts+1 END
    RETURNING attempts`, [scope, bucket, seconds]);
  if (result.rows[0].attempts > limit) throw new BookingError(429, 'Please use the contact link without a reference.');
}

/** Anonymous, opt-in contact reference. This never accepts free text or patient fields. */
export async function issueReference(request: Request, input: unknown) {
  const body = record(input);
  only(body, ['department', 'source']);
  const chosen = department(body.department);
  const source: Source = ['google_search', 'google_maps'].includes(String(body.source)) ? body.source as Source : 'unknown';
  // A caller can forge forwarded IP headers; a global quota remains enforceable
  // at this boundary and caps both database growth and request volume.
  await issueRate('booking_issue_minute', 'lumident', 60, 60);
  await issueRate('booking_issue_day', 'lumident', 300, 86400);
  await getPool().query(`DELETE FROM rankme_cloud.booking_references WHERE code IN
    (SELECT r.code FROM rankme_cloud.booking_references r LEFT JOIN rankme_cloud.bookings b ON b.reference_code=r.code
     WHERE r.expires_at<now() AND b.id IS NULL LIMIT 20)`);
  await getPool().query(`DELETE FROM rankme_cloud.rate_limits WHERE (scope,bucket) IN
    (SELECT scope,bucket FROM rankme_cloud.rate_limits WHERE scope LIKE 'booking_issue_%' AND window_start<now()-interval '2 days' LIMIT 20)`);
  const owner = await getPool().query('SELECT id FROM rankme_cloud.owner LIMIT 1');
  if (!owner.rowCount) throw new BookingError(503, 'Booking references are temporarily unavailable.');
  const code = `LM-${randomBytes(6).toString('hex').toUpperCase()}`;
  await getPool().query(`INSERT INTO rankme_cloud.booking_references(code,owner_id,department,source) VALUES($1,$2,$3,$4)`,
    [code, owner.rows[0].id, chosen, source]);
  return { reference: code };
}

export async function listBookings(ownerId: string) {
  const totals = await getPool().query(`SELECT department,status,count(*)::int AS count FROM rankme_cloud.bookings
    WHERE owner_id=$1 AND clinic_key='lumident' AND test_mode=false GROUP BY department,status`, [ownerId]);
  const result = await getPool().query(`SELECT id,reference_code,department,source,status,test_mode,created_at,updated_at
    FROM rankme_cloud.bookings WHERE owner_id=$1 AND clinic_key='lumident' ORDER BY created_at DESC LIMIT 100`, [ownerId]);
  return { bookings: result.rows, totals: totals.rows, recent_limit: 100 };
}

export async function issueTestReference(ownerId: string, input: unknown) {
  const body = record(input);
  only(body, ['department']);
  const chosen = department(body.department);
  const code = `LM-${randomBytes(6).toString('hex').toUpperCase()}`;
  await getPool().query(`INSERT INTO rankme_cloud.booking_references(code,owner_id,department,source,test_mode)
    VALUES($1,$2,$3,'unknown',true)`, [code, ownerId, chosen]);
  return { reference: code, test_mode: true };
}

export async function lookupReference(ownerId: string, value: unknown) {
  const code = referenceCode(value);
  const result = await getPool().query(`SELECT r.code AS reference,r.department,r.source,r.test_mode,r.created_at,r.expires_at,
    b.id AS booking_id,b.status AS booking_status FROM rankme_cloud.booking_references r
    LEFT JOIN rankme_cloud.bookings b ON b.reference_code=r.code AND b.owner_id=r.owner_id
    WHERE r.owner_id=$1 AND r.code=$2`, [ownerId, code]);
  if (!result.rowCount) throw new BookingError(404, 'Reference not found. Record an unlinked booking only after staff confirmation.');
  return result.rows[0];
}

export async function confirmBooking(ownerId: string, request: Request, input: unknown) {
  const body = record(input);
  only(body, ['reference', 'department', 'test_mode']);
  const key = requestKey(request);
  const test = body.test_mode === true;
  if (body.test_mode !== undefined && typeof body.test_mode !== 'boolean') throw new BookingError(400, 'Invalid test flag.');
  const code = body.reference === undefined || body.reference === '' ? null : referenceCode(body.reference);
  const chosen = department(body.department);
  return withTransaction(async client => {
    await client.query('SELECT pg_advisory_xact_lock(hashtextextended($1, 0))', [`booking:${ownerId}:${key}`]);
    const existingKey = await client.query('SELECT id,reference_code,department,source,status,test_mode,created_at,updated_at FROM rankme_cloud.bookings WHERE owner_id=$1 AND request_key=$2', [ownerId, key]);
    if (existingKey.rowCount) {
      const old = existingKey.rows[0];
      if (old.reference_code !== code || old.department !== chosen || old.test_mode !== test)
        throw new BookingError(409, 'This request key was already used for another booking.');
      return old;
    }
    let source: Source = 'unknown';
    if (code) {
      const ref = await client.query(`SELECT department,source,test_mode FROM rankme_cloud.booking_references
        WHERE owner_id=$1 AND code=$2 AND expires_at>now() FOR UPDATE`, [ownerId, code]);
      if (!ref.rowCount) throw new BookingError(404, 'Reference not found or expired. Record an unlinked booking if staff confirmed it.');
      if (ref.rows[0].department !== chosen) throw new BookingError(409, 'The reference belongs to another department. Correct the department selection.');
      if (ref.rows[0].test_mode !== test) throw new BookingError(409, 'The reference and test setting do not match.');
      source = ref.rows[0].source;
      const existingRef = await client.query('SELECT id FROM rankme_cloud.bookings WHERE reference_code=$1', [code]);
      if (existingRef.rowCount) throw new BookingError(409, 'This reference is already linked to a booking.');
    }
    const id = randomUUID();
    const inserted = await client.query(`INSERT INTO rankme_cloud.bookings(id,owner_id,reference_code,request_key,department,source,status,test_mode)
      VALUES($1,$2,$3,$4,$5,$6,'confirmed',$7) RETURNING id,reference_code,department,source,status,test_mode,created_at,updated_at`,
      [id, ownerId, code, key, chosen, source, test]);
    await client.query(`INSERT INTO rankme_cloud.booking_events(booking_id,owner_id,action,department,status)
      VALUES($1,$2,'confirmed',$3,'confirmed')`, [id, ownerId, chosen]);
    return inserted.rows[0];
  });
}

export async function updateBooking(ownerId: string, id: string, input: unknown) {
  if (!/^[a-f0-9-]{36}$/i.test(id)) throw new BookingError(404, 'Booking not found.');
  const body = record(input);
  only(body, ['department', 'status']);
  if (body.department === undefined && body.status === undefined) throw new BookingError(400, 'Choose a correction.');
  const chosen = body.department === undefined ? null : department(body.department);
  if (body.status !== undefined && !['confirmed', 'cancelled'].includes(String(body.status))) throw new BookingError(400, 'Invalid booking status.');
  return withTransaction(async client => {
    const current = await client.query('SELECT department,status FROM rankme_cloud.bookings WHERE owner_id=$1 AND id=$2 FOR UPDATE', [ownerId, id]);
    if (!current.rowCount) throw new BookingError(404, 'Booking not found.');
    const nextDepartment = chosen ?? current.rows[0].department;
    const nextStatus = body.status ?? current.rows[0].status;
    if (nextDepartment === current.rows[0].department && nextStatus === current.rows[0].status) throw new BookingError(409, 'Booking already has this status and department.');
    const action = nextStatus === 'cancelled' ? 'cancelled' : current.rows[0].status === 'cancelled' ? 'reconfirmed' : 'corrected';
    const changed = await client.query(`UPDATE rankme_cloud.bookings SET department=$3,status=$4,updated_at=now()
      WHERE owner_id=$1 AND id=$2 RETURNING id,reference_code,department,source,status,test_mode,created_at,updated_at`,
      [ownerId, id, nextDepartment, nextStatus]);
    await client.query(`INSERT INTO rankme_cloud.booking_events(booking_id,owner_id,action,department,status)
      VALUES($1,$2,$3,$4,$5)`, [id, ownerId, action, nextDepartment, nextStatus]);
    return changed.rows[0];
  });
}

export async function bookingHistory(ownerId: string, id: string) {
  if (!/^[a-f0-9-]{36}$/i.test(id)) throw new BookingError(404, 'Booking not found.');
  const result = await getPool().query(`SELECT e.action,e.department,e.status,e.created_at FROM rankme_cloud.booking_events e
    JOIN rankme_cloud.bookings b ON b.id=e.booking_id WHERE b.owner_id=$1 AND b.id=$2 ORDER BY e.id ASC`, [ownerId, id]);
  if (!result.rowCount) throw new BookingError(404, 'Booking not found.');
  return { events: result.rows };
}

export async function exportBookingData(ownerId: string) {
  const [bookings, events] = await Promise.all([
    getPool().query(`SELECT id,reference_code,department,source,status,test_mode,created_at,updated_at
      FROM rankme_cloud.bookings WHERE owner_id=$1 AND clinic_key='lumident' ORDER BY created_at`, [ownerId]),
    getPool().query(`SELECT e.booking_id,e.action,e.department,e.status,e.created_at FROM rankme_cloud.booking_events e
      JOIN rankme_cloud.bookings b ON b.id=e.booking_id WHERE b.owner_id=$1 AND b.clinic_key='lumident' ORDER BY e.id`, [ownerId]),
  ]);
  return { bookings: bookings.rows, booking_events: events.rows };
}
