import assert from 'node:assert/strict';
import { test } from 'node:test';
import { allowedCommand, chatseoSnapshotRead, handleWorker, validateChatseoReferences } from '../cloud/bridge';

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
  assert.equal(allowedCommand('POST', `/api/clients/${id}/aio-collect`, {}), true);
  assert.equal(allowedCommand('POST', `/api/clients/${id}/evidence-audit`, { force: true }), false);
  assert.equal(allowedCommand('POST', `/api/clients/${id}/evidence-report`, { format: 'pdf' }), true);
  assert.equal(allowedCommand('POST', `/api/clients/${id}/evidence-report`, { format: 'html' }), false);
  assert.equal(allowedCommand('POST', `/api/clients/${id}/aio-history`, { query: 'solar maintenance' }), true);
  assert.equal(allowedCommand('POST', `/api/clients/${id}/aio-history`, { query: 'solar maintenance', allClients: true }), false);
  assert.equal(allowedCommand('POST', `/api/clients/${id}/experience`, { claim: 'A confirmed first-hand service fact.', source_note: 'Owner records', confirmed: true }), true);
  assert.equal(allowedCommand('PATCH', `/api/clients/${id}`, { growth_settings: { enabled: true, interval_days: 1, jev_enabled: false } }), true);
  assert.equal(allowedCommand('PATCH', `/api/clients/${id}`, { growth_settings: { enabled: true, interval_days: 0 } }), false);
  assert.equal(allowedCommand('PATCH', `/api/clients/${id}`, { aio_settings: { api_key: 'secret' } }), false);
});

test('worker endpoints reject missing bearer before database access', async () => {
  const response = await handleWorker(new Request('https://example.test/api/worker/poll', { method: 'POST', body: '{}' }));
  assert.equal(response.status, 401);
});

test('ChatSEO bridge accepts only bounded commands and keeps connection setup local', () => {
  const path = `/api/clients/${id}/chatseo-`;
  const send = { message: 'Research this site', credit_acknowledged: true, request_id: '12345678-1234-1234-1234-123456789abc' };
  for (const action of ['sync', 'disconnect']) assert.equal(allowedCommand('POST', `/api/chatseo/${action}`, {}), true);
  assert.equal(allowedCommand('POST', `${path}map`, { site_id: 'site_1' }), true);
  assert.equal(allowedCommand('POST', `${path}settings`, { enabled: true, daily_request_limit: 2, minimum_credits: 10 }), true);
  assert.equal(allowedCommand('POST', `${path}sync`, {}), true);
  assert.equal(allowedCommand('POST', `${path}send`, send), true);
  assert.equal(allowedCommand('POST', `${path}poll`, { run_id: id }), true);
  assert.equal(allowedCommand('POST', `${path}read`, { run_id: id }), true);
  assert.equal(allowedCommand('POST', `${path}read`, { run_id: id, force: true }), false);
  assert.equal(allowedCommand('POST', `${path}resolve`, { run_id: id, checked_provider: true }), true);
  assert.equal(allowedCommand('POST', `${path}resolve`, { run_id: id, checked_provider: false }), false);
  assert.equal(allowedCommand('POST', `${path}resolve`, { run_id: id, checked_provider: true, force: true }), false);
  assert.equal(allowedCommand('POST', `${path}import`, { run_id: id, artifact_id: 'artifact_1' }), true);
  for (const action of ['connect', 'configure', 'callback', 'execute', 'retry']) assert.equal(allowedCommand('POST', `/api/chatseo/${action}`, {}), false);
  assert.equal(allowedCommand('POST', `${path}send`, { ...send, credit_acknowledged: false }), false);
  assert.equal(allowedCommand('POST', `${path}send`, { ...send, request_id: id }), false);
  assert.equal(allowedCommand('POST', `${path}send`, { ...send, message: ' '.repeat(4) }), false);
  assert.equal(allowedCommand('POST', `${path}send`, { ...send, message: 'x'.repeat(8001) }), false);
  for (const key of ['token', 'authorization', 'endpoint', 'tool', 'force', 'api_key']) assert.equal(allowedCommand('POST', `${path}send`, { ...send, [key]: 'unsafe' }), false);
  assert.equal(allowedCommand('POST', `${path}map`, { site_id: 'https://evil.test' }), false);
  assert.equal(allowedCommand('POST', `${path}import`, { run_id: id, artifact_id: '../artifact' }), false);
  for (const daily of [0, 11, true, 1.5]) assert.equal(allowedCommand('POST', `${path}settings`, { enabled: true, daily_request_limit: daily, minimum_credits: 1 }), false);
  assert.equal(allowedCommand('POST', `${path}settings`, { enabled: true, daily_request_limit: 1, minimum_credits: 0 }), false);
  assert.equal(allowedCommand('PATCH', `${path}send`, send), false);
});


test('ChatSEO references and snapshot reads stay within the selected client and site', () => {
  const other = 'b'.repeat(32);
  const run = 'c'.repeat(32);
  const snapshot = {
    clients: [{ id, chatseo_site_id: 'site_1' }, { id: other, chatseo_site_id: 'site_2' }],
    chatseo: { connected: true, sites: [{ id: 'site_1' }] },
    chatseo_reports: [{ client_id: id, site_id: 'site_1' }, { client_id: other, site_id: 'site_2' }],
    chatseo_runs: [{ id: run, client_id: id, site_id: 'site_1', artifacts: [{ id: 'artifact_1' }] }],
  };
  const path = `/api/clients/${id}/chatseo-`;
  assert.doesNotThrow(() => validateChatseoReferences(`${path}map`, { site_id: 'site_1' }, snapshot));
  assert.doesNotThrow(() => validateChatseoReferences(`${path}import`, { run_id: run, artifact_id: 'artifact_1' }, snapshot));
  assert.throws(() => validateChatseoReferences(`${path}map`, { site_id: 'site_2' }, snapshot), /connected account/);
  assert.throws(() => validateChatseoReferences(`${path}import`, { run_id: run, artifact_id: 'foreign' }, snapshot), /artifact/);
  assert.throws(() => validateChatseoReferences(`/api/clients/${other}/chatseo-poll`, { run_id: run }, snapshot), /does not belong/);
  assert.throws(() => validateChatseoReferences(`${path}poll`, { run_id: run }, { ...snapshot, clients: [{ id, chatseo_site_id: 'changed' }] }), /does not belong/);
  assert.throws(() => validateChatseoReferences(`${path}send`, {}, {}), /Client not found/);
  assert.throws(() => validateChatseoReferences(`/api/clients/${other}/chatseo-read`, { run_id: run }, snapshot), /does not belong/);
  assert.doesNotThrow(() => validateChatseoReferences(`${path}resolve`, { run_id: run, checked_provider: true }, snapshot));
  assert.throws(() => validateChatseoReferences(`/api/clients/${other}/chatseo-resolve`, { run_id: run, checked_provider: true }, snapshot), /does not belong/);
  assert.deepEqual(chatseoSnapshotRead('/api/chatseo/sites', snapshot), { sites: [{ id: 'site_1' }] });
  assert.deepEqual(chatseoSnapshotRead(`/api/clients/${other}/chatseo`, snapshot), { report: snapshot.chatseo_reports[1], runs: [] });
  assert.throws(() => chatseoSnapshotRead(`/api/clients/${run}/chatseo`, snapshot), /Client not found/);
  assert.equal(chatseoSnapshotRead('/api/chatseo/callback', snapshot), null);
  assert.equal(chatseoSnapshotRead('/api/chatseo/status', {})?.connected, false);
});
