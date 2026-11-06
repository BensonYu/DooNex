#!/usr/bin/env node
'use strict';

// Build apps/web first. Run with an existing Playwright installation, for example:
// PLAYWRIGHT_MODULE=/path/to/playwright node scripts/check_discovery_ui.cjs
// CHROME_EXECUTABLE may select an already installed Chrome. No browser is downloaded.
// All API responses are synthetic and served on an ephemeral loopback port.
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const { createHash } = require('node:crypto');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

const root = path.resolve(__dirname, '..');
const dist = path.join(root, 'apps/web/dist');
const clone = (value) => JSON.parse(JSON.stringify(value));
const hex = (letter) => letter.repeat(64);
const now = '2026-11-06T20:00:00Z';
const ids = { good: '00000000-0000-4000-8000-000000000001', bad: '00000000-0000-4000-8000-000000000002' };
const candidateA = hex('a'), candidateB = hex('b');
let revision = 1;
const nextRevision = () => String(revision++).padStart(64, '0');
const source = { repository_id: 123, repository: 'example/skills', commit: 'c'.repeat(40), root_tree: 'd'.repeat(40), prefix_tree: 'e'.repeat(40), resolved_at: now, api_version: '2022-11-28' };
const budget = { max_requests: 32, requests_used: 4, remaining_requests: 28, max_bytes: 33554432, bytes_used: 512, bytes_reserved: 0, remaining_bytes: 33553920 };
const makeRecord = (id) => ({ id, revision: nextRevision(), status: 'ready', created_at: now, updated_at: now, input: { repository: 'example/skills', ref: 'main', prefix: 'skills' }, source, candidates: [{ id: candidateA, path: 'skills/supported', tree_sha: 'f'.repeat(40), file_count: 2, entry_count: 2, total_bytes: 400 }, { id: candidateB, path: 'skills/unknown', tree_sha: 'a'.repeat(40), file_count: 1, entry_count: 1, total_bytes: 100 }], preview: null, imported: null, budget, requests: [{ sequence: 1, kind: 'repository', status: 'completed', received_bytes: 512, http_status: 200, error: null }], error: null, source_rate_limit: { remaining: 54, reset: 1794000000 } });
const makePreview = (candidateId) => ({ candidate_id: candidateId, sha256: candidateId === candidateA ? hex('1') : hex('2'), instructions: 'Synthetic instructions\n<img src="https://outside.invalid/preview.png">\n<script>window.upstreamExecuted = true</script>', files: [{ path: 'SKILL.md', mode: 33188, size: 100, sha256: hex('3'), blob_sha: 'b'.repeat(40) }, ...(candidateId === candidateA ? [{ path: 'LICENSE.txt', mode: 33188, size: 300, sha256: hex('4'), blob_sha: 'c'.repeat(40) }] : [])], directories: ['examples'], license: { status: candidateId === candidateA ? 'supported' : 'preview_only', spdx: candidateId === candidateA ? 'MIT' : null, files: candidateId === candidateA ? [{ path: 'LICENSE.txt', text: 'SYNTHETIC MIT LICENSE BODY\n<img src="https://outside.invalid/license.png">', sha256: hex('4'), blob_sha: 'c'.repeat(40) }] : [], reasons: candidateId === candidateA ? [] : ['license_missing'] }, importable: candidateId === candidateA });
const skill = { version_id: hex('5'), content_digest: hex('6'), created_at: now, metadata: { source: `https://github.com/example/skills/tree/${source.commit}/skills/supported`, commit: source.commit, license_status: 'declared', license_text: 'SYNTHETIC MIT LICENSE BODY', scope: [], permissions: [] }, review_status: 'pending', integrity: { valid: true, errors: [] }, files: makePreview(candidateA).files, instructions: 'Synthetic imported instructions' };
const existingSkill = { ...clone(skill), version_id: hex('7'), review_status: 'approved', metadata: { ...skill.metadata, source: 'existing-local-skill' } };
const records = new Map();
const requests = [], violations = [], pageErrors = [], checks = [];
let delayReadId = null, delayResolve = null;
let conflictNextPreview = false;
let unknownNextPreview = false;
let signalReadStarted;
let readStarted = new Promise((resolve) => { signalReadStarted = resolve; });
const check = (name, condition) => { assert.ok(condition, name); checks.push(name); };
const json = (response, status, value) => { response.writeHead(status, { 'Content-Type': 'application/json' }); response.end(JSON.stringify(value)); };
const readBody = async (request) => { let text = ''; for await (const chunk of request) { text += chunk; assert.ok(text.length < 10000); } return text ? JSON.parse(text) : {}; };
const server = http.createServer(async (request, response) => {
  try {
    const url = new URL(request.url, 'http://127.0.0.1');
    if (!url.pathname.startsWith('/api/')) {
      const target = url.pathname === '/' ? path.join(dist, 'index.html') : path.join(dist, url.pathname);
      if (!target.startsWith(`${dist}${path.sep}`)) return json(response, 404, {});
      const content = fs.readFileSync(target);
      response.writeHead(200, { 'Content-Type': target.endsWith('.js') ? 'text/javascript' : target.endsWith('.css') ? 'text/css' : 'text/html' });
      return response.end(content);
    }
    const body = await readBody(request);
    requests.push({ method: request.method, path: url.pathname, body });
    const route = `${request.method} ${url.pathname}`;
    if (route === 'GET /api/settings') return json(response, 200, { model: null, credential_available: false, default_budget: { max_calls: 1, max_tokens: 1000, max_cost_usd: null, max_seconds: 60 } });
    if (route === 'GET /api/projects') return json(response, 200, []);
    if (route === 'GET /api/model-profiles') return json(response, 200, { profiles: [], default_profile_id: null });
    if (route === 'GET /api/arena/recommendations') return json(response, 200, { current: [], history: [] });
    if (route === 'GET /api/skills') return json(response, 200, { skills: [existingSkill, ...(records.get(ids.good)?.imported ? [skill] : [])] });
    if (route === `GET /api/skills/${existingSkill.version_id}`) return json(response, 200, existingSkill);
    if (route === `GET /api/skills/${skill.version_id}`) return json(response, 200, skill);
    if (route === `POST /api/skills/${skill.version_id}/review`) { assert.equal(body.status, 'approved'); assert.ok(body.note); skill.review_status = 'approved'; return json(response, 200, skill); }
    if (route === 'GET /api/skills/discoveries') return json(response, 200, { discoveries: [...records.values()].reverse() });
    if (route === 'POST /api/skills/discoveries') {
      assert.deepEqual(Object.keys(body).sort(), ['prefix', 'ref', 'repository']);
      if (body.repository === 'example/failure') {
        const failed = makeRecord(ids.bad); failed.input.repository = 'example/failure'; failed.status = 'failed'; failed.candidates = []; failed.error = { code: 'github_timeout' };
        failed.budget = { ...budget, requests_used: 5, remaining_requests: 27, bytes_reserved: 2097152, remaining_bytes: budget.remaining_bytes - 2097152 };
        failed.requests.push({ sequence: 2, kind: 'tree', status: 'unknown', received_bytes: 0, http_status: null, error: 'github_timeout' }); records.set(ids.bad, failed);
        return json(response, 400, { detail: { code: 'github_timeout', discovery_id: ids.bad } });
      }
      assert.deepEqual(body, { repository: 'example/skills', ref: 'main', prefix: 'skills' });
      const record = makeRecord(ids.good); records.set(ids.good, record); return json(response, 200, record);
    }
    const match = url.pathname.match(/^\/api\/skills\/discoveries\/([a-f0-9-]+)(?:\/(preview|import))?$/);
    if (match) {
      const record = records.get(match[1]); if (!record) return json(response, 404, { detail: { code: 'discovery_not_found' } });
      if (request.method === 'GET' && !match[2]) {
        const frozen = clone(record);
        if (delayReadId === record.id) { delayReadId = null; await new Promise((resolve) => { delayResolve = resolve; signalReadStarted(); }); }
        return json(response, 200, frozen);
      }
      if (request.method === 'POST' && match[2] === 'preview') {
        assert.deepEqual(Object.keys(body).sort(), ['candidate_id', 'expected_revision']); assert.equal(body.expected_revision, record.revision);
        if (conflictNextPreview) { conflictNextPreview = false; return json(response, 409, { detail: { code: 'revision_conflict', discovery_id: record.id } }); }
        if (unknownNextPreview) { unknownNextPreview = false; return json(response, 502, { detail: 'PRIVATE_PROVIDER_BODY_SENTINEL' }); }
        record.preview = makePreview(body.candidate_id); record.revision = nextRevision(); record.status = 'preview_ready'; return json(response, 200, record);
      }
      if (request.method === 'POST' && match[2] === 'import') {
        assert.deepEqual(Object.keys(body).sort(), ['candidate_id', 'confirm_license', 'confirm_source', 'expected_revision', 'permissions', 'preview_sha256', 'scope']);
        assert.equal(body.candidate_id, candidateA); assert.equal(body.expected_revision, record.revision); assert.equal(body.preview_sha256, record.preview.sha256); assert.equal(body.confirm_source, true); assert.equal(body.confirm_license, true);
        assert.deepEqual(body.scope, ['web testing']); assert.deepEqual(body.permissions, ['no additional permissions']);
        record.imported = { candidate_id: candidateA, version_id: skill.version_id }; record.status = 'imported'; record.revision = nextRevision();
        return json(response, 200, { discovery: record, skill });
      }
    }
    violations.push(`Unexpected API route: ${route}`); return json(response, 500, { detail: 'Unexpected synthetic API request' });
  } catch (error) { violations.push(error.message); if (!response.headersSent) json(response, 500, { detail: 'Synthetic assertion failed' }); else response.end(); }
});

