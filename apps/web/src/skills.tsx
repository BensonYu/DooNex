import { useEffect, useRef, useState } from 'react';
import { Api, type ArenaEvaluation, type ArenaRecommendation, type ModelConfig, type RecommendationPosition, type Run, type SkillMetadata, type SkillVersion } from './api';

export const skillReady = (skill: SkillVersion) => skill.review_status === 'approved' && skill.integrity.valid;
const reviewNames = { pending: '待审核', approved: '已批准', rejected: '已拒绝' };
const licenseNames: Record<SkillMetadata['license_status'], string> = { unknown: '许可未知', declared: '已声明许可', verified: '许可已核对', restricted: '限制使用' };
const lines = (text: string) => text.split('\n').map((item) => item.trim()).filter(Boolean);
const when = (value: string) => new Date(value).toLocaleString('zh-CN', { hour12: false });
const recommendationPositions: Record<RecommendationPosition, string> = { reliable: '可靠优先', cost: '成本优先', speed: '速度优先' };
const modelIdentity = (model: ModelConfig) => JSON.stringify(Object.entries(model).sort(([left], [right]) => left.localeCompare(right)));
export const sameModelConfig = (left: ModelConfig, right: ModelConfig) => modelIdentity(left) === modelIdentity(right);

export function RecommendationChoice({ recommendations, value, loading, error, onChange, onRefresh }: {
  recommendations: ArenaRecommendation[]; value: string; loading: boolean; error: string;
  onChange: (recommendation: ArenaRecommendation | null) => void; onRefresh: () => void;
}) {
  const selected = recommendations.find((item) => item.id === value);
  return <section className="section-block" aria-label="执行推荐选择">
    <div className="section-heading"><h2>场景推荐</h2><button type="button" className="quiet compact" disabled={loading} onClick={onRefresh}>刷新当前推荐</button></div>
    <label htmlFor="run-recommendation">使用已选用推荐</label>
    <select id="run-recommendation" value={value} disabled={loading} onChange={(event) => onChange(recommendations.find((item) => item.id === event.target.value) ?? null)}>
      <option value="">自行选择 Skill</option>
      {value && !selected && <option value={value} disabled>已选推荐当前不可用，请重新选择</option>}
      {recommendations.map((item) => <option value={item.id} key={item.id}>{item.category} · {recommendationPositions[item.position]} · {item.arm_id}</option>)}
    </select>
    <p className="field-help">仅显示与本次执行模型全部配置一致、版本可用的当前推荐。请按任务类别明确选择；系统不推断任务类别，也不自动应用推荐。</p>
    {loading && <p className="muted small" role="status">正在读取当前推荐…</p>}
    {error && <p className="warning-text small" role="alert">{error}</p>}
    {!loading && !error && !recommendations.length && <p className="muted small">当前执行模型没有可选用的推荐。</p>}
    {selected && <p className="small path-text">已选：{selected.category} · {recommendationPositions[selected.position]} · 版本 <code>{selected.arm_id}</code>；来源评测 <code>{selected.evaluation_id}</code>。新运行将冻结此推荐记录。</p>}
    {value && !selected && <p className="warning-text small">推荐或执行条件已变化，请刷新后重新选择，或手动选择 Skill。</p>}
  </section>;
}

export function SkillMetadataReport({ metadata }: { metadata: SkillMetadata }) {
  return <dl className="config-list">
    <div><dt>来源</dt><dd className="path-text">{metadata.source}</dd></div>
    <div><dt>来源提交</dt><dd><code>{metadata.commit || '未提供，使用内容摘要识别版本'}</code></dd></div>
    <div><dt>许可证状态</dt><dd>{licenseNames[metadata.license_status]}</dd></div>
    <div><dt>许可记录</dt><dd className="preserve-text">{metadata.license_text || '未知；不会由系统推定授权'}</dd></div>
    <div><dt>适用范围</dt><dd>{metadata.scope.join('、') || '未声明'}</dd></div>
    <div><dt>声明权限</dt><dd>{metadata.permissions.join('、') || '未声明额外权限'}</dd></div>
  </dl>;
}

