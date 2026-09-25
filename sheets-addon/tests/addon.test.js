// The Sheets add-on is a host for the web workbook (/embed/sheets): Code.js supplies the Google token,
// the spreadsheet's identity and its cell grids; Sidebar.html frames the page and answers its requests.
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const root = path.resolve(__dirname, '..');
const source = fs.readFileSync(path.join(root, 'Code.js'), 'utf8');
const sidebar = fs.readFileSync(path.join(root, 'Sidebar.html'), 'utf8');
const manifest = JSON.parse(fs.readFileSync(path.join(root, 'appsscript.json'), 'utf8'));

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
function load(spreadsheet) {
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
    }}
  };
  vm.createContext(context);
  vm.runInContext(source, context);
  return context;
}

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
const spreadsheet = {
  getId: () => 'sheet-id-1', getName: () => 'Sales data', getSpreadsheetTimeZone: () => 'UTC',
  getActiveSheet: () => notes, getSheets: () => [orders, notes, hidden, blank]
};
const addon = load(spreadsheet);
const context = addon.getHostContext();
assert.strictEqual(context.token, 'google-access-token');
assert.strictEqual(context.spreadsheetId, 'sheet-id-1');
assert.strictEqual(context.name, 'Sales data');
const grids = context.workbook.grids;
assert.deepStrictEqual(grids.map(grid => grid.name), ['Notes', 'Orders'], 'the active tab first; hidden and header-only tabs skipped');
const grid = grids[1];
assert.strictEqual(grid.rows[1][1], 45292, '2024-01-01 is serial 45292');
assert(Math.abs(grid.rows[1][2] - (1 + 3.5 / 24)) < 1e-9, 'a 27:30 duration is 1.1458 days');
assert.strictEqual(grid.formats[1][2], '[h]:mm');
assert.deepStrictEqual(JSON.parse(JSON.stringify(grid.errors[1])), [false, false, false, true, false, false]);
assert.deepStrictEqual(JSON.parse(JSON.stringify(grid.merges)), [{s: {r: 0, c: 4}, e: {r: 0, c: 5}}]);
assert.strictEqual(grid.date1904, false);
assert.deepStrictEqual(JSON.parse(JSON.stringify(addon.getWorkbookGrids().grids[0].rows)), [['note'], ['keep']]);

// The page imports these grids with the web upload importer (web/public/lib/xlsx-worker.js): Sheets'
// serial days and formats read back as the date and the duration, and a failed formula is refused.
const {normalizeGrids} = require('../../tests/workbook_fixture.js');
const clean = load({getId: () => 'x', getName: () => 'x', getSpreadsheetTimeZone: () => 'UTC',
  getActiveSheet: () => orders, getSheets: () => [sheet(6, 'Hours', [
    ['order ID', 'placed', 'worked'], [101, new Date(Date.UTC(2024, 0, 1)), new Date(Date.UTC(1899, 11, 31, 3, 30))]
  ], {formats: [['General', 'General', 'General'], ['0', 'yyyy-mm-dd', '[h]:mm']]})]}).getHostContext();
assert.strictEqual(normalizeGrids(clean.workbook.grids)[0].csv, 'order ID,placed,worked\n101,2024-01-01,1.145833333');
assert.throws(() => normalizeGrids(grids), /formula error|No unambiguous header/);

// Apps Script cannot load web/public/lib/upload-limits.js, so the add-on's copy is pinned to it here.
const shared = {};
vm.runInNewContext(fs.readFileSync(path.join(root, '../web/public/lib/upload-limits.js'), 'utf8'), shared);
for (const key of ['sheets', 'rows', 'columns']) assert.strictEqual(addon.GRID_LIMITS[key], shared.UPLOAD_LIMITS[key], key);

// The upload's worksheet limits hold per tab before any cell is read: two 6,000-row tabs are read, as an
// upload of them would be; a tab past 10,000 data rows or 256 columns is refused by name.
const rows = count => [['id', 'amount']].concat(Array.from({length: count}, (_, i) => [i + 1, 1]));
const book = tabs => load({getId: () => 'x', getName: () => 'x', getSpreadsheetTimeZone: () => 'UTC',
  getActiveSheet: () => tabs[0], getSheets: () => tabs});
assert.strictEqual(book([sheet(7, 'A', rows(6000)), sheet(8, 'B', rows(6000))]).getHostContext().workbook.grids.length, 2);
assert.throws(() => book([sheet(9, 'Long', rows(10001))]).getHostContext(),
  /^Error: Sheet "Long": each worksheet may contain at most 10,000 data rows$/);
const wide = sheet(5, 'Wide', [Array.from({length: 257}, (_, i) => 'c' + i), Array.from({length: 257}, () => 1)]);
assert.throws(() => book([wide]).getHostContext(), /^Error: Sheet "Wide": each worksheet may contain at most 256 columns$/);
const dense = [Array.from({length: 26}, (_, i) => 'c' + i)].concat(Array.from({length: 10000}, () => Array(26).fill(1)));
assert.throws(() => book([sheet(10, 'Dense', dense)]).getHostContext(), /too large to analyze in one request/);   // 260,026 cells
assert.throws(() => load({getId: () => 'x', getName: () => 'x', getSpreadsheetTimeZone: () => 'UTC',
  getActiveSheet: () => blank, getSheets: () => [blank]}).getHostContext(), /header row and at least one data row/);

// The sidebar frames the web workbook and answers only that frame, from its origin.
assert(sidebar.includes('src="<?= embedUrl ?>"'));
assert(sidebar.includes("var ORIGIN = 'https://chat.prereasoner.com';"));
assert(sidebar.includes('event.origin !== ORIGIN || event.source !== frame.contentWindow'));
assert(sidebar.includes("SERVER = {context: 'getHostContext', grids: 'getWorkbookGrids'}"));
assert(sidebar.includes('postMessage({prereasoner: 1, id: id, type: type, payload: payload}, ORIGIN)'));
assert(source.includes("PREREASONER_EMBED_URL = 'https://chat.prereasoner.com/embed/sheets'"));
assert(source.includes("addItem('Ask a question', 'showSidebar')"));
assert(source.includes("addItem('Previous conversations', 'showPreviousConversations')"));
assert(source.includes(".setTitle(ADDON_NAME)"));

// Read-only and minimal: no URL fetching, no storage, no write scope.
assert(!/UrlFetchApp|PropertiesService|setValue|setValues/.test(source));
assert.strictEqual(manifest.urlFetchWhitelist, undefined);
assert.deepStrictEqual(manifest.oauthScopes.slice().sort(), [
  'https://www.googleapis.com/auth/script.container.ui',
  'https://www.googleapis.com/auth/spreadsheets.currentonly',
  'https://www.googleapis.com/auth/userinfo.email',
  'https://www.googleapis.com/auth/userinfo.profile',
  'openid'
]);
assert.strictEqual(manifest.dependencies, undefined);

console.log('Sheets add-on: 29 checks passed');
