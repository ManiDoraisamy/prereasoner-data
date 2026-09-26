import {initializeApp} from 'https://www.gstatic.com/firebasejs/10.12.0/firebase-app.js';
import {getAuth, OAuthProvider, onAuthStateChanged, signInWithCredential, getIdToken} from 'https://www.gstatic.com/firebasejs/10.12.0/firebase-auth.js';
import {getDatabase, ref, onValue} from 'https://www.gstatic.com/firebasejs/10.12.0/firebase-database.js';
import {firebaseConfig} from '../../lib/config.js';
import {readWorkbook, workbookKey} from './host.js';
import {credentialForDialog} from './auth-bridge.js';
import {explainMicrosoftAuthError} from './auth-errors.js';
import {streamResponse} from './turn-bridge.js';

const app = initializeApp(firebaseConfig, 'prereasoner-excel');
const auth = getAuth(app);
const database = getDatabase(app);
const $ = id => document.getElementById(id);
const renderer = window.PrereasonerTurnRenderer;
const state = {conversationId: null, history: [], turns: [], tables: [], workbookId: null, nextPage: null};

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
  $('question').disabled = busy || !auth.currentUser;
  $('send').disabled = busy || !auth.currentUser;
}

function renderTurns() {
  const thread = $('thread');
  thread.querySelectorAll('.turn-pair,.empty').forEach(el => el.remove());
  if (!state.turns.length) {
    const empty = document.createElement('div'); empty.id = 'empty'; empty.className = 'empty';
    empty.textContent = 'Ask a question about this workbook.'; thread.append(empty); return;
  }
  for (const turn of state.turns) {
    const reasoningBody = renderer.renderReasoningTree(turn.reasoning || []);
    const analysisUrl = turn.conversationId ? `https://chat.prereasoner.com/reason/${encodeURIComponent(turn.conversationId)}` : '';
    const reasoningHtml = renderer.renderReasoningPanel({bodyHtml: reasoningBody, title: 'How this was calculated', analysisUrl});
    const assistantHtml = renderer.renderAssistantTurn({reply: turn.reply, reasoningHtml});
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
    const views = trace?.views || trace?.engine?.views || [];
    return (Array.isArray(views) ? views : []).map((view, vi) => ({
      index: ti * 100 + vi, label: view.title || view.label || view.operation || `Calculation ${vi + 1}`,
      detail: view.description || view.question || [view.op, Array.isArray(view.rows) ? `${view.rows.length} rows` : ''].filter(Boolean).join(' · '), sectionId: view.section || view.section_id || '',
      sectionLabel: view.section_label || '', sectionQuestion: view.section_question || '',
      sectionInputs: view.section_inputs || [], inputs: view.inputs || [], isOutput: Boolean(view.is_output)
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
  let baseHistory = state.history.slice();
  try {
    const workbook = await readWorkbook();
    state.tables = workbook.tables;
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
      if (error instanceof TypeError || /timed out|network|failed to fetch/i.test(error.message)) response = await streamResult.promise;
      else { streamResult.cancel(); throw error; }
    }
    streamResult.cancel();
    if (response.error) throw new Error(response.error);
    state.conversationId = response.conversation_id || state.conversationId;
    state.turns[state.turns.length - 1] = {
      question, reply: response.reply || 'No answer was returned.', reasoning: mapReasoning(response),
      conversationId: state.conversationId
    };
    state.history.push({role: 'assistant', content: response.reply || ''});
    await persist();
    $('question').value = '';
    renderTurns();
    notice('');
  } catch (error) {
    if (state.turns[state.turns.length - 1]?.pending) state.turns.pop();
    state.history = baseHistory;
    renderTurns(); notice(error.message || 'Could not answer that question.', true);
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
  } catch (error) { openSignIn(explainMicrosoftAuthError(error)); }
}

function launchAuth(provider) {
  showAuthError('');
  Office.context.ui.displayDialogAsync(`${location.origin}/office/excel/auth-dialog.html?provider=${provider}`, {
    height: 55, width: 35, displayInIframe: false
  }, result => {
    if (result.status !== Office.AsyncResultStatus.Succeeded) {
      showAuthError('Microsoft sign-in could not open. Please try again.'); return;
    }
    const dialog = result.value;
    dialog.addEventHandler(Office.EventType.DialogMessageReceived, acceptDialogMessage);
    dialog.addEventHandler(Office.EventType.DialogEventReceived, arg => {
      if (arg.error === 12006) showAuthError('Sign-in was closed before it finished.');
    });
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
    if (!question || !auth.currentUser) return;
    ask(question).catch(error => notice(error.message, true));
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
