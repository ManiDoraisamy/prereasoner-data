import assert from 'node:assert/strict';
import {credentialForDialog} from '../public/office/excel/auth-bridge.js';
import {explainMicrosoftAuthError} from '../public/office/excel/auth-errors.js';

const calls = [];
class OAuthProvider {
  constructor(id) { this.id = id; }
  credential(options) { calls.push(['microsoft', this.id, options]); return 'microsoft-credential'; }
  static credentialFromJSON(value) { calls.push(['from-json', value]); return {providerId: value.providerId, value}; }
}
const serialized = {providerId: 'microsoft.com', signInMethod: 'microsoft.com',
  oauthIdToken: 'ms-id', oauthAccessToken: 'ms-access', pendingToken: 'pending'};
assert.deepEqual(credentialForDialog({provider: 'microsoft', credential: serialized},
  {OAuthProvider}), {providerId: 'microsoft.com', value: serialized});
assert.deepEqual(calls.pop(), ['from-json', serialized]);
assert.equal(credentialForDialog({provider: 'microsoft', idToken: 'ms-id', accessToken: 'ms-access'},
  {OAuthProvider}), 'microsoft-credential');
assert.deepEqual(calls.pop(), ['microsoft', 'microsoft.com', {idToken: 'ms-id', accessToken: 'ms-access'}]);
assert.throws(() => credentialForDialog({provider: 'microsoft'}, {OAuthProvider}),
  /no sign-in token/);
assert.throws(() => credentialForDialog({provider: 'google', idToken: 'g-id'},
  {OAuthProvider}), /not recognized/);
assert.throws(() => credentialForDialog({provider: 'other', accessToken: 'token'}, {OAuthProvider}),
  /not recognized/);
assert.match(explainMicrosoftAuthError({code: 'auth/operation-not-allowed'}), /not enabled/);
assert.match(explainMicrosoftAuthError({code: 'auth/account-exists-with-different-credential'}), /not merged automatically/);
assert.match(explainMicrosoftAuthError({code: 'auth/popup-blocked'}), /Allow pop-ups/);

console.log('Excel auth bridge: 7 checks passed');
