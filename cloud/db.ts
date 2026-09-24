import { readFile, readdir } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { Pool, type PoolClient } from 'pg';
import { attachDatabasePool } from '@neon/functions';

let sharedPool: Pool | undefined;

export function getPool(): Pool {
  if (!sharedPool) {
    const connectionString = process.env.DATABASE_URL;
    if (!connectionString) throw new Error('DATABASE_URL is required');
    sharedPool = new Pool({ connectionString, max: 5 });
    attachDatabasePool(sharedPool);
  }
  return sharedPool;
}

export async function withTransaction<T>(fn: (client: PoolClient) => Promise<T>, pool = getPool()): Promise<T> {
  const client = await pool.connect();
  try {
    await client.query('BEGIN');
    const result = await fn(client);
    await client.query('COMMIT');
    return result;
  } catch (error) {
    await client.query('ROLLBACK');
    throw error;
  } finally {
    client.release();
  }
}

/** Explicit admin operation. Requires a direct connection and never runs on API startup. */
export async function migrate(connectionString = process.env.DATABASE_URL_UNPOOLED): Promise<number[]> {
  if (!connectionString) throw new Error('DATABASE_URL_UNPOOLED is required for migrations');
  const pool = new Pool({ connectionString, max: 1 });
  const applied: number[] = [];
  try {
    const files = (await readdir(fileURLToPath(new URL('./migrations/', import.meta.url))))
      .filter(name => /^\d+_[a-z_]+\.sql$/.test(name)).sort();
    for (const file of files) {
      const version = Number(file.split('_')[0]);
      await withTransaction(async client => {
        await client.query('SELECT pg_advisory_xact_lock($1)', [72149301]);
        await client.query('CREATE SCHEMA IF NOT EXISTS rankme_cloud');
        await client.query('CREATE TABLE IF NOT EXISTS rankme_cloud.schema_migrations (version integer PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())');
        const seen = await client.query('SELECT 1 FROM rankme_cloud.schema_migrations WHERE version=$1', [version]);
        if (seen.rowCount) return;
        await client.query(await readFile(new URL(`./migrations/${file}`, import.meta.url), 'utf8'));
        await client.query('INSERT INTO rankme_cloud.schema_migrations(version) VALUES($1)', [version]);
        applied.push(version);
      }, pool);
    }
    return applied;
  } finally {
    await pool.end();
  }
}
