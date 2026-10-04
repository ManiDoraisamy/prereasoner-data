import {initializeApp} from 'https://www.gstatic.com/firebasejs/10.12.0/firebase-app.js';
import {getAuth, OAuthProvider, onAuthStateChanged, signInWithCredential, getIdToken} from 'https://www.gstatic.com/firebasejs/10.12.0/firebase-auth.js';
import {getDatabase, ref, onValue} from 'https://www.gstatic.com/firebasejs/10.12.0/firebase-database.js';
import {firebaseConfig} from '../../lib/config.js';
import {readWorkbook, readWorkbookSchema, workbookKey} from './host.js';
import {credentialForDialog} from './auth-bridge.js';
import {explainMicrosoftAuthError} from './auth-errors.js';
import {streamResponse} from './turn-bridge.js';

const app = initializeApp(firebaseConfig, 'prereasoner-excel');
const auth = getAuth(app);
const database = getDatabase(app);
const $ = id => document.getElementById(id);
const renderer = window.PrereasonerTurnRenderer;
const state = {conversationId: null, history: [], turns: [], tables: [], workbookId: null, nextPage: null};
const suggestions = window.PrereasonerSuggestions.create({container: $('suggestions'), composer: $('question'),
  request: schema => api('/chat/suggestions', schema)});
let activeDialog = null;
let pendingWorkbookRead = null;
let workbookSchema = null;
function refreshSuggestions(workbook) {
  if (workbook && !workbookSchema) workbookSchema = {
    sheets: workbook.sheetNames.map(name=>({name,columns:[]})), active_sheet:workbook.activeSheet, scope:[]};
  const schema=window.PrereasonerSuggestions.merge(workbookSchema,state.tables,workbook?.activeSheet);
  if(schema)suggestions.update(schema);
}
function currentWorkbook() {
  if (!pendingWorkbookRead) pendingWorkbookRead = readWorkbook().finally(() => { pendingWorkbookRead = null; });
  return pendingWorkbookRead;
}

function notice(message, error = false) {
  const el = $('notice');
  el.textContent = message || '';
  el.hidden = !message;
  el.classList.toggle('error', error);
}

function token() {
  if (!auth.currentUser) throw new Error('Sign in to Prereasoner to continue.');
  return getIdToken(auth.currentUser);
}

async function api(path, body) {
  const response = await fetch(path, {
    method: 'POST', headers: {'Content-Type': 'application/json', Authorization: `Bearer ${await token()}`},
    body: JSON.stringify(body)
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status}).`);
  return data;
}

async function get(path) {
  const response = await fetch(path, {headers: {Authorization: `Bearer ${await token()}`}});
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status}).`);
  return data;
}

function setBusy(busy) {
  state.busy = busy;
  $('question').disabled = false;
  $('send').disabled = busy || !auth.currentUser;
  $('newChat').disabled = busy || !auth.currentUser;
  $('previousChats').disabled = busy || !auth.currentUser;
}

function renderTurns() {
  const thread = $('thread');
  thread.querySelectorAll('.turn-pair,.empty').forEach(el => el.remove());
  // Starter questions belong to an empty conversation only.
  suggestions.setActive(!state.turns.length);
  for (const turn of state.turns) {
    const analysisUrl = turn.conversationId ? `https://chat.prereasoner.com/reason/${encodeURIComponent(turn.conversationId)}` : '';
    const reasoningBody = renderer.renderReasoningTree(turn.reasoning || [], {
      renderStep: (step, index) => renderer.renderStepLink(step, index,
        analysisUrl ? {href: analysisUrl, title: 'Open this reasoning in Prereasoner'} : {})
    });
    const reasoningHtml = renderer.renderReasoningPanel({bodyHtml: reasoningBody, title: 'How this was calculated', analysisUrl});
    const assistantHtml = turn.error
      ? `<div class="turn-content"><div class="answer error" role="alert">${renderer.escapeHtml(turn.reply)}</div></div>`
      : renderer.renderAssistantTurn({reply: turn.reply, reasoningHtml,
        afterHtml: renderer.renderResultLink(turn.reply, analysisUrl)});
    thread.insertAdjacentHTML('beforeend', renderer.renderTurn({question: turn.question, assistantHtml}));
  }
  thread.scrollTop = thread.scrollHeight;
}