export function SkillChoice({ skills, value, onChange, onManage }: {
  skills: SkillVersion[]; value: string; onChange: (id: string) => void; onManage: () => void;
}) {
  const selected = skills.find((skill) => skill.version_id === value);
  return <section className="section-block" aria-label="执行 Skill 版本选择">
    <div className="section-heading"><h2>执行 Skill</h2><button type="button" className="quiet compact" onClick={onManage}>管理 Skills</button></div>
    <label htmlFor="run-skill-version">本次额外 Skill 版本</label>
    <select id="run-skill-version" value={value} onChange={(event) => onChange(event.target.value)}>
      <option value="">无额外 Skill（基线）</option>
      {value && (!selected || !skillReady(selected)) && <option value={value} disabled>已选版本不可用：{value}</option>}
      {skills.filter(skillReady).map((skill) => <option key={skill.version_id} value={skill.version_id}>{skill.metadata.source} · {skill.version_id}</option>)}
    </select>
    <p className="field-help">只对执行角色加载已批准、内容完整的不可变版本。推荐不会自动替换本次选择；不额外授权脚本、依赖或工具权限。</p>
    {selected && <><p className="small path-text">内容摘要：<code>{selected.content_digest}</code> · {reviewNames[selected.review_status]} · {selected.integrity.valid ? '内容完整' : '内容校验失败'}</p><SkillMetadataReport metadata={selected.metadata} /></>}
    {value && (!selected || !skillReady(selected)) && <p className="warning-text small">此版本不能用于新任务，请重新选择。</p>}
  </section>;
}

export function FrozenSkill({ run }: { run: Run }) {
  const skill = run.request.skill;
  const recommendation = run.request.recommendation;
  return <details className="frozen-skill"><summary>本次冻结的 Skill 版本</summary>
    {skill ? <><dl className="config-list"><div><dt>版本</dt><dd><code>{skill.version_id}</code></dd></div><div><dt>内容摘要</dt><dd><code>{skill.content_digest}</code></dd></div><div><dt>检查报告摘要</dt><dd><code>{skill.report_sha256}</code></dd></div></dl><SkillMetadataReport metadata={skill.metadata} /><p className="field-help">这份记录随本次运行固定，后续审核、选用或回滚不会改写它。</p></> : <p className="muted small">无额外 Skill（基线）。项目共同规范仍然适用。</p>}
    {recommendation && <section aria-label="本次冻结的推荐来源"><h3>推荐来源</h3><dl className="config-list"><div><dt>任务类别 / 推荐位置</dt><dd>{recommendation.category} · {recommendationPositions[recommendation.position]}</dd></div><div><dt>推荐记录</dt><dd><code>{recommendation.id}</code></dd></div><div><dt>来源评测</dt><dd><code>{recommendation.evaluation_id}</code></dd></div><div><dt>版本</dt><dd><code>{recommendation.arm_id}</code></dd></div><div><dt>推荐记录时间</dt><dd>{when(recommendation.created_at)}</dd></div><div><dt>执行条件摘要</dt><dd><code>{recommendation.fingerprint}</code></dd></div></dl></section>}
  </details>;
}

