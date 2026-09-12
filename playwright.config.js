const {defineConfig} = require('@playwright/test');

module.exports = defineConfig({
  testDir: './web/tests/browser',
  timeout: 30000,
  expect: {timeout: 10000},
  use: {
    baseURL: 'http://127.0.0.1:4173',
    headless: true,
    channel: process.env.PLAYWRIGHT_CHANNEL || undefined,
    trace: 'retain-on-failure',
  },
  webServer: {
    command: 'node web/tests/browser/server.js',
    url: 'http://127.0.0.1:4173/__health',
    reuseExistingServer: false,
    timeout: 10000,
  },
});
