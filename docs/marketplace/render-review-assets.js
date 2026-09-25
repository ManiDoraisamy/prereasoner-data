// Marketplace artwork rendered from the shipped add-on, with example data. The Google Sheets sidebar
// frames the web workbook (/embed/sheets), so each scene loads that page inside a stand-in host with a
// local stand-in for the Prereasoner API. PNGs are the Marketplace uploads; each SVG wraps its PNG.
const fs = require('fs');
const http = require('http');
const path = require('path');
const { pathToFileURL } = require('url');
const { chromium } = require('@playwright/test');
const root = path.resolve(__dirname, '../..');
const publicDir = path.join(root, 'web/public');
const out = __dirname;
const escape = value => String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;');
const logoPath = 'C:/work/FormFacade/public/logo-full.png';
const logo = fs.readFileSync(logoPath).toString('base64');
const conversationId = 'c_0123456789abcdef0123456789abcdef';
const question = 'What are total sales in France?';
const sidebar = { width: 300, height: 420 };   // Sheets sidebars are 300 px wide
const cells = [['Country', 'Amount (USD)'], ['France', 840], ['France', 400], ['Germany', 620], ['Spain', 260]];
const grids = [{ name: 'Orders', rows: cells, formats: cells.map(row => row.map(() => 'General')),
  errors: cells.map(row => row.map(() => false)), merges: [], date1904: false }];
const views = [
  { name: 'france_orders', logical_name: 'france_orders', op: 'filter', label: 'France orders',
    columns: ['Country', 'Amount (USD)'], rows: [['France', 840], ['France', 400]],
    sql: "SELECT * FROM orders WHERE country = 'France'", python: "france_orders = orders.filter(country='France')" },
  { name: 'total_sales', logical_name: 'total_sales', op: 'group_agg', label: 'Total sales', columns: ['total_usd'],
    rows: [[1240]], sql: 'SELECT SUM(amount_usd) AS total_usd FROM france_orders',
    python: "total_sales = france_orders.reduce(SUM('amount_usd'))", inputs: ['france_orders'], is_output: true }
];
const answer = { reply: 'Total sales in France are **US$1,240**.', conversation_id: conversationId, history: [],
  traces: [{ jobId: 'artwork', question, engine: { status: 'answered', model: 'artwork', views, sql: views[1].sql,
    answer: { columns: ['total_usd'], rows: [[1240]] }, conversation_id: conversationId, trace: { jobId: 'artwork' },
    execution: { requested: 'default', actual: 'python', verified: false, implementation: 'shared_plan', fallback_reason: null },
    analysis: { analysis_id: 'a_11111111111111111111111111111111', slug: 'france_sales', revision: 1, action: 'create',
      stale: false, display_name: 'France sales' } } }] };
const conversations = [
  { id: conversationId, question, ts: '2026-09-24T10:15:00Z' },
  { id: 'c_11111111111111111111111111111111', question: 'Which product sold the most?', ts: '2026-09-23T15:20:00Z' },
  { id: 'c_22222222222222222222222222222222', question: 'Compare sales by country', ts: '2026-09-22T09:00:00Z' }
];
// Stands in for sheets-addon/Sidebar.html and its Apps Script server (Code.js).
const host = `<!doctype html><html><body style="margin:0;background:#fff">
<iframe id=prereasoner style="display:block;width:${sidebar.width}px;height:${sidebar.height}px;border:0"></iframe>
<script>
const frame=document.getElementById('prereasoner');
frame.src='/embed/sheets'+location.search;
const grids=${JSON.stringify(grids)};
addEventListener('message',event=>{
  if(event.source!==frame.contentWindow)return;
  const m=event.data||{};if(!m.prereasoner)return;
  const payload=m.type==='context'?{token:'artwork',spreadsheetId:'artwork-sheet',name:'Sales',workbook:{grids}}:{grids};
  frame.contentWindow.postMessage({prereasoner:1,id:m.id,type:m.type,payload},location.origin);
});
</script></body></html>`;
const firebase = {
  'firebase-app.js': 'export function initializeApp(){return {}}',
  'firebase-auth.js': `const user={uid:'artwork',displayName:'Sample User',email:'sample@example.com'};
    export function getAuth(){return {get currentUser(){return window.__uid?user:null},authStateReady:async()=>{}}}
    export class GoogleAuthProvider { addScope(){} static credential(idToken,accessToken){return {idToken,accessToken}} }
    export class OAuthProvider { setCustomParameters(){} }
    export async function signInWithCredential(){window.__uid='artwork';return {user}}
    export async function signInWithRedirect(){} export async function signInAnonymously(){return {user}}
    export async function getRedirectResult(){return null} export async function getIdToken(){return 'artwork'}
    export async function signOut(){}`,
  'firebase-database.js': `export function getDatabase(){return {}} export function ref(_db,path){return {path}}
    export function onValue(){return ()=>{}} export function onChildAdded(){return ()=>{}} export function off(){}`
};

