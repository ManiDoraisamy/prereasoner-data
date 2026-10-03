import {signInWithHostToken} from 'https://chat.prereasoner.com/lib/firebase-init.js';
      window.__prereasonerLive = {signIn: signInWithHostToken};
      window.dispatchEvent(new Event('prereasoner-live'));
