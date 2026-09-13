var PREREASONER_CHAT_URL = 'https://chat.prereasoner.com/chat';
var PREREASONER_REASON_URL = 'https://chat.prereasoner.com/reason/';
var PREREASONER_API_URL = 'https://chat.prereasoner.com';
var PREREASONER_PRIVACY_URL = 'https://chat.prereasoner.com/privacy';
var PREREASONER_TERMS_URL = 'https://chat.prereasoner.com/terms';
var PREREASONER_SUPPORT_URL = 'https://chat.prereasoner.com/support';
var FIREBASE_API_KEY = 'AIzaSyAC_Kiqj3lqd52ufpqYDAO17G6T7wfBd9Q';
var FIREBASE_TOKEN_URL = 'https://identitytoolkit.googleapis.com/v1/accounts:signInWithIdp?key=' + FIREBASE_API_KEY;
var ADDON_LIMITS = {
  sheets: 8,
  rows: 10000,
  tableChars: 2000000,
  totalChars: 6000000,
  questionChars: 20000,
  historyItems: 24,
  historyChars: 80000
};

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
  var token = firebaseIdToken_();
  var response;
  try {
    response = UrlFetchApp.fetch(PREREASONER_API_URL + '/api/conversations?limit=50', {
      method: 'get',
      headers: {Authorization: 'Bearer ' + token, Accept: 'application/json'},
      muteHttpExceptions: true
    });
  } catch (error) {
    throw new Error('Prereasoner could not load previous conversations. Try again in a moment.');
  }
  var status = response.getResponseCode();
  var text = response.getContentText();
  var body = {};
  try { body = text ? JSON.parse(text) : {}; } catch (_) {}
  if (status === 401) throw new Error('Google sign-in could not be verified. Reopen the add-on and authorize it again.');
  if (status < 200 || status >= 300) throw new Error('Prereasoner could not load previous conversations.');
  return {
    conversations: (Array.isArray(body.conversations) ? body.conversations : []).filter(function(item) {
      return item && /^c_[0-9a-f]{32}$/i.test(String(item.id || ''));
    }).slice(0, 50)
  };
}

