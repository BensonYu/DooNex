export type Budget = {
  max_calls: number;
  max_tokens: number;
  max_cost_usd: number | null;
  max_seconds: number;
};

export type ModelConfig = {
  provider: string;
  model_id: string;
  api_mode: string;
  base_url?: string | null;
  max_input_tokens: number;
  max_output_tokens: number;
  timeout_seconds: number;
  input_price_per_million?: number | null;
  output_price_per_million?: number | null;
  price_source?: string | null;
  credential_env: string;
  billing_mode: 'metered' | 'subscription';
  allow_private_http: boolean;
};

export type Settings = {
  model: ModelConfig | null;
  credential_available: boolean;
  default_budget: Budget;
};

export type ModelProfile = {
  id: string;
  name: string;
  model: ModelConfig;
  revision: string;
  read_only: boolean;
  usage_adapter: 'none' | 'gateway_v1';
  credential_available: boolean;
};
export type ModelProfiles = { profiles: ModelProfile[]; default_profile_id: string | null };

export type MetadataStatus = 'ok' | 'not_configured' | 'unsupported' | 'unauthorized' | 'rate_limited' | 'unavailable' | 'invalid_response';
export type MetadataResult = {
  status: MetadataStatus;
  profile_id: string;
  revision: string;
  source: string | null;
  queried_at: string;
  http_status: number | null;
  error: string | null;
};
export type ModelsResult = MetadataResult & { models: string[] };
export type AccountUsage = {
  is_valid: boolean | null;
  mode: string | null;
  plan_name: string | null;
  remaining: number | null;
  unit: string | null;
  unrestricted: boolean;
  metrics: Record<string, number>;
  windows: { name: string; used: number | null; limit: number | null; remaining: number | null; unit: string | null; reset_at: string | null }[];
};
export type AccountUsageResult = MetadataResult & { usage: AccountUsage | null };

