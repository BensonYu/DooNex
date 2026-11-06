import { useEffect, useRef, useState } from 'react';
import { Api, type AccountUsageResult, type MetadataResult, type ModelConfig, type ModelProfile, type ModelProfiles, type ModelsResult, type Role, type Run } from './api';

export const formatMoney = (value: number | null | undefined) => value == null || !Number.isFinite(value) ? '未知' : `US$${value.toFixed(4)}`;

export function ModelConfigFields({ value, onChange, disabled = false, prefix = 'model', candidates = [] }: {
  value: ModelConfig;
  onChange: (next: ModelConfig) => void;
  disabled?: boolean;
  prefix?: string;
  candidates?: string[];
}) {
  const change = (patch: Partial<ModelConfig>) => onChange({ ...value, ...patch });
  const metered = value.billing_mode === 'metered';
  return <fieldset className="model-fields" disabled={disabled}>
    <legend>连接与能力配置</legend>
    <div className="settings-grid">
      <div><label htmlFor={`${prefix}-provider`}>供应商适配器</label><select id={`${prefix}-provider`} value={value.provider} onChange={(event) => change({ provider: event.target.value, ...(event.target.value === 'anthropic' ? { api_mode: 'chat' } : {}) })}>
        <option value="openai">OpenAI 兼容接口</option><option value="anthropic">Anthropic 接口</option>
      </select></div>
      <div><label htmlFor={`${prefix}-api-mode`}>API 模式</label><select id={`${prefix}-api-mode`} value={value.api_mode} onChange={(event) => change({ api_mode: event.target.value })}>
        <option value="chat">Chat</option><option value="responses" disabled={value.provider === 'anthropic'}>Responses</option>
      </select></div>
      <div className="span-all"><label htmlFor={`${prefix}-base-url`}>API 地址</label><input id={`${prefix}-base-url`} type="url" value={value.base_url ?? ''} onChange={(event) => change({ base_url: event.target.value || null })} placeholder="留空使用供应商默认地址" spellCheck={false} autoComplete="off" /></div>
      <div><label htmlFor={`${prefix}-model-id`}>实际模型 ID</label><input id={`${prefix}-model-id`} list={`${prefix}-candidates`} value={value.model_id} onChange={(event) => change({ model_id: event.target.value })} required spellCheck={false} autoComplete="off" /><datalist id={`${prefix}-candidates`}>{candidates.map((id) => <option key={id} value={id} />)}</datalist></div>
      <div><label htmlFor={`${prefix}-credential-env`}>服务端凭据引用</label><input id={`${prefix}-credential-env`} value={value.credential_env} onChange={(event) => change({ credential_env: event.target.value })} placeholder="例如 MODEL_API_KEY" required spellCheck={false} autoComplete="off" /></div>
    </div>
    <p className="field-help">这里只保存凭据的环境变量名或服务端引用，不输入 API key。模型列表是候选 ID；接口、工具调用和上下文能力仍需分别验证。</p>
    <div className="settings-grid settings-numbers">
      <div><label htmlFor={`${prefix}-max-input`}>输入 token 上限</label><input id={`${prefix}-max-input`} type="number" min={1} step={1} required value={value.max_input_tokens} onChange={(event) => change({ max_input_tokens: Number(event.target.value) })} /></div>
      <div><label htmlFor={`${prefix}-max-output`}>输出 token 上限</label><input id={`${prefix}-max-output`} type="number" min={1} step={1} required value={value.max_output_tokens} onChange={(event) => change({ max_output_tokens: Number(event.target.value) })} /></div>
      <div><label htmlFor={`${prefix}-timeout`}>请求超时（秒）</label><input id={`${prefix}-timeout`} type="number" min={1} step={1} required value={value.timeout_seconds} onChange={(event) => change({ timeout_seconds: Number(event.target.value) })} /></div>
    </div>
    <label className="checkbox-label"><input type="checkbox" checked={value.allow_private_http} onChange={(event) => change({ allow_private_http: event.target.checked })} />允许连接已授权的私网 HTTP 地址</label>
    <h3>计费与价格元数据</h3>
    <label htmlFor={`${prefix}-billing`}>计费模式</label><select id={`${prefix}-billing`} value={value.billing_mode} onChange={(event) => change({ billing_mode: event.target.value as ModelConfig['billing_mode'] })}>
      <option value="metered">按量计费</option><option value="subscription">已授权包月连接</option>
    </select>
    {metered ? <>
      <div className="settings-grid">
        <div><label htmlFor={`${prefix}-input-price`}>每百万输入 tokens 单价（US$）</label><input id={`${prefix}-input-price`} type="number" min={0} step="any" required value={value.input_price_per_million ?? ''} onChange={(event) => change({ input_price_per_million: event.target.value === '' ? null : Number(event.target.value) })} /></div>
        <div><label htmlFor={`${prefix}-output-price`}>每百万输出 tokens 单价（US$）</label><input id={`${prefix}-output-price`} type="number" min={0} step="any" required value={value.output_price_per_million ?? ''} onChange={(event) => change({ output_price_per_million: event.target.value === '' ? null : Number(event.target.value) })} /></div>
      </div>
      <label htmlFor={`${prefix}-price-source`}>价格来源</label><input id={`${prefix}-price-source`} value={value.price_source ?? ''} onChange={(event) => change({ price_source: event.target.value || null })} required placeholder="供应商价格页或已确认的价格依据与日期" />
      <p className="field-help">Run 费用按冻结价格估算，与账户账单分开记录。价格未知时不能按零计费。</p>
    </> : <p className="field-help">包月连接的货币费用保持未知，仍受 Run 调用、token 和时长预算控制。选择此项前应已确认实际 API 的包月授权。</p>}
  </fieldset>;
}

