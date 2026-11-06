import { useEffect, useRef, useState } from 'react';
import { Api, ApiError, type ArenaEvaluation, type ArenaTaskset, type BackgroundAuthorization, type BackgroundDay, type BackgroundInput, type Budget, type ModelProfiles, type SkillVersion } from './api';
import { formatMoney } from './model-settings';
import { skillReady } from './skills';

const states: Record<BackgroundAuthorization['state'], string> = { draft: '草稿 · 未启用', enabled: '已启用 · 等待派发', consumed: '唯一批次已创建', disabled: '已撤销' };
const evaluationStates: Record<string, string> = { queued: '等待执行', running: '正在调优', yielded: '已安全让出', cancelling: '正在停止', complete: '批次已跑完', completed: '批次已跑完', interrupted: '故障中断 · 待核查', failed: '调优失败', cancelled: '已停止', budget_exceeded: '预算已用尽' };
const fmtLocal = (value: string) => new Date(value).toLocaleString('zh-CN', { hour12: false, timeZoneName: 'short' });
const fmtUTC = (value: string) => new Date(value).toISOString().replace('T', ' ').replace('.000Z', ' UTC');
const errorMessage = (error: unknown) => error instanceof Error ? error.message : String(error);
const normalizedBudget = (value: Budget, subscription: boolean): Budget => ({ ...value, max_cost_usd: subscription ? null : value.max_cost_usd });
const validBudget = (value: Budget, batch: boolean, subscription: boolean) => Number.isInteger(value.max_calls) && value.max_calls >= 1 && value.max_calls <= (batch ? 10000 : 100)
  && Number.isInteger(value.max_tokens) && value.max_tokens >= 1 && value.max_tokens <= (batch ? 100000000 : 10000000)
  && Number.isInteger(value.max_seconds) && value.max_seconds >= 1 && value.max_seconds <= (batch ? 86400 : 7200)
  && (subscription || value.max_cost_usd != null && Number.isFinite(value.max_cost_usd) && value.max_cost_usd > 0 && value.max_cost_usd <= (batch ? 1000 : 100));

function defaultWindow() {
  let start = new Date(Math.ceil((Date.now() + 5 * 60000) / 60000) * 60000);
  const lastMinute = new Date(`${start.toISOString().slice(0, 10)}T23:59:00Z`);
  if (start >= lastMinute) start = new Date(lastMinute.getTime() + 6 * 60000);
  const end = new Date(Math.min(start.getTime() + 55 * 60000, new Date(`${start.toISOString().slice(0, 10)}T23:59:00Z`).getTime()));
  return { date: start.toISOString().slice(0, 10), start: start.toISOString().slice(11, 16), end: end.toISOString().slice(11, 16) };
}

function BudgetInputs({ prefix, title, value, onChange, subscription, batch = false }: { prefix: string; title: string; value: Budget; onChange: (value: Budget) => void; subscription: boolean; batch?: boolean }) {
  return <fieldset className="model-fields"><legend>{title}</legend><div className="budget-inputs">
    <label htmlFor={`${prefix}-calls`}>调用上限<input id={`${prefix}-calls`} type="number" min={1} max={batch ? 10000 : 100} step={1} required value={value.max_calls} onChange={(event) => onChange({ ...value, max_calls: Number(event.target.value) })} /></label>
    <label htmlFor={`${prefix}-tokens`}>Token 上限<input id={`${prefix}-tokens`} type="number" min={1} max={batch ? 100000000 : 10000000} step={1} required value={value.max_tokens} onChange={(event) => onChange({ ...value, max_tokens: Number(event.target.value) })} /></label>
    <label htmlFor={`${prefix}-cost`}>估计费用上限（US$）<input id={`${prefix}-cost`} type="number" min={0} max={batch ? 1000 : 100} step="any" required={!subscription} disabled={subscription} value={subscription ? '' : value.max_cost_usd ?? ''} placeholder={subscription ? '包月，金额未知' : ''} onChange={(event) => onChange({ ...value, max_cost_usd: event.target.value === '' ? null : Number(event.target.value) })} /></label>
    <label htmlFor={`${prefix}-seconds`}>累计时长上限（秒）<input id={`${prefix}-seconds`} type="number" min={1} max={batch ? 86400 : 7200} step={1} required value={value.max_seconds} onChange={(event) => onChange({ ...value, max_seconds: Number(event.target.value) })} /></label>
  </div></fieldset>;
}

