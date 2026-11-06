import type { SkillDiscovery, SkillDiscoveryPreview } from './api';

export type CommunitySettings = { enabled: boolean; title: string; description: string; revision: string; commercial_enabled: false; limits: Record<string, { daily: number; total: number }>; totals: Record<string, number> };
export type CommunityCredential = { id: string; kind?: 'submission' | 'usage'; label: string; expires_at: string; revoked_at: string | null; created_at: string; max_submissions?: number; submissions_used?: number };
export type SubmissionInput = { request_id: string; repository: string; ref: string; prefix: string; title: string; summary: string; category: string; author_label: string; author_statement: string };
export type CommunityContent = { title: string; summary: string; category: string; author_label: string; author_statement: string; identity_status: 'self_reported'; source: { repository: string; commit: string; path: string; tree_sha: string }; content_digest: string; files: SkillDiscoveryPreview['files']; directories: string[]; instructions: string; license: SkillDiscoveryPreview['license']; archive_sha256: string };
export type CommunitySubmission = { id: string; input: SubmissionInput & { source: { repository: string; ref: string; prefix: string } }; state: string; revision?: string; discovery?: SkillDiscovery | null; preview?: { sha256: string; public: CommunityContent; discovery_preview_sha256: string } | null; review?: { status: string; reviewer_label: string; note: string; preview_sha256: string; at: string } | null; operation?: { kind: string; request_id: string; at: string } | null; release_id: string | null; error: string | null; created_at: string; updated_at: string };
export type CommunityRelease = CommunityContent & { id: string; state: 'published' | 'withdrawn'; published_at: string; review: { reviewer_label: string; approved_at: string }; quality: { controller_verified: boolean; evidence_count: number } };
export type Distribution = { receipt: { id: string; release_id: string; source: 'distribution_request'; created_at: string; installation_verified: false; content_digest: string; archive_sha256: string }; usage_token: string | null; shown_once: true };
export type SelfReport = { event_id: string; release_id: string; distribution_id: string; outcome: string; source: 'self_reported'; controller_verified: false; reported_at: string };
export type Site = { title: string; description: string; commercial: { enabled: false }; identity_policy: string; leaderboard_reason?: string };
export type EvidencePublic = { id?: string; schema_version: number; source: 'controller_verified'; release_id: string; content_digest: string; category: string; model_id: string; model_fingerprint: string; taskset_fingerprint: string; environment_fingerprint: string; budget_fingerprint: string; condition_fingerprint: string; task_count: number; repeats: number; minimum_pass_rate: number; pass_rate: number | null; stability: string; counts: Record<string, number>; batch_counts: Record<string, number>; usage: { calls: number; tokens: number; reserved_tokens: number; unknown_calls: number; cost_usd: number | null; unit_success_cost_usd: number | null }; timing: Record<string, number | null>; eligible: boolean; successful_timing_complete: boolean; recommendations: Record<'reliable' | 'cost' | 'speed', boolean>; verified_at: string; expires_at: string; published_at?: string };
export type EvidenceRecord = { id: string; revision: string; state: string; preview_sha256: string | null; public: EvidencePublic | null; error?: string | null; public_fields: { path: string; value: unknown }[]; private_binding_digest: string | null };
export type Leaderboard = { groups: { condition_fingerprint: string; category: string; model_id: string; model_fingerprint: string; taskset_fingerprint: string; environment_fingerprint: string; budget_fingerprint: string; entries: EvidencePublic[]; recommendations: Record<'reliable' | 'cost' | 'speed', string[]> }[] };
export class CommunityError extends Error { constructor(readonly status: number, readonly code: string | null) { super(`社区操作未完成（HTTP ${status}${code ? ` · ${code}` : ''}）。请读取已保存记录核对。`); } }

// A public instance never inherits the workbench token, cookies, or private API paths.
export class CommunityClient {
  constructor(private readonly token = '') {}
  private async response(route: string, init: RequestInit = {}): Promise<Response> {
    if (!route.startsWith('/community/')) throw new Error('社区接口范围无效。');
    const response = await fetch(`/api${route}`, { ...init, credentials: 'omit', cache: 'no-store', headers: { ...(this.token ? { Authorization: `Bearer ${this.token}` } : {}), ...(init.body ? { 'Content-Type': 'application/json' } : {}) } });
    if (!response.ok) {
      let code: string | null = null;
      try { const data = await response.json(); if (typeof data?.detail?.code === 'string' && /^[a-zA-Z0-9_]{1,100}$/.test(data.detail.code)) code = data.detail.code; } catch { /* Do not surface provider bodies. */ }
      throw new CommunityError(response.status, code);
    }
    return response;
  }
  async get<T>(route: string, signal?: AbortSignal): Promise<T> { return (await this.response(route, { signal })).json(); }
  async write<T>(route: string, body: unknown, signal?: AbortSignal, method = 'POST'): Promise<T> { return (await this.response(route, { method, body: JSON.stringify(body), signal })).json(); }
  async download(id: string, expectedSha256: string, signal?: AbortSignal): Promise<void> {
    const response = await this.response(`/community/public/distributions/${encodeURIComponent(id)}/download`, { signal });
    const buffer = await response.arrayBuffer();
    const digest = [...new Uint8Array(await crypto.subtle.digest('SHA-256', buffer))].map(value => value.toString(16).padStart(2, '0')).join('');
    if (digest !== expectedSha256) throw new Error('分发文件摘要与固定发布不一致，已停止下载。');
    if (signal?.aborted) return;
    const url = URL.createObjectURL(new Blob([buffer], { type: 'application/gzip' }));
    const anchor = document.createElement('a'); anchor.href = url; anchor.download = `doonex-community-${id}.tar.gz`; document.body.appendChild(anchor); anchor.click(); anchor.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
}
