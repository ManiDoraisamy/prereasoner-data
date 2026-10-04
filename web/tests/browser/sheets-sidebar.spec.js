// The Google Sheets add-on's sidebar (sheets-addon/Sidebar.html), run as Sheets runs it: on an Apps Script
// content origin, loading the web rail's shared component, the upload importer and the shared Firebase
// module from chat.prereasoner.com (served here from web/public), with google.script.run standing in for
// Code.js and a realtime database the test drives.
const {test, expect} = require('@playwright/test');
const {conversation, orders, views, openSidebar, answer} = require('./sheets-sidebar-harness.js');

const calls = (page, name) => page.evaluate(n => window.__calls.filter(call => call.name === n), name);

test('hosted shell loads three shared schema-only questions while the composer stays editable', async ({page}) => {
  await openSidebar(page, orders, {}, {contextDelay: 1000});
  await expect(page.locator('#question')).toBeEditable();
  await page.locator('#question').fill('my draft');
  await expect(page.locator('#suggestions .starter-question')).toHaveCount(3);
  await expect(page.locator('#suggestions')).toHaveText(
    ['How many rows are in "Orders"?', 'Count rows in "Orders" by "country".', 'What is the total "amount" in "Orders"?'].join(''));
  await expect(page.locator('#suggestions .starter-question').first()).toHaveCSS('white-space','normal');
  await expect(page.locator('#sheetScope')).toHaveCount(0);
  const [request] = await calls(page, 'getPrereasonerSuggestions');
  expect(request.arg).toEqual({sheets:[{name:'Orders',columns:['country','amount']}],active_sheet:'Orders',scope:['Orders']});
  expect(JSON.stringify(request.arg)).not.toContain('840');
  await page.locator('#suggestions .starter-question').first().click();
  await expect(page.locator('#question')).toHaveValue('my draft');
  await page.getByRole('button',{name:'Replace my draft with this question',exact:true}).click();
  await expect(page.locator('#question')).toHaveValue('How many rows are in "Orders"?');
  expect(await page.evaluate(()=>Boolean(window.__server.pendingAsk))).toBe(false);
});

test('suggestion failure never blocks reading, typing or sending a question, and shows nothing', async ({page}) => {
  await openSidebar(page, orders, {getPrereasonerSuggestions:'Gemini unavailable'});
  await expect(page.locator('#newConversation')).toBeEnabled();
  await expect.poll(async () => (await calls(page, 'getPrereasonerSuggestions')).length).toBeGreaterThan(0);
  await expect(page.locator('#suggestions')).toBeHidden();
  await expect(page.locator('#thread')).toHaveText('');
  await expect(page.locator('#topline')).toBeHidden();
  await page.locator('#question').fill('total amount');
  await page.locator('#question').press('Enter');
  await expect.poll(()=>page.evaluate(()=>Boolean(window.__server.pendingAsk))).toBe(true);
});

test('long suggested questions wrap into full-height cards without overlapping', async ({page}) => {
  await openSidebar(page, orders);
  await page.locator('.branding-below').evaluate(element => { element.style.width = '220px'; });
  const buttons = page.locator('#suggestions .starter-question');
  await expect(buttons).toHaveCount(3);
  // Apps Script's add-on stylesheet gives buttons a compact fixed height. The shared component
  // must override that host default for multi-line generated prompts.
  await page.addStyleTag({content: 'button { height: 28px; max-height: 28px; }'});
  await buttons.first().evaluate(element => {
    element.textContent = 'What is the total amount across every customer and currency in this sheet, grouped by tier?';
  });
  const first = await buttons.first().evaluate(element => {
    const rect = element.getBoundingClientRect();
    return {height: rect.height, bottom: rect.bottom, scrollHeight: element.scrollHeight};
  });
  const second = await buttons.nth(1).evaluate(element => {
    const rect = element.getBoundingClientRect();
    return {top: rect.top};
  });
  expect(first.height).toBeGreaterThan(44);
  expect(first.height).toBeGreaterThanOrEqual(first.scrollHeight - 1);
  expect(second.top).toBeGreaterThanOrEqual(first.bottom);
});