function BudgetSummary({ value }: { value: Budget }) {
  return <>{value.max_calls.toLocaleString()} 次调用 · {value.max_tokens.toLocaleString()} tokens · {value.max_seconds.toLocaleString()} 秒 · 费用上限 {formatMoney(value.max_cost_usd)}</>;
}

function useDay(api: Api, date: string, refresh: number) {
  const [value, setValue] = useState<{ date: string; day: BackgroundDay | null; loading: boolean; error: string }>({ date: '', day: null, loading: false, error: '' });
  useEffect(() => {
    const controller = new AbortController();
    setValue((old) => ({ date, day: old.date === date ? old.day : null, loading: !!date, error: '' }));
    if (date) void api.get<BackgroundDay>(`/background/days/${encodeURIComponent(date)}`, controller.signal).then((day) => {
      if (!controller.signal.aborted) setValue({ date, day, loading: false, error: '' });
    }).catch((error: unknown) => {
      if (!controller.signal.aborted) setValue((old) => ({ date, day: error instanceof ApiError && error.status === 404 ? null : old.date === date ? old.day : null, loading: false, error: error instanceof ApiError && error.status === 404 ? '' : errorMessage(error) }));
    });
    return () => controller.abort();
  }, [api, date, refresh]);
  return value.date === date ? value : { date, day: null, loading: !!date, error: '' };
}

function DayLedger({ date, day, loading, error }: { date: string; day: BackgroundDay | null; loading: boolean; error: string }) {
  return <section className="background-day" aria-label="共享后台日账本"><h2>{date || '所选日期'} UTC · 全部后台授权共用</h2>
    {error && <p className="notice error" role="alert">{error}{day && ' 以下保留最近读取的日账本，尚未同步最新用量。'}</p>}
    {day ? <>
      <p className="small">当天固定额度：<BudgetSummary value={day.budget} /></p>
      <div className="table-scroll"><table className="role-table background-ledger"><caption>原日用量与保留额度</caption><thead><tr><th>项目</th><th>已结算</th><th>仍预留</th><th>当日共享上限</th></tr></thead><tbody>
        <tr><th scope="row">调用数</th><td>{day.used.calls.toLocaleString()}</td><td>{day.reserved.calls.toLocaleString()}</td><td>{day.budget.max_calls.toLocaleString()}</td></tr>
        <tr><th scope="row">Tokens</th><td>{day.used.tokens.toLocaleString()}</td><td>{day.reserved.tokens.toLocaleString()}</td><td>{day.budget.max_tokens.toLocaleString()}</td></tr>
        <tr><th scope="row">累计时长（秒）</th><td>{day.used.seconds.toLocaleString()}</td><td>{day.reserved.seconds.toLocaleString()}</td><td>{day.budget.max_seconds.toLocaleString()}</td></tr>
        <tr><th scope="row">总金额</th><td>{formatMoney(day.used.cost_usd)}</td><td>{formatMoney(day.reserved.cost_usd)}</td><td>{formatMoney(day.budget.max_cost_usd)}</td></tr>
        <tr><th scope="row">按量费用小计</th><td>{formatMoney(day.used.metered_cost_usd)}</td><td>{formatMoney(day.reserved.metered_cost_usd)}</td><td>{formatMoney(day.budget.max_cost_usd)}</td></tr>
      </tbody></table></div>
      <p className="field-help">失败、未知结果和未结算批次继续保留预留。包月总金额为未知；有按量费用上限时，它继续约束按量小计。换授权、模型或重启不会增加当天额度。</p>
    </> : loading ? <p className="muted small" role="status">正在读取共享日账本…</p> : !error && <p className="field-help">当天尚无配置。首次保存授权会固定本产品该 UTC 日期的后台日额度；之后的授权必须复用同一上限。</p>}
  </section>;
}

