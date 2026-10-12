import { useState, type FormEvent } from "react";
import type { Pairing } from "../storage";
import { GatewayError, pairDevice } from "../transport";

type Props = { onPaired: (pairing: Pairing) => void };

export function PairForm({ onPaired }: Props) {
  const [gatewayUrl, setGatewayUrl] = useState("");
  const [backendId, setBackendId] = useState("");
  const [deviceToken, setDeviceToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function submit(event: FormEvent) {
    event.preventDefault();
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

  return (
    <main className="pair-screen">
      <div className="pair-mark">Z</div>
      <p className="eyebrow">ZCHARLIE · ANDROID</p>
      <h1>连接你的研究台</h1>
      <p className="muted">输入网关地址、服务 ID 和设备凭证。凭证只保存在本机应用数据中。</p>
      <form className="form-card" onSubmit={submit}>
        <label>网关地址
          <input autoCapitalize="none" autoCorrect="off" inputMode="url" placeholder="https://gateway.example.com" value={gatewayUrl} onChange={(e) => setGatewayUrl(e.target.value)} required />
        </label>
        <label>服务 ID
          <input autoCapitalize="none" autoCorrect="off" placeholder="设置页显示的服务 ID" value={backendId} onChange={(e) => setBackendId(e.target.value)} required />
        </label>
        <label>设备凭证
          <input autoCapitalize="none" autoCorrect="off" autoComplete="off" type="password" placeholder="zgd_…" value={deviceToken} onChange={(e) => setDeviceToken(e.target.value)} required />
        </label>
        {error && <p className="form-error" role="alert">{error}</p>}
        <button className="primary-button" type="submit" disabled={busy}>{busy ? "正在验证…" : "验证并连接"}</button>
      </form>
      <p className="fine-print">设备凭证由网关管理员签发。zcharlie 不会读取或保存后端 API 密钥。</p>
    </main>
  );
}
