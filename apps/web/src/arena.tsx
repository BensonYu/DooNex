import { useEffect, useRef, useState } from 'react';
import { Api, type ArenaCounts, type ArenaEvaluation, type ArenaRecommendation, type ArenaRecommendations, type ArenaStability, type ArenaTaskset, type Budget, type ModelProfiles, type RecommendationPosition, type SkillVersion } from './api';
import { formatMoney } from './model-settings';
import { skillReady } from './skills';

const positions: Record<RecommendationPosition, string> = { reliable: '可靠优先', cost: '成本优先', speed: '速度优先' };
const stability: Record<ArenaStability, string> = { all_passed: '全部通过', unstable: '不稳定：重复结果有成有败', all_failed: '全部失败', incomplete: '重复证据不完整' };
const states: Record<string, string> = { queued: '等待执行', running: '正在评测', yielded: '已安全让出', cancelling: '正在取消', complete: '批次已跑完', completed: '批次已跑完', failed: '评测失败', cancelled: '已取消', budget_exceeded: '已达到批次预算', interrupted: '已中断' };
const outcomeNames: Record<string, string> = { passed: '验收通过', pass: '验收通过', failed: '验收失败', failure: '验收失败', environment_failure: '环境故障', cancelled: '取消', assisted: '辅助运行（自主性未证实）', pending: '待完成', running: '执行中', evidence_invalid: '证据无效' };
const reasonNames: Record<string, string> = {
  not_heldout: '不是保留评测集', evaluation_not_complete: '评测尚未结束', batch_incomplete: '批次证据不完整', insufficient_repeats: '独立重复次数不足', cancelled_attempts: '包含取消记录', assisted_attempts: '包含人工介入或自主性无法证明的辅助运行', pending_attempts: '还有未完成尝试', invalid_attempt_records: '尝试记录无效', missing_evidence: '缺少有效证据', safety_violation: '发生权限边界违规', insufficient_valid_attempts: '有效尝试次数不足', task_without_success: '存在从未通过的任务', critical_task_failed: '关键任务未全部通过', below_threshold: '任务等权通过率低于门槛', below_baseline: '通过率低于同轮基线', baseline_ineligible: '同轮基线不满足门槛', duplicate_run_id: '存在重复运行记录', unknown_task: '记录引用了未知任务', unknown_arm: '记录引用了未知候选', invalid_repeat: '重复序号无效', invalid_attempt_fields: '尝试记录字段无效',
};
const activeEvaluation = (state: string) => ['queued', 'running', 'cancelling', 'yielded'].includes(state);
const percentage = (value: number | null) => value == null ? '不可计算' : `${(value * 100).toFixed(1)}%`;
const seconds = (value: number | null) => value == null ? '未知' : `${value.toFixed(1)} 秒`;
const successCost = (value: number | null, passed: number) => passed === 0 ? '不可计算' : formatMoney(value);
const date = (value: string) => new Date(value).toLocaleString('zh-CN', { hour12: false });
const countLabels: [keyof ArenaCounts, string][] = [
  ['planned', '计划'], ['attempts', '已派发'], ['completed', '已完成'], ['valid', '质量有效分母'], ['passed', '通过'], ['failed', '失败'], ['environment_failure', '环境故障'], ['cancelled', '取消'], ['assisted', '辅助运行'], ['pending', '未完成'], ['evidence_invalid', '证据无效'], ['safety_violations', '边界违规'], ['regression_failures', '回归失败'], ['missing_slots', '缺失尝试'],
];

