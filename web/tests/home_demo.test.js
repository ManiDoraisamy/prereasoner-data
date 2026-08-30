// Dependency-free checks for the default home-page conversion demo.
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const html = fs.readFileSync(path.join(__dirname, '..', 'public', 'index.html'), 'utf8');

function stringConstant(name) {
  const match = html.match(new RegExp(`const ${name}=('(?:\\\\.|[^'])*');`));
  assert(match, `missing ${name} demo constant`);
  return vm.runInNewContext(match[1]);
}

const orders = stringConstant('ORD');
const orderLines = orders.split('\n');

assert.strictEqual(
  orderLines[0],
  'order ID,customer,ordered,currency,amount',
  'currency must appear immediately before amount',
);
assert.strictEqual(orderLines.length, 24, 'the demo must retain all 23 orders');
assert(orderLines.slice(1).every(line => /,(USD|EUR|GBP|INR),[0-9.]+$/.test(line)),
  'every demo order must have a supported currency code before amount');
// FX comes from the knowledgebase (ECB daily sync -> knowledgebase."exchange_rate"), never from a
// demo sheet: an illustrative rate table on the page would SHADOW the real rates, because an
// uploaded rate sheet deliberately wins over the knowledge join (own data first).
assert(!/const FX=/.test(html), 'no illustrative rate sheet may ship on the page');
assert(!/illustrative fx rates/.test(html), 'the default workbook must not include a rate sheet');
assert(html.includes('>total amount in France in US dollars</textarea>'),
  'the default question must exercise world filtering and currency conversion');

// The chat widget is the hero: the paper-carousel column is gone (the two cards live on as
// standalone SVGs published for the marketing site, not embedded here).
assert(!/interpretable\.svg|hero-right/.test(html), 'the home page must not embed the paper carousel');
for (const svg of ['anthropic-paper.svg', 'interpretability-blog.svg']) {
  assert(fs.existsSync(path.join(__dirname, '..', 'public', svg)), `${svg} must be published for the marketing site`);
}

// Single-source landings: /sheets, /excel and /csv serve this page (firebase.json rewrites) and
// narrow the bare "+" button to that one source. The picker lives at /picker — its path is
// irrelevant to Google (the browser key is restricted by ORIGIN, not path), measured 2026-08-29.
assert(html.includes('<span class=pl>+</span></button>'), 'the add button is a bare "+" everywhere');
assert(html.includes("location.href='picker'"), 'the Google Sheets flow must round-trip via /picker');
assert(html.includes('>Attach a spreadsheet and ask a question</div>'),
  'the generic headline invites the attach-and-ask action');
assert(html.includes('>Watch how our model arrives at the answer.</div>'),
  'the subline promises the visible derivation');
assert(html.includes('placeholder="What question do you have about your spreadsheet?"'),
  'the placeholder must not repeat the headline');
// Each landing rewrites the headline, placeholder, and the bare "+"'s accessible name to its source.
for (const [mode, noun, ph] of [
  ['sheets', 'Attach a Google Sheet and ask a question', 'What question do you have about this Sheet?'],
  ['excel', 'Attach an Excel sheet and ask a question', 'What question do you have about this Excel sheet?'],
  ['csv', 'Attach a CSV file and ask a question', 'What question do you have about this CSV?'],
]) {
  assert(new RegExp(`\\b${mode}: *\\{src:`).test(html), `/${mode} must be a landing route`);
  assert(html.includes(`h1:'${noun}'`), `/${mode} must retitle the headline`);
  assert(html.includes(`ph:'${ph}'`), `/${mode} must reword the placeholder`);
}
assert(/lbl:'Add a Google Sheet'/.test(html) && /lbl:'Add an Excel file'/.test(html) && /lbl:'Add a CSV file'/.test(html),
  'each landing must keep an accessible label for the bare "+"');
assert(/\$\('hd'\)\.textContent=cfg\.h1/.test(html) && /\$\('q'\)\.setAttribute\('placeholder',cfg\.ph\)/.test(html),
  'the mode block must apply the headline and placeholder');
assert(fs.existsSync(path.join(__dirname, '..', 'public', 'picker.html')),
  'the Google Picker page must exist at /picker');
assert(!fs.existsSync(path.join(__dirname, '..', 'public', 'sheets.html')),
  '/sheets is the landing rewrite, so no page may shadow it');
