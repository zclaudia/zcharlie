import { useCallback, useEffect, useRef, useState } from "react";
import { GatewayError, request, watchTask, type AgentEvent, type AgentTask } from "../transport";

type Conversation = {
  id: string;
  title?: string | null;
  created_at: string;
  last_activity_at?: string;
  attention?: string;
  latest_task?: { status: string } | null;
};
type Message = {
  id: string;
  role: "user" | "assistant";
  text: string;
  sequence: number;
  created_at?: string;
  task_ref?: { id: string } | null;
  context_refs?: { kind: string; id: string; revision: number }[];
};
type TaskState = AgentTask & { events: string[]; mode: "sse" | "poll" };

const STATUS: Record<string, string> = {
  queued: "排队中", running: "研究中", waiting_data: "等待材料", waiting_user: "等待你补充",
  succeeded: "已完成", partial: "部分完成", failed: "未完成", cancelled: "已取消",
};
const TERMINAL = new Set(["succeeded", "partial", "failed", "cancelled"]);

function eventLabel(event: AgentEvent): string | null {
  const payload = event.payload ?? {};
  if (event.type === "task_state") {
    const status = String(payload.status ?? "");
    return `任务状态：${STATUS[status] ?? (status || "已更新")}`;
  }
  if (event.type === "progress" || event.type === "step_started") return String(payload.summary ?? "研究进度更新");
  if (event.type === "input_required") return String(payload.question ?? "任务需要补充信息");
  if (event.type === "error") return String(payload.message ?? "任务遇到问题");
  if (event.type === "tool_state") return String(payload.tool_name ? `${payload.tool_name} · ${payload.state}` : "工具状态更新");
  if (event.type === "publication_result") return "已保存研究结果";
  return null;
}

function explainError(error: unknown): string {
  if (error instanceof GatewayError) return error.message;
  return "操作失败，请检查连接后重试。";
}

function shortDate(value?: string) {
  if (!value) return "";
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? "" : date.toLocaleDateString("zh-CN", { month: "numeric", day: "numeric" });
}

