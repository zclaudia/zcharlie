import { invoke } from "@tauri-apps/api/core";
import { fetch as nativeFetch } from "@tauri-apps/plugin-http";
import { readPairing, savePairing, type Pairing } from "./storage";

export type FetchLike = typeof nativeFetch;
export type AgentEvent = {
  event_id: number;
  task_id: string;
  type: string;
  payload: Record<string, unknown>;
};
export type AgentTask = {
  id: string;
  status: string;
  cancel_requested: boolean;
  stop_reason?: string | null;
};
export type TaskSnapshot = {
  task: AgentTask;
  event_cursor: number;
  messages: unknown[];
};

export class GatewayError extends Error {
  constructor(
    readonly status: number,
    message: string,
    readonly code?: string,
  ) {
    super(message);
    this.name = "GatewayError";
  }
}

const REQUEST_TIMEOUT_MS = 30_000;
const STREAM_IDLE_TIMEOUT_MS = 60_000;
const TERMINAL = new Set(["succeeded", "partial", "failed", "cancelled"]);

function cleanGatewayUrl(value: string): string {
  let parsed: URL;
  try {
    parsed = new URL(value.trim());
  } catch {
    throw new GatewayError(0, "请输入有效的网关地址。", "invalid_gateway");
  }
  if (!(["http:", "https:"].includes(parsed.protocol)) || parsed.username || parsed.password) {
    throw new GatewayError(0, "网关地址必须使用 HTTP 或 HTTPS，且不能包含账号密码。", "invalid_gateway");
  }
  if (parsed.pathname !== "/" || parsed.search || parsed.hash) {
    throw new GatewayError(0, "请输入网关主机地址，不要附加路径或查询参数。", "invalid_gateway");
  }
  return parsed.origin;
}

export function proxyUrl(gatewayUrl: string, backendId: string, path: string): string {
  if (!path.startsWith("/") || path.startsWith("//") || path.includes("\\")) {
    throw new GatewayError(0, "API 路径格式无效。", "invalid_path");
  }
  const base = `${cleanGatewayUrl(gatewayUrl)}/api/proxy/${encodeURIComponent(backendId)}`;
  const result = new URL(`${base}${path}`);
  if (result.origin !== new URL(base).origin || !result.pathname.startsWith(`${new URL(base).pathname}/`)) {
    throw new GatewayError(0, "API 路径超出当前服务范围。", "invalid_path");
  }
  return result.toString();
}

function classifyStatus(status: number): never {
  if (status === 401 || status === 403) {
    throw new GatewayError(status, "设备凭证无效或已过期，请重新配对。", "invalid_device_token");
  }
  if (status === 502 || status === 504) {
    throw new GatewayError(status, "服务暂时离线，请检查 zcharlie 服务状态。", "backend_offline");
  }
  throw new GatewayError(status, `请求失败（HTTP ${status}）。`, "http_error");
}

function headersFor(pairing: Pairing, accept = "application/json", body?: unknown): Headers {
  const headers = new Headers({
    Accept: accept,
    Authorization: `Bearer ${pairing.deviceToken}`,
  });
  if (body !== undefined) headers.set("Content-Type", "application/json");
  return headers;
}

function combineSignal(parent?: AbortSignal, controller = new AbortController()): AbortSignal {
  if (parent?.aborted) controller.abort(parent.reason);
  else parent?.addEventListener("abort", () => controller.abort(parent.reason), { once: true });
  return controller.signal;
}

async function nativeRequest(
  pairing: Pairing,
  path: string,
  init: {
    method?: string;
    body?: unknown;
    accept?: string;
    signal?: AbortSignal;
    fetchImpl?: FetchLike;
    timeoutMs?: number;
  } = {},
): Promise<Response> {
  const controller = new AbortController();
  const signal = combineSignal(init.signal, controller);
  const timeout = setTimeout(() => controller.abort(), init.timeoutMs ?? REQUEST_TIMEOUT_MS);
  const fetchImpl = init.fetchImpl ?? nativeFetch;
  try {
    const response = await fetchImpl(proxyUrl(pairing.gatewayUrl, pairing.backendId, path), {
      method: init.method ?? "GET",
      headers: headersFor(pairing, init.accept, init.body),
      body: init.body === undefined ? undefined : JSON.stringify(init.body),
      credentials: "omit",
      signal,
      connectTimeout: init.timeoutMs ?? REQUEST_TIMEOUT_MS,
    });
    return response;
  } catch {
    if (init.signal?.aborted) throw new GatewayError(0, "请求已取消。", "cancelled");
    throw new GatewayError(0, "网关不可达或请求超时，请检查地址与网络。", "gateway_unreachable");
  } finally {
    clearTimeout(timeout);
  }
}

