const MAX_SHEETS = 8;
const MAX_ROWS = 10000;
const MAX_COLUMNS = 256;
const MAX_CELLS = 250000;
const VALUE_CHUNK_ROWS = 200;
const MAX_TABLE_BYTES = 2 * 1024 * 1024;
const MAX_TOTAL_BYTES = 6 * 1024 * 1024;

function csvCell(value) {
  const text = value == null ? '' : String(value);
  return /[",\r\n]/.test(text) ? '"' + text.replace(/"/g, '""') + '"' : text;
}

function headers(values) {
  const seen = new Map();
  return values.map((v, i) => {
    const label = String(v == null ? '' : v).trim() || `column_${i + 1}`;
    const count = (seen.get(label.toLowerCase()) || 0) + 1;
    seen.set(label.toLowerCase(), count);
    return count === 1 ? label : `${label}_${count}`;
  });
}

function dateSerialToIso(value, date1904) {
  const whole = Math.floor(value);
  const milliseconds = Math.round((value - whole) * 86400000);
  const base = date1904 ? Date.UTC(1904, 0, 1) : Date.UTC(1899, 11, 30);
  // Excel serial 0 is 1899-12-30; only serials 1–59 need shifting around
  // Excel's fictitious 1900-02-29.
  const adjusted = whole + (!date1904 && value >= 1 && value < 60 ? 1 : 0);
  const date = new Date(base + adjusted * 86400000 + milliseconds);
  return Number.isNaN(date.getTime()) ? String(value) : date.toISOString().replace(/\.000Z$/, 'Z');
}

function cellValue(value, type, numberFormat, date1904) {
  if (value == null) return '';
  if (typeof value === 'number' && !Number.isFinite(value)) return '';
  if (type === Excel.RangeValueType.error) return String(value);
  if (typeof value === 'number') {
    const format = String(numberFormat || '').replace(/"[^"]*"|\\./g, '').toLowerCase();
    if (/[ydhms]/.test(format) && /[ymd]/.test(format)) return dateSerialToIso(value, date1904);
    return String(value);
  }
  if (type === Excel.RangeValueType.boolean) return value ? 'TRUE' : 'FALSE';
  if (type === Excel.RangeValueType.string) return String(value);
  if (type === Excel.RangeValueType.empty) return '';
  if (type === Excel.RangeValueType.integer && typeof value === 'number') return String(value);
  if (type === Excel.RangeValueType.unknown && typeof value === 'number') return String(value);
  if (type === Excel.RangeValueType.formula && typeof value === 'number') return String(value);
  // Excel API marks date-formatted cells as `double` in some builds. The number format
  // identifies date/time sections without relying on the user's display locale.
  return value instanceof Date ? value.toISOString() : String(value);
}

export async function readWorkbook() {
  await Office.onReady();
  const collected = await Excel.run(async context => {
    const workbook = context.workbook;
    const worksheets = workbook.worksheets;
    worksheets.load('items/name,items/visibility,items/id');
    workbook.load('use1904DateSystem,name');
    await context.sync();
    const visible = worksheets.items.filter(sheet => sheet.visibility === Excel.SheetVisibility.visible);
    const targets = [];
    for (const sheet of visible) {
      const used = sheet.getUsedRange(true);
      used.load('address,rowCount,columnCount,rowIndex,columnIndex');
      targets.push({sheet, used});
    }
    await context.sync();
    let rowTotal = 0;
    let cellTotal = 0;
    const candidates = targets.filter(({used}) => used.rowCount > 1 && used.columnCount > 0);
    if (candidates.length > MAX_SHEETS) throw new Error(`This workbook has more than ${MAX_SHEETS} visible tabs with data.`);
    for (const {used} of candidates) {
      rowTotal += used.rowCount - 1;
      cellTotal += used.rowCount * used.columnCount;
      if (rowTotal > MAX_ROWS || used.columnCount > MAX_COLUMNS || cellTotal > MAX_CELLS) {
        throw new Error('This workbook is too large to analyze in one request. Reduce its used range and try again.');
      }
    }
    let byteTotal = 0;
    const tables = [];
    for (const {sheet, used} of candidates) {
      // Fetch bounded chunks so a wide worksheet cannot exceed Excel on the web's
      // request/response limit just because several Office.js matrices are returned together.
      const lines = [];
      let tableBytes = 0;
      let hasValues = false;
      for (let offset = 0; offset < used.rowCount; offset += VALUE_CHUNK_ROWS) {
        const count = Math.min(VALUE_CHUNK_ROWS, used.rowCount - offset);
        const range = sheet.getRangeByIndexes(used.rowIndex + offset, used.columnIndex, count, used.columnCount);
        range.load('values,valueTypes,numberFormat');
        await context.sync();
        const rows = range.values.map((row, r) => row.map((value, c) => {
          if (value !== null && value !== '') hasValues = true;
          return cellValue(value, range.valueTypes?.[r]?.[c], range.numberFormat?.[r]?.[c], Boolean(workbook.use1904DateSystem));
        }));
        if (offset === 0) rows[0] = headers(rows[0]);
        const chunk = rows.map(row => row.map(csvCell).join(',')).join('\n');
        tableBytes += new TextEncoder().encode(chunk).byteLength + (lines.length ? 1 : 0);
        if (tableBytes > MAX_TABLE_BYTES) throw new Error(`The tab “${sheet.name}” is larger than 2 MB.`);
        lines.push(chunk);
      }
      if (!hasValues) continue;
      byteTotal += tableBytes;
      if (byteTotal > MAX_TOTAL_BYTES) throw new Error('Combined workbook data is larger than 6 MB.');
      tables.push({name: sheet.name, data: lines.join('\n'), source: {kind: 'excel'}});
    }
    if (!tables.length) throw new Error('This workbook has no visible table with a header and data rows.');
    return {tables, name: workbook.name || 'Excel workbook'};
  });
  return collected;
}

export async function workbookKey() {
  const url = String(Office.context.document.url || '').trim();
  if (!url) return `excel_${crypto.randomUUID().replaceAll('-', '')}`;
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(url));
  const hex = Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('');
  return `excel_${hex}`;
}
