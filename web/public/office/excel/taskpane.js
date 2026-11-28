import {initializeApp} from 'https://www.gstatic.com/firebasejs/10.12.0/firebase-app.js';
import {getAuth, OAuthProvider, onAuthStateChanged, signInWithCredential, getIdToken} from 'https://www.gstatic.com/firebasejs/10.12.0/firebase-auth.js';
import {getDatabase, ref, onValue} from 'https://www.gstatic.com/firebasejs/10.12.0/firebase-database.js';
import {firebaseConfig} from '../../lib/config.js';
import {readActiveSheet, readWorkbook, readWorkbookSchema, workbookKey} from './host.js';
import {credentialForDialog} from './auth-bridge.js';
import {explainMicrosoftAuthError} from './auth-errors.js';
import {streamResponse} from './turn-bridge.js';

const app = initializeApp(firebaseConfig, 'prereasoner-excel');
const auth = getAuth(app);
const database = getDatabase(app);
const $ = id => document.getElementById(id);
const renderer = window.PrereasonerTurnRenderer;
const state = {conversationId: null, history: [], turns: [], tables: [], workbookId: null, nextPage: null};
const suggestions = window.PrereasonerSuggestions.create({container: $('suggestions'),
  request: schema => api('/chat/suggestions', schema),
  ask: question => { if (auth.currentUser && !state.busy) ask(question).catch(error => notice(error.message, true)); }});
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

// `answered`: HTTP statuses the caller handles itself; their body comes back with its `status`.
async function api(path, body, answered = []) {
  const response = await fetch(path, {
    method: 'POST', headers: {'Content-Type': 'application/json', Authorization: `Bearer ${await token()}`},
    body: JSON.stringify(body)
  });
  const data = await response.json().catch(() => ({}));
  if (answered.includes(response.status)) return {...data, status: response.status};
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status}).`);
  return data;
}

// Upload once (2026-10-02): the workbook goes to the conversation before its first question and when it
// changed; a question names the stored workbook by its hash instead of sending every cell again.
async function fingerprint(tables) {
  const bytes = new TextEncoder().encode(JSON.stringify(tables.map(table => [table.name, table.data])));
  const hash = await crypto.subtle.digest('SHA-256', bytes);
  return Array.from(new Uint8Array(hash), byte => byte.toString(16).padStart(2, '0')).join('');
}

async function syncWorkbook(question, tables, print) {
  const synced = await api('/api/conversation/sync', {id: state.conversationId || '', question, tables});
  state.conversationId = synced.conversation_id || state.conversationId;
  state.sourceHash = synced.source_hash || '';
  state.syncedFingerprint = print;
}

// The workbook goes to its chat as soon as it is read, and the engine prepares it there (engine/prepared.py), so a
// question finds it ready: it was sent with the first question, which waited for the preparation it started
// (2026-10-30). The chat is kept for this workbook; it stays out of the conversations list until a question names
// it. A failure here is tried again, and reported, by the next question.
function uploadAtOpen(workbook) {
  state.uploading = (async () => {
    const print = await fingerprint(workbook.tables);
    if (!state.restored) await restore(workbook.tables, print);
    if (state.conversationId && state.sourceHash && print === state.syncedFingerprint) return;
    await syncWorkbook('', workbook.tables, print);
    await persist();
  })().catch(() => {}).finally(() => { state.uploading = null; });
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
  state.turns.forEach((turn, index) => {
    const analysisUrl = turn.conversationId ? `https://chat.prereasoner.com/reason/${encodeURIComponent(turn.conversationId)}` : '';
    const reasoningBody = renderer.renderReasoningTree(turn.reasoning || [], {
      renderStep: (step, index) => renderer.renderStepLink(step, index,
        analysisUrl ? {href: analysisUrl, title: 'Open this reasoning in Prereasoner'} : {})
    });
    const reasoningHtml = renderer.renderReasoningPanel({bodyHtml: reasoningBody, title: 'How this was calculated', analysisUrl});
    const assistantHtml = turn.error
      ? `<div class="turn-content"><div class="answer error" role="alert">${renderer.escapeHtml(turn.reply)}</div></div>`
      : renderer.renderAssistantTurn({reply: turn.reply, reasoningHtml,
        // The answer's mark, one of four levels, on screen with the answer; its Retry runs in place (retryTurn).
        // A turn still running shows its mark with no Retry.
        checkHtml: !renderer.renderCheck ? '' : turn.pending ? renderer.renderCheck(turn.check)
          : renderer.renderCheck(turn.check, `data-retry="${index}"`, turn.retrying),
        afterHtml: renderer.renderResultLink(turn.reply, analysisUrl)});
    thread.insertAdjacentHTML('beforeend', renderer.renderTurn({question: turn.question, assistantHtml}));
  });
  thread.scrollTop = thread.scrollHeight;
}

