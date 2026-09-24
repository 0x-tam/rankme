import { createHash, randomBytes } from 'node:crypto';
import type { Pool } from 'pg';
import { withTransaction } from './db.js';

/** Returns a one-time secret to the caller. Only an explicit operator command should expose it. */
export async function issueBootstrap(pool: Pool): Promise<string> {
  const secret = randomBytes(32).toString('base64url');
  const digest = createHash('sha256').update(secret).digest('hex');
  await withTransaction(async client => {
    await client.query('SELECT pg_advisory_xact_lock($1)', [72149302]);
    const owner = await client.query('SELECT 1 FROM rankme_cloud.owner LIMIT 1');
    if (owner.rowCount) throw new Error('An owner is already enrolled');
    await client.query(`INSERT INTO rankme_cloud.bootstrap(singleton,secret_hash,expires_at)
      VALUES(true,$1,now()+interval '10 minutes') ON CONFLICT(singleton) DO UPDATE
      SET secret_hash=EXCLUDED.secret_hash,expires_at=EXCLUDED.expires_at,created_at=now()`, [digest]);
  }, pool);
  return secret;
}

/** Operator-only invitation for the existing owner; the database stores only its digest. */
export async function issuePasskeyInvite(pool: Pool): Promise<string> {
  const secret = randomBytes(32).toString('base64url');
  const digest = createHash('sha256').update(secret).digest('hex');
  await withTransaction(async client => {
    await client.query('SELECT pg_advisory_xact_lock($1)', [72149304]);
    const owner = await client.query('SELECT id FROM rankme_cloud.owner LIMIT 1');
    if (owner.rowCount !== 1) throw new Error('Exactly one existing owner is required');
    const ownerId = owner.rows[0].id;
    const count = await client.query('SELECT count(*)::int AS count FROM rankme_cloud.credentials WHERE owner_id=$1', [ownerId]);
    if (count.rows[0].count >= 10) throw new Error('Owner already has the maximum number of passkeys');
    await client.query(`DELETE FROM rankme_cloud.challenges WHERE kind='invite' AND invite_hash IN
      (SELECT secret_hash FROM rankme_cloud.passkey_invites WHERE owner_id=$1)`, [ownerId]);
    await client.query(`INSERT INTO rankme_cloud.passkey_invites(owner_id,secret_hash,expires_at)
      VALUES($1,$2,now()+interval '10 minutes') ON CONFLICT(owner_id) DO UPDATE
      SET secret_hash=EXCLUDED.secret_hash,expires_at=EXCLUDED.expires_at,created_at=now()`, [ownerId, digest]);
  }, pool);
  return secret;
}
