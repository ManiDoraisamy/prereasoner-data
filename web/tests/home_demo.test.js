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
const denorm = parse(csvText('customer-orders/orders.csv'));
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
assert(/'customer-orders':\['orders'\]/.test(html),
  'customer-orders must load only its denormalized orders sheet');
assert(/'orders-tiers':\['orders','tier'\]/.test(html),
  'orders-tiers must load its orders and tier schedule together');
assert(/'payment-commissions':\['payments','commission_rates'\]/.test(html),
  'the independent commission calculation dataset must be registered');
assert(/'customers-orders':\['customers','orders'\]/.test(html), 'customers-orders must load both CSVs');
assert(/\?p:'customer-orders'/.test(html), 'the default dataset must be the denormalized customer-orders');
assert(html.includes("fetch('/dataset/'+DATASET+'/'+(xlsx?n:n+'.csv'))"), 'the demo must load from /dataset/<name>/');
assert(html.includes("XLSX_READER.readWorkbook(b)"),
  'a .xlsx registry entry must convert through the same bounded worker the upload path uses');
assert(/'neartail-orders-xlsx':\['orders\.xlsx'\]/.test(html), 'the workbook-format dataset must be registered');
assert(html.includes('await DEMO_LOAD'), 'submit must wait for the demo fetch so a fast Ask cannot race it');
// Every dataset directory ships a prompt.txt, is registered on the page with its exact CSV list,
// and the page only prefills a PRISTINE question box (a typed or restored question always wins).
const registered = {};
for (const m of html.matchAll(/'([a-z-]+)':\[([^\]]*)\]/g)) {
  registered[m[1]] = m[2].split(',').map(s => s.replace(/'/g, '').trim()).filter(Boolean);
}
for (const dir of fs.readdirSync(dsDir).filter(d => fs.statSync(path.join(dsDir, d)).isDirectory())) {
  const files = fs.readdirSync(path.join(dsDir, dir));
  const prompt = files.includes('prompt.txt') && csvText(path.join(dir, 'prompt.txt')).trim();
  assert(prompt, `${dir} must ship a non-empty prompt.txt`);
  // A registry entry is the bare name for a CSV and keeps its .xlsx suffix for a workbook,
  // matching how the loader picks the fetch/parse path.
  const tables = files.filter(f => f.endsWith('.csv')).map(f => f.replace(/\.csv$/, ''))
    .concat(files.filter(f => f.endsWith('.xlsx'))).sort();
  assert.deepStrictEqual((registered[dir] || []).slice().sort(), tables,
    `${dir} must be registered on the page with exactly its data files`);
}
assert(html.includes("fetch('/dataset/'+DATASET+'/prompt.txt')"), 'the page must fetch the dataset prompt');
assert(html.includes("$('q').value===Q0"), 'the prompt must only replace the pristine default question');
assert(html.includes(".get('load')"), 'the dataset selector must be the ?load= query parameter');
// "More examples": the badge under the prompt opens a picker built from dataset.txt + each
// prompt.txt, and picking one navigates to the same ?load= URL the shareable index uses.
assert(html.includes('id=morex') && html.includes('>More examples<svg'),
  'the More examples link must sit under the prompt card and carry its chevron');
assert(/\.morex\{[^}]*border:/.test(html) === false && /\.morex\{[^}]*font-size:14px/.test(html),
  'it is a plain 14px text link (the formfacade "More >" affordance), not a bordered pill');
assert(html.includes('Object.keys(DATASETS)'),
  'the picker must list exactly the datasets ?load= can resolve, so no row silently loads the default');
assert(html.includes("'/dataset/'+d+'/prompt.txt'"), 'the picker must show each dataset prompt');
// A row names the sheets it attaches — the chips the user gets — not the directory path, which is an
// internal detail they never see anywhere else. The names must be printed VERBATIM from the same
// DATASETS values the chips are rendered from, so the dialog and the chips read identically; no
// ".csv" decoration, because a chip is a sheet name (an .xlsx upload has one chip per worksheet).
assert(/files:DATASETS\[d\]\.map\(baseName\)\.join\(', '\)/.test(html),
  'picker rows must name sheets exactly as the chips do, with no extension appended');
assert(html.includes('<div class=xd>\'+esc(e.files)+\'</div>'), 'the row subtitle must render the sheet names');
assert(!html.includes('<div class=xd>\'+esc(e.dir)+\'</div>'), 'the row subtitle must not be the directory name');
assert(/name:base,/.test(html) && html.includes("base=xlsx?n.slice(0,-5):n"),
  'chips must show the table name with no extension, for CSV and workbook entries alike');
assert(html.includes("executionQuery('?load='+encodeURIComponent(b.dataset.load))"),
  'picking an example must navigate to its ?load= URL');
assert(html.includes("location.href='reason'+executionQuery()"),
  'submitting a dataset must carry the URL-selected execution mode to /reason');
const shared = fs.readFileSync(path.join(__dirname, '..', 'public', 'lib', 'shared.js'), 'utf8');
assert(shared.includes("raw==='sql'") && shared.includes("raw==='py'") && shared.includes("raw==='both'"),
  'the browser URL contract must accept sql, py, and both');
assert(shared.includes('function executionRequestFields(override)'),
  'the browser must expose the selected execution mode to request builders');
const modeSource = shared.slice(shared.indexOf('const EXECUTION_USE'), shared.indexOf('// sessionStorage keys'));
for (const [raw, expected] of [['sql','sql'], ['py','py'], ['python','py'], ['both','both'], ['verify','both'], ['auto','auto'], ['typo','typo'], ['',null]]) {
  const context = vm.createContext({location: {search: '?use='+raw}, URLSearchParams});
  const actual = JSON.parse(vm.runInContext(modeSource + `\nJSON.stringify({fields:executionRequestFields(), query:executionQuery('?load=orders-tiers')})`, context));
  assert.deepStrictEqual(actual.fields, expected ? {use:expected} : {});
  // A one-off override (the workbook's "run both and compare") wins for a single request and
  // must NOT be written back, or the conversation would stay pinned to that backend.
  const overridden = JSON.parse(vm.runInContext(
    `JSON.stringify({fields:executionRequestFields('both'), after:executionRequestFields()})`, context));
  assert.deepStrictEqual(overridden.fields, {use:'both'});
  assert.deepStrictEqual(overridden.after, expected ? {use:expected} : {});
  const params = new URLSearchParams(actual.query);
  assert.strictEqual(params.get('load'), 'orders-tiers');
  assert.strictEqual(params.get('use'), expected);
}
const conversations = fs.readFileSync(path.join(__dirname, '..', 'public', 'lib', 'workbook-conversations.js'), 'utf8');
assert(conversations.includes("'/reason/'+cid+executionQuery()"),
  'conversation URLs must preserve the selected execution mode');
const workbook = fs.readFileSync(path.join(__dirname, '..', 'public', 'lib', 'workbook.js'), 'utf8');
assert((workbook.match(/executionRequestFields\(ONESHOT_USE\)/g) || []).length >= 3,
  'direct and orchestrated retries must carry the selected execution mode');
assert(/ONESHOT_USE=null;\s*\/\/ an ordinary question/.test(workbook),
  'an ordinary question must clear the one-off backend override');
const css = fs.readFileSync(path.join(__dirname, '..', 'public', 'styles.css'), 'utf8');
assert(/min-width:1000px[^}]*\{[^}]*\.hero-left \.h1\{white-space:nowrap/.test(css.replace(/\s+/g, '')) ||
  css.includes('.hero-left .h1{white-space:nowrap;max-width:none}'),
  'the hero headline must render as one line on desktop widths');
// dataset.txt is the shareable index: one line per dataset directory, in exactly the ?load= form
// the page implements. Regenerating it here keeps the list complete when a dataset is added.
const dirs = fs.readdirSync(dsDir).filter(d => fs.statSync(path.join(dsDir, d)).isDirectory()).sort();
assert.strictEqual(
  csvText('dataset.txt'),
  dirs.map(d => `${d}: https://chat.prereasoner.com/?load=${d}`).join('\n') + '\n',
  'dataset.txt must list every dataset directory as <dir>: https://chat.prereasoner.com/?load=<dir>');
// FX comes from the knowledgebase (ECB daily sync -> knowledgebase."exchange_rate"), never from a
// demo sheet: an illustrative rate table on the page would SHADOW the real rates, because an
// uploaded rate sheet deliberately wins over the knowledge join (own data first).
assert(!/const FX=/.test(html), 'no illustrative rate sheet may ship on the page');
assert(!fs.readdirSync(dsDir, { recursive: true }).some(f =>
  /(^|[\\/])(?:fx|exchange[_ -]?rates?)\.csv$/i.test(String(f))),
  'no dataset directory may contain an FX sheet that shadows the ECB source');
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
assert(html.includes('>Attach a spreadsheet and ask a question</h1>'),
  'the generic headline invites the attach-and-ask action');
assert(!/<div class=sub>/.test(html), 'the hero carries no subline under the headline');
// The brand mark is the marketing site's front door (prereasoner.com), not the app root: this IS
// the app, so "/" is already where the visitor stands.
assert(html.includes('<a class=brand href="https://prereasoner.com/">'),
  'the header brand must link to the marketing site');
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
    location: { pathname, origin: 'https://chat.prereasoner.com' },
    URL,
  };
  vm.createContext(ctx);
  return vm.runInContext(returnPathSrc[0] + '\nreturnPath();', ctx);
}
for (const [stored, pathname, want, why] of [
  ['/sheets', '/picker', '/sheets', 'returns to the landing that opened the picker'],
  ['/excel', '/picker', '/excel', 'any landing round-trips'],
  ['/sheets?use=both', '/picker', '/sheets?use=both', 'preserves the execution mode'],
  ['/picker?use=both', '/picker', '/', 'query params cannot bypass the self-navigation check'],
  ['/picker', '/picker', '/', 'NEVER navigates the picker to itself (the sign-in loop)'],
  ['/PICKER', '/picker', '/', 'self-navigation check is case-insensitive'],
  ['https://evil.example', '/picker', '/', 'rejects an absolute off-site URL'],
  ['//evil.example', '/picker', '/', 'rejects a protocol-relative URL'],
  ['/a/b', '/picker', '/', 'rejects a multi-segment path'],
  [null, '/picker', '/', 'falls back home when nothing was stored'],
]) {
  assert.strictEqual(runReturnPath(stored, pathname), want, `returnPath(${stored}) — ${why}`);
}

