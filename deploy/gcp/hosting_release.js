// Prepare Firebase Auth and the client config for ONE Community deployment, then hand back to
// `firebase deploy`. cloudbuild.hosting.yaml runs this after `firebase apps:sdkconfig` has written
// the target project's Web app config to /tmp/firebase-js-config.json.
//
// This lives in a file rather than inline in cloudbuild.hosting.yaml because a Cloud Build step
// argument is capped at 10000 characters: growing the inline script past that limit failed the
// release with "build step 0 arg 1 too long" AFTER the database had already been seeded
// (observed 2026-09-16). A file has no such ceiling and can be linted and read normally.
//
// Inputs (environment): TARGET_PROJECT, HOSTING_SITE. Writes: web/public/lib/config.js.
const fs = require("fs");

const sdk = JSON.parse(fs.readFileSync("/tmp/firebase-js-config.json", "utf8"));
const projectId = sdk.projectId || process.env.TARGET_PROJECT;
const hostingSite = process.env.HOSTING_SITE;
// The ONE definition of this deployment's browser origins: the client pins authDomain to them and
// Firebase Auth is told to trust them, so the two can never disagree.
const hostingDomains = [hostingSite + ".web.app", hostingSite + ".firebaseapp.com"];

async function buildToken() {
  const metadata = await fetch(
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token",
    { headers: { "Metadata-Flavor": "Google" } });
  if (!metadata.ok) throw new Error("build credential unavailable: HTTP " + metadata.status);
  return (await metadata.json()).access_token;
}

// A Community install must not ask the operator to configure anything, and Google sign-in cannot
// meet that bar: the google.com provider requires an OAuth client id, and no public API creates an
// OAuth client for a project outside an organization (IAP brands answers "Project must belong to
// an organization"). Anonymous auth needs no client, still issues a real uid and a verifiable ID
// token, and is a single enable call -- so it is what this deployment provisions, and what the
// client config below is told to use.
async function enableAnonymousSignIn() {
  const headers = { Authorization: "Bearer " + (await buildToken()) };
  // Auth has to exist before a provider can be turned on; already-initialized is fine.
  await fetch("https://identitytoolkit.googleapis.com/v2/projects/" + projectId +
    "/identityPlatform:initializeAuth",
    { method: "POST", headers: Object.assign({ "Content-Type": "application/json" }, headers), body: "{}" });
  const url = "https://identitytoolkit.googleapis.com/admin/v2/projects/" + projectId +
    "/config?updateMask=signIn.anonymous.enabled";
  const res = await fetch(url, {
    method: "PATCH",
    headers: Object.assign({ "Content-Type": "application/json" }, headers),
    body: JSON.stringify({ signIn: { anonymous: { enabled: true } } }),
  });
  if (!res.ok) {
    throw new Error("enabling anonymous sign-in failed: HTTP " + res.status + " " + (await res.text()));
  }
  console.log("Firebase Auth: anonymous sign-in enabled");
}

// Anonymous sign-in never redirects, so this is not what unblocks the default question. It keeps
// the deployment internally consistent and lets a self-hoster turn on a redirect-based provider
// later without rediscovering auth/unauthorized-domain.
async function authorizeHostingDomains() {
  const headers = { Authorization: "Bearer " + (await buildToken()) };
  const url = "https://identitytoolkit.googleapis.com/admin/v2/projects/" + projectId + "/config";
  const read = await fetch(url, { headers });
  if (!read.ok) {
    throw new Error("reading Firebase Auth config failed: HTTP " + read.status + " " + (await read.text()));
  }
  const trusted = (await read.json()).authorizedDomains || [];
  const missing = hostingDomains.filter(function (domain) {
    return !trusted.includes(domain);
  });
  if (missing.length === 0) return;
  // Read-modify-write, because PATCH replaces the whole list: the project may already trust a
  // custom domain, localhost, or another deployment's site, and a Community install must never
  // revoke an origin it did not create.
  const write = await fetch(url + "?updateMask=authorizedDomains", {
    method: "PATCH",
    headers: Object.assign({ "Content-Type": "application/json" }, headers),
    body: JSON.stringify({ authorizedDomains: trusted.concat(missing) }),
  });
  if (!write.ok) {
    throw new Error("authorizing " + missing.join(", ") + " failed: HTTP " + write.status +
      " " + (await write.text()));
  }
  console.log("Firebase Auth authorized sign-in from: " + missing.join(", "));
}

function writeClientConfig() {
  const firebaseConfig = {
    apiKey: sdk.apiKey,
    authDomain: sdk.authDomain || (projectId + ".firebaseapp.com"),
    projectId,
    appId: sdk.appId,
    databaseURL: sdk.databaseURL || ("https://" + projectId + "-default-rtdb.firebaseio.com"),
  };
  const source = fs.readFileSync("web/public/lib/config.js", "utf8");
  const start = source.indexOf("const HOSTING_DOMAINS =");
  const end = source.indexOf("// Google Picker credentials", start);
  if (start < 0 || end < 0) throw new Error("config.js markers are missing");
  const block = "const HOSTING_DOMAINS = " +
    JSON.stringify(hostingDomains) + ";\n" +
    "export const firebaseConfig = {\n" +
    "  apiKey: " + JSON.stringify(firebaseConfig.apiKey) + ",\n" +
    "  authDomain: HOSTING_DOMAINS.includes(location.hostname) ? location.hostname : " +
      JSON.stringify(firebaseConfig.authDomain) + ",\n" +
    "  projectId: " + JSON.stringify(firebaseConfig.projectId) + ",\n" +
    "  appId: " + JSON.stringify(firebaseConfig.appId) + ",\n" +
    "  databaseURL: " + JSON.stringify(firebaseConfig.databaseURL) + "\n};\n" +
    'export const AUTH_PROVIDER = "anonymous";\n\n';
  fs.writeFileSync("web/public/lib/config.js", source.slice(0, start) + block + source.slice(end));
}

enableAnonymousSignIn()
  .then(authorizeHostingDomains)
  .then(writeClientConfig)
  .catch(function (error) {
    console.error(String((error && error.message) || error));
    process.exit(1);
  });
