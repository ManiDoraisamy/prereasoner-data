// The Google Sheets add-on's sidebar (sheets-addon/Sidebar.html), run as Sheets runs it: on an Apps Script
// content origin, loading the web rail's shared component, the upload importer and the shared Firebase
// module from chat.prereasoner.com (served here from web/public), with google.script.run standing in for
// Code.js and a realtime database the test drives.
const {test, expect} = require('@playwright/test');
const {conversation, orders, views, openSidebar, answer} = require('./sheets-sidebar-harness.js');

const calls = (page, name) => page.evaluate(n => window.__calls.filter(call => call.name === n), name);

test('the Sheets sidebar renders the web rail, with live steps from the realtime trace', async ({page}) => {
  await openSidebar(page, orders);
  await expect(page.locator('.empty')).toContainText('Ask a question about the current sheet.');
  await expect(page.locator('.data-notice')).toContainText('This data is not used to train generalized AI models.');
  await expect(page.locator('#sheetCount')).toHaveText('1 tab: Orders');
  expect(await page.evaluate(() => window.__signedInWith)).toBe('google-token');
  const [restore] = await calls(page, 'restorePrereasonerSheetConversation');
  expect(restore.arg.tables).toEqual([{name: 'Orders', data: 'country,amount\nFrance,840\nFrance,400\nGermany,620',
    source: {kind: 'google-sheets-addon'}}]);

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

test('the Sheets sidebar refuses a shifted header row, then reads the fixed sheet without the unnamed column', async ({page}) => {
  // The owner's sheet: an inserted index column shifted the header row one cell left, so "amount" sits
  // over text and the amounts have no header. Every answer would read the wrong column.
  const shifted = [['order ID', 'customer', 'amount'], [1, 101, 'Holmes', 118], [2, 102, 'Watson', 95]];
  await openSidebar(page, shifted);
  await expect(page.locator('.empty.sheet-error')).toContainText('Sheet "Orders": Column D has values but no header, and the ' +
    'headers look one column to the left of their data (C1 "amount" is above "Holmes"). Put each header above its data.');
  expect(await calls(page, 'restorePrereasonerSheetConversation')).toEqual([]);
  await page.locator('#question').fill('total amount');
  await page.locator('#question').press('Enter');
  await expect(page.locator('.answer.error')).toContainText('the headers look one column to the left of their data');
  await expect(page.locator('#question')).toHaveValue('total amount');

  // The owner's fix: the headers moved right, leaving A1 empty over the row numbers. That column is
  // not a field; it is left out, the note says so, and the question runs on the rest.
  await page.evaluate(() => { window.__server.rows = [['', 'order ID', 'customer', 'amount'], [1, 101, 'Holmes', 118], [2, 102, 'Watson', 95]]; });
  await page.locator('#question').press('Enter');
  await expect.poll(() => page.evaluate(() => Boolean(window.__server.pendingAsk))).toBe(true);
  expect((await calls(page, 'restorePrereasonerSheetConversation')).length).toBe(1);
  expect((await page.evaluate(() => window.__server.pendingAsk.arg)).tables[0].data)
    .toBe('order ID,customer,amount\n101,Holmes,118\n102,Watson,95');
  await expect(page.locator('#note')).toHaveText('Sheet "Orders": column A has no header, so it was left out.');
  await expect(page.locator('.answer.error')).toHaveCount(0);
});