function serve() {
  const types = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
    '.css': 'text/css; charset=utf-8', '.svg': 'image/svg+xml', '.png': 'image/png', '.json': 'application/json' };
  const json = (res, body, status = 200) => { res.writeHead(status, { 'content-type': 'application/json' }); res.end(JSON.stringify(body)); };
  const server = http.createServer((req, res) => {
    const url = new URL(req.url, 'http://127.0.0.1');
    if (url.pathname === '/chat') return json(res, answer);
    if (url.pathname === '/api/spreadsheet/conversation/restore') return json(res, { conversation_id: null, state: {} });
    if (url.pathname.startsWith('/api/spreadsheet/conversation/')) return json(res, { saved: true });
    if (url.pathname === '/api/conversation/state') return json(res, { saved: conversationId });
    if (url.pathname === '/api/conversations') return json(res, { conversations });
    if (url.pathname.startsWith('/api/')) return json(res, {}, url.pathname === '/api/reason' ? 200 : 404);
    if (url.pathname === '/host') { res.writeHead(200, { 'content-type': 'text/html; charset=utf-8' }); return res.end(host); }
    const relative = url.pathname === '/embed/sheets' ? 'reason.html' : decodeURIComponent(url.pathname).replace(/^\/+/, '');
    const file = path.resolve(publicDir, relative);
    if (!file.startsWith(publicDir + path.sep) || !fs.existsSync(file) || !fs.statSync(file).isFile()) {
      res.writeHead(404); return res.end();
    }
    res.writeHead(200, { 'content-type': types[path.extname(file)] || 'application/octet-stream' });
    fs.createReadStream(file).pipe(res);
  });
  return new Promise(resolve => server.listen(0, '127.0.0.1', () => resolve(server)));
}

// One scene in a fresh browser session: the frame builds its session exactly as in Sheets.
async function capture(browser, base, scene) {
  const context = await browser.newContext({ viewport: { width: sidebar.width, height: sidebar.height }, deviceScaleFactor: 3 });
  try {
    await context.route('https://www.gstatic.com/firebasejs/**', route =>
      route.fulfill({ contentType: 'text/javascript', body: firebase[path.basename(new URL(route.request().url()).pathname)] }));
    const page = await context.newPage();
    await page.goto(base + '/host' + (scene === 'previous' ? '?view=conversations' : ''));
    const frame = page.frameLocator('#prereasoner');
    if (scene === 'previous') await frame.locator('.convitem').first().waitFor();
    else {
      await frame.locator('.embedempty').waitFor();
      await frame.locator('#chatq').fill(question);
    }
    if (scene === 'answer') {
      await frame.locator('#chatq').press('Enter');
      await frame.locator('.turn-answer').last().waitFor();
      await frame.locator('.turn-reasoning summary').last().click();
      await frame.locator('#chatq').fill('Which product sold the most?');
    }
    await page.mouse.move(0, 0);
    await page.waitForTimeout(500);   // transitions settle
    return (await page.locator('#prereasoner').screenshot()).toString('base64');
  } finally { await context.close(); }
}

