const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('deploy/gcp/hosting_release.js', 'utf8');
const client = fs.readFileSync('web/public/lib/config.js', 'utf8');

async function generate(provider, domains = '') {
  let written;
  const calls = [];
  const context = {process: {env: {HOSTING_SITE: 'fixture', HOSTING_AUTH_PROVIDER: provider,
    HOSTING_CUSTOM_DOMAINS: domains}, exit: () => { throw new Error('release failed'); }}, console,
    require: name => {
      assert.equal(name, 'fs');
      return {readFileSync: path => path.includes('firebase-js-config') ? JSON.stringify({
        apiKey: 'public-fixture', projectId: 'fixture-project', appId: 'fixture-app'
      }) : client, writeFileSync: (_path, value) => { written = value; }};
    }, fetch: async (url, options) => {
      calls.push({url, options});
      return {ok: true, json: async () => url.includes('metadata.google.internal')
        ? {access_token: 'fixture-token'} : {authorizedDomains: ['existing.example']}};
    }};
  await vm.runInNewContext(source, context);
  return {written, calls};
}

(async () => {
  const community = await generate('');
  assert.match(community.written, /AUTH_PROVIDER = "anonymous"/);
  assert(community.calls.some(call => call.url.includes('signIn.anonymous.enabled')));
  const reference = await generate('google', 'chat.prereasoner.com,prereasoner.com');
  assert.match(reference.written, /AUTH_PROVIDER = "google"/);
  assert.match(reference.written, /"chat.prereasoner.com"/);
  assert.match(reference.written, /HOSTING_DOMAINS.includes\(location.hostname\) \? location.hostname/);
  assert(!reference.calls.some(call => call.url.includes('signIn.anonymous.enabled') || call.url.includes('initializeAuth')));
  const update = reference.calls.find(call => call.url.endsWith('updateMask=authorizedDomains'));
  assert(JSON.parse(update.options.body).authorizedDomains.includes('existing.example'));
  assert(JSON.parse(update.options.body).authorizedDomains.includes('chat.prereasoner.com'));
  console.log('Hosting release: reference Google auth and Community anonymous auth preserved');
})().catch(error => { console.error(error); process.exitCode = 1; });
