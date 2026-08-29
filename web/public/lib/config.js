// config.js — the ONE place the deployment-specific client identifiers live (ES module).
//
// NONE of these values are secrets. The Firebase web config (apiKey, authDomain, projectId,
// appId, databaseURL) is a set of PUBLIC client identifiers that every visitor's browser
// downloads anyway; access control lives server-side (Firebase Auth + database.rules.json +
// the engine verifying ID tokens). See:
// https://firebase.google.com/docs/projects/api-keys
//
// SELF-HOSTERS: replace these with the values from YOUR Firebase project
// (Firebase console -> Project settings -> Your apps -> Web app -> SDK setup and configuration).
// You need: Google sign-in enabled in Authentication, and a Realtime Database instance
// (its URL goes in databaseURL) for the live reasoning-trace stream.
// The values below are the defaults for the reference deployment (chat.prereasoner.com)
// so the existing deployment keeps working out of the box.
// authDomain must be the domain the user is ON whenever possible: Firebase Hosting serves the
// auth handler (/__/auth/*) on every connected domain, and if the sign-in redirect round-trips
// through a DIFFERENT domain, browsers that partition third-party storage (Chrome 115+, Safari)
// lose the pending sign-in on return — the user gets bounced back to the Google account chooser
// forever. All domains listed here are Firebase Auth authorized domains for this project.
const HOSTING_DOMAINS = ["prereasoner.com", "chat.prereasoner.com",
                         "prereasoner-inference.web.app", "prereasoner-inference.firebaseapp.com"];
export const firebaseConfig = {
  apiKey: "AIzaSyAC_Kiqj3lqd52ufpqYDAO17G6T7wfBd9Q",
  authDomain: HOSTING_DOMAINS.includes(location.hostname) ? location.hostname : "chat.prereasoner.com",
  projectId: "prereasoner-inference",
  appId: "1:271377281957:web:1eb1bced0b0c8d1c7aee4c",
  databaseURL: "https://prereasoner-inference-default-rtdb.firebaseio.com"
};

// Google Picker credentials — used ONLY by sheets.html (the Google Sheets import page).
// Also public client identifiers: a browser API key restricted by HTTP referrer (enable the
// Picker, Sheets and Drive APIs on it and restrict it to your domain) and the numeric Google
// Cloud PROJECT NUMBER (required so the narrow drive.file scope can read the picked file).
export const PICKER_API_KEY = "AIzaSyAat-hVxaxc6bR_V2H0wzVg9khLfM5NWyE";
export const PICKER_APP_ID = "271377281957";
