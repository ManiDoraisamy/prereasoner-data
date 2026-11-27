import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';

const root = new URL('../public/office/excel/', import.meta.url);
const [html, pane, auth, bridge, css, sharedCss, webPage, sheetsSidebar] = await Promise.all([
  readFile(new URL('taskpane.html', root), 'utf8'),
  readFile(new URL('taskpane.js', root), 'utf8'),
  readFile(new URL('auth-dialog.js', root), 'utf8'),
  readFile(new URL('auth-bridge.js', root), 'utf8'),
  readFile(new URL('taskpane.css', root), 'utf8'),
  readFile(new URL('../../lib/conversation.css', root), 'utf8'),
  readFile(new URL('../../reason.html', root), 'utf8'),
  Promise.all(['../sheets/sidebar.html','../sheets/sidebar.js'].map(file=>readFile(new URL(file,root),'utf8'))).then(parts=>parts.join('\n'))
]);

assert.match(html, /id="authPanel"/);
assert.doesNotMatch(html, /<dialog|Continue with Google|signInGoogle/);
assert.doesNotMatch(pane, /GoogleAuthProvider|signInGoogle|authDialog/);
assert.doesNotMatch(auth, /GoogleAuthProvider|select_account/);
assert.doesNotMatch(bridge, /GoogleAuthProvider|google/);
assert.match(auth, /signInWithRedirect\(auth, provider\)/);
assert.match(auth, /getRedirectResult\(auth\)/);
assert.match(pane, /12007[\s\S]*sign-in window is already open/);
assert.match(css, /\.auth-panel/);
assert.match(html, /lib\/conversation\.css/);
assert.match(html, /class="topline-actions"/);
assert.match(pane, /renderer\.stepsFromViews/);
assert.match(pane, /renderer\.renderStepLink/);
// A long answer links to all its rows in both add-ons, under the answer rather than in the collapsed panel.
assert.match(pane, /afterHtml: renderer\.renderResultLink\(turn\.reply, analysisUrl\)/);
assert.match(sheetsSidebar, /afterHtml: R\.renderResultLink\(turn\.reply, analysisUrl\(turn\)\)/);
assert.match(pane, /event\.key === 'Enter' && !event\.altKey && !event\.shiftKey/);
assert.match(sheetsSidebar, /event\.key === 'Enter' && !event\.altKey && !event\.shiftKey/);
assert.match(sharedCss, /\.turn-answer\.answer/);
assert.match(webPage, /lib\/conversation\.css/);
assert.match(sheetsSidebar, /lib\/conversation\.css/);
// The owner's rule (CLAUDE.md, privacy experience): the add-on surfaces carry no privacy link, notice or
// text. The install consent screen and the listing link /privacy; an in-sidebar notice hurt onboarding.
const shown = text => text.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '').replace(/<!--[\s\S]*?-->/g, '');
const addonSources = await Promise.all(['../sheets/sidebar.html', '../sheets/sidebar.js', '../sheets/boot.js',
  '../sheets/previous.html', '../sheets/previous.js', 'taskpane.html', 'taskpane.js', '../../lib/sidebar-suggestions.js',
  '../../../../sheets-addon/Sidebar.html', '../../../../sheets-addon/Previous.html']
  .map(file => readFile(new URL(file, root), 'utf8').then(text => [file, text])));
for (const [file, text] of addonSources) {
  assert.doesNotMatch(shown(text), /privacy/i, file + ' must carry no privacy link, notice or text');
}
// Upload once (2026-10-02): the workbook goes to the conversation when it changes, and a question names it.
assert.match(pane, /api\('\/api\/conversation\/sync', \{id: state\.conversationId \|\| '', question, tables\}\)/);
assert.match(pane, /api\('\/chat', \{\s*message: question, history: baseHistory,\s*conversation_id: state\.conversationId, source_hash: state\.sourceHash, active_sheet: activeSheet, turnId\s*\}, \[409\]\)/);
// An answer's mark (the engine's check, one of four levels) is on screen with the answer. Its Retry runs in place, in
// the background (owner, 2026-11-27): the turn's question asked again with retry:true and what came before the turn,
// the composer never busy for it, at once for an answer served very likely wrong and on a click of the mark's Retry.
assert.match(pane, /check: renderer\.turnCheck \? renderer\.turnCheck\(response\.traces\) : null/);
assert.match(pane, /renderer\.renderCheck\(turn\.check, `data-retry="\$\{index\}"`, turn\.retrying\)/);
assert.match(pane, /if \(renderer\.owesRetry && renderer\.owesRetry\(answered\.check\)\) retryTurn\(answered\);/);
assert.match(pane, /if \(button && auth\.currentUser\) retryTurn\(state\.turns\[Number\(button\.dataset\.retry\)\]\);/);
const retry = /async function retryTurn\(turn\) \{[\s\S]*?\n\}\n/.exec(pane)[0];
assert.match(retry, /message: turn\.question, history: renderer\.historyBefore\(state\.history, turn\.question, turn\.reply\),/);
assert.match(retry, /retry: true/);
assert.match(retry, /renderer\.revisedTurn\(was, retried\)/);
assert.doesNotMatch(retry, /setBusy/, 'a retry never closes the composer');
assert.doesNotMatch(pane, /checking/, 'no answer waits for its mark');
assert.doesNotMatch(pane, /api\('\/chat', \{[^}]*tables/);
console.log('Excel and shared conversation UI: 25 checks passed');