export function FrozenModels({ run }: { run: Run }) {
  const roles: Role[] = run.request.workflow === 'execute_review' ? ['executor', 'reviewer'] : ['executor'];
  return <details className="frozen-models">
    <summary>本次冻结的模型配置</summary>
    <p className="field-help">以下配置随运行保存。以后编辑配置档，不会改写本次模型、接口、能力或价格。</p>
    {roles.map((role) => {
      const model = run.request.role_models?.[role] ?? run.request.model;
      const profile = run.request.model_profiles?.[role];
      return <section key={role} className="frozen-model">
        <h3>{role === 'executor' ? '执行模型' : '审查模型'} · {model.model_id}</h3>
        <dl className="config-list">
          {profile && <div><dt>配置档</dt><dd>{profile.name} · <code>{profile.id}</code> · 版本 {profile.revision}</dd></div>}
          <div><dt>供应商 / 接口</dt><dd>{model.provider} · {model.api_mode}</dd></div>
          <div><dt>凭据引用</dt><dd>{model.credential_env}</dd></div>
          {model.base_url && <div><dt>API 地址</dt><dd className="path-text">{model.base_url}</dd></div>}
          <div><dt>输入 / 输出上限</dt><dd>{model.max_input_tokens.toLocaleString()} / {model.max_output_tokens.toLocaleString()} tokens</dd></div>
          <div><dt>计费与价格</dt><dd>{model.billing_mode === 'subscription' ? '包月连接 · 货币费用未知' : `按量计费 · 输入 ${formatMoney(model.input_price_per_million)} / 输出 ${formatMoney(model.output_price_per_million)}（每百万 tokens）`}</dd></div>
          {model.billing_mode === 'metered' && <div><dt>价格来源</dt><dd>{model.price_source || '未知'}</dd></div>}
        </dl>
      </section>;
    })}
  </details>;
}


