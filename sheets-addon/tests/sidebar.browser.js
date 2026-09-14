const fs = require('fs');
const path = require('path');
const {chromium} = require('@playwright/test');

(async () => {
  const root = path.resolve(__dirname, '..');
  const sidebar = fs.readFileSync(path.join(root, 'Sidebar.html'), 'utf8');
  const resultWire = fs.readFileSync(path.join(root, '..', 'web', 'public', 'lib', 'result-wire.js'), 'utf8');
  const turnRenderer = fs.readFileSync(path.join(root, '..', 'web', 'public', 'lib', 'turn-renderer.js'), 'utf8');
  const mock = `<script>
    window.fetch = async function(url) {
      const text = String(url);
      let value = null;
      if (text.includes('/job-live/views/0.json')) value = {op:'filter',label:'streamed filter',section:'france',section_label:'France orders',section_question:'Orders in France',section_inputs:[],columns:['amount'],rows:[[120]]};
      else if (text.includes('/job-live/views.json')) value = {'0':true};
      else if (text.includes('/job-live/analysis.json')) value = {display_name:'France total'};
      else if (text.includes('/job-live/status.json')) value = 'running';
      else if (text.includes('/runs/test-user/')) value = {status:'running',reply:'Streaming **now**',conversation_id:'c_0123456789abcdef0123456789abcdef',calls:{0:{jobId:'job-live',question:'total amount'}}};
      return {ok:true,json:async()=>value};
    };
    window.__sheetSessionState = {
      client: 'google-sheets-addon', version: 1,
      history: [{role:'user',content:'Who placed the most orders?'},{role:'assistant',content:'Sherlock Holmes placed 3 orders.'}],
      turns: [{question:'Who placed the most orders?',reply:'Sherlock Holmes placed 3 orders.',reasoning:[],result:null}],
      syncedFingerprint: 'sheet-v1'
    };
    window.google = {script: {run: {
      withSuccessHandler(success) {
        return {withFailureHandler() {
          return {
            getSheetContext() { success({spreadsheet:'Prereasoner Sheets Copilot',activeSheet:'Customers',totalRows:23,fingerprint:'sheet-v1',tables:[{name:'Customers',range:'A1:E24',rows:23,columns:5,preview:[['order ID','customer','city','tier'],['101','Sherlock Holmes','London','Gold'],['102','Sherlock Holmes','London','Gold'],['103','Sherlock Holmes','London','Gold']]}]}); },
            restorePrereasonerSheetConversation() { success({conversationId:'c_abcdefabcdefabcdefabcdefabcdefab',state:window.__sheetSessionState,legacy:false,stale:false}); },
            savePrereasonerSheetConversation(payload) { window.__sheetSessionState = payload.state; success({saved:payload.conversationId}); },
            clearPrereasonerSheetConversation() { window.__sheetSessionState = null; success({cleared:'sheet_1234567890'}); },
            syncPrereasonerConversation() { success({changed:false}); },
            getPrereasonerLiveSession() { success({token:'test-token',uid:'test-user',databaseUrl:'https://test.invalid'}); },
            askPrereasoner(payload) { setTimeout(()=>success({question:payload.question,conversationId:'c_0123456789abcdef0123456789abcdef',history:[],reply:'The total is **US$1,240**. [Open source](https://example.com/source).',analysis:{analysis_id:'a_11111111111111111111111111111111',slug:'france_total',revision:1,display_name:'France total'},reasoning:[{label:'France orders',kind:'filter',detail:'Kept the rows that match the question.',sectionId:'france',sectionLabel:'France orders',sectionQuestion:'Orders in France',sectionInputs:[]},{label:'Total amount',kind:'group_agg',detail:'Grouped the matching rows and calculated the measure.',sectionId:'result',sectionLabel:'Total amount',sectionQuestion:'Total amount in France',sectionInputs:['france'],isOutput:true}],result:{headers:['total_usd'],rows:[['1240']]},context:{spreadsheet:'Prereasoner Sheets Copilot',activeSheet:'Customers',totalRows:23,fingerprint:'sheet-v1',tables:[{name:'Customers'}]}}), 500); }
          };
        }};
      }
    }}};
  </script>`;
  const browser = await chromium.launch({headless: true});
  const page = await browser.newPage({viewport: {width: 320, height: 800}, deviceScaleFactor: 1});
  page.setDefaultTimeout(5000);
  const fixtureContext = JSON.stringify({spreadsheet:'Prereasoner Sheets Copilot',activeSheet:'Customers',totalRows:23,fingerprint:'sheet-v1',tables:[{name:'Customers'}],privacyUrl:'https://chat.prereasoner.com/privacy',termsUrl:'https://chat.prereasoner.com/terms',supportUrl:'https://chat.prereasoner.com/support'});
  const prepared = sidebar
    .replace('<script src="https://chat.prereasoner.com/lib/result-wire.js?v=1"></script>', '<script>' + resultWire + '</script>')
    .replace('<script src="https://chat.prereasoner.com/lib/turn-renderer.js?v=2"></script>', '<script>' + turnRenderer + '</script>')
    .replace('<?!= initialContext ?>', fixtureContext)
    .replace('<?!= reasonBase ?>', JSON.stringify('https://chat.prereasoner.com/reason/'));
  await page.setContent(mock + prepared, {waitUntil: 'domcontentloaded'});
  await page.locator('#context').waitFor({state:'hidden'});
  await page.getByText('Sherlock Holmes placed 3 orders.').waitFor();
  if (await page.locator('.legal').count()) throw new Error('The reasoning sidebar must not show legal links');
  await page.getByRole('button', {name: /New chat/}).click();
  await page.getByText('Ask a question about the current sheet.').waitFor();
  if (await page.getByText('Sherlock Holmes placed 3 orders.').count()) throw new Error('New chat did not clear the restored turn');
  await page.getByLabel('Ask about this spreadsheet').fill('total amount in France in US dollars');
  await page.getByRole('button', {name: 'Send'}).click();
  await page.getByText('Streaming now', {exact:false}).waitFor();
  await page.locator('.live-reasoning').waitFor();
  await page.getByText('US$1,240', {exact:false}).waitFor();
  const completedTurn = page.locator('.turn-pair').last();
  const reasoningPosition = await completedTurn.locator('.turn-reasoning').evaluate(node => Array.from(node.parentElement.children).indexOf(node));
  const answerPosition = await completedTurn.locator('.turn-answer').evaluate(node => Array.from(node.parentElement.children).indexOf(node));
  if (reasoningPosition >= answerPosition) throw new Error('Reasoning must use the shared before-answer turn order');
  const sourceLink = page.getByRole('link', {name:'Open source'});
  if (await sourceLink.getAttribute('href') !== 'https://example.com/source') throw new Error('Markdown link was flattened');
  await page.locator('details.reasoning').click();
  await page.getByText('Reasoning steps for France total').waitFor();
  await page.getByText('Total amount in France', {exact:true}).waitFor();
  const analysisHref = await page.getByRole('link', {name:'Open full analysis ↗'}).getAttribute('href');
  if (analysisHref !== 'https://chat.prereasoner.com/reason/c_0123456789abcdef0123456789abcdef') throw new Error('Analysis link was omitted');
  if (await page.locator('.result-table').count()) throw new Error('A scalar answer must not be repeated in a one-cell table');
  const saved = await page.evaluate(() => window.__sheetSessionState);
  if (!saved || saved.turns.length !== 1 || !saved.turns[0].reply.includes('US$1,240') || saved.syncedFingerprint !== 'sheet-v1') {
    throw new Error('The completed sidebar turn was not persisted');
  }
  const out = path.resolve(root, '..', 'test-results', 'sheets-addon-sidebar.png');
  fs.mkdirSync(path.dirname(out), {recursive: true});
  await page.screenshot({path: out, fullPage: true});
  console.log(out);
  await browser.close();
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
