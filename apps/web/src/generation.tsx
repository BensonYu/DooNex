import { useEffect, useRef, useState } from 'react';
import { Api, ApiError, type Budget, type GenerationInputs, type GenerationLicense, type Run, type SkillGeneration, type SkillVersion } from './api';
import { formatMoney } from './model-settings';
import { SkillMetadataReport } from './skills';

const root = '/skills/generations';
const states: Record<string, string> = { draft: '草稿 · 未启动', queued: '等待生成', running: '正在生成', candidate_ready: '候选已保存 · 待检查', candidate_invalid: '候选结构无效', preview_ready: '预览已保存 · 待审核', imported: '已导入 Library', paused: '已暂停', pausing: '正在暂停', interrupted: '中断待核查', failed: '生成失败', cancelled: '已取消', cancelling: '正在取消', budget_exceeded: '预算已用尽', completed_unverified: '待检查' };
const storageKey = 'doonex.selectedGeneration', unknownKey = 'doonex.generationNeedsRead';
const read = (key: string) => { try { return localStorage.getItem(key) ?? ''; } catch { return ''; } };
const write = (key: string, value: string) => { try { value ? localStorage.setItem(key, value) : localStorage.removeItem(key); } catch { /* The view remains usable without browser storage. */ } };
const textError = (error: unknown) => error instanceof Error ? error.message : String(error);
const when = (value: string) => new Date(value).toLocaleString('zh-CN', { hour12: false });
const validBudget = (budget: Budget, subscription: boolean) => Number.isInteger(budget.max_calls) && budget.max_calls >= 1 && budget.max_calls <= 100
  && Number.isInteger(budget.max_tokens) && budget.max_tokens >= 1 && budget.max_tokens <= 10000000
  && Number.isInteger(budget.max_seconds) && budget.max_seconds >= 1 && budget.max_seconds <= 7200
  && (subscription || budget.max_cost_usd != null && Number.isFinite(budget.max_cost_usd) && budget.max_cost_usd > 0);

function License({ value }: { value: GenerationLicense }) {
  return <section className="generation-license" aria-label="固定许可与归属">
    <h3>许可与归属正文</h3><p className="small">{value.status === 'supported' ? `已保存可核对的 ${value.spdx} 正文。` : '许可依据不满足本次生成条件。'}父授权、版权与 NOTICE 继续保留；新增模型文本仍须人工核对。</p>
    {value.reasons.length > 0 && <p className="warning-text small">{value.reasons.join('、')}</p>}
    {value.files.map((file) => <details key={file.path} open><summary>{file.path}</summary><pre>{file.text}</pre><p className="small path-text">SHA-256 <code>{file.sha256}</code></p></details>)}
  </section>;
}

function BudgetSummary({ value }: { value: Budget }) {
  return <span>{value.max_calls} 次调用 · {value.max_tokens.toLocaleString()} tokens · {value.max_seconds} 秒 · 费用上限 {formatMoney(value.max_cost_usd)}</span>;
}