let browser;
const timeout = setTimeout(() => { process.stderr.write('Synthetic UI watchdog expired\n'); process.exit(1); }, 120000);
(async () => {
  assert.ok(fs.existsSync(path.join(dist, 'index.html')), 'Build apps/web first');
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  browser = await chromium.launch({ headless: true, executablePath: process.env.CHROME_EXECUTABLE || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  page.on('pageerror', (error) => pageErrors.push(error.message));
  await page.route('**/*', (route) => { if (new URL(route.request().url()).origin !== origin) { violations.push('External browser request'); return route.abort(); } return route.continue(); });
  await page.goto(origin);
  await page.getByLabel('访问令牌', { exact: true }).fill('synthetic-ui-token');
  await page.getByRole('button', { name: '连接工作台', exact: true }).click();
  await page.getByRole('navigation', { name: '工作区页面' }).getByRole('button', { name: 'Skills', exact: true }).click();
  await page.getByText('从公开 GitHub 仓库发现 Skill', { exact: true }).click();
  await page.getByText('尚无发现回执。', { exact: true }).waitFor();
  check('Mount only reads local discovery history', !requests.some((item) => item.method !== 'GET'));
  await page.getByLabel('公开仓库（owner/repo）', { exact: true }).fill('example/skills');
  await page.getByLabel('仓库内目录（可选）', { exact: true }).fill('skills');
  await page.getByRole('button', { name: '开始一次有限发现', exact: true }).click();
  await page.getByRole('button', { name: '预览固定候选', exact: true }).waitFor();
  check('Shows full frozen commit', (await page.locator('.discovery-detail').innerText()).includes(source.commit));
  check('Shows separate request and provider budgets', (await page.locator('.discovery-detail').innerText()).includes('剩余 28 次') && (await page.locator('.discovery-detail').innerText()).includes('剩余 54 次'));
  conflictNextPreview = true;
  await page.getByRole('button', { name: '预览固定候选', exact: true }).click();
  await page.getByRole('alert').filter({ hasText: 'revision_conflict' }).waitFor();
  check('Conflict reads original receipt without retrying mutation', requests.filter((item) => item.path.endsWith('/preview')).length === 1 && records.get(ids.good).preview === null);
  await page.getByRole('button', { name: '预览固定候选', exact: true }).click();
  await page.getByRole('heading', { name: '固定候选预览', exact: true }).waitFor();
  check('Requires both explicit import confirmations', await page.getByRole('button', { name: '导入固定候选', exact: true }).isDisabled());
  await page.getByText('查看候选指令原文', { exact: true }).click();
  await page.getByText('LICENSE.txt', { exact: true }).first().click();
  check('Instructions and license remain text', await page.locator('.discovery-preview img, .discovery-preview script').count() === 0 && await page.evaluate(() => window.upstreamExecuted) === undefined);
  check('License body visible', (await page.locator('.discovery-preview').innerText()).includes('SYNTHETIC MIT LICENSE BODY'));
  await page.getByLabel('候选目录', { exact: true }).selectOption(candidateB);
  await page.getByRole('button', { name: '预览固定候选', exact: true }).click();
  await page.getByText('license_missing', { exact: true }).waitFor();
  check('Unknown license cannot import', await page.getByRole('button', { name: '导入固定候选', exact: true }).isDisabled());
  check('Unknown license cannot confirm', await page.getByLabel('我已核对固定 commit、候选路径和文件清单。', { exact: true }).isDisabled());
  check('Preview did not import or approve', !requests.some((item) => item.path.endsWith('/import') || item.path.endsWith('/review')));
  await page.getByLabel('候选目录', { exact: true }).selectOption(candidateA);
  await page.getByRole('button', { name: '预览固定候选', exact: true }).click();
  await page.getByText('许可正文可核对：MIT。导入后仍需人工审核。', { exact: true }).waitFor();
  unknownNextPreview = true;
  await page.getByRole('button', { name: '预览固定候选', exact: true }).click();
  await page.getByText('上次写操作尚未核对，请先读取已保存回执，再决定后续操作。', { exact: true }).waitFor();
  check('Unknown mutation blocks replay before reading saved receipt', await page.getByRole('button', { name: '预览固定候选', exact: true }).isDisabled() && await page.getByRole('button', { name: '导入固定候选', exact: true }).isDisabled());
  check('Unexpected discovery errors omit raw response body', !(await page.locator('body').innerText()).includes('PRIVATE_PROVIDER_BODY_SENTINEL'));
  await page.getByRole('button', { name: '读取此回执', exact: true }).click();
  await page.getByRole('button', { name: '预览固定候选', exact: true }).waitFor({ state: 'visible' });
  await page.getByLabel('适用范围（每行一项）', { exact: true }).first().fill('web testing');
  await page.getByLabel('声明权限（每行一项）', { exact: true }).first().fill('no additional permissions');
  await page.getByLabel('我已核对固定 commit、候选路径和文件清单。', { exact: true }).check();
  check('Source confirmation alone is insufficient', await page.getByRole('button', { name: '导入固定候选', exact: true }).isDisabled());
  await page.getByLabel('我已阅读候选内许可正文及适用范围依据，确认本次导入。', { exact: true }).check();
  await page.getByRole('button', { name: '导入固定候选', exact: true }).click();
  await page.getByRole('heading', { name: `固定版本 ${skill.version_id}`, exact: true }).waitFor();
  check('Import opens existing pending review flow', (await page.locator('.skill-detail').innerText()).includes('待审核') && requests.filter((item) => item.path.endsWith('/import')).length === 1);
  check('Existing local version retained', await page.getByRole('navigation', { name: 'Skill 版本', exact: true }).getByText('existing-local-skill', { exact: true }).count() === 1);
  check('No automatic approval', !requests.some((item) => item.path.endsWith('/review')));
  await page.getByLabel('审核说明', { exact: true }).fill('Synthetic explicit manual review');
  await page.getByRole('button', { name: '批准此版本', exact: true }).click();
  await page.getByText('审核已记录：已批准。', { exact: true }).waitFor();
  check('Approval needs explicit existing review action', requests.filter((item) => item.path.endsWith('/review')).length === 1);
  await page.getByLabel('公开仓库（owner/repo）', { exact: true }).fill('example/failure');
  await page.getByRole('button', { name: '开始一次有限发现', exact: true }).click();
  await page.locator('.discovery-detail .notice').getByText('github_timeout', { exact: true }).waitFor();
  check('Failed mutation recovers saved receipt with local GET', requests.some((item) => item.method === 'GET' && item.path === `/api/skills/discoveries/${ids.bad}`));
  check('Unknown request and bytes reservation remain visible', (await page.locator('.discovery-detail').innerText()).includes('2,097,152 bytes'));
  await page.getByText('有限请求记录（2）', { exact: true }).click();
  check('Unknown request result is visible', (await page.locator('.discovery-detail').innerText()).includes('结果未知'));
  delayReadId = ids.good;
  const nav = page.getByRole('navigation', { name: 'GitHub 发现回执', exact: true });
  await nav.getByRole('button').filter({ hasText: 'example/skills' }).click();
  await readStarted;
  await page.waitForFunction(() => document.querySelector('.discovery-detail')?.textContent.includes('00000000-0000-4000-8000-000000000001'));
  await nav.getByRole('button').filter({ hasText: 'example/failure' }).click();
  assert.ok(delayResolve); delayResolve();
  await page.locator('.discovery-detail .notice').getByText('github_timeout', { exact: true }).waitFor();
  check('Earlier read cannot overwrite later selection', (await page.locator('.discovery-detail').innerText()).includes(ids.bad) && !(await page.locator('.discovery-detail').innerText()).includes(ids.good));
  const mutations = requests.filter((item) => item.method !== 'GET').length;
  await page.reload();
  await page.getByRole('navigation', { name: '工作区页面' }).getByRole('button', { name: 'Skills', exact: true }).click();
  await page.getByText('从公开 GitHub 仓库发现 Skill', { exact: true }).click();
  await page.locator('.discovery-detail .notice').getByText('github_timeout', { exact: true }).waitFor();
  check('Reload reads saved history without replay', requests.filter((item) => item.method !== 'GET').length === mutations);
  await page.setViewportSize({ width: 390, height: 844 });
  check('Mobile failure receipt has no document overflow', await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  await page.getByRole('navigation', { name: 'GitHub 发现回执', exact: true }).getByRole('button').filter({ hasText: 'example/skills' }).click();
  await page.getByRole('button', { name: '打开版本并审核', exact: true }).waitFor();
  await page.locator('.discovery-preview').getByText('查看候选指令原文', { exact: true }).click();
  await page.locator('.discovery-preview').getByText('LICENSE.txt', { exact: true }).first().click();
  await page.locator('.discovery-preview').getByText('文件清单与摘要（2）', { exact: true }).click();
  check('Mobile imported preview has no document overflow', await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  check('No Run, Arena or background mutation', !requests.some((item) => item.method !== 'GET' && /\/(runs|arena|background)(\/|$)/.test(item.path)));
  check('No external requests or synthetic server contract failures', violations.length === 0);
  check('No browser runtime errors', pageErrors.length === 0);
  const sourceFiles = ['apps/web/src/api.ts', 'apps/web/src/skills.tsx', 'apps/web/src/style.css', 'scripts/check_discovery_ui.cjs'];
  const output = { source_commit: execFileSync('git', ['rev-parse', 'HEAD'], { cwd: root, encoding: 'utf8' }).trim(), source_files_sha256: Object.fromEntries(sourceFiles.map((file) => [file, createHash('sha256').update(fs.readFileSync(path.join(root, file))).digest('hex')])), passed: checks.length, checks, api_requests: requests.length, model_calls: 0, upstream_executions: 0, external_requests: 0, mutations: requests.filter((item) => item.method !== 'GET').map(({ method, path }) => ({ method, path })) };
  if (process.env.DOONEX_UI_EVIDENCE_DIR) { fs.mkdirSync(process.env.DOONEX_UI_EVIDENCE_DIR, { recursive: true }); fs.writeFileSync(path.join(process.env.DOONEX_UI_EVIDENCE_DIR, 'discovery-ui-results.json'), JSON.stringify(output, null, 2) + '\n'); await page.screenshot({ path: path.join(process.env.DOONEX_UI_EVIDENCE_DIR, 'discovery-mobile.png'), fullPage: true }); }
  process.stdout.write(JSON.stringify(output, null, 2) + '\n');
})().catch((error) => { process.stderr.write(JSON.stringify({ error: error.message, checks, violations, pageErrors }, null, 2) + '\n'); process.exitCode = 1; }).finally(async () => { clearTimeout(timeout); delayResolve?.(); await browser?.close(); server.closeAllConnections(); server.close(); });