test('suggested questions stay aligned under a host stylesheet that spaces adjacent buttons', async ({page}) => {
  // Google's add-on stylesheet sets `button + button { margin-left: 12px }`, which pushed every
  // card after the first to the right (2026-10-04).
  await openSidebar(page, orders);
  await page.addStyleTag({content: 'button + button { margin-left: 12px; }'});
  const buttons = page.locator('#suggestions .starter-question');
  await expect(buttons).toHaveCount(3);
  const lefts = await buttons.evaluateAll(elements => elements.map(element => element.getBoundingClientRect().left));
  expect(new Set(lefts).size).toBe(1);
});

test('suggestions receive every sheet name and column plus the active sheet without a scope picker', async ({page}) => {
  const schema={sheets:[{name:'Orders',columns:['country','amount']},{name:'Archive',columns:['id']}],
    active_sheet:'Orders',scope:['Orders','Archive']};
  const grids=[{name:'Orders',rows:orders},{name:'Archive',rows:[['id'],[1]]}];
  await openSidebar(page,orders,{}, {schema,grids});
  await expect(page.locator('#newConversation')).toBeEnabled();
  await expect(page.locator('#suggestions .starter-question')).toHaveCount(3);
  await expect(page.locator('#sheetScope')).toHaveCount(0);
  const [request] = await calls(page, 'getPrereasonerSuggestions');
  expect(request.arg).toEqual(schema);
});

