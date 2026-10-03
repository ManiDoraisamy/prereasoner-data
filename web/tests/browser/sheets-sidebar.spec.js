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
  const [request] = await calls(page, 'getPrereasonerSuggestions');
  expect(request.arg).toEqual({sheets:[{name:'Orders',columns:['country','amount']}],active_sheet:'Orders',scope:['Orders']});
  expect(JSON.stringify(request.arg)).not.toContain('840');
  await page.locator('#suggestions .starter-question').first().click();
  await expect(page.locator('#question')).toHaveValue('my draft');
  await page.getByRole('button',{name:'Replace my draft with this question',exact:true}).click();
  await expect(page.locator('#question')).toHaveValue('How many rows are in "Orders"?');
  expect(await page.evaluate(()=>Boolean(window.__server.pendingAsk))).toBe(false);
});

test('suggestion failure never blocks reading, typing or sending a question', async ({page}) => {
  await openSidebar(page, orders, {getPrereasonerSuggestions:'Gemini unavailable'});
  await expect(page.locator('#newConversation')).toBeEnabled();
  await expect(page.locator('#suggestions')).toBeHidden();
  await page.locator('#question').fill('total amount');
  await page.locator('#question').press('Enter');
  await expect.poll(()=>page.evaluate(()=>Boolean(window.__server.pendingAsk))).toBe(true);
});

test('changing source scope discards old schemas and ignores a delayed earlier scope read', async ({page}) => {
  const schema={sheets:[{name:'Orders',columns:['country','amount']},{name:'Archive',columns:['id']}],
    active_sheet:'Orders',scope:['Orders','Archive']};
  const grids=[{name:'Orders',rows:orders},{name:'Archive',rows:[['id'],[1]]}];
  await openSidebar(page,orders,{}, {schema,grids});
  await expect(page.locator('#newConversation')).toBeEnabled();
  await expect(page.locator('#suggestions .starter-question')).toHaveCount(3);
  await page.evaluate(()=>{window.__server.holdSchemas=true;});
  await page.locator('#question').fill('keep my draft');
  await page.locator('#sheetScope').selectOption('active');
  await expect.poll(()=>page.evaluate(()=>window.__server.pendingSchemas.length)).toBe(1);
  await page.evaluate(s=>window.__server.pendingSchemas[0].ok({...s,scope:['Orders']}),schema);
  await expect.poll(async()=> (await calls(page,'getPrereasonerSuggestions')).at(-1).arg.scope).toEqual(['Orders']);
  await page.locator('#sheetScope').selectOption('all');
  await expect.poll(()=>page.evaluate(()=>window.__server.pendingSchemas.length)).toBe(2);
  await page.locator('#sheetScope').selectOption('active');
  await expect.poll(()=>page.evaluate(()=>window.__server.pendingSchemas.length)).toBe(3);
  await page.evaluate(s=>window.__server.pendingSchemas[2].ok({...s,scope:['Orders']}),schema);
  await expect(page.locator('#suggestions .starter-question')).toHaveCount(3);
  const count=(await calls(page,'getPrereasonerSuggestions')).length;
  await page.evaluate(s=>window.__server.pendingSchemas[1].ok(s),schema);
  await page.waitForTimeout(50);
  expect((await calls(page,'getPrereasonerSuggestions')).length).toBe(count);
  expect((await calls(page,'getPrereasonerSuggestions')).at(-1).arg.scope).toEqual(['Orders']);
  await expect(page.locator('#question')).toHaveValue('keep my draft');
});

