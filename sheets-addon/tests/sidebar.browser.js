const fs = require('fs');
const path = require('path');
const {chromium} = require('@playwright/test');

(async () => {
  const root = path.resolve(__dirname, '..');
  const sidebar = fs.readFileSync(path.join(root, 'Sidebar.html'), 'utf8');
  const mock = `<script>
    window.__sheetSessionState = {
      client: 'google-sheets-addon', version: 1,
      history: [{role:'user',content:'Who placed the most orders?'},{role:'assistant',content:'Sherlock Holmes placed 3 orders.'}],
      turns: [{question:'Who placed the most orders?',reply:'Sherlock Holmes placed 3 orders.',reasoning:[],result:null}]
    };
    window.google = {script: {run: {
      withSuccessHandler(success) {
        return {withFailureHandler() {
          return {
            getSheetContext() { success({spreadsheet:'Prereasoner Sheets Copilot',activeSheet:'Customers',totalRows:23,tables:[{name:'Customers',range:'A1:E24',rows:23,columns:5,preview:[['order ID','customer','city','tier'],['101','Sherlock Holmes','London','Gold'],['102','Sherlock Holmes','London','Gold'],['103','Sherlock Holmes','London','Gold']]}]}); },
            restorePrereasonerSheetConversation() { success({conversationId:'c_abcdefabcdefabcdefabcdefabcdefab',state:window.__sheetSessionState,legacy:false,stale:false}); },
            savePrereasonerSheetConversation(payload) { window.__sheetSessionState = payload.state; success({saved:payload.conversationId}); },
            clearPrereasonerSheetConversation() { window.__sheetSessionState = null; success({cleared:'sheet_1234567890'}); },
            syncPrereasonerConversation() { success({changed:false}); },
            askPrereasoner(payload) { success({question:payload.question,conversationId:'c_0123456789abcdef0123456789abcdef',history:[],reply:'The total amount in France is US$1,240.',reasoning:[{label:'France orders',kind:'filter',detail:'Kept the rows that match the question.'},{label:'Total amount',kind:'group_agg',detail:'Grouped the matching rows and calculated the measure.'}],result:{headers:['total_usd'],rows:[['1240']]}}); }
          };
        }};
      }
    }}};
  </script>`;
  const browser = await chromium.launch({headless: true});
  const page = await browser.newPage({viewport: {width: 320, height: 800}, deviceScaleFactor: 1});
  page.setDefaultTimeout(5000);
  const fixtureContext = JSON.stringify({spreadsheet:'Prereasoner Sheets Copilot',activeSheet:'Customers',totalRows:23,tables:[{name:'Customers'}],privacyUrl:'https://chat.prereasoner.com/privacy',termsUrl:'https://chat.prereasoner.com/terms',supportUrl:'https://chat.prereasoner.com/support'});
  await page.setContent(mock + sidebar.replace('<?!= initialContext ?>', fixtureContext), {waitUntil: 'domcontentloaded'});
  await page.locator('#context').waitFor({state:'hidden'});
  await page.getByText('Sherlock Holmes placed 3 orders.').waitFor();
  if (await page.locator('.legal').count()) throw new Error('The reasoning sidebar must not show legal links');
  await page.getByRole('button', {name: /New chat/}).click();
  await page.getByText('Ask a question about the current sheet.').waitFor();
  if (await page.getByText('Sherlock Holmes placed 3 orders.').count()) throw new Error('New chat did not clear the restored turn');
  await page.getByLabel('Ask about this spreadsheet').fill('total amount in France in US dollars');
  await page.getByRole('button', {name: 'Send'}).click();
  await page.getByText('The total amount in France is US$1,240.').waitFor();
  if (await page.locator('.result-table').count()) throw new Error('A scalar answer must not be repeated in a one-cell table');
  const saved = await page.evaluate(() => window.__sheetSessionState);
  if (!saved || saved.turns.length !== 1 || saved.turns[0].reply !== 'The total amount in France is US$1,240.') {
    throw new Error('The completed sidebar turn was not persisted');
  }
  await page.locator('details').click();
  const out = path.resolve(root, '..', 'test-results', 'sheets-addon-sidebar.png');
  fs.mkdirSync(path.dirname(out), {recursive: true});
  await page.screenshot({path: out, fullPage: true});
  console.log(out);
  await browser.close();
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