test('the Sheets sidebar renders the web rail, with live steps from the realtime trace', async ({page}) => {
  await openSidebar(page, orders);
  // Empty: the composer and three starter questions, nothing else.
  await expect(page.locator('#newConversation')).toBeEnabled();
  await expect(page.locator('#suggestions .starter-question')).toHaveCount(3);
  await expect(page.locator('#thread')).toHaveText('');
  await expect(page.locator('#topline')).toBeHidden();
  for (const removed of ['.data-notice', '#syncStatus', '#note', '.starter-title', '.empty'])
    await expect(page.locator(removed)).toHaveCount(0);
  expect(await page.evaluate(() => window.__signedInWith)).toBe('google-token');
  const [restore] = await calls(page, 'restorePrereasonerSheetConversation');
  expect(restore.arg.tables).toEqual([{name: 'Orders', data: 'country,amount\nFrance,840\nFrance,400\nGermany,620',
    source: {kind: 'google-sheets-addon', warnings: [], scope: {mode:'all', included:['Orders'], available:['Orders']}}}]);

  await page.locator('#question').fill('What are total sales in France?');
  await page.locator('#question').press('Enter');
  await expect.poll(() => page.evaluate(() => Boolean(window.__server.pendingAsk))).toBe(true);
  await expect(page.locator('#suggestions')).toBeHidden();   // starters belong to an empty chat
  const ask = await page.evaluate(() => window.__server.pendingAsk.arg);
  expect(ask.question).toBe('What are total sales in France?');
  expect(ask.tables[0].data).toBe('country,amount\nFrance,840\nFrance,400\nGermany,620');
  const turn = 'runs/sheet-user/' + ask.turnId;
  await expect.poll(() => page.evaluate(() => window.__rtdb.paths())).toContain('child:' + turn + '/calls');

  // Live: the call, each step as it finishes (the web's sentences and backend badge), and the reply.
  await page.evaluate(path => window.__rtdb.child(path + '/calls', '0', {jobId: 'job-1', question: 'total amount in France'}), turn);
  await expect(page.locator('#liveTurn .statusline')).toContainText('Reading as: “total amount in France”');
  await page.evaluate(view => window.__rtdb.child('runs/sheet-user/job-1/views', '0', view), views[0]);
  await page.evaluate(() => window.__rtdb.value('runs/sheet-user/job-1/execution', {actual: 'python', verified: false}));
  await expect(page.locator('#liveTurn .steplink')).toHaveText(['1Kept only the rows where country is France. · from OrdersPY ran']);
  await expect(page.locator('#liveTurn .statusline')).toContainText('Filtering the rows…');
  await page.evaluate(path => window.__rtdb.value(path + '/reply', 'Total sales in France are **US$1,240**.'), turn);
  await expect(page.locator('#liveTurn .turn-answer')).toHaveText('Total sales in France are US$1,240.');

  // The finished turn: the authoritative trace, linked to the full analysis, and saved for this sheet.
  await answer(page, 'Total sales in France are **US$1,240**.', views);
  await expect(page.locator('#liveTurn')).toHaveCount(0);
  await expect(page.locator('.turn-answer')).toHaveText('Total sales in France are US$1,240.');
  await expect(page.locator('.reasoning-title')).toHaveText('Reasoning steps for France sales');
  await expect(page.locator('.cotask')).toHaveText('read as “total amount in France”');
  const steps = page.locator('.turn-reasoning .steplink');
  await expect(steps).toHaveText(['1Kept only the rows where country is France. · from OrdersPY ran',
    '2Added up the values to get the total. · from filteredPY ran']);
  await expect(steps.first()).toHaveAttribute('href',
    'https://chat.prereasoner.com/reason/' + conversation + '?analysis_id=a_11111111111111111111111111111111&revision=1');
  await expect.poll(async () => (await calls(page, 'savePrereasonerSheetConversation')).length).toBe(1);
  const [save] = await calls(page, 'savePrereasonerSheetConversation');
  expect(save.arg.conversationId).toBe(conversation);
  expect(save.arg.state.version).toBe(2);
  expect(save.arg.state.turns[0].steps.map(step => step.description))
    .toEqual(['Kept only the rows where country is France.', 'Added up the values to get the total.']);

  // New chat unbinds the sheet and brings the starters back.
  await expect(page.locator('#topline')).toBeVisible();
  await page.locator('#newConversation').click();
  await expect(page.locator('#thread')).toHaveText('');
  await expect(page.locator('#suggestions .starter-question')).toHaveCount(3);
  await expect(page.locator('#topline')).toBeHidden();
  expect((await calls(page, 'clearPrereasonerSheetConversation')).length).toBe(1);
});

test('a reworded question is the turn\'s visible read-as line, and the answer is only the answer', async ({page}) => {
  // The owner's sheet (2026-10-04): "Gemini reworded the question as: ..." sat under "5,000" in the
  // answer. What the engine read belongs on the line the live "Reading as" status becomes.
  const reading = "What is the Avg. monthly searches for the Keyword 'home inspection checklist'?";
  await openSidebar(page, orders);
  await page.locator('#question').fill('keyword volume for home inspection checklist');
  await expect(page.locator('#newConversation')).toBeEnabled();
  await page.locator('#question').press('Enter');
  await expect.poll(() => page.evaluate(() => Boolean(window.__server.pendingAsk))).toBe(true);
  await page.evaluate(({conversation, reading, views}) => window.__server.pendingAsk.ok({reply: '5,000',
    conversationId: conversation, history: [],
    traces: [{jobId: 'job-1', question: reading, analysis: {analysis_id: 'a_11111111111111111111111111111111', revision: 1,
      display_name: 'keyword volume'}, views}]}), {conversation, reading, views});
  await expect(page.locator('.turn-answer')).toHaveText('5,000');
  await expect(page.locator('.turn-reading .cotask')).toHaveText('read as “' + reading + '”');
  await expect(page.locator('.turn-reading .cotask')).toBeVisible();
  await expect(page.locator('details.reasoning .cotask')).toHaveCount(0);
  // The engine reading the typed words adds no line.
  await page.locator('#question').fill('total amount');
  await page.locator('#question').press('Enter');
  await expect.poll(() => page.evaluate(() => window.__server.pendingAsk.arg.question)).toBe('total amount');
  await page.evaluate(({conversation, views}) => window.__server.pendingAsk.ok({reply: '1,860', conversationId: conversation,
    history: [], traces: [{jobId: 'job-2', question: 'total amount', views}]}), {conversation, views});
  await expect(page.locator('.turn-answer')).toHaveText(['5,000', '1,860']);
  await expect(page.locator('.turn-reading')).toHaveCount(1);
});

