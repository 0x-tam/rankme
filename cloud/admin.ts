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
