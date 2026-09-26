// Tests share one fixture server (server.js). It used to keep one request count, deleted flag and
// revision store for its whole lifetime, so a repeated or reordered test saw another test's requests:
// with --repeat-each, the release journey's second run expected 1 request and found 4. Each test's
// browser context now carries its own state cookie (fixtures.js), and the server keys state by it.
const {test, expect} = require('./fixtures.js');
const {randomUUID} = require('node:crypto');
const {STATE_COOKIE} = require('./server.js');

test('each test starts from its own fixture-server state', async ({page, playwright, baseURL}) => {
  // This test's state: one answered question, stored as an analysis revision, and a deleted conversation.
  await page.request.post('/chat', {headers: {authorization: 'Bearer local-dev'}, data: {message: 'total amount'}});
  await page.request.post('/api/conversation/delete', {data: {id: 'c_0123456789abcdef0123456789abcdef'}});
  expect(await (await page.request.get('/__state')).json()).toEqual({deleted: true, requestCount: 1});
  const revision = '/api/analysis?analysis_id=a_11111111111111111111111111111111&revision=1';
  expect((await page.request.get(revision)).status()).toBe(200);

  // Another test's state on the same server is untouched by all of that.
  const other = await playwright.request.newContext({baseURL, extraHTTPHeaders: {cookie: STATE_COOKIE + '=' + randomUUID()}});
  expect(await (await other.get('/__state')).json()).toEqual({deleted: false, requestCount: 0});
  expect((await (await other.get('/api/conversations')).json()).conversations).toHaveLength(1);
  expect((await other.get(revision)).status()).toBe(404);

  // A request for state without the cookie is refused, never served from a shared default.
  const bare = await playwright.request.newContext({baseURL});
  expect((await bare.get('/__state')).status()).toBe(400);
  await other.dispose();
  await bare.dispose();
});
