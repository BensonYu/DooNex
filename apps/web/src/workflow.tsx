import { type Run, type Role } from './api';

const roles: Role[] = ['executor', 'reviewer'];
const names = { executor: '执行', reviewer: '只读审查' };
const states: Record<string, string> = {
  pending: '等待开始', queued: '等待开始', running: '进行中', completed: '已完成', yielded: '已安全让出',
  interrupted: '已中断', failed: '失败', budget_exceeded: '预算已用尽', paused: '已暂停', cancelled: '已取消', skipped: '未执行',
};
const money = (value: number | null | undefined) => value == null || !Number.isFinite(value) ? '未知' : `US$${value.toFixed(4)}`;

export function WorkflowProgress({ run }: { run: Run }) {
  const enabled = run.request.workflow === 'execute_review';
  const visibleRoles = enabled ? roles : roles.slice(0, 1);
  const phase = ({ execution: '执行', review: '只读审查', verification: '固定快照检查' })[run.phase ?? 'execution'];
  return <section className="workflow-panel" aria-label="角色与共享预算">
    <div className="section-heading"><h2>{enabled ? '执行 → 只读审查 → 检查' : '执行 → 检查'}</h2><span className="muted small">当前阶段：{phase}</span></div>
    <p className="field-help">{enabled ? '两个角色按固定顺序工作，共用下方累计预算。审查读取执行者的固定快照，不修改代码，也不继续委派。' : '本次仅执行；检查结果仍绑定最终代码快照。'}</p>
    <div className="role-table-wrap"><table className="role-table">
      <thead><tr><th>角色 / 状态</th><th>调用</th><th>已记录 tokens</th><th>估计费用</th></tr></thead>
      <tbody>{visibleRoles.map((role) => {
        const assignment = run.assignments?.[role];
        const usage = assignment?.usage ?? run.role_usage?.[role] ?? (!enabled && role === 'executor' ? run.usage : undefined);
        const legacyState = run.state.startsWith('completed_') || run.state === 'verifying' ? 'completed' : run.state;
        const model = run.request.role_models?.[role] ?? run.request.model;
        const state = assignment?.state ?? (role === 'executor' ? legacyState : 'pending');
        return <tr key={role}>
          <th scope="row"><strong>{names[role]}</strong><span>{states[state] ?? state}</span>{assignment?.id && <small className="path-text">{assignment.id}</small>}</th>
          <td>{usage ? usage.calls : '—'}</td><td>{usage ? usage.tokens.toLocaleString() : '—'}{usage && usage.unknown_calls > 0 && <small>{usage.unknown_calls} 次调用用量未知</small>}</td>
          <td>{usage ? model.billing_mode === 'subscription' ? '包月，金额未知' : money(usage.cost_usd) : '—'}<small>{model.model_id}</small></td>
        </tr>;
      })}</tbody>
    </table></div>
    <p className="field-help path-text">执行输入：起始提交 <code>{run.request.base_commit}</code>{enabled && <>；审查输入：{run.handoff?.snapshot_digest ? <code>{run.handoff.snapshot_digest}</code> : '等待执行快照'}</>}</p>
  </section>;
}

export function ReviewReport({ run }: { run: Run }) {
  if (run.request.workflow !== 'execute_review') return null;
  const review = run.review;
  const finalDigest = typeof run.evidence?.snapshot_digest === 'string' ? run.evidence.snapshot_digest : null;
  const stale = review?.valid === false || run.review_integrity?.valid === false || !!(review && finalDigest && review.snapshot_digest !== finalDigest);
  const errors = [...new Set([...(review?.errors ?? []), ...(run.review_integrity?.errors ?? [])])];
  return <section className="review-panel" aria-label="审查意见">
    <h2>审查意见</h2>
    <p className="field-help">审查意见是独立角色的分析。是否通过验收，以本页固定快照的实际检查结果为准。</p>
    {!review ? <p className="muted small">尚无已完成的审查结论。</p> : <>
      {stale && <div className="notice error" role="status"><strong>审查证据已过期或不完整</strong><p>当前代码或检查快照与审查记录不一致，不能将此结论用于当前交付。</p>{errors.map((error, index) => <p key={index}>{error}</p>)}</div>}
      <p className={review.status === 'approved' && !stale ? 'positive' : 'attention'}><strong>{review.status === 'approved' ? '审查未提出阻断问题' : '审查要求修改'}</strong></p>
      <p className="preserve-text">{review.summary}</p>
      <p className="muted small path-text">审查快照：<code>{review.snapshot_digest}</code></p>
      {finalDigest && <p className="muted small path-text">检查快照：<code>{finalDigest}</code></p>}
      {review.findings.length > 0 && <ol className="review-findings">{review.findings.map((finding, index) => <li key={index}>
        <div><span className={`severity severity-${finding.severity}`}>{({ high: '高', medium: '中', low: '低' })[finding.severity]}</span><code>{finding.path || '总体问题'}{finding.line != null ? `:${finding.line}` : ''}</code>{finding.side === 'before' && <span className="muted small">删除前版本</span>}</div>
        <p className="preserve-text">{finding.message}</p>
      </li>)}</ol>}
    </>}
  </section>;
}
