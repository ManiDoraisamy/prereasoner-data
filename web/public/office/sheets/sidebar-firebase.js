import {hostIdToken, signInWithHostToken} from 'https://chat.prereasoner.com/lib/firebase-init.js';
      window.__prereasonerLive = {signIn: signInWithHostToken, idToken: hostIdToken};
      window.dispatchEvent(new Event('prereasoner-live'));