function Counts({ counts }: { counts: ArenaCounts }) {
  return <dl className="arena-counts">{countLabels.map(([key, label]) => <div key={key}><dt>{label}</dt><dd>{counts[key]}</dd></div>)}</dl>;
}
function Reasons({ items }: { items: string[] }) {
  return items.length ? <ul className="metric-reasons">{items.map((item, index) => <li key={index}>{reasonNames[item] || item}</li>)}</ul> : null;
}
function AssistanceReport({ attempt }: { attempt: ArenaEvaluation['attempts'][number] }) {
  const records = attempt.assistance ?? [];
  if (!records.length && attempt.classification !== 'assisted') return null;
  return <div className="assistance-report">
    <p className="field-help">{attempt.classification === 'assisted' ? '本次自主性未证实，列为辅助运行，不计入自主质量通过率分母。' : '本次保留当前验收分类；恢复核查记录不覆盖失败、回归或安全违规。'}</p>
    {records.length > 0 && <details><summary>辅助与自主性核查记录（{records.length}）</summary><ul>{records.map((record, index) => <li key={index}><div><strong>{record.reason}</strong><span>记录于 {date(record.at)}</span><span className="path-text">恢复时快照 <code>{record.snapshot_digest}</code></span></div></li>)}</ul></details>}
  </div>;
}
function BudgetFields({ prefix, title, value, onChange, subscription, batch = false }: { prefix: string; title: string; value: Budget; onChange: (next: Budget) => void; subscription: boolean; batch?: boolean }) {
  return <fieldset className="model-fields"><legend>{title}</legend><div className="budget-inputs">
    <label htmlFor={`${prefix}-calls`}>调用上限<input id={`${prefix}-calls`} type="number" min={1} max={batch ? 10000 : 100} step={1} required value={value.max_calls} onChange={(event) => onChange({ ...value, max_calls: Number(event.target.value) })} /></label>
    <label htmlFor={`${prefix}-tokens`}>Token 上限<input id={`${prefix}-tokens`} type="number" min={1} max={batch ? 100000000 : 10000000} step={1} required value={value.max_tokens} onChange={(event) => onChange({ ...value, max_tokens: Number(event.target.value) })} /></label>
    <label htmlFor={`${prefix}-cost`}>估计费用上限（US$）<input id={`${prefix}-cost`} type="number" min={0} max={batch ? 1000 : 100} step="any" required={!subscription} disabled={subscription} value={subscription ? '' : value.max_cost_usd ?? ''} placeholder={subscription ? '包月，金额未知' : ''} onChange={(event) => { event.currentTarget.setCustomValidity(event.target.value !== '' && Number(event.target.value) <= 0 ? '费用上限必须大于 0。' : ''); onChange({ ...value, max_cost_usd: event.target.value === '' ? null : Number(event.target.value) }); }} /></label>
    <label htmlFor={`${prefix}-seconds`}>时长上限（秒）<input id={`${prefix}-seconds`} type="number" min={1} max={batch ? 86400 : 7200} step={1} required value={value.max_seconds} onChange={(event) => onChange({ ...value, max_seconds: Number(event.target.value) })} /></label>
  </div></fieldset>;
}

function FrozenBudget({ title, value }: { title: string; value?: Budget }) {
  return <div><dt>{title}</dt><dd>{value ? <>{value.max_calls} 次调用 · {value.max_tokens.toLocaleString()} tokens · {formatMoney(value.max_cost_usd)} · {value.max_seconds} 秒</> : '本次记录未提供'}</dd></div>;
}

