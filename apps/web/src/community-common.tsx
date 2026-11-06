import { useEffect, useRef, useState } from 'react';
import type { CommunityContent } from './community-api';

type PendingIntent = { kind: string; intent_id: string; at: string };
const stored = (key: string): PendingIntent | null => { try { const value = localStorage.getItem(key); return value ? JSON.parse(value) : null; } catch { return null; } };
export const expiresWithinThirtyDays = (value: string) => { const time = new Date(value).getTime(); return Number.isFinite(time) && time > Date.now() && time <= Date.now() + 30 * 86400000; };
const remember = (key: string, value: string) => { try { value ? localStorage.setItem(key, value) : localStorage.removeItem(key); return true; } catch { return false; } };
export const dateText = (value: string) => new Date(value).toLocaleString('zh-CN', { hour12: false });
export const communityStates: Record<string, string> = { submitted: '待读取来源', source_ready: '来源已冻结', source_reads_in_progress: '来源读取中', previews_in_progress: '预览中', source_unknown: '来源结果未知', preview_ready: '待社区审核', preview_only: '仅供预览', approved: '社区审核已批准', rejected: '社区审核已拒绝', published: '已发布', withdrawn: '已下架', preparing: '证据准备中', prepared: '待公开字段确认', failed: '已保留失败记录' };
export function useCommunityAction(scope: string) {
  const key = `doonex.community.unknown.${scope}`;
  const [pending, setPending] = useState<PendingIntent | null>(() => stored(key));
  const needsRead = pending !== null;
  const [busy, setBusy] = useState(''); const [error, setError] = useState('');
  const state = useRef({ epoch: 0, controller: null as AbortController | null, busy: false });
  const clearUnknown = () => { if (remember(key, '')) setPending(null); else setError('浏览器未能清除操作意图，请恢复本站存储后重新核对。'); };
  const perform = async (kind: string, action: (signal: AbortSignal, current: () => boolean) => Promise<void>, mutation = false, intentId?: string) => {
    if (state.current.busy || mutation && needsRead) return;
    if (mutation) { const intent = { kind, intent_id: intentId ?? crypto.randomUUID(), at: new Date().toISOString() }; if (!remember(key, JSON.stringify(intent))) { setError('浏览器无法保存操作意图，请启用本站存储后再执行写入。'); return; } setPending(intent); }
    const controller = new AbortController(), epoch = ++state.current.epoch;
    state.current.controller?.abort(); state.current.controller = controller; state.current.busy = true; setBusy(kind); setError('');
    const current = () => !controller.signal.aborted && state.current.epoch === epoch;
    try { await action(controller.signal, current); if (current() && mutation) clearUnknown(); }
    catch (error) { if (current()) setError(error instanceof Error ? error.message : String(error)); }
    finally { if (current()) { state.current.busy = false; state.current.controller = null; setBusy(''); } }
  };
  useEffect(() => () => { state.current.epoch += 1; state.current.controller?.abort(); state.current.busy = false; }, []);
  return { needsRead, pending, busy, error, perform, clearUnknown };
}
export type CommunityAction = ReturnType<typeof useCommunityAction>;
export function ActionStatus({ action, unknownMessage = '请明确读取保存记录；结果未知的来源读取须保留原回执，不另起重试。' }: { action: CommunityAction; unknownMessage?: string }) {
  return <>{action.error && <p className="notice error" role="alert">{action.error}</p>}{action.needsRead && <p className="notice" role="status">上次写操作尚未核对。{unknownMessage}刷新页面不会重发。{action.pending && <span className="path-text">意图 <code>{action.pending.intent_id}</code></span>}</p>}{action.busy && <p className="muted small" role="status">正在{action.busy}…</p>}</>;
}
export function ContentSnapshot({ value }: { value: CommunityContent }) {
  return <section className="community-snapshot" aria-label="固定发布内容">
    <h3>{value.title}</h3><p className="preserve-text">{value.summary || '未提供简介。'}</p>
    <dl className="config-list"><div><dt>类别 / 作者声明</dt><dd>{value.category} · {value.author_label}（身份自报）<p className="small preserve-text">{value.author_statement || '未提供作者声明。'}</p></dd></div><div><dt>公开来源</dt><dd>{value.source.repository}<br /><code>{value.source.commit}</code><br />{value.source.path}<br />树 <code>{value.source.tree_sha}</code></dd></div><div><dt>内容摘要</dt><dd><code>{value.content_digest}</code></dd></div><div><dt>分发包 SHA-256</dt><dd><code>{value.archive_sha256}</code></dd></div></dl>
    <details className="community-long-text"><summary>完整指令原文</summary><pre>{value.instructions}</pre></details>
    <section className="community-section"><h4>许可、版权与归属</h4><p className="small">{value.license.status === 'supported' ? '正文可核对' : '仅预览'} · {value.license.spdx ?? '许可未知'}</p>{value.license.reasons.length > 0 && <p className="warning-text small">{value.license.reasons.join('、')}</p>}{value.license.files.map(file => <details className="community-long-text" key={file.path}><summary>{file.path}</summary><p className="small path-text">SHA-256 <code>{file.sha256}</code></p><pre>{file.text}</pre></details>)}</section>
    <details className="community-section"><summary>文件清单与摘要（{value.files.length}）</summary><ul className="file-manifest">{value.files.map(file => <li key={file.path}><strong>{file.path}</strong><span>{file.size.toLocaleString()} bytes · mode {file.mode}</span><code>{file.sha256}</code><code>blob {file.blob_sha}</code></li>)}</ul><p className="small path-text">目录：{value.directories.join('、') || '无子目录'}</p></details>
  </section>;
}
