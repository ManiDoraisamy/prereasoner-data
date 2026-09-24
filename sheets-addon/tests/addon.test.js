const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const root = path.resolve(__dirname, '..');
const source = fs.readFileSync(path.join(root, 'Code.js'), 'utf8');
const sidebar = fs.readFileSync(path.join(root, 'Sidebar.html'), 'utf8');
const previous = fs.readFileSync(path.join(root, 'Previous.html'), 'utf8');
const manifest = JSON.parse(fs.readFileSync(path.join(root, 'appsscript.json'), 'utf8'));
const turnRenderer = fs.readFileSync(path.join(root, '..', 'web', 'public', 'lib', 'turn-renderer.js'), 'utf8');

const context = {
  console,
  Utilities: {formatDate: () => '2026-09-13T00:00:00'}
};
vm.createContext(context);
vm.runInContext(source, context);

assert.strictEqual(context.csvCell_('Sherlock, Holmes', 'UTC'), '"Sherlock, Holmes"');
assert.strictEqual(context.csvCell_('He said "hello"', 'UTC'), '"He said ""hello"""');
assert.deepStrictEqual(
  Array.from(context.normalizeHeaders_(['order ID', '', 'Order ID'])),
  ['order ID', 'column_2', 'Order ID_2']
);

const reasoning = context.extractReasoning_({
  traces: [{
    question: 'total amount in France',
    engine: {
      analysis: {analysis_id: 'a_11111111111111111111111111111111', slug: 'france_total', revision: 1, display_name: 'France total'},
      resolves: [{column: 'country', table: 'orders'}],
      views: [{op: 'filter', label: 'France orders', columns: ['amount'], rows: [[120], [80]],
        section: 'france', section_label: 'France orders', section_question: 'Orders in France', section_inputs: [], is_output: true}],
      answer: {columns: ['total'], rows: [[200]]}
    }
  }]
});
assert.strictEqual(reasoning.steps.length, 2);
assert.strictEqual(reasoning.steps[0].label, 'Resolve country');
assert.strictEqual(reasoning.steps[1].label, 'Filtered');
assert.strictEqual(reasoning.steps[1].sectionLabel, 'France orders');
assert.strictEqual(reasoning.analysis.display_name, 'France total');
assert.deepStrictEqual(Array.from(reasoning.result.columns), ['total']);
assert.strictEqual(reasoning.result.rows[0][0], '200');

assert(sidebar.includes('Ask about this spreadsheet'));
assert(sidebar.includes('visible, non-empty tabs in this spreadsheet'));
assert(sidebar.includes('not used to train generalized AI models'));
assert(sidebar.includes('https://chat.prereasoner.com/privacy'));
assert(sidebar.includes('Reasoning steps for '));
assert(turnRenderer.includes('Open full analysis'));
assert(sidebar.includes('turn-renderer.js'));
assert(sidebar.includes('getPrereasonerLiveSession'));
assert(sidebar.includes('startLiveTurn'));
assert(sidebar.includes('Reading your sheet…'));
assert(sidebar.includes('Planning the analysis…'));
assert(sidebar.includes('Calculating and verifying…'));
assert(sidebar.includes('complex questions can take about a minute'));
assert(sidebar.includes('+ New chat'));
assert(sidebar.includes('.new-chat {'));
assert(sidebar.includes('display: inline-flex;'));
assert(sidebar.includes('align-items: center;'));
assert(sidebar.includes('justify-content: center;'));
assert(sidebar.includes('line-height: 14px;'));
assert(sidebar.includes('background: transparent;'));
assert(sidebar.includes('.answer.error'));
assert(sidebar.includes('questionEl.value = text;'));
assert(sidebar.includes("pending.setAttribute('role', 'alert')"));
assert(sidebar.includes('questionEl.scrollTop = 0;'));
assert(sidebar.includes('if (questionEl.value) questionEl.scrollTop = 0;'));
assert(sidebar.includes("questionEl.value = '';"));
assert(sidebar.includes('Answer is stale. Recalculate'));
assert(sidebar.includes('window.setInterval(checkSync, 15000)'));
assert(sidebar.includes('state.syncedFingerprint && context.fingerprint !== state.syncedFingerprint'));
assert(sidebar.includes('syncedFingerprint: state.syncedFingerprint'));
assert(sidebar.includes('https://ssl.gstatic.com/docs/script/css/add-ons1.css'));
assert(!sidebar.includes('linear-gradient'));
assert(!sidebar.includes('class="legal"'));
assert(sidebar.includes('aria-label="Send"'));
assert(sidebar.includes("contextEl.hidden = true"));
assert(sidebar.includes("result.rows.length === 1 && headers.length === 1"));
assert(manifest.urlFetchWhitelist.includes('https://chat.prereasoner.com/'));
assert(manifest.urlFetchWhitelist.includes('https://prereasoner-chat-271377281957.us-central1.run.app/'));
assert(manifest.urlFetchWhitelist.includes('https://identitytoolkit.googleapis.com/'));
assert(source.includes("addItem('Ask a question', 'showSidebar')"));
assert(source.includes("addItem('Previous conversations', 'showPreviousConversations')"));
assert(source.includes("PREREASONER_CHAT_URL = 'https://prereasoner-chat-271377281957.us-central1.run.app/chat'"));
assert(source.includes('/api/conversations?limit=50'));
assert(source.includes('/api/conversation/sync'));
assert(source.includes("spreadsheetId: spreadsheet.getId()"));
assert(source.includes('/api/spreadsheet/conversation/restore'));
assert(source.includes('/api/spreadsheet/conversation/state'));
assert(source.includes('/api/spreadsheet/conversation/clear'));
assert(source.includes('function restorePrereasonerSheetConversation()'));
assert(source.includes('function savePrereasonerSheetConversation(request)'));
assert(source.includes('function clearPrereasonerSheetConversation()'));
assert(source.includes('function getPrereasonerLiveSession()'));
assert(source.includes('payload.turnId = turnId'));
assert(source.includes("payload.analysis = {action: 'modify'"));
assert(source.includes('PREREASONER_RTDB_URL'));
assert(source.includes("source: {kind: 'google-sheets-addon'}"));
assert(!source.includes('PropertiesService'));
assert(previous.includes('Previous conversations'));
assert(previous.includes('REASON_BASE'));
assert.deepStrictEqual(manifest.dependencies, undefined);
assert(manifest.oauthScopes.includes('openid'));
assert(manifest.oauthScopes.includes('https://www.googleapis.com/auth/userinfo.profile'));
assert(manifest.oauthScopes.includes('https://www.googleapis.com/auth/spreadsheets.currentonly'));
assert(!manifest.oauthScopes.includes('https://www.googleapis.com/auth/spreadsheets'));

const removedCredentialMarker = ['PRIVATE', 'KEY'].join('_');
assert(!source.includes(removedCredentialMarker));
assert(!source.includes('Promptrepo'));
assert(!sidebar.includes('Promptrepo'));
assert(sidebar.includes("callServer('restorePrereasonerSheetConversation')"));
assert(sidebar.includes("callServer('savePrereasonerSheetConversation'"));
assert(sidebar.includes("callServer('clearPrereasonerSheetConversation')"));
assert(sidebar.includes("var source = await callServer('syncPrereasonerConversation'"));
assert(sidebar.includes("turns: state.turns.slice(-24)"));
assert(turnRenderer.includes('renderMarkdown'));
assert(turnRenderer.includes('renderReasoningTree'));
assert(source.includes("ADDON_NAME = 'Prereasoner'"));
assert(source.includes(".setTitle(ADDON_NAME)"));

console.log('Sheets add-on tests passed.');
