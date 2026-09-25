import '../../lib/upload-limits.js';

// The upload's worksheet limits hold per tab before any cell is read; the cell cap bounds what one
// task-pane read requests from Excel.
const {sheets: MAX_SHEETS, rows: MAX_ROWS, columns: MAX_COLUMNS} = globalThis.UPLOAD_LIMITS;
const MAX_CELLS = 250000;
const VALUE_CHUNK_ROWS = 200;
const MAX_TABLE_BYTES = 2 * 1024 * 1024;
const MAX_TOTAL_BYTES = 6 * 1024 * 1024;

// The workbook upload's importer decides headers, dates, durations, merged cells, totals and errors
// (lib/workbook-import.js through lib/xlsx-worker.js). The task pane reads the cells and hands the
// grids to that same worker, so an Excel workbook and its uploaded .xlsx become the same tables.
function normalizeInWorker(grids) {
  return new Promise((resolve, reject) => {
    const worker = new Worker(new URL('../../lib/xlsx-worker.js', import.meta.url));
    worker.onmessage = event => {
      worker.terminate();
      if (event.data && event.data.ok) resolve(event.data.sheets);
      else reject(new Error((event.data && event.data.error) || 'The workbook could not be read.'));
    };
    worker.onerror = event => {
      worker.terminate();
      reject(new Error(event.message || 'The workbook could not be read.'));
    };
    worker.postMessage({grids});
  });
}

// Merged areas need ExcelApi 1.13; older hosts send no merge information, so a merged area arrives
// as its value plus blank cells, exactly as a merged range reads without formatting.
function mergesSupported() {
  const requirements = Office.context && Office.context.requirements;
  return Boolean(requirements && requirements.isSetSupported('ExcelApi', '1.13'));
}

function cellValue(value, type) {
  if (value == null) return null;
  if (typeof value === 'number' && !Number.isFinite(value)) return null;
  if (type === Excel.RangeValueType.boolean) return Boolean(value);
  return value;
}

export async function readWorkbook(normalize = normalizeInWorker) {
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
    let cellTotal = 0;
    const candidates = targets.filter(({used}) => used.rowCount > 1 && used.columnCount > 0);
    if (candidates.length > MAX_SHEETS) throw new Error(`This workbook has more than ${MAX_SHEETS} visible tabs with data.`);
    for (const {sheet, used} of candidates) {
      if (used.rowCount - 1 > MAX_ROWS) {
        throw new Error(`Sheet "${sheet.name}": each worksheet may contain at most ${MAX_ROWS.toLocaleString('en-US')} data rows`);
      }
      if (used.columnCount > MAX_COLUMNS) {
        throw new Error(`Sheet "${sheet.name}": each worksheet may contain at most ${MAX_COLUMNS} columns`);
      }
      cellTotal += used.rowCount * used.columnCount;
      if (cellTotal > MAX_CELLS) {
        throw new Error('This workbook is too large to analyze in one request. Reduce its used range and try again.');
      }
    }
    const date1904 = Boolean(workbook.use1904DateSystem);
    const readMerges = mergesSupported();
    const grids = [];
    for (const {sheet, used} of candidates) {
      // Fetch bounded chunks so a wide worksheet cannot exceed Excel on the web's
      // request/response limit just because several Office.js matrices are returned together.
      const rows = [];
      const formats = [];
      const errors = [];
      let hasValues = false;
      for (let offset = 0; offset < used.rowCount; offset += VALUE_CHUNK_ROWS) {
        const count = Math.min(VALUE_CHUNK_ROWS, used.rowCount - offset);
        const range = sheet.getRangeByIndexes(used.rowIndex + offset, used.columnIndex, count, used.columnCount);
        range.load('values,valueTypes,numberFormat');
        await context.sync();
        range.values.forEach((row, r) => {
          const types = range.valueTypes?.[r] || [];
          rows.push(row.map((value, c) => {
            if (value !== null && value !== '') hasValues = true;
            return cellValue(value, types[c]);
          }));
          formats.push(range.numberFormat?.[r] || []);
          errors.push(row.map((_, c) => types[c] === Excel.RangeValueType.error));
        });
      }
      if (!hasValues) continue;
      const merges = [];
      if (readMerges) {
        const areas = used.getMergedAreasOrNullObject();
        areas.load('isNullObject,areas/items/rowIndex,areas/items/columnIndex,areas/items/rowCount,areas/items/columnCount');
        await context.sync();
        if (!areas.isNullObject) {
          for (const area of areas.areas.items) {
            const r = area.rowIndex - used.rowIndex;
            const c = area.columnIndex - used.columnIndex;
            merges.push({s: {r, c}, e: {r: r + area.rowCount - 1, c: c + area.columnCount - 1}});
          }
        }
      }
      grids.push({name: sheet.name, rows, formats, errors, merges, date1904});
    }
    return {grids, name: workbook.name || 'Excel workbook'};
  });
  const sheets = collected.grids.length ? await normalize(collected.grids) : [];
  let byteTotal = 0;
  const tables = sheets.map(sheet => {
    const bytes = new TextEncoder().encode(sheet.csv).byteLength;
    if (bytes > MAX_TABLE_BYTES) throw new Error(`The tab “${sheet.name}” is larger than 2 MB.`);
    byteTotal += bytes;
    if (byteTotal > MAX_TOTAL_BYTES) throw new Error('Combined workbook data is larger than 6 MB.');
    return {name: sheet.name, data: sheet.csv, import: sheet.import, source: {kind: 'excel'}};
  });
  if (!tables.length) throw new Error('This workbook has no visible table with a header and data rows.');
  return {tables, name: collected.name};
}

export async function workbookKey() {
  const url = String(Office.context.document.url || '').trim();
  if (!url) return `excel_${crypto.randomUUID().replaceAll('-', '')}`;
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(url));
  const hex = Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('');
  return `excel_${hex}`;
}