test('the Sheets sidebar renders the web rail, with live steps from the realtime trace', async ({page}) => {
  await openSidebar(page, orders);
  await expect(page.locator('.empty')).toContainText('Ask a question about the current sheet.');
  await expect(page.locator('.data-notice')).toContainText('This data is not used to train generalized AI models.');
  await expect(page.locator('#sheetCount')).toHaveText('1 tab: Orders');
  await expect(page.locator('#newConversation')).toBeEnabled();
  expect(await page.evaluate(() => window.__signedInWith)).toBe('google-token');
  const [restore] = await calls(page, 'restorePrereasonerSheetConversation');
  expect(restore.arg.tables).toEqual([{name: 'Orders', data: 'country,amount\nFrance,840\nFrance,400\nGermany,620',
    source: {kind: 'google-sheets-addon', warnings: [], scope: {mode:'all', included:['Orders'], available:['Orders']}}}]);

  await page.locator('#question').fill('What are total sales in France?');
  await page.locator('#question').press('Enter');
  await expect.poll(() => page.evaluate(() => Boolean(window.__server.pendingAsk))).toBe(true);
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

  // New chat unbinds the sheet.
  await page.locator('#newConversation').click();
  await expect(page.locator('.empty')).toContainText('Ask a question about the current sheet.');
  expect((await calls(page, 'clearPrereasonerSheetConversation')).length).toBe(1);
});

test('messy headers warn without preventing a question', async ({page}) => {
  await openSidebar(page, [['id','amount','amount',null],[1,10,20,'note']]);
  await expect(page.locator('#newConversation')).toBeEnabled();
  await expect(page.locator('.sheet-error')).toHaveCount(0);
  await expect(page.locator('#note')).toContainText('Repeated headers were kept');
  await page.locator('#question').fill('How many rows are there?');
  await page.locator('#question').press('Enter');
  await expect.poll(() => page.evaluate(() => Boolean(window.__server.pendingAsk))).toBe(true);
  expect((await page.evaluate(() => window.__server.pendingAsk.arg)).tables[0].data)
    .toBe('id,amount [column B],amount [column C],Column D\n1,10,20,note');
});

test('typing while reading and answering preserves the draft even on failure', async ({page}) => {
  await openSidebar(page, orders, {}, {contextDelay: 1500});
  await expect(page.locator('#sheetCount')).toHaveText('Reading the sheet…');
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
  await expect(page.locator('#question')).toHaveValue('and for Germany?');
});

test('the Sheets sidebar refuses an incomplete workbook from an older add-on server', async ({page}) => {
  await openSidebar(page, orders, {}, {skipped: [
    {name: 'SI', reason: 'cells'}, {name: 'Log', reason: 'rows'}, {name: 'Wide', reason: 'columns'}
  ]});
  await expect(page.locator('.sheet-error')).toContainText('SI, Log, Wide');
  await expect(page.locator('.sheet-error')).toContainText('Choose Active sheet');
  await expect(page.locator('#sheetCount')).toHaveText('');
});

test('an active-sheet scope is visible and can be changed without editing the workbook', async ({page}) => {
  await openSidebar(page, orders, {}, {scope:'active', availableTabs:['Orders','Archive']});
  await expect(page.locator('#newConversation')).toBeEnabled();
  await expect(page.locator('#sheetScope')).toHaveValue('active');
  await expect(page.locator('#note')).toContainText('Using the active sheet only: Orders.');
  await page.locator('#sheetScope').selectOption('all');
  await expect(page.locator('#note')).toContainText('Your next question will use all visible tabs.');
  await page.locator('#question').fill('total amount');
  await page.locator('#question').press('Enter');
  await expect.poll(async () => (await calls(page, 'getWorkbookGrids')).length).toBe(1);
  expect((await calls(page,'getWorkbookGrids'))[0].arg).toEqual({scope:'all'});
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

test('the Sheets sidebar keeps the answer and shows the server reason when sheet history cannot save', async ({page}) => {
  await openSidebar(page, orders, {savePrereasonerSheetConversation: 'Prereasoner: spreadsheet session limit reached.'});
  await page.locator('#question').fill('What are total sales in France?');
  await expect(page.locator('#send')).toBeEnabled();
  await page.locator('#question').press('Enter');
  await expect.poll(() => page.evaluate(() => Boolean(window.__server.pendingAsk))).toBe(true);
  await answer(page, 'Total sales in France are **US$1,240**.', views);
  await expect(page.locator('.turn-answer')).toHaveText('Total sales in France are US$1,240.');
  await expect(page.locator('#note')).toHaveText(
    'The answer was returned, but this sheet’s conversation could not be saved: Prereasoner: spreadsheet session limit reached.'
  );
  await expect(page.locator('#newConversation')).toBeEnabled();
});
