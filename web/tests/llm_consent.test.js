// Dependency-free checks for the external-LLM notice-and-choice flow in the production client scripts.
// Run: node web/tests/llm_consent.test.js
//
// Server-side, /chat, /api/converse and /api/master/generate refuse (503) any request that does not
// carry external_llm_consent:true. The reference deployment uses NOTICE-AND-CHOICE (PRIVACY.md): the
// assistant proceeds immediately, a one-time dismissible line in the rail says where the data goes,
// and one click switches to local-only mode. These checks pin the client half of that contract:
// no blocking dialog anywhere, the flag reflects the stored choice, and opting out genuinely stops
// every Anthropic-bound request.
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const workbook = fs.readFileSync(path.join(__dirname, '..', 'public', 'lib', 'workbook.js'), 'utf8');
const chatui = fs.readFileSync(path.join(__dirname, '..', 'public', 'chatui.html'), 'utf8');

function harness(source, {stored = {}, search = ''} = {}) {
  const store = new Map(Object.entries(stored));
  let reloads = 0;
  const ctx = {
    localStorage: {
      getItem: k => (store.has(k) ? store.get(k) : null),
      setItem: (k, v) => store.set(k, String(v)),
      removeItem: k => store.delete(k),
    },
    location: {search, reload: () => { reloads += 1; }},
    URLSearchParams,
    ORCH: true,
    renderRail: () => {},
    addTurn: () => ({querySelector: () => ({textContent: ''})}),
  };
  vm.createContext(ctx);
  vm.runInContext(source, ctx);
  return {ctx, store, reloads: () => reloads};
}

// ---- workbook.js: extract the notice module ----
const modMatch = workbook.match(/let LLM_CONSENT=[\s\S]*?function optOutLlm\(\)\{[\s\S]*?\n\}/);
assert(modMatch, 'notice module not found in workbook.js');
const MOD = modMatch[0]
  + '\n; this.ask = askLlmConsent; this.state = () => LLM_CONSENT;'
  + ' this.notice = llmNoticeHtml; this.ack = ackLlmNotice; this.optOut = optOutLlm;';

// 1. Fresh visitor: assistant PROCEEDS (no dialog), notice is pending and rendered.
{
  const {ctx} = harness(MOD);
  assert.strictEqual(ctx.ask(), true, 'the notice model must not block the first turn');
  assert.strictEqual(ctx.state(), null, 'unacknowledged state is null (notice pending)');
  const html = ctx.notice();
  assert(/Anthropic/.test(html), 'the notice must say where the data goes');
  assert(/ackLlmNotice/.test(html) && /optOutLlm/.test(html), 'both choices must be offered');
}

// 2. Dismissing the notice stores '1' and stops rendering it.
{
  const {ctx, store} = harness(MOD);
  ctx.ack();
  assert.strictEqual(store.get('pr_llm_consent'), '1');
  assert.strictEqual(ctx.notice(), '', 'an acknowledged notice must not render again');
  assert.strictEqual(ctx.ask(), true);
}

// 3. Local-only opt-out stores '0' AND turns the orchestrated mode off, then reloads.
{
  const {ctx, store, reloads} = harness(MOD);
  ctx.optOut();
  assert.strictEqual(store.get('pr_llm_consent'), '0');
  assert.strictEqual(store.get('pr_chat'), '0', 'opt-out must also disable the orchestrated path');
  assert.strictEqual(reloads(), 1);
}

// 4. A stored opt-out refuses without any prompt or notice.
{
  const {ctx} = harness(MOD, {stored: {pr_llm_consent: '0'}});
  assert.strictEqual(ctx.ask(), false);
  assert.strictEqual(ctx.notice(), '');
}

// 5. ?chat=1 (the documented re-enable flag) clears an opt-out so the notice shows again.
{
  const {ctx} = harness(MOD, {stored: {pr_llm_consent: '0'}, search: '?chat=1'});
  assert.strictEqual(ctx.state(), null);
  assert.strictEqual(ctx.ask(), true);
  assert(/Anthropic/.test(ctx.notice()));
}

// ---- wiring pins (source-level) ----
assert(/if\(ORCH && askLlmConsent\(\)\) return startTurn\(\)/.test(workbook),
       'the orchestrated path must fall through to the local path when opted out');
assert((workbook.match(/external_llm_consent:LLM_CONSENT!==false/g) || []).length >= 3,
       '/chat, /api/converse and /api/master/generate must all derive the flag from the stored choice');
assert(/if\(LLM_CONSENT===false\) throw/.test(workbook),
       '/api/converse must be skipped entirely when opted out');
const consentModule = modMatch[0];
assert(!/confirm\(/.test(consentModule), 'no blocking dialog in the workbook notice flow');

// ---- chatui.html: same key, notice-once, refusal on opt-out, no dialogs ----
assert(!/confirm\(/.test(chatui), 'no blocking dialog anywhere in chatui');
assert(/pr_llm_consent/.test(chatui), 'chatui must share the workbook consent key');
assert(/external_llm_consent: true/.test(chatui), 'chatui sends the flag on the gated request');
{
  const uiMatch = chatui.match(/function llmOptedOut\(\)\{[\s\S]*?\n\}/);
  const onceMatch = chatui.match(/function llmNoticeOnce\(\)\{[\s\S]*?\n\}/);
  assert(uiMatch && onceMatch, 'chatui notice helpers not found');
  const {ctx, store} = harness(uiMatch[0] + '\n' + onceMatch[0]
    + '; this.optedOut = llmOptedOut; this.once = llmNoticeOnce;');
  assert.strictEqual(ctx.optedOut(), false);
  ctx.once();
  assert.strictEqual(store.get('pr_llm_consent'), '1', 'first send stores the acknowledgement');
  const opted = harness(uiMatch[0] + '; this.optedOut = llmOptedOut;',
                        {stored: {pr_llm_consent: '0'}});
  assert.strictEqual(opted.ctx.optedOut(), true);
}

console.log('llm notice-and-choice: 10 cases passed');
