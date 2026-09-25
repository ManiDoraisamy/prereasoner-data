// The Sheets add-on's server (Code.js): the spreadsheet's cells for the sidebar, and authenticated calls
// to Prereasoner. The sidebar itself (Sidebar.html) is driven in a browser by
// web/tests/browser/sheets-sidebar.spec.js.
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const root = path.resolve(__dirname, '..');
const source = fs.readFileSync(path.join(root, 'Code.js'), 'utf8');
const sidebar = fs.readFileSync(path.join(root, 'Sidebar.html'), 'utf8');
const manifest = JSON.parse(fs.readFileSync(path.join(root, 'appsscript.json'), 'utf8'));
let checks = 0;
const check = (condition, message) => { assert(condition, message); checks++; };
const equal = (actual, expected, message) => { assert.deepStrictEqual(JSON.parse(JSON.stringify(actual)), expected, message); checks++; };

function range(values, formats, merges) {
  return {
    getValues: () => values,
    getNumberFormats: () => formats || values.map(row => row.map(() => 'General')),
    getMergedRanges: () => (merges || []).map(([row, column, lastRow, lastColumn]) => ({
      getRow: () => row, getColumn: () => column, getLastRow: () => lastRow, getLastColumn: () => lastColumn
    }))
  };
}
function sheet(id, name, values, options = {}) {
  return {
    getSheetId: () => id, getName: () => name, getIndex: () => id,
    isSheetHidden: () => Boolean(options.hidden),
    getLastRow: () => values.length, getLastColumn: () => Math.max(0, ...values.map(row => row.length)),
    getRange: (row, column, rows, columns) => {
      assert.deepStrictEqual([row, column, rows, columns], [1, 1, values.length, Math.max(...values.map(r => r.length))]);
      return range(values, options.formats, options.merges);
    }
  };
}
// Code.js in its own context with Apps Script stubs; `responses` answers UrlFetchApp by URL.
function load(spreadsheet, responses = {}) {
  const fetches = [];
  const context = {
    console,
    SpreadsheetApp: {getActiveSpreadsheet: () => spreadsheet},
    ScriptApp: {getOAuthToken: () => 'google-access-token'},
    // Apps Script formats a Date in the spreadsheet's zone; UTC here keeps the arithmetic visible.
    Utilities: {formatDate: (date, zone, pattern) => {
      assert.strictEqual(pattern, 'yyyy,MM,dd,HH,mm,ss');
      const p = n => String(n).padStart(2, '0');
      return [date.getUTCFullYear(), p(date.getUTCMonth() + 1), p(date.getUTCDate()),
        p(date.getUTCHours()), p(date.getUTCMinutes()), p(date.getUTCSeconds())].join(',');
    }},
    UrlFetchApp: {fetch: (url, options) => {
      fetches.push({url, options, body: options.payload ? JSON.parse(options.payload) : null});
      const key = Object.keys(responses).find(prefix => url.startsWith(prefix));
      const [status, body] = key ? responses[key] : [200, {}];
      return {getResponseCode: () => status, getContentText: () => JSON.stringify(body)};
    }}
  };
  vm.createContext(context);
  vm.runInContext(source, context);
  context.fetches = fetches;
  return context;
}
const book = (tabs, active) => ({getId: () => 'sheet-id-1', getName: () => 'Sales data', getSpreadsheetTimeZone: () => 'UTC',
  getActiveSheet: () => active || tabs[0], getSheets: () => tabs});

// Grids: visible non-empty tabs, the active tab first; dates and durations become Sheets' serial day
// counts with their formats; failed formulas and merged ranges travel with the grid.
const orders = sheet(1, 'Orders', [
  ['order ID', 'placed', 'worked', 'ratio', 'Amounts', ''],
  [101, new Date(Date.UTC(2024, 0, 1)), new Date(Date.UTC(1899, 11, 31, 3, 30)), '#DIV/0!', 10, 2]
], {formats: [['General', 'General', 'General', 'General', 'General', 'General'],
              ['0', 'yyyy-mm-dd', '[h]:mm', 'General', '0', '0']],
    merges: [[1, 5, 1, 6]]});
const notes = sheet(2, 'Notes', [['note'], ['keep']]);
const hidden = sheet(3, 'Secret', [['secret'], ['x']], {hidden: true});
const blank = sheet(4, 'Blank', [['only a header']]);
const addon = load(book([orders, notes, hidden, blank], notes));
const context = addon.getSidebarContext();
equal([context.token, context.spreadsheetId, context.name], ['google-access-token', 'sheet-id-1', 'Sales data']);
const grids = context.workbook.grids;
equal(grids.map(grid => grid.name), ['Notes', 'Orders'], 'the active tab first; hidden and header-only tabs skipped');
const grid = grids[1];
equal(grid.rows[1][1], 45292, '2024-01-01 is serial 45292');
check(Math.abs(grid.rows[1][2] - (1 + 3.5 / 24)) < 1e-9, 'a 27:30 duration is 1.1458 days');
equal(grid.formats[1][2], '[h]:mm');
equal(grid.errors[1], [false, false, false, true, false, false]);
equal(grid.merges, [{s: {r: 0, c: 4}, e: {r: 0, c: 5}}]);
equal(addon.getWorkbookGrids().grids[0].rows, [['note'], ['keep']]);

