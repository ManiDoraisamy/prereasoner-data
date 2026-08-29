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
// narrow the bare "+" button to that one source. The Sheets flow round-trips via /picker.
assert(/p==='sheets'\|\|p==='excel'\|\|p==='csv'/.test(html), 'the route mode must cover sheets|excel|csv');
assert(html.includes('<span class=pl>+</span></button>'), 'the add button is a bare "+" everywhere');
assert(html.includes("location.href='picker'"), 'the Google Sheets flow must round-trip via /picker');
assert(html.includes('>Attach a spreadsheet (or excel sheet or Google Sheets or CSV) and ask a question</div>'),
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
  assert(html.includes(`h1:'${noun}'`), `/${mode} must retitle the headline`);
  assert(html.includes(`ph:'${ph}'`), `/${mode} must reword the placeholder`);
}
assert(/lbl:'Add a Google Sheet'/.test(html) && /lbl:'Add an Excel file'/.test(html) && /lbl:'Add a CSV file'/.test(html),
  'each landing must keep an accessible label for the bare "+"');
assert(/\$\('hd'\)\.textContent=cfg\.h1/.test(html) && /\$\('q'\)\.setAttribute\('placeholder',cfg\.ph\)/.test(html),
  'the mode block must apply the headline and placeholder');
assert(!fs.existsSync(path.join(__dirname, '..', 'public', 'sheets.html')),
  '/sheets must be a landing rewrite, not the picker page');
const fb = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'firebase.json'), 'utf8'));
for (const route of ['/sheets', '/excel', '/csv']) {
  assert(fb.hosting.rewrites.some(r => r.source === route && r.destination === '/index.html'),
    `${route} must rewrite to the home page`);
}

console.log('home demo + landing routes: 24 passed, 0 failed');
