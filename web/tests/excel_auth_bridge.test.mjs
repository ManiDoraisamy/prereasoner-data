import assert from 'node:assert/strict';
import {credentialForDialog} from '../public/office/excel/auth-bridge.js';

const calls = [];
class OAuthProvider {
  constructor(id) { this.id = id; }
  credential(options) { calls.push(['microsoft', this.id, options]); return 'microsoft-credential'; }
}
const GoogleAuthProvider = {
  credential(idToken, accessToken) {
    calls.push(['google', idToken, accessToken]);
    return 'google-credential';
  }
};

assert.equal(credentialForDialog({provider: 'microsoft', idToken: 'ms-id', accessToken: 'ms-access'},
  {OAuthProvider, GoogleAuthProvider}), 'microsoft-credential');
assert.deepEqual(calls.pop(), ['microsoft', 'microsoft.com', {idToken: 'ms-id', accessToken: 'ms-access'}]);
assert.equal(credentialForDialog({provider: 'google', idToken: 'g-id'},
  {OAuthProvider, GoogleAuthProvider}), 'google-credential');
assert.deepEqual(calls.pop(), ['google', 'g-id', null]);
assert.throws(() => credentialForDialog({provider: 'microsoft'}, {OAuthProvider, GoogleAuthProvider}),
  /no sign-in token/);
assert.throws(() => credentialForDialog({provider: 'other', accessToken: 'token'},
  {OAuthProvider, GoogleAuthProvider}), /not recognized/);

console.log('Excel auth bridge: 4 checks passed');
