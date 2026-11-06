#!/usr/bin/env node
'use strict';

// Build apps/web first. Use an existing Playwright installation and Chrome:
// PLAYWRIGHT_MODULE=/path/to/playwright node scripts/check_generation_ui.cjs
// All API responses are synthetic; no production service or provider is used.
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const { createHash } = require('node:crypto');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '..'), dist = path.join(root, 'apps/web/dist');
const clone = (value) => JSON.parse(JSON.stringify(value));
const hash = (text) => createHash('sha256').update(text).digest('hex');
const hex = (letter) => letter.repeat(64);
const now = '2026-11-06T20:00:00Z';
const project = { id: '00000000-0000-4000-8000-000000000010', name: 'Synthetic project', path: '/synthetic/project', head: 'a'.repeat(40), dirty: false };
const ids = { good: '00000000-0000-4000-8000-000000000001', bad: '00000000-0000-4000-8000-000000000002', run: '00000000-0000-4000-8000-000000000003', badRun: '00000000-0000-4000-8000-000000000004' };
const budget = { max_calls: 2, max_tokens: 100000, max_cost_usd: null, max_seconds: 600 };
const model = { provider: 'openai', model_id: 'synthetic-generation-model', api_mode: 'responses', max_input_tokens: 16384, max_output_tokens: 512, timeout_seconds: 10, input_price_per_million: null, output_price_per_million: null, price_source: null, credential_env: 'SYNTHETIC_MODEL_REFERENCE', billing_mode: 'subscription', allow_private_http: false };
const profile = { id: 'synthetic-profile', name: 'Synthetic generation profile', revision: hex('1'), model, credential_available: true, usage_adapter: 'none', read_only: false };
const license = { status: 'supported', spdx: 'MIT', files: [{ path: 'LICENSE', text: 'SYNTHETIC LICENSE BODY\nPreserve synthetic copyright and attribution.\n<img src="https://outside.invalid/license.png">', sha256: hex('2'), blob_sha: null }], reasons: [] };
const metadata = { source: 'Synthetic approved parent', commit: null, license_status: 'declared', license_text: license.files[0].text, scope: ['controlled synthetic test'], permissions: ['no additional permissions'] };
const parent = { version_id: hex('3'), content_digest: hex('4'), report_sha256: hex('5'), metadata };
const skill = { ...parent, created_at: now, integrity: { valid: true, errors: [] }, review_status: 'approved', files: [{ path: 'SKILL.md', size: 40, sha256: hex('6'), mode: 33188 }], instructions: 'Synthetic parent instructions' };
const importedSkill = { ...clone(skill), version_id: hex('7'), review_status: 'pending', metadata: { ...metadata, source: `doonex-generation:${ids.good}` }, instructions: 'Synthetic parent instructions\nSynthetic append' };
const pendingParent = { ...clone(skill), version_id: hex('8'), review_status: 'pending', metadata: { ...metadata, source: 'Pending parent must not be selectable' } };
const taskset = { taskset_id: hex('9'), name: 'Synthetic tuning tasks', split: 'tuning', tasks: Array.from({ length: 4 }, (_, index) => ({ id: `task-${index + 1}`, title: `Visible task ${index + 1}` })) };
const inputs = { parents: [parent], tasksets: [taskset, { taskset_id: hex('a'), name: 'HELDOUT_MUST_NOT_BE_SELECTABLE', split: 'heldout', tasks: [{ id: 'hidden-task', title: 'Hidden task' }] }], models: [profile, { ...clone(profile), id: 'missing-price', name: 'Missing price', model: { ...model, billing_mode: 'metered' } }, { ...clone(profile), id: 'missing-credential', name: 'Missing credential', credential_available: false }], projects: [{ id: project.id, name: project.name }] };
let revision = 1;
const nextRevision = () => String(revision++).padStart(64, '0');
const makeRecord = (id, body) => ({ id, revision: nextRevision(), state: 'draft', run_id: null, run_state: null, usage: null, input: body, parent: clone(parent), license: clone(license), visible_tasks: body.task_ids.map((id) => ({ id, title: `Visible ${id}`, prompt: 'Visible task prompt\n<img src="https://outside.invalid/task.png">', visible_checks: ['python -m unittest'] })), projection_sha256: hex('b'), model: clone(model), model_profile: { id: profile.id, name: profile.name, revision: profile.revision }, prompt: 'FROZEN MODEL INPUT\nSynthetic parent instructions\nVisible task prompt\n<img src="https://outside.invalid/prompt.png">\n<script>window.generationExecuted = true</script>', prompt_sha256: hex('c'), policy: 'append_instructions_v1', budget: clone(body.budget), candidate: null, preview: null, imported: null, created_at: now, updated_at: now, error: null });
const records = new Map(), runs = new Map();
const usage = { calls: 1, tokens: 120, cost_usd: null, reserved_tokens: 0, reserved_cost_usd: null, unknown_calls: 0 };
const makeRun = (record, id, state) => ({ id, state, reason: null, created_at: now, updated_at: now, phase: 'execution', usage: clone(usage), request: { purpose: 'skill_generation', generation: { id: record.id }, project_id: project.id, project_name: project.name, workflow: 'execute', prompt: record.prompt, base_commit: null, repo_path: null, checks: [], budget: record.budget, model, role_models: { executor: model }, model_profiles: { executor: record.model_profile }, skill: null } });
const structural = { schema_version: 1, generation_id: ids.good, run_id: ids.run, policy: 'append_instructions_v1', valid: true, errors: [], parent_manifest_sha256: parent.content_digest, parent_report_sha256: parent.report_sha256 };
const candidate = { ...structural, output_sha256: hex('d'), report_sha256: hex('e'), manifest_sha256: hex('f'), archive_sha256: hex('0'), content_digest: hex('1') };
const makePreview = () => ({ sha256: hex('2'), instructions: `${importedSkill.instructions}\n<img src="https://outside.invalid/candidate.png">\n<script>window.generationExecuted = true</script>`, diff: '--- a/SKILL.md\n+++ b/SKILL.md\n+Synthetic append\n+<img src="https://outside.invalid/diff.png">', license: clone(license), metadata: clone(importedSkill.metadata), provenance: { generation_id: ids.good, parent_version_id: parent.version_id, run_id: ids.run }, structural_report: clone(structural), evidence: { report_sha256: hex('3'), verified: false } });
const requests = [], violations = [], pageErrors = [], checks = [];
let unknownStart = true, conflictPreview = true, mismatchedPreview = true, unknownImport = true;
let delayId = null, delayResolve = null, readStartedResolve;
let readStarted = new Promise((resolve) => { readStartedResolve = resolve; });
const check = (name, condition) => { assert.ok(condition, name); checks.push(name); };
const json = (response, status, value) => { response.writeHead(status, { 'Content-Type': 'application/json' }); response.end(JSON.stringify(value)); };
const readBody = async (request) => { let body = ''; for await (const chunk of request) { body += chunk; assert.ok(body.length < 10000); } return body ? JSON.parse(body) : {}; };
const server = http.createServer(async (request, response) => {
  try {
    const url = new URL(request.url, 'http://127.0.0.1');
    if (!url.pathname.startsWith('/api/')) {
      const target = url.pathname === '/' ? path.join(dist, 'index.html') : path.join(dist, url.pathname);
      if (!target.startsWith(`${dist}${path.sep}`)) return json(response, 404, {});
      const content = fs.readFileSync(target); response.writeHead(200, { 'Content-Type': target.endsWith('.js') ? 'text/javascript' : target.endsWith('.css') ? 'text/css' : 'text/html' }); return response.end(content);
    }
    assert.equal(request.headers.authorization, 'Bearer synthetic-ui-token');
    const body = await readBody(request), route = `${request.method} ${url.pathname}`;
    requests.push({ method: request.method, path: url.pathname, query: url.search, body });
    if (route === 'GET /api/settings') return json(response, 200, { model, credential_available: true, default_budget: budget });
    if (route === 'GET /api/projects') return json(response, 200, [project]);
    if (route === 'GET /api/model-profiles') return json(response, 200, { profiles: [profile], default_profile_id: profile.id });
    if (route === 'GET /api/arena/recommendations') return json(response, 200, { current: [], history: [] });
    if (route === 'GET /api/skills') return json(response, 200, { skills: [skill, pendingParent, ...(records.get(ids.good)?.imported ? [importedSkill] : [])] });
    if (route === 'GET /api/skills/discoveries') return json(response, 200, { discoveries: [] });
    if (route === `GET /api/skills/${parent.version_id}`) return json(response, 200, skill);
    if (route === `GET /api/skills/${importedSkill.version_id}`) return json(response, 200, importedSkill);
    if (route === `POST /api/skills/${importedSkill.version_id}/review`) { assert.equal(body.status, 'approved'); assert.ok(body.note); importedSkill.review_status = 'approved'; return json(response, 200, importedSkill); }
    if (route === 'GET /api/skills/generations/inputs') return json(response, 200, inputs);
    if (route === 'GET /api/skills/generations') return json(response, 200, { generations: [...records.values()].reverse() });
    if (route === 'POST /api/skills/generations') {
      assert.deepEqual(Object.keys(body).sort(), ['budget', 'model_profile_id', 'parent_version_id', 'project_id', 'task_ids', 'taskset_id']);
      assert.equal(body.project_id, project.id); assert.equal(body.parent_version_id, parent.version_id); assert.equal(body.taskset_id, taskset.taskset_id); assert.equal(body.model_profile_id, profile.id); assert.deepEqual(body.task_ids, ['task-1', 'task-2', 'task-3']); assert.equal(body.budget.max_cost_usd, null);
      const record = makeRecord(ids.good, body); assert.equal(records.size, 0); records.set(record.id, record); return json(response, 200, record);
    }
    const match = url.pathname.match(/^\/api\/skills\/generations\/([a-f0-9-]+)(?:\/(start|preview|import|artifact))?$/);
    if (match) {
      const record = records.get(match[1]); if (!record) return json(response, 404, { detail: { code: 'generation_not_found' } });
      if (request.method === 'GET' && !match[2]) {
        const saved = clone(record);
        if (delayId === record.id) { delayId = null; await new Promise((resolve) => { delayResolve = resolve; readStartedResolve(); }); }
        return json(response, 200, saved);
      }
      if (request.method === 'POST' && match[2] === 'start') {
        assert.deepEqual(body, { expected_revision: record.revision, confirm_model_usage: true, confirm_derivative_rights: true }); assert.equal(record.state, 'draft');
        record.state = 'queued'; record.run_id = ids.run; record.run_state = 'queued'; record.revision = nextRevision(); runs.set(ids.run, makeRun(record, ids.run, 'queued'));
        if (unknownStart) { unknownStart = false; return json(response, 502, { detail: 'PRIVATE_START_RESPONSE_SENTINEL' }); }
        return json(response, 200, record);
      }
      if (request.method === 'POST' && match[2] === 'preview') {
        assert.deepEqual(body, { expected_revision: record.revision }); assert.ok(record.candidate.valid);
        if (conflictPreview) { conflictPreview = false; return json(response, 409, { detail: { code: 'revision_conflict', generation_id: record.id } }); }
        record.preview = makePreview(); record.state = 'preview_ready'; record.revision = nextRevision();
        if (mismatchedPreview) { mismatchedPreview = false; return json(response, 200, { ...record, id: ids.bad }); }
        return json(response, 200, record);
      }
      if (request.method === 'POST' && match[2] === 'import') {
        assert.deepEqual(body, { expected_revision: record.revision, preview_sha256: record.preview.sha256, confirm_source: true, confirm_license: true }); assert.equal(record.state, 'preview_ready');
        record.imported = { version_id: importedSkill.version_id, report_sha256: hex('4'), content_digest: importedSkill.content_digest, at: now }; record.state = 'imported'; record.revision = nextRevision();
        if (unknownImport) { unknownImport = false; return json(response, 502, { detail: { code: 'response_unknown', generation_id: record.id } }); }
        return json(response, 200, { generation: record, skill: importedSkill });
      }
      if (request.method === 'GET' && match[2] === 'artifact') {
        assert.ok(['candidate', 'report', 'output'].includes(url.searchParams.get('name'))); response.writeHead(200, { 'Content-Type': 'application/octet-stream', 'Content-Disposition': 'attachment; filename="synthetic-generation.json"' }); return response.end('{}');
      }
    }
    if (route === 'GET /api/runs') { assert.equal(url.searchParams.get('project_id'), project.id); return json(response, 200, [...runs.values()]); }
    const runMatch = url.pathname.match(/^\/api\/runs\/([a-f0-9-]+)(?:\/(events|diff|evidence|pause|resume))?$/);
    if (runMatch) {
      assert.equal(url.searchParams.get('project_id'), project.id);
      const run = runs.get(runMatch[1]); assert.ok(run);
      if (request.method === 'GET' && !runMatch[2]) return json(response, 200, run);
      if (request.method === 'GET' && runMatch[2] === 'events') return json(response, 200, { events: [], cursor: 0 });
      if (request.method === 'GET' && runMatch[2] === 'diff') return json(response, 200, { diff: makePreview().diff, source: 'evidence' });
      if (request.method === 'GET' && runMatch[2] === 'evidence') return json(response, 200, { verified: false, checks: [], snapshot_digest: candidate.manifest_sha256 });
      if (request.method === 'POST' && ['pause', 'resume'].includes(runMatch[2])) { run.state = runMatch[2] === 'pause' ? 'paused' : 'queued'; recordForRun(run.id).run_state = run.state; return json(response, 200, run); }
    }
    violations.push(`Unexpected API route: ${route}`); return json(response, 500, { detail: 'Unexpected synthetic route' });
  } catch (error) { violations.push(error.message); if (!response.headersSent) json(response, 500, { detail: 'Synthetic contract assertion failed' }); else response.end(); }
});
const recordForRun = (id) => [...records.values()].find((item) => item.run_id === id);
let browser;
const watchdog = setTimeout(() => { process.stderr.write('Generation UI watchdog expired\n'); process.exit(1); }, 120000);
(async () => {
  assert.ok(fs.existsSync(path.join(dist, 'index.html')), 'Build apps/web first');
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  browser = await chromium.launch({ headless: true, executablePath: process.env.CHROME_EXECUTABLE || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  page.on('pageerror', (error) => pageErrors.push(error.message));
  await page.route('**/*', (route) => { if (new URL(route.request().url()).origin !== origin) { violations.push('External browser request'); return route.abort(); } return route.continue(); });
  await page.goto(origin); await page.getByLabel('访问令牌', { exact: true }).fill('synthetic-ui-token'); await page.getByRole('button', { name: '连接工作台', exact: true }).click();
  const generationNav = () => page.getByRole('navigation', { name: '工作区页面' }).getByRole('button', { name: '候选生成', exact: true });
  await generationNav().click(); await page.getByText('尚无生成记录。', { exact: true }).waitFor();
  check('Mount performs local reads only', !requests.some((item) => item.method !== 'GET'));
  check('Generation reads the narrow inputs endpoint', requests.some((item) => item.path === '/api/skills/generations/inputs') && !requests.some((item) => item.path === '/api/arena/tasksets'));
  check('Unapproved parent is not selectable', !(await page.getByLabel('父 Skill 版本', { exact: true }).innerText()).includes(pendingParent.metadata.source));
  check('Heldout set is not selectable', !(await page.getByLabel('Tuning 任务集', { exact: true }).innerText()).includes('HELDOUT'));
  await page.getByLabel('父 Skill 版本', { exact: true }).selectOption(parent.version_id); await page.getByLabel('Tuning 任务集', { exact: true }).selectOption(taskset.taskset_id);
  for (let index = 1; index <= 3; index++) await page.getByLabel(`Visible task ${index} task-${index}`, { exact: true }).check();
  check('Selection is limited to three visible tasks', await page.getByLabel('Visible task 4 task-4', { exact: true }).isDisabled());
  await page.getByLabel('生成模型配置档', { exact: true }).selectOption('missing-price');
  check('Metered model without prices cannot save', await page.getByRole('button', { name: '保存生成草稿', exact: true }).isDisabled());
  await page.getByLabel('生成模型配置档', { exact: true }).selectOption(profile.id);
  await page.getByLabel('生成调用上限', { exact: true }).fill('0'); check('Nonpositive finite budget cannot save', await page.getByRole('button', { name: '保存生成草稿', exact: true }).isDisabled());
  await page.getByLabel('生成调用上限', { exact: true }).fill('2'); await page.getByLabel('生成 Token 上限', { exact: true }).fill('100'); check('Cannot underreserve a model request', await page.getByRole('button', { name: '保存生成草稿', exact: true }).isDisabled());
  await page.getByLabel('生成 Token 上限', { exact: true }).fill('100000');
  check('Subscription cost stays null and disabled', await page.getByLabel('生成费用上限（US$）', { exact: true }).isDisabled());
  await page.getByRole('button', { name: '保存生成草稿', exact: true }).click(); await page.getByRole('button', { name: '启动唯一生成 Run', exact: true }).waitFor();
  check('Saving a draft does not create a Run', runs.size === 0 && requests.filter((item) => item.path.endsWith('/start')).length === 0);
  check('Draft displays complete frozen input and rights text', (await page.locator('.generation-detail').innerText()).includes('FROZEN MODEL INPUT') && (await page.locator('.generation-license').innerText()).includes('SYNTHETIC LICENSE BODY'));
  check('Model input renders as text without upstream execution', await page.locator('.generation-detail img, .generation-detail script').count() === 0 && await page.evaluate(() => window.generationExecuted) === undefined);
  check('Start needs both explicit confirmations', await page.getByRole('button', { name: '启动唯一生成 Run', exact: true }).isDisabled());
  await page.getByLabel('我已核对完整输入、实际模型及有限预算，授权本次 Run 消耗模型用量。', { exact: true }).check(); check('Model budget confirmation alone is insufficient', await page.getByRole('button', { name: '启动唯一生成 Run', exact: true }).isDisabled());
  await page.getByLabel('我已核对父内容、许可及归属，确认本次私有衍生用途的授权。', { exact: true }).check();
  await page.getByRole('button', { name: '启动唯一生成 Run', exact: true }).dblclick(); await page.getByRole('alert').filter({ hasText: 'HTTP 502' }).waitFor();
  check('Unknown start is dispatched once despite double click', requests.filter((item) => item.path.endsWith('/start')).length === 1 && runs.size === 1);
  check('Unknown response omits private error body', !(await page.locator('body').innerText()).includes('PRIVATE_START_RESPONSE_SENTINEL'));
  check('Unknown write blocks immediate replay', await page.getByRole('button', { name: '启动唯一生成 Run', exact: true }).isDisabled());
  const mutations = requests.filter((item) => item.method !== 'GET').length;
  await page.reload(); await generationNav().click(); await page.getByRole('button', { name: '打开原始 Run 与恢复入口', exact: true }).waitFor();
  check('Reload retains unknown-write warning and never replays start', (await page.locator('.generation-view').innerText()).includes('上次写操作尚未核对') && requests.filter((item) => item.method !== 'GET').length === mutations);
  await page.getByRole('button', { name: '读取已保存记录', exact: true }).click();
  await page.getByRole('button', { name: '打开原始 Run 与恢复入口', exact: true }).click(); await page.getByRole('heading', { name: 'Skill 候选生成', exact: true }).waitFor();
  check('Generation Run displays purpose and nullable Git baseline accurately', (await page.locator('.run-meta').innerText()).includes('Git 起始提交不适用') && (await page.locator('.run-meta').innerText()).includes('用途：Skill 候选生成'));
  check('Existing pause control remains available', await page.getByRole('button', { name: '暂停', exact: true }).count() === 1);
  await page.getByRole('tab', { name: '验证与交付', exact: true }).click(); await page.getByRole('heading', { name: '与固定候选绑定的交付证据', exact: true }).waitFor();
  check('Generation Run has no Git adoption action', await page.getByRole('button', { name: '检查采纳条件', exact: true }).count() === 0);
  await page.getByRole('tab', { name: '代码变更', exact: true }).click(); await page.locator('.diff-output').getByText('+Synthetic append', { exact: false }).waitFor();
  check('Generation Run keeps the append diff visible', (await page.locator('.diff-output').innerText()).includes('SKILL.md'));
  await page.getByRole('button', { name: '查看固定候选与导入', exact: true }).click();
  const good = records.get(ids.good); good.state = 'candidate_ready'; good.run_state = 'completed_unverified'; good.candidate = clone(candidate); good.revision = nextRevision(); runs.get(ids.run).state = 'completed_unverified';
  await page.getByRole('button', { name: '读取已保存记录', exact: true }).click(); await page.getByRole('heading', { name: '候选结构检查通过，质量尚未验证', exact: true }).waitFor();
  check('Structure is explicitly distinct from quality', (await page.locator('.generation-detail').innerText()).includes('不代表能力提高、兼容、安全'));
  for (const [label, name] of [['下载原始输出', 'output'], ['下载结构报告', 'report'], ['下载完整候选', 'candidate']]) {
    const [download] = await Promise.all([page.waitForEvent('download'), page.getByRole('button', { name: label, exact: true }).click()]);
    assert.equal(download.suggestedFilename(), 'synthetic-generation.json');
    assert.equal(await download.failure(), null);
    assert.ok(requests.some((item) => item.method === 'GET' && item.path === `/api/skills/generations/${ids.good}/artifact` && item.query === `?name=${name}`));
  }
  check('Bound output, report and candidate download through authenticated reads', requests.filter((item) => item.path.endsWith('/artifact')).length === 3);
  await page.getByRole('button', { name: '保存固定候选预览', exact: true }).click(); await page.getByRole('alert').filter({ hasText: 'revision_conflict' }).waitFor();
  check('Preview conflict does not retry or import', requests.filter((item) => item.path.endsWith('/preview')).length === 1 && good.preview === null && !requests.some((item) => item.path.endsWith('/import')));
  await page.getByRole('button', { name: '读取已保存记录', exact: true }).click(); await page.getByRole('button', { name: '保存固定候选预览', exact: true }).click();
  await page.getByRole('alert').filter({ hasText: '生成记录归属不匹配' }).waitFor();
  check('Mismatched success response keeps an unknown-write marker without replay', (await page.locator('.generation-view').innerText()).includes('上次写操作尚未核对') && requests.filter((item) => item.path.endsWith('/preview')).length === 2);
  await page.getByRole('button', { name: '读取已保存记录', exact: true }).click(); await page.getByRole('heading', { name: '固定候选预览', exact: true }).waitFor();
  await page.getByText('候选完整指令原文', { exact: true }).click();
  check('Preview diff, instructions and license cannot load external content', await page.locator('.generation-preview img, .generation-preview script').count() === 0 && await page.evaluate(() => window.generationExecuted) === undefined);
  check('Preview displays its exact saved digest', (await page.locator('.generation-preview').innerText()).includes(good.preview.sha256));
  check('Import requires two fresh confirmations', await page.getByRole('button', { name: '导入为待审核版本', exact: true }).isDisabled());
  await page.getByLabel('我已核对父版本、生成来源、追加差异与完整候选。', { exact: true }).check(); check('Source confirmation alone cannot import', await page.getByRole('button', { name: '导入为待审核版本', exact: true }).isDisabled());
  await page.getByLabel('我已阅读保留的许可、归属及新增文本说明，确认本次导入。', { exact: true }).check();
  await page.getByRole('button', { name: '导入为待审核版本', exact: true }).click(); await page.getByRole('button', { name: '打开版本并审核', exact: true }).waitFor();
  check('Unknown import reads committed original record without replay', requests.filter((item) => item.path.endsWith('/import')).length === 1 && good.imported.version_id === importedSkill.version_id);
  check('Import never auto approves or starts regression', importedSkill.review_status === 'pending' && !requests.some((item) => item.path.endsWith('/review')) && !requests.some((item) => /\/arena\/(evaluations|tasksets)/.test(item.path)));
  await page.getByRole('button', { name: '读取已保存记录', exact: true }).click(); await page.getByRole('button', { name: '打开版本并审核', exact: true }).click(); await page.getByRole('heading', { name: `固定版本 ${importedSkill.version_id}`, exact: true }).waitFor();
  check('Generated version enters the original pending review flow', (await page.locator('.skill-detail').innerText()).includes('待审核'));
  await page.getByLabel('审核说明', { exact: true }).fill('Synthetic explicit manual review'); await page.getByRole('button', { name: '批准此版本', exact: true }).click(); await page.getByText('审核已记录：已批准。', { exact: true }).waitFor();
  check('Only an explicit original review action approves', requests.filter((item) => item.path.endsWith('/review')).length === 1);
  const bad = makeRecord(ids.bad, clone(good.input)); bad.state = 'failed'; bad.run_id = ids.badRun; bad.run_state = 'failed'; records.set(ids.bad, bad); const badRun = makeRun(bad, ids.badRun, 'failed'); badRun.reason = 'model_timeout'; badRun.usage = { ...clone(usage), tokens: 0, reserved_tokens: 16896, unknown_calls: 1 }; runs.set(ids.badRun, badRun);
  await generationNav().click(); await page.getByRole('button', { name: '读取已保存记录', exact: true }).click();
  const nav = page.getByRole('navigation', { name: '候选生成记录', exact: true }); await nav.getByRole('button').filter({ hasText: ids.bad }).click(); await page.getByText('model_timeout', { exact: true }).waitFor(); await page.getByText('1 次调用实际用量未知，预留保留。', { exact: false }).waitFor();
  check('Failed Run preserves unknown usage and cannot preview', (await page.locator('.generation-detail').innerText()).includes('16,896 tokens') && await page.getByRole('button', { name: '保存固定候选预览', exact: true }).count() === 0);
  delayId = ids.good; await nav.getByRole('button').filter({ hasText: ids.good }).click(); await readStarted;
  await nav.getByRole('button').filter({ hasText: ids.bad }).click(); assert.ok(delayResolve); delayResolve(); await page.getByText('model_timeout', { exact: true }).waitFor();
  check('A delayed earlier read cannot replace a later selection', (await page.locator('.generation-detail').innerText()).includes(ids.bad) && !(await page.locator('.generation-detail').innerText()).includes(ids.good));
  await page.setViewportSize({ width: 390, height: 844 }); check('Mobile failed record has no document overflow', await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  await nav.getByRole('button').filter({ hasText: ids.good }).click(); await page.getByRole('heading', { name: '固定候选预览', exact: true }).waitFor(); await page.getByText('候选完整指令原文', { exact: true }).click(); check('Mobile full candidate preview has no document overflow', await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  check('Never calls hidden taskset, Git adoption or background mutation routes', !requests.some((item) => item.path === '/api/arena/tasksets' || item.path.endsWith('/adoption-check') || item.method !== 'GET' && /\/background\//.test(item.path)));
  check('Generation creates only its one explicit Run', requests.filter((item) => item.path.endsWith('/start')).length === 1 && !requests.some((item) => item.method === 'POST' && item.path === '/api/runs'));
  check('No external requests or mock contract failures', violations.length === 0); check('No browser runtime errors', pageErrors.length === 0);
  const files = ['apps/web/src/generation.tsx', 'apps/web/src/api.ts', 'apps/web/src/main.tsx', 'apps/web/src/style.css', 'scripts/check_generation_ui.cjs'];
  const output = { source_commit: execFileSync('git', ['rev-parse', 'HEAD'], { cwd: root, encoding: 'utf8' }).trim(), source_files_sha256: Object.fromEntries(files.map((file) => [file, hash(fs.readFileSync(path.join(root, file)))])), passed: checks.length, checks, api_requests: requests.length, real_model_calls: 0, production_database_access: 0, external_requests: 0, mutations: requests.filter((item) => item.method !== 'GET').map(({ method, path }) => ({ method, path })) };
  if (process.env.DOONEX_UI_EVIDENCE_DIR) { fs.mkdirSync(process.env.DOONEX_UI_EVIDENCE_DIR, { recursive: true }); fs.writeFileSync(path.join(process.env.DOONEX_UI_EVIDENCE_DIR, 'generation-ui-results.json'), JSON.stringify(output, null, 2) + '\n'); await page.screenshot({ path: path.join(process.env.DOONEX_UI_EVIDENCE_DIR, 'generation-mobile.png'), fullPage: true }); }
  process.stdout.write(JSON.stringify(output, null, 2) + '\n');
})().catch((error) => { process.stderr.write(JSON.stringify({ error: error.message, checks, violations, pageErrors }, null, 2) + '\n'); process.exitCode = 1; }).finally(async () => { clearTimeout(watchdog); delayResolve?.(); await browser?.close(); server.closeAllConnections(); server.close(); });