function BackgroundDraft({ api, tasksets, skills, registry, defaultBudget, busy, onCreate, onClose }: { api: Api; tasksets: ArenaTaskset[]; skills: SkillVersion[]; registry: ModelProfiles | null; defaultBudget: Budget; busy: boolean; onCreate: (input: BackgroundInput) => void; onClose: () => void }) {
  const [name, setName] = useState('后台调优');
  const [tasksetId, setTasksetId] = useState(tasksets.find((item) => item.split === 'tuning' && item.valid)?.id ?? '');
  const [candidates, setCandidates] = useState<string[]>([]);
  const [profileId, setProfileId] = useState(registry?.default_profile_id ?? registry?.profiles[0]?.id ?? '');
  const [repeats, setRepeats] = useState(1);
  const [window, setWindow] = useState(defaultWindow);
  const [runBudget, setRunBudget] = useState(defaultBudget);
  const [batchBudget, setBatchBudget] = useState<Budget>({ max_calls: 100, max_tokens: 1000000, max_cost_usd: 10, max_seconds: 3600 });
  const [dailyBudget, setDailyBudget] = useState<Budget>({ max_calls: 100, max_tokens: 1000000, max_cost_usd: 10, max_seconds: 3600 });
  const day = useDay(api, /^\d{4}-\d{2}-\d{2}$/.test(window.date) ? window.date : '', 0);
  const profile = registry?.profiles.find((item) => item.id === profileId);
  const subscription = profile?.model.billing_mode === 'subscription';
  const taskset = tasksets.find((item) => item.id === tasksetId);
  const availableSkills = skills.filter(skillReady);
  const start = new Date(`${window.date}T${window.start}:00Z`), end = new Date(`${window.date}T${window.end}:00Z`);
  const validWindow = Number.isFinite(start.getTime()) && Number.isFinite(end.getTime()) && start < end && start.toISOString().slice(0, 10) === end.toISOString().slice(0, 10);
  const priceReady = subscription || !!profile?.model.price_source && profile.model.input_price_per_million != null && profile.model.output_price_per_million != null;
  const sharedBudget = day.day?.budget ?? normalizedBudget(dailyBudget, subscription);
  const dailyCompatible = subscription || sharedBudget.max_cost_usd != null && sharedBudget.max_cost_usd > 0;
  const canSave = !busy && !day.loading && !day.error && validWindow && !!taskset?.valid && taskset.split === 'tuning' && taskset.task_count >= 5 && candidates.length >= 2 && candidates.length <= 3
    && candidates.every((id) => availableSkills.some((item) => item.version_id === id)) && !!profile?.credential_available && priceReady && dailyCompatible
    && Number.isInteger(repeats) && repeats >= 1 && repeats <= 10 && validBudget(runBudget, false, subscription) && validBudget(batchBudget, true, subscription) && validBudget(sharedBudget, true, subscription);
  return <form className="background-draft" onSubmit={(event) => { event.preventDefault(); if (canSave) onCreate({ name: name.trim() || '后台调优', taskset_id: tasksetId, skill_version_ids: candidates, model_profile_id: profileId, repeats, run_budget: normalizedBudget(runBudget, subscription), batch_budget: normalizedBudget(batchBudget, subscription), daily_budget: sharedBudget, window_start: start.toISOString(), window_end: end.toISOString() }); }}>
    <div className="section-heading"><h2>保存一份关闭的授权草稿</h2><button className="quiet compact" type="button" onClick={onClose}>返回记录</button></div>
    <p className="field-help">保存后先核对冻结条件，再明确启用。一次授权最多创建一个批次，包含无额外 Skill 的基线；后台只产生调优结果。</p>
    <label htmlFor="background-name">授权名称</label><input id="background-name" maxLength={100} value={name} onChange={(event) => setName(event.target.value)} />
    <label htmlFor="background-taskset">固定调优任务集</label><select id="background-taskset" value={tasksetId} onChange={(event) => setTasksetId(event.target.value)} required><option value="" disabled>选择有效 tuning 任务集</option>{tasksets.filter((item) => item.split === 'tuning').map((item) => <option key={item.id} value={item.id} disabled={!item.valid || item.task_count < 5}>{item.name} · {item.task_count} 任务{!item.valid ? ' · 完整性无效' : item.task_count < 5 ? ' · 数量不足' : ''}</option>)}</select>
    {taskset && <p className="field-help path-text">{taskset.category} · tuning · 版本 <code>{taskset.id}</code>。保留评测集不用于后台调优。</p>}
    {!tasksets.some((item) => item.split === 'tuning' && item.valid && item.task_count >= 5) && <p className="warning-text small">请先在 Arena 导入至少含 5 个任务的有效调优集。</p>}
    <fieldset className="model-fields"><legend>固定 2–3 个已批准 Skill 版本</legend><p className="field-help">仅显示审核已批准且完整性有效的版本，启用和派发时服务端会再次核查。</p>{availableSkills.length === 0 && <p className="warning-text small">暂无可用版本，请先在 Skills 导入并审核。</p>}{availableSkills.map((skill) => <label className="checkbox-label" key={skill.version_id}><input type="checkbox" checked={candidates.includes(skill.version_id)} disabled={busy || !candidates.includes(skill.version_id) && candidates.length >= 3} onChange={(event) => setCandidates((old) => event.target.checked ? [...old, skill.version_id] : old.filter((id) => id !== skill.version_id))} /><span>{skill.metadata.source}<br /><code>{skill.version_id}</code><br /><small>内容摘要 {skill.content_digest}</small></span></label>)}</fieldset>
    <div className="settings-grid"><div><label htmlFor="background-model">固定模型配置</label><select id="background-model" value={profileId} onChange={(event) => setProfileId(event.target.value)} required><option value="" disabled>选择模型配置</option>{registry?.profiles.map((item) => <option key={item.id} value={item.id}>{item.name} · {item.model.model_id}</option>)}</select>{profile && <p className="field-help path-text">{profile.model.provider} · {profile.model.api_mode} · {profile.credential_available ? '凭据引用已就绪' : '凭据引用未就绪'}<br />配置版本 <code>{profile.revision}</code></p>}</div><div><label htmlFor="background-repeats">每任务每组重复次数</label><input id="background-repeats" type="number" min={1} max={10} step={1} required value={repeats} onChange={(event) => setRepeats(Number(event.target.value))} /><p className="field-help">计划 {(taskset?.task_count ?? 0) * (candidates.length + 1) * repeats} 次尝试；后台批次不具有推广资格。</p></div></div>
    {!priceReady && <p className="warning-text small">按量模型需要有来源的输入和输出单价。</p>}
    <fieldset className="model-fields"><legend>同一 UTC 日期的有限窗口</legend><div className="settings-grid background-window"><div><label htmlFor="background-date">UTC 日期</label><input id="background-date" type="date" required value={window.date} onChange={(event) => setWindow({ ...window, date: event.target.value })} /></div><div><label htmlFor="background-start">开始（UTC）</label><input id="background-start" type="time" required value={window.start} onChange={(event) => setWindow({ ...window, start: event.target.value })} /></div><div><label htmlFor="background-end">截止并到期（UTC）</label><input id="background-end" type="time" required value={window.end} onChange={(event) => setWindow({ ...window, end: event.target.value })} /></div></div>
      {validWindow ? <p className="field-help" aria-label="运行窗口换算">UTC：{fmtUTC(start.toISOString())} → {fmtUTC(end.toISOString())}<br />本地：{fmtLocal(start.toISOString())} → {fmtLocal(end.toISOString())}</p> : <p className="warning-text small" role="alert">开始必须早于截止，且窗口必须完整位于同一个 UTC 日期内。</p>}
      <p className="field-help">到期或 UTC 日期变化后停止新动作；在途结果继续计入原日账本。窗口不自动重复。</p>
    </fieldset>
    <BudgetInputs prefix="background-run" title="每次尝试的预算" value={runBudget} onChange={setRunBudget} subscription={subscription} />
    <BudgetInputs prefix="background-batch" title="唯一批次的共享上限" value={batchBudget} onChange={setBatchBudget} subscription={subscription} batch />
    <DayLedger {...day} />
    {day.day ? <p className="field-help">这份草稿将复用上述固定日额度，不能编辑或通过新增授权提额。</p> : !day.loading && !day.error && <BudgetInputs prefix="background-daily" title="首次固定当天产品后台总额度" value={dailyBudget} onChange={setDailyBudget} subscription={subscription} batch />}
    {!dailyCompatible && <p className="warning-text small">当天已固定的日额度没有按量费用上限，不能新增按量授权。换授权不能扩大或修改该日额度。</p>}
    <div className="form-footer"><span className="muted small">保存草稿不会开始模型调用。包月金额未知，调用、tokens 和时长仍有限。</span><button className="primary" type="submit" disabled={!canSave}>{busy ? '正在保存…' : '保存关闭草稿'}</button></div>
  </form>;
}