function snapshot() {
  // A retry still out is not a saved turn's state, and neither is a question still being answered: a retry that
  // comes back meanwhile saves the conversation without that question's placeholder and its unanswered message.
  const asking = state.turns.some(turn => turn.pending);
  const turns = state.turns.filter(turn => !turn.pending).slice(-24).map(({retrying, ...turn}) => turn);
  const history = state.history.slice();
  if (asking && history.length && history[history.length - 1].role === 'user') history.pop();
  return {client: 'prereasoner-excel-addon', version: 1, turns, history: history.slice(-24)};
}

async function persist() {
  if (!state.conversationId || !state.workbookId) return;
  await api('/api/spreadsheet/conversation/state', {
    host: 'excel', spreadsheet_id: state.workbookId, conversation_id: state.conversationId, state: snapshot()
  });
}

async function restore(tables, print) {
  if (!state.workbookId) state.workbookId = await workbookKey();
  const response = await api('/api/spreadsheet/conversation/restore', {
    host: 'excel', spreadsheet_id: state.workbookId, tables
  });
  state.conversationId = response.conversation_id || null;
  // Unless the workbook changed since, the conversation already stores it: questions name it by hash.
  state.sourceHash = state.conversationId && !response.source_changed ? response.source_hash || '' : '';
  state.syncedFingerprint = state.sourceHash ? print : '';
  state.restored = true;
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

// ``onLive(turn, engineRuns)``: called as the turn's stream changes, so the pane can show the reply as it is written.
function awaitStream(turnId, onLive) {
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
          if (onLive) onLive(latestTurn, engineRuns);
          finishIfReady();
        }, error => {
          clearTimeout(timer); unsubscribe(); reject(error);
        }));
      }
      if (onLive) onLive(latestTurn, engineRuns);
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

// Retry (engine/retry.py: Gemini 4 Neon writes the query): the turn's question asked again with retry, in the
// background. The turn's icon loads, the composer stays open, and the retry's answer takes the turn's place in view
// (owner, 2026-11-27: "never ever wait for neon to show engine's answer ... the icon can change into retry loading").
// It runs at once for an answer served very likely wrong, and on a click of the mark's Retry.
async function retryTurn(turn) {
  if (!turn || turn.retrying || turn.pending || turn.error || !turn.question || !renderer.revisedTurn) return;
  const was = {reply: turn.reply, check: turn.check}, conversation = state.conversationId;
  const repaint = () => { const thread = $('thread'), top = thread.scrollTop; renderTurns(); thread.scrollTop = top; };
  let retried = null;
  turn.retrying = true;
  repaint();
  try {
    const response = await api('/chat', {
      message: turn.question, history: renderer.historyBefore(state.history, turn.question, turn.reply),
      conversation_id: conversation, source_hash: state.sourceHash, active_sheet: await readActiveSheet(), retry: true
    }, [409]);
    if (response && response.reply && !response.error) {
      retried = {reply: response.reply, check: renderer.turnCheck(response.traces), reasoning: mapReasoning(response)};
    }
  } catch (_) { /* the turn keeps its answer; its mark says the retry did not finish */ }
  turn.retrying = false;
  if (state.conversationId !== conversation || !state.turns.includes(turn)) return;   // a new chat by now
  const now = renderer.revisedTurn(was, retried);
  turn.check = now.check;
  if (now.replaced) {
    turn.reply = now.reply;
    turn.reasoning = retried.reasoning;
    state.history = renderer.revisedHistory(state.history, turn.question, was.reply, now.reply);
  }
  repaint();
  persist().catch(() => {});
}

