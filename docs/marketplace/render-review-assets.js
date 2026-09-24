// Marketplace artwork built from the shipped add-on components, with example data.
// The UI remains editable vector HTML inside SVG; PNGs are the Marketplace uploads.
const fs = require('fs');
const path = require('path');
const { pathToFileURL } = require('url');
const { chromium } = require('@playwright/test');
const root = path.resolve(__dirname, '../..');
const out = __dirname;
const read = relative => fs.readFileSync(path.join(root, relative), 'utf8');
const escape = value => String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;');
const logoPath = 'C:/work/FormFacade/public/logo-full.png';
const logo = fs.readFileSync(logoPath).toString('base64');
const conversationId = 'c_0123456789abcdef0123456789abcdef';
const question = 'What are total sales in France?';
const context = { spreadsheet: 'Sales', activeSheet: 'Orders', totalRows: 4, fingerprint: 'sample-1',
  tables: [{ name: 'Orders', rows: 4, columns: 2 }], privacyUrl: 'https://chat.prereasoner.com/privacy' };
const turn = { question, reply: 'Total sales in France are **US$1,240**.',
  analysis: { analysis_id: 'a_11111111111111111111111111111111', revision: 1, display_name: 'France sales' },
  reasoning: [
    { label: 'France orders', kind: 'filter', detail: 'Country = France (2 source rows).' },
    { label: 'Total sales', kind: 'group_agg', detail: '840 + 400 = 1,240 USD.' }
  ], result: { headers: ['total_usd'], rows: [[1240]] } };

async function loadSidebar(page, answered) {
  const state = answered ? { conversationId, state: { client: 'google-sheets-addon', version: 1,
    turns: [turn], history: [], syncedFingerprint: 'sample-1' } } : {};
  const bridge = `<script>window.google={script:{run:{withSuccessHandler(ok){return {withFailureHandler(){return {
    restorePrereasonerSheetConversation(){ok(${JSON.stringify(state)})},
    getSheetContext(){ok(${JSON.stringify(context)})},
    syncPrereasonerConversation(){ok({changed:false})}
  }}}}}}};</script>`;
  let html = read('sheets-addon/Sidebar.html')
    .replace('<?!= initialContext ?>', JSON.stringify(context))
    .replace('<?!= reasonBase ?>', JSON.stringify('https://chat.prereasoner.com/reason/'))
    .replace('<script src="https://chat.prereasoner.com/lib/result-wire.js?v=1"></script>',
      '<script>' + read('web/public/lib/result-wire.js') + '</script>')
    .replace('<script src="https://chat.prereasoner.com/lib/turn-renderer.js?v=2"></script>',
      '<script>' + read('web/public/lib/turn-renderer.js') + '</script>');
  await page.setContent(bridge + html, { waitUntil: 'networkidle' });
  if (answered) {
    await page.getByText('US$1,240', { exact: false }).waitFor();
    await page.locator('details.reasoning > summary').click();
    await page.getByLabel('Ask about this spreadsheet').fill('Which product sold the most?');
  } else {
    await page.getByText('Ask a question about the current sheet.').waitFor();
    await page.getByLabel('Ask about this spreadsheet').fill(question);
  }
}

// Freeze computed styles, not a screenshot: text and UI geometry stay sharp in SVG.
async function vectorPanel(page, width, height) {
  return page.evaluate(({ width, height }) => {
    const wrapper = document.createElementNS('http://www.w3.org/1999/xhtml', 'div');
    wrapper.setAttribute('style', `width:${width}px;height:${height}px;position:relative;overflow:hidden;background:white;font:13px Arial,sans-serif;color:#202124;`);
    function copy(node) {
      if (node.nodeType === Node.TEXT_NODE) return document.createTextNode(node.textContent);
      if (node.nodeType !== Node.ELEMENT_NODE || ['SCRIPT', 'STYLE', 'LINK'].includes(node.tagName)) return null;
      const clone = document.createElementNS('http://www.w3.org/1999/xhtml', node.tagName.toLowerCase());
      const css = getComputedStyle(node);
      if (css.display === 'none') return null;
      for (const property of css) clone.style.setProperty(property, css.getPropertyValue(property));
      if (css.position === 'fixed') clone.style.position = 'absolute';
      for (const attr of node.attributes) {
        if (!/^on/i.test(attr.name) && !['style', 'id'].includes(attr.name)) clone.setAttribute(attr.name, attr.value);
      }
      if (node.tagName === 'TEXTAREA') clone.textContent = node.value;
      else for (const child of node.childNodes) { const next = copy(child); if (next) clone.appendChild(next); }
      return clone;
    }
    for (const child of document.body.children) { const next = copy(child); if (next) wrapper.appendChild(next); }
    return new XMLSerializer().serializeToString(wrapper);
  }, { width, height });
}