export function SkillsView({ api, skills, onSkills, onEvaluation }: { api: Api; skills: SkillVersion[]; onSkills: (next: SkillVersion[]) => void; onEvaluation: (id: string) => void }) {
  const [selectedId, setSelectedId] = useState(skills[0]?.version_id ?? '');
  const [detail, setDetail] = useState<SkillVersion | null>(null);
  const [path, setPath] = useState('');
  const [source, setSource] = useState('local');
  const [license, setLicense] = useState<SkillMetadata['license_status']>('unknown');
  const [licenseText, setLicenseText] = useState('');
  const [commit, setCommit] = useState('');
  const [scope, setScope] = useState('');
  const [permissions, setPermissions] = useState('');
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [history, setHistory] = useState<ArenaEvaluation[] | null>(null);
  const operation = useRef({ generation: 0, controller: null as AbortController | null });
  const perform = async (kind: string, action: (signal: AbortSignal, current: () => boolean) => Promise<void>) => {
    operation.current.controller?.abort();
    const controller = new AbortController(), generation = ++operation.current.generation;
    operation.current.controller = controller;
    const current = () => !controller.signal.aborted && operation.current.generation === generation;
    setBusy(kind); setError(''); if (kind !== 'detail') setNotice('');
    try { await action(controller.signal, current); }
    catch (error) { if (current()) setError(error instanceof Error ? error.message : String(error)); }
    finally { if (current()) { setBusy(''); operation.current.controller = null; } }
  };
  useEffect(() => {
    setDetail(null); setNote(''); setHistory(null);
    if (selectedId) void perform('detail', async (signal, current) => {
      const result = await api.get<SkillVersion>(`/skills/${encodeURIComponent(selectedId)}`, signal);
      if (current() && result.version_id === selectedId) setDetail(result);
    });
    return () => { operation.current.generation += 1; operation.current.controller?.abort(); };
  }, [api, selectedId]);
  const store = (record: SkillVersion) => onSkills([...skills.filter((skill) => skill.version_id !== record.version_id), record]);

  return <div className="skills-view">
    <div className="page-heading"><div><h1>Skills</h1><p className="muted">导入本地内容，核对来源、许可与权限，再批准具体版本。</p></div><button disabled={!!busy} onClick={() => { void perform('refresh', async (signal, current) => {
      const result = await api.get<{ skills: SkillVersion[] }>('/skills', signal);
      if (!current()) return;
      onSkills(result.skills); setSelectedId((old) => result.skills.some((skill) => skill.version_id === old) ? old : result.skills[0]?.version_id ?? ''); setDetail(null);
      if (selectedId && result.skills.some((skill) => skill.version_id === selectedId)) {
        const next = await api.get<SkillVersion>(`/skills/${encodeURIComponent(selectedId)}`, signal);
        if (current()) setDetail(next);
      }
    }); }}>刷新版本</button></div>
    {error && <p className="notice error" role="alert">{error}</p>}{notice && <p className="notice" role="status">{notice}</p>}
    <details className="import-panel"><summary>导入本地 Skill</summary>
      <p className="field-help">服务端读取本机目录并保存不可变版本；导入不会运行其中的脚本。内容改变后再次导入会产生新版本。</p>
      <form onSubmit={(event) => { event.preventDefault(); void perform('import', async (signal, current) => {
        const metadata: SkillMetadata = { source: source.trim() || 'local', license_status: license, license_text: licenseText.trim() || null, commit: commit.trim() || null, scope: lines(scope), permissions: lines(permissions) };
        const record = await api.post<SkillVersion>('/skills/import', { path: path.trim(), metadata }, signal);
        if (!current()) return;
        store(record); setSelectedId(record.version_id); setDetail(record); setNotice('已导入固定版本，审核通过前不能用于新任务。');
      }); }}>
        <label htmlFor="skill-import-path">本机 Skill 目录</label><input id="skill-import-path" value={path} onChange={(event) => setPath(event.target.value)} required placeholder="/本机路径/skill" autoComplete="off" spellCheck={false} />
        <div className="settings-grid"><div><label htmlFor="skill-source">来源记录</label><input id="skill-source" value={source} onChange={(event) => setSource(event.target.value)} required /></div><div><label htmlFor="skill-commit">来源提交（可选）</label><input id="skill-commit" value={commit} onChange={(event) => setCommit(event.target.value)} pattern="([a-fA-F0-9]{40}|[a-fA-F0-9]{64})" placeholder="40 或 64 位提交摘要" spellCheck={false} /></div></div>
        <label htmlFor="skill-license">许可证状态</label><select id="skill-license" value={license} onChange={(event) => setLicense(event.target.value as SkillMetadata['license_status'])}>{Object.entries(licenseNames).map(([id, name]) => <option key={id} value={id}>{name}</option>)}</select>
        <label htmlFor="skill-license-text">许可记录{license === 'unknown' ? '（可选）' : ''}</label><textarea id="skill-license-text" value={licenseText} onChange={(event) => setLicenseText(event.target.value)} required={license !== 'unknown'} placeholder="记录许可文本、授权依据或限制；不由系统代选许可证" />
        <div className="settings-grid"><div><label htmlFor="skill-scope">适用范围（每行一项）</label><textarea id="skill-scope" value={scope} onChange={(event) => setScope(event.target.value)} /></div><div><label htmlFor="skill-permissions">声明权限（每行一项）</label><textarea id="skill-permissions" value={permissions} onChange={(event) => setPermissions(event.target.value)} /></div></div>
        <div className="form-footer"><span className="muted small">声明权限不会扩展执行环境的授权。</span><button className="primary" disabled={!!busy} type="submit">{busy === 'import' ? '正在导入…' : '导入固定版本'}</button></div>
      </form>
    </details>
    <div className="profiles-layout skill-layout"><nav className="profiles-list" aria-label="Skill 版本">
      <h2>版本记录</h2>{!skills.length && <p className="muted small">尚未导入 Skill。</p>}
      {skills.map((skill) => <button className={`profile-item ${skill.version_id === selectedId ? 'selected' : ''}`} key={skill.version_id} aria-current={skill.version_id === selectedId ? 'page' : undefined} onClick={() => setSelectedId(skill.version_id)}><strong>{skill.metadata.source}</strong><code>{skill.version_id}</code><small>{reviewNames[skill.review_status]} · {skill.integrity.valid ? '内容完整' : '校验失败'}</small></button>)}
    </nav><section className="skill-detail" aria-label="Skill 版本详情">
      {busy === 'detail' && <p className="muted" role="status">正在读取版本…</p>}
      {detail && <><h2>固定版本 <code>{detail.version_id}</code></h2><p className="small path-text">内容摘要 <code>{detail.content_digest}</code><br />导入于 {when(detail.created_at)} · {reviewNames[detail.review_status]}</p><SkillMetadataReport metadata={detail.metadata} />
        {!detail.integrity.valid && <div className="notice error" role="alert"><strong>内容完整性检查失败</strong><ul>{detail.integrity.errors.map((item, i) => <li key={i}>{item}</li>)}</ul></div>}
        <details className="skill-content"><summary>查看 Skill 指令原文</summary><pre>{detail.instructions ?? '本次记录未提供指令原文。'}</pre></details>
        <details className="skill-content"><summary>查看文件清单与摘要（{detail.files.length}）</summary><ul className="file-manifest">{detail.files.map((file) => <li key={file.path}><strong>{file.path}</strong><span>{file.size.toLocaleString()} bytes · mode {file.mode}</span><code>{file.sha256}</code></li>)}</ul></details>
        <section className="skill-content" aria-label="此版本的历史评测"><div className="section-heading"><h3>此版本的历史评测</h3><button className="compact" disabled={!!busy} onClick={() => { void perform('history', async (signal, current) => {
          const result = await api.get<{ evaluations: ArenaEvaluation[] }>('/arena/evaluations', signal);
          if (current()) setHistory(result.evaluations.filter((evaluation) => evaluation.metrics.arms.some((arm) => arm.id === detail.version_id)));
        }); }}>查询评测记录</button></div><p className="field-help">记录只属于这个固定版本；不同模型、任务类别与条件不合并成总分。</p>{history && (history.length ? <ul className="attempt-list">{history.map((evaluation) => {
          const arm = evaluation.metrics.arms.find((item) => item.id === detail.version_id)!;
          return <li key={evaluation.id}><div><strong>{evaluation.metrics.category} · {evaluation.model.model_id}</strong><span>{evaluation.metrics.split === 'heldout' ? '保留评测集' : '调优集'} · {arm.counts.passed} / {arm.counts.valid} 次有效尝试通过 · {arm.stability === 'unstable' ? '不稳定' : arm.stability === 'incomplete' ? '证据不完整' : arm.stability === 'all_passed' ? '全部通过' : '全部失败'}</span><code>{evaluation.id}</code></div><button className="compact" onClick={() => onEvaluation(evaluation.id)}>查看本轮证据</button></li>;
        })}</ul> : <p className="muted small">此版本暂无评测记录。</p>)}</section>
        <form className="review-form" onSubmit={(event) => event.preventDefault()}><h3>审核这个版本</h3><label htmlFor="skill-review-note">审核说明</label><textarea id="skill-review-note" value={note} onChange={(event) => setNote(event.target.value)} required placeholder="记录已核对的内容、许可与权限边界，或拒绝原因" />
          <p className="field-help">批准只允许此版本被选择，不授予额外工具权限。更改审核状态保留记录，不改写正在运行的任务。</p>
          <div className="button-row">{(['approved', 'rejected'] as const).map((status) => <button key={status} className={status === 'approved' ? 'primary' : 'danger'} type="button" disabled={!!busy || !note.trim() || status === 'approved' && !detail.integrity.valid} onClick={() => { void perform('review', async (signal, current) => {
            const result = await api.post<SkillVersion>(`/skills/${encodeURIComponent(detail.version_id)}/review`, { status, note: note.trim() }, signal);
            if (!current()) return;
            store(result); setDetail(result); setNote(''); setNotice(`审核已记录：${reviewNames[status]}。`);
          }); }}>{status === 'approved' ? '批准此版本' : '拒绝此版本'}</button>)}</div>
        </form>
      </>}
      {!selectedId && <p className="empty-copy">导入一个本地 Skill 后，在这里核对版本与内容。</p>}
    </section></div>
  </div>;
}