const metadataNames: Record<MetadataResult['status'], string> = {
  ok: '查询成功', not_configured: '未配置', unsupported: '不支持此查询', unauthorized: '鉴权失败',
  rate_limited: '查询受到限流', unavailable: '暂时无法查询', invalid_response: '响应无法识别',
};
const formatTime = (value: string) => {
  const time = new Date(value);
  return Number.isNaN(time.getTime()) ? value : time.toLocaleString('zh-CN', { hour12: false });
};
const quantity = (value: number | null | undefined, unit?: string | null) => value == null ? '未知' : `${value.toLocaleString('zh-CN')}${unit ? ` ${unit}` : ''}`;

function QuerySource({ result }: { result: MetadataResult }) {
  return <div className="query-source">
    <p className={result.status === 'ok' ? 'positive small' : 'attention small'} role="status">{metadataNames[result.status]}{result.http_status != null ? ` · HTTP ${result.http_status}` : ''}</p>
    {result.error && <p className="small">{result.error}</p>}
    <p className="muted small path-text">来源：{result.source || '没有发起外部查询'}<br />查询时间：{formatTime(result.queried_at)}</p>
  </div>;
}

export function AccountUsageReport({ result }: { result: AccountUsageResult }) {
  const usage = result.usage;
  return <section className="account-usage" aria-label="网关或账户查询结果">
    <QuerySource result={result} />
    {result.status === 'ok' && usage && <>
      <dl className="config-list">
        <div><dt>网关报告套餐</dt><dd>{usage.plan_name ?? '未知'}</dd></div>
        <div><dt>网关报告模式</dt><dd>{usage.mode ?? '未知'}</dd></div>
        <div><dt>凭据有效性</dt><dd>{usage.is_valid == null ? '未知' : usage.is_valid ? '查询接口确认有效' : '查询接口报告无效'}</dd></div>
        <div><dt>网关报告剩余量</dt><dd>{quantity(usage.remaining, usage.unit)}{!usage.unit && usage.remaining != null ? '（单位未知）' : ''}</dd></div>
      </dl>
      {usage.unrestricted && <p className="notice">网关返回“不限量”标记。这只说明该查询接口的模式，上游套餐剩余额度仍未知。</p>}
      {Object.keys(usage.metrics).length > 0 && <details className="usage-metrics"><summary>查询接口返回的用量指标</summary><dl className="config-list">{Object.entries(usage.metrics).map(([name, value]) => <div key={name}><dt>{name}</dt><dd>{quantity(value)}</dd></div>)}</dl></details>}
      {usage.windows.length > 0 && <div className="account-windows"><table className="role-table"><caption>查询接口返回的额度窗口</caption><thead><tr><th>窗口</th><th>已用 / 上限</th><th>剩余</th><th>重置时间</th></tr></thead><tbody>{usage.windows.map((window, index) => <tr key={index}><th scope="row">{window.name}</th><td>{quantity(window.used)} / {quantity(window.limit, window.unit)}</td><td>{quantity(window.remaining, window.unit)}</td><td>{window.reset_at ? formatTime(window.reset_at) : '未知'}</td></tr>)}</tbody></table></div>}
    </>}
    <p className="field-help">这是网关或账户接口的记录，覆盖范围与 Run 本地用量、冻结价格估算及供应商最终账单分别计算。没有明确返回的数据保持未知。</p>
  </section>;
}

type ProfileDraft = { id: string; name: string; model: ModelConfig; usage_adapter: ModelProfile['usage_adapter'] };
const freshModel: ModelConfig = {
  provider: 'openai', model_id: '', api_mode: 'responses', base_url: null, credential_env: 'MODEL_API_KEY',
  max_input_tokens: 16000, max_output_tokens: 4000, timeout_seconds: 60, billing_mode: 'metered',
  input_price_per_million: null, output_price_per_million: null, price_source: null, allow_private_http: false,
};
const profileDraft = (profile?: ModelProfile): ProfileDraft => profile
  ? { id: profile.id, name: profile.name, model: { ...profile.model }, usage_adapter: profile.usage_adapter }
  : { id: '', name: '', model: { ...freshModel }, usage_adapter: 'none' };

