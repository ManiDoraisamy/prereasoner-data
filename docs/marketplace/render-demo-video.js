const { chromium } = require('@playwright/test');
const path = require('path');
const fs = require('fs');

(async () => {
  const root = __dirname;
  const videoDir = path.join(root, '.video-tmp');
  fs.mkdirSync(videoDir, { recursive: true });
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({
    viewport: { width: 1280, height: 720 },
    recordVideo: { dir: videoDir, size: { width: 1280, height: 720 } }
  });
  const page = await context.newPage();
  const video = page.video();
  const imageData = (file) => `data:image/png;base64,${fs.readFileSync(file).toString('base64')}`;
  let html = fs.readFileSync(path.join(root, 'oauth-demo.html'), 'utf8');
  html = html
    .replaceAll('screenshot-1280x800.png', imageData(path.join(root, 'screenshot-1280x800.png')))
    .replaceAll('../../test-results/sheets-addon-sidebar.png', imageData(path.join(root, '..', '..', 'test-results', 'sheets-addon-sidebar.png')))
    .replaceAll('../../test-results/sheets-addon-previous.png', imageData(path.join(root, '..', '..', 'test-results', 'sheets-addon-previous.png')));
  await page.setContent(html, { waitUntil: 'load' });
  await page.waitForFunction(() => [...document.images].every((image) => image.complete && image.naturalWidth > 0));
  await page.waitForTimeout(1000);
  for (let index = 0; index < 6; index += 1) {
    await page.evaluate((scene) => window.showScene(scene), index);
    await page.waitForTimeout(index === 0 || index === 5 ? 6000 : 7000);
  }
  await context.close();
  await browser.close();
  const source = await video.path();
  const target = path.join(root, 'prereasoner-sheets-copilot-oauth-demo.webm');
  fs.copyFileSync(source, target);
  console.log(target);
})();
