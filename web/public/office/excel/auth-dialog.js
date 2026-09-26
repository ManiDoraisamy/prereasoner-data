import {initializeApp} from 'https://www.gstatic.com/firebasejs/10.12.0/firebase-app.js';
import {getAuth, OAuthProvider, signInWithPopup} from 'https://www.gstatic.com/firebasejs/10.12.0/firebase-auth.js';
import {firebaseConfig} from '../../lib/config.js';
import {explainMicrosoftAuthError} from './auth-errors.js';

const app = initializeApp(firebaseConfig, 'prereasoner-excel-auth');
const auth = getAuth(app);
const status = document.getElementById('status');
const retry = document.getElementById('retry');

async function start() {
  retry.hidden = true;
  status.textContent = 'Connecting to your account…';
  const providerName = new URLSearchParams(location.search).get('provider');
  if (providerName !== 'microsoft') {
    status.textContent = 'This Excel add-in currently supports Microsoft sign-in only.';
    retry.hidden = false;
    return;
  }
  const provider = new OAuthProvider('microsoft.com');
  try {
    const result = await signInWithPopup(auth, provider);
    const credential = OAuthProvider.credentialFromResult(result);
    if (!credential) throw new Error('The identity provider did not return a sign-in credential.');
    await Office.onReady();
    Office.context.ui.messageParent(JSON.stringify({
      kind: 'pr-auth-result', provider: 'microsoft',
      idToken: credential.idToken || '', accessToken: credential.accessToken || ''
    }), {targetOrigin: location.origin});
    status.textContent = 'Signed in. You can close this window.';
  } catch (error) {
    status.textContent = explainMicrosoftAuthError(error);
    retry.hidden = false;
  }
}

retry.addEventListener('click', start);
start();