function snapshot() {
  return {client: 'prereasoner-excel-addon', version: 1, turns: state.turns.slice(-24), history: state.history.slice(-24)};
}

async function persist() {
  if (!state.conversationId || !state.workbookId) return;
  await api('/api/spreadsheet/conversation/state', {
    host: 'excel', spreadsheet_id: state.workbookId, conversation_id: state.conversationId, state: snapshot()
  });
}

async function restore(tables) {
  if (!state.workbookId) state.workbookId = await workbookKey();
  const response = await api('/api/spreadsheet/conversation/restore', {
    host: 'excel', spreadsheet_id: state.workbookId, tables
  });
  state.conversationId = response.conversation_id || null;
  const saved = response.state;
  if (saved?.version === 1 && saved.client === 'prereasoner-excel-addon') {
    state.turns = Array.isArray(saved.turns) ? saved.turns.slice(-24) : [];
    state.history = Array.isArray(saved.history) ? saved.history.slice(-24) : [];
  } else {
    state.turns = [];
    state.history = [];
  }
  if (response.source_changed) notice('Workbook data changed since this conversation. Ask again to analyze the current data.');
  renderTurns();
}

function mapReasoning(response) {
  if (Array.isArray(response.reasoning)) return response.reasoning;
  if (Array.isArray(response.steps)) return response.steps;
  const traces = Array.isArray(response.traces) ? response.traces : [];
  return traces.flatMap((trace, ti) => {
    const engine = trace?.engine || trace || {};
    const views = Array.isArray(engine.views) ? engine.views : [];
    const normalizedViews = views.map(view => ({
      ...view,
      op: view.op || view.operation || '',
      label: view.label || view.title || '',
    }));
    return renderer.stepsFromViews(normalizedViews, {execution: engine.execution})
      .map((step, vi) => ({
        ...step,
        description: views[vi].description || views[vi].question || step.description,
        index: ti * 100 + vi,
      }));
  });
}

function awaitStream(turnId) {
  const unsubs = [];
  const engineRuns = {};
  const watchedJobs = new Set();
  const readyJobs = new Set();
  let latestTurn = {};
  let unsubscribe = () => unsubs.splice(0).forEach(stop => stop());
  let timer;
  let cancel;
  const promise = new Promise((resolve, reject) => {
    const base = ref(database, `runs/${auth.currentUser.uid}/${turnId}`);
    const finishIfReady = () => {
      if (latestTurn.status !== 'done' || typeof latestTurn.reply !== 'string') return;
      if (![...watchedJobs].every(jobId => readyJobs.has(jobId))) return;
      clearTimeout(timer); unsubscribe(); resolve(streamResponse(latestTurn, engineRuns));
    };
    unsubs.push(onValue(base, snapshot => {
      const run = snapshot.val() || {};
      latestTurn = run;
      const calls = run.calls && typeof run.calls === 'object' ? Object.values(run.calls) : [];
      for (const call of calls) {
        if (!call?.jobId || watchedJobs.has(call.jobId)) continue;
        watchedJobs.add(call.jobId);
        unsubs.push(onValue(ref(database, `runs/${auth.currentUser.uid}/${call.jobId}`), trace => {
          engineRuns[call.jobId] = trace.val() || {};
          readyJobs.add(call.jobId);
          finishIfReady();
        }, error => {
          clearTimeout(timer); unsubscribe(); reject(error);
        }));
      }
      finishIfReady();
      if (run.status === 'error') {
        clearTimeout(timer); unsubscribe(); reject(new Error(run.error || 'The analysis could not be completed.'));
      }
    }, error => { clearTimeout(timer); unsubscribe(); reject(error); }));
    timer = setTimeout(() => { unsubscribe(); reject(new Error('The analysis is taking longer than expected. You can try again.')); }, 240000);
  });
  cancel = () => { clearTimeout(timer); unsubscribe(); };
  return {promise, cancel};
}

