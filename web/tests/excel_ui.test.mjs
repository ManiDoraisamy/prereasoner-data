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
  readFile(new URL('../../../../sheets-addon/Sidebar.html', root), 'utf8')
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
assert.match(pane, /event\.key === 'Enter' && !event\.altKey && !event\.shiftKey/);
assert.match(sheetsSidebar, /event\.key === 'Enter' && !event\.altKey && !event\.shiftKey/);
assert.match(sharedCss, /\.turn-answer\.answer/);
assert.match(webPage, /lib\/conversation\.css/);
assert.match(sheetsSidebar, /lib\/conversation\.css/);
console.log('Excel and shared conversation UI: 15 checks passed');
