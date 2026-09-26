import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';

const root = new URL('../public/office/excel/', import.meta.url);
const [html, pane, auth, bridge, css] = await Promise.all([
  readFile(new URL('taskpane.html', root), 'utf8'),
  readFile(new URL('taskpane.js', root), 'utf8'),
  readFile(new URL('auth-dialog.js', root), 'utf8'),
  readFile(new URL('auth-bridge.js', root), 'utf8'),
  readFile(new URL('taskpane.css', root), 'utf8')
]);

assert.match(html, /id="authPanel"/);
assert.doesNotMatch(html, /<dialog|Continue with Google|signInGoogle/);
assert.doesNotMatch(pane, /GoogleAuthProvider|signInGoogle|authDialog/);
assert.doesNotMatch(auth, /GoogleAuthProvider|select_account/);
assert.doesNotMatch(bridge, /GoogleAuthProvider|google/);
assert.match(css, /\.auth-panel/);
console.log('Excel sign-in UX: 6 checks passed');