test('messy headers are read without a warning and without preventing a question', async ({page}) => {
  await openSidebar(page, [['id','amount','amount',null],[1,10,20,'note']]);
  await expect(page.locator('#newConversation')).toBeEnabled();
  await expect(page.locator('.sheet-error')).toHaveCount(0);
  await expect(page.locator('#scroll')).not.toContainText('Repeated headers');
  await page.locator('#question').fill('How many rows are there?');
  await page.locator('#question').press('Enter');
  await expect.poll(() => page.evaluate(() => Boolean(window.__server.pendingAsk))).toBe(true);
  const table = (await page.evaluate(() => window.__server.pendingAsk.arg)).tables[0];
  expect(table.data).toBe('id,amount [column B],amount [column C],Column D\n1,10,20,note');
  expect(table.source.warnings.join(' ')).toContain('Repeated headers were kept');   // still sent with the data
});

test('typing while reading and answering preserves the draft even on failure', async ({page}) => {
  await openSidebar(page, orders, {}, {contextDelay: 1500});
  await expect(page.locator('#question')).toBeEnabled();
  await expect(page.locator('#send')).toBeDisabled();
  await page.locator('#question').fill('total amount');
  await expect(page.locator('#send')).toBeEnabled();
  await expect(page.locator('#question')).toHaveValue('total amount');
  await page.locator('#question').press('Enter');
  await expect.poll(() => page.evaluate(() => Boolean(window.__server.pendingAsk))).toBe(true);
  await expect(page.locator('#question')).toBeEnabled();
  await page.locator('#question').fill('and for Germany?');
  await page.evaluate(() => window.__server.pendingAsk.fail({message:'Network unavailable'}));
  await expect(page.locator('.answer.error')).toContainText('Network unavailable');
  // The failed question stays in the thread; it is never silently dropped.
  await expect(page.locator('.turn.user')).toHaveText('total amount');
  await expect(page.locator('#question')).toHaveValue('and for Germany?');
});

test('the Sheets sidebar explains an incomplete import without asking users to select a scope', async ({page}) => {
  await openSidebar(page, orders, {}, {skipped: [
    {name: 'SI', reason: 'cells'}, {name: 'Log', reason: 'rows'}, {name: 'Wide', reason: 'columns'}
  ]});
  await expect(page.locator('.sheet-error')).toContainText('SI, Log, Wide');
  await expect(page.locator('.sheet-error')).toContainText('Hide or close unrelated large tabs');
  await expect(page.locator('#suggestions')).toBeHidden();
});

test('an automatically selected active-sheet scope reads the current sheet without a selectbox', async ({page}) => {
  await openSidebar(page, orders, {}, {scope:'active', availableTabs:['Orders','Archive']});
  await expect(page.locator('#newConversation')).toBeEnabled();
  await expect(page.locator('#sheetScope')).toHaveCount(0);
  await page.locator('#question').fill('total amount');
  await page.locator('#question').press('Enter');
  await expect.poll(async () => (await calls(page, 'getWorkbookGrids')).length).toBe(1);
  expect((await calls(page,'getWorkbookGrids'))[0].arg).toEqual({scope:'active'});
});