async function ask(question) {
  setBusy(true);
  let baseHistory = state.history.slice();
  try {
    const workbook = await currentWorkbook();
    state.tables = workbook.tables;
    refreshSuggestions(workbook);
    $('sheetCount').textContent = `${workbook.tables.length} tab${workbook.tables.length === 1 ? '' : 's'} · ${workbook.name}`;
    const print = await fingerprint(workbook.tables);
    if (state.uploading) await state.uploading;
    if (!state.restored) await restore(workbook.tables, print);
    if (!state.conversationId || !state.sourceHash || print !== state.syncedFingerprint) {
      await syncWorkbook(question, workbook.tables, print);
    }
    baseHistory = state.history.slice();
    const turnId = crypto.randomUUID().replaceAll('-', '');
    state.history.push({role: 'user', content: question});
    state.turns.push({question, reply: 'Working on your question…', reasoning: [], pending: true});
    renderTurns();
    // The reply is the engine's answer as soon as it is written, and the last engine call's mark is on screen with
    // it; the finished turn replaces this one.
    const streamResult = awaitStream(turnId, (run, runs) => {
      const pending = state.turns[state.turns.length - 1];
      if (!pending || !pending.pending || typeof run.reply !== 'string' || !run.reply) return;
      const calls = run.calls && typeof run.calls === 'object' ? Object.values(run.calls) : [];
      const last = calls.length ? runs[calls[calls.length - 1].jobId] : null;
      const check = (last && last.check) || null;
      if (pending.reply === run.reply && JSON.stringify(pending.check || null) === JSON.stringify(check)) return;
      pending.reply = run.reply;
      pending.check = check;
      renderTurns();
    });
    let response;
    const activeSheet = await readActiveSheet();
    const chat = () => api('/chat', {
      message: question, history: baseHistory,
      conversation_id: state.conversationId, source_hash: state.sourceHash, active_sheet: activeSheet, turnId
    }, [409]);
    try {
      response = await chat();
      if (response.status === 409) {                         // the stored workbook was replaced since
        await syncWorkbook(question, workbook.tables, print);
        response = await chat();
        if (response.status === 409) throw new Error('The workbook keeps changing. Ask again in a moment.');
      }
    } catch (error) {
      if (state.conversationId && /conversation not found/i.test(error.message)) {
        // Deleted elsewhere (orchestrator/server.py answers 404): ask once more as a new chat, keeping
        // the thread and the history the assistant reads. The workbook goes to the new chat first.
        state.conversationId = null;
        state.sourceHash = '';
        await syncWorkbook(question, workbook.tables, print);
        response = await chat();
      } else if (error instanceof TypeError || /timed out|network|failed to fetch/i.test(error.message)) {
        response = await streamResult.promise;
      } else { streamResult.cancel(); throw error; }
    }
    streamResult.cancel();
    if (response.error) throw new Error(response.error);
    state.conversationId = response.conversation_id || state.conversationId;
    state.turns[state.turns.length - 1] = {
      question, reply: response.reply || 'No answer was returned.', reasoning: mapReasoning(response),
      conversationId: state.conversationId,
      check: renderer.turnCheck ? renderer.turnCheck(response.traces) : null
    };
    state.history.push({role: 'assistant', content: response.reply || ''});
    renderTurns();
    notice('');
    // A failed save loses nothing on screen; the next answer saves the whole conversation again.
    persist().catch(() => {});
    // An answer very likely wrong: its retry starts now, in the background; the composer is open.
    const answered = state.turns[state.turns.length - 1];
    if (renderer.owesRetry && renderer.owesRetry(answered.check)) retryTurn(answered);
  } catch (error) {
    // A failed question stays in the thread with its reason; it was dropped, and lost entirely when
    // the user had typed a new draft meanwhile (2026-10-04).
    const failed = {question, reply: error.message || 'Could not answer that question.', error: true};
    if (state.turns[state.turns.length - 1]?.pending) state.turns[state.turns.length - 1] = failed;
    else state.turns.push(failed);
    state.history = state.history.slice(0, baseHistory.length);   // what came before, with any retry's answer since
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
      uploadAtOpen(workbook);
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
  $('thread').addEventListener('click', event => {        // an answer mark's Retry: the turn's question, retried in place
    const button = event.target.closest('[data-retry]');
    if (button && auth.currentUser) retryTurn(state.turns[Number(button.dataset.retry)]);
  });
  $('composer').addEventListener('submit', event => {
    event.preventDefault(); const question = $('question').value.trim();
    if (!question || !auth.currentUser || state.busy) return;
    $('question').value = '';
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