function showSidebar() {
  var template = HtmlService.createTemplateFromFile('Sidebar');
  try {
    template.initialContext = JSON.stringify(collectWorkbook_().summary).replace(/<\//g, '<\\/');
  } catch (error) {
    template.initialContext = JSON.stringify({error: errorMessage_(error)});
  }
  var html = template.evaluate()
    .setTitle('Prereasoner');
  SpreadsheetApp.getUi().showSidebar(html);
}

function getSheetContext() {
  try {
    return collectWorkbook_().summary;
  } catch (error) {
    return {
      error: errorMessage_(error),
      activeSheet: '',
      tables: [],
      totalRows: 0,
      privacyUrl: PREREASONER_PRIVACY_URL,
      termsUrl: PREREASONER_TERMS_URL,
      supportUrl: PREREASONER_SUPPORT_URL
    };
  }
}

function askPrereasoner(request) {
  request = request || {};
  var question = String(request.question || '').trim();
  if (!question) throw new Error('Enter a question about this spreadsheet.');
  if (question.length > ADDON_LIMITS.questionChars) {
    throw new Error('Questions must be ' + ADDON_LIMITS.questionChars + ' characters or fewer.');
  }

  var conversationId = request.conversationId == null ? null : String(request.conversationId);
  if (conversationId && !/^c_[0-9a-f]{32}$/i.test(conversationId)) {
    throw new Error('The conversation expired. Start a new conversation and try again.');
  }

  var workbook = collectWorkbook_();
  var payload = {
    message: question,
    tables: workbook.tables,
    history: normalizeHistory_(request.history),
    conversation_id: conversationId
  };
  var response = fetchPrereasoner_(payload);
  return clientResponse_(response, workbook.summary, question);
}

function syncPrereasonerConversation(request) {
  request = request || {};
  var conversationId = String(request.conversationId || '');
  if (!/^c_[0-9a-f]{32}$/i.test(conversationId)) {
    throw new Error('Start a chat before checking data sync.');
  }
  var workbook = collectWorkbook_();
  var token = firebaseIdToken_();
  var response;
  try {
    response = UrlFetchApp.fetch(PREREASONER_API_URL + '/api/conversation/sync', {
      method: 'post',
      contentType: 'application/json',
      headers: {Authorization: 'Bearer ' + token, Accept: 'application/json'},
      payload: JSON.stringify({id: conversationId, tables: workbook.tables}),
      muteHttpExceptions: true
    });
  } catch (error) {
    throw new Error('Prereasoner could not check data sync. Try again.');
  }
  var body = {};
  try { body = JSON.parse(response.getContentText() || '{}'); } catch (_) {}
  if (response.getResponseCode() < 200 || response.getResponseCode() >= 300) {
    throw new Error(body.error || 'Prereasoner could not sync this spreadsheet.');
  }
  return {
    changed: !!body.changed,
    sourceHash: body.source_hash || '',
    datasetVersion: Number(body.dataset_version || 0),
    context: workbook.summary
  };
}

function collectWorkbook_() {
  var spreadsheet = SpreadsheetApp.getActiveSpreadsheet();
  if (!spreadsheet) throw new Error('Open a Google Sheet before using Prereasoner.');

  var activeSheet = spreadsheet.getActiveSheet();
  var sheets = spreadsheet.getSheets().filter(function(sheet) {
    return !sheet.isSheetHidden() && sheet.getLastRow() > 1 && sheet.getLastColumn() > 0;
  });
  sheets.sort(function(a, b) {
    if (a.getSheetId() === activeSheet.getSheetId()) return -1;
    if (b.getSheetId() === activeSheet.getSheetId()) return 1;
    return a.getIndex() - b.getIndex();
  });

  if (!sheets.length) {
    throw new Error('This spreadsheet needs a header row and at least one data row.');
  }
  if (sheets.length > ADDON_LIMITS.sheets) {
    throw new Error('Prereasoner can read at most ' + ADDON_LIMITS.sheets + ' non-empty sheets at once.');
  }

  var totalRows = 0;
  var totalChars = 0;
  var timeZone = spreadsheet.getSpreadsheetTimeZone() || 'UTC';
  var tables = [];
  var summaries = [];

  sheets.forEach(function(sheet) {
    var rowCount = sheet.getLastRow();
    var columnCount = sheet.getLastColumn();
    totalRows += Math.max(0, rowCount - 1);
    if (totalRows > ADDON_LIMITS.rows) {
      throw new Error('The workbook has more than ' + ADDON_LIMITS.rows + ' data rows. Use a smaller workbook.');
    }

    var range = sheet.getRange(1, 1, rowCount, columnCount);
    var values = range.getValues();
    values[0] = normalizeHeaders_(values[0]);
    var csv = valuesToCsv_(values, timeZone);
    if (csv.length > ADDON_LIMITS.tableChars) {
      throw new Error('The sheet “' + sheet.getName() + '” is too large. Keep each sheet under 2 MB of text.');
    }
    totalChars += csv.length;
    if (totalChars > ADDON_LIMITS.totalChars) {
      throw new Error('The workbook is too large. Keep the combined sheet data under 6 MB of text.');
    }

    tables.push({name: sheet.getName(), data: csv, source: {kind: 'google-sheets-addon'}});
    summaries.push({
      name: sheet.getName(),
      range: range.getA1Notation(),
      rows: Math.max(0, rowCount - 1),
      columns: columnCount,
      preview: values.slice(0, 4).map(function(row) {
        return row.slice(0, 4).map(function(value) { return cellText_(value, timeZone); });
      })
    });
  });

  return {
    tables: tables,
    summary: {
      spreadsheet: spreadsheet.getName(),
      activeSheet: activeSheet.getName(),
      tables: summaries,
      tableCount: summaries.length,
      totalRows: totalRows,
      fingerprint: workbookFingerprint_(tables),
      privacyUrl: PREREASONER_PRIVACY_URL,
      termsUrl: PREREASONER_TERMS_URL,
      supportUrl: PREREASONER_SUPPORT_URL
    }
  };
}

function workbookFingerprint_(tables) {
  var snapshot = (tables || []).map(function(table) { return [table.name, table.data]; });
  var digest = Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256, JSON.stringify(snapshot));
  return Utilities.base64EncodeWebSafe(digest).replace(/=+$/, '');
}

