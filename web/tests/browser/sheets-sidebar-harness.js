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
    export async function getIdToken(){ return window.__server.workbookOptions.hostToken === false ? null : 'id-token'; } export async function signInAnonymously(){}
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
// and askPrereasoner waits in window.__server.pendingAsk for the caller to answer. `failing` maps a server
// function to the message Apps Script rejects it with.
async function openSidebar(page, rows, failing = {}, workbookOptions = {}) {
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
  await page.addInitScript(({initialRows, failing, workbookOptions}) => {
    window.__calls = [];
    window.__server = {rows: initialRows, pendingAsk: null, pendingSchemas: [], holdSchemas: false, workbookOptions};
    // The sidebar's upload goes straight to the API with its sign-in (`hostToken: false`: a sign-in that gave none).
    const hostFetch = window.fetch;
    window.fetch = async (url, init) => {
      if (!String(url).endsWith('/api/conversation/sync')) return hostFetch(url, init);
      const body = JSON.parse(init.body);
      window.__calls.push({name: 'syncPrereasonerConversation', authorization: init.headers.Authorization,
        arg: {conversationId: body.id || null, question: body.question, tables: body.tables}});
      const once = window.__server.workbookOptions.failOnce || {};
      const message = once.syncPrereasonerConversation || failing.syncPrereasonerConversation;
      delete once.syncPrereasonerConversation;
      if (window.__server.holdUpload) await new Promise(resolve => { window.__server.releaseUpload = resolve; });
      return new Response(JSON.stringify(message ? {error: message} : {changed: true,
        conversation_id: body.id || 'c_0123456789abcdef0123456789abcdef', source_hash: 'a'.repeat(64)}),
      {status: message ? 500 : 200, headers: {'content-type': 'application/json'}});
    };
    // What Code.js:readTab_ returns: the upload-once script sends each column's format once and the cells
    // holding an error; a script published before it (`uploadOnce: false`) sends both for every cell. A tab named in
    // `tabLeftOut` is past the limits: {name, leftOut: why}. `grids` gives whole tabs, and `tabRows` other tabs' rows.
    const tab = name => {
      const options = window.__server.workbookOptions;
      const given = (options.grids || []).find(grid => grid.name === name);
      if (given) return given;
      const leftOut = (options.tabLeftOut || {})[name];
      if (leftOut) return {name, leftOut};
      const rows = (options.tabRows || {})[name] || window.__server.rows;
      const cells = options.uploadOnce === false
        ? {formats: rows.map(row => row.map(() => 'General')), errors: rows.map(row => row.map(() => false))}
        : {columnFormats: (rows[0] || []).map(() => ({f: 'General'})), errorCells: []};
      return Object.assign({name, rows, merges: [], date1904: false}, cells);
    };
    // What Code.js:workbookPlan_ returns: the tabs one read holds, and why it leaves out any other.
    const plan = () => {
      const options = window.__server.workbookOptions;
      return {tabs: options.tabs || ['Orders'], scope: options.scope || 'all',
        availableTabs: options.availableTabs || options.tabs || ['Orders'], leftOut: options.leftOut || []};
    };
    const handlers = {
      // The script that uploads a sheet once says so; `uploadOnce: false` is a script published before it.
      getSidebarContext: (_, ok) => ok(Object.assign({token: 'google-token', spreadsheetId: 'sheet-1', name: 'Sales',
        uploadOnce: window.__server.workbookOptions.uploadOnce !== false}, plan())),
      getWorkbookPlan: (_, ok) => ok(plan()),
      getWorkbookTab: (arg, ok) => ok(tab(arg.name)),
      getWorkbookSchema: (arg, ok) => {
        if(window.__server.holdSchemas) window.__server.pendingSchemas.push({arg,ok});
        else ok(workbookOptions.schema || {sheets:[{name:'Orders',columns:window.__server.rows[0]}],active_sheet:'Orders',scope:['Orders']});
      },
      getPrereasonerSuggestions: (schema, ok) => ok({questions:['How many rows are in \"Orders\"?','Count rows in \"Orders\" by \"country\".','What is the total \"amount\" in \"Orders\"?'],source:'gemini',schema_only:true}),
      // A script published before upload once refuses a restore without the cells (its requestTables_).
      restorePrereasonerSheetConversation: (arg, ok, fail) => window.__server.workbookOptions.uploadOnce === false &&
        !(arg && arg.tables) ? fail({name: 'ScriptError', message: 'The spreadsheet could not be read. Close and reopen Prereasoner.'})
        : ok(window.__server.workbookOptions.restored || {conversationId: '', state: null, stale: false}),
      askPrereasoner: (arg, ok, fail) => { window.__server.pendingAsk = {arg, ok, fail}; },
      savePrereasonerSheetConversation: (arg, ok) => ok({saved: arg.conversationId}),
      clearPrereasonerSheetConversation: (_, ok) => ok({cleared: 'sheet-1'}),
      syncPrereasonerConversation: (arg, ok) => ok({changed: true,
        conversationId: arg.conversationId || 'c_0123456789abcdef0123456789abcdef', sourceHash: 'a'.repeat(64)})
    };
    window.google = {script: {run: {withSuccessHandler(ok) {
      return {withFailureHandler(fail) {
        // `missing`: functions this script does not have, as google.script.run has none it was not published with.
        return new Proxy({}, {get: (_, name) => (workbookOptions.missing || []).includes(name) ? undefined : arg => {
          window.__calls.push({name, arg: arg === undefined ? null : JSON.parse(JSON.stringify(arg))});
          // `failOnce` rejects only the next call to a function, as a deleted conversation does.
          const once = window.__server.workbookOptions.failOnce || {};
          const message = once[name] || failing[name];
          delete once[name];
          setTimeout(() => message ? fail({name: 'ScriptError', message}) : handlers[name](arg, ok, fail), name === 'getSidebarContext' ? (workbookOptions.contextDelay || 10) : 10);
        }});
      }};
    }}}};
  }, {initialRows: rows, failing, workbookOptions});
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
