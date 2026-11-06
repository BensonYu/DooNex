import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { Api, type ArtifactName, type Budget, type Diff, type ModelProfiles, type Project, type Recovery, type Run, type RunEvent, type Settings, type Workflow } from './api';
import './style.css';
import { ReviewReport, WorkflowProgress } from './workflow';
import { FrozenModels, ModelSettings, RoleModelChoices } from './model-settings';

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

function readStorage(storage: 'local' | 'session', key: string): string {
  try { return (storage === 'local' ? localStorage : sessionStorage).getItem(key) ?? ''; } catch { return ''; }
}
function writeStorage(storage: 'local' | 'session', key: string, value: string) {
  try {
    const target = storage === 'local' ? localStorage : sessionStorage;
    value ? target.setItem(key, value) : target.removeItem(key);
  } catch { /* The interface still works without browser storage. */ }
}
const selectionKey = (projectId: string) => `doonex.selectedRun.${projectId}`;
const runPath = (projectId: string, id: string, suffix = '', query: Record<string, string> = {}) =>
  `/runs/${encodeURIComponent(id)}${suffix ? `/${suffix}` : ''}?${new URLSearchParams({ ...query, project_id: projectId })}`;
type TaskDraft = { baseRef: string; prompt: string; checks: string; workflow: Workflow; executorProfileId?: string; reviewerProfileId?: string; budget?: Budget };
const emptyDraft: TaskDraft = { baseRef: 'HEAD', prompt: '', checks: '', workflow: 'execute_review' };
type ActionContext = { signal: AbortSignal; isCurrent: () => boolean };
function readDrafts(): Record<string, TaskDraft> {
  try {
    const value: unknown = JSON.parse(readStorage('local', 'doonex.projectDrafts') || '{}');
    if (!value || typeof value !== 'object' || Array.isArray(value)) return {};
    return Object.fromEntries(Object.entries(value).filter((entry): entry is [string, TaskDraft] => {
      const draft = entry[1];
      if (!draft || typeof draft !== 'object') return false;
      const item = draft as Record<string, unknown>;
      if (typeof item.baseRef !== 'string' || typeof item.prompt !== 'string' || typeof item.checks !== 'string' || !['execute', 'execute_review'].includes(String(item.workflow))) return false;
      if (['executorProfileId', 'reviewerProfileId'].some((key) => item[key] !== undefined && typeof item[key] !== 'string')) return false;
      if (item.budget == null) return true;
      const budget = item.budget as Record<string, unknown>;
      return ['max_calls', 'max_tokens', 'max_seconds'].every((key) => typeof budget[key] === 'number' && Number.isFinite(budget[key]))
        && (budget.max_cost_usd === null || typeof budget.max_cost_usd === 'number');
    }));
  } catch { return {}; }
}

function Status({ state }: { state: string }) {
  return <span className={`status status-${state}`}>{stateNames[state] ?? state}</span>;
}

