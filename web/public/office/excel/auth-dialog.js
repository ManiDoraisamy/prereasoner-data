import {initializeApp} from 'https://www.gstatic.com/firebasejs/10.12.0/firebase-app.js';
import {getAuth, GoogleAuthProvider, OAuthProvider, signInWithPopup} from 'https://www.gstatic.com/firebasejs/10.12.0/firebase-auth.js';
import {firebaseConfig} from '../../lib/config.js';

const app = initializeApp(firebaseConfig, 'prereasoner-excel-auth');
const auth = getAuth(app);
const status = document.getElementById('status');
const retry = document.getElementById('retry');

function explain(error) {
  const code = String(error?.code || '');
  if (code.includes('operation-not-allowed')) return 'This sign-in provider is not enabled yet. Please contact Prereasoner support.';
  if (code.includes('popup-blocked')) return 'The sign-in window was blocked. Allow popups for chat.prereasoner.com, then try again.';
  if (code.includes('unauthorized-domain')) return 'This sign-in page is not authorized in Firebase Authentication yet.';
  return error?.message || 'Sign-in could not be completed. Please try again.';
}

async function start() {
  retry.hidden = true;
  status.textContent = 'Connecting to your account…';
  const providerName = new URLSearchParams(location.search).get('provider');
  let provider;
  if (providerName === 'microsoft') {
    provider = new OAuthProvider('microsoft.com');
    provider.setCustomParameters({prompt: 'select_account'});
  } else {
    provider = new GoogleAuthProvider();
    provider.setCustomParameters({prompt: 'select_account'});
  }
  try {
    const result = await signInWithPopup(auth, provider);
    const credential = providerName === 'microsoft'
      ? OAuthProvider.credentialFromResult(result)
      : GoogleAuthProvider.credentialFromResult(result);
    if (!credential) throw new Error('The identity provider did not return a sign-in credential.');
    await Office.onReady();
    Office.context.ui.messageParent(JSON.stringify({
      kind: 'pr-auth-result', provider: providerName === 'microsoft' ? 'microsoft' : 'google',
      idToken: credential.idToken || '', accessToken: credential.accessToken || ''
    }), {targetOrigin: location.origin});
    status.textContent = 'Signed in. You can close this window.';
  } catch (error) {
    status.textContent = explain(error);
    retry.hidden = false;
  }
}

retry.addEventListener('click', start);
start();
