// The `test` every spec that loads pages from the fixture server (server.js) imports. Each test's
// browser context carries its own state cookie, and the server keeps the request count, the deleted
// conversation and the stored analysis revisions per cookie. Every test therefore starts from a
// fresh state, whatever ran before it and however many workers share the one server (for example
// with --repeat-each). A cookie stays on the server's origin; an extra header would also reach the
// cross-origin requests the pages make and trigger CORS preflights there.
const {test: base, expect} = require('@playwright/test');
const {randomUUID} = require('node:crypto');
const {STATE_COOKIE} = require('./server.js');

const test = base.extend({
  context: async ({context, baseURL}, use) => {
    await context.addCookies([{name: STATE_COOKIE, value: randomUUID(), url: baseURL}]);
    await use(context);
  },
});

module.exports = {test, expect};
