import { onBackButtonPress } from "@tauri-apps/api/app";
import { getCurrentWindow } from "@tauri-apps/api/window";
import { useCallback, useEffect, useRef, useState } from "react";
import { removePairing, readPairing, type Pairing } from "../storage";
import { checkConnection, GatewayError } from "../transport";
import { PairForm } from "./PairForm";
import { SettingsScreen } from "./SettingsScreen";

// 配对完成即进入研究台（内嵌完整 Web UI）；原生壳只承担配对表单、设置页
//（研究台"设备与配对"行经 tauri.localhost/#pairing 进入）与启动跳转。
const CONSOLE_URL = "http://zcharlie.localhost/app";

export function App() {
  const [hasPairing, setHasPairing] = useState<boolean | null>(null);
  const [settingsMode, setSettingsMode] = useState(() => window.location.hash === "#pairing");
  const [online, setOnline] = useState<boolean | null>(null);
  // 网关可达但后端离线（代理 502 BACKEND_OFFLINE）——与网关不可达分开呈现。
  const [backendDown, setBackendDown] = useState(false);
  const settingsRef = useRef(settingsMode);
  settingsRef.current = settingsMode;

  useEffect(() => {
    let active = true;
    let listener: { unregister: () => Promise<void> } | undefined;
    void onBackButtonPress(({ canGoBack }) => {
      // 设置页 BACK 回研究台（前一历史条目）；启动页没有历史可退，退出应用。
      if (settingsRef.current || canGoBack) {
        window.history.back();
      } else {
        void getCurrentWindow().close();
      }
    }).then((registered) => {
      if (active) listener = registered;
      else void registered.unregister();
    }).catch(() => undefined);
    return () => {
      active = false;
      void listener?.unregister();
    };
  }, []);

  useEffect(() => {
    let live = true;
    readPairing()
      .then((pairing) => {
        if (live) setHasPairing(Boolean(pairing));
      })
      .catch(() => {
        if (live) setHasPairing(false);
      });
    return () => {
      live = false;
    };
  }, []);

  const probe = useCallback(async (signal?: AbortSignal) => {
    try {
      await checkConnection(signal);
      setOnline(true);
      setBackendDown(false);
    } catch (error) {
      if (signal?.aborted) return;
      setOnline(false);
      setBackendDown(error instanceof GatewayError && error.code === "backend_offline");
      if (error instanceof GatewayError && error.code === "pairing_required") setHasPairing(false);
    }
  }, []);

  useEffect(() => {
    if (!hasPairing || !settingsMode) return;
    const controller = new AbortController();
    void probe(controller.signal);
    // 探活同样计入网关按 IP 的限流桶，30s 一次足够维持在线指示。
    const timer = window.setInterval(() => void probe(controller.signal), 30_000);
    return () => {
      controller.abort();
      window.clearInterval(timer);
    };
  }, [hasPairing, settingsMode, probe]);

  // 配对完成（或冷启动）且不在设置页：直接进研究台。
  useEffect(() => {
    if (hasPairing && !settingsMode) {
      window.location.href = CONSOLE_URL;
    }
  }, [hasPairing, settingsMode]);

  const paired = useCallback((_pairing: Pairing) => {
    setHasPairing(true);
    setOnline(true);
    setBackendDown(false);
    setSettingsMode(false);
  }, []);

  const unpair = async () => {
    await removePairing();
    setHasPairing(false);
    setOnline(null);
    setBackendDown(false);
  };

  if (hasPairing === null) return <div className="boot">正在打开 Charlie…</div>;
  if (!hasPairing) return <PairForm onPaired={paired} />;
  if (!settingsMode) return <div className="boot">正在打开研究台…</div>;

  return (
    <div className="app-shell">
      <header className="app-header">
        <div>
          <div className="brand">ZCHARLIE</div>
          <div className="header-title">设置</div>
        </div>
        <div className={`connection-pill ${online === false ? "is-offline" : ""}`} aria-live="polite">
          <span className="connection-dot" />
          {online === false ? (backendDown ? "服务离线" : "连接中断") : online ? "已连接" : "检查连接"}
        </div>
      </header>
      {online === false && (
        <div className="offline-banner" role="status">
          {backendDown
            ? "zcharlie 服务暂不可达，正在自动重试；已保存的对话仍可查看。"
            : "网关暂时不可达，正在自动重试；已保存的对话仍可查看。"}
        </div>
      )}
      <main className="app-main">
        <SettingsScreen onPaired={paired} onUnpair={unpair} />
      </main>
    </div>
  );
}