async function ask(question) {
  setBusy(true);
  $('question').value = '';
  let baseHistory = state.history.slice();
  try {
    const workbook = await currentWorkbook();
    state.tables = workbook.tables;
    refreshSuggestions(workbook);
    $('sheetCount').textContent = `${workbook.tables.length} tab${workbook.tables.length === 1 ? '' : 's'} · ${workbook.name}`;
    await restore(workbook.tables);
    baseHistory = state.history.slice();
    const turnId = crypto.randomUUID().replaceAll('-', '');
    state.history.push({role: 'user', content: question});
    state.turns.push({question, reply: 'Working on your question…', reasoning: [], pending: true});
    renderTurns();
    const streamResult = awaitStream(turnId);
    let response;
    try {
      response = await api('/chat', {
        message: question, tables: workbook.tables, history: baseHistory,
        conversation_id: state.conversationId, turnId
      });
    } catch (error) {
      if (state.conversationId && /conversation not found/i.test(error.message)) {
        // Deleted elsewhere (orchestrator/server.py answers 404): ask once more as a new chat, keeping
        // the thread and the history the assistant reads.
        state.conversationId = null;
        response = await api('/chat', {message: question, tables: workbook.tables, history: baseHistory,
          conversation_id: null, turnId});
      } else if (error instanceof TypeError || /timed out|network|failed to fetch/i.test(error.message)) {
        response = await streamResult.promise;
      } else { streamResult.cancel(); throw error; }
    }
    streamResult.cancel();
    if (response.error) throw new Error(response.error);
    state.conversationId = response.conversation_id || state.conversationId;
    state.turns[state.turns.length - 1] = {
      question, reply: response.reply || 'No answer was returned.', reasoning: mapReasoning(response),
      conversationId: state.conversationId
    };
    state.history.push({role: 'assistant', content: response.reply || ''});
    renderTurns();
    notice('');
    // A failed save loses nothing on screen; the next answer saves the whole conversation again.
    persist().catch(() => {});
  } catch (error) {
    // A failed question stays in the thread with its reason; it was dropped, and lost entirely when
    // the user had typed a new draft meanwhile (2026-10-04).
    const failed = {question, reply: error.message || 'Could not answer that question.', error: true};
    if (state.turns[state.turns.length - 1]?.pending) state.turns[state.turns.length - 1] = failed;
    else state.turns.push(failed);
    state.history = baseHistory;
    if (!$('question').value.trim()) $('question').value = question;
    renderTurns(); notice('');
  } finally { setBusy(false); }
}

async function loadHistory(cursor) {
  const path = `/api/conversations?limit=30${cursor ? `&before=${encodeURIComponent(cursor)}` : ''}`;
  const response = await get(path);
  const items = response.conversations || [];
  const list = $('historyList');
  if (!cursor) list.replaceChildren();
  for (const item of items) {
    const link = document.createElement('a');
    link.className = 'history-item';
    const microsoftAccount = auth.currentUser.providerData.some(provider => provider.providerId === 'microsoft.com');
    link.href = `https://chat.prereasoner.com/reason/${encodeURIComponent(item.id)}${microsoftAccount ? '?auth=microsoft' : ''}`;
    link.target = '_blank'; link.rel = 'noopener noreferrer';
    const question = document.createElement('div'); question.className = 'history-question';
    question.textContent = item.question || 'Spreadsheet analysis';
    const date = document.createElement('div'); date.className = 'history-date';
    date.textContent = item.ts || '';
    link.append(question, date); list.append(link);
  }
  state.nextPage = response.next_cursor || null;
  $('loadMore').hidden = !state.nextPage;
}

function showHistory(show) {
  $('history').hidden = !show;
  $('thread').hidden = show;
  $('composer').hidden = show;
  $('previousChats').textContent = show ? 'Ask a question' : 'Previous conversations';
  if (show) loadHistory().catch(error => notice(error.message, true));
}

function openSignIn(error) {
  $('authPanel').hidden = false;
  $('thread').hidden = true;
  $('composer').hidden = true;
  showAuthError(error || '');
}

function showAuthError(message) {
  $('authError').textContent = message;
  $('authError').hidden = !message;
}

async function acceptDialogMessage(event) {
  if (event.origin !== location.origin) return;
  let message;
  try { message = JSON.parse(event.message ?? event.data); } catch (_) { return; }
  if (message.kind !== 'pr-auth-result' || message.provider !== 'microsoft') return;
  try {
    const credential = credentialForDialog(message, {OAuthProvider});
    await signInWithCredential(auth, credential);
    onSignedIn(auth.currentUser);
    activeDialog?.close();
    activeDialog = null;
  } catch (error) {
    const code = typeof error?.code === 'string' ? ` (Firebase error ${error.code})` : '';
    console.warn('Microsoft credential handoff failed', {code: error?.code || 'unknown'});
    openSignIn(`${explainMicrosoftAuthError(error)}${code}`);
  }
}

