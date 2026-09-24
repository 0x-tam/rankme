'use strict';
// Run with: node tests/ui_measurement_smoke.js
// Exercise the real render functions with representative backend records.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8');
const context = vm.createContext({ URL, console, setTimeout, clearTimeout });
vm.runInContext(source.slice(0, source.indexOf("document.addEventListener('click'")), context);
const goal = { goal_type: 'lead', event_name: 'generate_lead', landing_page: 'https://example.com/contact' };
const current = { start: '2026-08-01', end: '2026-08-28', organic_sessions: 120, conversions: 8, event_name: goal.event_name };
const client = { id: 'client-a', conversion_goal: goal };
const state = {
  clients: [client], articles: [], jobs: [], events: [], settings: {},
  visibility: [{ id: client.id, client_id: client.id, conversion_measurement: { status: 'measured', goal, current } }],
  measurements: [{ id: 'measurement-a', client_id: client.id, source: 'seo', observed_at: '2026-09-01', snapshot: {
    periods: { current }, search_console: { current: { totals: { clicks: 25, impressions: 300 } } },
    conversion_measurement: { status: 'measured', goal, current }
  } }],
  experiments: [{ id: 'experiment-a', client_id: client.id, hypothesis: '<script>unsafe</script>', status: 'observing',
    started_at: '2026-09-02', change: 'Updated heading', baseline: current,
    evaluation: { reason: 'Waiting for comparable observations', current: null } }],
  opportunities: [{ id: 'opportunity-a', client_id: client.id, title: 'Review page', score: 55,
    conversion_score_reason: '<img src=x onerror=alert(1)> measured event evidence' }]
};
function setState(value) { vm.runInContext('state=' + JSON.stringify(value), context); }
function render() { return vm.runInContext('goalHistory(state.clients[0])', context); }
setState(state);
let html = render();
for (const phrase of ['generate_lead', 'GOAL EVENT OCCURRENCES', '>120<', '>8<', 'Cancel tracking',
  'Waiting for comparable observations', 'do not establish causation', '&lt;script&gt;']) assert(html.includes(phrase), phrase);
assert(!html.includes('<script>'));
assert(html.includes('data-action="experiment-create" data-id="client-a" >'));
// A stale goal/property must not advertise old counts as a current measurement.
state.visibility[0].conversion_measurement = { status: 'unavailable', issues: ['Sync the selected GA4 property.'], current };
setState(state);
html = render();
assert(html.includes('Sync the selected GA4 property.'));
assert(html.includes('data-action="experiment-create" data-id="client-a" disabled'));
const statusHTML = vm.runInContext('conversionStatusHTML(state.visibility[0].conversion_measurement)', context);
assert(!statusHTML.includes('>120<'));
assert(statusHTML.includes('Historical counts below remain evidence'));
// Cancellation explanation takes precedence over the prior evaluation reason.
state.experiments[0].status = 'cancelled';
state.experiments[0].interpretation = 'Cancelled; no conclusion.';
setState(state);
html = render();
assert(html.includes('Cancelled; no conclusion.'));
assert(!html.includes('Cancel tracking'));
// Persisted history is bounded for rendering but its total remains truthful.
state.measurements = Array.from({ length: 23 }, (_, i) => ({ ...state.measurements[0], id: 'm' + i }));
setState(state);
html = render();
assert(html.includes('23 observations'));
assert.equal((html.match(/data-action="measurement-details"/g) || []).length, 20);
state.measurement_counts = { 'client-a': 120 };
setState(state);
assert(render().includes('120 observations'));
assert(render().includes('/api/clients/client-a/measurement-history'));
state.measurement_counts = {};
// Capture real modal output without needing a browser DOM.
vm.runInContext('modal=function(title,html){capturedModal=html;}', context);
vm.runInContext("visibilityOpportunity('opportunity-a')", context);
const detail = vm.runInContext('capturedModal', context);
assert(detail.includes('Conversion evidence in this priority'));
assert(detail.includes('&lt;img'));
assert(!detail.includes('<img'));
vm.runInContext("measurementDetails('m0')", context);
assert(vm.runInContext('capturedModal', context).includes('measurement-json'));
state.clients = [{ id: 'client-a' }]; state.visibility = []; state.measurements = []; state.experiments = [];
setState(state);
html = render();
for (const phrase of ['Define the action that matters', 'No measurements saved yet', 'No experiments tracked yet', 'unconfigured']) assert(html.includes(phrase));
console.log('Measurement UI smoke checks passed: goals, stale data, history, experiments, evidence, and escaping.');
