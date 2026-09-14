const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const root = path.resolve(__dirname, '..');
const source = fs.readFileSync(path.join(root, 'Code.js'), 'utf8');
const sidebar = fs.readFileSync(path.join(root, 'Sidebar.html'), 'utf8');
const previous = fs.readFileSync(path.join(root, 'Previous.html'), 'utf8');
const manifest = JSON.parse(fs.readFileSync(path.join(root, 'appsscript.json'), 'utf8'));

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
      resolves: [{column: 'country', table: 'orders'}],
      views: [{op: 'filter', label: 'France orders', columns: ['amount'], rows: [[120], [80]]}],
      answer: {columns: ['total'], rows: [[200]]}
    }
  }]
});
assert.strictEqual(reasoning.steps.length, 2);
assert.strictEqual(reasoning.steps[0].label, 'Resolve country');
assert.strictEqual(reasoning.steps[1].label, 'France orders');
assert.deepStrictEqual(Array.from(reasoning.result.columns), ['total']);
assert.strictEqual(reasoning.result.rows[0][0], '200');

assert(sidebar.includes('Ask about this spreadsheet'));
assert(sidebar.includes('Reasoning steps</summary>'));
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
assert(sidebar.includes('https://ssl.gstatic.com/docs/script/css/add-ons1.css'));
assert(!sidebar.includes('linear-gradient'));
assert(!sidebar.includes('class="legal"'));
assert(sidebar.includes('aria-label="Send"'));
assert(sidebar.includes("contextEl.hidden = true"));
assert(sidebar.includes("result.rows.length === 1 && headers.length === 1"));
assert(manifest.urlFetchWhitelist.includes('https://chat.prereasoner.com/'));
assert(manifest.urlFetchWhitelist.includes('https://identitytoolkit.googleapis.com/'));
assert(source.includes("addItem('Ask a question', 'showSidebar')"));
assert(source.includes("addItem('Previous conversations', 'showPreviousConversations')"));
assert(source.includes('/api/conversations?limit=50'));
assert(source.includes('/api/conversation/sync'));
assert(source.includes("spreadsheetId: spreadsheet.getId()"));
assert(source.includes('/api/spreadsheet/conversation/restore'));
assert(source.includes('/api/spreadsheet/conversation/state'));
assert(source.includes('/api/spreadsheet/conversation/clear'));
assert(source.includes('function restorePrereasonerSheetConversation()'));
assert(source.includes('function savePrereasonerSheetConversation(request)'));
assert(source.includes('function clearPrereasonerSheetConversation()'));
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
assert(sidebar.includes("turns: state.turns.slice(-24)"));

console.log('Sheets add-on tests passed.');