function launchAuth(provider) {
  showAuthError('');
  if (!window.Office?.context?.ui?.displayDialogAsync) {
    showAuthError('This Excel host does not support the sign-in dialog. Open the workbook in Excel for the web or desktop and try again.');
    return;
  }
  Office.context.ui.displayDialogAsync(`${location.origin}/office/excel/auth-dialog.html?provider=${provider}`, {
    height: 55, width: 35, displayInIframe: false
  }, result => {
    if (result.status !== Office.AsyncResultStatus.Succeeded) {
      const code = result.error?.code;
      if (code === 12007) {
        showAuthError('A Microsoft sign-in window is already open. Finish signing in there, then return to Excel.');
      } else if (code === 12011) {
        showAuthError('Your browser blocked the sign-in window. Allow pop-ups for Excel, then try again.');
      } else if (code === 12009) {
        showAuthError('The sign-in window was dismissed. Select Continue with Microsoft to try again.');
      } else {
        const reason = code ? ` (Office error ${code})` : '';
        showAuthError(`Microsoft sign-in could not open${reason}. Please try again.`);
      }
      return;
    }
    const dialog = result.value;
    activeDialog = dialog;
    dialog.addEventHandler(Office.EventType.DialogMessageReceived, acceptDialogMessage);
    dialog.addEventHandler(Office.EventType.DialogEventReceived, arg => {
      activeDialog = null;
      if (arg.error === 12006) showAuthError('Sign-in was closed before it finished.');
    });
    showAuthError('A separate Microsoft sign-in window is open. Complete sign-in there, then return to Excel.');
  });
}

function onSignedIn(user) {
  if (!user) return;
  $('authPanel').hidden = true;
  $('thread').hidden = false;
  $('composer').hidden = false;
  setBusy(false);
  $('newChat').hidden = false;
  $('previousChats').hidden = false;
  $('question').focus();
  if (new URLSearchParams(location.search).get('view') === 'history') showHistory(true);
  else if (!state.tables.length) {
    // Reading suggestions is independent of submitting a question; the draft stays editable.
    currentWorkbook().then(workbook => {
      state.tables = workbook.tables;
      $('sheetCount').textContent = `${workbook.tables.length} tabs · ${workbook.name}`;
      refreshSuggestions(workbook);
    }).catch(error => notice(error.message, true));
    readWorkbookSchema().then(schema=>{workbookSchema=schema;refreshSuggestions();}).catch(()=>{});
  }
}

async function init() {
  await Office.onReady();
  window.addEventListener('message', acceptDialogMessage);
  $('newChat').addEventListener('click', async () => {
    try {
      // New chat must create a durable blank binding even before the first question. Otherwise
      // the next ask's restore can silently revive the workbook's previous conversation.
      if (!state.workbookId) state.workbookId = await workbookKey();
      await api('/api/spreadsheet/conversation/clear', {host: 'excel', spreadsheet_id: state.workbookId});
      state.conversationId = null; state.history = []; state.turns = []; renderTurns(); notice(''); showHistory(false);
    } catch (error) { notice(error.message, true); }
  });
  $('previousChats').addEventListener('click', () => showHistory($('history').hidden));
  $('composer').addEventListener('submit', event => {
    event.preventDefault(); const question = $('question').value.trim();
    if (!question || !auth.currentUser || state.busy) return;
    ask(question).catch(error => notice(error.message, true));
  });
  $('question').addEventListener('keydown', event => {
    if (!state.busy && event.key === 'Enter' && !event.altKey && !event.shiftKey && !event.ctrlKey && !event.metaKey) {
      event.preventDefault();
      $('composer').requestSubmit();
    }
  });
  $('question').addEventListener('input', () => {
    $('question').style.height = 'auto'; $('question').style.height = `${Math.min($('question').scrollHeight, 120)}px`;
  });
  $('loadMore').addEventListener('click', () => loadHistory(state.nextPage).catch(error => notice(error.message, true)));
  $('signInMicrosoft').addEventListener('click', () => launchAuth('microsoft'));
  onAuthStateChanged(auth, user => {
    if (!user) { setBusy(true); openSignIn(); }
    else onSignedIn(user);
  });
}

init().catch(error => notice(error.message || 'Prereasoner could not start.', true));