export type SkillMetadata = {
  source: string;
  license_status: 'unknown' | 'declared' | 'verified' | 'restricted';
  license_text: string | null;
  commit: string | null;
  scope: string[];
  permissions: string[];
};
export type SkillVersion = {
  version_id: string;
  content_digest: string;
  created_at: string;
  metadata: SkillMetadata;
  review_status: 'pending' | 'approved' | 'rejected';
  integrity: { valid: boolean; errors: string[] };
  files: { path: string; size: number; sha256: string; mode: number | string }[];
  instructions?: string;
};
export type SkillDiscoveryPreview = {
  candidate_id: string;
  sha256: string;
  instructions: string;
  files: { path: string; mode: number; size: number; sha256: string; blob_sha: string }[];
  directories: string[];
  license: {
    status: 'supported' | 'preview_only';
    spdx: 'MIT' | 'Apache-2.0' | null;
    files: { path: string; text: string; sha256: string; blob_sha: string }[];
    reasons: string[];
  };
  importable: boolean;
};
export type SkillDiscovery = {
  id: string;
  revision: string;
  status: 'scanning' | 'ready' | 'previewing' | 'preview_ready' | 'importing' | 'imported' | 'failed' | 'interrupted';
  created_at: string;
  updated_at: string;
  input: { repository: string; ref: string; prefix: string };
  source: null | { repository_id: number; repository: string; commit: string; root_tree: string; prefix_tree: string; resolved_at: string; api_version: string };
  candidates: { id: string; path: string; tree_sha: string; file_count: number; entry_count: number; total_bytes: number }[];
  preview: SkillDiscoveryPreview | null;
  imported: { candidate_id: string; version_id: string } | null;
  budget: { max_requests: number; requests_used: number; remaining_requests: number; max_bytes: number; bytes_used: number; bytes_reserved: number; remaining_bytes: number };
  requests: { sequence: number; kind: string; status: 'reserved' | 'completed' | 'failed' | 'unknown'; received_bytes: number; http_status: number | null; error: string | null }[];
  error: { code: string } | null;
  source_rate_limit: { remaining: number | null; reset: number | null } | null;
};
export type GenerationParent = { version_id: string; content_digest: string; report_sha256: string; metadata: SkillMetadata };
export type GenerationInputs = {
  parents: GenerationParent[];
  tasksets: { taskset_id: string; name: string; split: 'tuning'; tasks: { id: string; title: string }[] }[];
  models: ModelProfile[];
  projects: { id: string; name: string }[];
};
export type GenerationLicense = {
  status: 'supported' | 'preview_only';
  spdx: 'MIT' | 'Apache-2.0' | null;
  files: { path: string; text: string; sha256: string; blob_sha: string | null }[];
  reasons: string[];
};
export type GenerationReport = { valid: boolean; errors: string[]; [key: string]: unknown };
export type SkillGeneration = {
  id: string;
  revision: string;
  state: string;
  run_id: string | null;
  run_state: string | null;
  usage: Usage | null;
  input: { project_id: string; parent_version_id: string; taskset_id: string; task_ids: string[]; model_profile_id: string; budget: Budget };
  parent: GenerationParent;
  license: GenerationLicense;
  visible_tasks: { id: string; title: string; prompt: string; visible_checks: string[] }[];
  projection_sha256: string;
  model: ModelConfig;
  model_profile: { id: string; name: string; revision: string };
  prompt: string;
  prompt_sha256: string;
  policy: 'append_instructions_v1';
  budget: Budget;
  candidate: (GenerationReport & { output_sha256: string; report_sha256: string; manifest_sha256: string | null; archive_sha256: string | null; content_digest: string | null }) | null;
  preview: {
    sha256: string; instructions: string; diff: string; license: GenerationLicense;
    metadata: SkillMetadata; provenance: Record<string, unknown>;
    structural_report: GenerationReport; evidence: Record<string, unknown>;
  } | null;
  imported: { version_id: string; report_sha256: string; content_digest: string; at: string } | null;
  created_at: string;
  updated_at: string;
  error: { code: string } | null;
};
export type ArenaTaskset = {
  id: string;
  name: string;
  category: string;
  split: 'tuning' | 'heldout';
  task_count: number;
  tasks: { id: string; title: string; critical: boolean }[];
  created_at: string;
  valid: boolean;
  errors: string[];
};
export type ArenaCounts = Record<'planned' | 'attempts' | 'completed' | 'valid' | 'passed' | 'failed' | 'environment_failure' | 'cancelled' | 'assisted' | 'pending' | 'evidence_invalid' | 'safety_violations' | 'regression_failures' | 'missing_slots', number>;
export type ArenaStability = 'all_passed' | 'unstable' | 'all_failed' | 'incomplete';
export type ArenaArm = {
  id: string;
  label: string;
  counts: ArenaCounts;
  tasks: {
    task_id: string;
    critical: boolean;
    counts: ArenaCounts;
    pass_rate: number | null;
    stability: ArenaStability;
    missing_repeats: number[];
    attempts: {
      run_id: string; repeat: number; classification: string; reported_classification?: string;
      evidence_valid: boolean; safety_violation: boolean; regression_failed: boolean;
      duration_seconds?: number | null; queued_seconds?: number | null;
      usage?: { calls: number; tokens: number; reserved_tokens: number; unknown_calls: number; cost_usd: number | null };
    }[];
  }[];
  pass_rate: number | null;
  stability: ArenaStability;
  usage: { calls: number; tokens: number; reserved_tokens: number; unknown_calls: number; cost_usd: number | null; unit_success_cost_usd: number | null };
  timing: { successful_median_seconds: number | null; valid_median_seconds: number | null; queued_median_seconds: number | null };
  eligibility: { eligible: boolean; reasons: string[] };
};
export type RecommendationPosition = 'reliable' | 'cost' | 'speed';
export type ArenaMetrics = {
  state: string;
  split: ArenaTaskset['split'];
  category: string;
  fingerprint: string;
  planned_attempts: number;
  counts: ArenaCounts;
  complete: boolean;
  issues: string[];
  arms: ArenaArm[];
  usage: ArenaArm['usage'];
  timing: ArenaArm['timing'];
  recommendations: Record<RecommendationPosition, string[]>;
};
export type ArenaEvaluation = {
  id: string;
  state: string;
  reason: string | null;
  taskset_id: string;
  model: ModelConfig;
  run_budget?: Budget;
  batch_budget?: Budget;
  repeats?: number;
  skill_version_ids?: string[];
  usage: Usage;
  metrics: ArenaMetrics;
  attempts: {
    run_id: string; project_id: string; task_id: string; arm_id: string; repeat: number;
    classification: string; state: string; reason: string | null;
    evidence_valid?: boolean; safety_violation?: boolean; regression_failed?: boolean;
    assistance?: { reason: string; at: string; snapshot_digest: string }[];
  }[];
};
export type ArenaRecommendation = {
  id: string;
  position: RecommendationPosition;
  arm_id: string;
  evaluation_id: string;
  fingerprint: string;
  category: string;
  model: ModelConfig;
  created_at: string;
  previous_id: string | null;
};
export type ArenaRecommendations = { current: ArenaRecommendation[]; history: ArenaRecommendation[] };