export function AgentScreen({
  online,
  sessionsOpen,
  onSessionsOpenChange,
  onHasConversation,
}: {
  online: boolean;
  sessionsOpen: boolean;
  onSessionsOpenChange: (open: boolean) => void;
  onHasConversation: (value: boolean) => void;
}) {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [current, setCurrent] = useState("");
  const [messages, setMessages] = useState<Message[]>([]);
  const [tasks, setTasks] = useState<Record<string, TaskState>>({});
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const bottomRef = useRef<HTMLDivElement>(null);
  const watchers = useRef(new Map<string, AbortController>());

  useEffect(() => onHasConversation(Boolean(current)), [current, onHasConversation]);

  const loadConversations = useCallback(async () => {
    const all: Conversation[] = [];
    let after: string | null = null;
    do {
      const suffix: string = after ? `&after=${encodeURIComponent(after)}` : "";
      const page: Conversation[] = await request<Conversation[]>(`/agent/conversations?limit=100${suffix}`);
      all.push(...page);
      after = page.length === 100 ? page[page.length - 1]?.id ?? null : null;
    } while (after);
    setConversations(all);
    setCurrent((value) => value || all[0]?.id || "");
  }, []);

  const loadMessages = useCallback(async (conversationId: string) => {
    const all: Message[] = [];
    let after = 0;
    while (true) {
      const page = await request<Message[]>(`/agent/conversations/${encodeURIComponent(conversationId)}/messages?after=${after}&limit=100`);
      all.push(...page);
      if (page.length < 100) break;
      const next = page[page.length - 1]?.sequence ?? after;
      if (next <= after) throw new Error("消息游标未前进，请重新加载。");
      after = next;
    }
    setMessages(all);
    return all;
  }, []);

  const stopWatchers = useCallback(() => {
    watchers.current.forEach((controller) => controller.abort());
    watchers.current.clear();
  }, []);

  const watch = useCallback((taskId: string) => {
    if (watchers.current.has(taskId)) return;
    const controller = new AbortController();
    watchers.current.set(taskId, controller);
    setTasks((currentTasks) => ({
      ...currentTasks,
      [taskId]: currentTasks[taskId] ?? { id: taskId, status: "queued", cancel_requested: false, events: [], mode: "sse" },
    }));
    void watchTask(taskId, {
      signal: controller.signal,
      onEvent(event) {
        const label = eventLabel(event);
        if (!label) return;
        setTasks((currentTasks) => {
          const existing = currentTasks[taskId] ?? { id: taskId, status: "running", cancel_requested: false, events: [], mode: "sse" as const };
          return { ...currentTasks, [taskId]: { ...existing, events: [...existing.events, label].slice(-12) } };
        });
      },
      onTask(task) {
        setTasks((currentTasks) => {
          const existing = currentTasks[taskId] ?? { ...task, events: [], mode: "sse" as const };
          return { ...currentTasks, [taskId]: { ...existing, ...task } };
        });
      },
      onSnapshot(snapshot) {
        if (snapshot.messages.length) {
          const taskMessages = snapshot.messages as Message[];
          setMessages((currentMessages) => {
            const merged = new Map(currentMessages.map((message) => [message.id, message]));
            taskMessages.forEach((message) => merged.set(message.id, message));
            return [...merged.values()].sort((a, b) => a.sequence - b.sequence);
          });
        }
      },
      onConnectionMode(mode) {
        setTasks((currentTasks) => {
          const existing = currentTasks[taskId] ?? { id: taskId, status: "running", cancel_requested: false, events: [], mode };
          return { ...currentTasks, [taskId]: { ...existing, mode } };
        });
      },
    }).then(async () => {
      watchers.current.delete(taskId);
      try {
        const task = await request<AgentTask>(`/agent/tasks/${encodeURIComponent(taskId)}`);
        setTasks((currentTasks) => ({
          ...currentTasks,
          [taskId]: { ...(currentTasks[taskId] ?? { events: [], mode: "sse" as const }), ...task },
        }));
        if (TERMINAL.has(task.status) && current) await loadMessages(current);
      } catch {
        // The regular connection probe will surface transport outages in the app header.
      }
    }).catch((reason: unknown) => {
      watchers.current.delete(taskId);
      if (!controller.signal.aborted) setError(explainError(reason));
    });
  }, [current, loadMessages]);

  useEffect(() => {
    void loadConversations().catch((reason: unknown) => setError(explainError(reason)));
    return () => stopWatchers();
  }, [loadConversations, stopWatchers]);

  useEffect(() => {
    if (!current) {
      setMessages([]);
      return;
    }
    stopWatchers();
    let live = true;
    setError("");
    setTasks({});
    void loadMessages(current).then((rows) => {
      if (!live) return;
      rows.forEach((message) => {
        const taskId = message.task_ref?.id;
        if (taskId) watch(taskId);
      });
    }).catch((reason: unknown) => {
      if (live) setError(explainError(reason));
    });
    return () => {
      live = false;
      stopWatchers();
    };
  }, [current, loadMessages, stopWatchers, watch]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages, tasks]);

  async function createConversation() {
    setError("");
    try {
      const conversation = await request<Conversation>("/agent/conversations", {
        method: "POST",
        body: { title: null, context_refs: [] },
      });
      setConversations((rows) => [conversation, ...rows.filter((row) => row.id !== conversation.id)]);
      setMessages([]);
      setCurrent(conversation.id);
      onSessionsOpenChange(false);
    } catch (reason) {
      setError(explainError(reason));
    }
  }

  async function sendMessage() {
    if (!text.trim() || busy || !online) return;
    setBusy(true);
    setError("");
    let conversationId = current;
    try {
      if (!conversationId) {
        const created = await request<Conversation>("/agent/conversations", {
          method: "POST",
          body: { title: text.trim().slice(0, 80), context_refs: [] },
        });
        conversationId = created.id;
        setCurrent(conversationId);
        setConversations((rows) => [created, ...rows]);
      }
      const sentText = text;
      const accepted = await request<{ task: AgentTask }>(`/agent/conversations/${encodeURIComponent(conversationId)}/messages`, {
        method: "POST",
        body: {
          client_message_id: crypto.randomUUID(),
          text: sentText,
          context_refs: [],
          research_kind: "auto",
        },
      });
      setText("");
      await Promise.all([loadMessages(conversationId), loadConversations()]);
      watch(accepted.task.id);
    } catch (reason) {
      setError(explainError(reason));
    } finally {
      setBusy(false);
    }
  }

  async function cancelTask(taskId: string) {
    try {
      const task = await request<AgentTask>(`/agent/tasks/${encodeURIComponent(taskId)}/cancel`, {
        method: "POST",
        body: { idempotency_key: crypto.randomUUID() },
      });
      setTasks((currentTasks) => ({ ...currentTasks, [taskId]: { ...(currentTasks[taskId] ?? { events: [], mode: "sse" as const }), ...task } }));
    } catch (reason) {
      setError(explainError(reason));
    }
  }

  const currentTitle = conversations.find((conversation) => conversation.id === current)?.title || "新对话";

  return (
    <section className="agent-screen">
      <div className="agent-toolbar">
        <button className="secondary-button" onClick={() => onSessionsOpenChange(!sessionsOpen)}>{sessionsOpen ? "返回对话" : "会话列表"}</button>
        <strong>{sessionsOpen ? "最近会话" : currentTitle}</strong>
        <button className="icon-button" aria-label="新建对话" onClick={() => void createConversation()}>＋</button>
      </div>

      {sessionsOpen ? (
        <div className="session-list">
          {!conversations.length && <p className="empty-note">还没有会话，点击右上角开始。</p>}
          {conversations.map((conversation) => (
            <button key={conversation.id} className={`session-row ${current === conversation.id ? "selected" : ""}`} onClick={() => { setCurrent(conversation.id); onSessionsOpenChange(false); }}>
              <span className="session-title">{conversation.title || "未命名对话"}</span>
              <span className="session-meta">{conversation.latest_task ? STATUS[conversation.latest_task.status] ?? conversation.latest_task.status : "对话"} · {shortDate(conversation.last_activity_at ?? conversation.created_at)}</span>
            </button>
          ))}
        </div>
      ) : (
        <>
          {error && <div className="inline-error" role="alert">{error}</div>}
          <div className="message-list" aria-live="polite">
            {!messages.length && <div className="empty-chat"><div className="empty-orbit">✦</div><h2>今天想研究什么？</h2><p>从一个具体问题开始。对话和任务进度会保存在服务端。</p></div>}
            {messages.map((message) => (
              <article className={`message ${message.role === "user" ? "from-user" : "from-agent"}`} key={message.id}>
                {message.role === "assistant" && <span className="message-author">ZCHARLIE · AI</span>}
                <p>{message.text || (message.role === "assistant" ? "研究结果已保存。" : "")}</p>
                {message.context_refs?.length ? <div className="context-chips">{message.context_refs.map((ref) => <span key={`${ref.kind}:${ref.id}`}>{ref.kind} · {ref.id}</span>)}</div> : null}
                {message.task_ref && tasks[message.task_ref.id] && (
                  <TaskCard task={tasks[message.task_ref.id]} onCancel={() => void cancelTask(message.task_ref!.id)} />
                )}
              </article>
            ))}
            {Object.values(tasks).filter((task) => !messages.some((message) => message.task_ref?.id === task.id)).map((task) => <TaskCard key={task.id} task={task} onCancel={() => void cancelTask(task.id)} />)}
            <div ref={bottomRef} />
          </div>
          <form className="composer" onSubmit={(event) => { event.preventDefault(); void sendMessage(); }}>
            <label className="sr-only" htmlFor="agent-prompt">输入问题</label>
            <textarea id="agent-prompt" rows={2} maxLength={32000} placeholder="输入你的研究问题…" value={text} onChange={(event) => setText(event.target.value)} onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); void sendMessage(); }
            }} />
            <div className="composer-foot"><span>Enter 发送 · Shift+Enter 换行</span><button className="primary-button" disabled={!text.trim() || busy || !online}>{busy ? "发送中…" : "发送"}</button></div>
          </form>
          <p className="disclaimer">研究观点包含假设和不确定性；交易决定由你作出。</p>
        </>
      )}
    </section>
  );
}

function TaskCard({ task, onCancel }: { task: TaskState; onCancel: () => void }) {
  const terminal = TERMINAL.has(task.status);
  return (
    <section className={`task-card ${terminal ? "task-terminal" : ""}`}>
      <div className="task-topline"><span className="task-symbol">✦</span><strong>研究任务 · {STATUS[task.status] ?? task.status}</strong><span className={`task-mode ${task.mode}`}>{task.mode === "poll" ? "轮询" : "实时"}</span></div>
      {task.stop_reason && <p className="task-stop">{task.stop_reason}</p>}
      {!!task.events.length && <ol className="task-events">{task.events.map((event, index) => <li key={`${index}:${event}`}>{event}</li>)}</ol>}
      {!terminal && <button className="cancel-button" disabled={task.cancel_requested} onClick={onCancel}>{task.cancel_requested ? "正在停止…" : "取消任务"}</button>}
    </section>
  );
}
