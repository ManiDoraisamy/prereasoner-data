const { chromium } = require('@playwright/test');
const { pathToFileURL } = require('url');
const path = require('path');

(async () => {
  const root = __dirname;
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 720 } });
  const videoUrl = pathToFileURL(path.join(root, 'prereasoner-sheets-copilot-oauth-demo.webm')).href;
  await page.goto(videoUrl);
  await page.locator('video').waitFor();
  await page.evaluate(() => new Promise((resolve) => {
    const video = document.querySelector('video');
    if (video.readyState >= 1) resolve();
    else video.addEventListener('loadedmetadata', resolve, { once: true });
  }));
  const times = [2, 8, 15, 22, 29, 37];
  for (const time of times) {
    await page.evaluate((value) => new Promise((resolve) => {
      const video = document.querySelector('video');
      video.addEventListener('seeked', resolve, { once: true });
      video.currentTime = value;
    }), time);
    await page.screenshot({ path: path.join(root, `demo-frame-${time}.png`) });
  }
  const metadata = await page.evaluate(() => ({
    duration: document.querySelector('video').duration,
    width: document.querySelector('video').videoWidth,
    height: document.querySelector('video').videoHeight
  }));
  console.log(JSON.stringify(metadata));
  await browser.close();
})();
