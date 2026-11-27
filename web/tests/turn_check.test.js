// Dependency-free checks of an answer's mark as every client draws it (lib/turn-renderer.js renderCheck).
// Owner, 2026-11-27: the answer is served before its second check has answered. While it is being checked its mark
// is a quiet pulse with no reason and no Retry; it then settles into no mark, or a warning with its reason.
const assert = require('assert');
const path = require('path');

require(path.join(__dirname, '..', 'public', 'lib', 'turn-renderer.js'));
const R = globalThis.PrereasonerTurnRenderer;

const checking = R.renderCheck({state: 'checking', confidence: 0.9}, 'data-retry="0"');
assert.match(checking, /class="turn-check check-checking"/);
assert.match(checking, /role="status"/);
assert.match(checking, /Checking this answer/);
assert.ok(!/<details|<button|checkretry/.test(checking), 'an answer being checked has no reason to open and no Retry');

assert.strictEqual(R.renderCheck({state: 'answered', confidence: 0.9}, 'data-retry="0"'), '', 'an answered answer has no mark');
assert.strictEqual(R.renderCheck(null, 'data-retry="0"'), '');

const warned = R.renderCheck({state: 'warned', reason: 'I read this as "the total of Amount", 50% sure.'}, 'data-retry="0"');
assert.match(warned, /<details class="turn-check check-warned">/);
assert.match(warned, /I read this as &quot;the total of Amount&quot;, 50% sure\./);
assert.match(warned, /<button type="button" class="checkretry" data-retry="0">Retry<\/button>/);

// The turn's mark is its last engine call's: an answer served most likely wrong, then its retry's.
const traces = [
  {jobId: 't_0', engine: {check: {state: 'erred', reason: 'This answer is most likely wrong.'}}},
  {jobId: 't_1', engine: {check: {state: 'retried', reason: 'My reading was most likely wrong, so Gemini 4 Neon wrote this query.'}}}
];
assert.strictEqual(R.turnCheck(traces).state, 'retried');
assert.strictEqual(R.turnCheck([{jobId: 't_0', engine: {check: {state: 'checking'}}}]).state, 'checking');

console.log('turn check: ok');