export function ModelSettings({ api, registry, onRegistry, onClose }: {
  api: Api;
  registry: ModelProfiles;
  onRegistry: (next: ModelProfiles) => void;
  onClose: () => void;
}) {
  const first = registry.profiles.find((profile) => profile.id === registry.default_profile_id) ?? registry.profiles[0];
  const [selectedId, setSelectedId] = useState(first?.id ?? '');
  const [draft, setDraft] = useState(() => profileDraft(first));
  const [busy, setBusy] = useState<'save' | 'refresh' | 'models' | 'usage' | null>(null);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [models, setModels] = useState<ModelsResult | null>(null);
  const [usage, setUsage] = useState<AccountUsageResult | null>(null);
  const operation = useRef({ generation: 0, controller: null as AbortController | null });
  const selected = registry.profiles.find((profile) => profile.id === selectedId);
  const readOnly = !!selected?.read_only;
  const dirty = !selected || JSON.stringify(profileDraft(selected)) !== JSON.stringify(draft);

  const invalidate = () => {
    operation.current.generation += 1; operation.current.controller?.abort(); operation.current.controller = null;
    setBusy(null); setModels(null); setUsage(null); setError(''); setNotice('');
  };
  const choose = (profile?: ModelProfile) => {
    invalidate(); setSelectedId(profile?.id ?? ''); setDraft(profileDraft(profile));
  };
  const update = (patch: Partial<ProfileDraft>) => { invalidate(); setDraft((old) => ({ ...old, ...patch })); };
  useEffect(() => () => operation.current.controller?.abort(), []);

  const perform = async (kind: NonNullable<typeof busy>, action: (signal: AbortSignal, current: () => boolean) => Promise<void>) => {
    operation.current.controller?.abort();
    const controller = new AbortController(), generation = ++operation.current.generation;
    operation.current.controller = controller;
    const current = () => !controller.signal.aborted && operation.current.generation === generation;
    setBusy(kind); setError(''); setNotice('');
    try { await action(controller.signal, current); }
    catch (error) { if (current()) setError(error instanceof Error ? error.message : String(error)); }
    finally { if (current()) { setBusy(null); operation.current.controller = null; } }
  };
  const query = (kind: 'models' | 'usage') => {
    if (!selected || dirty) return;
    const id = selected.id, revision = selected.revision;
    void perform(kind, async (signal, current) => {
      const result = await api.get<ModelsResult | AccountUsageResult>(`/model-profiles/${encodeURIComponent(id)}/${kind}`, signal);
      if (!current()) return;
      if (result.profile_id !== id || result.revision !== revision) throw new Error('配置版本已变化，请重新读取配置后查询。');
      if (kind === 'models') setModels(result as ModelsResult); else setUsage(result as AccountUsageResult);
    });
  };
  const copy = () => {
    if (!selected) return;
    invalidate();
    let id = selected.id === 'legacy' ? 'primary' : `${selected.id.slice(0, 55)}_copy`;
    const stem = id; let index = 2;
    while (registry.profiles.some((profile) => profile.id === id)) id = `${stem}_${index++}`;
    setSelectedId(''); setDraft({ ...profileDraft(selected), id, name: `${selected.name} 副本` });
  };

  return <div className="model-settings-view">
    <div className="page-heading"><div><h1>模型设置</h1><p className="muted">保存明确的连接与能力配置，在创建任务时按角色选择。</p></div><button onClick={onClose}>返回工作台</button></div>
    {error && <p className="notice error" role="alert">{error}</p>}{notice && <p className="notice" role="status">{notice}</p>}
    <div className="profiles-layout">
      <nav className="profiles-list" aria-label="模型配置档">
        <div className="section-heading"><h2>配置档</h2><button className="compact" onClick={() => choose()}>新配置</button></div>
        {registry.profiles.length === 0 && <p className="muted small">尚无模型配置。保存后可供执行或审查使用。</p>}
        {registry.profiles.map((profile) => <button key={profile.id} className={`profile-item ${selectedId === profile.id ? 'selected' : ''}`} aria-current={selectedId === profile.id ? 'page' : undefined} onClick={() => choose(profile)}>
          <strong>{profile.name}</strong><code>{profile.id}</code><span>{profile.model.model_id}</span><small>{profile.credential_available ? '凭据引用已就绪' : '凭据引用未就绪'}{profile.read_only ? ' · 只读' : ''}</small>
        </button>)}
        <button className="quiet compact" disabled={!!busy} onClick={() => { void perform('refresh', async (signal, current) => {
          const next = await api.get<ModelProfiles>('/model-profiles', signal);
          if (!current()) return;
          onRegistry(next); choose(next.profiles.find((profile) => profile.id === selectedId));
        }); }}>重新读取配置</button>
      </nav>
      <div className="profile-editor">
        <div className="section-heading"><h2>{selected ? selected.name : '新建配置档'}</h2>{selected && <button className="compact" disabled={!!busy} onClick={copy}>复制为新配置</button>}</div>
        {readOnly && <p className="notice">这是服务端现有的只读配置，可复制后保存为独立配置档。</p>}
        <form onSubmit={(event) => { event.preventDefault(); if (readOnly) return;
          void perform('save', async (signal, current) => {
            const result = await api.put<ModelProfile>(`/model-profiles/${encodeURIComponent(draft.id)}`, { name: draft.name.trim(), model: draft.model, usage_adapter: draft.usage_adapter, expected_revision: selected?.revision ?? null }, signal);
            if (!current()) return;
            onRegistry({ ...registry, profiles: [...registry.profiles.filter((profile) => profile.id !== result.id), result] });
            choose(result); setNotice('配置已保存。保存和模型列表查询不代表该模型能力已经验证。');
          });
        }}>
          <fieldset className="model-fields" disabled={readOnly || busy === 'save'}>
            <legend>配置身份</legend><div className="settings-grid">
              <div><label htmlFor="profile-name">配置名称</label><input id="profile-name" value={draft.name} onChange={(event) => update({ name: event.target.value })} required maxLength={100} placeholder="例如 复杂任务 / 快速任务" /></div>
              <div><label htmlFor="profile-id">稳定配置 ID</label><input id="profile-id" value={draft.id} onChange={(event) => update({ id: event.target.value })} required pattern="[a-z][a-z0-9_-]{0,63}" maxLength={64} disabled={!!selected} placeholder="例如 primary / fast" spellCheck={false} autoComplete="off" /></div>
            </div>
            <p className="field-help">ID 以小写字母开头，可含数字、下划线和连字符；保存后通过复制创建新的 ID。</p>
          </fieldset>
          <ModelConfigFields value={draft.model} onChange={(model) => update({ model })} disabled={readOnly || busy === 'save'} candidates={models?.status === 'ok' ? models.models : []} />
          <fieldset className="model-fields" disabled={readOnly || busy === 'save'}><legend>只读账户查询</legend><label htmlFor="profile-usage-adapter">用量查询适配器</label><select id="profile-usage-adapter" value={draft.usage_adapter} onChange={(event) => update({ usage_adapter: event.target.value as ModelProfile['usage_adapter'] })}>
            <option value="none">不启用账户查询</option><option value="gateway_v1">已确认兼容的网关 /v1/usage</option>
          </select><p className="field-help">网关查询仅在你显式启用后使用当前 API 地址的同源固定端点。返回口径由该接口决定。</p></fieldset>
          <div className="form-footer"><span className="muted small">{selected && !dirty ? selected.credential_available ? '服务端检测到凭据引用' : '服务端尚未检测到凭据引用' : '保存后由服务端检查凭据引用'}</span><button className="primary" type="submit" disabled={readOnly || !!busy || !dirty}>{busy === 'save' ? '正在保存…' : '保存配置'}</button></div>
        </form>
        <section className="profile-queries" aria-label="当前配置的元数据查询">
          <h2>模型与账户元数据</h2><p className="field-help">只查询已保存版本；不会发起模型推理。修改连接或凭据引用后，先保存再查询。</p>
          <div className="button-row"><button disabled={!selected || dirty || !!busy} onClick={() => query('models')}>{busy === 'models' ? '正在查询模型…' : '查询候选模型 ID'}</button><button disabled={!selected || dirty || !!busy} onClick={() => query('usage')}>{busy === 'usage' ? '正在查询用量…' : '查询网关或账户用量'}</button></div>
          {models && <section className="models-query" aria-label="候选模型查询结果"><QuerySource result={models} />{models.status === 'ok' && <><p className="small">返回 {models.models.length} 个候选 ID；本次查询没有验证工具调用、API 模式或上下文能力。</p><ul className="model-id-list">{models.models.map((id) => <li key={id}><code>{id}</code></li>)}</ul>{models.models.length > 0 && <><label htmlFor="candidate-model">选用候选 ID</label><select id="candidate-model" value="" disabled={readOnly} onChange={(event) => update({ model: { ...draft.model, model_id: event.target.value } })}><option value="" disabled>{readOnly ? '复制只读配置后可选用' : '选择后填入模型 ID'}</option>{models.models.map((id) => <option key={id} value={id}>{id}</option>)}</select></>}</>}</section>}
          {usage && <AccountUsageReport result={usage} />}
        </section>
      </div>
    </div>
  </div>;
}