const fb = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'firebase.json'), 'utf8'));
for (const route of ['/sheets', '/excel', '/csv']) {
  assert(fb.hosting.rewrites.some(r => r.source === route && r.destination === '/index.html'),
    `${route} must rewrite to the home page`);
}
// The superseded singular landing keeps working instead of 404-ing.
assert(fb.hosting.redirects.some(r => r.source === '/sheet' && r.destination === '/sheets'),
  '/sheet must redirect to the canonical /sheets landing');

// --- picker regressions, both observed live on 2026-08-29 -------------------------------------
const picker = fs.readFileSync(path.join(__dirname, '..', 'public', 'picker.html'), 'utf8');
// (1) One request PER TAB spent the whole 60-reads/min/user Sheets quota on a single multi-tab
//     workbook and failed with a bare "HTTP 429". Reads go through batchGet now.
assert(picker.includes('/values:batchGet?'), 'tab values must be read with ONE batchGet per chunk');
assert(!/\/values\/" \+ encodeURIComponent\(range\)/.test(picker), 'no per-tab values GET may remain');
assert(/429/.test(picker), 'a rate-limit must be reported in words, not as a bare HTTP code');
// (2) A stale return path pointing at the picker itself sent a SUCCESSFUL import back into the
//     sign-in loop instead of home with the sheet attached. Assert the BEHAVIOUR, not the source:
//     lift the real returnPath() out of the page and run it against stubbed browser state, so a
//     refactor that keeps the guard passes and one that drops it fails.
const returnPathSrc = picker.match(/function returnPath\(\)\{[\s\S]*?\n\}/);
assert(returnPathSrc, 'returnPath must be defined in the picker page');
function runReturnPath(stored, pathname) {
  const ctx = {
    SS: { RETURN_TO: 'pr_return_to' },
    sessionStorage: { getItem: k => (k === 'pr_return_to' ? stored : null) },
    location: { pathname },
  };
  vm.createContext(ctx);
  return vm.runInContext(returnPathSrc[0] + '\nreturnPath();', ctx);
}
for (const [stored, pathname, want, why] of [
  ['/sheets', '/picker', '/sheets', 'returns to the landing that opened the picker'],
  ['/excel', '/picker', '/excel', 'any landing round-trips'],
  ['/picker', '/picker', '/', 'NEVER navigates the picker to itself (the sign-in loop)'],
  ['/PICKER', '/picker', '/', 'self-navigation check is case-insensitive'],
  ['https://evil.example', '/picker', '/', 'rejects an absolute off-site URL'],
  ['//evil.example', '/picker', '/', 'rejects a protocol-relative URL'],
  ['/a/b', '/picker', '/', 'rejects a multi-segment path'],
  [null, '/picker', '/', 'falls back home when nothing was stored'],
]) {
  assert.strictEqual(runReturnPath(stored, pathname), want, `returnPath(${stored}) — ${why}`);
}

// --- external LLM: the in-rail notice-and-choice UI was removed by owner decision on 2026-08-30.
// Disclosure now lives in the published privacy policy; the SERVER still refuses any request without
// external_llm_consent:true, so the client must assert it on every Anthropic-bound call.
const wb = fs.readFileSync(path.join(__dirname, '..', 'public', 'lib', 'workbook.js'), 'utf8');
for (const gone of ['llmNoticeHtml', 'ackLlmNotice', 'optOutLlm', 'LLM_CONSENT', 'llmnotice']) {
  assert(!wb.includes(gone), `the removed consent UI must leave no ${gone} behind`);
}
assert(!fs.readFileSync(path.join(__dirname, '..', 'public', 'styles.css'), 'utf8').includes('llmnotice'),
  'the notice stylesheet rule must be removed too');
assert.strictEqual((wb.match(/external_llm_consent:true/g) || []).length, 4,
  'all four Anthropic-bound calls (/chat, /api/converse, /api/reason, /api/master/generate) assert the flag');
// (the confirm() calls that remain guard destructive deletes, which is unrelated and correct)
assert(!/confirm\([^)]*(Anthropic|Claude|consent|local-only)/i.test(wb),
  'the external LLM must never be gated behind a blocking dialog');

console.log("home demo + landing routes + picker: 43 passed, 0 failed");
