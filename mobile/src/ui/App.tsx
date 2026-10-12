import { onBackButtonPress } from "@tauri-apps/api/app";
import { getCurrentWindow } from "@tauri-apps/api/window";
import { useCallback, useEffect, useRef, useState } from "react";
import { removePairing, readPairing, type Pairing } from "../storage";
import { checkConnection, GatewayError } from "../transport";
import { AgentScreen } from "./AgentScreen";
import { PairForm } from "./PairForm";
import { SettingsScreen } from "./SettingsScreen";

type Page = "agent" | "settings";

export function App() {
  const [hasPairing, setHasPairing] = useState<boolean | null>(null);
  const [page, setPage] = useState<Page>("agent");
  const [online, setOnline] = useState<boolean | null>(null);
  const [refresh, setRefresh] = useState(0);
  const [sessionsOpen, setSessionsOpen] = useState(false);
  const [hasConversation, setHasConversation] = useState(false);
  const pageRef = useRef(page);
  const sessionsRef = useRef(sessionsOpen);
  const conversationRef = useRef(hasConversation);
  pageRef.current = page;
  sessionsRef.current = sessionsOpen;
  conversationRef.current = hasConversation;

  useEffect(() => {
    let active = true;
    let listener: { unregister: () => Promise<void> } | undefined;
    void onBackButtonPress(({ canGoBack }) => {
      if (sessionsRef.current) {
        setSessionsOpen(false);
      } else if (pageRef.current === "settings") {
        setPage("agent");
      } else if (conversationRef.current) {
        setSessionsOpen(true);
      } else if (canGoBack) {
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
    } catch (error) {
      if (signal?.aborted) return;
      setOnline(false);
      if (error instanceof GatewayError && error.code === "pairing_required") setHasPairing(false);
    }
  }, []);

  useEffect(() => {
    if (!hasPairing) {
      setOnline(null);
      return;
    }
    const controller = new AbortController();
    void probe(controller.signal);
    const timer = window.setInterval(() => void probe(controller.signal), 8_000);
    return () => {
      controller.abort();
      window.clearInterval(timer);
    };
  }, [hasPairing, probe, refresh]);

  const paired = useCallback((_pairing: Pairing) => {
    setHasPairing(true);
    setOnline(true);
    setPage("agent");
    setRefresh((value) => value + 1);
  }, []);

  const unpair = async () => {
    await removePairing();
    setHasPairing(false);
    setOnline(null);
  };

  if (hasPairing === null) return <div className="boot">正在打开 zcharlie…</div>;
  if (!hasPairing) {
    return <PairForm onPaired={paired} />;
  }

  return (
    <div className="app-shell">
      <header className="app-header">
        <div>
          <div className="brand">ZCHARLIE</div>
          <div className="header-title">{page === "agent" ? "AI 对话" : "设置"}</div>
        </div>
        <div className={`connection-pill ${online === false ? "is-offline" : ""}`} aria-live="polite">
          <span className="connection-dot" />
          {online === false ? "连接中断" : online ? "已连接" : "检查连接"}
        </div>
      </header>
      {online === false && (
        <div className="offline-banner" role="status">
          网关暂时不可达，正在自动重试；已保存的对话仍可查看。
        </div>
      )}
      <main className="app-main" key={page}>
        {page === "agent" ? <AgentScreen online={online === true} sessionsOpen={sessionsOpen} onSessionsOpenChange={setSessionsOpen} onHasConversation={setHasConversation} /> : (
          <SettingsScreen onPaired={paired} onUnpair={unpair} />
        )}
      </main>
      <nav className="bottom-nav" aria-label="主导航">
        <button className={page === "agent" ? "active" : ""} onClick={() => setPage("agent")}>AI 对话</button>
        <button className={page === "settings" ? "active" : ""} onClick={() => setPage("settings")}>设置</button>
      </nav>
    </div>
  );
}
