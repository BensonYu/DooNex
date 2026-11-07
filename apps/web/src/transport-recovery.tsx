import { useEffect, useRef, useState } from 'react';
import { Api, type TransportInspection } from './api';

type Helpers = { items: { action_id: string; run_id: string }[]; next_cursor: string | null };

export function TransportRecovery({ api }: { api: Api }) {
  const [list, setList] = useState<Helpers | null>(null);
  const [detail, setDetail] = useState<TransportInspection | null>(null);
  const [confirmed, setConfirmed] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const operation = useRef({ generation: 0, controller: null as AbortController | null });
  const confirmationKey = detail?.record_sha256 ? `${detail.action_id}:${detail.record_sha256}` : '';
  useEffect(() => {
    setList(null); setDetail(null); setConfirmed(''); setBusy(false); setError(''); setNotice('');
    return () => { operation.current.generation += 1; operation.current.controller?.abort(); };
  }, [api]);

  const perform = async (work: (signal: AbortSignal, current: () => boolean) => Promise<void>) => {
    operation.current.controller?.abort();
    const generation = ++operation.current.generation;
    const controller = new AbortController();
    operation.current.controller = controller;
    const current = () => generation === operation.current.generation && !controller.signal.aborted;
    setBusy(true); setError(''); setNotice(''); setConfirmed('');
    try { await work(controller.signal, current); }
    catch (failure) { if (current()) setError(failure instanceof Error ? failure.message : String(failure)); }
    finally { if (current()) setBusy(false); }
  };

  return <details className="section-block" onToggle={(event) => {
    if (event.currentTarget.open && !list && !busy) void perform(async (signal, current) => {
      const result = await api.get<Helpers>('/transport-helpers', signal);
      if (current()) setList(result);
    });
  }}>
    <summary>模型传输恢复核查</summary>
    <p>未确认退出的独立传输进程会阻止后续执行。先停止执行服务，再检查记录；确认只解除本机进程阻止，未知用量和原预算继续保留。</p>
    <button disabled={busy} onClick={() => { void perform(async (signal, current) => {
      const result = await api.get<Helpers>('/transport-helpers', signal);
      if (current()) { setList(result); setDetail(null); }
    }); }}>刷新待核查记录</button>
    {error && <p role="alert" className="attention">{error}</p>}
    {notice && <p role="status">{notice}</p>}
    {list && !list.items.length && <p>没有待核查的模型传输记录。</p>}
    {list && <ul className="attempt-list">{list.items.map((item) => <li key={item.action_id}>
      <div><strong>运行 {item.run_id}</strong><code>{item.action_id}</code></div>
      <button disabled={busy} onClick={() => { void perform(async (signal, current) => {
        const result = await api.get<TransportInspection>(`/transport-helpers/${item.action_id}`, signal);
        if (current()) setDetail(result);
      }); }}>核查此记录</button>
    </li>)}</ul>}
    {list?.next_cursor && <button disabled={busy} onClick={() => { void perform(async (signal, current) => {
      const result = await api.get<Helpers>(`/transport-helpers?after_id=${encodeURIComponent(list.next_cursor!)}`, signal);
      if (current()) { setList(result); setDetail(null); }
    }); }}>下一页</button>}
    {detail && <section className="recovery-panel" aria-label="传输进程核查结果">
      <h3>核查结果</h3>
      <p>记录：{detail.valid ? '绑定有效' : '绑定无效'}；本机进程锁：{detail.lease ? detail.lease.locked ? '仍被占用' : '当前空闲' : '无法核实'}；{detail.unresolved ? '仍阻止执行' : '已解除本机阻止'}。</p>
      {detail.problems.length > 0 && <p className="attention">核查问题：{detail.problems.join('、')}</p>}
      <details><summary>绑定与进程证据</summary><pre>{JSON.stringify(detail, null, 2)}</pre></details>
      <p className="field-help">服务端会再次检查执行服务已停止、同一记录版本和同一个空闲进程锁。不会使用旧 PID 杀进程，也不会认定远端请求或计费已结束。</p>
      <label className="checkbox-label"><input type="checkbox" disabled={busy} checked={!!confirmationKey && confirmed === confirmationKey} onChange={(event) => setConfirmed(event.target.checked ? confirmationKey : '')} />我已核对本机进程证据，并确认保留未知用量。</label>
      <button disabled={busy || !confirmationKey || confirmed !== confirmationKey || !detail.valid || !detail.unresolved || !detail.lease || detail.lease.locked || !detail.record_sha256} onClick={() => {
        if (!detail.record || !detail.lease) return;
        const body = { expected_record_sha256: detail.record_sha256, attempt_id: detail.record.attempt_id, nonce: detail.record.nonce, lease_identity: { device: detail.lease.device, inode: detail.lease.inode }, reason: 'operator_confirmed_local_lease' };
        void perform(async (signal, current) => {
          const result = await api.post<TransportInspection>(`/transport-helpers/${detail.action_id}/resolve`, body, signal);
          if (current()) { setDetail(result); setList(null); setNotice('已记录人工核查。未知用量仍保留；重新启动执行服务后，再按原运行的恢复条件处理。'); }
        });
      }}>确认解除本机传输阻止</button>
    </section>}
  </details>;
}