test('a saved chat that cannot be read is tried again, and never replaced by a new chat', async ({page}) => {
  // A passing outage was treated as an expired chat, which wiped the sheet's saved conversation
  // (2026-10-04). The question fails and stays; nothing is asked over the unread chat.
  await openSidebar(page, orders, {restorePrereasonerSheetConversation: 'Prereasoner: request failed.'});
  await expect(page.locator('#newConversation')).toBeEnabled();
  await expect(page.locator('#scroll')).not.toContainText('request failed');
  await page.locator('#question').fill('keyword volume for home inspection checklist');
  await page.locator('#question').press('Enter');
  await expect(page.locator('.answer.error')).toContainText('Prereasoner: request failed.');
  await expect(page.locator('.turn.user')).toHaveText('keyword volume for home inspection checklist');
  expect((await calls(page, 'restorePrereasonerSheetConversation')).length).toBe(2);
  expect(await calls(page, 'askPrereasoner')).toEqual([]);
});

test('a question in a chat deleted elsewhere is asked once more as a new chat, keeping the thread', async ({page}) => {
  // The chat service answered 500 to every question in a deleted conversation, and the sidebar kept
  // sending the dead id (2026-10-04). Its 404 now drops the id, and the question runs once more.
  const saved = {client: 'google-sheets-addon', version: 2, syncedFingerprint: '',
    turns: [{question: 'What are total sales in France?', reply: 'Total sales in France are US$1,240.', steps: [], asks: []}],
    history: [{role: 'user', content: 'What are total sales in France?'}, {role: 'assistant', content: 'Total sales in France are US$1,240.'}]};
  await openSidebar(page, orders, {}, {restored: {conversationId: conversation, state: saved},
    failOnce: {askPrereasoner: 'Prereasoner: conversation not found.'}});
  await expect(page.locator('.turn-answer')).toHaveText('Total sales in France are US$1,240.');
  await page.locator('#question').fill('and in Germany?');
  await page.locator('#question').press('Enter');
  await expect.poll(() => page.evaluate(() => Boolean(window.__server.pendingAsk))).toBe(true);
  const asks = await calls(page, 'askPrereasoner');
  expect(asks.map(ask => ask.arg.conversationId)).toEqual([conversation, null]);
  expect(asks[1].arg.history).toEqual(saved.history);
  await expect(page.locator('.answer.error')).toHaveCount(0);
  await expect(page.locator('.turn.user').first()).toHaveText('What are total sales in France?');
});

test('a passing sync failure keeps the chat and the earlier turns', async ({page}) => {
  const saved = {client: 'google-sheets-addon', version: 2, syncedFingerprint: 'an-older-sheet',
    turns: [{question: 'What are total sales in France?', reply: 'Total sales in France are US$1,240.', steps: [], asks: []}],
    history: []};
  await openSidebar(page, orders, {}, {restored: {conversationId: conversation, state: saved},
    failOnce: {syncPrereasonerConversation: 'Prereasoner: request failed.'}});
  await expect(page.locator('.turn-answer')).toHaveText('Total sales in France are US$1,240.');
  await page.locator('#question').fill('and in Germany?');
  await page.locator('#question').press('Enter');
  await expect(page.locator('.answer.error')).toContainText('Prereasoner: request failed.');
  expect(await calls(page, 'askPrereasoner')).toEqual([]);
  // Asked again, the same chat is synced and asked.
  await page.locator('#question').fill('and in Germany?');
  await page.locator('#question').press('Enter');
  await expect.poll(() => page.evaluate(() => Boolean(window.__server.pendingAsk))).toBe(true);
  expect((await calls(page, 'askPrereasoner'))[0].arg.conversationId).toBe(conversation);
  await expect(page.locator('.turn.user')).toHaveText(['What are total sales in France?', 'and in Germany?', 'and in Germany?']);
});

