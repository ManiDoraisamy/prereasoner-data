// firebase-init.js — Firebase app init + auth + the live RTDB run subscription (ES module).
// Pages load it with <script type="module"> and it BRIDGES to the pages' classic inline
// scripts by publishing window.ensureToken / window.subscribeRun / window.__uid — the same
// contract the pages have always used. The config lives in lib/config.js (public identifiers).
import { initializeApp } from "https://www.gstatic.com/firebasejs/10.12.0/firebase-app.js";
import { getAuth, GoogleAuthProvider, signInWithRedirect, getRedirectResult, getIdToken } from "https://www.gstatic.com/firebasejs/10.12.0/firebase-auth.js";
import { getDatabase, ref, onValue, onChildAdded, off } from "https://www.gstatic.com/firebasejs/10.12.0/firebase-database.js";
import { firebaseConfig } from "./config.js";

export const app = initializeApp(firebaseConfig);
export const auth = getAuth(app);
export const db = getDatabase(app);

// A fresh Firebase ID token for the signed-in user (the engine verifies it server-side;
// the per-user Postgres schema = the verified Google sub).
window.ensureToken = () => getIdToken(auth.currentUser);

// Subscribe to the live reasoning trace the Cloud Run service streams to /runs/{uid}/{jobId}
// (decoupled from the 60s HTTP proxy timeout — a slow lazy Wikidata fill can exceed it).
// onChildAdded(resolve/views) fires once per slide/view as it completes; onValue tracks
// status/result/clarify/error. Returns an unsubscribe fn so the caller can detach on
// done / fallback. Reads are allowed only for uid === auth.uid (database.rules.json).
window.subscribeRun = (uid, jobId, cb) => {
  const base = ref(db, `runs/${uid}/${jobId}`);
  const convRef = ref(db, `runs/${uid}/${jobId}/conversation_id`);
  const statusRef = ref(db, `runs/${uid}/${jobId}/status`);
  const resolveRef = ref(db, `runs/${uid}/${jobId}/resolve`);
  const viewsRef = ref(db, `runs/${uid}/${jobId}/views`);
  const resultRef = ref(db, `runs/${uid}/${jobId}/result`);
  const clarifyRef = ref(db, `runs/${uid}/${jobId}/clarify`);
  const lowConfRef = ref(db, `runs/${uid}/${jobId}/low_confidence`);
  const presentRef = ref(db, `runs/${uid}/${jobId}/present`);
  const errorRef = ref(db, `runs/${uid}/${jobId}/error`);
  const questionRef = ref(db, `runs/${uid}/${jobId}/question`);
  const uConv = onValue(convRef, s => { const v = s.val(); if (v != null && cb.onConversation) cb.onConversation(v); });
  const uStatus = onValue(statusRef, s => { const v = s.val(); if (v != null && cb.onStatus) cb.onStatus(v); });
  const uResolve = onChildAdded(resolveRef, s => { if (cb.onResolve) cb.onResolve(s.key, s.val()); });
  const uView = onChildAdded(viewsRef, s => { if (cb.onView) cb.onView(s.key, s.val()); });
  const uResult = onValue(resultRef, s => { const v = s.val(); if (v && cb.onResult) cb.onResult(v); });
  const uQuestion = onValue(questionRef, s => { const v = s.val(); if (v != null && cb.onQuestion) cb.onQuestion(v); });
  const uClarify = onValue(clarifyRef, s => { const v = s.val(); if (v && cb.onClarify) cb.onClarify(v); });
  const uLowConf = onValue(lowConfRef, s => { const v = s.val(); if (v && cb.onLowConfidence) cb.onLowConfidence(); });
  const uPresent = onValue(presentRef, s => { const v = s.val(); if (v && cb.onPresent) cb.onPresent(); });
  const uError = onValue(errorRef, s => { const v = s.val(); if (v != null && cb.onError) cb.onError(v); });
  return () => { try { uConv(); uStatus(); uResolve(); uView(); uResult(); uQuestion(); uClarify(); uLowConf(); uPresent(); uError(); off(base); } catch(_){} };
};

// Google sign-in as a same-tab REDIRECT (no button, no popup blockers): completes a pending
// redirect if we are returning from Google, otherwise starts one. Resolves to the uid when
// signed in (also published as window.__uid), or null when the page is about to navigate away.
export async function ensureSignedIn(){
  // LOCAL DEV bypass (localhost only): sessionStorage.setItem('pr_test_auth','1') skips Google
  // sign-in and sends a dummy bearer token. Pair with AUTH_TEST_SUB on the engine, which then
  // skips token verification. When the flag is unset, ask the local dev server: the orchestrator
  // (localhost:8090) serves GET /config with authMode "test" when it proxies a local engine —
  // auto-bypass so the click-through from the home page just works. The Firebase Hosting
  // emulator has no /config (404) -> normal Google sign-in. No effect on any deployed origin.
  if (location.hostname === 'localhost' || location.hostname === '127.0.0.1') {
    let t = sessionStorage.getItem('pr_test_auth');
    if (t == null) {
      try {
        const c = await fetch('/config', { cache: 'no-store' }).then(r => r.ok ? r.json() : null);
        if (c && c.authMode === 'test') t = '1';
      } catch (_) {}
    }
    if (t) { window.ensureToken = async () => 'local-dev'; window.__uid = 'local-dev'; return 'local-dev'; }
  }
  let redirectErr = null;
  try { await getRedirectResult(auth); } catch (e) { redirectErr = e; }   // completes the sign-in when returning from Google
  await auth.authStateReady();
  if (!auth.currentUser) {
    // LOOP BREAKER: if we set the pending flag before redirecting and came back STILL signed
    // out, the redirect flow failed (cancelled, or the browser dropped the pending sign-in).
    // Never re-redirect in that state — throw so the page can show a retry UI instead of
    // bouncing the user to the Google account chooser forever.
    if (sessionStorage.getItem('pr_auth_pending')) {
      sessionStorage.removeItem('pr_auth_pending');
      throw new Error('Google sign-in did not complete' + (redirectErr ? ` (${redirectErr.code || redirectErr.message})` : '') + ' — please try again.');
    }
    sessionStorage.setItem('pr_auth_pending', '1');
    await signInWithRedirect(auth, new GoogleAuthProvider()); return null;  // -> Google -> back here
  }
  sessionStorage.removeItem('pr_auth_pending');
  window.__uid = auth.currentUser.uid;
  return auth.currentUser.uid;
}
