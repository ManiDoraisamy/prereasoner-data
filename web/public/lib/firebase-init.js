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
  const statusRef = ref(db, `runs/${uid}/${jobId}/status`);
  const resolveRef = ref(db, `runs/${uid}/${jobId}/resolve`);
  const viewsRef = ref(db, `runs/${uid}/${jobId}/views`);
  const resultRef = ref(db, `runs/${uid}/${jobId}/result`);
  const clarifyRef = ref(db, `runs/${uid}/${jobId}/clarify`);
  const errorRef = ref(db, `runs/${uid}/${jobId}/error`);
  const questionRef = ref(db, `runs/${uid}/${jobId}/question`);
  const uStatus = onValue(statusRef, s => { const v = s.val(); if (v != null && cb.onStatus) cb.onStatus(v); });
  const uResolve = onChildAdded(resolveRef, s => { if (cb.onResolve) cb.onResolve(s.key, s.val()); });
  const uView = onChildAdded(viewsRef, s => { if (cb.onView) cb.onView(s.key, s.val()); });
  const uResult = onValue(resultRef, s => { const v = s.val(); if (v && cb.onResult) cb.onResult(v); });
  const uQuestion = onValue(questionRef, s => { const v = s.val(); if (v != null && cb.onQuestion) cb.onQuestion(v); });
  const uClarify = onValue(clarifyRef, s => { const v = s.val(); if (v && cb.onClarify) cb.onClarify(v); });
  const uError = onValue(errorRef, s => { const v = s.val(); if (v != null && cb.onError) cb.onError(v); });
  return () => { try { uStatus(); uResolve(); uView(); uResult(); uQuestion(); uClarify(); uError(); off(base); } catch(_){} };
};

// Google sign-in as a same-tab REDIRECT (no button, no popup blockers): completes a pending
// redirect if we are returning from Google, otherwise starts one. Resolves to the uid when
// signed in (also published as window.__uid), or null when the page is about to navigate away.
export async function ensureSignedIn(){
  // LOCAL DEV bypass (localhost only): sessionStorage.setItem('pr_test_auth','1') skips Google
  // sign-in and sends a dummy bearer token. Pair with AUTH_TEST_SUB on the engine, which then
  // skips token verification. Has no effect on any deployed origin.
  if ((location.hostname === 'localhost' || location.hostname === '127.0.0.1') && sessionStorage.getItem('pr_test_auth')) {
    window.ensureToken = async () => 'local-dev'; window.__uid = 'local-dev'; return 'local-dev';
  }
  try { await getRedirectResult(auth); } catch (e) {}   // completes the sign-in when returning from Google
  await auth.authStateReady();
  if (!auth.currentUser) { await signInWithRedirect(auth, new GoogleAuthProvider()); return null; }  // -> Google -> back here
  window.__uid = auth.currentUser.uid;
  return auth.currentUser.uid;
}
