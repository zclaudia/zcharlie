import { useMemo, useState, type FormEvent } from "react";
import useSWR from "swr";
import type { Pairing } from "../storage";
import { readPairing } from "../storage";
import { GatewayError, request } from "../transport";
import { PairForm } from "./PairForm";

type SettingItem = {
  key: string;
  label: string;
  help: string;
  apply: string;
  secret: boolean;
  configured?: boolean;
  env_fallback?: boolean;
  hint?: string;
  value?: unknown;
  default?: unknown;
  input: { type: string; options?: string[]; min?: number; max?: number; nullable?: boolean };
};
type SettingsData = {
  revision: number;
  encryption_key_configured: boolean;
  groups: { key: string; title: string; items: SettingItem[] }[];
  bootstrap: { key: string; label: string; value?: unknown; configured?: boolean; hint?: string }[];
};
type HistoryData = { items: { id: number; label?: string; key: string; changed_at: string; actor: string; note?: string }[] };

type Props = { onPaired: (pairing: Pairing) => void; onUnpair: () => Promise<void> };

function shownValue(value: unknown): string {
  if (value === null || value === undefined || value === "") return "未设置";
  if (typeof value === "boolean") return value ? "开启" : "关闭";
  if (Array.isArray(value)) return value.join("、") || "未选择";
  return String(value);
}

function inputValue(item: SettingItem): unknown {
  return item.secret ? undefined : item.value;
}

function applyName(value: string) {
  return ({ live: "立即生效", next_job: "下个任务生效", reschedule: "重新排期" } as Record<string, string>)[value] ?? value;
}

