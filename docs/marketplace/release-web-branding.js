// Rename only currently hosted public text. Reuse all other deployed file hashes
// and the live Hosting config so unrelated worktree changes cannot be published.
const { execFileSync } = require('child_process');
const { gzipSync } = require('zlib');
const { createHash } = require('crypto');
const fs = require('fs');
const os = require('os');
const path = require('path');
const project = 'prereasoner-inference';
const api = 'https://firebasehosting.googleapis.com/v1beta1/';
const token = execFileSync('pwsh', ['-NoProfile', '-Command', 'gcloud auth print-access-token'], { encoding: 'utf8' }).trim();
const headers = { Authorization: `Bearer ${token}`, 'x-goog-user-project': project };
async function request(route, method = 'GET', body) {
  const response = await fetch(api + route, { method, headers: { ...headers, 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body) });
  if (!response.ok) throw new Error(`${method} ${route}: ${response.status} ${await response.text()}`);
  return response.json();
}
async function latest() { return (await request(`sites/${project}/releases?pageSize=1`)).releases[0]; }
function rename(text) {
  return text.replaceAll('Prereasoner - Sheets Copilot', 'Prereasoner')
    .replaceAll('Prereasoner Sheets Copilot', 'Prereasoner')
    .replace(/Google Sheets(?!™)/g, 'Google Sheets™');
}
(async () => {
  const base = await latest();
  const files = [];
  let pageToken = '';
  do {
    const result = await request(`${base.version.name}/files?pageSize=1000${pageToken ? '&pageToken=' + encodeURIComponent(pageToken) : ''}`);
    files.push(...(result.files || [])); pageToken = result.nextPageToken || '';
  } while (pageToken);
  const manifest = Object.fromEntries(files.map(file => [file.path, file.hash]));
  const blobs = new Map();
  const changes = [];
  const backup = fs.mkdtempSync(path.join(os.tmpdir(), 'prereasoner-branding-release-'));
  const candidates = files.filter(file => /\.(html|js)$/.test(file.path) && !file.path.startsWith('/__/'));
  await Promise.all(candidates.map(async file => {
    const response = await fetch(`https://${project}.web.app${file.path}`);
    if (!response.ok) throw new Error(`Cannot read ${file.path}: ${response.status}`);
    const before = await response.text();
    const after = rename(before);
    if (before === after) return;
    const local = path.join(backup, file.path.slice(1));
    fs.mkdirSync(path.dirname(local), { recursive: true });
    fs.writeFileSync(local + '.before', before);
    fs.writeFileSync(local + '.after', after);
    const compressed = gzipSync(after, { level: 9 });
    const hash = createHash('sha256').update(compressed).digest('hex');
    blobs.set(hash, compressed); manifest[file.path] = hash;
    changes.push(file.path);
  }));
  console.log(JSON.stringify({ baseVersion: base.version.name, fileCount: files.length, changed: changes.sort(), backup }, null, 2));
  if (!process.argv.includes('--release') || !changes.length) return;
  if ((await latest()).version.name !== base.version.name) throw new Error('Live release changed; rerun against the new release.');
  const version = await request(`sites/${project}/versions`, 'POST', { config: base.version.config });
  const population = await request(`${version.name}:populateFiles`, 'POST', { files: manifest });
  for (const hash of population.uploadRequiredHashes || []) {
    if (!blobs.has(hash)) throw new Error('An unchanged deployed file unexpectedly needs upload; stopping.');
    const response = await fetch(`${population.uploadUrl}/${hash}`, { method: 'POST', headers: { ...headers, 'Content-Type': 'application/octet-stream' }, body: blobs.get(hash) });
    if (!response.ok) throw new Error(`Upload failed: ${response.status}`);
  }
  await request(`${version.name}?updateMask=status`, 'PATCH', { status: 'FINALIZED' });
  if ((await latest()).version.name !== base.version.name) throw new Error('Concurrent release detected; new version was not released.');
  const release = await request(`sites/${project}/releases?versionName=${version.name}`, 'POST', { message: 'Prereasoner branding and Google Sheets trademark attribution' });
  fs.writeFileSync(path.join(backup, 'release.json'), JSON.stringify({ previous: base.version.name, release: release.name, version: version.name, changed: changes }, null, 2));
  console.log(JSON.stringify({ release: release.name, version: version.name, previous: base.version.name }, null, 2));
})().catch(error => { console.error(error.message); process.exitCode = 1; });
