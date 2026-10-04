import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {readWorkbook, readWorkbookSchema} from '../public/office/excel/host.js';

// The task pane posts its cell grids to lib/xlsx-worker.js, the upload importer. The same worker
// runs here (tests/workbook_fixture.js), so these checks exercise the production rule.
const {normalizeGrids} = createRequire(import.meta.url)('../../tests/workbook_fixture.js');

const types = {string: 'string', integer: 'integer', double: 'double', boolean: 'boolean', error: 'error'};
globalThis.Excel = {
  SheetVisibility: {visible: 'Visible', hidden: 'Hidden'},
  RangeValueType: {string: types.string, integer: types.integer, double: types.double,
    boolean: types.boolean, error: types.error}
};
globalThis.Office = {
  onReady: async () => {},
  context: {document: {url: 'https://excel.cloud.microsoft/workbook/one'}}
};

function sheet(name, visibility, grid, options = {}) {
  const rowCount = options.rowCount ?? grid.length;
  const columnCount = options.columnCount ?? Math.max(0, ...grid.map(row => row.length));
  const rowIndex = options.rowIndex ?? 0;
  const columnIndex = options.columnIndex ?? 0;
  return {
    name, visibility, id: name,
    getUsedRangeOrNullObject(valuesOnly){return {...this.getUsedRange(valuesOnly),isNullObject:false};},
    getUsedRange(valuesOnly) {
      assert.equal(valuesOnly, true);
      return {
        rowCount, columnCount, rowIndex, columnIndex,
        address: `${name}!A1`, load() {},
        getMergedAreasOrNullObject() {
          const merges = options.merges || [];
          return {
            isNullObject: merges.length === 0,
            areas: {items: merges.map(([r, c, rows, columns]) => ({
              rowIndex: rowIndex + r, columnIndex: columnIndex + c, rowCount: rows, columnCount: columns
            }))},
            load() {}
          };
        }
      };
    },
    getRangeByIndexes(firstRow, firstColumn, count, requestedColumns) {
      const values = grid.slice(firstRow - rowIndex, firstRow - rowIndex + count)
        .map(row => row.slice(firstColumn - columnIndex, firstColumn - columnIndex + requestedColumns));
      const cell = value => value?.type === 'error' ? value.value : value?.value ?? value;
      const valueTypes = values.map(row => row.map(value => typeof cell(value) === 'boolean' ? types.boolean :
        typeof cell(value) === 'number' ? (Number.isInteger(cell(value)) ? types.integer : types.double) :
        value?.type === 'error' ? types.error : types.string));
      const numberFormat = values.map(row => row.map(value => value?.format || 'General'));
      return {values: values.map(row => row.map(cell)), valueTypes, numberFormat, load() {}};
    }
  };
}

async function runWorkbook(sheets, name = 'Book1', date1904 = false, merges = false) {
  globalThis.Office.context.requirements = {isSetSupported: (set, version) => merges && set === 'ExcelApi' && version === '1.13'};
  globalThis.Excel.run = callback => callback({
    workbook: {
      name, use1904DateSystem: date1904,
      worksheets: {items: sheets, load() {}, getActiveWorksheet(){return {name:sheets[0].name,load(){}};}},
      load() {}
    },
    sync: async () => {}
  });
  return readWorkbook(normalizeGrids);
}

const visible = sheet('Orders', 'Visible', [
  ['id', 'customer', 'company', 'date', 'paid'],
  [101, 'A, Inc', 'TRUE', {value: 45292, format: 'yyyy-mm-dd'}, true],
  [102, 'B "Ltd"', 'FALSE', {value: 45293, format: 'yyyy-mm-dd'}, false]
], {rowIndex: 4, columnIndex: 2});
const hidden = sheet('Hidden', 'Hidden', [['secret'], ['not included']]);
const empty = sheet('Blank', 'Visible', [[''], ['']]);
const result = await runWorkbook([visible, hidden, empty]);
assert.equal(result.name, 'Book1');
assert.equal(result.tables.length, 1);
assert.equal(result.tables[0].name, 'Orders');
assert.equal(result.tables[0].source.kind, 'excel');
assert.equal(result.tables[0].import.headerRow, 1);
assert.equal(result.tables[0].data,
  'id,customer,company,date,paid\n101,"A, Inc",TRUE,2024-01-01,TRUE\n102,"B ""Ltd""",FALSE,2024-01-02,FALSE');

