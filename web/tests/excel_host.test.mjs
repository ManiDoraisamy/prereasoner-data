import assert from 'node:assert/strict';
import '../public/lib/number-format.js';
import {readWorkbook} from '../public/office/excel/host.js';

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
  return {
    name, visibility, id: name,
    getUsedRange(valuesOnly) {
      assert.equal(valuesOnly, true);
      return {
        rowCount, columnCount, rowIndex: options.rowIndex ?? 0, columnIndex: options.columnIndex ?? 0,
        address: `${name}!A1`, load() {}
      };
    },
    getRangeByIndexes(rowIndex, columnIndex, count, requestedColumns) {
      const values = grid.slice(rowIndex - (options.rowIndex ?? 0), rowIndex - (options.rowIndex ?? 0) + count)
        .map(row => row.slice(columnIndex - (options.columnIndex ?? 0), columnIndex - (options.columnIndex ?? 0) + requestedColumns));
      const cell = value => value?.type === 'error' ? value : value?.value ?? value;
      const valueTypes = values.map(row => row.map(value => typeof cell(value) === 'boolean' ? types.boolean :
        typeof cell(value) === 'number' ? (Number.isInteger(cell(value)) ? types.integer : types.double) :
        value?.type === 'error' ? types.error : types.string));
      const numberFormat = values.map(row => row.map(value => value?.format || 'General'));
      return {values: values.map(row => row.map(cell)), valueTypes, numberFormat, load() {}};
    }
  };
}

async function runWorkbook(sheets, name = 'Book1', date1904 = false) {
  globalThis.Excel.run = callback => callback({
    workbook: {
      name, use1904DateSystem: date1904,
      worksheets: {items: sheets, load() {}},
      load() {}
    },
    sync: async () => {}
  });
  return readWorkbook();
}

const visible = sheet('Orders', 'Visible', [
  ['id', 'customer', 'customer', 'date'],
  [101, 'A, Inc', 'TRUE', {value: 45292, format: 'yyyy-mm-dd'}],
  [102, 'B "Ltd"', 'FALSE', {value: 45293, format: 'yyyy-mm-dd'}]
], {rowIndex: 4, columnIndex: 2});
const hidden = sheet('Hidden', 'Hidden', [['secret'], ['not included']]);
const empty = sheet('Blank', 'Visible', [[''], ['']]);
const result = await runWorkbook([visible, hidden, empty]);
assert.equal(result.name, 'Book1');
assert.equal(result.tables.length, 1);
assert.equal(result.tables[0].name, 'Orders');
assert.equal(result.tables[0].source.kind, 'excel');
assert.equal(result.tables[0].data,
  'id,customer,customer_2,date\n101,"A, Inc",TRUE,2024-01-01T00:00:00Z\n102,"B ""Ltd""",FALSE,2024-01-02T00:00:00Z');

const dateEpoch = await runWorkbook([sheet('Dates', 'Visible', [
  ['date'], [{value: 0, format: 'yyyy-mm-dd'}]
])]);
assert.equal(dateEpoch.tables[0].data, 'date\n1899-12-30T00:00:00Z');
const date1904Epoch = await runWorkbook([sheet('Dates', 'Visible', [
  ['date'], [{value: 0, format: 'yyyy-mm-dd'}]
])], 'Book1904', true);
assert.equal(date1904Epoch.tables[0].data, 'date\n1904-01-01T00:00:00Z');

const elapsedDuration = await runWorkbook([sheet('Durations', 'Visible', [
  ['elapsed'], [{value: 1.1458333333, format: '[h]:mm'}]
])]);
assert.equal(elapsedDuration.tables[0].data, 'elapsed\n1.1458333333',
  'elapsed hours remain numeric instead of becoming an 1899 calendar timestamp');

// Only real date/time codes make a date. `#,##0.00;[Red]-#,##0.00` once turned amounts into
// dates through the "d" of [Red]; colours, currencies, conditions, padding and escapes are not codes.
const formats = await runWorkbook([sheet('Formats', 'Visible', [
  ['red', 'accounting', 'currency', 'condition', 'escaped', 'minutes', 'localeDate', 'clock'],
  [{value: 1234.5, format: '#,##0.00;[Red]-#,##0.00'},
   {value: 1234.5, format: '_(* #,##0.00_);_(* \\(#,##0.00\\);_(* "-"??_);_(@_)'},
   {value: 1234.5, format: '[$USD] #,##0.00'},
   {value: 150, format: '[Blue][>=100]0;[Magenta]0'},
   {value: 12, format: '0 \\d\\a\\y\\s'},
   {value: 0.0017361111, format: '[mm]:ss'},
   {value: 45292, format: '[$-409]m/d/yyyy'},
   {value: 0.5625, format: 'h:mm AM/PM'}]
])]);
assert.equal(formats.tables[0].data,
  'red,accounting,currency,condition,escaped,minutes,localeDate,clock\n' +
  '1234.5,1234.5,1234.5,150,12,0.0017361111,2024-01-01T00:00:00Z,1899-12-30T13:30:00Z');

await assert.rejects(() => runWorkbook([sheet('Wide', 'Visible', [], {rowCount: 1000, columnCount: 256})]),
  /too large to analyze/);
await assert.rejects(() => runWorkbook([sheet('No data', 'Visible', [['header']])]),
  /no visible table/);

console.log('Excel workbook reader: 5 checks passed');
