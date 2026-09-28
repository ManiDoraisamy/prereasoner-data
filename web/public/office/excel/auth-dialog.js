import {initializeApp} from 'https://www.gstatic.com/firebasejs/10.12.0/firebase-app.js';
import {getAuth, OAuthProvider, getRedirectResult, signInWithRedirect} from 'https://www.gstatic.com/firebasejs/10.12.0/firebase-auth.js';
import {firebaseConfig} from '../../lib/config.js';
import {explainMicrosoftAuthError} from './auth-errors.js';

const app = initializeApp(firebaseConfig, 'prereasoner-excel-auth');
const auth = getAuth(app);
const status = document.getElementById('status');
const retry = document.getElementById('retry');

async function sendCredential(result) {
  const credential = OAuthProvider.credentialFromResult(result);
  if (!credential) throw new Error('The identity provider did not return a sign-in credential.');
  if (!window.Office?.context?.ui?.messageParent) {
    status.textContent = 'Microsoft sign-in succeeded. Return to Excel to finish connecting.';
    retry.hidden = true;
    return;
  }
  await Office.onReady();
  Office.context.ui.messageParent(JSON.stringify({
    kind: 'pr-auth-result', provider: 'microsoft',
    credential: credential.toJSON()
  }), {targetOrigin: location.origin});
  status.textContent = 'Signed in. You can close this window.';
  retry.hidden = true;
}

async function start() {
  retry.disabled = true;
  status.textContent = 'Connecting to your account…';
  try {
    const provider = new OAuthProvider('microsoft.com');
    await signInWithRedirect(auth, provider);
  } catch (error) {
    status.textContent = explainMicrosoftAuthError(error);
    retry.textContent = 'Try again';
    retry.disabled = false;
  }
}

retry.addEventListener('click', () => start().catch(error => {
  status.textContent = explainMicrosoftAuthError(error);
  retry.textContent = 'Try again';
  retry.disabled = false;
}));

async function finishRedirect() {
  const providerName = new URLSearchParams(location.search).get('provider');
  if (providerName !== 'microsoft') {
    status.textContent = 'This Excel add-in currently supports Microsoft sign-in only.';
    retry.hidden = true;
    return;
  }
  try {
    const result = await getRedirectResult(auth);
    if (result) await sendCredential(result);
    else status.textContent = 'Continue securely with the Microsoft account you use for Prereasoner.';
  } catch (error) {
    status.textContent = explainMicrosoftAuthError(error);
    retry.textContent = 'Try again';
    retry.disabled = false;
  }
}

finishRedirect().catch(error => {
  status.textContent = explainMicrosoftAuthError(error);
  retry.textContent = 'Try again';
  retry.disabled = false;
});
