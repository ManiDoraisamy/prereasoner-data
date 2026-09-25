// Prereasoner for Google Sheets. The sidebar is the Prereasoner web workbook itself: Sidebar.html frames
// https://chat.prereasoner.com/embed/sheets (web/public/lib/host-bridge.js), the same page, live
// reasoning and conversation history as chat.prereasoner.com. This script only supplies what that page
// cannot read for itself: the signed-in user's Google token, this spreadsheet's identity, and its cells.
// The page imports the cells with the web upload importer, so the sidebar reads a sheet exactly as an
// upload of the same sheet would. The add-on is read-only: it never writes to the spreadsheet.
var PREREASONER_EMBED_URL = 'https://chat.prereasoner.com/embed/sheets';
var ADDON_NAME = 'Prereasoner';
// The upload's worksheet limits (web/public/lib/upload-limits.js), checked before any cell is read, plus
// a cell cap that bounds what one sidebar call reads and sends.
var GRID_LIMITS = {sheets: 8, rows: 10000, columns: 256, cells: 250000};
// The strings Sheets returns for a cell whose formula failed.
var SHEETS_ERRORS = /^#(?:NULL!|DIV\/0!|VALUE!|REF!|NAME\?|NUM!|N\/A|ERROR!)$/;
var DAY_MS = 86400000;

function onInstall(e) {
  onOpen(e);
}

function onOpen() {
  SpreadsheetApp.getUi()
    .createAddonMenu()
    .addItem('Ask a question', 'showSidebar')
    .addItem('Previous conversations', 'showPreviousConversations')
    .addToUi();
}

function showSidebar() {
  showSidebar_('');
}

// The same sidebar with the conversation history open.
function showPreviousConversations() {
  showSidebar_('conversations');
}

function showSidebar_(view) {
  var template = HtmlService.createTemplateFromFile('Sidebar');
  template.embedUrl = PREREASONER_EMBED_URL + (view ? '?view=' + encodeURIComponent(view) : '');
  SpreadsheetApp.getUi().showSidebar(template.evaluate().setTitle(ADDON_NAME));
}

// Called by Sidebar.html (google.script.run) when the framed page asks for its host context.
function getHostContext() {
  var spreadsheet = activeSpreadsheet_();
  return {
    token: ScriptApp.getOAuthToken(),
    spreadsheetId: spreadsheet.getId(),
    name: spreadsheet.getName(),
    workbook: readGrids_(spreadsheet)
  };
}

// Called before each question, so a changed sheet is noticed.
function getWorkbookGrids() {
  return readGrids_(activeSpreadsheet_());
}

function activeSpreadsheet_() {
  var spreadsheet = SpreadsheetApp.getActiveSpreadsheet();
  if (!spreadsheet) throw new Error('Open a spreadsheet before using Prereasoner.');
  return spreadsheet;
}

// Visible, non-empty tabs (the active one first) as cell grids: values, number formats, failed
// formulas and merged ranges.
function readGrids_(spreadsheet) {
  var timeZone = spreadsheet.getSpreadsheetTimeZone() || 'UTC';
  var active = spreadsheet.getActiveSheet();
  var sheets = spreadsheet.getSheets().filter(function(sheet) {
    return !sheet.isSheetHidden() && sheet.getLastRow() > 1 && sheet.getLastColumn() > 0;
  });
  sheets.sort(function(a, b) {
    if (a.getSheetId() === active.getSheetId()) return -1;
    if (b.getSheetId() === active.getSheetId()) return 1;
    return a.getIndex() - b.getIndex();
  });
  if (!sheets.length) throw new Error('This spreadsheet needs a header row and at least one data row.');
  if (sheets.length > GRID_LIMITS.sheets) {
    throw new Error('Prereasoner can read at most ' + GRID_LIMITS.sheets + ' non-empty tabs at once.');
  }
  var cellTotal = 0;
  var grids = sheets.map(function(sheet) {
    var rowCount = sheet.getLastRow();
    var columnCount = sheet.getLastColumn();
    var tab = 'Sheet "' + sheet.getName() + '": ';
    if (rowCount - 1 > GRID_LIMITS.rows) {
      throw new Error(tab + 'each worksheet may contain at most 10,000 data rows');
    }
    if (columnCount > GRID_LIMITS.columns) throw new Error(tab + 'each worksheet may contain at most 256 columns');
    cellTotal += rowCount * columnCount;
    if (cellTotal > GRID_LIMITS.cells) {
      throw new Error('This spreadsheet is too large to analyze in one request. Reduce the data and try again.');
    }
    var range = sheet.getRange(1, 1, rowCount, columnCount);
    var values = range.getValues();
    return {
      name: sheet.getName(),
      rows: values.map(function(row) {
        return row.map(function(value) { return gridValue_(value, timeZone); });
      }),
      formats: range.getNumberFormats(),
      errors: values.map(function(row) {
        return row.map(function(value) { return typeof value === 'string' && SHEETS_ERRORS.test(value); });
      }),
      merges: range.getMergedRanges().map(function(merged) {
        return {s: {r: merged.getRow() - 1, c: merged.getColumn() - 1},
                e: {r: merged.getLastRow() - 1, c: merged.getLastColumn() - 1}};
      }),
      date1904: false
    };
  });
  return {grids: grids};
}

// google.script.run cannot return Date objects. A date, time or duration cell becomes the serial day
// count Sheets stores for it (days since 1899-12-30 in the spreadsheet's time zone); its number format
// travels with it, so the importer reads it as the upload of the same sheet would.
function gridValue_(value, timeZone) {
  if (Object.prototype.toString.call(value) === '[object Date]') {
    var parts = Utilities.formatDate(value, timeZone, 'yyyy,MM,dd,HH,mm,ss').split(',').map(Number);
    var wallClock = Date.UTC(parts[0], parts[1] - 1, parts[2], parts[3], parts[4], parts[5]);
    return (wallClock - Date.UTC(1899, 11, 30)) / DAY_MS;
  }
  if (typeof value === 'number' && !isFinite(value)) return '';
  return value;
}
