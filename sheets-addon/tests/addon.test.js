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
assert(sidebar.includes('Reasoning steps for'));
assert(sidebar.includes('https://ssl.gstatic.com/docs/script/css/add-ons1.css'));
assert(!sidebar.includes('linear-gradient'));
assert(sidebar.includes('chat.prereasoner.com/privacy'));
assert(source.includes("addItem('Ask a question', 'showSidebar')"));
assert(source.includes("addItem('Previous conversations', 'showPreviousConversations')"));
assert(source.includes('/api/conversations?limit=50'));
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

console.log('Sheets add-on tests passed.');
