// The Sheets add-on's server (Code.js): the spreadsheet's cells for the sidebar, and authenticated calls
// to Prereasoner. The sidebar itself (Sidebar.html) is driven in a browser by
// web/tests/browser/sheets-sidebar.spec.js.
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const root = path.resolve(__dirname, '..');
const source = fs.readFileSync(path.join(root, 'Code.js'), 'utf8');
const shell = fs.readFileSync(path.join(root, 'Sidebar.html'), 'utf8');
const sidebar = fs.readFileSync(path.join(root, '../web/public/office/sheets/sidebar.html'), 'utf8') + fs.readFileSync(path.join(root, '../web/public/office/sheets/sidebar.js'), 'utf8') + fs.readFileSync(path.join(root, '../web/public/office/sheets/sidebar-firebase.js'), 'utf8');
checkShell();
function checkShell(){assert(shell.includes('/office/sheets/boot.js'));assert(!shell.includes('<style>'));assert(!shell.includes('google.script.run'));}
const manifest = JSON.parse(fs.readFileSync(path.join(root, 'appsscript.json'), 'utf8'));
let checks = 0;
const check = (condition, message) => { assert(condition, message); checks++; };
const equal = (actual, expected, message) => { assert.deepStrictEqual(JSON.parse(JSON.stringify(actual)), expected, message); checks++; };

