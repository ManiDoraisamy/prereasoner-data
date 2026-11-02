// Prereasoner for Google Sheets. The sidebar (Sidebar.html) is the add-on's own UI in Sheets' style. It
// renders each turn with the web rail's shared component (web/public/lib/turn-renderer.js), follows the
// live trace through the shared Firebase module (web/public/lib/firebase-init.js), and reads the sheet
// with the web upload importer (web/public/lib/workbook-import.js), all loaded from chat.prereasoner.com.
// This server supplies what the sidebar cannot do from the Apps Script sandbox: the spreadsheet's cells,
// and authenticated calls to Prereasoner, which accepts browser requests only from its own origins.
// The add-on is read-only: it never writes to the spreadsheet.
//
// Apps Script calls Cloud Run directly so requests can use the service's 300-second timeout.
// Firebase Hosting's rewrite can return a gateway timeout before a cold LLM presentation finishes.
var PREREASONER_CHAT_URL = 'https://prereasoner-chat-271377281957.us-central1.run.app/chat';
var PREREASONER_REASON_URL = 'https://chat.prereasoner.com/reason/';
var PREREASONER_API_URL = 'https://chat.prereasoner.com';
var ADDON_NAME = 'Prereasoner';
var FIREBASE_API_KEY = 'AIzaSyAC_Kiqj3lqd52ufpqYDAO17G6T7wfBd9Q';
var FIREBASE_TOKEN_URL = 'https://identitytoolkit.googleapis.com/v1/accounts:signInWithIdp?key=' + FIREBASE_API_KEY;
// The upload's worksheet limits (web/public/lib/upload-limits.js; tests pin this copy to it), checked
// before any cell is read, plus a cell cap that bounds what one sidebar call reads and sends.
var GRID_LIMITS = {sheets: 8, rows: 50000, columns: 256, cells: 1000000};
// The most one execution reads of a tab past those limits to find out whether what it holds fits them.
var TRIM_READ_CELLS = 2000000;
var REQUEST_LIMITS = {questionChars: 20000, historyItems: 24, historyChars: 80000};
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
  var template = HtmlService.createTemplateFromFile('Sidebar');
  template.reasonBase = JSON.stringify(PREREASONER_REASON_URL);
  SpreadsheetApp.getUi().showSidebar(template.evaluate().setTitle(ADDON_NAME));
}