async function responseData<T>(response: Response): Promise<T> {
  if (!response.ok) classifyStatus(response.status);
  if (response.status === 204) return null as T;
  const contentType = response.headers.get("content-type") ?? "";
  if (contentType.includes("application/json")) return (await response.json()) as T;
  return (await response.text()) as T;
}

export async function request<T = unknown>(
  path: string,
  init: { method?: string; body?: unknown; signal?: AbortSignal; fetchImpl?: FetchLike } = {},
): Promise<T> {
  const pairing = await readPairing();
  if (!pairing) throw new GatewayError(0, "请先完成设备配对。", "pairing_required");
  const response = await nativeRequest(pairing, path, init);
  return responseData<T>(response);
}

export async function pairDevice(
  input: { gatewayUrl: string; backendId: string; deviceToken: string },
  fetchImpl?: FetchLike,
): Promise<Pairing> {
  const gatewayUrl = await validateGatewayUrl(input.gatewayUrl);
  const backendId = input.backendId.trim();
  const deviceToken = input.deviceToken.trim();
  if (!backendId) throw new GatewayError(0, "请输入服务 ID。", "invalid_backend_id");
  if (!deviceToken.startsWith("zgd_") || deviceToken.length < 8 || /\s/.test(deviceToken)) {
    throw new GatewayError(0, "设备凭证格式无效，应以 zgd_ 开头。", "invalid_device_token");
  }
  const pairing = { gatewayUrl, backendId, deviceToken };
  const response = await nativeRequest(pairing, "/healthz", { fetchImpl });
  if (response.status === 401 || response.status === 403) classifyStatus(response.status);
  if (response.status === 502 || response.status === 504) classifyStatus(response.status);
  if (!response.ok) classifyStatus(response.status);
  await savePairing(pairing);
  return pairing;
}

export async function isDebugBuild(): Promise<boolean> {
  try {
    return await invoke<boolean>("is_debug_build");
  } catch {
    return false;
  }
}

export async function validateGatewayUrl(value: string): Promise<string> {
  const gatewayUrl = cleanGatewayUrl(value);
  if (new URL(gatewayUrl).protocol === "http:" && !(await isDebugBuild())) {
    throw new GatewayError(0, "正式构建只能连接 HTTPS 网关。", "https_required");
  }
  return gatewayUrl;
}

export async function checkConnection(signal?: AbortSignal): Promise<void> {
  await request("/healthz", { signal });
}

export function parseSseFrame(frame: string):
  | { kind: "event"; event: AgentEvent; id?: number }
  | { kind: "cursor_expired"; detail: unknown }
  | null {
  let id: number | undefined;
  let eventName = "message";
  const data: string[] = [];
  for (const line of frame.split(/\r?\n/)) {
    if (!line || line.startsWith(":")) continue;
    const separator = line.indexOf(":");
    const field = separator < 0 ? line : line.slice(0, separator);
    const value = separator < 0 ? "" : line.slice(separator + 1).replace(/^ /, "");
    if (field === "id" && /^\d+$/.test(value)) id = Number(value);
    if (field === "event") eventName = value;
    if (field === "data") data.push(value);
  }
  if (!data.length) return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(data.join("\n"));
  } catch {
    return null;
  }
  if (eventName === "cursor_expired") return { kind: "cursor_expired", detail: parsed };
  if (!parsed || typeof parsed !== "object") return null;
  return { kind: "event", event: parsed as AgentEvent, id };
}

function delay(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    const timer = setTimeout(resolve, ms);
    signal.addEventListener("abort", () => {
      clearTimeout(timer);
      resolve();
    }, { once: true });
  });
}

async function readSnapshot(taskId: string, signal: AbortSignal): Promise<TaskSnapshot> {
  return request<TaskSnapshot>(`/agent/tasks/${encodeURIComponent(taskId)}/snapshot`, { signal });
}