export function GenerationView({ api, projectId, defaultBudget, onOpenRun, onImported }: {
  api: Api; projectId: string; defaultBudget: Budget;
  onOpenRun: (projectId: string, runId: string) => void;
  onImported: (skill: SkillVersion) => void;
}) {
  const [inputs, setInputs] = useState<GenerationInputs | null>(null);
  const [records, setRecords] = useState<SkillGeneration[]>([]);
  const [selectedId, setSelectedId] = useState(() => read(storageKey));
  const [selected, setSelected] = useState<SkillGeneration | null>(null);
  const [run, setRun] = useState<Run | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const [parentId, setParentId] = useState('');
  const [tasksetId, setTasksetId] = useState('');
  const [taskIds, setTaskIds] = useState<string[]>([]);
  const [modelId, setModelId] = useState('');
  const [ownerProject, setOwnerProject] = useState(projectId);
  const [budget, setBudget] = useState(defaultBudget);
  const [confirmUsage, setConfirmUsage] = useState(false);
  const [confirmRights, setConfirmRights] = useState(false);
  const [confirmSource, setConfirmSource] = useState(false);
  const [confirmLicense, setConfirmLicense] = useState(false);
  const [busy, setBusy] = useState('');
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState('');
  const [pollError, setPollError] = useState('');
  const [needsRead, setNeedsRead] = useState(() => read(unknownKey) === '1');
  const operation = useRef({ generation: 0, controller: null as AbortController | null });
  const busyRef = useRef('');
  const readEpoch = useRef(0);
  const activeId = useRef(selectedId);
  const resetConfirmation = () => { setConfirmUsage(false); setConfirmRights(false); setConfirmSource(false); setConfirmLicense(false); };
  const uncertain = (value: boolean) => { setNeedsRead(value); write(unknownKey, value ? '1' : ''); };
  const save = (record: SkillGeneration) => { setRecords((old) => [record, ...old.filter((item) => item.id !== record.id)]); setSelected(record); };
  const select = (id: string) => {
    operation.current.generation += 1; operation.current.controller?.abort(); operation.current.controller = null;
    readEpoch.current += 1; busyRef.current = ''; setBusy(''); setSelected(null); setRun(null);
    activeId.current = id; setSelectedId(id); write(storageKey, id); setShowCreate(false); resetConfirmation(); setError(''); setPollError('');
  };
  const show = (record: SkillGeneration) => {
    activeId.current = record.id; setSelectedId(record.id); write(storageKey, record.id); save(record); setShowCreate(false); resetConfirmation();
  };
  const perform = async (kind: string, action: (signal: AbortSignal, current: () => boolean) => Promise<void>) => {
    if (busyRef.current) return;
    const mutation = ['create', 'start', 'preview', 'import'].includes(kind);
    if (mutation && needsRead) return;
    const controller = new AbortController(), generation = ++operation.current.generation;
    operation.current.controller?.abort(); operation.current.controller = controller;
    readEpoch.current += 1; busyRef.current = kind; setBusy(kind); setError('');
    const current = () => !controller.signal.aborted && operation.current.generation === generation;
    if (mutation) { uncertain(true); resetConfirmation(); }
    try { await action(controller.signal, current); if (current() && mutation) uncertain(false); }
    catch (error) {
      if (!current()) return;
      setError(textError(error));
      if (error instanceof ApiError && error.generationId) {
        try { const record = await api.get<SkillGeneration>(`${root}/${encodeURIComponent(error.generationId)}`, controller.signal); if (current()) show(record); } catch { /* Keep the original failure and require a read. */ }
      }
    } finally { if (current()) { busyRef.current = ''; setBusy(''); operation.current.controller = null; readEpoch.current += 1; } }
  };
  const readRecords = async (explicit = false) => perform('read', async (signal, current) => {
    const [options, history] = await Promise.all([api.get<GenerationInputs>(`${root}/inputs`, signal), api.get<{ generations: SkillGeneration[] }>(root, signal)]);
    if (!current()) return;
    setInputs(options); setRecords(history.generations); setLoaded(true);
    const id = history.generations.find((item) => item.id === activeId.current)?.id ?? history.generations[0]?.id ?? '';
    if (id) {
      const record = await api.get<SkillGeneration>(`${root}/${encodeURIComponent(id)}`, signal);
      if (!current() || record.id !== id) return;
      show(record);
    } else { activeId.current = ''; setSelectedId(''); write(storageKey, ''); setSelected(null); setShowCreate(true); }
    if (explicit) uncertain(false);
  });
  useEffect(() => {
    void readRecords();
    return () => { operation.current.generation += 1; operation.current.controller?.abort(); busyRef.current = ''; };
  }, [api]);
  useEffect(() => {
    resetConfirmation(); setRun(null); setPollError('');
    if (!selectedId) return;
    const controller = new AbortController(); let timer: ReturnType<typeof setTimeout> | undefined;
    const poll = async () => {
      const epoch = readEpoch.current;
      const current = () => !controller.signal.aborted && activeId.current === selectedId && epoch === readEpoch.current && !busyRef.current;
      try {
        const record = await api.get<SkillGeneration>(`${root}/${encodeURIComponent(selectedId)}`, controller.signal);
        if (!current() || record.id !== selectedId) return;
        save(record); setPollError('');
        if (record.run_id) {
          const next = await api.get<Run>(`/runs/${encodeURIComponent(record.run_id)}?${new URLSearchParams({ project_id: record.input.project_id })}`, controller.signal);
          if (!current()) return;
          if (next.id !== record.run_id || next.request.project_id !== record.input.project_id || next.request.purpose !== 'skill_generation') throw new Error('服务返回的生成运行归属不匹配。');
          setRun(next);
        }
      } catch (error) { if (current()) setPollError(textError(error)); }
      finally { if (!controller.signal.aborted) timer = setTimeout(() => { void poll(); }, 2500); }
    };
    void poll();
    return () => { controller.abort(); if (timer) clearTimeout(timer); };
  }, [api, selectedId]);
  useEffect(() => { resetConfirmation(); }, [selected?.id, selected?.revision]);

  const parent = inputs?.parents.find((item) => item.version_id === parentId);
  const taskset = inputs?.tasksets.find((item) => item.taskset_id === tasksetId && item.split === 'tuning');
  const profile = inputs?.models.find((item) => item.id === modelId);
  const subscription = profile?.model.billing_mode === 'subscription';
  const priceKnown = !!profile && (subscription || profile.model.input_price_per_million != null && profile.model.output_price_per_million != null && !!profile.model.price_source);
  const enoughTokens = !!profile && budget.max_tokens >= profile.model.max_input_tokens + profile.model.max_output_tokens;
  const canCreate = !!parent && !!taskset && taskIds.length >= 1 && taskIds.length <= 3 && taskIds.every((id) => taskset.tasks.some((item) => item.id === id))
    && !!inputs?.projects.some((item) => item.id === ownerProject) && !!profile?.credential_available && priceKnown && enoughTokens && validBudget(budget, !!subscription) && !busy && !needsRead;
  const candidate = selected?.candidate;
  const preview = selected?.preview;
  const canPreview = !!selected && ['candidate_ready', 'preview_ready'].includes(selected.state) && !!candidate?.valid && !selected.imported && !needsRead && !busy;
  const canImport = !!selected && selected.state === 'preview_ready' && !!preview?.structural_report.valid && preview.license.status === 'supported' && !selected.imported && !needsRead && !busy && confirmSource && confirmLicense;

  return <div className="generation-view">
    <div className="page-heading"><div><h1>候选生成</h1><p className="muted">从已批准父版本追加指令，保存未验证候选，再明确导入审核。</p></div><button disabled={!!busy} onClick={() => { setShowCreate((value) => !value); resetConfirmation(); }}>{showCreate ? '收起新草稿' : '新建生成草稿'}</button></div>
    <p className="field-help">每份草稿最多启动一个前台 Run。生成不执行父脚本、不建立质量成绩，也不自动创建评测或更改推荐。</p>
    {error && <p className="notice error" role="alert">{error}</p>}
    {needsRead && <p className="notice" role="status">上次写操作尚未核对，请读取已保存记录后再决定后续操作。离开页面不会撤销服务端操作。</p>}
    {showCreate && <form className="generation-draft" onSubmit={(event) => { event.preventDefault(); if (!canCreate) return; void perform('create', async (signal, current) => {
      const record = await api.post<SkillGeneration>(root, { project_id: ownerProject, parent_version_id: parentId, taskset_id: tasksetId, task_ids: taskIds, model_profile_id: modelId, budget: { ...budget, max_cost_usd: subscription ? null : budget.max_cost_usd } }, signal);
      if (current()) show(record);
    }); }}>
      <h2>关闭的生成草稿</h2><p className="small muted">保存只冻结输入，模型调用为 0；启动前仍需核对全文、许可和预算。</p>
      <label htmlFor="generation-project">归属项目</label><select id="generation-project" value={ownerProject} onChange={(event) => setOwnerProject(event.target.value)} required><option value="">选择归属项目</option>{inputs?.projects.map((item) => <option key={item.id} value={item.id}>{item.name || item.id}</option>)}</select><p className="field-help">项目仅用于归属和 Run 访问；生成输入不读取项目代码或 Git 起始提交。</p>
      <label htmlFor="generation-parent">父 Skill 版本</label><select id="generation-parent" value={parentId} onChange={(event) => setParentId(event.target.value)} required><option value="">选择已批准父版本</option>{inputs?.parents.map((item) => <option key={item.version_id} value={item.version_id}>{item.metadata.source} · {item.version_id}</option>)}</select>
      {!inputs?.parents.length && loaded && <p className="warning-text small">暂无满足生成条件的已批准父版本。</p>}
      {parent && <p className="small path-text">固定父内容摘要 <code>{parent.content_digest}</code></p>}
      <label htmlFor="generation-taskset">Tuning 任务集</label><select id="generation-taskset" value={tasksetId} onChange={(event) => { setTasksetId(event.target.value); setTaskIds([]); }} required><option value="">选择 tuning 可见任务</option>{inputs?.tasksets.filter((item) => item.split === 'tuning').map((item) => <option key={item.taskset_id} value={item.taskset_id}>{item.name}</option>)}</select>
      {taskset && <fieldset className="generation-tasks"><legend>选择 1–3 个可见任务</legend>{taskset.tasks.map((task) => <label className="checkbox-label" key={task.id}><input type="checkbox" checked={taskIds.includes(task.id)} disabled={!taskIds.includes(task.id) && taskIds.length >= 3} onChange={(event) => setTaskIds((old) => event.target.checked ? [...old, task.id] : old.filter((id) => id !== task.id))} /><span>{task.title} <code>{task.id}</code></span></label>)}</fieldset>}
      <label htmlFor="generation-model">生成模型配置档</label><select id="generation-model" value={modelId} onChange={(event) => setModelId(event.target.value)} required><option value="">选择模型配置档</option>{inputs?.models.map((item) => <option key={item.id} value={item.id} disabled={!item.credential_available}>{item.name} · {item.model.model_id}{!item.credential_available ? ' · 凭据不可用' : ''}</option>)}</select>
      {profile && <p className="small">{profile.model.provider} · {profile.model.api_mode} · {subscription ? '包月连接，货币费用未知' : '按量计费'} · 单次预留至少 {(profile.model.max_input_tokens + profile.model.max_output_tokens).toLocaleString()} tokens</p>}
      {profile && !priceKnown && <p className="warning-text small">按量模型需要有来源的输入和输出价格。</p>}
      <h3>本次唯一 Run 预算</h3><div className="budget-inputs">
        <label>生成调用上限<input type="number" min={1} max={100} step={1} required value={budget.max_calls} onChange={(event) => setBudget({ ...budget, max_calls: Number(event.target.value) })} /></label>
        <label>生成 Token 上限<input type="number" min={1} max={10000000} step={1} required value={budget.max_tokens} onChange={(event) => setBudget({ ...budget, max_tokens: Number(event.target.value) })} /></label>
        <label>生成费用上限（US$）<input type="number" min={0.0001} step="any" required={!subscription} disabled={subscription} placeholder={subscription ? '包月金额未知' : ''} value={subscription ? '' : budget.max_cost_usd ?? ''} onChange={(event) => setBudget({ ...budget, max_cost_usd: event.target.value === '' ? null : Number(event.target.value) })} /></label>
        <label>生成总时长（秒）<input type="number" min={1} max={7200} step={1} required value={budget.max_seconds} onChange={(event) => setBudget({ ...budget, max_seconds: Number(event.target.value) })} /></label>
      </div>{profile && !enoughTokens && <p className="warning-text small">Token 预算不足以预留一次已选模型的完整输入与输出上限。</p>}
      <p className="field-help">首次启动时钟包含暂停；未知用量保留预留，刷新与恢复不会增加额度。</p><div className="form-footer"><span className="muted small">草稿尚未授权模型用量。</span><button className="primary" type="submit" disabled={!canCreate}>{busy === 'create' ? '正在保存…' : '保存生成草稿'}</button></div>
    </form>}
    <div className="section-heading"><h2>已保存的生成记录</h2><button type="button" disabled={!!busy} onClick={() => { void readRecords(true); }}>读取已保存记录</button></div>
    {!loaded && <p className="muted" role="status">正在读取本机生成记录…</p>}{loaded && !records.length && <p className="muted">尚无生成记录。</p>}
    <div className="generation-layout"><nav className="generation-records" aria-label="候选生成记录">{records.map((record) => <button type="button" className={`profile-item ${record.id === selectedId ? 'selected' : ''}`} key={record.id} aria-current={record.id === selectedId ? 'page' : undefined} onClick={() => select(record.id)}><strong>{states[record.state] ?? record.state}</strong><span>{when(record.created_at)}</span><code>{record.id}</code></button>)}</nav>
      {selected && <section className="generation-detail" aria-label="候选生成详情">
        <h2>{states[selected.state] ?? selected.state}</h2><p className="small path-text">记录 <code>{selected.id}</code> · 修订 <code>{selected.revision}</code><br />更新于 {when(selected.updated_at)}</p>
        {pollError && <p className="notice error" role="alert">{pollError} 当前保留最近一次已读取结果。</p>}
        {selected.error && <p className="notice error" role="alert">生成未完成：<code>{selected.error.code}</code>。原失败与未知预留继续保留。</p>}
        <dl className="config-list"><div><dt>父版本 / 内容摘要</dt><dd><code>{selected.parent.version_id}</code><br /><code>{selected.parent.content_digest}</code></dd></div><div><dt>模型与配置档</dt><dd>{selected.model_profile.name} · {selected.model.model_id}<br />{selected.model.provider} · {selected.model.api_mode} · 配置版本 <code>{selected.model_profile.revision}</code></dd></div><div><dt>冻结预算</dt><dd><BudgetSummary value={selected.budget} />{selected.model.billing_mode === 'subscription' && <p className="small">包月费用未知，仍限制调用、token 和时长。</p>}</dd></div><div><dt>生成策略</dt><dd>保留父文件，只在 SKILL.md 后追加至多 8 KiB 指令。</dd></div></dl>
        <details><summary>完整冻结模型配置与父来源</summary><pre>{JSON.stringify({ model: selected.model, model_profile: selected.model_profile, parent: selected.parent, projection_sha256: selected.projection_sha256, policy: selected.policy }, null, 2)}</pre></details>
        <section className="section-block" aria-label="全部模型可见输入"><h3>全部模型可见输入</h3><p className="small muted">以下全文随草稿冻结，启动后发送给所选供应商。仅包含父指令及所选 tuning 可见任务。</p><pre className="generation-prompt">{selected.prompt}</pre><p className="small path-text">输入 SHA-256 <code>{selected.prompt_sha256}</code></p><details><summary>所选可见任务（{selected.visible_tasks.length}）</summary>{selected.visible_tasks.map((task) => <article key={task.id}><h4>{task.title} · <code>{task.id}</code></h4><pre>{task.prompt}</pre><pre>{task.visible_checks.join('\n') || '无可见检查命令'}</pre></article>)}</details></section>
        <License value={selected.license} />
        {selected.state === 'draft' && <section className="generation-confirm" aria-label="明确启动唯一生成运行"><h3>核对后启动</h3><label className="checkbox-label"><input type="checkbox" checked={confirmUsage} disabled={!!busy || needsRead} onChange={(event) => setConfirmUsage(event.target.checked)} /><span>我已核对完整输入、实际模型及有限预算，授权本次 Run 消耗模型用量。</span></label><label className="checkbox-label"><input type="checkbox" checked={confirmRights} disabled={!!busy || needsRead} onChange={(event) => setConfirmRights(event.target.checked)} /><span>我已核对父内容、许可及归属，确认本次私有衍生用途的授权。</span></label><button className="primary" type="button" disabled={!!busy || needsRead || !confirmUsage || !confirmRights || selected.license.status !== 'supported'} onClick={() => { const record = selected; void perform('start', async (signal, current) => { const next = await api.post<SkillGeneration>(`${root}/${encodeURIComponent(record.id)}/start`, { expected_revision: record.revision, confirm_model_usage: true, confirm_derivative_rights: true }, signal); if (next.id !== record.id) throw new Error('服务返回的生成记录归属不匹配，请读取已保存记录。'); if (current()) show(next); }); }}>启动唯一生成 Run</button></section>}
        {selected.run_id && <section className="section-block" aria-label="关联生成运行"><h3>原始 Run 与用量</h3><p className="path-text"><code>{selected.run_id}</code> · {states[run?.state ?? selected.run_state ?? ''] ?? run?.state ?? selected.run_state ?? '正在读取状态'}</p><button type="button" onClick={() => onOpenRun(selected.input.project_id, selected.run_id!)}>打开原始 Run 与恢复入口</button>{run?.reason && <p className="notice">{run.reason}</p>}{run && <p className="small">累计 {run.usage.calls} 次调用 · {run.usage.tokens.toLocaleString()} tokens · 已记录费用 {formatMoney(run.usage.cost_usd)}；预留 {run.usage.reserved_tokens.toLocaleString()} tokens · 费用预留 {formatMoney(run.usage.reserved_cost_usd)}。{run.usage.unknown_calls > 0 && `${run.usage.unknown_calls} 次调用实际用量未知，预留保留。`}</p>}</section>}
        {candidate && <section className="section-block" aria-label="候选结构结果"><h3>{candidate.valid ? '候选结构检查通过，质量尚未验证' : '候选结构检查未通过'}</h3><p className="small">结构只核对追加、原文件、目录、模式和许可保留。通过不代表能力提高、兼容、安全或获得新授权。</p>{candidate.errors.length > 0 && <p className="notice error">{candidate.errors.join('、')}</p>}<p className="small path-text">结构报告 <code>{candidate.report_sha256}</code></p><div className="button-row"><button type="button" disabled={!!busy} onClick={() => { void perform('download', (signal) => api.downloadGeneration(selected.id, 'output', signal)); }}>下载原始输出</button><button type="button" disabled={!!busy} onClick={() => { void perform('download', (signal) => api.downloadGeneration(selected.id, 'report', signal)); }}>下载结构报告</button>{candidate.valid && candidate.archive_sha256 && <button type="button" disabled={!!busy} onClick={() => { void perform('download', (signal) => api.downloadGeneration(selected.id, 'candidate', signal)); }}>下载完整候选</button>}</div><button type="button" disabled={!canPreview} onClick={() => { const record = selected; void perform('preview', async (signal, current) => { const next = await api.post<SkillGeneration>(`${root}/${encodeURIComponent(record.id)}/preview`, { expected_revision: record.revision }, signal); if (next.id !== record.id) throw new Error('服务返回的生成记录归属不匹配，请读取已保存记录。'); if (current()) show(next); }); }}>保存固定候选预览</button></section>}
        {preview && <section className="generation-preview" aria-label="固定生成候选预览"><h3>固定候选预览</h3><p className="small path-text">预览 SHA-256 <code>{preview.sha256}</code></p><details open><summary>追加差异</summary><pre className="diff-output">{preview.diff}</pre></details><details><summary>候选完整指令原文</summary><pre>{preview.instructions}</pre></details><SkillMetadataReport metadata={preview.metadata} /><License value={preview.license} /><details><summary>结构报告、来源与交付绑定</summary><pre>{JSON.stringify({ structural_report: preview.structural_report, provenance: preview.provenance, evidence: preview.evidence }, null, 2)}</pre></details>
          {!selected.imported && <section className="generation-confirm"><label className="checkbox-label"><input type="checkbox" checked={confirmSource} disabled={!!busy || needsRead || !preview.structural_report.valid} onChange={(event) => setConfirmSource(event.target.checked)} /><span>我已核对父版本、生成来源、追加差异与完整候选。</span></label><label className="checkbox-label"><input type="checkbox" checked={confirmLicense} disabled={!!busy || needsRead || preview.license.status !== 'supported'} onChange={(event) => setConfirmLicense(event.target.checked)} /><span>我已阅读保留的许可、归属及新增文本说明，确认本次导入。</span></label><button className="primary" type="button" disabled={!canImport} onClick={() => { const record = selected; void perform('import', async (signal, current) => { const result = await api.post<{ generation: SkillGeneration; skill: SkillVersion }>(`${root}/${encodeURIComponent(record.id)}/import`, { expected_revision: record.revision, preview_sha256: preview.sha256, confirm_source: true, confirm_license: true }, signal); if (result.generation.id !== record.id || result.generation.imported?.version_id !== result.skill.version_id) throw new Error('服务返回的生成记录归属不匹配，请读取已保存记录。'); if (current()) { show(result.generation); uncertain(false); onImported(result.skill); } }); }}>导入为待审核版本</button><p className="field-help">导入不继承父版本成绩，不自动批准、回归或推广。请继续原有人工审核。</p></section>}
        </section>}
        {selected.imported && <section className="notice"><p className="path-text">已关联 Library 版本 <code>{selected.imported.version_id}</code>，审核状态以版本记录为准。</p><button type="button" disabled={!!busy} onClick={() => { void perform('open', async (signal, current) => { const skill = await api.get<SkillVersion>(`/skills/${encodeURIComponent(selected.imported!.version_id)}`, signal); if (current() && skill.version_id === selected.imported!.version_id) onImported(skill); }); }}>打开版本并审核</button></section>}
      </section>}
    </div>
  </div>;
}
