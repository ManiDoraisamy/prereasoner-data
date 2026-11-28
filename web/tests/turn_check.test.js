// Dependency-free checks of an answer's mark as every client draws it (lib/turn-renderer.js).
// Owner, 2026-11-27: "never ever wait for neon to show engine's answer. Always show engine's answer immediately ... and
// show only icons for: Very likely correct, likely Correct, likely Wrong, Very likely Wrong. And the icon can change
// into retry loading for errors like Very likely Wrong". The mark is the engine's own (check.level), on screen with
// the answer: there is no state in which an answer waits for its mark. An answer very likely wrong is retried in the
// background: its icon loads, and the retry's answer takes its place in the turn and in the transcript.
const assert = require('assert');
const path = require('path');

require(path.join(__dirname, '..', 'public', 'lib', 'turn-renderer.js'));
const R = globalThis.PrereasonerTurnRenderer;

// Four levels, four icons, each with its reason on a click.
const levels = {
  very_likely_correct: ['answered', 'Very likely correct'],
  likely_correct: ['answered', 'Likely correct'],
  likely_wrong: ['warned', 'Likely wrong'],
  very_likely_wrong: ['erred', 'Very likely wrong']
};
const icons = new Set();
Object.keys(levels).forEach(level => {
  const html = R.renderCheck({state: levels[level][0], level, reason: 'I read this as "the total of Amount", 70% sure.'}, 'data-retry="0"');
  assert.match(html, new RegExp('<details class="turn-check check-' + level + '">'));
  assert.match(html, new RegExp('aria-label="' + levels[level][1] + '"'));
  assert.match(html, /I read this as &quot;the total of Amount&quot;, 70% sure\./);
  icons.add(/<summary[^>]*>([^<]+)<\/summary>/.exec(html)[1]);
  // the Retry is offered where the engine doubts its answer, never on one it stands behind
  assert.strictEqual(/checkretry/.test(html), level === 'likely_wrong' || level === 'very_likely_wrong', level);
});
assert.strictEqual(icons.size, 4, 'each level has its own icon');
assert.strictEqual(R.renderCheck(null, 'data-retry="0"'), '');
assert.strictEqual(R.renderCheck({state: 'checking'}, 'data-retry="0"'), '', 'no waiting state is drawn');

// A turn stored before the four levels holds its state alone.
assert.match(R.renderCheck({state: 'warned', reason: 'r'}, 'data-retry="0"'), /check-likely_wrong/);
assert.match(R.renderCheck({state: 'erred', reason: 'r'}, 'data-retry="0"'), /check-very_likely_wrong/);
assert.strictEqual(R.renderCheck({state: 'answered'}, 'data-retry="0"'), '', 'a stored answer with no level has no mark');

// An answer served very likely wrong is owed its retry; while the retry runs the icon loads and offers no Retry.
const erred = {state: 'erred', level: 'very_likely_wrong', confidence: 0.31,
  reason: 'This answer is very likely wrong. I read this as “the count of Region”, 31% sure.'};
assert.ok(R.owesRetry(erred));
assert.ok(!R.owesRetry({state: 'warned', level: 'likely_wrong'}), 'a likely-wrong answer stays the engine\'s');
assert.ok(!R.owesRetry({state: 'answered', level: 'likely_correct'}) && !R.owesRetry(null));
const loading = R.renderCheck(erred, 'data-retry="0"', true);
assert.match(loading, /check-very_likely_wrong check-retrying/);
assert.match(loading, /role="status"/);
assert.match(loading, /<span class="checkspin"><\/span>/);
assert.match(loading, /Very likely wrong\. Retrying/);
assert.ok(!/checkretry/.test(loading), 'a retry that is running offers no second one');

// The retry's answer takes the turn's place and keeps what the first answer was.
const turn = {reply: '42\n\n3 rows could not be matched.', check: erred};
const retriedCheck = {state: 'retried', level: 'likely_correct', confidence: null,
  reason: 'Retried: Gemini 4 Neon wrote this query. Adds Amount over the East rows.', retry: {sql: 'SELECT SUM("Amount") FROM "orders"'}};
const revised = R.revisedTurn(turn, {reply: '876.50', check: retriedCheck});
assert.strictEqual(revised.replaced, true);
assert.strictEqual(revised.reply, '876.50');
assert.deepStrictEqual(revised.check.first, {reply: '42', reason: erred.reason});
const shown = R.renderCheck(revised.check, 'data-retry="0"');
assert.match(shown, /check-likely_correct/);
assert.match(shown, /First answer: 42 This answer is very likely wrong\./);
assert.match(shown, /SELECT SUM\(&quot;Amount&quot;\) FROM &quot;orders&quot;/);
assert.match(shown, /checkretry/, 'a retried answer can be retried again');
assert.ok(!R.owesRetry(revised.check));