async function streamOnce(
  taskId: string,
  cursor: number,
  signal: AbortSignal,
  onEvent: (event: AgentEvent) => void,
  onTask: (task: AgentTask) => void,
): Promise<{ cursor: number; expired: boolean; terminal: boolean }> {
  const pairing = await readPairing();
  if (!pairing) throw new GatewayError(0, "设备配对已清除。", "pairing_required");
  const response = await nativeRequest(pairing,
    `/agent/tasks/${encodeURIComponent(taskId)}/events?after_event_id=${cursor}`,
    { accept: "text/event-stream", signal });
  if (response.status === 410) return { cursor, expired: true, terminal: false };
  if (!response.ok) classifyStatus(response.status);
  if (!response.body) throw new GatewayError(0, "网关没有返回事件流。", "stream_unavailable");

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let latest = cursor;
  try {
    while (!signal.aborted) {
      let idleTimer: ReturnType<typeof setTimeout> | undefined;
      const idle = new Promise<never>((_, reject) => {
        idleTimer = setTimeout(() => reject(new GatewayError(0, "事件流超过 60 秒没有数据。", "stream_idle")), STREAM_IDLE_TIMEOUT_MS);
      });
      let part: ReadableStreamReadResult<Uint8Array>;
      try {
        part = await Promise.race([reader.read(), idle]);
      } finally {
        if (idleTimer) clearTimeout(idleTimer);
      }
      if (part.done) return { cursor: latest, expired: false, terminal: false };
      buffer += decoder.decode(part.value, { stream: true });
      let boundary: number;
      while ((boundary = buffer.search(/\r?\n\r?\n/)) >= 0) {
        const delimiter = buffer.match(/\r?\n\r?\n/)?.[0] ?? "\n\n";
        const frame = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + delimiter.length);
        const parsed = parseSseFrame(frame);
        if (!parsed) continue;
        if (parsed.kind === "cursor_expired") {
          return { cursor: latest, expired: true, terminal: false };
        }
        const eventCursor = parsed.event.event_id || parsed.id;
        if (eventCursor && eventCursor > latest) latest = eventCursor;
        onEvent(parsed.event);
        if (parsed.event.type === "task_state") {
          const task = await request<AgentTask>(`/agent/tasks/${encodeURIComponent(taskId)}`, { signal });
          onTask(task);
          if (TERMINAL.has(task.status)) return { cursor: latest, expired: false, terminal: true };
        }
      }
    }
  } finally {
    await reader.cancel().catch(() => undefined);
  }
  return { cursor: latest, expired: false, terminal: false };
}

async function pollOnce(
  taskId: string,
  cursor: number,
  signal: AbortSignal,
  onEvent: (event: AgentEvent) => void,
  onTask: (task: AgentTask) => void,
): Promise<{ cursor: number; terminal: boolean; expired: boolean }> {
  const response = await request<{
    events: AgentEvent[];
    next_cursor: number;
    task: AgentTask;
  }>(`/agent/tasks/${encodeURIComponent(taskId)}/events?transport=poll&after_event_id=${cursor}`, { signal });
  response.events.forEach(onEvent);
  onTask(response.task);
  return {
    cursor: Math.max(cursor, response.next_cursor),
    terminal: TERMINAL.has(response.task.status),
    expired: false,
  };
}

export async function watchTask(
  taskId: string,
  options: {
    signal: AbortSignal;
    onEvent: (event: AgentEvent) => void;
    onTask: (task: AgentTask) => void;
    onSnapshot?: (snapshot: TaskSnapshot) => void;
    onConnectionMode?: (mode: "sse" | "poll") => void;
  },
): Promise<void> {
  const { signal } = options;
  let snapshot = await readSnapshot(taskId, signal);
  let cursor = snapshot.event_cursor;
  options.onTask(snapshot.task);
  options.onSnapshot?.(snapshot);
  if (TERMINAL.has(snapshot.task.status)) return;
  let failures = 0;
  options.onConnectionMode?.("sse");
  while (!signal.aborted) {
    try {
      const result = await streamOnce(taskId, cursor, signal, options.onEvent, options.onTask);
      if (result.terminal) return;
      if (result.expired) {
        snapshot = await readSnapshot(taskId, signal);
        cursor = snapshot.event_cursor;
        options.onTask(snapshot.task);
        options.onSnapshot?.(snapshot);
        if (TERMINAL.has(snapshot.task.status)) return;
        failures = 0;
        continue;
      } else {
        cursor = result.cursor;
      }
      throw new GatewayError(0, "事件流已结束，正在重新连接。", "stream_closed");
    } catch (error) {
      if (signal.aborted) return;
      failures += 1;
      if (failures < 3) {
        await delay(Math.min(500 * 2 ** (failures - 1), 8_000), signal);
        continue;
      }
      options.onConnectionMode?.("poll");
      while (!signal.aborted) {
        try {
          const result = await pollOnce(taskId, cursor, signal, options.onEvent, options.onTask);
          cursor = result.cursor;
          if (result.terminal) return;
        } catch (pollError) {
          if (signal.aborted) return;
          if (pollError instanceof GatewayError && pollError.status === 410) {
            snapshot = await readSnapshot(taskId, signal);
            cursor = snapshot.event_cursor;
            options.onTask(snapshot.task);
            options.onSnapshot?.(snapshot);
            if (TERMINAL.has(snapshot.task.status)) return;
          }
        }
        await delay(2_000, signal);
      }
      return;
    }
  }
}
