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
var GRID_LIMITS = {sheets: 8, rows: 10000, columns: 256, cells: 250000};
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
// the live trace), this spreadsheet's identity, and its cells.
function getSidebarContext() {
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

function restorePrereasonerSheetConversation(request) {
  var tables = requestTables_(request);
  var body = apiRequest_('post', '/api/spreadsheet/conversation/restore',
    {spreadsheet_id: activeSpreadsheet_().getId(), host: 'sheets', tables: tables},
    'Prereasoner could not restore this sheet’s conversation.');
  return {
    conversationId: body.conversation_id || '',
    state: body.state && typeof body.state === 'object' ? body.state : null,
    stale: !!body.source_changed
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

// The sheet changed since the conversation last saw it: bring the conversation's source up to date,
// which marks its earlier answers stale.
function syncPrereasonerConversation(request) {
  request = request || {};
  var conversationId = conversationId_(request.conversationId);
  if (!conversationId) throw new Error('Start a chat before syncing data.');
  var body = apiRequest_('post', '/api/conversation/sync', {id: conversationId, tables: requestTables_(request)},
    'Prereasoner could not sync this spreadsheet.');
  return {changed: !!body.changed};
}

function askPrereasoner(request) {
  request = request || {};
  var question = String(request.question || '').trim();
  if (!question) throw new Error('Enter a question about this spreadsheet.');
  if (question.length > REQUEST_LIMITS.questionChars) {
    throw new Error('Questions must be ' + REQUEST_LIMITS.questionChars + ' characters or fewer.');
  }
  var payload = {
    message: question,
    tables: requestTables_(request),
    history: normalizeHistory_(request.history),
    conversation_id: conversationId_(request.conversationId) || null
  };
  if (request.conversationId && !payload.conversation_id) {
    throw new Error('The conversation expired. Start a new conversation and try again.');
  }
  var turnId = request.turnId == null ? '' : String(request.turnId).trim();
  if (turnId) {
    if (!/^[A-Za-z0-9_-]{1,128}$/.test(turnId)) throw new Error('The live request id is invalid.');
    payload.turnId = turnId;
  }
  var raw = chatRequest_(payload);
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
    return {name: table.name, data: table.data, source: {kind: 'google-sheets-addon'}};
  });
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

function chatRequest_(payload) {
  return fetchJson_(PREREASONER_CHAT_URL, 'post', payload, 'Prereasoner could not be reached. Try again in a moment.');
}

function fetchJson_(url, method, payload, failure) {
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
  if (status < 200 || status >= 300) {
    var message = body && body.error ? String(body.error) : 'request failed';
    if (status === 401) message = 'Google sign-in could not be verified';
    if (status === 429) message = 'too many requests; wait a moment and try again';
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