// Dates follow the upload importer in both date systems.
const dateEpoch = await runWorkbook([sheet('Dates', 'Visible', [
  ['date'], [{value: 45292, format: 'yyyy-mm-dd'}]
])]);
assert.equal(dateEpoch.tables[0].data, 'date\n2024-01-01');
const date1904Epoch = await runWorkbook([sheet('Dates', 'Visible', [
  ['date'], [{value: 0, format: 'yyyy-mm-dd'}]
])], 'Book1904', true);
assert.equal(date1904Epoch.tables[0].data, 'date\n1904-01-01');

const elapsedDuration = await runWorkbook([sheet('Durations', 'Visible', [
  ['elapsed'], [{value: 1.1458333333, format: '[h]:mm'}]
])]);
assert.equal(elapsedDuration.tables[0].data, 'elapsed\n1.145833333',
  'elapsed hours remain a day count instead of becoming an 1899 calendar timestamp');

// Only real date/time codes make a date: colours, currencies, conditions, padding and escapes are
// not codes, so these amounts stay numbers.
const formats = await runWorkbook([sheet('Formats', 'Visible', [
  ['red', 'accounting', 'currency', 'condition', 'escaped', 'minutes', 'localeDate'],
  [{value: 1234.5, format: '#,##0.00;[Red]-#,##0.00'},
   {value: 1234.5, format: '_(* #,##0.00_);_(* \\(#,##0.00\\);_(* "-"??_);_(@_)'},
   {value: 1234.5, format: '[$USD] #,##0.00'},
   {value: 150, format: '[Blue][>=100]0;[Magenta]0'},
   {value: 12, format: '0 \\d\\a\\y\\s'},
   {value: 0.0017361111, format: '[mm]:ss'},
   {value: 45292, format: '[$-409]m/d/yyyy'}]
])]);
assert.equal(formats.tables[0].data,
  'red,accounting,currency,condition,escaped,minutes,localeDate\n' +
  '1234.5,1234.5,1234.5,150,12,0.001736111,2024-01-01');

