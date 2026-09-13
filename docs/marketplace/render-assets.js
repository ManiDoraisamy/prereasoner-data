const { chromium } = require('@playwright/test');
const path = require('path');
const { pathToFileURL } = require('url');
const fs = require('fs');

(async () => {
  const root = __dirname;
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 800 }, deviceScaleFactor: 1 });

  await page.goto(pathToFileURL(path.join(root, 'listing-screenshot.html')).href);
  await page.screenshot({ path: path.join(root, 'screenshot-1280x800.png') });

  const logoData = fs.readFileSync(path.join(root, 'logo-source.png')).toString('base64');
  for (const size of [32, 48, 96, 128]) {
    await page.setViewportSize({ width: size, height: size });
    await page.setContent(`<style>html,body{margin:0;width:${size}px;height:${size}px;background:transparent}img{display:block;width:${size}px;height:${size}px}</style><img src="data:image/png;base64,${logoData}">`);
    await page.locator('img').waitFor();
    await page.screenshot({ path: path.join(root, `icon-${size}.png`), omitBackground: true });
  }

  await page.setViewportSize({ width: 220, height: 140 });
  await page.goto(pathToFileURL(path.join(root, 'card-banner.svg')).href);
  await page.screenshot({ path: path.join(root, 'card-banner-220x140.png') });
  await browser.close();
})();
