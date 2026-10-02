const assert = require('node:assert/strict');
const {assessHealth, overallStatus} = require('../docs/health.js');

const now = Date.parse('2026-10-02T12:18:00Z');
const latest = {id: 42, status: 'completed', conclusion: 'success'};
const schedule = {id: 42, created_at: '2026-10-01T12:17:00Z'};
const data = {run_id: '42', data_status: 'WARN'};
assert.equal(assessHealth(data, latest, schedule, now).job, 'PASS');
assert.equal(assessHealth(data, {...latest, conclusion: 'failure'}, schedule, now).job, 'FAIL');
assert.equal(assessHealth({...data, run_id: '41'}, latest, schedule, now).job, 'FAIL');
assert.equal(assessHealth(null, latest, schedule, now).job, 'UNKNOWN');
assert.equal(assessHealth(data, latest, {created_at: '2026-09-30T12:17:00Z'}, now).job, 'UNKNOWN');
assert.equal(overallStatus('PASS', 'WARN'), 'WARN');
assert.equal(overallStatus('UNKNOWN', 'FAIL'), 'FAIL');
console.log('Workflow health cases passed');