function text(x, y, value, size = 24, weight = 400, fill = '#202124') {
  return `<text x="${x}" y="${y}" font-family="Arial, sans-serif" font-size="${size}" font-weight="${weight}" fill="${fill}">${escape(value)}</text>`;
}
function sourceTable() {
  const rows = cells.slice(1).map(([country, amount]) => [country, '$' + amount]);
  return text(56, 373, 'EXAMPLE SOURCE DATA', 15, 700, '#70757a') +
    '<rect x="56" y="393" width="538" height="255" rx="10" fill="#fff" stroke="#dadce0"/>' +
    text(80, 428, 'Country', 21, 700) + text(355, 428, 'Amount (USD)', 21, 700) +
    rows.map((row, i) => '<path d="M56 ' + (444 + i * 50) + 'H594" stroke="#e8eaed"/>' +
      text(80, 478 + i * 50, row[0], 22) + text(448, 478 + i * 50, row[1], 22)).join('');
}
function artwork(scene, png) {
  const names = { ask: ['Ask a question.', 'Use your own words.'], answer: ['See the answer.', 'Inspect each calculation.'], previous: ['Reopen a conversation.', 'Continue your analysis.'] };
  const lines = names[scene];
  const scale = 1.5;
  const width = sidebar.width * scale;
  const height = sidebar.height * scale;
  const left = scene === 'previous'
    ? text(56, 362, 'Extensions → Prereasoner', 26, 600) + text(56, 409, '→ Previous conversations', 26, 600) +
      text(56, 479, 'Select a saved conversation to open', 23, 400, '#5f6368') +
      text(56, 514, 'its full analysis in Prereasoner.', 23, 400, '#5f6368')
    : sourceTable();
  return `<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="800" viewBox="0 0 1280 800" role="img">
  <title>Prereasoner — ${escape(lines.join(' '))}</title>
  <desc>The actual add-on interface rendered with illustrative sample data and enlarged for readability.</desc>
  <rect width="1280" height="800" fill="#f8f9fa"/>
  <image href="data:image/png;base64,${logo}" x="52" y="38" width="50" height="50"/>
  ${text(118, 71, 'Prereasoner', 29, 700)}
  ${text(56, 187, lines[0], 39, 700)}${text(56, 241, lines[1], 39, 700)}
  ${text(56, 293, 'For use with Google Sheets™', 23, 400, '#5f6368')}
  ${left}
  <rect x="674" y="78" width="${width + 2}" height="${height + 60}" rx="12" fill="#fff" stroke="#dadce0"/>
  ${text(696, 115, 'Prereasoner', 24, 600)}
  <path d="M674 134H${674 + width + 2}" stroke="#dadce0"/>
  <image href="data:image/png;base64,${png}" x="675" y="136" width="${width}" height="${height}"/>
  ${text(56, 702, scene === 'answer' ? 'Source cells stay unchanged.' : scene === 'ask' ? 'Extensions → Prereasoner → Ask a question' : 'Saved to your Prereasoner account.', 20, 400, '#5f6368')}
  ${text(56, 756, 'Google Sheets™ is a trademark of Google LLC.', 14, 400, '#70757a')}
  ${text(700, 778, 'Example data · Actual add-on interface, enlarged', 13, 400, '#70757a')}
  </svg>`;
}

(async () => {
  const server = await serve();
  const base = 'http://127.0.0.1:' + server.address().port;
  const browser = await chromium.launch({ headless: true });
  try {
    for (const [index, scene] of ['ask', 'answer', 'previous'].entries()) {
      const png = await capture(browser, base, scene);
      const filename = `review-${index + 1}-${scene}`;
      fs.writeFileSync(path.join(out, filename + '.svg'), artwork(scene, png));
      const rendered = await browser.newPage({ viewport: { width: 1280, height: 800 }, deviceScaleFactor: 1 });
      await rendered.goto(pathToFileURL(path.join(out, filename + '.svg')).href);
      await rendered.screenshot({ path: path.join(out, filename + '-1280x800.png') });
      await rendered.close();
      console.log(filename);
    }
    const banner = await browser.newPage({ viewport: { width: 220, height: 140 }, deviceScaleFactor: 1 });
    await banner.goto(pathToFileURL(path.join(out, 'card-banner.svg')).href);
    await banner.screenshot({ path: path.join(out, 'card-banner-220x140.png') });
  } finally { await browser.close(); server.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