function EvaluationReport({ evaluation, skills, onOpenRun }: { evaluation: ArenaEvaluation; skills: SkillVersion[]; onOpenRun: (projectId: string, runId: string) => void }) {
  const metrics = evaluation.metrics;
  const armName = (id: string) => metrics.arms.find((arm) => arm.id === id)?.label || id;
  return <section aria-label="本轮评测结果">
    <p className="small path-text">类别 {metrics.category} · {metrics.split === 'heldout' ? '保留评测集' : '调优集'} · 条件指纹 <code>{metrics.fingerprint}</code></p>
    <details className="frozen-evaluation"><summary>本轮冻结的重复次数、候选与预算</summary><dl className="config-list"><div><dt>重复次数</dt><dd>{evaluation.repeats ?? '本次记录未提供'} / 任务 / 组</dd></div><div><dt>候选版本</dt><dd className="path-text">无额外 Skill 基线；{evaluation.skill_version_ids?.join('；') || '请查看下方分组记录'}</dd></div><FrozenBudget title="每次尝试上限" value={evaluation.run_budget} /><FrozenBudget title="批次共享上限" value={evaluation.batch_budget} /></dl></details>
    <Counts counts={metrics.counts} />
    <p className="field-help">质量通过率按各任务等权聚合；通过数 / 有效尝试是原始分母。环境故障、取消和辅助运行单列，不会静默删除。人工介入或自主性无法证明的辅助运行不计入自主质量分母。已观察到的失败、回归与安全违规仍保留，缺失记录与违规仍阻止推荐。</p>
    {!metrics.complete && <p className="notice">本轮尚未满足完整性条件，当前结果不能视为完整评测。</p>}<Reasons items={metrics.issues} />
    <section className="usage-panel arena-summary" aria-label="批次累计用量">
      <div><span>累计调用 / tokens</span><strong>{metrics.usage.calls} / {metrics.usage.tokens.toLocaleString()}</strong></div><div><span>含失败尝试的批次费用</span><strong>{formatMoney(metrics.usage.cost_usd)}</strong></div><div><span>每次成功的模型成本</span><strong>{successCost(metrics.usage.unit_success_cost_usd, metrics.counts.passed)}</strong></div><div><span>成功耗时中位数</span><strong>{seconds(metrics.timing.successful_median_seconds)}</strong></div>
      <p>预留 {metrics.usage.reserved_tokens.toLocaleString()} tokens · {metrics.usage.unknown_calls} 次调用用量未知。金额未知保持未知；零成功的单位成功成本不可计算。有效尝试耗时中位数 {seconds(metrics.timing.valid_median_seconds)}，排队中位数 {seconds(metrics.timing.queued_median_seconds)}。</p>
    </section>
    <div className="table-scroll"><table className="role-table arena-comparison"><caption>无额外 Skill 基线与固定候选对比</caption><thead><tr><th>版本 / 稳定性</th><th>通过 / 有效尝试</th><th>任务等权通过率</th><th>全部成本 / 单位成功成本</th><th>成功 / 有效 / 排队耗时中位数</th><th>调用 / tokens</th></tr></thead><tbody>{metrics.arms.map((arm) => <tr key={arm.id}><th scope="row"><strong>{arm.id === 'baseline' ? '无额外 Skill（基线）' : arm.label}</strong><small>{arm.id}</small><span>{stability[arm.stability]}</span></th><td>{arm.counts.passed} / {arm.counts.valid}<small>计划 {arm.counts.planned} · 失败 {arm.counts.failed}</small></td><td>{percentage(arm.pass_rate)}</td><td>{formatMoney(arm.usage.cost_usd)}<small>{successCost(arm.usage.unit_success_cost_usd, arm.counts.passed)} / 成功</small></td><td>{seconds(arm.timing.successful_median_seconds)}<small>{seconds(arm.timing.valid_median_seconds)} / {seconds(arm.timing.queued_median_seconds)}</small></td><td>{arm.usage.calls} / {arm.usage.tokens.toLocaleString()}<small>未知调用 {arm.usage.unknown_calls}</small></td></tr>)}</tbody></table></div>
    <p className="field-help">横向滚动表格可查看成本、耗时与用量。</p>
    {metrics.arms.map((arm) => {
      const skill = skills.find((item) => item.version_id === arm.id);
      return <details className="arena-arm" key={arm.id}><summary>{arm.id === 'baseline' ? '基线' : arm.label} · 每任务重复记录与推荐资格</summary>
        {skill && <p className="small path-text">Skill 来源 {skill.metadata.source} · 内容摘要 <code>{skill.content_digest}</code></p>}
        <p className={arm.eligibility.eligible ? 'positive small' : 'attention small'}>{arm.eligibility.eligible ? '满足本轮个人试用资格门槛' : '尚不满足本轮个人试用资格门槛'}</p><Reasons items={arm.eligibility.reasons} /><Counts counts={arm.counts} />
        {arm.tasks.map((task) => <section className="arena-task-result" key={task.task_id}><h3>{task.task_id}{task.critical && ' · 关键任务'}</h3><p className="small">{task.counts.passed} / {task.counts.valid} 次有效尝试通过 · {percentage(task.pass_rate)} · {stability[task.stability]} · 缺少 {task.missing_repeats.length} 次有效重复{task.missing_repeats.length > 0 ? `（第 ${task.missing_repeats.map((repeat) => repeat + 1).join('、')} 次）` : ''}</p>
          <ul className="attempt-list">{task.attempts.map((attempt, index) => {
            const record = evaluation.attempts.find((item) => item.run_id === attempt.run_id);
            return <li key={`${attempt.run_id}-${index}`}><div><strong>第 {attempt.repeat + 1} 次 · {outcomeNames[attempt.classification] || attempt.classification}</strong><span>{attempt.evidence_valid ? '证据有效' : '证据无效'}{attempt.safety_violation ? ' · 权限违规' : ''}{attempt.regression_failed ? ' · 回归失败' : ''}</span>{attempt.reported_classification && attempt.reported_classification !== attempt.classification && <span>原记录分类：{outcomeNames[attempt.reported_classification] || attempt.reported_classification}；以上结果以证据核验为准。</span>}<code>{attempt.run_id}</code><span>尝试耗时 {seconds(attempt.duration_seconds ?? null)} · 排队 {seconds(attempt.queued_seconds ?? null)}</span>{attempt.usage && <span>{attempt.usage.calls} 次调用 · {attempt.usage.tokens.toLocaleString()} tokens · 费用 {formatMoney(attempt.usage.cost_usd)} · 预留 {attempt.usage.reserved_tokens.toLocaleString()} tokens · {attempt.usage.unknown_calls} 次用量未知</span>}{record?.reason && <p>{record.reason}</p>}{record && <AssistanceReport attempt={record} />}</div>{record && <button className="compact" onClick={() => onOpenRun(record.project_id, record.run_id)}>查看运行证据</button>}</li>;
          })}</ul>
        </section>)}
      </details>;
    })}
    <details className="arena-arm"><summary>全部已派发尝试（{evaluation.attempts.length}，含未完成与异常分类）</summary><ul className="attempt-list">{evaluation.attempts.map((attempt, index) => <li key={`${attempt.run_id}-${index}`}><div><strong>{armName(attempt.arm_id)} · {attempt.task_id} · 第 {attempt.repeat + 1} 次</strong><span>{outcomeNames[attempt.classification] || attempt.classification} · {states[attempt.state] || attempt.state}{attempt.safety_violation ? ' · 权限违规' : ''}{attempt.regression_failed ? ' · 回归失败' : ''}</span><code>{attempt.run_id}</code>{attempt.reason && <p>{attempt.reason}</p>}<AssistanceReport attempt={attempt} /></div><button className="compact" onClick={() => onOpenRun(attempt.project_id, attempt.run_id)}>查看运行证据</button></li>)}</ul></details>
    <details className="arena-arm"><summary>本轮冻结模型与原始指标</summary><dl className="config-list"><div><dt>模型 / 接口</dt><dd>{evaluation.model.model_id} · {evaluation.model.provider} · {evaluation.model.api_mode}</dd></div><div><dt>输入 / 输出上限</dt><dd>{evaluation.model.max_input_tokens} / {evaluation.model.max_output_tokens} tokens</dd></div><div><dt>计费</dt><dd>{evaluation.model.billing_mode === 'subscription' ? '包月，货币金额未知' : `输入 ${formatMoney(evaluation.model.input_price_per_million)} / 输出 ${formatMoney(evaluation.model.output_price_per_million)}（每百万 tokens）`}</dd></div><div><dt>价格来源</dt><dd>{evaluation.model.price_source || '未知'}</dd></div></dl><pre className="raw-metrics">{JSON.stringify(metrics, null, 2)}</pre></details>
  </section>;
}

