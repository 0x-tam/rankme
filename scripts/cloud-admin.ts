import { writeFile } from 'node:fs/promises';
import { Pool } from 'pg';
import { migrate } from '../cloud/db.js';
import { issueBootstrap } from '../cloud/admin.js';

async function main(): Promise<void> {
  const [command, flag, file] = process.argv.slice(2);
  if (command === 'migrate') {
    const versions = await migrate();
    process.stdout.write(`Applied migrations: ${versions.length ? versions.join(', ') : 'none'}\n`);
    return;
  }
  if (command !== 'bootstrap' || (flag && flag !== '--output') || (flag === '--output' && !file)) {
    throw new Error('Usage: cloud-admin.ts migrate | bootstrap [--output private-file]');
  }
  if (!file && !process.stdout.isTTY) throw new Error('Bootstrap link requires a TTY or --output private-file');
  const connectionString = process.env.DATABASE_URL_UNPOOLED;
  if (!connectionString) throw new Error('DATABASE_URL_UNPOOLED is required');
  const origin = process.env.RANKME_PUBLIC_ORIGIN ?? 'https://getrankme.vercel.app';
  const parsed = new URL(origin);
  if (parsed.origin !== origin) throw new Error('RANKME_PUBLIC_ORIGIN must be an exact origin');
  const pool = new Pool({ connectionString, max: 1 });
  try {
    const secret = await issueBootstrap(pool);
    const link = `${origin}/#bootstrap=${secret}\n`;
    if (file) {
      await writeFile(file, link, { mode: 0o600, flag: 'wx' });
      process.stdout.write('Bootstrap link written to private file. It expires in 10 minutes.\n');
    } else {
      process.stdout.write(link);
    }
  } finally { await pool.end(); }
}

main().catch(error => { process.stderr.write(`${error.message}\n`); process.exitCode = 1; });