function showPreviousConversations() {
  var template = HtmlService.createTemplateFromFile('Previous');
  var data;
  try {
    data = listPreviousConversations();
  } catch (error) {
    data = {error: errorMessage_(error), conversations: []};
  }
  template.initialData = JSON.stringify(data).replace(/<\//g, '<\\/');
  template.reasonBase = JSON.stringify(PREREASONER_REASON_URL);
  var html = template.evaluate().setTitle('Previous conversations');
  SpreadsheetApp.getUi().showModalDialog(html, 'Previous conversations');
}

function listPreviousConversations() {
  var body = apiRequest_('get', '/api/conversations?limit=50', null, 'Prereasoner could not load previous conversations.');
  return {
    conversations: (Array.isArray(body.conversations) ? body.conversations : []).filter(function(item) {
      return item && /^c_[0-9a-f]{32}$/i.test(String(item.id || ''));
    }).slice(0, 50)
  };
}

// Called once when the sidebar opens: the Google token (the sidebar signs in to Firebase with it for
// the live trace), this spreadsheet's identity, and the tabs to read. `uploadOnce` tells the hosted sidebar that
// this script uploads a sheet once and asks by its hash (the sidebar deploys before a script version is
// published, and keeps the older calls for an older script). The cells come tab by tab (getWorkbookTab): read here,
// in one execution, a six-tab workbook held up the token and the first question for 1:50 (2026-10-30).
function getSidebarContext() {
  var spreadsheet = activeSpreadsheet_();
  var plan = workbookPlan_(spreadsheet);
  return {
    token: ScriptApp.getOAuthToken(),
    spreadsheetId: spreadsheet.getId(),
    name: spreadsheet.getName(),
    activeSheet: spreadsheet.getActiveSheet().getName(),
    tabs: plan.tabs,
    scope: plan.scope,
    availableTabs: plan.availableTabs,
    leftOut: plan.leftOut,
    uploadOnce: true
  };
}

// Fast metadata lane; the full cell read and conversation restoration run independently.
function getWorkbookSchema(request) {
  var spreadsheet = activeSpreadsheet_();
  var active = spreadsheet.getActiveSheet().getName();
  var visible = dataTabs_(spreadsheet);
  if (!visible.length || visible.length > 64) throw new Error('Schema suggestions unavailable for this workbook.');
  // Visible, populated tabs only (the privacy page's promise): hidden tabs' names and headers went to
  // the suggestion service when the sidebar opened (2026-10-04).
  var schemas = visible.map(function(sheet) {
    var width = Math.min(sheet.getLastColumn(), GRID_LIMITS.columns);
    var headers = width ? sheet.getRange(1, 1, 1, width).getValues()[0] : [];
    var reliable = headers.length && headers.every(function(value) { return typeof value === 'string' && value.trim(); });
    if (reliable && headers.some(function(label) {
      return headers.filter(function(other) { return other.trim().toLowerCase() === label.trim().toLowerCase(); }).length > 1;
    })) reliable = false;
    return {name: sheet.getName(), columns: reliable ? headers.map(function(value) { return value.trim(); }) : []};
  });
  var scope = request && request.scope === 'active' ? [active] : withinOneRead_(activeFirst_(visible, active)).tabs;
  // Uncertain headers wait for the shared importer's positional names, without guessing.
  return {sheets: schemas, active_sheet: active, scope: scope};
}

function getPrereasonerSuggestions(schema) {
  // Whitelist before sending to the service. The service validates nested keys and budget too.
  if (!schema || Object.keys(schema).some(function(key) { return ['sheets', 'active_sheet', 'scope'].indexOf(key) < 0; })) {
    throw new Error('Suggestions accept schema metadata only.');
  }
  return fetchJson_(PREREASONER_CHAT_URL + '/suggestions', 'post', schema, 'Suggested questions are unavailable.');
}

// Called before each question, so a changed sheet is noticed: which tabs to read, then each one (the sidebar
// reads several at once, each in its own execution).
function getWorkbookPlan(request) {
  return workbookPlan_(activeSpreadsheet_(), request && request.scope);
}

function getWorkbookTab(request) {
  var spreadsheet = activeSpreadsheet_();
  var name = String((request && request.name) || '');
  var sheet = dataTabs_(spreadsheet).filter(function(candidate) { return candidate.getName() === name; })[0];
  if (!sheet) throw new Error('This spreadsheet changed while it was being read. Try again.');
  var timeZone = spreadsheet.getSpreadsheetTimeZone() || 'UTC';
  return overLimit_(sheet.getLastRow(), sheet.getLastColumn()) ? readTrimmed_(sheet, timeZone) : readTab_(sheet, timeZone);
}

// The sheet's conversation and saved sidebar. The cells are not sent: the sidebar compares the sheet's
// fingerprint with the saved one, and uploads the sheet only when it changed (syncPrereasonerConversation).
function restorePrereasonerSheetConversation() {
  var body = apiRequest_('post', '/api/spreadsheet/conversation/restore',
    {spreadsheet_id: activeSpreadsheet_().getId(), host: 'sheets'},
    'Prereasoner could not restore this sheet’s conversation.');
  return {
    conversationId: body.conversation_id || '',
    state: body.state && typeof body.state === 'object' ? body.state : null,
    stale: !!body.source_changed,
    sourceHash: body.source_hash || ''
  };
}

function savePrereasonerSheetConversation(request) {
  request = request || {};
  var conversationId = conversationId_(request.conversationId);
  if (!conversationId) throw new Error('The conversation expired. Start a new conversation and try again.');
  if (!request.state || typeof request.state !== 'object' || Array.isArray(request.state)) {
    throw new Error('The sidebar conversation could not be saved.');
  }
  return apiRequest_('post', '/api/spreadsheet/conversation/state',
    {spreadsheet_id: activeSpreadsheet_().getId(), host: 'sheets', conversation_id: conversationId, state: request.state},
    'Prereasoner could not save this sheet’s conversation.');
}

function clearPrereasonerSheetConversation() {
  return apiRequest_('post', '/api/spreadsheet/conversation/clear',
    {spreadsheet_id: activeSpreadsheet_().getId(), host: 'sheets'},
    'Prereasoner could not start a new conversation.');
}

// The one upload of the sheet: before the first question (it starts the conversation) and whenever the sheet
// changed since the conversation last saw it (that marks its earlier answers stale). Questions then name the
// stored sheet by its hash instead of sending every cell again (2026-10-02).
function syncPrereasonerConversation(request) {
  request = request || {};
  var conversationId = request.conversationId ? conversationId_(request.conversationId) : '';
  if (request.conversationId && !conversationId) {
    throw new Error('The conversation expired. Start a new conversation and try again.');
  }
  var body = apiRequest_('post', '/api/conversation/sync',
    {id: conversationId, question: String(request.question || '').slice(0, REQUEST_LIMITS.questionChars),
     tables: requestTables_(request)},
    'Prereasoner could not sync this spreadsheet.');
  return {changed: !!body.changed, conversationId: body.conversation_id || conversationId,
          sourceHash: body.source_hash || ''};
}

function askPrereasoner(request) {
  request = request || {};
  var question = String(request.question || '').trim();
  if (!question) throw new Error('Enter a question about this spreadsheet.');
  if (question.length > REQUEST_LIMITS.questionChars) {
    throw new Error('Questions must be ' + REQUEST_LIMITS.questionChars + ' characters or fewer.');
  }
  var sourceHash = String(request.sourceHash || '');
  if (!/^[0-9a-f]{64}$/.test(sourceHash)) throw new Error('Send the sheet to Prereasoner before asking.');
  var payload = {
    message: question,
    history: normalizeHistory_(request.history),
    conversation_id: conversationId_(request.conversationId) || null,
    source_hash: sourceHash,
    active_sheet: activeSpreadsheet_().getActiveSheet().getName()
  };
  if (!payload.conversation_id) {
    throw new Error('The conversation expired. Start a new conversation and try again.');
  }
  var turnId = request.turnId == null ? '' : String(request.turnId).trim();
  if (turnId) {
    if (!/^[A-Za-z0-9_-]{1,128}$/.test(turnId)) throw new Error('The live request id is invalid.');
    payload.turnId = turnId;
  }
  var raw = chatRequest_(payload, [409]);
  // The stored sheet was replaced since (another sidebar synced it): the sidebar uploads it again and asks once more.
  if (raw.status === 409) return {sourceChanged: true};
  return {
    reply: String(raw.reply || '').trim(),
    conversationId: raw.conversation_id || null,
    history: normalizeHistory_(raw.history),
    traces: traceSummary_(raw)
  };
}

function activeSpreadsheet_() {
  var spreadsheet = SpreadsheetApp.getActiveSpreadsheet();
  if (!spreadsheet) throw new Error('Open a spreadsheet before using Prereasoner.');
  return spreadsheet;
}

function conversationId_(value) {
  var id = value == null ? '' : String(value);
  return /^c_[0-9a-f]{32}$/i.test(id) ? id : '';
}

// Tables the sidebar made with the upload importer: [{name, data, source}], within the request limits.
function requestTables_(request) {
  var tables = request && request.tables;
  if (!Array.isArray(tables) || !tables.length || tables.length > GRID_LIMITS.sheets) {
    throw new Error('The spreadsheet could not be read. Close and reopen Prereasoner.');
  }
  return tables.map(function(table) {
    if (!table || typeof table.name !== 'string' || typeof table.data !== 'string') {
      throw new Error('The spreadsheet could not be read. Close and reopen Prereasoner.');
    }
    return {name: table.name, data: table.data, source: Object.assign({}, table.source || {}, {kind: 'google-sheets-addon'})};
  });
}

// The tabs that hold cells to read: visible, non-empty grids. A chart tab has no cells, and refuses even being asked
// how long it is ("The action is not supported for OBJECT sheet"): four real workbooks failed to open over one
// (2026-11-01). The type comes first, so nothing else is asked of it.
function dataTabs_(spreadsheet) {
  return spreadsheet.getSheets().filter(function(sheet) {
    return String(sheet.getType()) === 'GRID' && !sheet.isSheetHidden() && sheet.getLastRow() > 1 && sheet.getLastColumn() > 0;
  });
}

function activeFirst_(sheets, activeName) {
  return sheets.slice().sort(function(a, b) {
    if (a.getName() === activeName) return -1;
    if (b.getName() === activeName) return 1;
    return a.getIndex() - b.getIndex();
  });
}

// The tabs to read, the active one first, as many as one read holds, and why it leaves out any other.
function workbookPlan_(spreadsheet, requestedScope) {
  var scope = requestedScope || 'auto';
  if (['auto', 'active', 'all'].indexOf(scope) < 0) throw new Error('Choose the active sheet or all tabs.');
  var active = spreadsheet.getActiveSheet().getName();
  var sheets = activeFirst_(dataTabs_(spreadsheet), active);
  if (!sheets.length) throw new Error('This spreadsheet needs a header row and at least one data row.');
  var available = sheets.map(function(sheet) { return sheet.getName(); });
  if (scope === 'auto') scope = 'all';
  var wanted = scope === 'active' ? sheets.filter(function(sheet) { return sheet.getName() === active; }) : sheets;
  if (!wanted.length) throw new Error('The current sheet has no data rows. Open a populated sheet and try again.');
  var fit = withinOneRead_(wanted);
  return {tabs: fit.tabs, scope: scope, availableTabs: available, leftOut: fit.leftOut};
}

// Of `sheets` in order, the names one read holds and {name, reason} for the rest: one tab too many or too large never
// takes the others down. A tab past the worksheet limits stays in: only reading it can tell whether what it holds fits
// them (readTrimmed_), and that does not hold up the sidebar's token, which this plan travels with.
function withinOneRead_(sheets) {
  var tabs = [], leftOut = [], cells = 0;
  sheets.forEach(function(sheet) {
    var rows = sheet.getLastRow(), columns = sheet.getLastColumn();
    if (tabs.length >= GRID_LIMITS.sheets) {
      leftOut.push({name: sheet.getName(), reason: 'one read holds ' + GRID_LIMITS.sheets + ' tabs'});
    } else if (overLimit_(rows, columns)) {
      tabs.push(sheet.getName());
    } else if (cells + rows * columns > GRID_LIMITS.cells) {
      leftOut.push({name: sheet.getName(), reason: 'one read holds ' + grouped_(GRID_LIMITS.cells) + ' cells'});
    } else {
      cells += rows * columns;
      tabs.push(sheet.getName());
    }
  });
  return {tabs: tabs, leftOut: leftOut};
}

// Which worksheet limit a tab of this size is past, in the words the sidebar tells the user; '' when none.
function overLimit_(rows, columns) {
  if (rows - 1 > GRID_LIMITS.rows) return 'it has more than ' + grouped_(GRID_LIMITS.rows) + ' data rows';
  if (columns > GRID_LIMITS.columns) return 'it has more than ' + GRID_LIMITS.columns + ' columns';
  if (rows * columns > GRID_LIMITS.cells) return 'it has more than ' + grouped_(GRID_LIMITS.cells) + ' cells';
  return '';
}

// A tab past the limits, as a grid of the columns and rows that hold anything when those fit them: one number in column
// IV made a 7,827-row tab with 31,943 filled cells a grid of 2,003,712 (2026-11-01). No filled cell is dropped.
// Otherwise {name, leftOut: why}, and the other tabs are read without it, as they are when the read itself fails.
function readTrimmed_(sheet, timeZone) {
  var height = sheet.getLastRow();
  var over = {name: sheet.getName(), leftOut: overLimit_(height, sheet.getLastColumn())};
  try {
    var columns = filledColumns_(sheet, height);
    if (!columns) return over;
    var width = columns.reduce(function(sum, run) { return sum + run[1] - run[0] + 1; }, 0);
    return height * width > TRIM_READ_CELLS ? over : readTab_(sheet, timeZone, columns);
  } catch (error) {
    return over;
  }
}

// The [first, last] runs of `sheet`'s columns that hold anything, asking whether a band is blank and halving those that
// are not, so a wide empty stretch takes one question. Null once more are filled than a worksheet may have.
function filledColumns_(sheet, height) {
  var runs = [], filled = 0;
  function walk(first, last) {
    if (filled > GRID_LIMITS.columns || sheet.getRange(1, first, height, last - first + 1).isBlank()) return;
    if (first < last) {
      var middle = Math.floor((first + last) / 2);
      walk(first, middle);
      walk(middle + 1, last);
      return;
    }
    filled++;
    var run = runs[runs.length - 1];
    if (run && run[1] === first - 1) run[1] = first;
    else runs.push([first, first]);
  }
  walk(1, sheet.getLastColumn());
  return filled > GRID_LIMITS.columns ? null : runs;
}

// One tab as a cell grid: values, number formats, failed formulas and merged ranges. `columns`: the runs a tab past the
// limits is trimmed to (readTrimmed_); its blank rows go too when the columns alone do not bring it within them, one
// kept between filled stretches, as a blank row separates what the importer reads. A trimmed tab still past them comes
// back as {name, leftOut: why}.
function readTab_(sheet, timeZone, columns) {
  var height = sheet.getLastRow(), lastColumn = sheet.getLastColumn();
  var ranges = (columns || [[1, lastColumn]]).map(function(run) {
    return sheet.getRange(1, run[0], height, run[1] - run[0] + 1);
  });
  var values = sideBySide_(ranges.map(function(range) { return range.getValues(); }));
  var kept = null;
  if (columns) {
    kept = overLimit_(height, values[0].length) ? rowsWorthKeeping_(values) : values.map(function(_, r) { return r; });
    values = kept.map(function(r) { return values[r]; });
    var still = overLimit_(values.length, values[0].length);
    if (still) return {name: sheet.getName(), leftOut: still};
  }
  var offsets = {};
  var rows = values.map(function(row) {
    return row.map(function(value) { return gridValue_(value, timeZone, offsets); });
  });
  var formats = sideBySide_(ranges.map(function(range) { return range.getNumberFormats(); }));
  if (kept) formats = kept.map(function(r) { return formats[r]; });
  return {
    name: sheet.getName(),
    rows: rows,
    columnFormats: columnFormats_(formats, rows),
    errorCells: errorCells_(values),
    merges: kept ? trimmedMerges_(sheet.getRange(1, 1, height, lastColumn).getMergedRanges(), kept, columns)
      : ranges[0].getMergedRanges().map(function(merged) {
        return {s: {r: merged.getRow() - 1, c: merged.getColumn() - 1},
                e: {r: merged.getLastRow() - 1, c: merged.getLastColumn() - 1}};
      }),
    date1904: false
  };
}

// Column runs read separately, as one matrix.
function sideBySide_(matrices) {
  if (matrices.length === 1) return matrices[0];
  return matrices[0].map(function(_, r) {
    return Array.prototype.concat.apply([], matrices.map(function(matrix) { return matrix[r]; }));
  });
}

function rowsWorthKeeping_(values) {
  function blank(row) { return row.every(function(value) { return value === '' || value === null; }); }
  var kept = [];
  values.forEach(function(row, r) {
    if (!blank(row) || (r > 0 && !blank(values[r - 1]))) kept.push(r);
  });
  return kept;
}

// Merged ranges at their place among the kept rows (`kept`, zero-based) and column runs. A range whose first cell went
// held nothing, and one left a single cell merges nothing.
function trimmedMerges_(merges, kept, columns) {
  var rowAt = {}, columnAt = {}, next = 0;
  kept.forEach(function(row, index) { rowAt[row + 1] = index; });
  columns.forEach(function(run) {
    for (var column = run[0]; column <= run[1]; column++) columnAt[column] = next++;
  });
  var trimmed = [];
  merges.forEach(function(merged) {
    var r = rowAt[merged.getRow()], c = columnAt[merged.getColumn()];
    if (r === undefined || c === undefined) return;
    var lastRow = merged.getLastRow(), lastColumn = merged.getLastColumn();
    while (rowAt[lastRow] === undefined) lastRow--;
    while (columnAt[lastColumn] === undefined) lastColumn--;
    if (rowAt[lastRow] > r || columnAt[lastColumn] > c) trimmed.push({s: {r: r, c: c}, e: {r: rowAt[lastRow], c: columnAt[lastColumn]}});
  });
  return trimmed;
}

// Each column's number format once, with the rows whose format differs ({f, x: [[row, format]]}), instead of a
// format for every cell: a six-tab export read 24.5 MB, 10 MB of it per-cell formats (2026-10-04). Only a
// number's format changes what the import reads (a date, a duration, a percentage), so a text or blank cell's
// format is not sent. The importer reads them back (web/public/lib/workbook-import.js gridWorkbook).
function columnFormats_(formats, rows) {
  var width = formats.length ? formats[0].length : 0;
  var columns = [];
  for (var c = 0; c < width; c++) {
    var counts = Object.create(null);
    var best = 'General', most = 0;
    for (var r = 0; r < formats.length; r++) {
      if (typeof rows[r][c] !== 'number') continue;
      var format = formats[r][c];
      counts[format] = (counts[format] || 0) + 1;
      if (counts[format] > most) { most = counts[format]; best = format; }
    }
    var exceptions = [];
    for (var row = 0; row < formats.length; row++) {
      if (typeof rows[row][c] === 'number' && formats[row][c] !== best) exceptions.push([row, formats[row][c]]);
    }
    columns.push(exceptions.length ? {f: best, x: exceptions} : {f: best});
  }
  return columns;
}

// The [row, column] of every cell holding a formula error ("#DIV/0!"), instead of a flag for every cell: the
// flags were 4.6 MB of the same read, almost all of them false.
function errorCells_(values) {
  var cells = [];
  values.forEach(function(row, r) {
    row.forEach(function(value, c) {
      if (typeof value === 'string' && SHEETS_ERRORS.test(value)) cells.push([r, c]);
    });
  });
  return cells;
}

// A count as the add-on's messages write it: 50000 is "50,000".
function grouped_(count) {
  return String(count).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
}

// google.script.run cannot return Date objects. A date, time or duration cell becomes the serial day
// count Sheets stores for it (days since 1899-12-30 in the spreadsheet's time zone); its number format
// travels with it, so the importer reads it as the upload of the same sheet would. A zone's offset is asked of Apps Script once
// for a day it does not change on (`offsets`, by day): a subscriptions export holds six date columns of 11,500 rows.
function gridValue_(value, timeZone, offsets) {
  if (Object.prototype.toString.call(value) === '[object Date]') {
    var seconds = Math.floor(value.getTime() / 1000) * 1000;
    return (seconds + zoneOffset_(seconds, timeZone, offsets) - Date.UTC(1899, 11, 30)) / DAY_MS;
  }
  if (typeof value === 'number' && !isFinite(value)) return '';
  return value;
}

// The zone's offset from UTC at this moment, in milliseconds. A day whose clock changes is asked moment by moment.
function zoneOffset_(moment, timeZone, offsets) {
  var day = Math.floor(moment / DAY_MS);
  if (!(day in offsets)) {
    var start = offsetAt_(day * DAY_MS, timeZone), end = offsetAt_((day + 1) * DAY_MS, timeZone);
    offsets[day] = start === end ? start : null;
  }
  return offsets[day] === null ? offsetAt_(moment, timeZone) : offsets[day];
}

function offsetAt_(moment, timeZone) {
  var parts = Utilities.formatDate(new Date(moment), timeZone, 'yyyy,MM,dd,HH,mm,ss').split(',').map(Number);
  return Date.UTC(parts[0], parts[1] - 1, parts[2], parts[3], parts[4], parts[5]) - moment;
}

// The engine's views for the sidebar's reasoning steps: what each step did and where it came from,
// without its rows (the sidebar shows the answer, and the full analysis opens in Prereasoner).
function traceSummary_(raw) {
  var traces = Array.isArray(raw.traces) ? raw.traces : [];
  return traces.map(function(trace) {
    var engine = (trace && trace.engine) || {};
    return {
      jobId: String((trace && trace.jobId) || ''),
      question: String((trace && trace.question) || ''),
      analysis: engine.analysis && typeof engine.analysis === 'object' ? engine.analysis : null,
      execution: engine.execution && typeof engine.execution === 'object' ? engine.execution : null,
      views: (Array.isArray(engine.views) ? engine.views : []).map(function(view) {
        return {name: view.name, op: view.op, label: view.label, sql: view.sql, inputs: view.inputs,
          section: view.section, section_label: view.section_label, section_question: view.section_question,
          section_inputs: view.section_inputs, is_output: view.is_output, execution: view.execution};
      })
    };
  });
}

function errorMessage_(error) {
  var message = error && error.message ? String(error.message) : String(error || 'Unknown error');
  if (/PERMISSION_DENIED/i.test(message)) {
    return 'Google blocked access to this sheet. Reopen the add-on in the account that owns the sheet, then authorize it.';
  }
  return message;
}

function apiRequest_(method, path, payload, failure) {
  return fetchJson_(PREREASONER_API_URL + path, method, payload, failure);
}

function chatRequest_(payload, answered) {
  return fetchJson_(PREREASONER_CHAT_URL, 'post', payload, 'Prereasoner could not be reached. Try again in a moment.',
    answered);
}

// `answered`: HTTP statuses the caller handles itself; their body comes back with its `status`.
function fetchJson_(url, method, payload, failure, answered) {
  var options = {
    method: method,
    headers: {Authorization: 'Bearer ' + firebaseIdToken_(), Accept: 'application/json'},
    muteHttpExceptions: true
  };
  if (payload) {
    options.contentType = 'application/json';
    options.payload = JSON.stringify(payload);
  }
  var response;
  try {
    response = UrlFetchApp.fetch(url, options);
  } catch (error) {
    throw new Error(failure + ' Try again in a moment.');
  }
  var status = response.getResponseCode();
  var body = {};
  try { body = JSON.parse(response.getContentText() || '{}'); } catch (_) {}
  if (answered && answered.indexOf(status) >= 0) return {status: status, error: body && body.error};
  if (status < 200 || status >= 300) {
    var message = body && body.error ? String(body.error) : 'request failed';
    if (status === 401) message = 'Google sign-in could not be verified';
    if (status === 429 && /too many requests|rate limit|request rate/i.test(message)) {
      message = 'too many requests; wait a moment and try again';
    }
    throw new Error('Prereasoner: ' + message + '.');
  }
  return body;
}

function firebaseIdToken_() {
  var googleAccessToken = ScriptApp.getOAuthToken();
  var exchange = {
    requestUri: 'https://chat.prereasoner.com',
    postBody: 'access_token=' + encodeURIComponent(googleAccessToken) + '&providerId=google.com',
    returnIdpCredential: true,
    returnSecureToken: true
  };
  var response;
  try {
    response = UrlFetchApp.fetch(FIREBASE_TOKEN_URL, {
      method: 'post',
      contentType: 'application/json',
      payload: JSON.stringify(exchange),
      muteHttpExceptions: true
    });
  } catch (error) {
    var fetchDetail = error && error.message ? String(error.message) : String(error || 'request failed');
    throw new Error('Google sign-in could not be connected to Prereasoner (' + fetchDetail + ').');
  }
  var data = {};
  try { data = JSON.parse(response.getContentText() || '{}'); } catch (_) {}
  if (response.getResponseCode() !== 200) {
    var detail = data && data.error && data.error.message ? String(data.error.message) : 'identity exchange failed';
    throw new Error('Google sign-in could not be connected to Prereasoner (' + detail + ').');
  }
  if (!data.idToken) throw new Error('Google sign-in did not return a usable identity.');
  return data.idToken;
}

function normalizeHistory_(history) {
  if (!Array.isArray(history)) return [];
  var clean = history.filter(function(item) {
    return item && (item.role === 'user' || item.role === 'assistant') && typeof item.content === 'string';
  }).slice(-REQUEST_LIMITS.historyItems);
  var kept = [];
  var chars = 0;
  for (var i = clean.length - 1; i >= 0; i--) {
    var content = clean[i].content.slice(0, REQUEST_LIMITS.questionChars);
    if (chars + content.length > REQUEST_LIMITS.historyChars) break;
    kept.unshift({role: clean[i].role, content: content});
    chars += content.length;
  }
  return kept;
}