export type BackgroundInput = {
  name?: string;
  taskset_id: string;
  skill_version_ids: string[];
  model_profile_id: string;
  repeats: number;
  run_budget: Budget;
  batch_budget: Budget;
  daily_budget: Budget;
  window_start: string;
  window_end: string;
};
export type BackgroundAuthorization = {
  id: string;
  name: string;
  state: 'draft' | 'enabled' | 'consumed' | 'disabled';
  revision: string;
  created_at: string;
  updated_at: string;
  enabled_at: string | null;
  disabled_at: string | null;
  consumed_at: string | null;
  evaluation_id: string | null;
  evaluation?: Pick<ArenaEvaluation, 'id' | 'state' | 'reason' | 'usage'> & { yielded_runs?: number } | null;
  dispatch_status?: { state: 'waiting' | 'blocked'; reason: string; at: string };
  date: string;
  window_start: string;
  window_end: string;
  expires_at: string;
  input: BackgroundInput;
  snapshot: {
    taskset_id: string;
    taskset_report_sha256: string;
    skills: Record<string, unknown>;
    model: ModelConfig;
    model_profile: { id: string; name: string; revision: string };
    environment: unknown;
  };
  history: { event: string; at: string; [key: string]: unknown }[];
};
export type BackgroundDayUsage = { calls: number; tokens: number; seconds: number; cost_usd: number | null; metered_cost_usd: number };
export type BackgroundDay = {
  date: string;
  budget: Budget;
  used: BackgroundDayUsage;
  reserved: BackgroundDayUsage;
  created_at: string;
  updated_at: string;
};

export type Project = { id: string; path: string; head: string; dirty: boolean; name?: string };
export type Role = 'executor' | 'reviewer';
export type Workflow = 'execute' | 'execute_review';
export type Usage = {
  calls: number;
  tokens: number;
  cost_usd: number | null;
  reserved_tokens: number;
  reserved_cost_usd: number | null;
  unknown_calls: number;
  metered_cost_usd?: number;
  reserved_metered_cost_usd?: number;
};
export type Assignment = {
  id: string;
  role: Role;
  state: string;
  started_at?: string | null;
  ended_at?: string | null;
  session_generation?: number;
  resume_session?: boolean;
  usage?: Usage;
};
export type Review = {
  status: 'approved' | 'changes_requested';
  summary: string;
  valid?: boolean;
  errors?: string[];
  findings: { path: string | null; side?: 'before' | 'after'; line?: number | null; severity: 'high' | 'medium' | 'low'; message: string }[];
  snapshot_digest: string;
};

export type Run = {
  id: string;
  state: string;
  reason: string | null;
  created_at: string;
  updated_at: string;
  phase?: 'execution' | 'review' | 'verification';
  active_assignment?: Role;
  assignments?: Partial<Record<Role, Assignment>>;
  role_usage?: Partial<Record<Role, Usage>>;
  handoff?: { snapshot_digest: string } | null;
  review?: Review | null;
  review_integrity?: { valid: boolean; errors: string[] };
  request: {
    purpose?: 'skill_generation';
    generation?: { id: string; [key: string]: unknown };
    project_id: string;
    project_name?: string;
    workflow?: Workflow;
    prompt: string;
    base_commit: string | null;
    repo_path: string | null;
    checks: string[];
    budget: Budget;
    model: ModelConfig;
    role_models?: Partial<Record<Role, ModelConfig>>;
    model_profiles?: Partial<Record<Role, { id: string; name: string; revision: string | number }>>;
    skill?: { version_id: string; content_digest: string; report_sha256: string; metadata: SkillMetadata } | null;
    recommendation?: ArenaRecommendation | null;
  };
  usage: Usage;
  evidence?: Record<string, unknown> | null;
};

export type RunEvent = {
  id: number;
  kind: string;
  created_at: string;
  payload: unknown;
};

export type Recovery = {
  containers: unknown[];
  pending_actions: unknown[];
  workspace_ok: boolean;
  notes: string[];
};

export type Diff = { diff: string; source?: 'review_handoff' | 'evidence' | 'unavailable'; snapshot_digest?: string; verified?: boolean };

export type ArtifactName = 'patch' | 'snapshot' | 'report' | `check-${number}.log`;

function errorMessage(value: unknown): string | null {
  if (typeof value === 'string') return value;
  if (!value || typeof value !== 'object') return null;
  const object = value as Record<string, unknown>;
  if (typeof object.detail === 'string') return object.detail;
  if (Array.isArray(object.detail)) {
    return object.detail.map((item: unknown) => {
      if (item && typeof item === 'object' && 'msg' in item) return String(item.msg);
      return JSON.stringify(item);
    }).join('；');
  }
  if (typeof object.message === 'string') return object.message;
  return null;
}

export class ApiError extends Error {
  constructor(message: string, readonly status: number, readonly code: string | null = null, readonly discoveryId: string | null = null, readonly generationId: string | null = null) { super(message); }
}

