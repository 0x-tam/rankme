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
  for (const action of ['stop', 'dismiss', 'retry']) assert.equal(allowedCommand('POST', `/api/jobs/${id}/${action}`, {}), true);
  assert.equal(allowedCommand('POST', `/api/jobs/${id}/stop`, { force: true }), false);
  for (const action of ['remove', 'remove-cover']) assert.equal(allowedCommand('POST', `/api/articles/${id}/${action}`, {}), true);
  assert.equal(allowedCommand('POST', `/api/clients/${id}/remove`, { confirm: 'eoncoatings.com' }), true);
  assert.equal(allowedCommand('POST', `/api/clients/${id}/remove`, {}), false);
  assert.equal(allowedCommand('POST', `/api/clients/${id}/remove`, { confirm: 'x.com', extra: 1 }), false);
});

test('worker endpoints reject missing bearer before database access', async () => {
  const response = await handleWorker(new Request('https://example.test/api/worker/poll', { method: 'POST', body: '{}' }));
  assert.equal(response.status, 401);
});