export function SettingsScreen({ onPaired, onUnpair }: Props) {
  const options = { revalidateOnFocus: false, revalidateOnReconnect: false, shouldRetryOnError: false };
  const { data: pairing, error: pairingError, isLoading: pairingLoading, mutate: updatePairing } = useSWR("native-pairing", readPairing, options);
  const scope = pairing ? [pairing.gatewayUrl, pairing.backendId] : null;
  const { data, error: loadError, isLoading, mutate: updateSettings } = useSWR(scope ? ["settings", ...scope] : null, () => request<SettingsData>("/settings"), options);
  const { data: history, error: historyError, isLoading: historyLoading, mutate: updateHistory } = useSWR(scope ? ["settings-history", ...scope] : null, () => request<HistoryData>("/settings/history?limit=30"), options);
  const [editingPairing, setEditingPairing] = useState(false);
  const [confirmUnpair, setConfirmUnpair] = useState(false);
  const [unpairing, setUnpairing] = useState(false);
  const [unpairError, setUnpairError] = useState("");
  const [edits, setEdits] = useState<Record<string, unknown>>({});
  const [cleared, setCleared] = useState<Set<string>>(new Set());
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ tone: "success" | "error"; text: string } | null>(null);

  const pendingValues = useMemo(() => {
    const values: Record<string, unknown> = {};
    for (const item of data?.groups.flatMap((group) => group.items) ?? []) {
      if (cleared.has(item.key)) values[item.key] = null;
      else if (Object.hasOwn(edits, item.key) && (item.secret ? edits[item.key] !== "" : JSON.stringify(edits[item.key]) !== JSON.stringify(item.value))) values[item.key] = edits[item.key];
    }
    return values;
  }, [data, edits, cleared]);
  const dirtyCount = Object.keys(pendingValues).length;
  const change = (key: string, value: unknown) => {
    setEdits((current) => ({ ...current, [key]: value }));
    setCleared((current) => { const next = new Set(current); next.delete(key); return next; });
    setMessage(null);
  };
  const discard = () => { setEdits({}); setCleared(new Set()); setNote(""); setMessage(null); };

  async function save(event: FormEvent) {
    event.preventDefault();
    if (!dirtyCount || busy) return;
    setBusy(true);
    setMessage(null);
    try {
      const result = await request<{ changed: string[]; settings: SettingsData }>("/settings", {
        method: "PATCH",
        body: { values: pendingValues, note: note.trim() || null },
      });
      await updateSettings(result.settings, { revalidate: false });
      setEdits({});
      setCleared(new Set());
      setNote("");
      setMessage({ tone: "success", text: `已保存 ${result.changed.length} 项设置。` });
      void updateHistory().catch(() => undefined);
    } catch (error) {
      setMessage({ tone: "error", text: error instanceof GatewayError ? error.message : "设置保存失败，修改已保留，请重试。" });
    } finally {
      setBusy(false);
    }
  }

  async function unpair() {
    if (unpairing) return;
    setUnpairing(true);
    setUnpairError("");
    try {
      await onUnpair();
      await updatePairing(null, { revalidate: false });
      await updateSettings(undefined, { revalidate: false });
      await updateHistory(undefined, { revalidate: false });
      discard();
      setConfirmUnpair(false);
    } catch {
      setUnpairError("解除配对失败，当前连接未切换，请重试。");
    } finally {
      setUnpairing(false);
    }
  }

  if (pairingLoading) return <div className="loading-card" role="status">正在读取连接信息…</div>;
  if (pairingError) return <div className="error-card" role="alert"><p>无法读取本机连接信息，请重试。</p><button className="secondary-button" onClick={() => void updatePairing().catch(() => undefined)}>重试读取连接</button></div>;

  if (editingPairing || !pairing) {
    return (
      <section className="settings-page">
        <div className="section-heading"><h1>连接设置</h1><p>修改网关或设备凭证后会验证新连接，再替换本机保存的信息。</p></div>
        <PairForm onPaired={(value) => { discard(); void updatePairing(value, { revalidate: false }); setEditingPairing(false); onPaired(value); }} />
        {pairing && <button className="text-button" onClick={() => setEditingPairing(false)}>返回设置</button>}
      </section>
    );
  }

  return (
    <section className="settings-page">
      <div className="section-heading"><h1>连接设置</h1><p>设备凭证保存在应用私有数据目录，不会放入网页存储或日志。</p></div>
      <div className="connection-card">
        <div><span className="field-label">网关</span><strong>{pairing.gatewayUrl}</strong></div>
        <div><span className="field-label">服务 ID</span><strong className="mono">{pairing.backendId}</strong></div>
        <div><span className="field-label">设备凭证</span><strong>已保存到应用私有存储</strong></div>
        <div className="button-row">
          <button className="secondary-button" disabled={busy || unpairing || dirtyCount > 0} onClick={() => setEditingPairing(true)}>重新配对</button>
          <button className="text-button danger-button" disabled={busy || unpairing} aria-expanded={confirmUnpair} aria-controls="unpair-confirmation" onClick={() => { setConfirmUnpair(true); setUnpairError(""); }}>解除配对</button>
        </div>
        {dirtyCount > 0 && <p className="field-help">重新配对前，请先保存或撤销修改。</p>}
        {confirmUnpair && <section id="unpair-confirmation" className="unpair-confirmation" aria-labelledby="unpair-title">
          <h2 id="unpair-title">确定解除此设备的配对？</h2>
          <p>将清除本机连接信息和未保存的修改，不会删除研究台数据。再次连接需要重新输入设备凭证。</p>
          <div className="button-row">
            <button autoFocus className="secondary-button" disabled={unpairing} onClick={() => setConfirmUnpair(false)}>取消</button>
            <button className="secondary-button danger-button" disabled={unpairing} onClick={() => void unpair()}>{unpairing ? "正在解除…" : "确认解除配对"}</button>
          </div>
          {unpairError && <p role="alert">{unpairError}</p>}
        </section>}
      </div>

      <div className="section-heading settings-heading"><h2>运行参数</h2><p>修改后由服务端校验并保存。密钥值不会从服务端返回。</p></div>
      {loadError && <div className="error-card" role="alert"><strong>设置读取失败</strong><p>{loadError instanceof GatewayError ? loadError.message : "请检查连接后重试。"}</p><button type="button" className="secondary-button" onClick={() => void updateSettings().catch(() => undefined)}>重试读取设置</button></div>}
      {isLoading && !loadError && <div className="loading-card" role="status">正在读取设置…</div>}
      {data && (
        <form id="runtime-settings" onSubmit={save} aria-busy={busy}>
          <fieldset className="settings-fields" disabled={busy || unpairing || confirmUnpair}>
          {data.groups.map((group) => (
            <fieldset className="settings-group" key={group.key}>
              <legend>{group.title}</legend>
              {group.items.map((item) => {
                const edited = Object.hasOwn(edits, item.key);
                const value = edited ? edits[item.key] : inputValue(item);
                return (
                  <div className="setting-field" key={item.key}>
                    <div className="setting-label-row"><label htmlFor={`setting-${item.key}`}>{item.label}</label><span>{applyName(item.apply)}</span></div>
                    {item.secret ? (
                      <div className="secret-row">
                        <input id={`setting-${item.key}`} type="password" autoComplete="new-password" placeholder={item.configured || item.env_fallback ? `已设置${item.hint ? ` · ${item.hint}` : ""}；留空保持不变` : "请输入密钥"} value={String(edits[item.key] ?? "")} onChange={(e) => change(item.key, e.target.value)} disabled={!data.encryption_key_configured} />
                        {(item.configured || item.env_fallback) && !cleared.has(item.key) && <button type="button" className="text-button compact danger-button" disabled={!data.encryption_key_configured} onClick={() => { setCleared((current) => new Set(current).add(item.key)); setMessage(null); }}>清除</button>}
                        {cleared.has(item.key) && <button type="button" className="text-button compact" onClick={() => setCleared((current) => { const next = new Set(current); next.delete(item.key); return next; })}>撤销清除</button>}
                      </div>
                    ) : typeof item.default === "boolean" ? (
                      <label className="switch-row"><input id={`setting-${item.key}`} type="checkbox" checked={Boolean(value)} onChange={(e) => change(item.key, e.target.checked)} /><span>{value ? "开启" : "关闭"}</span></label>
                    ) : item.input.type === "select" ? (
                      <select id={`setting-${item.key}`} value={String(value ?? "")} onChange={(e) => change(item.key, e.target.value)}>{(item.input.options ?? []).map((option) => <option key={option} value={option}>{option}</option>)}</select>
                    ) : item.input.type === "multi" ? (
                      <div className="multi-options">{(item.input.options ?? []).map((option) => {
                        const selected = Array.isArray(value) ? value.includes(option) : false;
                        return <label key={option}><input type="checkbox" checked={selected} onChange={(e) => {
                          const current = Array.isArray(value) ? value as string[] : [];
                          change(item.key, e.target.checked ? [...current, option] : current.filter((entry) => entry !== option));
                        }} />{option}</label>;
                      })}</div>
                    ) : (
                      <input id={`setting-${item.key}`} type={item.input.type === "int" || item.input.type === "float" ? "number" : item.input.type === "url" ? "url" : "text"} inputMode={item.input.type === "int" ? "numeric" : item.input.type === "float" ? "decimal" : undefined} min={item.input.min} max={item.input.max} step={item.input.type === "int" ? 1 : item.input.type === "float" ? "any" : undefined} value={String(value ?? "")} onChange={(e) => change(item.key, item.input.type === "int" ? (e.target.value === "" ? null : Number.parseInt(e.target.value, 10)) : item.input.type === "float" ? (e.target.value === "" ? null : Number.parseFloat(e.target.value)) : e.target.value)} />
                    )}
                    {cleared.has(item.key) && <p className="field-help danger-button">此密钥将在保存后清除，可在保存前撤销。</p>}
                    {item.help && <p className="field-help">{item.help}</p>}
                    <p className="field-value">当前：{item.secret ? item.configured || item.env_fallback ? `已设置${item.hint ? ` · ${item.hint}` : ""}` : "未设置" : shownValue(item.value)}</p>
                  </div>
                );
              })}
            </fieldset>
          ))}
          <label className="note-field">变更说明（可选）<input maxLength={500} value={note} onChange={(e) => setNote(e.target.value)} placeholder="例如：调整研究任务频率" /></label>
          </fieldset>
        </form>
      )}

      <div className="section-heading settings-heading"><h2>最近变更</h2></div>
      {historyError ? <div className="error-card" role="alert"><p>变更记录暂时无法读取，不影响已保存的设置。</p><button className="secondary-button" onClick={() => void updateHistory().catch(() => undefined)}>重试读取记录</button></div> : historyLoading ? <p className="field-help" role="status">正在读取变更记录…</p> : !history?.items.length ? <p className="empty-history">暂无变更记录，保存设置后会在这里显示。</p> : null}
      <div className="history-list">{history?.items.map((item) => <article key={item.id}><strong>{item.label ?? item.key}</strong><span>{new Date(item.changed_at).toLocaleString("zh-CN")}</span>{item.note && <p>{item.note}</p>}</article>)}</div>
      {data?.bootstrap?.length ? <details className="bootstrap-panel"><summary>启动配置（只读）</summary>{data.bootstrap.map((item) => <div key={item.key}><span>{item.label}</span><span>{item.configured ? `已设置${item.hint ? ` · ${item.hint}` : ""}` : shownValue(item.value)}</span></div>)}</details> : null}
      {data && <div className="settings-savebar">
        {message && <p className={`inline-status is-${message.tone}`} role={message.tone === "error" ? "alert" : "status"}><strong>{message.tone === "error" ? "保存失败：" : "保存成功："}</strong>{message.text}</p>}
        <div className="settings-savebar-actions">
          <span aria-live="polite">{dirtyCount ? `${dirtyCount} 项待保存` : "所有修改已保存"}</span>
          {dirtyCount > 0 && <button type="button" className="text-button" disabled={busy || unpairing || confirmUnpair} onClick={discard}>撤销修改</button>}
          <button className="primary-button" form="runtime-settings" type="submit" disabled={busy || unpairing || confirmUnpair || dirtyCount === 0}>{busy ? "正在保存…" : "保存设置"}</button>
        </div>
      </div>}
    </section>
  );
}