function text(x, y, value, size = 24, weight = 400, fill = '#202124') {
  return `<text x="${x}" y="${y}" font-family="Arial, sans-serif" font-size="${size}" font-weight="${weight}" fill="${fill}">${escape(value)}</text>`;
}
function sourceTable() {
  const rows = [['France', '$840'], ['France', '$400'], ['Germany', '$620'], ['Spain', '$260']];
  return text(56, 373, 'EXAMPLE SOURCE DATA', 15, 700, '#70757a') +
    '<rect x="56" y="393" width="538" height="255" rx="10" fill="#fff" stroke="#dadce0"/>' +
    text(80, 428, 'Country', 21, 700) + text(355, 428, 'Amount (USD)', 21, 700) +
    rows.map((row, i) => '<path d="M56 ' + (444 + i * 50) + 'H594" stroke="#e8eaed"/>' +
      text(80, 478 + i * 50, row[0], 22) + text(448, 478 + i * 50, row[1], 22)).join('');
}
function artwork(scene, panel) {
  const names = { ask: ['Ask a question.', 'Use your own words.'], answer: ['See the answer.', 'Inspect each calculation.'], previous: ['Reopen a conversation.', 'Continue your analysis.'] };
  const lines = names[scene];
  const scale = 1.5;
  const width = scene === 'previous' ? 360 : 320;
  const height = scene === 'previous' ? 300 : 420;
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
  <rect x="674" y="78" width="${width * scale + 2}" height="${height * scale + 60}" rx="12" fill="#fff" stroke="#dadce0"/>
  ${text(696, 115, scene === 'previous' ? 'Previous conversations' : 'Prereasoner', 24, 600)}
  <path d="M674 134H${674 + width * scale + 2}" stroke="#dadce0"/>
  <g transform="translate(675 136) scale(${scale})"><foreignObject width="${width}" height="${height}">${panel}</foreignObject></g>
  ${text(56, 702, scene === 'answer' ? 'Source cells stay unchanged.' : scene === 'ask' ? 'Extensions → Prereasoner → Ask a question' : 'Saved to your Prereasoner account.', 20, 400, '#5f6368')}
  ${text(56, 756, 'Google Sheets™ is a trademark of Google LLC.', 14, 400, '#70757a')}
  ${text(700, 778, 'Example data · Actual add-on interface, enlarged', 13, 400, '#70757a')}
  </svg>`;
}

(async () => {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 320, height: 420 }, deviceScaleFactor: 2 });
  try {
    for (const [index, scene] of ['ask', 'answer', 'previous'].entries()) {
      if (scene === 'previous') {
        await page.setViewportSize({ width: 360, height: 300 });
        const initialData = { conversations: [
          { id: conversationId, question: 'What are total sales in France?', ts: '2026-09-24T10:15:00Z' },
          { id: 'c_11111111111111111111111111111111', question: 'Which product sold the most?', ts: '2026-09-23T15:20:00Z' },
          { id: 'c_22222222222222222222222222222222', question: 'Compare sales by country', ts: '2026-09-22T09:00:00Z' }
        ] };
        await page.setContent(read('sheets-addon/Previous.html')
          .replace('<?!= initialData ?>', JSON.stringify(initialData))
          .replace('<?!= reasonBase ?>', JSON.stringify('https://chat.prereasoner.com/reason/')), { waitUntil: 'networkidle' });
      } else await loadSidebar(page, scene === 'answer');
      await page.mouse.move(0, 0);
      const panel = await vectorPanel(page, scene === 'previous' ? 360 : 320, scene === 'previous' ? 300 : 420);
      const filename = `review-${index + 1}-${scene}`;
      fs.writeFileSync(path.join(out, filename + '.svg'), artwork(scene, panel));
      const rendered = await browser.newPage({ viewport: { width: 1280, height: 800 }, deviceScaleFactor: 1 });
      await rendered.goto(pathToFileURL(path.join(out, filename + '.svg')).href);
      await rendered.screenshot({ path: path.join(out, filename + '-1280x800.png') });
      await rendered.close();
      console.log(filename);
    }
    const banner = await browser.newPage({ viewport: { width: 220, height: 140 }, deviceScaleFactor: 1 });
    await banner.goto(pathToFileURL(path.join(out, 'card-banner.svg')).href);
    await banner.screenshot({ path: path.join(out, 'card-banner-220x140.png') });
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