function CheckReport({ evidence, busy, onDownloadLog }: { evidence: Record<string, unknown>; busy: boolean; onDownloadLog: (name: ArtifactName) => void }) {
  const reportValue = evidence.report ?? evidence.evidence ?? evidence;
  const report = reportValue && typeof reportValue === 'object' ? reportValue as Record<string, unknown> : {};
  const checks = Array.isArray(report.checks) ? report.checks : [];
  return <>
    {checks.length > 0 && <div className="check-results">{checks.map((value: unknown, index: number) => {
      const check = value && typeof value === 'object' ? value as Record<string, unknown> : {};
      const nested = check.result && typeof check.result === 'object' ? check.result as Record<string, unknown> : check;
      const exitCode = nested.exit_code;
      const result = nested.timed_out ? '超时' : nested.cancelled ? '已取消' : typeof exitCode === 'number' ? `退出码 ${exitCode}` : '未取得退出码';
      const logName = typeof check.log === 'string' && /^check-[1-9][0-9]*\.log$/.test(check.log) ? check.log as ArtifactName : null;
      const { output, ...metadata } = nested;
      return <article className="check-result" key={index}>
        <div className="check-heading"><code>{typeof check.command === 'string' ? check.command : `检查 ${index + 1}`}</code><span className={exitCode === 0 && !nested.timed_out && !nested.cancelled ? 'positive' : 'attention'}>{result}</span></div>
        {typeof nested.started_at === 'string' && <p className="muted small">开始 {fmtTime(nested.started_at)}{typeof nested.ended_at === 'string' ? ` · 结束 ${fmtTime(nested.ended_at)}` : ''}</p>}
        <details><summary>检查日志与原始记录</summary>
          {typeof output === 'string' && <pre>{output}</pre>}
          {nested.output_truncated === true && <p className="field-help">显示内容已截断，可下载完整检查日志。</p>}
          <pre>{json(metadata)}</pre>
        </details>
        {logName && <div className="button-row artifact-actions"><button disabled={busy} aria-label={`下载第 ${index + 1} 条检查日志`} onClick={() => onDownloadLog(logName)}>下载检查日志</button></div>}
      </article>;
    })}</div>}
    <details className="raw-evidence" open={!checks.length}><summary>完整证据报告</summary><pre className="evidence-output">{json(evidence)}</pre></details>
  </>;
}

