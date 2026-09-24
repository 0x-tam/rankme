import { open, unlink } from 'node:fs/promises';
import { Pool } from 'pg';
import { migrate } from '../cloud/db.js';
import { issueBootstrap, issuePasskeyInvite } from '../cloud/admin.js';

async function main(): Promise<void> {
  const [command, flag, file] = process.argv.slice(2);
  if (command === 'migrate') {
    const versions = await migrate();
    process.stdout.write(`Applied migrations: ${versions.length ? versions.join(', ') : 'none'}\n`);
    return;
  }
  if (!['bootstrap', 'invite-passkey'].includes(command) || (flag && flag !== '--output') || (flag === '--output' && !file)) {
    throw new Error('Usage: cloud-admin.ts migrate | bootstrap [--output private-file] | invite-passkey [--output private-file]');
  }
  if (!file && !process.stdout.isTTY) throw new Error('Secret link requires a TTY or --output private-file');
  const connectionString = process.env.DATABASE_URL_UNPOOLED;
  if (!connectionString) throw new Error('DATABASE_URL_UNPOOLED is required');
  const origin = process.env.RANKME_PUBLIC_ORIGIN ?? 'https://getrankme.vercel.app';
  const parsed = new URL(origin);
  if (parsed.origin !== origin) throw new Error('RANKME_PUBLIC_ORIGIN must be an exact origin');
  const pool = new Pool({ connectionString, max: 1 });
  const output = file ? await open(file, 'wx', 0o600) : null;
  try {
    const secret = command === 'bootstrap' ? await issueBootstrap(pool) : await issuePasskeyInvite(pool);
    const link = `${origin}/#${command === 'bootstrap' ? 'bootstrap' : 'add-passkey'}=${secret}\n`;
    if (output) {
      await output.writeFile(link);
      process.stdout.write(`${command === 'bootstrap' ? 'Bootstrap' : 'Passkey invitation'} link written to private file. It expires in 10 minutes.\n`);
    } else {
      process.stdout.write(link);
    }
  } catch (error) {
    if (output) await unlink(file!).catch(() => {});
    throw error;
  } finally { await output?.close(); await pool.end(); }
}

main().catch(error => { process.stderr.write(`${error.message}\n`); process.exitCode = 1; });
