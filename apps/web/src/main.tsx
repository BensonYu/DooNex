import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { Api, type Budget, type Project, type Recovery, type Run, type RunEvent, type Settings } from './api';
import './style.css';

const stateNames: Record<string, string> = {
  queued: '等待执行', running: '正在执行', pausing: '正在暂停', paused: '已暂停',
  verifying: '正在验证', completed_verified: '已验证完成', completed_unverified: '待检查',
  failed: '失败', cancelled: '已取消', interrupted: '意外中断', budget_exceeded: '预算已用尽',
  awaiting_input: '等待输入', awaiting_approval: '等待授权', cancelling: '正在取消',
};
const activeStates = new Set(['queued', 'running', 'pausing', 'verifying', 'cancelling']);
const fmtTime = (value: string) => {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', { hour12: false });
};
const money = (value: number | null | undefined) => value == null || !Number.isFinite(value) ? '未知' : `US$${value.toFixed(4)}`;
const json = (value: unknown) => JSON.stringify(value, null, 2) ?? '';
const message = (error: unknown) => error instanceof Error ? error.message : String(error);

function readStorage(storage: Storage, key: string): string {
  try { return storage.getItem(key) ?? ''; } catch { return ''; }
}
function writeStorage(storage: Storage, key: string, value: string) {
  try { value ? storage.setItem(key, value) : storage.removeItem(key); } catch { /* The interface still works without browser storage. */ }
}

function Status({ state }: { state: string }) {
  return <span className={`status status-${state}`}>{stateNames[state] ?? state}</span>;
}

function CheckReport({ evidence }: { evidence: Record<string, unknown> }) {
  const reportValue = evidence.report ?? evidence.evidence ?? evidence;
  const report = reportValue && typeof reportValue === 'object' ? reportValue as Record<string, unknown> : {};
  const checks = Array.isArray(report.checks) ? report.checks : [];
  return <>
    {checks.length > 0 && <div className="check-results">{checks.map((value: unknown, index: number) => {
      const check = value && typeof value === 'object' ? value as Record<string, unknown> : {};
      const nested = check.result && typeof check.result === 'object' ? check.result as Record<string, unknown> : check;
      const exitCode = nested.exit_code;
      const result = nested.timed_out ? '超时' : nested.cancelled ? '已取消' : typeof exitCode === 'number' ? `退出码 ${exitCode}` : '未取得退出码';
      return <article className="check-result" key={index}>
        <div className="check-heading"><code>{typeof check.command === 'string' ? check.command : `检查 ${index + 1}`}</code><span className={exitCode === 0 && !nested.timed_out && !nested.cancelled ? 'positive' : 'attention'}>{result}</span></div>
        {typeof nested.started_at === 'string' && <p className="muted small">开始 {fmtTime(nested.started_at)}{typeof nested.ended_at === 'string' ? ` · 结束 ${fmtTime(nested.ended_at)}` : ''}</p>}
        <details><summary>检查日志与原始记录</summary><pre>{json(value)}</pre></details>
      </article>;
    })}</div>}
    <details className="raw-evidence" open={!checks.length}><summary>完整证据报告</summary><pre className="evidence-output">{json(evidence)}</pre></details>
  </>;
}