// The retry's word that the sheet cannot answer the question takes the turn's place, with no mark: its check says the
// retry ran and found the question unanswerable.
const cannot = {state: 'retried', level: null, answerable: false, reason: 'The sheet has no column of costs.'};
const unanswerable = R.revisedTurn(turn, {reply: 'The sheet has no column of costs.', check: cannot});
assert.deepStrictEqual(unanswerable, {replaced: true, reply: 'The sheet has no column of costs.', check: null});
assert.strictEqual(R.renderCheck(cannot, 'data-retry="0"'), '', 'a question back carries no mark');

// A reply with no check is not the retry's: it is the chat's sentence for a failed engine call. It never takes the
// answer's place (until 2026-11-28 it did, and the mark went with it).
const sentence = R.revisedTurn(turn, {reply: "Prereasoner couldn't answer that right now. Please ask again in a moment.", check: null});
assert.strictEqual(sentence.replaced, false);
assert.strictEqual(sentence.reply, turn.reply);
assert.strictEqual(sentence.check.level, 'very_likely_wrong');
assert.strictEqual(sentence.check.note, 'The retry did not finish.');

// A retry that did not answer leaves the turn's answer and its reason, with a note of why; it is not asked again by
// itself, and its failed query is not shown under an answer it did not give.
const failed = R.revisedTurn(turn, {reply: 'x', check: {state: 'erred', level: 'very_likely_wrong',
  reason: 'The retry could not write a query that runs on this sheet.', retry: {sql: 'SELECT nothing'}}});
assert.strictEqual(failed.replaced, false);
assert.strictEqual(failed.reply, turn.reply);
assert.strictEqual(failed.check.reason, erred.reason);
assert.strictEqual(failed.check.note, 'The retry could not write a query that runs on this sheet.');
assert.deepStrictEqual(failed.check.retry, {failed: true});
assert.ok(!R.owesRetry(failed.check));
const lost = R.revisedTurn(turn, null);
assert.strictEqual(lost.replaced, false);
assert.strictEqual(lost.check.note, 'The retry did not finish.');
assert.ok(!R.owesRetry(lost.check));
const lostHtml = R.renderCheck(lost.check, 'data-retry="0"');
assert.match(lostHtml, /checkretry/, 'the person can still retry it');
assert.match(lostHtml, /<p class="checknote">The retry did not finish\.<\/p>/);
assert.ok(!/checksql/.test(lostHtml));
// failing twice says so once
assert.strictEqual(R.revisedTurn({reply: turn.reply, check: lost.check}, null).check.note, 'The retry did not finish.');

// A second Retry on an answer a retry already gave: when it fails the answer keeps its own query and reason; when it
// answers, the first answer shown is still the engine's.
const again = R.revisedTurn({reply: revised.reply, check: revised.check}, null);
assert.strictEqual(again.replaced, false);
assert.deepStrictEqual(again.check.retry, retriedCheck.retry);
assert.strictEqual(again.check.reason, retriedCheck.reason);
assert.strictEqual(again.check.note, 'The retry did not finish.');
const twice = R.revisedTurn({reply: revised.reply, check: revised.check}, {reply: '900', check: retriedCheck});
assert.deepStrictEqual(twice.check.first, {reply: '42', reason: erred.reason});

// The transcript the assistant reads: the retry is asked with what came before the turn, and its answer replaces the
// turn's there.
const history = [
  {role: 'user', content: 'total amount'}, {role: 'assistant', content: '900'},
  {role: 'user', content: 'how many regions'}, {role: 'assistant', content: turn.reply},
  {role: 'user', content: 'and for East?'}, {role: 'assistant', content: '120'}
];
assert.deepStrictEqual(R.historyBefore(history, 'how many regions', turn.reply), history.slice(0, 2));
assert.deepStrictEqual(R.historyBefore(history, 'a question it never held', 'x'), history);
const after = R.revisedHistory(history, 'how many regions', turn.reply, '876.50');
assert.strictEqual(after[3].content, '876.50');
assert.strictEqual(after.length, 6);
assert.strictEqual(history[3].content, turn.reply, 'the transcript passed in is not changed');

// The turn's mark is its last engine call's.
const traces = [
  {jobId: 't_0', engine: {check: {state: 'answered', level: 'likely_correct'}}},
  {jobId: 't_1', engine: {check: erred}}
];
assert.strictEqual(R.turnCheck(traces).level, 'very_likely_wrong');
assert.strictEqual(R.turnCheck([{jobId: 't_0', engine: {}}]), null);

console.log('turn check: ok');
