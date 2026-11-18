import { useState, type FormEvent } from "react";
import type { Pairing } from "../storage";
import brandIcon from "../../../src/investment_agent/web/static/brand/icon-180.png";
import { discoverBackends, GatewayError, pairDevice, type ReachableBackend } from "../transport";

type Props = { onPaired: (pairing: Pairing) => void };

export function PairForm({ onPaired }: Props) {
  const [gatewayUrl, setGatewayUrl] = useState("");
  const [backendId, setBackendId] = useState("");
  const [deviceToken, setDeviceToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  // 服务 ID 留空且发现多个在线后端时展示挑选列表（gateway 已按在线优先排序）。
  const [candidates, setCandidates] = useState<ReachableBackend[] | null>(null);

  async function pairWith(backendId: string) {
    setBusy(true);
    setError("");
    try {
      const pairing = await pairDevice({ gatewayUrl, backendId, deviceToken });
      setDeviceToken("");
      onPaired(pairing);
    } catch (reason) {
      setError(reason instanceof GatewayError ? reason.message : "配对失败，请检查输入后重试。");
    } finally {
      setBusy(false);
    }
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!backendId.trim()) {
      setBusy(true);
      setError("");
      try {
        // 组件侧分支用于渲染挑选列表；pairDevice 内部保留同样发现逻辑兜底。
        const reachable = await discoverBackends({ gatewayUrl, deviceToken });
        const online = reachable.filter((b) => b.online);
        if (reachable.length === 0) {
          setError("该凭证没有可寻址的后端，请重新签发设备凭证。");
          return;
        }
        if (online.length === 0) {
          setError("凭证可达的后端当前都离线，请等后端上线后再配对。");
          return;
        }
        if (online.length > 1) {
          setCandidates(reachable);
          return;
        }
        await pairWith(online[0].backendId);
        return;
      } catch (reason) {
        setError(reason instanceof GatewayError ? reason.message : "配对失败，请检查输入后重试。");
        return;
      } finally {
        setBusy(false);
      }
    }
    await pairWith(backendId);
  }

  return (
    <main className="pair-screen">
      <img className="pair-mark" src={brandIcon} width={56} height={56} alt="zcharlie" />
      <p className="eyebrow">ZCHARLIE · ANDROID</p>
      <h1>连接你的研究台</h1>
      <p className="muted">连接后即可访问你的研究台。</p>
      <form className="form-card" onSubmit={submit} aria-busy={busy}>
        <fieldset className="pair-fields" disabled={busy}>
        <label>网关地址
          <input autoCapitalize="none" autoCorrect="off" inputMode="url" placeholder="https://gateway.example.com" value={gatewayUrl} onChange={(e) => { setGatewayUrl(e.target.value); setCandidates(null); setError(""); }} required />
        </label>
        <label>设备凭证
          <input autoCapitalize="none" autoCorrect="off" autoComplete="off" type="password" placeholder="zgd_…" value={deviceToken} onChange={(e) => { setDeviceToken(e.target.value); setCandidates(null); setError(""); }} required />
        </label>
        <details className="pair-advanced">
          <summary>高级设置{backendId.trim() ? " · 已指定服务" : ""}</summary>
          <label>服务 ID（可选）
            <input autoCapitalize="none" autoCorrect="off" placeholder="留空自动发现" value={backendId} onChange={(e) => { setBackendId(e.target.value); setCandidates(null); setError(""); }} aria-describedby="backend-help" />
          </label>
          <p id="backend-help" className="field-help">默认自动发现可连接的研究台；仅在管理员要求时指定服务 ID。</p>
        </details>
        {candidates && !backendId.trim() && (
          <div className="backend-picker" role="group" aria-label="选择研究台">
            <p className="field-help" role="status">发现多个研究台，请选择要连接的一项。</p>
            {candidates.map((backend) => (
              <button
                key={backend.backendId}
                type="button"
                className="backend-row"
                disabled={busy || !backend.online}
                onClick={() => void pairWith(backend.backendId)}
              >
                <span className="backend-name">{backend.name || backend.backendId.slice(0, 8)}</span>
                <span className={`backend-state ${backend.online ? "is-online" : "is-offline"}`}>
                  {backend.online ? "在线" : "离线"}
                </span>
              </button>
            ))}
          </div>
        )}
        {error && <p className="form-error" role="alert">{error}</p>}
        <button className="primary-button" type="submit" disabled={busy}>{busy ? "正在验证…" : "验证并连接"}</button>
        </fieldset>
      </form>
      <p className="fine-print">设备凭证由网关管理员提供，仅保存在本机应用私有存储。后端 API 密钥不会保存到此设备。</p>
    </main>
  );
}
