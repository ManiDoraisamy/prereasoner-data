const fs = require('fs');
const path = require('path');
const {chromium} = require('@playwright/test');

(async () => {
  const root = path.resolve(__dirname, '..');
  const source = fs.readFileSync(path.join(root, 'Previous.html'), 'utf8');
  const data = JSON.stringify({conversations: [
    {id: 'c_6852132aa60a4260a1af3ecced605b4d', question: 'total amount in France in US dollars', ts: '2026-09-13T13:35:00Z'},
    {id: 'c_0123456789abcdef0123456789abcdef', question: 'Find the top 2 product categories', ts: '2026-09-12T15:25:00Z'}
  ]});
  const html = source
    .replace('<?!= initialData ?>', data)
    .replace('<?!= reasonBase ?>', JSON.stringify('https://chat.prereasoner.com/reason/'));
  const browser = await chromium.launch({headless: true});
  const page = await browser.newPage({viewport: {width: 360, height: 520}, deviceScaleFactor: 1});
  page.setDefaultTimeout(5000);
  await page.setContent(html, {waitUntil: 'domcontentloaded'});
  await page.getByText('total amount in France in US dollars').waitFor();
  const href = await page.getByRole('link', {name: 'total amount in France in US dollars'}).getAttribute('href');
  if (href !== 'https://chat.prereasoner.com/reason/c_6852132aa60a4260a1af3ecced605b4d') {
    throw new Error('Conversation link did not preserve the server conversation ID.');
  }
  const out = path.resolve(root, '..', 'test-results', 'sheets-addon-previous.png');
  fs.mkdirSync(path.dirname(out), {recursive: true});
  await page.screenshot({path: out, fullPage: true});
  console.log(out);
  await browser.close();
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