function App() {
  const [token, setToken] = useState(() => readStorage(sessionStorage, 'doonex.token'));
  const [tokenDraft, setTokenDraft] = useState('');
  const [settings, setSettings] = useState<Settings | null>(null);
  const [project, setProject] = useState<Project | null>(null);
  const [runs, setRuns] = useState<Run[]>([]);
  const [selectedId, setSelectedId] = useState(() => readStorage(localStorage, 'doonex.selectedRun'));
  const [run, setRun] = useState<Run | null>(null);
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [error, setError] = useState('');
  const [pollError, setPollError] = useState('');
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(false);
  const [showCreate, setShowCreate] = useState(true);
  const [tab, setTab] = useState<'events' | 'diff' | 'evidence'>('events');
  const [diff, setDiff] = useState<string | null>(null);
  const [evidence, setEvidence] = useState<Record<string, unknown> | null>(null);
  const [detailError, setDetailError] = useState('');
  const [detailLoading, setDetailLoading] = useState(false);
  const [recovery, setRecovery] = useState<Recovery | null>(null);
  const [acknowledge, setAcknowledge] = useState(false);
  const [adoption, setAdoption] = useState<{ safe: boolean; reason: string } | null>(null);
  const [projectPath, setProjectPath] = useState('');
  const [baseRef, setBaseRef] = useState('HEAD');
  const [prompt, setPrompt] = useState('');
  const [checks, setChecks] = useState('');
  const [budget, setBudget] = useState<Budget | null>(null);
  const cursor = useRef(0);
  const api = useMemo(() => new Api(token), [token]);

  const select = useCallback((id: string) => {
    setSelectedId(id);
    writeStorage(localStorage, 'doonex.selectedRun', id);
    setTab('events');
    setShowCreate(false);
  }, []);

  const refresh = useCallback(async (signal?: AbortSignal) => {
    const [nextSettings, nextProject, nextRuns] = await Promise.all([
      api.get<Settings>('/settings', signal), api.get<Project | null>('/project', signal), api.get<Run[]>('/runs', signal),
    ]);
    setSettings(nextSettings);
    setProject(nextProject);
    setRuns(nextRuns);
    setBudget((old) => old ?? nextSettings.default_budget);
    setProjectPath((old) => old || nextProject?.path || '');
    return nextRuns;
  }, [api]);

  useEffect(() => {
    if (!token) return;
    const controller = new AbortController();
    setLoading(true);
    setError('');
    void refresh(controller.signal).then((nextRuns) => {
      if (controller.signal.aborted) return;
      const existing = readStorage(localStorage, 'doonex.selectedRun');
      if (existing && nextRuns.some((item) => item.id === existing)) select(existing);
      else if (nextRuns[0]) select(nextRuns[0].id);
      else { setSelectedId(''); setRun(null); setShowCreate(true); }
    }).catch((err: unknown) => {
      if (!controller.signal.aborted) setError(message(err));
    }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [token, refresh, select]);

  useEffect(() => {
    setRun(null);
    setEvents([]);
    setDiff(null);
    setEvidence(null);
    setRecovery(null);
    setAcknowledge(false);
    setAdoption(null);
    setPollError('');
    setDetailError('');
    cursor.current = 0;
    if (!token || !selectedId) return;
    const controller = new AbortController();
    let timeout: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const [nextRun, nextEvents, nextRuns] = await Promise.all([
          api.get<Run>(`/runs/${encodeURIComponent(selectedId)}`, controller.signal),
          api.get<{ events: RunEvent[]; cursor: number }>(`/runs/${encodeURIComponent(selectedId)}/events?after=${cursor.current}`, controller.signal),
          api.get<Run[]>('/runs', controller.signal),
        ]);
        if (controller.signal.aborted) return;
        setRun(nextRun);
        setRuns(nextRuns);
        setEvents((old) => {
          const seen = new Set(old.map((item) => item.id));
          return [...old, ...nextEvents.events.filter((item) => !seen.has(item.id))];
        });
        cursor.current = nextEvents.cursor;
        setPollError('');
      } catch (err) {
        if (!controller.signal.aborted) setPollError(message(err));
      } finally {
        if (!controller.signal.aborted) timeout = setTimeout(poll, 2000);
      }
    };
    void poll();
    return () => { controller.abort(); clearTimeout(timeout); };
  }, [api, selectedId, token]);

  useEffect(() => {
    setDetailError('');
    if (!run || tab === 'events') return;
    const controller = new AbortController();
    setDetailLoading(true);
    const path = `/runs/${encodeURIComponent(run.id)}/${tab}`;
    const request = tab === 'diff'
      ? api.get<{ diff: string }>(path, controller.signal).then((result) => setDiff(result.diff))
      : api.get<Record<string, unknown>>(path, controller.signal).then(setEvidence);
    void request.catch((err: unknown) => { if (!controller.signal.aborted) setDetailError(message(err)); })
      .finally(() => { if (!controller.signal.aborted) setDetailLoading(false); });
    return () => controller.abort();
  }, [api, run?.id, run?.state, run?.updated_at, tab]);

  const perform = async (operation: () => Promise<void>) => {
    setBusy(true);
    setError('');
    try { await operation(); } catch (err) { setError(message(err)); } finally { setBusy(false); }
  };

  const control = (action: 'pause' | 'cancel' | 'resume') => {
    if (!run) return;
    void perform(async () => {
      let nextRun: Run;
      try {
        nextRun = await api.post<Run>(`/runs/${encodeURIComponent(run.id)}/${action}`, action === 'resume' ? { acknowledge_unknown: acknowledge } : {});
      } catch (err) {
        if (action === 'resume') {
          try { setRecovery(await api.get<Recovery>(`/runs/${encodeURIComponent(run.id)}/recovery`)); } catch { /* Keep the original actionable error. */ }
        }
        throw err;
      }
      setRun(nextRun);
      setRecovery(null);
      setAcknowledge(false);
      await refresh();
    });
  };

  if (!token) {
    return <main className="connect-page">
      <div className="connect-panel">
        <div className="wordmark">DooNex<span>本地编程工作台</span></div>
        <h1>连接你的工作台</h1>
        <p className="muted">输入本地服务生成的访问令牌，查看任务与运行证据。</p>
        <form onSubmit={(event) => {
          event.preventDefault();
          const value = tokenDraft.trim();
          if (!value) return;
          writeStorage(sessionStorage, 'doonex.token', value);
          setToken(value);
          setTokenDraft('');
        }}>
          <label htmlFor="access-token">访问令牌</label>
          <input id="access-token" type="password" autoComplete="off" value={tokenDraft} onChange={(event) => setTokenDraft(event.target.value)} required autoFocus />
          <p className="field-help">令牌仅保存在当前浏览器会话中。这里无需输入模型 API 密钥。</p>
          <button className="primary" type="submit">连接工作台</button>
        </form>
      </div>
    </main>;
  }

  const hasModel = !!settings?.model && settings.credential_available;
  const hasPrice = settings?.model?.input_price_per_million != null && settings?.model?.output_price_per_million != null;
  const canCreate = !!project && hasModel && hasPrice && !!budget && !busy;
  const selected = run;
  const isActive = !!selected && activeStates.has(selected.state);

  return <div className="app-shell">
    <header className="app-header">
      <div className="wordmark">DooNex<span>编程工作台</span></div>
      <div className="header-actions"><span className="local-label">本地 · 单 Agent</span><button className="quiet" onClick={() => {
        writeStorage(sessionStorage, 'doonex.token', ''); setToken(''); setSettings(null); setProject(null); setRuns([]); setRun(null);
      }}>断开连接</button></div>
    </header>
    <div className="workspace-layout">
      <aside className="sidebar" aria-label="运行列表">
        <div className="sidebar-title"><h2>任务记录</h2><button className="primary compact" onClick={() => { setShowCreate(true); setError(''); }}>新任务</button></div>
        {loading && <p className="muted" role="status">正在连接服务…</p>}
        {!loading && !runs.length && <p className="empty-copy">还没有运行记录。注册项目后，开始第一个任务。</p>}
        <ul className="run-list">{runs.map((item) => <li key={item.id}>
          <button className={`run-item ${item.id === selectedId && !showCreate ? 'selected' : ''}`} aria-current={item.id === selectedId && !showCreate ? 'page' : undefined} onClick={() => select(item.id)}>
            <span className="run-title">{item.request.prompt}</span>
            <Status state={item.state} />
            <time dateTime={item.created_at}>{fmtTime(item.created_at)}</time>
          </button>
        </li>)}</ul>
      </aside>
      <main className="main-content">
        {error && <div className="notice error" role="alert"><span>{error}</span><button className="quiet" aria-label="关闭错误提示" onClick={() => setError('')}>关闭</button></div>}
        <section className="project-strip" aria-label="当前项目">
          <div><span className="eyebrow">当前项目</span><strong className="path-text">{project?.path ?? '尚未注册'}</strong></div>
          {project && <span className="muted small">{project.dirty ? '原工作区有未提交改动' : '原工作区干净'}</span>}
        </section>

        {showCreate ? <div className="create-view">
          <div className="page-heading"><div><h1>开始一个可信的任务</h1><p className="muted">固定起始版本与验收条件，在隔离工作区中执行。</p></div>{selectedId && <button className="quiet" onClick={() => setShowCreate(false)}>返回运行</button>}</div>
          <section className="section-block">
            <h2>项目</h2>
            <form className="inline-form" onSubmit={(event) => {
              event.preventDefault();
              void perform(async () => {
                const result = await api.post<Project>('/project', { path: projectPath.trim() });
                setProject(result); setProjectPath(result.path); setBaseRef(result.head);
              });
            }}>
              <div className="grow"><label htmlFor="project-path">本地 Git 仓库路径</label><input id="project-path" value={projectPath} onChange={(event) => setProjectPath(event.target.value)} placeholder="/绝对路径/项目" required spellCheck={false} /></div>
              <button disabled={busy} type="submit">{project ? '重新校验项目' : '注册项目'}</button>
            </form>
            <p className="field-help">只读取选定提交建立独立工作副本，原工作区中的未提交内容不会自动带入。</p>
          </section>
          <section className="section-block model-section">
            <div className="section-heading"><h2>模型连接</h2><span className={`text-state ${hasModel && hasPrice ? 'positive' : 'attention'}`}>{hasModel ? hasPrice ? '凭据已就绪' : '价格信息缺失' : '待配置'}</span></div>
            {settings?.model ? <>
              <p className="model-name">{settings.model.model_id}<span>{settings.model.provider} · {settings.model.api_mode}</span></p>
              <details><summary>连接与能力配置</summary><dl className="config-list">
                <div><dt>凭据引用</dt><dd>{settings.model.credential_env} · {settings.credential_available ? '已提供' : '未提供'}</dd></div>
                <div><dt>输入 / 输出上限</dt><dd>{settings.model.max_input_tokens.toLocaleString()} / {settings.model.max_output_tokens.toLocaleString()} tokens</dd></div>
                <div><dt>请求超时</dt><dd>{settings.model.timeout_seconds} 秒</dd></div>
                <div><dt>每百万 tokens 价格</dt><dd>输入 {money(settings.model.input_price_per_million)}；输出 {money(settings.model.output_price_per_million)}</dd></div>
                <div><dt>价格来源</dt><dd>{settings.model.price_source || '未知'}</dd></div>
                {settings.model.base_url && <div><dt>API 地址</dt><dd className="path-text">{settings.model.base_url}</dd></div>}
              </dl></details>
            </> : <p className="muted">请在本地服务中配置供应商、实际模型 ID、凭据引用和价格。浏览器不接收模型密钥。</p>}
            {settings && !settings.credential_available && <p className="field-help warning-text">服务未检测到可用凭据，任务暂时不能启动。</p>}
            {settings?.model && !hasPrice && <p className="field-help warning-text">未知价格不会按免费处理。补齐价格后才能使用费用预算。</p>}
          </section>
          <form onSubmit={(event) => {
            event.preventDefault();
            if (!canCreate || !budget) return;
            void perform(async () => {
              const created = await api.post<Run>('/runs', { prompt: prompt.trim(), base_ref: baseRef.trim(), checks: checks.split('\n').map((line) => line.trim()).filter(Boolean), budget });
              setRuns((old) => [created, ...old.filter((item) => item.id !== created.id)]);
              select(created.id);
              setPrompt(''); setChecks('');
            });
          }}>
            <section className="section-block">
              <h2>需求与验收</h2>
              <label htmlFor="base-ref">起始提交或引用</label><input className="mono" id="base-ref" value={baseRef} onChange={(event) => setBaseRef(event.target.value)} required spellCheck={false} />
              <p className="field-help">启动前解析为固定提交。可输入 HEAD、分支名或提交 SHA。</p>
              <label htmlFor="task-prompt">希望完成的工作</label><textarea id="task-prompt" rows={4} value={prompt} onChange={(event) => setPrompt(event.target.value)} placeholder="描述要修改的行为、范围和需要保留的约束。" required />
              <label htmlFor="checks">验收命令</label><textarea className="mono" id="checks" rows={3} value={checks} onChange={(event) => setChecks(event.target.value)} placeholder="每行一条命令，例如 python -m unittest discover" spellCheck={false} />
              <p className="field-help">命令在隔离环境中按启动时版本保存并执行。未填写检查时，结果只能标记为待检查。</p>
            </section>
            <section className="section-block">
              <h2>本次运行预算</h2>
              {budget ? <div className="budget-inputs">
                <label>模型调用上限<input type="number" min={1} step={1} required value={budget.max_calls} onChange={(event) => setBudget({ ...budget, max_calls: Number(event.target.value) })} /></label>
                <label>Token 上限<input type="number" min={1} step={1} required value={budget.max_tokens} onChange={(event) => setBudget({ ...budget, max_tokens: Number(event.target.value) })} /></label>
                <label>估计费用上限（US$）<input type="number" min={0.0001} step="any" required value={budget.max_cost_usd} onChange={(event) => setBudget({ ...budget, max_cost_usd: Number(event.target.value) })} /></label>
                <label>总时长上限（秒）<input type="number" min={1} step={1} required value={budget.max_seconds} onChange={(event) => setBudget({ ...budget, max_seconds: Number(event.target.value) })} /></label>
              </div> : <p className="muted">正在读取预算配置…</p>}
              <p className="field-help">预算限制后续请求的派发。在途请求仍可能产生费用，预估上限不等于供应商账单的绝对封顶。</p>
            </section>
            <div className="form-footer"><span className="muted small">需求、验收和预算将随本次运行保存。</span><button className="primary" type="submit" disabled={!canCreate}>{busy ? '正在创建…' : '创建并开始运行'}</button></div>
          </form>
        </div> : selected ? <div className="run-view">
          <div className="page-heading"><div><span className="eyebrow">运行 {selected.id}</span><h1 className="task-heading">{selected.request.prompt}</h1></div><Status state={selected.state} /></div>
          <div className="run-meta"><span>起始提交 <code>{selected.request.base_commit}</code></span><span>创建于 {fmtTime(selected.created_at)}</span></div>
          {selected.reason && <p className="notice">{selected.reason}</p>}
          {pollError && <p className="notice error" role="alert">{pollError} 当前显示最近一次已记录结果；会自动重连。</p>}
          <div className="run-toolbar">
            <div className="button-row">
              {(selected.state === 'running' || selected.state === 'queued') && <button disabled={busy} onClick={() => control('pause')}>暂停</button>}
              {(isActive || selected.state === 'paused' || selected.state === 'interrupted') && <button className="danger" disabled={busy || selected.state === 'cancelling'} onClick={() => control('cancel')}>取消运行</button>}
              {selected.state === 'paused' && <button className="primary" disabled={busy} onClick={() => control('resume')}>继续执行</button>}
              {selected.state === 'interrupted' && <button disabled={busy} onClick={() => { void perform(async () => {
                setRecovery(await api.get<Recovery>(`/runs/${encodeURIComponent(selected.id)}/recovery`)); setAcknowledge(false);
              }); }}>核查恢复条件</button>}
            </div>
            <span className="muted small">{isActive ? '每 2 秒同步，关闭页面不停止任务' : `最近更新 ${fmtTime(selected.updated_at)}`}</span>
          </div>
          {(selected.state === 'pausing' || selected.state === 'cancelling') && <p className="notice">停止请求已记录。正在等待在途动作结束或超时，尚未确认全部动作终止。</p>}
          {recovery && <section className="recovery-panel">
            <h2>恢复前核查</h2>
            <p>工作区：{recovery.workspace_ok ? '可用' : '需要处理'}；容器记录：{recovery.containers.length}；待核对动作：{recovery.pending_actions.length}</p>
            {recovery.notes.length > 0 && <ul>{recovery.notes.map((note, index) => <li key={index}>{note}</li>)}</ul>}
            <details><summary>查看容器与动作记录</summary><pre>{json({ containers: recovery.containers, pending_actions: recovery.pending_actions })}</pre></details>
            <label className="checkbox-label"><input type="checkbox" checked={acknowledge} onChange={(event) => setAcknowledge(event.target.checked)} />我已核对工作区、容器和结果未知的外部动作，确认可以继续。</label>
            <button className="primary" disabled={busy || !acknowledge || !recovery.workspace_ok} onClick={() => control('resume')}>确认核查并恢复</button>
          </section>}
          <section className="usage-panel" aria-label="预算与用量">
            <div><span>模型调用</span><strong>{selected.usage.calls ?? 0}<small> / {selected.request.budget.max_calls}</small></strong></div>
            <div><span>已记录 tokens</span><strong>{(selected.usage.tokens ?? 0).toLocaleString()}<small> / {selected.request.budget.max_tokens.toLocaleString()}</small></strong></div>
            <div><span>已记录估计费用</span><strong>{money(selected.usage.cost_usd)}<small> / {money(selected.request.budget.max_cost_usd)}</small></strong></div>
            <div><span>总时长上限</span><strong>{selected.request.budget.max_seconds}<small> 秒</small></strong></div>
            <p>预留：{(selected.usage.reserved_tokens ?? 0).toLocaleString()} tokens，{money(selected.usage.reserved_cost_usd)}。{selected.usage.unknown_calls > 0 ? `有 ${selected.usage.unknown_calls} 次调用的实际用量未知，保留预算预留。` : '预留会在供应商返回用量后结算。'}</p>
          </section>
          <div className="tabs" role="tablist" aria-label="运行内容">{([['events', '执行过程'], ['diff', '代码变更'], ['evidence', '验证与交付']] as const).map(([value, title], index) => <button key={value} id={`tab-${value}`} role="tab" tabIndex={tab === value ? 0 : -1} aria-selected={tab === value} aria-controls={`panel-${value}`} onClick={() => setTab(value)} onKeyDown={(event) => {
            const keys = ['events', 'diff', 'evidence'] as const;
            const nextIndex = event.key === 'ArrowRight' ? (index + 1) % 3 : event.key === 'ArrowLeft' ? (index + 2) % 3 : event.key === 'Home' ? 0 : event.key === 'End' ? 2 : -1;
            if (nextIndex < 0) return;
            event.preventDefault(); setTab(keys[nextIndex]);
            (event.currentTarget.parentElement?.children[nextIndex] as HTMLButtonElement | undefined)?.focus();
          }}>{title}</button>)}</div>
          <section className="detail-panel" role="tabpanel" id={`panel-${tab}`} aria-labelledby={`tab-${tab}`}>
            {detailError && <p className="notice error" role="alert">{detailError}</p>}
            {tab === 'events' && <>
              <details className="requirements"><summary>本次需求与验收条件</summary><p className="preserve-text">{selected.request.prompt}</p><h3>固定验收命令</h3>{selected.request.checks.length ? <pre>{selected.request.checks.join('\n')}</pre> : <p className="muted">没有设置自动验收命令，产物需要人工检查。</p>}<p className="muted small">项目：{selected.request.repo_path}</p><p className="muted small">模型：{selected.request.model.model_id}</p></details>
              {!events.length && <p className="empty-copy">尚无已记录事件。运行启动后会在这里显示进展。</p>}
              <ol className="event-list">{events.map((event) => <li key={event.id}><div className="event-heading"><strong>{event.kind}</strong><time dateTime={event.created_at}>{fmtTime(event.created_at)}</time><span>#{event.id}</span></div><pre>{typeof event.payload === 'string' ? event.payload : json(event.payload)}</pre></li>)}</ol>
            </>}
            {tab === 'diff' && <>{detailLoading && diff === null ? <p role="status" className="muted">正在读取代码变更…</p> : diff ? <pre className="diff-output">{diff}</pre> : !detailError && <p className="empty-copy">目前没有可显示的代码差异。</p>}</>}
            {tab === 'evidence' && <>
              <div className="evidence-intro"><h2>与代码快照绑定的交付证据</h2><p className="muted">检查命令、退出码、日志和时间以服务保存的报告为准。失败、缺失或过期证据不能证明当前代码已通过验收。</p></div>
              <div className="button-row artifact-actions">{(['patch', 'snapshot', 'report'] as const).map((name) => <button key={name} disabled={busy} onClick={() => { void perform(() => api.download(selected.id, name)); }}>下载{({ patch: '补丁', snapshot: '代码快照', report: '检查报告' })[name]}</button>)}<button disabled={busy || isActive} onClick={() => { void perform(async () => setAdoption(await api.post<{ safe: boolean; reason: string }>(`/runs/${encodeURIComponent(selected.id)}/adoption-check`))); }}>检查采纳条件</button></div>
              {adoption && <div className={`notice ${adoption.safe ? '' : 'error'}`} role="status"><strong>{adoption.safe ? '采纳预检通过' : '采纳预检未通过'}</strong><p>{adoption.reason}</p><p>请保留补丁并人工检查后整合。预检不会修改原项目，也不会推送或部署。</p></div>}
              {detailLoading && evidence === null ? <p className="muted" role="status">正在读取证据…</p> : evidence ? <CheckReport evidence={evidence} /> : !detailError && <p className="empty-copy">尚无验证报告。</p>}
            </>}
          </section>
        </div> : <div className="empty-state"><h1>{selectedId ? '正在读取运行…' : '准备开始'}</h1><p className="muted">{selectedId ? '从已保存的记录恢复视图。' : '注册一个项目，创建第一个任务。'}</p><button onClick={() => setShowCreate(true)}>创建任务</button></div>}
      </main>
    </div>
  </div>;
}

createRoot(document.getElementById('root')!).render(<React.StrictMode><App /></React.StrictMode>);