function AuthorizationDetail({ api, item, refresh, busy, onChange, onEvaluation }: { api: Api; item: BackgroundAuthorization; refresh: number; busy: boolean; onChange: (action: 'enable' | 'disable', item: BackgroundAuthorization) => void; onEvaluation: (id: string) => void }) {
  const [acknowledge, setAcknowledge] = useState(false);
  const [evaluation, setEvaluation] = useState<Pick<ArenaEvaluation, 'id' | 'state' | 'reason' | 'usage'> | null>(null);
  const [evaluationError, setEvaluationError] = useState('');
  const day = useDay(api, item.date, refresh);
  const expired = new Date(item.expires_at).getTime() <= Date.now();
  useEffect(() => { setAcknowledge(false); }, [item.id, item.revision]);
  useEffect(() => {
    const controller = new AbortController();
    setEvaluation((old) => item.evaluation ?? (old?.id === item.evaluation_id ? old : null)); setEvaluationError('');
    if (item.evaluation_id && !item.evaluation) void api.get<ArenaEvaluation>(`/arena/evaluations/${encodeURIComponent(item.evaluation_id)}`, controller.signal).then((value) => { if (!controller.signal.aborted) setEvaluation(value); }).catch((error: unknown) => { if (!controller.signal.aborted) setEvaluationError(errorMessage(error)); });
    return () => controller.abort();
  }, [api, item.evaluation_id, item.evaluation, refresh]);
  return <section className="background-detail" aria-label="后台授权详情">
    <div className="section-heading"><div><h2>{item.name}</h2><p className="small path-text"><code>{item.id}</code> · 版本 {item.revision} · {states[item.state]}{expired && ' · 已到期'}</p></div>{['enabled', 'consumed'].includes(item.state) && <button className="danger compact" disabled={busy} onClick={() => onChange('disable', item)}>撤销并停止新派发</button>}</div>
    <section className="section-block" aria-label="冻结授权条件"><h3>冻结条件</h3><dl className="config-list">
      <div><dt>模型配置</dt><dd>{item.snapshot.model_profile.name} · {item.snapshot.model.model_id} · {item.snapshot.model.api_mode}<br /><code>{item.snapshot.model_profile.revision}</code></dd></div>
      <div><dt>调优集</dt><dd><code>{item.snapshot.taskset_id}</code><br />报告摘要 <code>{item.snapshot.taskset_report_sha256}</code></dd></div>
      <div><dt>候选版本</dt><dd>无额外 Skill 基线；{item.input.skill_version_ids.map((id) => <span className="background-skill-id" key={id}><code>{id}</code></span>)}</dd></div>
      <div><dt>固定重复 / 批次数</dt><dd>每任务每组 {item.input.repeats} 次 · 最多 1 批</dd></div>
      <div><dt>每次尝试上限</dt><dd><BudgetSummary value={item.input.run_budget} /></dd></div>
      <div><dt>唯一批次上限</dt><dd><BudgetSummary value={item.input.batch_budget} /></dd></div>
      <div><dt>当天产品后台上限</dt><dd><BudgetSummary value={item.input.daily_budget} /></dd></div>
      <div><dt>UTC 窗口与到期</dt><dd>{fmtUTC(item.window_start)} → {fmtUTC(item.window_end)}<br />到期 {fmtUTC(item.expires_at)}</dd></div>
      <div><dt>本地换算</dt><dd>{fmtLocal(item.window_start)} → {fmtLocal(item.window_end)}</dd></div>
    </dl><p className="field-help">授权固定以上条件；重启或撤销不会创建替代批次、退回未知预留或延长首次启动时钟。后台调优结果不能写入场景推荐。</p></section>
    {item.state === 'draft' && <section className="background-enable" aria-label="明确启用后台授权"><h3>核对后明确启用</h3><p className="small">启用会在有效窗口内消耗已配置的模型用量，并受 Run、批次和共享日额度限制。普通任务优先于手动 Arena，手动 Arena 优先于后台。</p><label className="checkbox-label"><input type="checkbox" checked={acknowledge} onChange={(event) => setAcknowledge(event.target.checked)} disabled={busy || expired} />我已核对冻结条件、有限窗口及共享日额度，授权此唯一后台批次消耗模型用量。</label><button className="primary" disabled={busy || expired || !acknowledge} onClick={() => onChange('enable', item)}>启用并授权模型用量</button>{expired && <p className="warning-text small">授权窗口已到期，不能启用。历史草稿继续保留。</p>}</section>}
    <section className="section-block" aria-label="调度状态与停止原因"><h3>当前调度与结果</h3>
      {item.evaluation_id ? <><p className="small path-text">已固定唯一 Arena 批次 <code>{item.evaluation_id}</code>。{evaluation ? `状态：${evaluationStates[evaluation.state] || evaluation.state}` : '正在读取批次状态…'}</p>{evaluation?.reason && <p className="notice">{evaluation.reason}</p>}{evaluationError && <p className="notice error" role="alert">{evaluationError} 批次记录读取失败。</p>}<button onClick={() => onEvaluation(item.evaluation_id!)}>查看 Arena 结果与运行证据</button></> : <p className="small">{item.state === 'draft' ? '草稿关闭，尚未授权派发。' : item.state === 'disabled' ? '授权已撤销，尚未创建批次。' : expired ? '授权已到期，不能创建批次。' : new Date(item.window_start).getTime() > Date.now() ? '等待 UTC 窗口开始；届时仍需核对更高优先级队列、完整性与共享日余量。' : '尚未创建批次；worker 会核对更高优先级任务、完整性与共享日余量后派发。'}</p>}
      {item.state === 'disabled' && item.evaluation_id && <p className="notice">授权已撤销，停止新派发。在途动作等待结果或超时后继续结算，原失败和未知预留保留。</p>}
      <p className="field-help">用户任务到达后，在可核验的安全检查点让出；已派发动作等待结果或超时。未知动作、残留容器和检查点漂移会故障阻塞，需要原有恢复核查。</p>
    </section>
    <DayLedger {...day} />
    <details className="arena-arm"><summary>授权历史与冻结回执</summary><pre className="raw-metrics">{JSON.stringify({ created_at: item.created_at, updated_at: item.updated_at, enabled_at: item.enabled_at, disabled_at: item.disabled_at, consumed_at: item.consumed_at, history: item.history, snapshot: item.snapshot }, null, 2)}</pre></details>
  </section>;
}

