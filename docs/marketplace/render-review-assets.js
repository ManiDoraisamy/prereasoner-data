// Marketplace artwork rendered from the shipped add-on, with example data: the sidebar
// (sheets-addon/Sidebar.html, run by web/tests/browser/sheets-sidebar-harness.js exactly as the browser
// test runs it) and the Previous conversations dialog (sheets-addon/Previous.html). PNGs are the
// Marketplace uploads; each SVG wraps its PNG.
const fs = require('fs');
const path = require('path');
const {pathToFileURL} = require('url');
const {chromium} = require('@playwright/test');
const {openSidebar, answer, views} = require('../../web/tests/browser/sheets-sidebar-harness.js');

const root = path.resolve(__dirname, '../..');
const out = __dirname;
const read = relative => fs.readFileSync(path.join(root, relative), 'utf8');
const escape = value => String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;');
const logoPath = 'C:/work/FormFacade/public/logo-full.png';
const logo = fs.readFileSync(logoPath).toString('base64');
const question = 'What are total sales in France?';
const cells = [['Country', 'Amount (USD)'], ['France', 840], ['France', 400], ['Germany', 620], ['Spain', 260]];
const panels = {ask: {width: 300, height: 420}, answer: {width: 300, height: 420}, previous: {width: 360, height: 300}};

async function capture(browser, scene) {
  const size = panels[scene];
  const context = await browser.newContext({viewport: size, deviceScaleFactor: 3});
  try {
    const page = await context.newPage();
    if (scene === 'previous') {
      const initialData = {conversations: [
        {id: 'c_0123456789abcdef0123456789abcdef', question, ts: '2026-09-24T10:15:00Z'},
        {id: 'c_11111111111111111111111111111111', question: 'Which product sold the most?', ts: '2026-09-23T15:20:00Z'},
        {id: 'c_22222222222222222222222222222222', question: 'Compare sales by country', ts: '2026-09-22T09:00:00Z'}
      ]};
      await page.route('https://ssl.gstatic.com/**', route => route.fulfill({contentType: 'text/css', body: ''}));
      await page.setContent(read('sheets-addon/Previous.html')
        .replace('<?!= initialData ?>', JSON.stringify(initialData))
        .replace('<?!= reasonBase ?>', JSON.stringify('https://chat.prereasoner.com/reason/')), {waitUntil: 'networkidle'});
    } else {
      await openSidebar(page, cells);
      await page.locator('.empty').waitFor();
      await page.locator('#question').fill(question);
      if (scene === 'answer') {
        await page.locator('#question').press('Enter');
        await page.waitForFunction(() => Boolean(window.__server.pendingAsk));
        await answer(page, 'Total sales in France are **US$1,240**.', views);
        await page.locator('.turn-answer').waitFor();
        await page.locator('.turn-reasoning summary').click();
        await page.locator('#question').fill('Which product sold the most?');
      }
    }
    await page.mouse.move(0, 0);
    await page.waitForTimeout(300);
    return (await page.screenshot()).toString('base64');
  } finally { await context.close(); }
}

function text(x, y, value, size = 24, weight = 400, fill = '#202124') {
  return `<text x="${x}" y="${y}" font-family="Arial, sans-serif" font-size="${size}" font-weight="${weight}" fill="${fill}">${escape(value)}</text>`;
}
function sourceTable() {
  const rows = cells.slice(1).map(([country, amount]) => [country, '$' + amount]);
  return text(56, 373, 'EXAMPLE SOURCE DATA', 15, 700, '#70757a') +
    '<rect x="56" y="393" width="538" height="255" rx="10" fill="#fff" stroke="#dadce0"/>' +
    text(80, 428, 'Country', 21, 700) + text(355, 428, 'Amount (USD)', 21, 700) +
    rows.map((row, i) => '<path d="M56 ' + (444 + i * 50) + 'H594" stroke="#e8eaed"/>' +
      text(80, 478 + i * 50, row[0], 22) + text(448, 478 + i * 50, row[1], 22)).join('');
}
function artwork(scene, png) {
  const names = {ask: ['Ask a question.', 'Use your own words.'], answer: ['See the answer.', 'Inspect each calculation.'], previous: ['Reopen a conversation.', 'Continue your analysis.']};
  const lines = names[scene];
  const scale = 1.5;
  const width = panels[scene].width * scale;
  const height = panels[scene].height * scale;
  const left = scene === 'previous'
    ? text(56, 362, 'Extensions → Prereasoner', 26, 600) + text(56, 409, '→ Previous conversations', 26, 600) +
      text(56, 479, 'Select a saved conversation to open', 23, 400, '#5f6368') +
      text(56, 514, 'its full analysis in Prereasoner.', 23, 400, '#5f6368')
    : sourceTable();
  return `<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="800" viewBox="0 0 1280 800" role="img">
  <title>Prereasoner — ${escape(lines.join(' '))}</title>
  <desc>The actual add-on interface rendered with illustrative sample data and enlarged for readability.</desc>
  <rect width="1280" height="800" fill="#f8f9fa"/>
  <image href="data:image/png;base64,${logo}" x="52" y="38" width="50" height="50"/>
  ${text(118, 71, 'Prereasoner', 29, 700)}
  ${text(56, 187, lines[0], 39, 700)}${text(56, 241, lines[1], 39, 700)}
  ${text(56, 293, 'For use with Google Sheets™', 23, 400, '#5f6368')}
  ${left}
  <rect x="674" y="78" width="${width + 2}" height="${height + 60}" rx="12" fill="#fff" stroke="#dadce0"/>
  ${text(696, 115, scene === 'previous' ? 'Previous conversations' : 'Prereasoner', 24, 600)}
  <path d="M674 134H${674 + width + 2}" stroke="#dadce0"/>
  <image href="data:image/png;base64,${png}" x="675" y="136" width="${width}" height="${height}"/>
  ${text(56, 702, scene === 'answer' ? 'Source cells stay unchanged.' : scene === 'ask' ? 'Extensions → Prereasoner → Ask a question' : 'Saved to your Prereasoner account.', 20, 400, '#5f6368')}
  ${text(56, 756, 'Google Sheets™ is a trademark of Google LLC.', 14, 400, '#70757a')}
  ${text(700, 778, 'Example data · Actual add-on interface, enlarged', 13, 400, '#70757a')}
  </svg>`;
}

(async () => {
  const browser = await chromium.launch({headless: true});
  try {
    for (const [index, scene] of ['ask', 'answer', 'previous'].entries()) {
      const png = await capture(browser, scene);
      const filename = `review-${index + 1}-${scene}`;
      fs.writeFileSync(path.join(out, filename + '.svg'), artwork(scene, png));
      const rendered = await browser.newPage({viewport: {width: 1280, height: 800}, deviceScaleFactor: 1});
      await rendered.goto(pathToFileURL(path.join(out, filename + '.svg')).href);
      await rendered.screenshot({path: path.join(out, filename + '-1280x800.png')});
      await rendered.close();
      console.log(filename);
    }
    const banner = await browser.newPage({viewport: {width: 220, height: 140}, deviceScaleFactor: 1});
    await banner.goto(pathToFileURL(path.join(out, 'card-banner.svg')).href);
    await banner.screenshot({path: path.join(out, 'card-banner-220x140.png')});
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
