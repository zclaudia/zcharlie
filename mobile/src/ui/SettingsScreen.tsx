import { useEffect, useMemo, useState, type FormEvent } from "react";
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
  const [pairing, setPairing] = useState<Pairing | null>(null);
  const [editingPairing, setEditingPairing] = useState(false);
  const [data, setData] = useState<SettingsData | null>(null);
  const [history, setHistory] = useState<HistoryData["items"]>([]);
  const [edits, setEdits] = useState<Record<string, unknown>>({});
  const [cleared, setCleared] = useState<Set<string>>(new Set());
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");

  async function reload() {
    try {
      const [settings, changes] = await Promise.all([
        request<SettingsData>("/settings"),
        request<HistoryData>("/settings/history?limit=30"),
      ]);
      setData(settings);
      setHistory(changes.items);
      setEdits({});
      setCleared(new Set());
    } catch (error) {
      setMessage(error instanceof GatewayError ? error.message : "设置读取失败，请重试。");
    }
  }

  useEffect(() => {
    let alive = true;
    readPairing().then((value) => { if (alive) setPairing(value); }).catch(() => undefined);
    void reload();
    return () => { alive = false; };
  }, []);

  const dirtyCount = useMemo(() => Object.keys(edits).length + cleared.size, [edits, cleared]);
  const change = (key: string, value: unknown) => setEdits((current) => ({ ...current, [key]: value }));

  async function save(event: FormEvent) {
    event.preventDefault();
    const secretKeys = new Set(data?.groups.flatMap((group) => group.items.filter((item) => item.secret).map((item) => item.key)) ?? []);
    const values: Record<string, unknown> = Object.fromEntries(
      Object.entries(edits).filter(([key, value]) => !(secretKeys.has(key) && value === "")),
    );
    cleared.forEach((key) => { values[key] = null; });
    if (!Object.keys(values).length) return;
    setBusy(true);
    setMessage("");
    try {
      const result = await request<{ changed: string[]; settings: SettingsData }>("/settings", {
        method: "PATCH",
        body: { values, note: note.trim() || null },
      });
      setData(result.settings);
      setEdits({});
      setCleared(new Set());
      setNote("");
      setMessage(`已保存 ${result.changed.length} 项设置。`);
      const changes = await request<HistoryData>("/settings/history?limit=30");
      setHistory(changes.items);
    } catch (error) {
      setMessage(error instanceof GatewayError ? error.message : "设置保存失败，请重试。");
    } finally {
      setBusy(false);
    }
  }

  async function unpair() {
    await onUnpair();
    setPairing(null);
  }

  if (editingPairing || !pairing) {
    return (
      <section className="settings-page">
        <div className="section-heading"><h1>连接设置</h1><p>修改网关或设备凭证后会验证新连接，再替换本机保存的信息。</p></div>
        <PairForm onPaired={(value) => { setPairing(value); setEditingPairing(false); onPaired(value); }} />
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
          <button className="secondary-button" onClick={() => setEditingPairing(true)}>重新配对</button>
          <button className="text-button" onClick={() => void unpair()}>解除配对</button>
        </div>
      </div>

      <div className="section-heading settings-heading"><h1>运行参数</h1><p>修改后由服务端校验并保存。密钥值不会从服务端返回。</p></div>
      {message && <p className="inline-status" role="status">{message}</p>}
      {!data ? <div className="loading-card">正在读取设置…</div> : (
        <form onSubmit={save}>
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
                        {(item.configured || item.env_fallback) && <button type="button" className="text-button compact" onClick={() => setCleared((current) => new Set(current).add(item.key))}>清除</button>}
                        {cleared.has(item.key) && <button type="button" className="text-button compact" onClick={() => setCleared((current) => { const next = new Set(current); next.delete(item.key); return next; })}>撤销清除</button>}
                      </div>
                    ) : typeof item.default === "boolean" ? (
                      <label className="switch-row"><input type="checkbox" checked={Boolean(value)} onChange={(e) => change(item.key, e.target.checked)} /><span>{value ? "开启" : "关闭"}</span></label>
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
                    {item.help && <p className="field-help">{item.help}</p>}
                    <p className="field-value">当前：{item.secret ? item.configured || item.env_fallback ? `已设置${item.hint ? ` · ${item.hint}` : ""}` : "未设置" : shownValue(item.value)}</p>
                  </div>
                );
              })}
            </fieldset>
          ))}
          <label className="note-field">变更说明（可选）<input maxLength={500} value={note} onChange={(e) => setNote(e.target.value)} placeholder="例如：调整研究任务频率" /></label>
          <button className="primary-button save-settings" type="submit" disabled={busy || dirtyCount === 0}>{busy ? "正在保存…" : dirtyCount ? `保存 ${dirtyCount} 项` : "没有待保存的修改"}</button>
        </form>
      )}

      <div className="section-heading settings-heading"><h1>最近变更</h1></div>
      <div className="history-list">{history.map((item) => <article key={item.id}><strong>{item.label ?? item.key}</strong><span>{new Date(item.changed_at).toLocaleString("zh-CN")}</span>{item.note && <p>{item.note}</p>}</article>)}</div>
      {data?.bootstrap?.length ? <details className="bootstrap-panel"><summary>启动配置（只读）</summary>{data.bootstrap.map((item) => <div key={item.key}><span>{item.label}</span><span>{item.configured ? `已设置${item.hint ? ` · ${item.hint}` : ""}` : shownValue(item.value)}</span></div>)}</details> : null}
    </section>
  );
}
