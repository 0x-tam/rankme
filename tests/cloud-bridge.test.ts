import assert from 'node:assert/strict';
import { test } from 'node:test';
import { allowedCommand, handleWorker } from '../cloud/bridge';

const id = 'a'.repeat(32);

test('cloud mutation allowlist mirrors local worker', () => {
  assert.equal(allowedCommand('POST', `/api/articles/${id}/publish`, {}), true);
  assert.equal(allowedCommand('PATCH', '/api/settings', { paused: true }), true);
  assert.equal(allowedCommand('PATCH', '/api/settings', { codex_path: '/tmp/tool' }), false);
  assert.equal(allowedCommand('PATCH', `/api/clients/${id}`, { connection: { deploy_command: ['sh'] } }), false);
  assert.equal(allowedCommand('POST', `/api/articles/${id}/publish`, { force: true }), false);
  assert.equal(allowedCommand('PATCH', `/api/clients/${id}`, { profile: { nested: { api_key: 'secret' } } }), false);
  assert.equal(allowedCommand('POST', '/api/google/configure', { client_secret: 'secret' }), false);
});

test('worker endpoints reject missing bearer before database access', async () => {
  const response = await handleWorker(new Request('https://example.test/api/worker/poll', { method: 'POST', body: '{}' }));
  assert.equal(response.status, 401);
});