function errorMessage_(error) {
  var message = error && error.message ? String(error.message) : String(error || 'Unknown error');
  if (/PERMISSION_DENIED/i.test(message)) {
    return 'Google blocked access to this sheet. Reopen the add-on in the account that owns the sheet, then authorize it.';
  }
  return message;
}

function fetchPrereasoner_(payload) {
  var token = firebaseIdToken_();
  var response;
  try {
    response = UrlFetchApp.fetch(PREREASONER_CHAT_URL, {
      method: 'post',
      contentType: 'application/json',
      headers: {Authorization: 'Bearer ' + token, Accept: 'application/json'},
      payload: JSON.stringify(payload),
      muteHttpExceptions: true
    });
  } catch (error) {
    throw new Error('Prereasoner could not be reached. Try again in a moment.');
  }

  var status = response.getResponseCode();
  var text = response.getContentText();
  var body = {};
  try { body = text ? JSON.parse(text) : {}; } catch (_) {}
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
  var responseText = response.getContentText() || '{}';
  var data = {};
  try { data = JSON.parse(responseText); } catch (_) {}
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
  }).slice(-ADDON_LIMITS.historyItems);
  var kept = [];
  var chars = 0;
  for (var i = clean.length - 1; i >= 0; i--) {
    var content = clean[i].content.slice(0, ADDON_LIMITS.questionChars);
    if (chars + content.length > ADDON_LIMITS.historyChars) break;
    kept.unshift({role: clean[i].role, content: content});
    chars += content.length;
  }
  return kept;
}

function clientResponse_(raw, context, question) {
  raw = raw || {};
  var reasoning = extractReasoning_(raw);
  var reply = String(raw.reply || '').trim();
  if (!reply) reply = fallbackReply_(reasoning.result);
  return {
    reply: reply,
    conversationId: raw.conversation_id || null,
    history: normalizeHistory_(raw.history),
    steps: reasoning.steps,
    reasoning: reasoning.steps,
    result: reasoning.result,
    context: context,
    question: question
  };
}

function extractReasoning_(raw) {
  var traces = Array.isArray(raw.traces) ? raw.traces : [];
  if (!traces.length && (raw.views || raw.answer || raw.result)) {
    traces = [{question: raw.question || '', engine: raw}];
  }
  var steps = [];
  var result = null;

  traces.forEach(function(trace) {
    var engine = trace && trace.engine ? trace.engine : {};
    (Array.isArray(engine.resolves) ? engine.resolves : []).forEach(function(resolve) {
      if (!resolve || !resolve.column) return;
      steps.push({
        label: 'Resolve ' + resolve.column,
        kind: 'resolve',
        detail: resolve.table ? 'Matched values from ' + resolve.table + '.' : 'Matched values to known entities.'
      });
    });

    var views = Array.isArray(engine.views) ? engine.views : [];
    views.forEach(function(view, index) {
      if (!view) return;
      var label = String(view.label || operationLabel_(view.op) || ('Step ' + (index + 1)));
      steps.push({
        label: label,
        kind: String(view.op || 'step'),
        detail: operationDetail_(view.op),
        preview: tablePreview_(view)
      });
    });

    var candidate = engine.answer || engine.result || (views.length ? views[views.length - 1] : null);
    if (candidate && Array.isArray(candidate.rows)) result = tablePreview_(candidate, 5, 5);
    if (!views.length && engine.sql && result) {
      steps.push({label: 'Result', kind: 'query', detail: 'Calculated the answer from the workbook.', preview: result});
    }
  });

  if (!result) {
    var top = raw.answer || raw.result;
    if (top && Array.isArray(top.rows)) result = tablePreview_(top, 5, 5);
  }
  return {steps: steps, result: result};
}