function App() {
  const [token, setToken] = useState(() => readStorage('session', 'doonex.token'));
  const [tokenDraft, setTokenDraft] = useState('');
  const [settings, setSettings] = useState<Settings | null>(null);
  const [registry, setRegistry] = useState<ModelProfiles | null>(null);
  const [view, setView] = useState<'workbench' | 'models'>('workbench');
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectId] = useState(() => readStorage('local', 'doonex.selectedProject'));
  const [runs, setRuns] = useState<Run[]>([]);
  const [selectedId, setSelectedId] = useState(() => readStorage('local', selectionKey(readStorage('local', 'doonex.selectedProject'))));
  const [run, setRun] = useState<Run | null>(null);
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [error, setError] = useState('');
  const [pollError, setPollError] = useState('');
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(false);
  const [showCreate, setShowCreate] = useState(true);
  const [tab, setTab] = useState<'events' | 'diff' | 'evidence'>('events');
  const [diff, setDiff] = useState<Diff | null>(null);
  const [evidence, setEvidence] = useState<Record<string, unknown> | null>(null);
  const [detailError, setDetailError] = useState('');
  const [detailLoading, setDetailLoading] = useState(false);
  const [recovery, setRecovery] = useState<Recovery | null>(null);
  const [acknowledge, setAcknowledge] = useState(false);
  const [adoption, setAdoption] = useState<{ safe: boolean; reason: string } | null>(null);
  const [projectPath, setProjectPath] = useState('');
  const [drafts, setDrafts] = useState<Record<string, TaskDraft>>(readDrafts);
  const cursor = useRef(0);
  const active = useRef({ projectId, runId: selectedId });
  const action = useRef<{ generation: number; controller: AbortController | null }>({ generation: 0, controller: null });
  const api = useMemo(() => new Api(token), [token]);
  const currentApi = useRef(api);
  currentApi.current = api;
  const project = projects.find((item) => item.id === projectId) ?? null;
  const draft = drafts[projectId] ?? emptyDraft;
  const { baseRef, prompt, checks, workflow } = draft;
  const executorProfileId = draft.executorProfileId ?? registry?.default_profile_id ?? registry?.profiles[0]?.id ?? '';
  const reviewerProfileId = draft.reviewerProfileId ?? executorProfileId;
  const executorProfile = registry?.profiles.find((profile) => profile.id === executorProfileId);
  const reviewerProfile = registry?.profiles.find((profile) => profile.id === reviewerProfileId);
  const executorModel = executorProfile?.model ?? (!executorProfileId ? settings?.model : null);
  const reviewerModel = reviewerProfile?.model ?? (!reviewerProfileId ? settings?.model : null);
  const models = workflow === 'execute_review' ? [executorModel, reviewerModel] : [executorModel];
  const budget = draft.budget ?? settings?.default_budget ?? null;

  const updateDraft = (update: Partial<TaskDraft>, targetProject = projectId) => {
    if (!targetProject) return;
    setDrafts((old) => {
      const next = { ...old, [targetProject]: { ...(old[targetProject] ?? emptyDraft), ...update } };
      writeStorage('local', 'doonex.projectDrafts', JSON.stringify(next));
      return next;
    });
  };

  const clearView = useCallback(() => {
    action.current.generation += 1;
    action.current.controller?.abort();
    action.current.controller = null;
    setBusy(false);
    setRun(null); setEvents([]); setDiff(null); setEvidence(null); setRecovery(null);
    setAcknowledge(false); setAdoption(null); setPollError(''); setDetailError('');
    setDetailLoading(false); setError(''); setTab('events'); cursor.current = 0;
  }, []);

  const select = useCallback((id: string) => {
    setView('workbench');
    if (active.current.runId === id) { setShowCreate(!id); setTab('events'); return; }
    clearView();
    active.current.runId = id;
    setSelectedId(id);
    writeStorage('local', selectionKey(active.current.projectId), id);
    setShowCreate(!id);
  }, [clearView]);

  const selectProject = useCallback((id: string) => {
    setView('workbench');
    if (active.current.projectId === id) { setShowCreate(!active.current.runId); return; }
    clearView();
    const saved = readStorage('local', selectionKey(id));
    active.current = { projectId: id, runId: saved };
    setProjectId(id); setSelectedId(saved); setRuns([]); setShowCreate(!saved);
    writeStorage('local', 'doonex.selectedProject', id);
  }, [clearView]);

  useEffect(() => {
    if (!token) return;
    const controller = new AbortController();
    setLoading(true); setError('');
    void Promise.all([
      api.get<Settings>('/settings', controller.signal), api.get<Project[]>('/projects', controller.signal), api.get<ModelProfiles>('/model-profiles', controller.signal),
    ]).then(([nextSettings, nextProjects, nextRegistry]) => {
      if (controller.signal.aborted) return;
      setSettings(nextSettings); setProjects(nextProjects); setRegistry(nextRegistry);
      const saved = readStorage('local', 'doonex.selectedProject');
      selectProject(nextProjects.some((item) => item.id === saved) ? saved : nextProjects[0]?.id ?? '');
    }).catch((err: unknown) => {
      if (!controller.signal.aborted) setError(message(err));
    }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [api, token, selectProject]);

  useEffect(() => {
    if (!token || !projectId || !project) return;
    const controller = new AbortController();
    setLoading(true);
    void api.get<Run[]>(`/runs?project_id=${encodeURIComponent(projectId)}`, controller.signal).then((nextRuns) => {
      if (controller.signal.aborted || active.current.projectId !== projectId) return;
      setRuns(nextRuns);
      if (!nextRuns.some((item) => item.id === active.current.runId)) select(nextRuns[0]?.id ?? '');
    }).catch((err: unknown) => {
      if (!controller.signal.aborted) setError(message(err));
    }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [api, token, projectId, project?.id, select]);

  useEffect(() => {
    if (!token || !projectId || !selectedId || !project) return;
    const controller = new AbortController();
    let timeout: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const [nextRun, nextEvents, nextRuns] = await Promise.all([
          api.get<Run>(runPath(projectId, selectedId), controller.signal),
          api.get<{ events: RunEvent[]; cursor: number }>(runPath(projectId, selectedId, 'events', { after: String(cursor.current) }), controller.signal),
          api.get<Run[]>(`/runs?project_id=${encodeURIComponent(projectId)}`, controller.signal),
        ]);
        if (controller.signal.aborted || active.current.projectId !== projectId || active.current.runId !== selectedId) return;
        if (nextRun.request.project_id !== projectId) throw new Error('服务返回的运行不属于当前项目。');
        setRun(nextRun); setRuns(nextRuns);
        setEvents((old) => {
          const seen = new Set(old.map((item) => item.id));
          return [...old, ...nextEvents.events.filter((item) => !seen.has(item.id))];
        });
        cursor.current = nextEvents.cursor; setPollError('');
      } catch (err) {
        if (!controller.signal.aborted) setPollError(message(err));
      } finally {
        if (!controller.signal.aborted) timeout = setTimeout(poll, 2000);
      }
    };
    void poll();
    return () => { controller.abort(); clearTimeout(timeout); };
  }, [api, selectedId, token, projectId, project?.id]);

  useEffect(() => {
    setDetailError('');
    if (!run || run.id !== selectedId || run.request.project_id !== projectId || tab === 'events') return;
    const controller = new AbortController();
    const isCurrent = () => !controller.signal.aborted && active.current.projectId === projectId && active.current.runId === run.id;
    setDetailLoading(true);
    const path = runPath(projectId, run.id, tab);
    const request = tab === 'diff'
      ? api.get<Diff>(path, controller.signal).then((result) => { if (isCurrent()) setDiff(result); })
      : api.get<Record<string, unknown>>(path, controller.signal).then((result) => { if (isCurrent()) setEvidence(result); });
    void request.catch((err: unknown) => { if (isCurrent()) setDetailError(message(err)); })
      .finally(() => { if (isCurrent()) setDetailLoading(false); });
    return () => controller.abort();
  }, [api, projectId, selectedId, run?.id, run?.state, run?.updated_at, tab]);

  useEffect(() => () => action.current.controller?.abort(), []);

  const perform = async (operation: (context: ActionContext) => Promise<void>) => {
    action.current.controller?.abort();
    const controller = new AbortController();
    const generation = ++action.current.generation;
    action.current.controller = controller;
    const source = { ...active.current };
    const isCurrent = () => !controller.signal.aborted && action.current.generation === generation && currentApi.current === api
      && active.current.projectId === source.projectId && active.current.runId === source.runId;
    setBusy(true); setError('');
    try { await operation({ signal: controller.signal, isCurrent }); }
    catch (err) { if (isCurrent()) setError(message(err)); }
    finally { if (isCurrent()) { setBusy(false); action.current.controller = null; } }
  };

  const control = (actionName: 'pause' | 'cancel' | 'resume') => {
    if (!run || run.id !== selectedId || run.request.project_id !== projectId) return;
    void perform(async ({ signal, isCurrent }) => {
      let nextRun: Run;
      try {
        nextRun = await api.post<Run>(runPath(projectId, run.id, actionName), actionName === 'resume' ? { acknowledge_unknown: acknowledge } : {}, signal);
      } catch (err) {
        if (actionName === 'resume' && isCurrent()) {
          try {
            const nextRecovery = await api.get<Recovery>(runPath(projectId, run.id, 'recovery'), signal);
            if (isCurrent()) setRecovery(nextRecovery);
          } catch { /* Keep the original actionable error. */ }
        }
        throw err;
      }
      if (!isCurrent()) return;
      setRun(nextRun); setRecovery(null); setAcknowledge(false);
      const nextRuns = await api.get<Run[]>(`/runs?project_id=${encodeURIComponent(projectId)}`, signal);
      if (isCurrent()) setRuns(nextRuns);
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
          writeStorage('session', 'doonex.token', value);
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

  const hasModel = !!executorModel && (executorProfile?.credential_available ?? (!executorProfileId && settings?.credential_available))
    && (workflow !== 'execute_review' || !!reviewerModel && (reviewerProfile?.credential_available ?? (!reviewerProfileId && settings?.credential_available)));
  const isSubscription = models.every((model) => model?.billing_mode === 'subscription');
  const hasPrice = models.every((model) => model && (model.billing_mode === 'subscription' || model.input_price_per_million != null && model.output_price_per_million != null && !!model.price_source));
  const canCreate = !!project && hasModel && hasPrice && !!budget && !busy;
  const selected = run?.id === selectedId && run.request.project_id === projectId ? run : null;
  const selectedModels = selected ? selected.request.workflow === 'execute_review'
    ? [selected.request.role_models?.executor ?? selected.request.model, selected.request.role_models?.reviewer ?? selected.request.model]
    : [selected.request.role_models?.executor ?? selected.request.model] : [];
  const selectedSubscription = selectedModels.length > 0 && selectedModels.every((model) => model.billing_mode === 'subscription');
  const selectedMixedBilling = selectedModels.some((model) => model.billing_mode === 'subscription') && selectedModels.some((model) => model.billing_mode === 'metered');
  const isActive = !!selected && activeStates.has(selected.state);

  return <div className="app-shell">
    <header className="app-header">
      <div className="wordmark">DooNex<span>编程工作台</span></div>
      <div className="header-actions"><button className="quiet" aria-pressed={view === 'models'} onClick={() => { setView((old) => old === 'models' ? 'workbench' : 'models'); setError(''); }}>模型设置</button><span className="local-label">本地 · 有限协作</span><button className="quiet" onClick={() => {
        clearView(); active.current = { projectId: '', runId: '' }; writeStorage('session', 'doonex.token', ''); setToken(''); setSettings(null); setRegistry(null); setView('workbench'); setProjects([]); setProjectId(''); setSelectedId(''); setRuns([]);
      }}>断开连接</button></div>
    </header>
    <div className="workspace-layout">
      <aside className="sidebar" aria-label="运行列表">
        <div className="project-picker"><label htmlFor="project-select">项目</label><select id="project-select" value={projectId} onChange={(event) => selectProject(event.target.value)} disabled={!projects.length}><option value="" disabled>尚未注册项目</option>{projects.map((item) => <option key={item.id} value={item.id}>{item.name || item.path}</option>)}</select><button className="quiet compact" onClick={() => { setView('workbench'); setShowCreate(true); setError(''); requestAnimationFrame(() => document.getElementById('project-path')?.focus()); }}>注册项目</button></div>
        <div className="sidebar-title"><h2>任务记录</h2><button className="primary compact" onClick={() => { setView('workbench'); setShowCreate(true); setError(''); }}>新任务</button></div>
        {loading && <p className="muted" role="status">正在连接服务…</p>}
        {!loading && !runs.length && <p className="empty-copy">还没有运行记录。注册项目后，开始第一个任务。</p>}
        <ul className="run-list">{runs.map((item) => <li key={item.id}>
          <button className={`run-item ${item.id === selectedId && !showCreate ? 'selected' : ''}`} aria-current={item.id === selectedId && !showCreate ? 'page' : undefined} onClick={() => select(item.id)}>
            <span className="run-title">{item.request.prompt}</span>
            <Status state={item.state} />
            <span className="muted run-project">{item.request.project_name || project?.name || project?.path}</span><time dateTime={item.created_at}>{fmtTime(item.created_at)}</time>
          </button>
        </li>)}</ul>
      </aside>
      <main className="main-content">
        {error && <div className="notice error" role="alert"><span>{error}</span><button className="quiet" aria-label="关闭错误提示" onClick={() => setError('')}>关闭</button></div>}
        <section className="project-strip" aria-label="当前项目">
          <div><span className="eyebrow">当前项目{project?.name ? ` · ${project.name}` : ''}</span><strong className="path-text">{project?.path ?? '尚未注册'}</strong>{project && <span className="muted small path-text">{project.id}</span>}</div>
          {project && <span className="muted small">{project.dirty ? '原工作区有未提交改动' : '原工作区干净'}</span>}
        </section>

        {view === 'models' && registry ? <ModelSettings api={api} registry={registry} onRegistry={setRegistry} onClose={() => setView('workbench')} /> : showCreate ? <div className="create-view">
          <div className="page-heading"><div><h1>开始一个可信的任务</h1><p className="muted">固定起始版本与验收条件，在隔离工作区中执行。</p></div>{selectedId && <button className="quiet" onClick={() => setShowCreate(false)}>返回运行</button>}</div>
          <section className="section-block">
            <h2>注册或重新校验项目</h2>
            <form className="inline-form" onSubmit={(event) => {
              event.preventDefault();
              void perform(async ({ signal, isCurrent }) => {
                const result = await api.post<Project>('/projects', { path: projectPath.trim() }, signal);
                if (!isCurrent()) return;
                setProjects((old) => [...old.filter((item) => item.id !== result.id), result]);
                updateDraft({ baseRef: result.head }, result.id);
                setProjectPath(''); selectProject(result.id); setShowCreate(true);
              });
            }}>
              <div className="grow"><label htmlFor="project-path">本地 Git 仓库路径</label><input id="project-path" value={projectPath} onChange={(event) => setProjectPath(event.target.value)} placeholder="/绝对路径/项目" required spellCheck={false} /></div>
              <button disabled={busy} type="submit">注册 / 重新校验</button>
            </form>
            <p className="field-help">只读取选定提交建立独立工作副本，原工作区中的未提交内容不会自动带入。</p>
          </section>
          <RoleModelChoices registry={registry} workflow={workflow} executorId={executorProfileId} reviewerId={reviewerProfileId} onChange={(role, id) => updateDraft(role === 'executor' ? { executorProfileId: id } : { reviewerProfileId: id })} onSettings={() => setView('models')} fallback={settings} />
          {!hasModel && <p className="field-help warning-text">请选择有效配置，并在服务端提供每个角色所需的凭据引用。</p>}
          {!hasPrice && <p className="field-help warning-text">按量模型需要有来源的输入和输出价格。补齐后才能使用共享费用预算。</p>}
          <form onSubmit={(event) => {
            event.preventDefault();
            if (!canCreate || !budget) return;
            void perform(async ({ signal, isCurrent }) => {
              const created = await api.post<Run>('/runs', { project_id: projectId, workflow, model_profile_id: executorProfileId || null, ...(workflow === 'execute_review' ? { reviewer_model_profile_id: reviewerProfileId || null } : {}), prompt: prompt.trim(), base_ref: baseRef.trim(), checks: checks.split('\n').map((line) => line.trim()).filter(Boolean), budget: { ...budget, max_cost_usd: isSubscription ? null : budget.max_cost_usd } }, signal);
              if (!isCurrent()) return;
              setRuns((old) => [created, ...old.filter((item) => item.id !== created.id)]);
              updateDraft({ prompt: '', checks: '' }); select(created.id);
            });
          }}>
            <section className="section-block">
              <h2>需求与验收</h2>
              <label htmlFor="workflow">执行方式</label><select id="workflow" value={workflow} onChange={(event) => updateDraft({ workflow: event.target.value as Workflow })}>
                <option value="execute_review">执行后只读审查，再运行检查</option><option value="execute">仅执行，再运行检查</option>
              </select>
              <p className="field-help">默认由执行者完成修改，再由审查者读取固定快照。两个角色共享同一预算；没有自动修复、重试或递归委派。</p>
              <label htmlFor="base-ref">起始提交或引用</label><input className="mono" id="base-ref" value={baseRef} onChange={(event) => updateDraft({ baseRef: event.target.value })} required spellCheck={false} />
              <p className="field-help">启动前解析为固定提交。可输入 HEAD、分支名或提交 SHA。</p>
              <label htmlFor="task-prompt">希望完成的工作</label><textarea id="task-prompt" rows={4} value={prompt} onChange={(event) => updateDraft({ prompt: event.target.value })} placeholder="描述要修改的行为、范围和需要保留的约束。" required />
              <label htmlFor="checks">验收命令</label><textarea className="mono" id="checks" rows={3} value={checks} onChange={(event) => updateDraft({ checks: event.target.value })} placeholder="每行一条命令，例如 python -m unittest discover" spellCheck={false} />
              <p className="field-help">命令在隔离环境中按启动时版本保存并执行。未填写检查时，结果只能标记为待检查。</p>
            </section>
            <section className="section-block">
              <h2>本次运行共享预算</h2>
              {budget ? <div className="budget-inputs">
                <label>模型调用上限<input type="number" min={1} step={1} required value={budget.max_calls} onChange={(event) => updateDraft({ budget: { ...budget, max_calls: Number(event.target.value) } })} /></label>
                <label>Token 上限<input type="number" min={1} step={1} required value={budget.max_tokens} onChange={(event) => updateDraft({ budget: { ...budget, max_tokens: Number(event.target.value) } })} /></label>
                <label>估计费用上限（US$）<input type="number" min={0.0001} step="any" required={!isSubscription} disabled={isSubscription} placeholder={isSubscription ? '全部包月连接' : ''} value={isSubscription ? '' : budget.max_cost_usd ?? ''} onChange={(event) => updateDraft({ budget: { ...budget, max_cost_usd: event.target.value === '' ? null : Number(event.target.value) } })} /></label>
                <label>总时长上限（秒）<input type="number" min={1} step={1} required value={budget.max_seconds} onChange={(event) => updateDraft({ budget: { ...budget, max_seconds: Number(event.target.value) } })} /></label>
              </div> : <p className="muted">正在读取预算配置…</p>}
              <p className="field-help">{isSubscription ? '所有角色均为包月连接，货币费用未知，仍受调用、token 和时长上限约束。' : '至少一个角色按量计费，费用预算用于已知按量小计。含包月角色时，总货币费用保持未知。'}</p>
            </section>
            <div className="form-footer"><span className="muted small">需求、验收和预算将随本次运行保存。</span><button className="primary" type="submit" disabled={!canCreate}>{busy ? '正在创建…' : '创建并开始运行'}</button></div>
          </form>
        </div> : selected ? <div className="run-view">
          <div className="page-heading"><div><span className="eyebrow">运行 {selected.id}</span><h1 className="task-heading">{selected.request.prompt}</h1></div><Status state={selected.state} /></div>
          <div className="run-meta"><span className="path-text">项目 {selected.request.project_name || project?.name || selected.request.repo_path}</span><span>起始提交 <code>{selected.request.base_commit}</code></span><span>创建于 {fmtTime(selected.created_at)}</span></div>
          <FrozenModels run={selected} />
          {selected.reason && <p className="notice">{selected.reason}</p>}
          {pollError && <p className="notice error" role="alert">{pollError} 当前显示最近一次已记录结果；会自动重连。</p>}
          <div className="run-toolbar">
            <div className="button-row">
              {(selected.state === 'running' || selected.state === 'queued') && <button disabled={busy} onClick={() => control('pause')}>暂停</button>}
              {(isActive || selected.state === 'paused' || selected.state === 'interrupted') && <button className="danger" disabled={busy || selected.state === 'cancelling'} onClick={() => control('cancel')}>取消运行</button>}
              {selected.state === 'paused' && <button className="primary" disabled={busy} onClick={() => control('resume')}>继续执行</button>}
              {selected.state === 'interrupted' && <button disabled={busy} onClick={() => { void perform(async ({ signal, isCurrent }) => {
                const nextRecovery = await api.get<Recovery>(runPath(projectId, selected.id, 'recovery'), signal);
                if (isCurrent()) { setRecovery(nextRecovery); setAcknowledge(false); }
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
          <WorkflowProgress run={selected} />
          <section className="usage-panel" aria-label="共享预算与累计用量">
            <div><span>累计模型调用</span><strong>{selected.usage.calls ?? 0}<small> / {selected.request.budget.max_calls}</small></strong></div>
            <div><span>累计已记录 tokens</span><strong>{(selected.usage.tokens ?? 0).toLocaleString()}<small> / {selected.request.budget.max_tokens.toLocaleString()}</small></strong></div>
            <div><span>{selectedMixedBilling ? '已知按量估计费用' : selectedSubscription ? '货币费用未知' : '已记录估计费用'}</span><strong>{selectedSubscription ? '包月连接' : money(selectedMixedBilling ? selected.usage.metered_cost_usd : selected.usage.cost_usd)}{!selectedSubscription && <small> / {money(selected.request.budget.max_cost_usd)}</small>}{selectedMixedBilling && <small>含包月角色，总费用未知</small>}</strong></div>
            <div><span>总时长上限</span><strong>{selected.request.budget.max_seconds}<small> 秒</small></strong></div>
            <p>预留：{(selected.usage.reserved_tokens ?? 0).toLocaleString()} tokens{selectedSubscription ? '。包月连接按调用、token 和时长约束，货币费用未知。' : selectedMixedBilling ? `，按量预留 ${money(selected.usage.reserved_metered_cost_usd)}。含包月角色，总费用未知。` : `，${money(selected.usage.reserved_cost_usd)}。`}{selected.usage.unknown_calls > 0 ? `有 ${selected.usage.unknown_calls} 次调用的实际用量未知，保留预算预留。` : '预留会在供应商返回用量后结算。'}</p>
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
            {tab === 'diff' && <>{diff?.source === 'review_handoff' && <div className="notice" role="status"><strong>审查交接快照，尚未完成验收</strong><p>以下修改来自执行者交给审查者的固定快照。{diff.snapshot_digest && <code>{diff.snapshot_digest}</code>}</p></div>}{detailLoading && diff === null ? <p role="status" className="muted">正在读取代码变更…</p> : diff?.source !== 'unavailable' && diff?.diff ? <pre className="diff-output">{diff.diff}</pre> : !detailError && <p className="empty-copy">目前没有可显示的代码差异。</p>}</>}
            {tab === 'evidence' && <>
              <ReviewReport run={selected} />
              <div className="evidence-intro"><h2>与代码快照绑定的交付证据</h2><p className="muted">检查命令、退出码、日志和时间以服务保存的报告为准。失败、缺失或过期证据不能证明当前代码已通过验收。</p></div>
              <div className="button-row artifact-actions">{(['patch', 'snapshot', 'report'] as const).map((name) => <button key={name} disabled={busy} onClick={() => { void perform(({ signal }) => api.download(projectId, selected.id, name, signal)); }}>下载{({ patch: '补丁', snapshot: '代码快照', report: '检查报告' })[name]}</button>)}<button disabled={busy || isActive} onClick={() => { void perform(async ({ signal, isCurrent }) => { const result = await api.post<{ safe: boolean; reason: string }>(runPath(projectId, selected.id, 'adoption-check'), {}, signal); if (isCurrent()) setAdoption(result); }); }}>检查采纳条件</button></div>
              {adoption && <div className={`notice ${adoption.safe ? '' : 'error'}`} role="status"><strong>{adoption.safe ? '采纳预检通过' : '采纳预检未通过'}</strong><p>{adoption.reason}</p><p>请保留补丁并人工检查后整合。预检不会修改原项目，也不会推送或部署。</p></div>}
              {detailLoading && evidence === null ? <p className="muted" role="status">正在读取证据…</p> : evidence ? <CheckReport evidence={evidence} busy={busy} onDownloadLog={(name) => { void perform(({ signal }) => api.download(projectId, selected.id, name, signal)); }} /> : !detailError && <p className="empty-copy">尚无验证报告。</p>}
            </>}
          </section>
        </div> : <div className="empty-state"><h1>{selectedId ? '正在读取运行…' : '准备开始'}</h1>{pollError && <p className="notice error" role="alert">{pollError} 会自动重试读取。</p>}<p className="muted">{selectedId ? '从已保存的记录恢复视图。' : '注册一个项目，创建第一个任务。'}</p><button onClick={() => setShowCreate(true)}>创建任务</button></div>}
      </main>
    </div>
  </div>;
}

createRoot(document.getElementById('root')!).render(<React.StrictMode><App /></React.StrictMode>);