test('a chat that can no longer take the changed sheet keeps its turns and history for the next question', async ({page}) => {
  const saved = {client: 'google-sheets-addon', version: 2, syncedFingerprint: 'an-older-sheet',
    turns: [{question: 'What are total sales in France?', reply: 'Total sales in France are US$1,240.', steps: [], asks: []}],
    history: [{role: 'user', content: 'What are total sales in France?'}, {role: 'assistant', content: 'Total sales in France are US$1,240.'}]};
  await openSidebar(page, orders, {syncPrereasonerConversation: 'conversation not found'}, {restored: {conversationId: conversation, state: saved}});
  await expect(page.locator('.turn-answer')).toHaveText('Total sales in France are US$1,240.');
  await page.locator('#question').fill('and in Germany?');
  await page.locator('#question').press('Enter');
  await expect.poll(() => page.evaluate(() => Boolean(window.__server.pendingAsk))).toBe(true);
  const request = await page.evaluate(() => window.__server.pendingAsk.arg);
  expect(request.conversationId).toBeNull();
  expect(request.history).toEqual(saved.history);
  await expect(page.locator('.turn.user').first()).toHaveText('What are total sales in France?');
  await expect(page.locator('#scroll')).not.toContainText('could not');
});

test('an answer that belongs to no conversation is shown and not saved', async ({page}) => {
  await openSidebar(page, orders);
  await page.locator('#question').fill('What are total sales in France?');
  await expect(page.locator('#newConversation')).toBeEnabled();
  await page.locator('#question').press('Enter');
  await expect.poll(() => page.evaluate(() => Boolean(window.__server.pendingAsk))).toBe(true);
  await page.evaluate(() => window.__server.pendingAsk.ok({reply: 'Prereasoner could not answer that question.',
    conversationId: null, history: [], traces: []}));
  await expect(page.locator('.turn-answer')).toHaveText('Prereasoner could not answer that question.');
  expect(await calls(page, 'savePrereasonerSheetConversation')).toEqual([]);
  await expect(page.locator('#scroll')).not.toContainText('expired');
});

test('the Sheets sidebar explains a multi-account refusal instead of blaming the sheet', async ({page}) => {
  // A user signed in to two Google accounts (2026-10-02): the menu opened the sidebar, then Apps Script
  // refused every sidebar call, made as the browser's default account, before any add-on code ran.
  const refused = 'Authorization is required to perform that action.';
  await openSidebar(page, orders, {getSidebarContext: refused, getWorkbookGrids: refused});
  const account = 'This happens when the browser is signed in to more than one Google account. Open the sheet in a ' +
    'window signed in only to the account that installed Prereasoner (an Incognito window works)';
  await expect(page.locator('.empty.sheet-error')).toContainText(account);
  await expect(page.locator('.empty.sheet-error')).not.toContainText('Fix the sheet');
  await expect(page.locator('.empty.sheet-error')).not.toContainText(refused);

  await page.locator('#question').fill('What is the keyword volume for permit management?');
  await page.locator('#question').press('Enter');
  await expect(page.locator('.answer.error')).toContainText(account);
  await expect(page.locator('#question')).toHaveValue('What is the keyword volume for permit management?');
  expect(await calls(page, 'askPrereasoner')).toEqual([]);
});

test('the Sheets sidebar keeps the answer without a notice when sheet history cannot save', async ({page}) => {
  await openSidebar(page, orders, {savePrereasonerSheetConversation: 'Prereasoner: spreadsheet session limit reached.'});
  await page.locator('#question').fill('What are total sales in France?');
  await expect(page.locator('#send')).toBeEnabled();
  await page.locator('#question').press('Enter');
  await expect.poll(() => page.evaluate(() => Boolean(window.__server.pendingAsk))).toBe(true);
  await answer(page, 'Total sales in France are **US$1,240**.', views);
  await expect(page.locator('.turn-answer')).toHaveText('Total sales in France are US$1,240.');
  await expect.poll(async () => (await calls(page, 'savePrereasonerSheetConversation')).length).toBe(1);
  await expect(page.locator('#scroll')).not.toContainText('could not be saved');
  await expect(page.locator('#newConversation')).toBeEnabled();
});