// --- provenance tags: render the server contract; never classify by column name -----------
{
  const wbSrc = fs.readFileSync(path.join(__dirname, '..', 'public', 'lib', 'workbook.js'), 'utf8');
  assert(wbSrc.includes('m.columnProvenance'), 'headers must read server-authored column provenance');
  assert(wbSrc.includes('column_provenance'), 'HTTP/trace views must preserve the wire field');
  assert(!wbSrc.includes('KB_COLS') && !wbSrc.includes('inputCols.has'),
    'the client must not infer provenance from a global column-name list');
  for (const tag of ["return 'SRC'", "return 'CALC'", "return 'WIKI'", "return 'ECB'"]) {
    assert(wbSrc.includes(tag), `provenance must render the ${tag} source label`);
  }
  assert(!/connected to wikipedia|world meaning/.test(wbSrc), 'no legacy wikipedia/world-meaning strings in the client');
  const cssSrc = fs.readFileSync(path.join(__dirname, '..', 'public', 'styles.css'), 'utf8');
  assert(cssSrc.includes('.provtag.kb') && cssSrc.includes('.provtag.mixed'), 'reference and mixed tags must be styled');
}

// Excel parsing is fixed, bounded, and isolated from the application thread.
{
  const reader = fs.readFileSync(path.join(__dirname, '..', 'public', 'lib', 'xlsx-reader.js'), 'utf8');
  const worker = fs.readFileSync(path.join(__dirname, '..', 'public', 'lib', 'xlsx-worker.js'), 'utf8');
  assert(!html.includes('xlsx/0.18.5') && !html.includes('XLSX.read('), 'the landing must not execute the vulnerable parser inline');
  assert(worker.includes("xlsx-0.20.3.full.min.js"), 'the worker must pin the patched vendored SheetJS build');
  assert(reader.includes('MAX_XLSX_BYTES') && reader.includes('WORKER_TIMEOUT_MS'), 'compressed input and parse time must be bounded');
  assert(worker.includes('MAX_ROWS') && worker.includes('MAX_COLUMNS') && worker.includes('MAX_OUTPUT_CHARS'),
    'expanded workbook dimensions must be bounded');
}

assert(html.includes('id=loginbtn') && html.includes('ensureSignedIn'), 'Login must invoke the real Firebase auth flow');
assert(!html.includes('loginSoon'), 'the dead coming-soon login handler must be removed');

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
// Its brand mark points at the marketing site like every other; the separate "Back to the app"
// link is what returns here (the two used to be the same destination).
assert(privacy.includes('<a class="brand" href="https://prereasoner.com/">'),
  'the privacy page brand must link to the marketing site');
assert(privacy.includes('<a class="back" href="/">'),
  'the privacy page must keep a distinct link back to the app');

console.log("home demo + landing routes + picker + privacy: passed");