export function BackgroundView({ api, skills, registry, defaultBudget, onEvaluation }: { api: Api; skills: SkillVersion[]; registry: ModelProfiles | null; defaultBudget: Budget; onEvaluation: (id: string) => void }) {
  const [authorizations, setAuthorizations] = useState<BackgroundAuthorization[]>([]);
  const [tasksets, setTasksets] = useState<ArenaTaskset[]>([]);
  const [selectedId, setSelectedId] = useState('');
  const [showCreate, setShowCreate] = useState(false);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [pollError, setPollError] = useState('');
  const [notice, setNotice] = useState('');
  const [refresh, setRefresh] = useState(0);
  const operation = useRef({ generation: 0, controller: null as AbortController | null });
  const pollVersion = useRef(0);
  const selected = authorizations.find((item) => item.id === selectedId);
  useEffect(() => {
    const controller = new AbortController(); let timer: ReturnType<typeof setTimeout> | undefined;
    const poll = async () => {
      const version = pollVersion.current;
      try {
        const [records, sets] = await Promise.all([api.get<{ authorizations: BackgroundAuthorization[] }>('/background/authorizations', controller.signal), api.get<{ tasksets: ArenaTaskset[] }>('/arena/tasksets', controller.signal)]);
        if (controller.signal.aborted) return;
        if (version === pollVersion.current) { setAuthorizations(records.authorizations); setTasksets(sets.tasksets); setSelectedId((id) => id || records.authorizations[0]?.id || ''); setRefresh((old) => old + 1); setPollError(''); }
      } catch (error: unknown) { if (!controller.signal.aborted) setPollError(errorMessage(error)); }
      finally { if (!controller.signal.aborted) { setLoading(false); timer = setTimeout(() => { void poll(); }, 3000); } }
    };
    void poll();
    return () => { controller.abort(); if (timer) clearTimeout(timer); operation.current.generation += 1; operation.current.controller?.abort(); };
  }, [api]);
  const perform = async (action: (signal: AbortSignal, current: () => boolean) => Promise<void>) => {
    operation.current.controller?.abort(); const controller = new AbortController(), generation = ++operation.current.generation;
    operation.current.controller = controller; pollVersion.current += 1;
    const current = () => !controller.signal.aborted && generation === operation.current.generation;
    setBusy(true); setError(''); setNotice('');
    try { await action(controller.signal, current); }
    catch (error: unknown) { if (current()) setError(errorMessage(error)); }
    finally { if (current()) { setBusy(false); operation.current.controller = null; pollVersion.current += 1; } }
  };
  const save = (item: BackgroundAuthorization) => { setAuthorizations((old) => [item, ...old.filter((entry) => entry.id !== item.id)]); setSelectedId(item.id); setShowCreate(false); setRefresh((old) => old + 1); };
  return <div className="background-view"><div className="page-heading"><div><h1>后台调优</h1><p className="muted">限期授权、共享日额度、最多一个批次；先完成用户任务，再执行已批准的调优计划。</p></div><button className="primary" disabled={busy || loading} onClick={() => { setShowCreate(true); setError(''); setNotice(''); }}>新建关闭草稿</button></div>
    {error && <p className="notice error" role="alert">{error}</p>}{pollError && <p className="notice error" role="alert">{pollError} 正在重试读取，当前显示最近记录。</p>}{notice && <p className="notice" role="status">{notice}</p>}{loading && <p className="muted small" role="status">正在读取后台授权…</p>}
    <nav className="evaluation-nav" aria-label="后台授权记录">{authorizations.map((item) => <button key={item.id} disabled={busy} className={selectedId === item.id && !showCreate ? 'selected' : ''} aria-current={selectedId === item.id && !showCreate ? 'page' : undefined} onClick={() => { setSelectedId(item.id); setShowCreate(false); setError(''); setNotice(''); }}><strong>{item.name}</strong><span>{states[item.state]} · {item.date} UTC</span><code>{item.id}</code></button>)}</nav>
    {showCreate ? <BackgroundDraft api={api} tasksets={tasksets} skills={skills} registry={registry} defaultBudget={defaultBudget} busy={busy} onClose={() => setShowCreate(false)} onCreate={(input) => { void perform(async (signal, current) => { const item = await api.post<BackgroundAuthorization>('/background/authorizations', input, signal); if (current()) { save(item); setNotice('关闭草稿已保存。核对冻结条件后，另行明确启用。'); } }); }} /> : selected ? <AuthorizationDetail api={api} item={selected} refresh={refresh} busy={busy} onEvaluation={onEvaluation} onChange={(action, item) => { void perform(async (signal, current) => { const next = await api.post<BackgroundAuthorization>(`/background/authorizations/${encodeURIComponent(item.id)}/${action}`, { expected_revision: item.revision }, signal); if (current()) { save(next); setNotice(action === 'enable' ? '授权已启用，将在有限窗口内检查条件后派发唯一批次。' : '撤销已记录，停止新派发；在途动作仍会结算，未知预留继续保留。'); } }); }} /> : !loading && <p className="empty-copy">尚无后台授权。保存一份关闭草稿，核对固定条件后再明确启用。</p>}
  </div>;
}