function tablePreview_(table, maxRows, maxColumns) {
  if (!table || !Array.isArray(table.rows)) return null;
  maxRows = maxRows || 3;
  maxColumns = maxColumns || 4;
  var columns = Array.isArray(table.columns) ? table.columns.slice(0, maxColumns) : [];
  var rows = table.rows.slice(0, maxRows).map(function(row) {
    return (Array.isArray(row) ? row : [row]).slice(0, maxColumns).map(wireText_);
  });
  return {columns: columns.map(wireText_), rows: rows, truncated: table.rows.length > maxRows};
}

function fallbackReply_(result) {
  if (result && result.rows && result.rows.length === 1 && result.rows[0].length === 1) {
    return 'The result is ' + result.rows[0][0] + '.';
  }
  return result ? 'Prereasoner completed the analysis.' : 'Prereasoner completed the request.';
}

function operationLabel_(operation) {
  var labels = {
    filter: 'Filter rows', time_filter: 'Filter dates', having: 'Filter groups', join: 'Join sheets',
    world_join: 'Connect reference data', world_filter: 'Filter reference data', group_agg: 'Aggregate',
    yoy: 'Calculate year-over-year change', running: 'Calculate running total', divide: 'Calculate ratio',
    share: 'Calculate share', topn: 'Rank results', sort: 'Sort results', select: 'Select result'
  };
  return labels[operation] || operation || '';
}

function operationDetail_(operation) {
  var details = {
    filter: 'Kept the rows that match the question.',
    time_filter: 'Kept the rows in the requested period.',
    having: 'Kept the groups that match the condition.',
    join: 'Connected related rows across sheets.',
    world_join: 'Connected workbook values to reference data.',
    world_filter: 'Applied the requested reference-data condition.',
    group_agg: 'Grouped the matching rows and calculated the measure.',
    yoy: 'Compared the measure with the previous year.',
    running: 'Accumulated the measure in order.',
    divide: 'Divided the requested measures.',
    share: 'Calculated each value as a share of the total.',
    topn: 'Kept the highest-ranked results.',
    sort: 'Ordered the result.',
    select: 'Selected the requested columns and rows.'
  };
  return details[operation] || 'Calculated an intermediate result.';
}

function valuesToCsv_(values, timeZone) {
  return values.map(function(row) {
    return row.map(function(cell) { return csvCell_(cell, timeZone); }).join(',');
  }).join('\n');
}

function csvCell_(value, timeZone) {
  var text = cellText_(value, timeZone);
  return /[",\r\n]/.test(text) ? '"' + text.replace(/"/g, '""') + '"' : text;
}

function cellText_(value, timeZone) {
  if (value == null) return '';
  if (Object.prototype.toString.call(value) === '[object Date]') {
    return Utilities.formatDate(value, timeZone || 'UTC', "yyyy-MM-dd'T'HH:mm:ss");
  }
  if (typeof value === 'number' && !isFinite(value)) return '';
  return String(value);
}

function wireText_(value) {
  if (value == null) return '';
  if (typeof value === 'object') {
    if (Object.prototype.hasOwnProperty.call(value, 'value')) return String(value.value);
    return JSON.stringify(value);
  }
  return String(value);
}

function normalizeHeaders_(headers) {
  var seen = {};
  return (headers || []).map(function(value, index) {
    var base = String(value == null ? '' : value).trim() || ('column_' + (index + 1));
    var key = base.toLowerCase();
    seen[key] = (seen[key] || 0) + 1;
    return seen[key] === 1 ? base : base + '_' + seen[key];
  });
}