// The sidebar imports these grids with the web upload importer: Sheets' serial days and formats read
// back as the date and the duration, a failed formula is refused, and a data column without a header
// is named.
const {normalizeGrids} = require('../../tests/workbook_fixture.js');
const clean = load(book([sheet(6, 'Hours', [
  ['order ID', 'placed', 'worked'], [101, new Date(Date.UTC(2024, 0, 1)), new Date(Date.UTC(1899, 11, 31, 3, 30))]
], {formats: [['General', 'General', 'General'], ['0', 'yyyy-mm-dd', '[h]:mm']]})])).getSidebarContext();
equal(normalizeGrids(clean.workbook.grids)[0].csv, 'order ID,placed,worked\n101,2024-01-01,1.145833333');
assert.throws(() => normalizeGrids(grids), /Sheet "Orders": No unambiguous header found/); checks++;   // a merged header cell is not a name
const failed = load(book([sheet(8, 'Ratios', [['id', 'ratio'], [1, '#DIV/0!']])])).getSidebarContext();
assert.throws(() => normalizeGrids(failed.workbook.grids), /formula error/); checks++;
const shifted = load(book([sheet(7, 'sales', [['order ID', 'customer', 'amount'], [1, 101, 'Holmes', 118]])])).getSidebarContext();
assert.throws(() => normalizeGrids(shifted.workbook.grids),
  /^Error: Sheet "sales": Column D has values but no header, and the headers look one column to the left of their data \(C1 "amount" is above "Holmes"\)\./); checks++;

// Apps Script cannot load web/public/lib/upload-limits.js, so the add-on's copy is pinned to it here.
const shared = {};
vm.runInNewContext(fs.readFileSync(path.join(root, '../web/public/lib/upload-limits.js'), 'utf8'), shared);
for (const key of ['sheets', 'rows', 'columns']) equal(addon.GRID_LIMITS[key], shared.UPLOAD_LIMITS[key], key);

// The upload's worksheet limits hold per tab before any cell is read: two 6,000-row tabs are read, as an
// upload of them would be; a tab past 10,000 data rows or 256 columns is refused by name.
const rows = count => [['id', 'amount']].concat(Array.from({length: count}, (_, i) => [i + 1, 1]));
equal(load(book([sheet(7, 'A', rows(6000)), sheet(8, 'B', rows(6000))])).getSidebarContext().workbook.grids.length, 2);
assert.throws(() => load(book([sheet(9, 'Long', rows(10001))])).getSidebarContext(),
  /^Error: Sheet "Long": each worksheet may contain at most 10,000 data rows$/); checks++;
const wide = sheet(5, 'Wide', [Array.from({length: 257}, (_, i) => 'c' + i), Array.from({length: 257}, () => 1)]);
assert.throws(() => load(book([wide])).getSidebarContext(), /^Error: Sheet "Wide": each worksheet may contain at most 256 columns$/); checks++;
const dense = [Array.from({length: 26}, (_, i) => 'c' + i)].concat(Array.from({length: 10000}, () => Array(26).fill(1)));
assert.throws(() => load(book([sheet(10, 'Dense', dense)])).getSidebarContext(), /too large to analyze in one request/); checks++;
assert.throws(() => load(book([blank])).getSidebarContext(), /header row and at least one data row/); checks++;

// Prereasoner calls go server to server with the Firebase identity of the Google account.
const tables = [{name: 'Orders', data: 'country,amount\nFrance,840', source: {kind: 'upload'}}];
const views = [{name: 'france_orders', op: 'filter', label: "where country = 'France'", inputs: ['c_x'],
  columns: ['amount'], rows: [[840]], python: 'orders.filter(...)'}];
const api = load(book([notes]), {
  'https://identitytoolkit.googleapis.com/': [200, {idToken: 'firebase-id-token'}],
  'https://prereasoner-chat-271377281957.us-central1.run.app/chat': [200, {
    reply: ' Total sales in France are **US$840**. ', conversation_id: 'c_0123456789abcdef0123456789abcdef',
    history: [{role: 'user', content: 'total'}, {role: 'tool', content: 'x'}],
    traces: [{jobId: 'job-1', question: 'total amount in France', engine: {views, analysis: {slug: 'france_sales'},
      execution: {actual: 'python'}, answer: {rows: [[840]]}}}]}],
  'https://chat.prereasoner.com/api/spreadsheet/conversation/restore': [200, {conversation_id: '', state: null, source_changed: false}]
});
const answer = api.askPrereasoner({question: '  What are total sales in France? ', tables,
  conversationId: 'c_0123456789abcdef0123456789abcdef', history: [{role: 'user', content: 'hi'}], turnId: 'turn_1'});
