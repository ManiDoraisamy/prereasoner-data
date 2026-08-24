// Dependency-free checks for the external-LLM consent gate in the production client scripts.
// Run: node web/tests/llm_consent.test.js
//
// Server-side, /chat, /api/converse and /api/master/generate refuse (503) any request that does not
// carry external_llm_consent:true (see PRIVACY.md and the orchestrator/engine tests). These checks pin
// the CLIENT half of that contract: consent is asked once, remembered, sent only as a literal `true`,
// and declining leaves the local deterministic path in charge — a consent flag that is hardcoded or
// silently assumed would make the server gate meaningless.
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const workbook = fs.readFileSync(path.join(__dirname, '..', 'public', 'lib', 'workbook.js'), 'utf8');
const chatui = fs.readFileSync(path.join(__dirname, '..', 'public', 'chatui.html'), 'utf8');

function harness(source, {stored = null, search = '', answers = []} = {}) {
  const store = new Map();
  if (stored !== null) store.set('pr_llm_consent', stored);
  const confirms = [];
  const ctx = {
    localStorage: {
      getItem: k => (store.has(k) ? store.get(k) : null),
      setItem: (k, v) => store.set(k, String(v)),
      removeItem: k => store.delete(k),
    },
    location: {search},
    URLSearchParams,
    confirm: msg => { confirms.push(msg); return answers.length ? answers.shift() : false; },
  };
  vm.createContext(ctx);
  vm.runInContext(source, ctx);
  return {ctx, store, confirms};
}

// ---- workbook.js: extract the consent module (initializer + askLlmConsent) ----
const modMatch = workbook.match(/let LLM_CONSENT=[\s\S]*?\nfunction askLlmConsent\(revisit\)\{[\s\S]*?\n\}/);
assert(modMatch, 'consent module not found in workbook.js');
const MOD = modMatch[0] + '\n; this.ask = askLlmConsent; this.state = () => LLM_CONSENT;';

// 1. First ask: accepted -> true, stored '1', exactly one prompt; later calls never re-prompt.
{
  const {ctx, store, confirms} = harness(MOD, {answers: [true]});
  assert.strictEqual(ctx.ask(), true);
  assert.strictEqual(store.get('pr_llm_consent'), '1');
  assert.strictEqual(ctx.ask(), true);
  assert.strictEqual(confirms.length, 1, 'consent must be asked once, not per turn');
}

// 2. First ask: declined -> false, stored '0', and NOT re-asked on ordinary turns.
{
  const {ctx, store, confirms} = harness(MOD, {answers: [false]});
  assert.strictEqual(ctx.ask(), false);
  assert.strictEqual(store.get('pr_llm_consent'), '0');
  assert.strictEqual(ctx.ask(), false);
  assert.strictEqual(confirms.length, 1, 'a remembered "No" must not nag every turn');
}

// 3. Stored consent is honored without any prompt at all.
{
  const {ctx, confirms} = harness(MOD, {stored: '1'});
  assert.strictEqual(ctx.ask(), true);
  assert.strictEqual(confirms.length, 0);
}
{
  const {ctx, confirms} = harness(MOD, {stored: '0'});
  assert.strictEqual(ctx.ask(), false);
  assert.strictEqual(confirms.length, 0);
}

// 4. revisit=true (an explicit AI action like Autofill) re-asks a remembered "No"...
{
  const {ctx, confirms} = harness(MOD, {stored: '0', answers: [true]});
  assert.strictEqual(ctx.ask(true), true);
  assert.strictEqual(confirms.length, 1);
}
// ...but never re-asks a remembered "Yes".
{
  const {ctx, confirms} = harness(MOD, {stored: '1'});
  assert.strictEqual(ctx.ask(true), true);
  assert.strictEqual(confirms.length, 0);
}

// 5. ?chat=1 (the documented force-orchestrated flag) clears a remembered "No" so it is asked again.
{
  const {ctx, confirms} = harness(MOD, {stored: '0', search: '?chat=1', answers: [true]});
  assert.strictEqual(ctx.state(), null, '?chat=1 must clear a stored decline');
  assert.strictEqual(ctx.ask(), true);
  assert.strictEqual(confirms.length, 1);
}

// ---- wiring pins (source-level): the flag reaches every gated request, and only as `=== true` ----
assert(/if\(ORCH && askLlmConsent\(\)\) return startTurn\(\)/.test(workbook),
       'the orchestrated path must be consent-gated with the local path as fallback');
assert(/external_llm_consent:LLM_CONSENT===true/.test(workbook),
       '/chat and /api/master/generate must send the flag as a strict boolean, never hardcoded');
assert(/if\(LLM_CONSENT!==true\) throw/.test(workbook),
       '/api/converse must be skipped entirely without stored consent (it may not prompt)');
assert(!/external_llm_consent:\s*true\b/.test(workbook.replace(/external_llm_consent:true\};/, '')) ||
       /no LLM consent/.test(workbook),
       'a literal `external_llm_consent:true` is only legal behind the consent guard');

// ---- chatui.html: same key, same contract, no hardcoded consent ----
const uiMatch = chatui.match(/function llmConsent\(\)\{[\s\S]*?\n\}/);
assert(uiMatch, 'llmConsent not found in chatui.html');
{
  const {ctx, store, confirms} = harness(uiMatch[0] + '; this.ask = llmConsent;', {answers: [true]});
  assert.strictEqual(ctx.ask(), true);
  assert.strictEqual(store.get('pr_llm_consent'), '1', 'chatui must share the workbook consent key');
  assert.strictEqual(confirms.length, 1);
}
{
  const {ctx, confirms} = harness(uiMatch[0] + '; this.ask = llmConsent;', {stored: '0'});
  assert.strictEqual(ctx.ask(), false);
  assert.strictEqual(confirms.length, 0);
}
assert(/if\(!llmConsent\(\)\)\{/.test(chatui), 'chatui send() must refuse before fetching without consent');

console.log('llm consent: 11 cases passed');
