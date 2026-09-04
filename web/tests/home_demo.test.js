// Dependency-free checks for the default home-page conversion demo.
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const html = fs.readFileSync(path.join(__dirname, '..', 'public', 'index.html'), 'utf8');

// The demo workbooks live as plain CSVs under public/dataset/<name>/, fetched by ?dataset=.
const dsDir = path.join(__dirname, '..', 'public', 'dataset');
const csvText = rel => fs.readFileSync(path.join(dsDir, rel), 'utf8');
function parseLine(line) {                     // quoted-field aware ("Magnifying Glass, Brass")
  const out = []; let cur = '', inQ = false;
  for (let i = 0; i < line.length; i++) {
    const ch = line[i];
    if (inQ) { if (ch === '"') { if (line[i + 1] === '"') { cur += '"'; i++; } else inQ = false; } else cur += ch; }
    else if (ch === '"') inQ = true;
    else if (ch === ',') { out.push(cur); cur = ''; }
    else cur += ch;
  }
  out.push(cur); return out;
}
const parse = text => text.trim().split('\n').map(parseLine);

const orderLines = csvText('customers-orders/orders.csv').trim().split('\n');
assert.strictEqual(
  orderLines[0],
  'order ID,customer,ordered,currency,amount',
  'currency must appear immediately before amount',
);
assert.strictEqual(orderLines.length, 24, 'the demo must retain all 23 orders');
assert(orderLines.slice(1).every(line => /,(USD|EUR|GBP|INR),[0-9.]+$/.test(line)),
  'every demo order must have a supported currency code before amount');
const customers = parse(csvText('customers-orders/customers.csv'));
assert.deepStrictEqual(customers[0], ['customer ID', 'name', 'city', 'tier']);
assert.strictEqual(customers.length, 10, 'the demo must retain all 9 customers');

// customer-orders.csv is the SAME orders denormalized with each customer's city and tier. Assert the
// join, not just the shape: a hand-edited drift between the two datasets would silently change what
// the two demo variants answer for the same question.
const denorm = parse(csvText('customer-orders/customer-orders.csv'));
assert.deepStrictEqual(denorm[0], ['order ID', 'customer', 'city', 'tier', 'ordered', 'currency', 'amount']);
const byName = new Map(customers.slice(1).map(c => [c[1], c]));
const expected = parse(csvText('customers-orders/orders.csv')).slice(1).map(o => {
  const c = byName.get(o[1]);
  assert(c, `order for unknown customer ${o[1]}`);
  return [o[0], o[1], c[2], c[3], o[2], o[3], o[4]];
});
assert.deepStrictEqual(denorm.slice(1), expected,
  'customer-orders.csv must equal customers ⋈ orders exactly');

// The page selects a dataset by ?dataset= with the denormalized single sheet as the default.
assert(/'customer-orders':\['customer-orders'\]/.test(html), 'customer-orders must be a registered dataset');
assert(/'customers-orders':\['customers','orders'\]/.test(html), 'customers-orders must load both CSVs');
assert(/\?p:'customer-orders'/.test(html), 'the default dataset must be the denormalized customer-orders');
assert(html.includes("fetch('/dataset/'+DATASET+'/'+n+'.csv')"), 'the demo must load from /dataset/<name>/');
assert(html.includes('await DEMO_LOAD'), 'submit must wait for the demo fetch so a fast Ask cannot race it');
// FX comes from the knowledgebase (ECB daily sync -> knowledgebase."exchange_rate"), never from a
// demo sheet: an illustrative rate table on the page would SHADOW the real rates, because an
// uploaded rate sheet deliberately wins over the knowledge join (own data first).
assert(!/const FX=/.test(html), 'no illustrative rate sheet may ship on the page');
assert(!fs.readdirSync(dsDir, { recursive: true }).some(f => /fx|rate/i.test(String(f))),
  'no dataset directory may contain a rate sheet');
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
assert(html.includes('>Get the result with the query and rows behind it.</div>'),
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

// --- external LLM: disclosure has one permanent home and never interrupts the user's workflow.
const wb = fs.readFileSync(path.join(__dirname, '..', 'public', 'lib', 'workbook.js'), 'utf8');
const chat = fs.readFileSync(path.join(__dirname, '..', 'public', 'chatui.html'), 'utf8');
const privacy = fs.readFileSync(path.join(__dirname, '..', 'public', 'privacy.html'), 'utf8');
assert.strictEqual(fb.hosting.cleanUrls, true, 'privacy.html must be published at the linked /privacy URL');
for (const gone of ['llmNoticeHtml', 'ackLlmNotice', 'optOutLlm', 'LLM_CONSENT', 'llmnotice']) {
  assert(!wb.includes(gone), `the removed consent UI must leave no ${gone} behind`);
}
for (const gone of ['llmNoticeOnce', 'llmOptedOut', 'pr_llm_consent', 'Heads up: this assistant']) {
  assert(!chat.includes(gone), `the standalone chat must leave no ${gone} notice path behind`);
}
assert(!fs.readFileSync(path.join(__dirname, '..', 'public', 'styles.css'), 'utf8').includes('llmnotice'),
  'the notice stylesheet rule must be removed too');
assert(!wb.includes('external_llm_consent') && !chat.includes('external_llm_consent'),
  'ordinary processing must not carry a misleading per-request consent field');
// (the confirm() calls that remain guard destructive deletes, which is unrelated and correct)
assert(!/confirm\([^)]*(Anthropic|Claude|consent|local-only)/i.test(wb),
  'the external LLM must never be gated behind a blocking dialog');
assert(!/confirm\([^)]*(Anthropic|Claude|consent|local-only)/i.test(chat),
  'the standalone chat must never show a processor dialog');
assert(html.includes('href="/privacy"'), 'the home page must link to the published privacy policy');
for (const page of ['reason.html', 'knowledge.html', 'picker.html', 'chatui.html']) {
  const body = fs.readFileSync(path.join(__dirname, '..', 'public', page), 'utf8');
  assert(body.includes('href="/privacy"'), `${page} must link to the published privacy policy`);
}
for (const required of ['What we process', 'Anthropic', 'Google Cloud and Firebase', 'Why we process data',
  'Storage and retention', 'Deletion', 'Customer spreadsheet rows are not included']) {
  assert(privacy.includes(required), `the published privacy policy must disclose: ${required}`);
}
assert(privacy.includes('toward operating this assistant layer locally'),
  'the policy must state the local-model direction without making it a user setting');

console.log("home demo + landing routes + picker + privacy: passed");