const chat = api.fetches.find(call => call.url.endsWith('/chat'));
equal(chat.options.headers.Authorization, 'Bearer firebase-id-token');
equal(chat.body, {message: 'What are total sales in France?', tables: [{name: 'Orders', data: 'country,amount\nFrance,840',
  source: {kind: 'google-sheets-addon'}}], history: [{role: 'user', content: 'hi'}],
  conversation_id: 'c_0123456789abcdef0123456789abcdef', turnId: 'turn_1'});
equal(answer.reply, 'Total sales in France are **US$840**.');
equal(answer.history, [{role: 'user', content: 'total'}], 'only user and assistant history returns');
equal(answer.traces, [{jobId: 'job-1', question: 'total amount in France', analysis: {slug: 'france_sales'},
  execution: {actual: 'python'}, views: [{name: 'france_orders', op: 'filter', label: "where country = 'France'", inputs: ['c_x']}]}],
  'steps travel without their rows');
assert.throws(() => api.askPrereasoner({question: 'x', tables, turnId: 'bad id!'}), /live request id is invalid/); checks++;
assert.throws(() => api.askPrereasoner({question: 'x', tables: [{name: 'Orders'}]}), /could not be read/); checks++;
api.restorePrereasonerSheetConversation({tables});
api.savePrereasonerSheetConversation({conversationId: 'c_0123456789abcdef0123456789abcdef', state: {version: 2}});
api.clearPrereasonerSheetConversation();
api.syncPrereasonerConversation({conversationId: 'c_0123456789abcdef0123456789abcdef', tables});
equal(api.fetches.filter(call => call.url.startsWith('https://chat.prereasoner.com/')).map(call => [call.url.slice('https://chat.prereasoner.com'.length), call.body]), [
  ['/api/spreadsheet/conversation/restore', {spreadsheet_id: 'sheet-id-1', host: 'sheets', tables: [{name: 'Orders',
    data: 'country,amount\nFrance,840', source: {kind: 'google-sheets-addon'}}]}],
  ['/api/spreadsheet/conversation/state', {spreadsheet_id: 'sheet-id-1', host: 'sheets',
    conversation_id: 'c_0123456789abcdef0123456789abcdef', state: {version: 2}}],
  ['/api/spreadsheet/conversation/clear', {spreadsheet_id: 'sheet-id-1', host: 'sheets'}],
  ['/api/conversation/sync', {id: 'c_0123456789abcdef0123456789abcdef', tables: [{name: 'Orders',
    data: 'country,amount\nFrance,840', source: {kind: 'google-sheets-addon'}}]}]
]);
const denied = load(book([notes]), {'https://identitytoolkit.googleapis.com/': [200, {idToken: 't'}],
  'https://chat.prereasoner.com/': [401, {error: 'sign in required'}]});
assert.throws(() => denied.clearPrereasonerSheetConversation(), /^Error: Prereasoner: Google sign-in could not be verified\.$/); checks++;

// The sidebar is the add-on's own UI and renders with the web's shared code: the rail component, the
// importer and the live trace, all from chat.prereasoner.com; it keeps no step presentation of its own.
for (const script of ['https://chat.prereasoner.com/lib/turn-renderer.js', 'https://chat.prereasoner.com/lib/workbook-import.js',
  'https://chat.prereasoner.com/vendor/xlsx-0.20.3.full.min.js', "from 'https://chat.prereasoner.com/lib/firebase-init.js'"]) {
  check(sidebar.includes(script), script);
}
check(sidebar.includes('https://ssl.gstatic.com/docs/script/css/add-ons1.css'), 'the add-on keeps Sheets’ own styling');
check(sidebar.includes('R.stepsFromViews(') && sidebar.includes('window.subscribeTurn') && sidebar.includes('WORKBOOK_IMPORT.convert('),
  'steps, the live trace and the import come from the shared web code');
check(!/liveStepLabel|operationLabel|liveJson|iframe/.test(sidebar), 'no second step presentation, no polling, no framed page');
check(sidebar.includes('When you send a question, \' +\n            \'Prereasoner securely processes your question and the visible, non-empty tabs in this spreadsheet to produce and save \' +\n            \'the answer. This data is not used to train generalized AI models.'),
  'the data-use notice the OAuth verification describes');

// Read-only and bounded: no writes, no storage, no CSV conversion of its own.
check(!/PropertiesService|setValue|setValues|valuesToCsv_|normalizeHeaders_|column_/.test(source), 'read-only, one importer');
equal(manifest.urlFetchWhitelist, ['https://chat.prereasoner.com/', 'https://prereasoner-chat-271377281957.us-central1.run.app/',
  'https://identitytoolkit.googleapis.com/']);
equal(manifest.oauthScopes.slice().sort(), [
  'https://www.googleapis.com/auth/script.container.ui',
  'https://www.googleapis.com/auth/script.external_request',
  'https://www.googleapis.com/auth/spreadsheets.currentonly',
  'https://www.googleapis.com/auth/userinfo.email',
  'https://www.googleapis.com/auth/userinfo.profile',
  'openid'
]);
check(source.includes("addItem('Ask a question', 'showSidebar')") && source.includes("addItem('Previous conversations', 'showPreviousConversations')"),
  'the Extensions menu');

console.log('Sheets add-on: ' + checks + ' checks passed');
