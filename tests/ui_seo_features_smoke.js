'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const base = fs.readFileSync('static/app.js', 'utf8');
const feature = fs.readFileSync('static/seo-features.js', 'utf8');
const context = vm.createContext({ URL, console, setTimeout, clearTimeout,
  document: { documentElement: { dataset: {} } } });
vm.runInContext(base.slice(0, base.indexOf("document.addEventListener('pointerdown'")), context);
vm.runInContext(feature.slice(0, feature.indexOf("document.addEventListener('click'")), context);
const client = { id: 'a'.repeat(32), name: 'Example', url: 'https://example.com',
  aio_settings: { enabled: false, interval_days: 1, queries: ['solar repair'], country: 'us', language: 'en', location: '', device: 'desktop' },
  growth_settings: { enabled: true, interval_days: 1 } };
const payload = { clients: [client], articles: [], jobs: [], events: [], settings: {}, aio_status: {
  configured: true, status: 'ready', calls_today: 0, daily_call_cap: 6 },
  aio_summaries: [{ client_id: client.id, query: 'solar repair', summary: {
    sample_days: 1, present_days: 1, absent_days: 0, basis_source: 'manual',
    daily_evidence: [{ date: '2026-10-07', status: 'present', source: 'manual', answer: '<img src=x onerror=alert(1)>' },
      { date: '2026-10-06', status: 'absent', source: 'browser' },
      { date: '2026-10-05', status: 'present', source: 'provider' }],
    citations: [{ url: 'javascript:alert(1)', title: '<script>unsafe</script>', days_cited: 1 }],
    citation_changes: [] }, brief: { status: 'insufficient_evidence' } }],
  evidence_audits: [{ client_id: client.id, observed_at: '2026-10-07', audit: { summary: { pages_observed: 2, queries_observed: 1 },
    actions: [{ id: 'one', title: '<svg onload=alert(1)>', kind: 'gsc_gap_candidate', status: 'needs_verification' }] } }],
  experience_claims: [{ id: 'b'.repeat(32), client_id: client.id, claim: '<b>untrusted</b>', source_note: 'Owner note', user_confirmed: false }] };
vm.runInContext('state=' + JSON.stringify(payload), context);
const html = vm.runInContext('seoFeaturesWorkspace(state.clients[0])', context);
for (const text of ['AI Overview monitor', 'Evidence audit', 'Business experience', 'Recurring audit every 1 day(s)',
  'Manual evidence', 'Local Chrome observation', 'Provider observation',
  '&lt;img src=x onerror=alert(1)&gt;', '&lt;svg onload=alert(1)&gt;', '&lt;b&gt;untrusted&lt;/b&gt;']) assert(html.includes(text), text);
assert(!html.includes('<script>unsafe</script>'));
assert(!html.includes('href="javascript:'));
console.log('SEO workspace render and untrusted content smoke passed.');