export function ArenaView({ api, skills, registry, defaultBudget, onOpenRun, initialEvaluationId = '' }: { api: Api; skills: SkillVersion[]; registry: ModelProfiles | null; defaultBudget: Budget; onOpenRun: (projectId: string, runId: string) => void; initialEvaluationId?: string }) {
  const [tasksets, setTasksets] = useState<ArenaTaskset[]>([]);
  const [evaluations, setEvaluations] = useState<ArenaEvaluation[]>([]);
  const [recommendations, setRecommendations] = useState<ArenaRecommendations>({ current: [], history: [] });
  const [selectedId, setSelectedId] = useState('');
  const [detail, setDetail] = useState<ArenaEvaluation | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const [tasksetId, setTasksetId] = useState('');
  const [tasksetPath, setTasksetPath] = useState('');
  const [candidates, setCandidates] = useState<string[]>([]);
  const [modelId, setModelId] = useState(registry?.default_profile_id ?? registry?.profiles[0]?.id ?? '');
  const [repeats, setRepeats] = useState(3);
  const [runBudget, setRunBudget] = useState(defaultBudget);
  const [batchBudget, setBatchBudget] = useState<Budget>({ max_calls: 200, max_tokens: 1000000, max_cost_usd: 10, max_seconds: 14400 });
  const [choices, setChoices] = useState<Partial<Record<RecommendationPosition, string>>>({});
  const [busy, setBusy] = useState('');
  const [loading, setLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);
  const [refreshEpoch, setRefreshEpoch] = useState(0);
  const [error, setError] = useState('');
  const [pollError, setPollError] = useState('');
  const [notice, setNotice] = useState('');
  const [resumeAcknowledge, setResumeAcknowledge] = useState(false);
  const operation = useRef({ generation: 0, controller: null as AbortController | null });
  const selectedRef = useRef('');
  const model = registry?.profiles.find((profile) => profile.id === modelId);
  const taskset = tasksets.find((item) => item.id === tasksetId);
  const subscription = model?.model.billing_mode === 'subscription';
  const hasPrice = subscription || !!model?.model.price_source && model.model.input_price_per_million != null && model.model.output_price_per_million != null;
  const eligibleSkills = skills.filter(skillReady);
  const validCandidates = candidates.length >= 2 && candidates.length <= 3 && candidates.every((id) => eligibleSkills.some((skill) => skill.version_id === id));
  const canStart = !!taskset?.valid && taskset.task_count >= 5 && validCandidates && !!model?.credential_available && hasPrice && !busy;
  const perform = async (kind: string, action: (signal: AbortSignal, current: () => boolean) => Promise<void>) => {
    operation.current.controller?.abort();
    const controller = new AbortController(), generation = ++operation.current.generation;
    operation.current.controller = controller;
    const current = () => !controller.signal.aborted && operation.current.generation === generation;
    setBusy(kind); setError(''); setNotice('');
    try { await action(controller.signal, current); }
    catch (error) { if (current()) setError(error instanceof Error ? error.message : String(error)); }
    finally { if (current()) { setBusy(''); operation.current.controller = null; } }
  };
  const choose = (id: string) => {
    operation.current.generation += 1; operation.current.controller?.abort(); setBusy(''); setError(''); setNotice(''); setChoices({}); setResumeAcknowledge(false); setShowCreate(false); selectedRef.current = id; setSelectedId(id); setRefreshEpoch((old) => old + 1);
  };
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    void Promise.all([api.get<{ tasksets: ArenaTaskset[] }>('/arena/tasksets', controller.signal), api.get<{ evaluations: ArenaEvaluation[] }>('/arena/evaluations', controller.signal), api.get<ArenaRecommendations>('/arena/recommendations', controller.signal)]).then(([sets, batches, recs]) => {
      if (controller.signal.aborted) return;
      setTasksets(sets.tasksets); setTasksetId(sets.tasksets[0]?.id ?? ''); setEvaluations(batches.evaluations); setRecommendations(recs);
      const first = batches.evaluations.some((item) => item.id === initialEvaluationId) ? initialEvaluationId : batches.evaluations[0]?.id ?? ''; selectedRef.current = first; setSelectedId(first); setShowCreate(!first);
    }).catch((error: unknown) => { if (!controller.signal.aborted) setError(error instanceof Error ? error.message : String(error)); }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => { controller.abort(); operation.current.generation += 1; operation.current.controller?.abort(); };
  }, [api, initialEvaluationId]);
  useEffect(() => {
    const controller = new AbortController(); let timer: ReturnType<typeof setTimeout> | undefined;
    setDetail(null); setPollError(''); setChoices({});
    if (!selectedId) return () => controller.abort();
    setDetailLoading(true);
    const poll = async () => {
      try {
        const result = await api.get<ArenaEvaluation>(`/arena/evaluations/${encodeURIComponent(selectedId)}`, controller.signal);
        if (controller.signal.aborted || selectedRef.current !== selectedId || result.id !== selectedId) return;
        setDetail(result); setPollError(''); setEvaluations((old) => old.map((item) => item.id === result.id ? result : item));
        if (activeEvaluation(result.state)) timer = setTimeout(() => { void poll(); }, 2000);
      } catch (error) {
        if (!controller.signal.aborted && selectedRef.current === selectedId) { setPollError(error instanceof Error ? error.message : String(error)); timer = setTimeout(() => { void poll(); }, 4000); }
      } finally { if (!controller.signal.aborted && selectedRef.current === selectedId) setDetailLoading(false); }
    };
    void poll();
    return () => { controller.abort(); if (timer) clearTimeout(timer); };
  }, [api, selectedId, refreshEpoch]);
  const refreshRecommendations = async (signal: AbortSignal, current: () => boolean) => {
    const result = await api.get<ArenaRecommendations>('/arena/recommendations', signal);
    if (current()) setRecommendations(result);
  };
  const armName = (id: string) => detail?.metrics.arms.find((arm) => arm.id === id)?.label || id;
  const sameScope = (record: ArenaRecommendation) => record.fingerprint === detail?.metrics.fingerprint;

  return <div className="arena-view">
    <div className="page-heading"><div><h1>Skill Arena</h1><p className="muted">手动固定任务、模型和预算，比较基线与 2–3 个 Skill 版本。</p></div><button className="primary" onClick={() => { setShowCreate(true); setError(''); setNotice(''); }}>新建评测</button></div>
    {error && <p className="notice error" role="alert">{error}</p>}{notice && <p className="notice" role="status">{notice}</p>}{loading && <p className="muted" role="status">正在读取任务集与评测记录…</p>}
    <details className="import-panel"><summary>导入本地任务集</summary><p className="field-help">填写包含 taskset.json 的本机目录。任务输入、起始提交、检查器与环境由服务端冻结；这里仅展示名称、类别和关键任务标记，不显示保留测试内容。</p><form className="inline-form" onSubmit={(event) => { event.preventDefault(); void perform('import', async (signal, current) => {
      const record = await api.post<ArenaTaskset>('/arena/tasksets/import', { path: tasksetPath.trim() }, signal);
      if (!current()) return;
      setTasksets((old) => [...old.filter((item) => item.id !== record.id), record]); setTasksetId(record.id); setNotice('任务集已导入并固定。请检查有效性和任务数量，再手动启动评测。');
    }); }}><div className="grow"><label htmlFor="arena-taskset-path">本机任务集目录</label><input id="arena-taskset-path" value={tasksetPath} onChange={(event) => setTasksetPath(event.target.value)} required placeholder="/本机路径/taskset" spellCheck={false} autoComplete="off" /></div><button type="submit" disabled={!!busy}>导入任务集</button></form></details>
    <nav className="evaluation-nav" aria-label="评测记录">{evaluations.map((item) => <button key={item.id} className={selectedId === item.id && !showCreate ? 'selected' : ''} aria-current={selectedId === item.id && !showCreate ? 'page' : undefined} onClick={() => choose(item.id)}><strong>{tasksets.find((set) => set.id === item.taskset_id)?.name || item.taskset_id}</strong><span>{states[item.state] || item.state} · {item.metrics.counts.completed} / {item.metrics.planned_attempts}</span><code>{item.id}</code></button>)}</nav>
    {showCreate ? <form className="arena-create" onSubmit={(event) => { event.preventDefault(); if (!canStart) return; void perform('create', async (signal, current) => {
      const result = await api.post<ArenaEvaluation>('/arena/evaluations', { taskset_id: tasksetId, skill_version_ids: candidates, model_profile_id: modelId, repeats, run_budget: { ...runBudget, max_cost_usd: subscription ? null : runBudget.max_cost_usd }, batch_budget: { ...batchBudget, max_cost_usd: subscription ? null : batchBudget.max_cost_usd } }, signal);
      if (!current()) return;
      setEvaluations((old) => [result, ...old.filter((item) => item.id !== result.id)]); choose(result.id);
    }); }}>
      <div className="section-heading"><h2>固定本轮比较条件</h2>{selectedId && <button className="quiet compact" type="button" onClick={() => setShowCreate(false)}>返回评测</button>}</div>
      <label htmlFor="arena-taskset">任务集版本</label><select id="arena-taskset" value={tasksetId} onChange={(event) => setTasksetId(event.target.value)} required><option value="" disabled>选择已导入任务集</option>{tasksets.map((set) => <option key={set.id} value={set.id}>{set.name} · {set.category} · {set.split === 'heldout' ? '保留评测集' : '调优集'} · {set.task_count} 任务{!set.valid ? ' · 无效' : ''}</option>)}</select>
      {taskset && <><p className="small path-text">版本 <code>{taskset.id}</code> · {taskset.valid ? '完整性有效' : '完整性失败'} · 创建于 {date(taskset.created_at)}</p>{taskset.errors.length > 0 && <ul className="warning-text small">{taskset.errors.map((item, i) => <li key={i}>{item}</li>)}</ul>}<ul className="taskset-titles">{taskset.tasks.map((task) => <li key={task.id}>{task.title} <code>{task.id}</code>{task.critical && <strong> · 关键任务</strong>}</li>)}</ul>{taskset.task_count < 5 && <p className="warning-text small">至少需要 5 个可验收任务才能启动。</p>}</>}
      <fieldset className="model-fields"><legend>候选 Skill 版本（选择 2–3 个）</legend><p className="field-help">无额外 Skill 的基线始终包含在本轮中。只允许已批准且内容完整的候选，所有组使用同一模型与预算。</p>{!eligibleSkills.length && <p className="warning-text small">暂无可用版本，请先在 Skills 页面导入并审核。</p>}{eligibleSkills.map((skill) => <label className="checkbox-label" key={skill.version_id}><input type="checkbox" checked={candidates.includes(skill.version_id)} disabled={!candidates.includes(skill.version_id) && candidates.length >= 3} onChange={(event) => setCandidates((old) => event.target.checked ? [...old, skill.version_id] : old.filter((id) => id !== skill.version_id))} /><span>{skill.metadata.source}<br /><code>{skill.version_id}</code><br /><small>内容摘要 {skill.content_digest}</small></span></label>)}</fieldset>
      <div className="settings-grid"><div><label htmlFor="arena-model">固定模型配置</label><select id="arena-model" value={modelId} onChange={(event) => setModelId(event.target.value)} required><option value="" disabled>选择模型</option>{registry?.profiles.map((profile) => <option key={profile.id} value={profile.id}>{profile.name} · {profile.model.model_id}</option>)}</select>{model && <p className="field-help path-text">{model.model.provider} · {model.model.api_mode} · {model.credential_available ? '凭据引用已就绪' : '凭据引用未就绪'}<br />配置版本 {model.revision}</p>}</div><div><label htmlFor="arena-repeats">每任务独立重复次数</label><input id="arena-repeats" type="number" min={1} max={10} step={1} required value={repeats} onChange={(event) => setRepeats(Number(event.target.value))} /></div></div>
      <p className="field-help">计划 {taskset?.task_count ?? 0} × {candidates.length + 1} 组 × {repeats} 次 = {(taskset?.task_count ?? 0) * (candidates.length + 1) * repeats} 次尝试。每次从干净工作区开始；不提供额外人工提示。调优集或不足三次有效重复不会产生可推广推荐。</p>
      {!hasPrice && <p className="warning-text small">按量模型需要已记录来源的输入和输出价格。</p>}
      <BudgetFields prefix="arena-run" title="每次尝试的预算" value={runBudget} onChange={setRunBudget} subscription={subscription} />
      <BudgetFields prefix="arena-batch" title="整个批次的共享上限" value={batchBudget} onChange={setBatchBudget} subscription={subscription} batch />
      <p className="field-help">批次上限覆盖基线、候选、失败与未知预留；达到上限后停止后续派发。包月的金额仍为未知，并限制调用、token 和时长。</p>
      <div className="form-footer"><span className="muted small">点击后会按以上配置消耗已授权的模型用量。</span><button className="primary" type="submit" disabled={!canStart}>{busy === 'create' ? '正在创建…' : '手动启动评测'}</button></div>
    </form> : detail ? <div className="evaluation-detail">
      <div className="section-heading"><div><h2>{tasksets.find((set) => set.id === detail.taskset_id)?.name || detail.taskset_id}</h2><p className="small path-text"><code>{detail.id}</code> · {states[detail.state] || detail.state}</p></div>{activeEvaluation(detail.state) && <button className="danger compact" disabled={!!busy || detail.state === 'cancelling'} onClick={() => { void perform('cancel', async (signal, current) => {
        await api.post(`/arena/evaluations/${encodeURIComponent(detail.id)}/cancel`, {}, signal);
        const next = await api.get<ArenaEvaluation>(`/arena/evaluations/${encodeURIComponent(detail.id)}`, signal);
        if (current() && selectedRef.current === next.id) { setDetail(next); setEvaluations((old) => old.map((item) => item.id === next.id ? next : item)); setRefreshEpoch((old) => old + 1); setNotice('取消请求已记录；等待在途动作结束或超时后确认停止。'); }
      }); }}>取消本轮评测</button>}</div>
      {detail.reason && <p className="notice">{detail.reason}</p>}{pollError && <p className="notice error" role="alert">{pollError} 当前显示最近一次记录，将自动重试读取。</p>}
      {detail.state === 'interrupted' && <section className="recovery-panel" aria-label="中断评测恢复核查"><h3>核对后恢复本轮</h3><p>先查看中断或暂停尝试的运行证据，核对工作区、残留容器和结果未知的外部动作。服务端会重新检查基线、Skill、任务集与恢复条件；原预算、首次启动时间和未知预留继续累计。</p><ul className="attempt-list">{detail.attempts.filter((attempt) => ['interrupted', 'paused'].includes(attempt.state)).map((attempt) => <li key={attempt.run_id}><div><strong>{attempt.task_id} · 第 {attempt.repeat + 1} 次</strong><code>{attempt.run_id}</code></div><button className="compact" onClick={() => onOpenRun(attempt.project_id, attempt.run_id)}>查看运行证据</button></li>)}</ul><label className="checkbox-label"><input type="checkbox" checked={resumeAcknowledge} onChange={(event) => setResumeAcknowledge(event.target.checked)} />我已核对残留容器、工作区及结果未知的外部动作，确认可以恢复本轮。</label><button className="primary" disabled={!!busy || !resumeAcknowledge} onClick={() => { void perform('resume', async (signal, current) => {
        await api.post(`/arena/evaluations/${encodeURIComponent(detail.id)}/resume`, { acknowledge_unknown: true }, signal);
        const result = await api.get<ArenaEvaluation>(`/arena/evaluations/${encodeURIComponent(detail.id)}`, signal);
        if (current() && selectedRef.current === result.id) { setDetail(result); setEvaluations((old) => old.map((item) => item.id === result.id ? result : item)); setRefreshEpoch((old) => old + 1); setResumeAcknowledge(false); setNotice('恢复请求已记录，继续沿用原批次预算与已有证据。'); }
      }); }}>确认核查并恢复评测</button></section>}
      <EvaluationReport evaluation={detail} skills={skills} onOpenRun={onOpenRun} />
      <section className="recommendation-section" aria-label="三个场景推荐"><h2>本轮场景推荐</h2><p className="field-help">结果只适用于上述任务类别、模型和条件指纹。允许空缺与并列，没有综合分冠军；查看证据后再明确选用。成本或时长未知时，相应位置可保持空缺。</p>
        {(Object.entries(positions) as [RecommendationPosition, string][]).map(([position, name]) => {
          const ids = detail.metrics.recommendations[position];
          const current = recommendations.current.find((item) => sameScope(item) && item.position === position);
          const choice = choices[position] ?? '';
          return <div className="recommendation-row" key={position}><h3>{name}</h3><p className="small">当前选用：{current ? <>{armName(current.arm_id)} <code>{current.arm_id}</code> · {date(current.created_at)}</> : '空缺'}</p>{ids.length ? <><p className="small">本轮合格候选{ids.length > 1 ? '（并列保留）' : ''}：{ids.map(armName).join('、')}</p><div className="inline-form"><div className="grow"><label htmlFor={`recommend-${position}`}>手动选择{name}版本</label><select id={`recommend-${position}`} value={choice} onChange={(event) => setChoices((old) => ({ ...old, [position]: event.target.value }))}><option value="">选择后再确认选用</option>{ids.map((id) => <option key={id} value={id}>{armName(id)} · {id}</option>)}</select></div><button disabled={!!busy || !choice || !ids.includes(choice)} onClick={() => { void perform('recommend', async (signal, current) => {
            await api.post('/arena/recommendations', { evaluation_id: detail.id, position, arm_id: choice }, signal);
            await refreshRecommendations(signal, current);
            if (current()) { setChoices({}); setNotice(`${name}推荐已明确选用。历史评测和运行内容保持冻结。`); }
          }); }}>确认选用{name}</button></div></> : <p className="muted small">本轮没有符合此位置条件的候选，保持空缺。</p>}</div>;
        })}
        <details className="arena-arm"><summary>同条件推荐历史与回滚</summary><p className="field-help">回滚前会重新核对资格，只变更后续可选推荐，不改写历史成绩或正在运行的版本。</p>{recommendations.history.filter(sameScope).length === 0 ? <p className="muted small">尚无同条件历史推荐。</p> : <ul className="attempt-list">{recommendations.history.filter(sameScope).map((record) => <li key={record.id}><div><strong>{positions[record.position]} · {armName(record.arm_id)}</strong><code>{record.id}</code><span>{date(record.created_at)} · 评测 {record.evaluation_id}</span></div><button className="compact" disabled={!!busy || recommendations.current.some((item) => item.id === record.id)} onClick={() => { void perform('rollback', async (signal, current) => {
          await api.post('/arena/recommendations/rollback', { recommendation_id: record.id }, signal);
          await refreshRecommendations(signal, current);
          if (current()) setNotice('已记录推荐回滚，历史评测和运行内容保持冻结。');
        }); }}>回滚到此推荐</button></li>)}</ul>}</details>
      </section>
    </div> : !loading && <p className="empty-copy">{detailLoading ? '正在读取评测…' : '还没有评测记录。导入任务集与候选版本后，手动创建一轮。'}{pollError && <span role="alert">{pollError}</span>}</p>}
  </div>;
}
