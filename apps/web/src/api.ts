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
    project_id: string;
    project_name?: string;
    workflow?: Workflow;
    prompt: string;
    base_commit: string;
    repo_path: string;
    checks: string[];
    budget: Budget;
    model: ModelConfig;
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
        throw new Error('访问令牌无效或无权执行此操作。请检查令牌与本地服务配置。');
      }
      const text = await response.text();
      let detail: string | null = null;
      try { detail = errorMessage(JSON.parse(text)); } catch { /* Non-JSON errors use a bounded fallback. */ }
      throw new Error(detail ?? `请求失败（HTTP ${response.status}）${text ? `：${text.slice(0, 300)}` : ''}`);
    }
    return response;
  }

  async get<T>(path: string, signal?: AbortSignal): Promise<T> {
    return (await this.response(path, { signal })).json() as Promise<T>;
  }

  async post<T>(path: string, body: unknown = {}, signal?: AbortSignal): Promise<T> {
    return (await this.response(path, { method: 'POST', body: JSON.stringify(body), signal })).json() as Promise<T>;
  }

  async download(projectId: string, runId: string, name: ArtifactName, signal?: AbortSignal): Promise<void> {
    const response = await this.response(`/runs/${encodeURIComponent(runId)}/artifact?name=${encodeURIComponent(name)}&project_id=${encodeURIComponent(projectId)}`, { signal });
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
    const extension = name === 'patch' ? '.patch' : name === 'report' ? '.json' : name === 'snapshot' ? '.tar.gz' : '';
    anchor.download = filename?.replace(/[\\/\x00-\x1f]/g, '_') || `doonex-${runId}-${name}${extension}`;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
}