export class Api {
  constructor(private readonly token: string) {}

  private async response(path: string, init: RequestInit = {}): Promise<Response> {
    let response: Response;
    try {
      response = await fetch(`/api${path}`, {
        ...init,
        headers: {
          Authorization: `Bearer ${this.token}`,
          ...(init.body ? { 'Content-Type': 'application/json' } : {}),
          ...init.headers,
        },
        cache: 'no-store',
      });
    } catch (error) {
      if (error instanceof DOMException && error.name === 'AbortError') throw error;
      throw new Error('无法连接本地服务。请确认 DooNex 服务正在运行。');
    }
    if (!response.ok) {
      if (response.status === 401 || response.status === 403) {
        throw new ApiError('访问令牌无效或无权执行此操作。请检查令牌与本地服务配置。', response.status);
      }
      const text = await response.text();
      let detail: string | null = null;
      let code: string | null = null, discoveryId: string | null = null, generationId: string | null = null;
      try {
        const payload: unknown = JSON.parse(text);
        detail = errorMessage(payload);
        if (payload && typeof payload === 'object' && 'detail' in payload && payload.detail && typeof payload.detail === 'object') {
          const value = payload.detail as Record<string, unknown>;
          if (typeof value.code === 'string' && /^[a-zA-Z0-9_]{1,100}$/.test(value.code)) code = value.code;
          if (typeof value.discovery_id === 'string' && /^[a-fA-F0-9-]{36}$/.test(value.discovery_id)) discoveryId = value.discovery_id;
          if (typeof value.generation_id === 'string' && /^[a-fA-F0-9-]{36}$/.test(value.generation_id)) generationId = value.generation_id;
        }
      } catch { /* Non-JSON errors use a bounded fallback. */ }
      const boundedOperation = path.startsWith('/skills/discoveries') || path.startsWith('/skills/generations');
      const fallback = boundedOperation
        ? `${path.startsWith('/skills/generations') ? '生成' : '发现'}操作未完成（HTTP ${response.status}${code ? ` · ${code}` : ''}）。请读取保存回执核对。`
        : `请求失败（HTTP ${response.status}）${text ? `：${text.slice(0, 300)}` : ''}`;
      throw new ApiError(boundedOperation ? fallback : detail ?? fallback, response.status, code, discoveryId, generationId);
    }
    return response;
  }

  async get<T>(path: string, signal?: AbortSignal): Promise<T> {
    return (await this.response(path, { signal })).json() as Promise<T>;
  }

  async post<T>(path: string, body: unknown = {}, signal?: AbortSignal): Promise<T> {
    return (await this.response(path, { method: 'POST', body: JSON.stringify(body), signal })).json() as Promise<T>;
  }

  async put<T>(path: string, body: unknown, signal?: AbortSignal): Promise<T> {
    return (await this.response(path, { method: 'PUT', body: JSON.stringify(body), signal })).json() as Promise<T>;
  }

  async download(projectId: string, runId: string, name: ArtifactName, signal?: AbortSignal): Promise<void> {
    const extension = name === 'patch' ? '.patch' : name === 'report' ? '.json' : name === 'snapshot' ? '.tar.gz' : '';
    return this.downloadFile(`/runs/${encodeURIComponent(runId)}/artifact?name=${encodeURIComponent(name)}&project_id=${encodeURIComponent(projectId)}`, `doonex-${runId}-${name}${extension}`, signal);
  }

  async downloadGeneration(id: string, name: 'candidate' | 'report' | 'output', signal?: AbortSignal): Promise<void> {
    return this.downloadFile(`/skills/generations/${encodeURIComponent(id)}/artifact?name=${name}`, `doonex-generation-${id}-${name}${name === 'candidate' ? '.tar.gz' : '.json'}`, signal);
  }

  private async downloadFile(path: string, fallback: string, signal?: AbortSignal): Promise<void> {
    const response = await this.response(path, { signal });
    const blob = await response.blob();
    if (signal?.aborted) return;
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement('a');
    anchor.href = url;
    const disposition = response.headers.get('Content-Disposition') ?? '';
    const encodedName = disposition.match(/filename\*=UTF-8''([^;]+)/i)?.[1];
    const plainName = disposition.match(/filename="([^"]+)"/i)?.[1] ?? disposition.match(/filename=([^;]+)/i)?.[1];
    let filename = plainName?.trim();
    if (encodedName) {
      try { filename = decodeURIComponent(encodedName); } catch { /* Use the plain name or fallback. */ }
    }
    anchor.download = filename?.replace(/[\\/\x00-\x1f]/g, '_') || fallback;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
}
