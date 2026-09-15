// Repeatable local browser smoke for CE-LOCAL-001.
// Usage: node tests/community_launch_smoke.js [http://127.0.0.1:8090]
const { chromium } = require('@playwright/test');

(async () => {
  const base = (process.argv[2] || process.env.COMMUNITY_UI_URL || 'http://127.0.0.1:8090').replace(/\/$/, '');
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext();
  await context.addInitScript(() => sessionStorage.setItem('pr_test_auth', '1'));
  const page = await context.newPage();

  try {
    await page.goto(base + '/', { waitUntil: 'domcontentloaded' });
    await page.locator('#chips .nm').first().waitFor({ state: 'visible', timeout: 30_000 });
    const sheets = await page.locator('#chips .nm').allTextContents();
    if (sheets.length !== 1 || sheets[0] !== 'orders') throw new Error(`unexpected demo sheets: ${sheets.join(', ')}`);
    const question = await page.locator('#q').inputValue();
    if (question !== 'total amount in France in US dollars') throw new Error(`unexpected default question: ${question}`);
    const ask = page.getByRole('button', { name: 'Ask', exact: true });
    if (!(await ask.isEnabled())) throw new Error('Ask button is disabled after demo load');
    await ask.click();
    await page.waitForURL(/\/reason\/c_[0-9a-f]{32}$/i, { timeout: 180_000 });
    await page.locator('.wb.result').waitFor({ state: 'visible', timeout: 180_000 });
    const body = await page.locator('body').innerText();
    if (/Could not|error|failed/i.test(body)) throw new Error('result page contains an error');
    console.log(JSON.stringify({ base, sheets, question, url: page.url(), resultVisible: true }));
  } finally {
    await context.close();
    await browser.close();
  }
})().catch(error => { console.error(error.stack || error); process.exitCode = 1; });