// A Range: its cells are cut from the tab's only when asked, and a read of them is noted in `reads`, so a test can say
// what was read.
function range(values, formats, merges, [row, column, rows, columns], reads) {
  const cut = matrix => matrix.slice(row - 1, row - 1 + rows).map(cells => {
    const part = cells.slice(column - 1, column - 1 + columns);
    while (part.length < columns) part.push('');
    return part;
  });
  return {
    getValues: () => { reads.push([row, column, rows, columns]); return cut(values); },
    getNumberFormats: () => cut(formats || values.map(cells => cells.map(() => 'General'))),
    isBlank: () => values.slice(row - 1, row - 1 + rows).every(cells =>
      cells.slice(column - 1, column - 1 + columns).every(value => value === '' || value === null)),
    getMergedRanges: () => (merges || []).map(([row, column, lastRow, lastColumn]) => ({
      getRow: () => row, getColumn: () => column, getLastRow: () => lastRow, getLastColumn: () => lastColumn
    }))
  };
}
function sheet(id, name, values, options = {}) {
  const reads = [];
  return {
    reads,
    getType: () => 'GRID',
    getSheetId: () => id, getName: () => name, getIndex: () => id,
    isSheetHidden: () => Boolean(options.hidden),
    getLastRow: () => values.length, getLastColumn: () => Math.max(0, ...values.map(row => row.length)),
    getRange: (...area) => range(values, options.formats, options.merges, area, reads)
  };
}
// A chart tab as Apps Script serves it: a type and a name, and every question about its cells refused.
function chartTab(id, name) {
  const refuse = () => { throw new Error('The action is not supported for OBJECT sheet.'); };
  return {getType: () => 'OBJECT', getSheetId: () => id, getName: () => name, getIndex: () => id, isSheetHidden: () => false,
    getLastRow: refuse, getLastColumn: refuse, getRange: refuse};
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
  // What the sidebar does: the plan, then each tab, some of which may come back left out.
  context.readAll = scope => {
    const plan = context.getWorkbookPlan({scope});
    const read = plan.tabs.map(name => context.getWorkbookTab({name}));
    return {grids: read.filter(tab => !tab.leftOut), scope: plan.scope, availableTabs: plan.availableTabs,
      leftOut: read.filter(tab => tab.leftOut).map(tab => ({name: tab.name, reason: tab.leftOut})).concat(plan.leftOut)};
  };
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
equal(context.uploadOnce, true, 'the hosted sidebar uploads a sheet once with this script');
check(!('workbook' in context), 'the token does not wait for the cells');
equal([context.tabs, context.scope, context.availableTabs], [['Notes', 'Orders'], 'all', ['Notes', 'Orders']]);
const grids = addon.readAll().grids;
equal(grids.map(grid => grid.name), ['Notes', 'Orders'], 'the active tab first; hidden and header-only tabs skipped');
const grid = grids[1];
equal(grid.rows[1][1], 45292, '2024-01-01 is serial 45292');
check(Math.abs(grid.rows[1][2] - (1 + 3.5 / 24)) < 1e-9, 'a 27:30 duration is 1.1458 days');
equal(grid.columnFormats[2], {f: '[h]:mm'}, 'a column format, sent once');
equal(grid.errorCells, [[1, 3]], 'the one failed formula');
check(!('formats' in grid) && !('errors' in grid), 'no per-cell formats or error flags');
equal(grid.merges, [{s: {r: 0, c: 4}, e: {r: 0, c: 5}}]);
equal(addon.readAll().grids[0].rows, [['note'], ['keep']]);

// The sidebar imports these grids with the web upload importer: Sheets' serial days and formats read
// back as the date and the duration, a failed formula is refused, and a data column without a header
// is named.
const {normalizeGrids} = require('../../tests/workbook_fixture.js');
const clean = load(book([sheet(6, 'Hours', [
  ['order ID', 'placed', 'worked'], [101, new Date(Date.UTC(2024, 0, 1)), new Date(Date.UTC(1899, 11, 31, 3, 30))]
], {formats: [['General', 'General', 'General'], ['0', 'yyyy-mm-dd', '[h]:mm']]})])).readAll();
equal(normalizeGrids(clean.grids)[0].csv, 'order ID,placed,worked\n101,2024-01-01,1.145833333');
assert(normalizeGrids(grids)[1].csv.includes('Column F')); checks++; // merged headers preserve positions
const failed = load(book([sheet(8, 'Ratios', [['id', 'ratio'], [1, '#DIV/0!']])])).readAll();
assert(normalizeGrids(failed.grids)[0].csv.includes('#DIV/0!')); checks++;
check(normalizeGrids(failed.grids)[0].import.warnings.some(text => text.includes('unavailable in columns B')),
  'a failed formula is read as an error, not as text');
// Formats travel per column, with the number cells whose format differs, and errors as the cells that hold
// one: the import reads them exactly as the per-cell formats and flags a script published before sends.
const mixedFormats = [['General', 'General', 'General'], ['yyyy-mm-dd', '0.00', 'General'],
                      ['yyyy-mm-dd', '[h]:mm', 'General'], ['yyyy-mm-dd', '0.00', 'General'], ['General', '0.00', 'General']];
const mixed = load(book([sheet(9, 'Shifts', [
  ['day', 'hours', 'ratio'],
  [new Date(Date.UTC(2024, 0, 1)), 1.5, 0.25],
  [new Date(Date.UTC(2024, 0, 2)), new Date(Date.UTC(1899, 11, 31, 3, 30)), '#DIV/0!'],
  [new Date(Date.UTC(2024, 0, 3)), 2.25, 0.5],
  ['', '', '']
], {formats: mixedFormats})])).readAll().grids[0];
equal(mixed.columnFormats, [{f: 'yyyy-mm-dd'}, {f: '0.00', x: [[2, '[h]:mm']]}, {f: 'General'}],
  'a text or blank cell sends no format');
equal(mixed.errorCells, [[2, 2]]);
const perCell = {name: mixed.name, rows: mixed.rows, formats: mixedFormats, merges: mixed.merges, date1904: false,
  errors: mixed.rows.map((row, r) => row.map((_, c) => r === 2 && c === 2))};
equal(JSON.stringify(normalizeGrids([mixed])), JSON.stringify(normalizeGrids([perCell])), 'compact and per-cell grids import alike');
check(normalizeGrids([mixed])[0].csv.startsWith('day,hours,ratio\n2024-01-01,1.5,0.25\n2024-01-02,1.145833333,#DIV/0!'),
  'the column format reads the dates; the row format reads the duration');
const shifted = load(book([sheet(7, 'sales', [['order ID', 'customer', 'amount'], [1, 101, 'Holmes', 118]])])).readAll();
assert(normalizeGrids(shifted.grids)[0].csv.startsWith('Column A,Column B,Column C,Column D')); checks++;

// A date's zone offset is asked of Apps Script once for a day its clock does not change on; a day it changes on
// is asked moment by moment, so the wall clock is right on both sides of the change.
{
  const zoned = load(book([sheet(20, 'Dates', [['when']].concat(
    Array.from({length: 2000}, (_, i) => [new Date(Date.UTC(2024, 0, 1 + i % 3, i % 24, i % 60))]),
    [[new Date(Date.UTC(2024, 2, 10, 1, 30))], [new Date(Date.UTC(2024, 2, 10, 2, 30))]]),
    {formats: [['General']].concat(Array.from({length: 2002}, () => ['yyyy-mm-dd hh:mm']))})]));
  let asked = 0;
  const change = Date.UTC(2024, 2, 10, 2);
  zoned.Utilities.formatDate = (date, zone, pattern) => {
    asked++;
    const local = new Date(date.getTime() + (date.getTime() >= change ? 3600000 : 0));
    const p = n => String(n).padStart(2, '0');
    return [local.getUTCFullYear(), p(local.getUTCMonth() + 1), p(local.getUTCDate()),
      p(local.getUTCHours()), p(local.getUTCMinutes()), p(local.getUTCSeconds())].join(',');
  };
  const dates = zoned.readAll().grids[0].rows;
  check(asked < 20, `a few days of dates ask the zone a few times, not ${asked}`);
  const serial = (y, m, d, h, min) => (Date.UTC(y, m, d, h, min) - Date.UTC(1899, 11, 30)) / 86400000;
  equal(dates[1][0], serial(2024, 0, 1, 0, 0));
  equal(dates[2001][0], serial(2024, 2, 10, 1, 30), 'before the clock changes');
  equal(dates[2002][0], serial(2024, 2, 10, 3, 30), 'after it');
}

// Apps Script cannot load web/public/lib/upload-limits.js, so the add-on's copy is pinned to it here.
const shared = {};
vm.runInNewContext(fs.readFileSync(path.join(root, '../web/public/lib/upload-limits.js'), 'utf8'), shared);
for (const key of ['sheets', 'rows', 'columns', 'cells']) equal(addon.GRID_LIMITS[key], shared.UPLOAD_LIMITS[key], key);

// A chart tab is skipped by everything that walks the tabs: "The action is not supported for OBJECT sheet" kept four real
// workbooks from opening (2026-11-01), active or not.
{
  const charted = load(book([chartTab(30, 'Chart1'), notes, chartTab(31, 'Chart2'), orders], notes));
  const opened = charted.getSidebarContext();
  equal([opened.tabs, opened.scope, opened.availableTabs, opened.leftOut], [['Notes', 'Orders'], 'all', ['Notes', 'Orders'], []]);
  equal(charted.readAll().grids.map(grid => grid.name), ['Notes', 'Orders'], 'the chart tabs are not read');
  equal(charted.getWorkbookSchema({}).sheets.map(tab => tab.name), ['Notes', 'Orders'], 'nor described');
  assert.throws(() => charted.getWorkbookTab({name: 'Chart1'}), /changed while it was being read/); checks++;
  const onChart = load(book([chartTab(30, 'Chart1'), notes], chartTab(30, 'Chart1')));
  equal([onChart.getSidebarContext().activeSheet, onChart.getSidebarContext().tabs], ['Chart1', ['Notes']], 'open on a chart tab');
  assert.throws(() => onChart.getWorkbookPlan({scope: 'active'}), /current sheet has no data rows/); checks++;
  assert.throws(() => load(book([chartTab(30, 'Chart1')])).getSidebarContext(), /header row and at least one data row/); checks++;
}

// The upload's worksheet limits hold per tab: two 30,000-row tabs are read, as an upload of them would be.
const rows = count => [['id', 'amount']].concat(Array.from({length: count}, (_, i) => [i + 1, 1]));
equal(load(book([sheet(7, 'A', rows(30000)), sheet(8, 'B', rows(30000))])).readAll().grids.length, 2);
const realCustomerShape = [Array.from({length: 18}, (_, i) => 'c' + i)]
  .concat(Array.from({length: 30000}, (_, i) => [i + 1].concat(Array(17).fill('value'))));
equal(load(book([sheet(10, 'Subscriptions', realCustomerShape)])).readAll().grids.length, 1,
  'a 30,000-row customer-shaped sheet fits the add-on grid cap');
assert.throws(() => load(book([blank])).getSidebarContext(), /header row and at least one data row/); checks++;

// A tab past them that really holds that much is left out, by name and with why, and never takes the workbook down: "The
// active sheet exceeds the current analysis capacity" kept a whole real workbook from opening (2026-11-01).
const dense = [Array.from({length: 26}, (_, i) => 'c' + i)].concat(Array.from({length: 40000}, () => Array(26).fill(1)));
const wide = sheet(5, 'Wide', [Array.from({length: 257}, (_, i) => 'c' + i), Array.from({length: 257}, () => 1)]);
for (const [tab, reason] of [[sheet(9, 'Long', rows(50001)), 'it has more than 50,000 data rows'], [wide, 'it has more than 256 columns'],
  [sheet(15, 'Dense', dense), 'it has more than 1,000,000 cells']]) {
  const alone = load(book([tab]));
  equal(alone.getSidebarContext().tabs, [tab.getName()], 'the plan does not refuse it, and holds up no token looking at its cells');
  equal(tab.reads, [], 'no cell is read for the plan');
  equal(alone.getWorkbookTab({name: tab.getName()}), {name: tab.getName(), leftOut: reason});
}
const scoped = load(book([sheet(11, 'NT', rows(30000)), sheet(12, 'SI', dense), sheet(14, 'FF', rows(100))]));
const scopedBook = scoped.readAll();
equal(scopedBook.scope, 'all');
equal(scopedBook.grids.map(grid => grid.name), ['NT', 'FF'], 'the other tabs are read');
equal(scopedBook.leftOut, [{name: 'SI', reason: 'it has more than 1,000,000 cells'}]);
equal(scopedBook.availableTabs, ['NT', 'SI', 'FF']);
equal(scoped.getWorkbookPlan({scope:'active'}).tabs, ['NT']);
equal(load(book([sheet(11, 'NT', rows(30000)), sheet(12, 'SI', dense, {hidden: true}),
  sheet(14, 'FF', rows(100))])).readAll().grids.map(grid => grid.name), ['NT', 'FF'],
  'explicit visible-tab scope still admits the complete 30,000-row source');
// So does a read that fails, and a tab too large to look into is not read at all.
{
  const huge = {getType: () => 'GRID', getName: () => 'Huge', getIndex: () => 1, isSheetHidden: () => false, getLastRow: () => 3000000,
    getLastColumn: () => 5, getRange: () => ({isBlank: () => false, getValues: () => { throw new Error('read ' + 3000000 * 5 + ' cells'); }})};
  equal(load(book([huge])).getWorkbookTab({name: 'Huge'}), {name: 'Huge', leftOut: 'it has more than 50,000 data rows'});
  const failing = {getType: () => 'GRID', getName: () => 'Failing', getIndex: () => 1, isSheetHidden: () => false, getLastRow: () => 60000,
    getLastColumn: () => 5, getRange: () => ({isBlank: () => false, getValues: () => { throw new Error('Service Spreadsheets failed'); }})};
  equal(load(book([failing])).getWorkbookTab({name: 'Failing'}), {name: 'Failing', leftOut: 'it has more than 50,000 data rows'});
}
// One read holds the upload's eight tabs and its million cells, the active tab first; the rest are named, not lost in
// silence, where the whole read used to fall back to the active tab alone.
{
  const tabs = Array.from({length: 10}, (_, i) => sheet(40 + i, 'T' + i, rows(1)));
  const many = load(book(tabs, tabs[9])).getWorkbookPlan({});
  equal(many.tabs, ['T9', 'T0', 'T1', 'T2', 'T3', 'T4', 'T5', 'T6']);
  equal(many.leftOut, [{name: 'T7', reason: 'one read holds 8 tabs'}, {name: 'T8', reason: 'one read holds 8 tabs'}]);
  const big = Array.from({length: 40000}, () => Array(15).fill(1));
  const budget = load(book([sheet(53, 'B1', big), sheet(54, 'B2', big), sheet(55, 'Small', rows(100))]));
  equal(budget.getWorkbookPlan({}).tabs, ['B1', 'Small']);
  equal(budget.getWorkbookPlan({}).leftOut, [{name: 'B2', reason: 'one read holds 1,000,000 cells'}]);
  equal(budget.getWorkbookSchema({}).scope, ['B1', 'Small'], 'suggestions describe what is read');
}

// A tab past them only because of where its cells are is read as what it holds, with no filled cell dropped. This is the
// real one: one number in column IV of a 7,827-row tab made a grid of 2,003,712 cells, 31,943 of them filled (2026-11-01).
{
  const grid = Array.from({length: 7827}, (_, r) => Array.from({length: 256}, (_, c) => {
    if (c === 0) return r + 1;
    if (c <= 10) return r < 540 ? c : '';
    if (c <= 12) return r < 5443 ? c : '';
    if (c === 13) return r < 7826 ? 'time' : '';
    return r === 0 && [14, 15, 16, 255].includes(c) ? c : '';
  }));
  const filled = table => table.flat().filter(value => value !== '').length;
  const results = sheet(60, 'Results', grid);
  const addonOf = load(book([results, sheet(61, 'EOL', rows(100))]));
  equal(addonOf.getSidebarContext().tabs, ['Results', 'EOL']);
  const read = addonOf.getWorkbookTab({name: 'Results'});
  check(!read.leftOut, 'the tab fits once it is trimmed to what it holds');
  equal([read.rows.length, read.rows[0].length, filled(read.rows)], [7827, 18, filled(grid)]);
  equal(filled(grid), 31943);
  equal(read.rows[0].slice(14), [14, 15, 16, 255], 'the far column stays, next to the others');
  equal(results.reads, [[1, 1, 7827, 17], [1, 256, 7827, 1]], 'only the columns that hold anything are read');
  equal(read.columnFormats.length, 18);
  check(normalizeGrids([read])[0].csv.startsWith('Column A,Column B'), 'the importer reads the trimmed tab');
}
// Formats and merges stay with their cells, and a merge over a dropped column ends at the last kept one.
{
  const row = (first, last) => { const cells = Array(400).fill(''); cells[0] = first; cells[1] = last; cells[399] = last; return cells; };
  const formats = Array.from({length: 3}, () => { const cells = Array(400).fill('General'); cells[1] = 'yyyy-mm-dd'; cells[399] = '0.00'; return cells; });
  const far = sheet(62, 'Far', [row('id', 'placed'), row(1, 45292), row(2, 45293)],
    {formats, merges: [[1, 1, 1, 2], [2, 2, 2, 200], [3, 300, 3, 301]]});
  const read = load(book([far])).getWorkbookTab({name: 'Far'});
  equal(read.rows, [['id', 'placed', 'placed'], [1, 45292, 45292], [2, 45293, 45293]]);
  equal(read.columnFormats, [{f: 'General'}, {f: 'yyyy-mm-dd'}, {f: '0.00'}]);
  equal(read.merges, [{s: {r: 0, c: 0}, e: {r: 0, c: 1}}], 'a merge left on one cell, and one whose first cell went, merge nothing');
}
// Rows too: a stray cell far below keeps its value, and one blank row still separates it from the table above it.
{
  const table = rows(100), gap = Array.from({length: 60000}, () => ['', '']);
  const tall = sheet(63, 'Tall', table.concat(gap, [['note', 7]]));
  const read = load(book([tall])).getWorkbookTab({name: 'Tall'});
  check(!read.leftOut, 'the tab fits without its blank rows');
  equal([read.rows.length, read.rows[100], read.rows[101], read.rows[102]],
    [103, [100, 1], ['', ''], ['note', 7]]);
  // The columns alone bringing a tab within the limits keep its rows where they are.
  const spaced = sheet(64, 'Spaced', [['a', 'b'], ['', ''], [1, 2]].map(cells => cells.concat(Array(300).fill(''), [cells[0] === 'a' ? 'z' : ''])));
  equal(load(book([spaced])).getWorkbookTab({name: 'Spaced'}).rows, [['a', 'b', 'z'], ['', '', ''], [1, 2, '']]);
  // A tab still past them once trimmed is left out, saying which limit it is past.
  const stillLong = rows(50001).map((cells, r) => cells.concat(Array(300).fill(''), [r === 0 ? 'stray' : '']));
  equal(load(book([sheet(65, 'StillLong', stillLong)])).getWorkbookTab({name: 'StillLong'}),
    {name: 'StillLong', leftOut: 'it has more than 50,000 data rows'});
}

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
// A question names the sheet the conversation stores by its hash; the cells travel only with the upload.
const stored = 'a'.repeat(64);
const answer = api.askPrereasoner({question: '  What are total sales in France? ', sourceHash: stored,
  conversationId: 'c_0123456789abcdef0123456789abcdef', history: [{role: 'user', content: 'hi'}], turnId: 'turn_1'});
const chat = api.fetches.find(call => call.url.endsWith('/chat'));
equal(chat.options.headers.Authorization, 'Bearer firebase-id-token');
equal(chat.body, {message: 'What are total sales in France?', history: [{role: 'user', content: 'hi'}],
  conversation_id: 'c_0123456789abcdef0123456789abcdef', source_hash: stored, active_sheet: 'Notes', turnId: 'turn_1'});
equal(answer.reply, 'Total sales in France are **US$840**.');
equal(answer.history, [{role: 'user', content: 'total'}], 'only user and assistant history returns');
equal(answer.traces, [{jobId: 'job-1', question: 'total amount in France', check: null, analysis: {slug: 'france_sales'},
  execution: {actual: 'python'}, views: [{name: 'france_orders', op: 'filter', label: "where country = 'France'", inputs: ['c_x']}]}],
  'steps travel without their rows');
// An answer's mark travels with its trace, and the person's Retry is a flag on the chat request (owner, 2026-11-21).
{
  const check = {state: 'warned', reason: 'I read this as “total amount”, 50% sure.'};
  const marked = load(book([notes]), {
    'https://identitytoolkit.googleapis.com/': [200, {idToken: 'firebase-id-token'}],
    'https://prereasoner-chat-271377281957.us-central1.run.app/chat': [200, {
      reply: '840', conversation_id: 'c_0123456789abcdef0123456789abcdef', history: [],
      traces: [{jobId: 'job-2', question: 'total amount', engine: {check, answer: {rows: [[840]]}}}]}]
  });
  const retried = marked.askPrereasoner({question: 'total amount', sourceHash: stored,
    conversationId: 'c_0123456789abcdef0123456789abcdef', turnId: 'turn_2', retry: true});
  equal(retried.traces[0].check, check, 'the mark and its reason reach the sidebar');
  equal(marked.fetches.find(call => call.url.endsWith('/chat')).body.retry, true, 'a Retry asks the engine to retry');
}
assert.throws(() => api.askPrereasoner({question: 'x', sourceHash: stored,
  conversationId: 'c_0123456789abcdef0123456789abcdef', turnId: 'bad id!'}), /live request id is invalid/); checks++;
assert.throws(() => api.askPrereasoner({question: 'x', conversationId: 'c_0123456789abcdef0123456789abcdef'}),
  /Send the sheet to Prereasoner before asking/); checks++;
assert.throws(() => api.syncPrereasonerConversation({tables: [{name: 'Orders'}]}), /could not be read/); checks++;
api.restorePrereasonerSheetConversation();
api.savePrereasonerSheetConversation({conversationId: 'c_0123456789abcdef0123456789abcdef', state: {version: 2}});
api.clearPrereasonerSheetConversation();
api.syncPrereasonerConversation({conversationId: 'c_0123456789abcdef0123456789abcdef', tables});
equal(api.fetches.filter(call => call.url.startsWith('https://chat.prereasoner.com/')).map(call => [call.url.slice('https://chat.prereasoner.com'.length), call.body]), [
  ['/api/spreadsheet/conversation/restore', {spreadsheet_id: 'sheet-id-1', host: 'sheets'}],   // no cells
  ['/api/spreadsheet/conversation/state', {spreadsheet_id: 'sheet-id-1', host: 'sheets',
    conversation_id: 'c_0123456789abcdef0123456789abcdef', state: {version: 2}}],
  ['/api/spreadsheet/conversation/clear', {spreadsheet_id: 'sheet-id-1', host: 'sheets'}],
  ['/api/conversation/sync', {id: 'c_0123456789abcdef0123456789abcdef', question: '', tables: [{name: 'Orders',
    data: 'country,amount\nFrance,840', source: {kind: 'google-sheets-addon'}}]}]
]);
// The first upload starts the conversation, and the question names it; a replaced stored sheet (409) is
// reported to the sidebar, which uploads it again and asks once more.
const first = load(book([notes]), {
  'https://identitytoolkit.googleapis.com/': [200, {idToken: 'firebase-id-token'}],
  'https://chat.prereasoner.com/api/conversation/sync': [200, {conversation_id: 'c_0123456789abcdef0123456789abcdef',
    changed: true, source_hash: stored}],
  'https://prereasoner-chat-271377281957.us-central1.run.app/chat': [409, {error: 'the sheets changed', source_hash: 'b'.repeat(64)}]
});
equal(first.syncPrereasonerConversation({question: 'total sales', tables}),
  {changed: true, conversationId: 'c_0123456789abcdef0123456789abcdef', sourceHash: stored});
equal(first.fetches.find(call => call.url.endsWith('/api/conversation/sync')).body.id, '');
equal(first.askPrereasoner({question: 'total sales', sourceHash: stored,
  conversationId: 'c_0123456789abcdef0123456789abcdef'}), {sourceChanged: true});
const denied = load(book([notes]), {'https://identitytoolkit.googleapis.com/': [200, {idToken: 't'}],
  'https://chat.prereasoner.com/': [401, {error: 'sign in required'}]});
assert.throws(() => denied.clearPrereasonerSheetConversation(), /^Error: Prereasoner: Google sign-in could not be verified\.$/); checks++;
const quota = load(book([notes]), {'https://identitytoolkit.googleapis.com/': [200, {idToken: 't'}],
  'https://prereasoner-chat-271377281957.us-central1.run.app/chat': [429,
    {error: 'conversation limit reached; delete a saved chat from Chats to start another one'}]});
const named = {conversationId: 'c_0123456789abcdef0123456789abcdef', sourceHash: 'a'.repeat(64)};
assert.throws(() => quota.askPrereasoner({question: 'total', ...named, turnId: 'quota-test'}),
  /conversation limit reached; delete a saved chat from Chats to start another one/); checks++;
const limited = load(book([notes]), {'https://identitytoolkit.googleapis.com/': [200, {idToken: 't'}],
  'https://prereasoner-chat-271377281957.us-central1.run.app/chat': [429, {error: 'request rate limit exceeded'}]});
assert.throws(() => limited.askPrereasoner({question: 'total', ...named, turnId: 'rate-test'}),
  /too many requests; wait a moment and try again/); checks++;

// The sidebar is the add-on's own UI and renders with the web's shared code: the rail component, the
// importer and the live trace, all from chat.prereasoner.com; it keeps no step presentation of its own.
for (const script of ['https://chat.prereasoner.com/lib/turn-renderer.js', 'https://chat.prereasoner.com/lib/workbook-import.js',
  'https://chat.prereasoner.com/vendor/xlsx-0.20.3.full.min.js', "from 'https://chat.prereasoner.com/lib/firebase-init.js'"]) {
  check(sidebar.includes(script), script);
}
// Google's add-on stylesheet spaces adjacent buttons (`button + button { margin-left: 12px }`), which
// pushed every suggested question after the first to the right; the sidebar styles itself.
check(!sidebar.includes('add-ons1.css'), 'the sidebar does not load Google’s add-on button styles');
check(sidebar.includes('R.stepsFromViews(') && sidebar.includes('window.subscribeTurn') && sidebar.includes('WORKBOOK_IMPORT.convert('),
  'steps, the live trace and the import come from the shared web code');
check(!/liveStepLabel|operationLabel|liveJson|iframe/.test(sidebar), 'no second step presentation, no polling, no framed page');
// No notices in the sidebar (CLAUDE.md privacy rule: one durable /privacy page, no repeated notices).
check(!sidebar.includes('This data is not used to train') && !sidebar.includes('Questions for this sheet'),
  'the sidebar shows no data-use notice and no suggestion heading');
// Suggestions describe visible, populated tabs only: a hidden tab's name and headers went to the
// suggestion service when the sidebar opened (2026-10-04).
const headerTab = (name, headers, hidden) => ({getType: () => 'GRID', getName: () => name, getIndex: () => 0, isSheetHidden: () => hidden,
  getLastRow: () => 3, getLastColumn: () => headers.length, getRange: () => ({getValues: () => [headers]})});
const described = load(book([headerTab('Orders', ['id', 'amount'], false), headerTab('Payroll', ['employee', 'salary'], true)]))
  .getWorkbookSchema({});
check(JSON.stringify(described.sheets.map(sheet => sheet.name)) === '["Orders"]' && !JSON.stringify(described).includes('Payroll'),
  'hidden tabs are not described for suggestions');
// Opening a previous conversation called syncPrereasonerConversation without tables, which always
// failed (requestTables_ throws) before the link opened (2026-10-04). The link opens directly.
const previousPage = fs.readFileSync(path.join(root, '../web/public/office/sheets/previous.js'), 'utf8');
check(!previousPage.includes('google.script.run'), 'opening a previous conversation makes no server call');

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

// The hosted sidebar itself, with its importer, on stubs of the page, against a script: google.script.run has exactly
// the functions `script` has, as it has only those a published version defines.
function sidebarOn(script) {
  const elements = {};
  const element = id => elements[id] || (elements[id] = {id, innerHTML: '', textContent: '', value: '', hidden: false,
    disabled: false, dataset: {}, listeners: {}, addEventListener(type, listener) { this.listeners[type] = listener; },
    setAttribute() {}, focus() {}});
  const page = {console: {info() {}, log() {}, warn() {}, error: console.error}, Date, Promise, Math, JSON, URL, TextEncoder,
    crypto: globalThis.crypto, Uint8Array, ArrayBuffer, setTimeout, clearTimeout,
    setInterval: () => 0, requestAnimationFrame: run => run(), addEventListener() {},
    document: {getElementById: element, querySelectorAll: () => []},
    google: {script: {run: {withSuccessHandler: ok => ({withFailureHandler: fail => Object.fromEntries(Object.keys(script).map(name =>
      [name, arg => { setTimeout(() => { try { ok(JSON.parse(JSON.stringify(script[name](arg) ?? null))); } catch (error) { fail(error); } }, 0); }]))})}}},
    PrereasonerShell: {reasonBase: 'https://chat.prereasoner.com/reason/'},
    PrereasonerSuggestions: {create: () => ({update() {}, setActive() {}}), merge: () => null},
    __prereasonerLive: {signIn: async () => 'sheet-user'}};
  page.window = page.self = page.globalThis = page;
  vm.createContext(page);
  const publicFile = file => fs.readFileSync(path.join(root, '../web/public', file), 'utf8');
  for (const file of ['vendor/xlsx-0.20.3.full.min.js', 'lib/upload-limits.js', 'lib/number-format.js', 'lib/workbook-import.js',
    'lib/turn-renderer.js', 'office/sheets/sidebar.js']) vm.runInContext(publicFile(file), page);
  const until = async (condition, what) => {
    for (let waited = 0; !condition(); waited += 5) {
      assert(waited < 30000, 'the sidebar never showed ' + what + '; it shows: ' + elements.thread.innerHTML);
      await new Promise(resolve => setTimeout(resolve, 5));
    }
    checks++;
  };
  const ask = question => {
    element('question').value = question;
    element('composer').listeners.submit({preventDefault() {}});
  };
  return {thread: () => elements.thread.innerHTML, until, ask};
}
const scriptOf = addon => Object.fromEntries(Object.keys(addon)
  .filter(name => typeof addon[name] === 'function' && !name.endsWith('_') && !['onInstall', 'onOpen', 'readAll'].includes(name))
  .map(name => [name, addon[name]]));

(async () => {
  // "runner[name] is not a function", on every question of five real workbooks (2026-11-01). The published script refuses
  // to open them; the sidebar, knowing nothing of the script, called a function it no longer has. It now says why.
  const refused = sidebarOn({
    getSidebarContext: () => { throw new Error('Exception: The action is not supported for OBJECT sheet.'); },
    getWorkbookSchema: () => { throw new Error('Exception: The action is not supported for OBJECT sheet.'); },
    getWorkbookPlan: () => { throw new Error('Exception: The action is not supported for OBJECT sheet.'); },
    getWorkbookTab: () => { throw new Error('unexpected'); },
    restorePrereasonerSheetConversation: () => ({conversationId: '', state: null, stale: false}),
    getPrereasonerSuggestions: () => ({questions: []})
  });
  await refused.until(() => refused.thread().includes('sheet-error'), 'why it could not open');
  check(refused.thread().includes('>The action is not supported for OBJECT sheet.<'), 'the reason, without Apps Script\'s prefix');
  refused.ask('total amount');
  await refused.until(() => /answer error/.test(refused.thread()), 'why the question failed');
  check(!refused.thread().includes('runner'), `no missing function reaches the user: ${refused.thread()}`);
  check(refused.thread().includes('The action is not supported for OBJECT sheet.'), 'the question shows the real reason');

  // The script with a chart tab, a tab that fits once trimmed and one that does not: the sidebar reads the others, sends
  // them, says once what it left out and why, and the next question's read does not say it again.
  const stray = [['id', 'amount'], [1, 10]].map((cells, r) => cells.concat(Array(300).fill(''), [r === 0 ? 'stray' : '']));
  const answered = load(book([chartTab(70, 'Chart1'), sheet(71, 'Stray', stray), sheet(72, 'Dense', dense), notes], notes), {
    'https://identitytoolkit.googleapis.com/': [200, {idToken: 'firebase-id-token'}],
    'https://chat.prereasoner.com/api/spreadsheet/conversation/restore': [200, {conversation_id: '', state: null, source_changed: false}],
    'https://chat.prereasoner.com/api/conversation/sync': [200, {conversation_id: 'c_0123456789abcdef0123456789abcdef', changed: true, source_hash: 'a'.repeat(64)}],
    'https://chat.prereasoner.com/api/spreadsheet/conversation/state': [200, {}],
    'https://prereasoner-chat-271377281957.us-central1.run.app/chat': [200, {reply: 'Done.', conversation_id: 'c_0123456789abcdef0123456789abcdef', history: [], traces: []}]
  });
  const opened = sidebarOn(scriptOf(answered));
  const told = 'Not read: “Dense” (it has more than 1,000,000 cells). The other tabs are read.';
  await opened.until(() => opened.thread().includes(told), 'the tab left out');
  check(!opened.thread().includes('sheet-error'), 'one tab left out is no error');
  const uploads = () => answered.fetches.filter(call => call.url.endsWith('/api/conversation/sync'));
  await opened.until(() => uploads().length === 1, 'the upload');
  equal(uploads()[0].body.tables.map(table => table.name), ['Notes', 'Stray'], 'the other tabs are sent');
  check(uploads()[0].body.tables[1].data.includes('stray'), 'with the far cell');
  opened.ask('total amount');
  await opened.until(() => opened.thread().includes('Done.'), 'the answer');
  check(!opened.thread().includes('Not read'), 'told once');

  console.log('Sheets add-on: ' + checks + ' checks passed');
})().catch(error => { console.error(error); process.exit(1); });
