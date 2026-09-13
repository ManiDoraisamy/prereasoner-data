const fs = require('fs');
const path = require('path');
const {chromium} = require('@playwright/test');

(async () => {
  const root = path.resolve(__dirname, '..');
  const sidebar = fs.readFileSync(path.join(root, 'Sidebar.html'), 'utf8');
  const mock = `<script>
    window.google = {script: {run: {
      withSuccessHandler(success) {
        return {withFailureHandler() {
          return {
            getSheetContext() { success({spreadsheet:'Prereasoner Sheets Copilot',activeSheet:'Customers',totalRows:23,tables:[{name:'Customers',range:'A1:E24',rows:23,columns:5,preview:[['order ID','customer','city','tier'],['101','Sherlock Holmes','London','Gold'],['102','Sherlock Holmes','London','Gold'],['103','Sherlock Holmes','London','Gold']]}]}); },
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
  await page.getByLabel('Ask about this spreadsheet').fill('total amount in France in US dollars');
  await page.getByRole('button', {name: 'Send'}).click();
  await page.getByText('The total amount in France is US$1,240.').waitFor();
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
