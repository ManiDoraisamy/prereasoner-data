// Runs the Google Sheets add-on's sidebar (sheets-addon/Sidebar.html) as Sheets runs it: on an Apps Script
// content origin, loading the web rail's shared component, the upload importer and the shared Firebase
// module from chat.prereasoner.com (served here from web/public), with google.script.run standing in for
// Code.js and a realtime database the caller drives (window.__rtdb). Used by the browser test
// (sheets-sidebar.spec.js) and the Marketplace artwork (docs/marketplace/render-review-assets.js).
const fs = require('node:fs');
const path = require('node:path');

const root = path.resolve(__dirname, '../../..');
const publicDir = path.join(root, 'web/public');
const SIDEBAR_URL = 'https://n-test-0lu-script.googleusercontent.com/userCodeAppPanel';
const conversation = 'c_0123456789abcdef0123456789abcdef';
const types = {'.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8'};

const fakeFirebase = {
  'firebase-app.js': 'export function initializeApp(){return {}}',
  'firebase-auth.js': `
    const auth = {currentUser: null, authStateReady: async () => {}};
    export function getAuth(){ return auth; }
    export class GoogleAuthProvider { addScope(){} static credential(idToken, accessToken){ return {idToken, accessToken}; } }
    export class OAuthProvider { setCustomParameters(){} }
    export async function signInWithCredential(target, credential){
      window.__signedInWith = credential.accessToken; target.currentUser = {uid: 'sheet-user'};
      return {user: {uid: 'sheet-user'}};
    }
    export async function signInWithRedirect(){} export async function getRedirectResult(){ return null; }
    export async function getIdToken(){ return 'id-token'; } export async function signInAnonymously(){}
    export async function signOut(){}`,
  'firebase-database.js': `
    const listeners = [];
    const stop = listener => () => { const index = listeners.indexOf(listener); if (index >= 0) listeners.splice(index, 1); };
    window.__rtdb = {
      value(path, value){ listeners.filter(l => l.kind === 'value' && l.path === path).forEach(l => l.cb({key: path.split('/').pop(), val: () => value})); },
      child(path, key, value){ listeners.filter(l => l.kind === 'child' && l.path === path).forEach(l => l.cb({key, val: () => value})); },
      paths(){ return listeners.map(l => l.kind + ':' + l.path); }
    };
    export function getDatabase(){ return {}; }
    export function ref(_db, path){ return {path}; }
    export function onValue(target, cb){ const l = {kind: 'value', path: target.path, cb}; listeners.push(l); return stop(l); }
    export function onChildAdded(target, cb){ const l = {kind: 'child', path: target.path, cb}; listeners.push(l); return stop(l); }
    export function off(){}`
};

const orders = [['country', 'amount'], ['France', 840], ['France', 400], ['Germany', 620]];
const views = [
  {name: 'france_orders', op: 'filter', label: "where country = 'France'", inputs: [conversation]},
  {name: 'france_total', op: 'group_agg', label: 'SUM(amount)', sql: 'SELECT SUM(amount) FROM france_orders',
    inputs: ['france_orders'], is_output: true}
];

// Opens the sidebar on `page` with the sheet `rows`; google.script.run calls are recorded in window.__calls
// and askPrereasoner waits in window.__server.pendingAsk for the caller to answer.
async function openSidebar(page, rows) {
  const sidebar = fs.readFileSync(path.join(root, 'sheets-addon/Sidebar.html'), 'utf8')
    .replace('<?!= reasonBase ?>', JSON.stringify('https://chat.prereasoner.com/reason/'));
  await page.route(SIDEBAR_URL, route => route.fulfill({contentType: 'text/html; charset=utf-8', body: sidebar}));
  await page.route('https://chat.prereasoner.com/**', route => {
    const file = path.join(publicDir, new URL(route.request().url()).pathname);
    if (!file.startsWith(publicDir) || !fs.existsSync(file)) return route.fulfill({status: 404, body: ''});
    return route.fulfill({contentType: types[path.extname(file)] || 'application/octet-stream',
      headers: {'access-control-allow-origin': '*'}, body: fs.readFileSync(file)});
  });
  await page.route('https://www.gstatic.com/firebasejs/**', route => route.fulfill({contentType: 'text/javascript',
    headers: {'access-control-allow-origin': '*'}, body: fakeFirebase[path.basename(new URL(route.request().url()).pathname)]}));
  await page.route('https://ssl.gstatic.com/**', route => route.fulfill({contentType: 'text/css', body: ''}));
  await page.addInitScript(initialRows => {
    window.__calls = [];
    window.__server = {rows: initialRows, pendingAsk: null};
    const grids = () => ({grids: [{name: 'Orders', rows: window.__server.rows,
      formats: window.__server.rows.map(row => row.map(() => 'General')),
      errors: window.__server.rows.map(row => row.map(() => false)), merges: [], date1904: false}]});
    const handlers = {
      getSidebarContext: (_, ok) => ok({token: 'google-token', spreadsheetId: 'sheet-1', name: 'Sales', workbook: grids()}),
      getWorkbookGrids: (_, ok) => ok(grids()),
      restorePrereasonerSheetConversation: (_, ok) => ok({conversationId: '', state: null, stale: false}),
      askPrereasoner: (arg, ok, fail) => { window.__server.pendingAsk = {arg, ok, fail}; },
      savePrereasonerSheetConversation: (arg, ok) => ok({saved: arg.conversationId}),
      clearPrereasonerSheetConversation: (_, ok) => ok({cleared: 'sheet-1'}),
      syncPrereasonerConversation: (_, ok) => ok({changed: true})
    };
    window.google = {script: {run: {withSuccessHandler(ok) {
      return {withFailureHandler(fail) {
        return new Proxy({}, {get: (_, name) => arg => {
          window.__calls.push({name, arg: arg === undefined ? null : JSON.parse(JSON.stringify(arg))});
          setTimeout(() => handlers[name](arg, ok, fail), 10);
        }});
      }};
    }}}};
  }, rows);
  await page.goto(SIDEBAR_URL);
}

// Answers the waiting askPrereasoner with a finished turn over `views`.
async function answer(page, reply, answerViews) {
  await page.evaluate(({conversation, reply, views}) => window.__server.pendingAsk.ok({
    reply, conversationId: conversation,
    history: [{role: 'user', content: window.__server.pendingAsk.arg.question}, {role: 'assistant', content: reply}],
    traces: [{jobId: 'job-1', question: 'total amount in France', execution: {actual: 'python', verified: false},
      analysis: {analysis_id: 'a_11111111111111111111111111111111', revision: 1, display_name: 'France sales'}, views}]
  }), {conversation, reply, views: answerViews});
}

module.exports = {SIDEBAR_URL, conversation, orders, views, openSidebar, answer};