export function RoleModelChoices({ registry, workflow, executorId, reviewerId, onChange, onSettings, fallback }: {
  registry: ModelProfiles | null;
  workflow: string;
  executorId: string;
  reviewerId: string;
  onChange: (role: Role, id: string) => void;
  onSettings: () => void;
  fallback: { model: ModelConfig | null; credential_available: boolean } | null;
}) {
  const roles: Role[] = workflow === 'execute_review' ? ['executor', 'reviewer'] : ['executor'];
  return <section className="section-block role-model-choices" aria-label="角色模型选择">
    <div className="section-heading"><h2>按角色选择模型</h2><button type="button" className="quiet compact" onClick={onSettings}>管理模型配置</button></div>
    <p className="field-help">角色使用明确选定的配置，两个角色共享本次 Run 预算。配置保存或列出模型 ID 都不代表能力已经验证。</p>
    <div className="settings-grid">{roles.map((role) => {
      const id = role === 'executor' ? executorId : reviewerId;
      const profile = registry?.profiles.find((profile) => profile.id === id);
      const model = profile?.model ?? (!id ? fallback?.model : null);
      const available = profile?.credential_available ?? (!id && fallback?.credential_available);
      return <div key={role}>
        <label htmlFor={`role-model-${role}`}>{role === 'executor' ? '执行模型配置' : '审查模型配置'}</label>
        <select id={`role-model-${role}`} value={id} onChange={(event) => onChange(role, event.target.value)}>
          {!registry?.profiles.length && <option value="">{fallback?.model ? '服务端默认配置（兼容模式）' : '尚未配置模型'}</option>}
          {id && !profile && <option value={id} disabled>保存的配置不可用：{id}</option>}
          {registry?.profiles.map((profile) => <option key={profile.id} value={profile.id}>{profile.name} · {profile.model.model_id}</option>)}
        </select>
        {model && <><p className="small model-selection-detail"><strong>{model.model_id}</strong><br />{model.provider} · {model.api_mode} · {model.billing_mode === 'subscription' ? '包月连接' : '按量计费'}<br />输入 {model.max_input_tokens.toLocaleString()} / 输出 {model.max_output_tokens.toLocaleString()} tokens</p><p className={`field-help ${available ? 'positive' : 'attention'}`}>{available ? '服务端凭据引用已就绪' : '服务端凭据引用未就绪'}</p></>}
      </div>;
    })}</div>
  </section>;
}