// The same forgiving importer preserves malformed headers and errors across hosts.
const shifted = await runWorkbook([sheet('sales', 'Visible', [
  ['order ID', 'customer', 'city', 'tier', 'ordered', 'currency', 'amount'],
  [1, 101, 'Sherlock Holmes', 'London', 'Gold', 'Magnifying Glass', 'GBP', 118],
  [2, 102, 'Sherlock Holmes', 'London', 'Gold', 'Calabash Pipe', 'GBP', 95]
])]);
assert.match(shifted.tables[0].data, /^Column A,Column B,Column C,Column D,Column E,Column F,Column G,Column H/);
const dupes = await runWorkbook([sheet('Dupes', 'Visible', [['id', 'customer', 'customer'], [1, 'A', 'B']])]);
assert.match(dupes.tables[0].data, /customer \[column B\],customer \[column C\]/);
const errors = await runWorkbook([sheet('Errors', 'Visible', [['id', 'ratio'], [1, {type: 'error', value: '#DIV/0!'}]])]);
assert.match(errors.tables[0].data, /#DIV\/0!/);

// Hosts with ExcelApi 1.13 send merged areas: a merged group header prefixes its columns.
const grouped = await runWorkbook([sheet('Grouped', 'Visible', [
  ['Order', 'Amounts', ''], ['ID', 'Net', 'Tax'], [1, 10, 2]
], {merges: [[0, 1, 1, 2]]})], 'Grouped', false, true);
assert.match(grouped.tables[0].data, /^"?ID"?,Amounts Net,Amounts Tax\n1,10,2$/);

// The upload's worksheet limits hold per tab before any cell is read: two 6,000-row tabs are read, as their
// uploaded .xlsx would be; a tab past 50,000 data rows or 256 columns is refused by name.
const rows = count => [['id', 'amount']].concat(Array.from({length: count}, (_, i) => [i + 1, 1]));
const pair = await runWorkbook([sheet('A', 'Visible', rows(6000)), sheet('B', 'Visible', rows(6000))]);
assert.equal(JSON.stringify(pair.tables.map(table => table.import.dataRows)), '[6000,6000]');   // tables from the worker's realm
await assert.rejects(() => runWorkbook([sheet('Long', 'Visible', [], {rowCount: 50002, columnCount: 2})]),
  /^Error: Sheet "Long": each worksheet may contain at most 50,000 data rows$/);
await assert.rejects(() => runWorkbook([sheet('Wider', 'Visible', [], {rowCount: 2, columnCount: 257})]),
  /^Error: Sheet "Wider": each worksheet may contain at most 256 columns$/);
const tooManyCells = [['id', ...Array.from({length: 255}, (_, i) => `c${i}`)]]
  .concat(Array.from({length: 4000}, (_, i) => [i + 1, ...Array(255).fill(1)]));
await assert.rejects(() => runWorkbook([sheet('Wide', 'Visible', tooManyCells)]),
  /too large to analyze/);
await assert.rejects(() => runWorkbook([sheet('No data', 'Visible', [['header']])]),
  /no visible table/);

await runWorkbook([visible,hidden]);
const metadata=await readWorkbookSchema();
assert.deepEqual(metadata.scope,['Orders']);
assert.equal(metadata.active_sheet,'Orders');
// Hidden tabs' names and headers are not described (they went to the suggestion service, 2026-10-04).
assert.deepEqual(metadata.sheets.map(s=>s.name),['Orders']);
assert(!JSON.stringify(metadata).includes('Hidden'));
assert.deepEqual(metadata.sheets[0].columns,['id','customer','company','date','paid']);
assert(!JSON.stringify(metadata).includes('A, Inc'));
await runWorkbook([sheet('Duplicate','Visible',[['Amount','Amount'],[1,2]])]);
assert.deepEqual((await readWorkbookSchema()).sheets[0].columns,[], 'uncertain headers retain sheet metadata for the normalized merge');
assert.deepEqual(result.sheetNames,['Orders','Hidden','Blank']);
const {merge,header}=createRequire(import.meta.url)('../public/lib/sidebar-suggestions.js');
const raw={sheets:[{name:'Orders',columns:[]},{name:'Hidden',columns:['secret']}],scope:['Orders'],active_sheet:'Orders'};
assert.equal(merge(raw,[]),null,'unknown in-scope headers wait without dropping metadata');
const normalized=[{name:'Orders',data:'Amount [column A],Amount [column B]\n1,2'},
  {name:'Orders summaries',data:'Amount\n3'}];
const merged=merge(raw,normalized);
assert.deepEqual(merged.sheets.map(s=>s.name),['Orders','Hidden','Orders summaries']);
assert.deepEqual(merged.scope,['Orders','Orders summaries']);
assert.deepEqual(merged.sheets[0].columns,['Amount [column A]','Amount [column B]']);
assert.deepEqual(merge({...raw,sheets:[{name:'Orders',columns:['Amount','Amount']},raw.sheets[1]]},normalized),merged,
  'a late raw header read cannot overwrite normalized names');
assert.deepEqual(header('"Column, one","Column\n two"\nPRIVATE,VALUES'),['Column, one','Column\n two']);
console.log('Excel workbook reader and shared metadata: 27 checks passed');
