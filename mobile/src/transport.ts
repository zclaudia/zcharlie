import { invoke } from "@tauri-apps/api/core";
import { fetch as nativeFetch } from "@tauri-apps/plugin-http";
import { readPairing, savePairing, type Pairing } from "./storage";

export type FetchLike = typeof nativeFetch;

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
  if (status === 429) {
    throw new GatewayError(status, "请求太频繁，网关正在限流，请稍候再试。", "rate_limited");
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

export type ReachableBackend = {
  backendId: string;
  name: string;
  online: boolean;
  lastSeenAt: number;
};

/**
 * Gateway-level request (not proxied to a backend): used while pairing, when
 * no backend has been picked yet.
 */
async function gatewayRootRequest(
  gatewayUrl: string,
  deviceToken: string,
  path: string,
  fetchImpl?: FetchLike,
): Promise<unknown> {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  const fetcher = fetchImpl ?? nativeFetch;
  try {
    const response = await fetcher(`${gatewayUrl}${path}`, {
      headers: { Accept: "application/json", Authorization: `Bearer ${deviceToken}` },
      credentials: "omit",
      signal: controller.signal,
      connectTimeout: REQUEST_TIMEOUT_MS,
    });
    if (!response.ok) classifyStatus(response.status);
    return await response.json();
  } catch (error) {
    if (error instanceof GatewayError) throw error;
    throw new GatewayError(0, "网关不可达或请求超时，请检查地址与网络。", "gateway_unreachable");
  } finally {
    clearTimeout(timeout);
  }
}

/** Backends the device credential can reach, per the gateway's registry. */
export async function discoverBackends(
  input: { gatewayUrl: string; deviceToken: string },
  fetchImpl?: FetchLike,
): Promise<ReachableBackend[]> {
  const gatewayUrl = await validateGatewayUrl(input.gatewayUrl);
  let body: { success?: boolean; data?: { items?: ReachableBackend[] } };
  try {
    body = await gatewayRootRequest(gatewayUrl, input.deviceToken, "/api/discovery/backends", fetchImpl) as {
      success?: boolean;
      data?: { items?: ReachableBackend[] };
    };
  } catch (error) {
    // 旧网关没有该端点：引导回退到手工填写服务 ID，而不是通用 404 报错。
    if (error instanceof GatewayError && error.status === 404) {
      throw new GatewayError(404, "该网关不支持自动发现，请手动填写服务 ID。", "discovery_unsupported");
    }
    throw error;
  }
  return body.data?.items ?? [];
}

export async function pairDevice(
  input: { gatewayUrl: string; backendId: string; deviceToken: string },
  fetchImpl?: FetchLike,
): Promise<Pairing> {
  const gatewayUrl = await validateGatewayUrl(input.gatewayUrl);
  let backendId = input.backendId.trim();
  const deviceToken = input.deviceToken.trim();
  if (!deviceToken.startsWith("zgd_") || deviceToken.length < 8 || /\s/.test(deviceToken)) {
    throw new GatewayError(0, "设备凭证格式无效，应以 zgd_ 开头。", "invalid_device_token");
  }
  if (!backendId) {
    // 网关接入架构里 backend id 是可选的：凭证未绑后端时由客户端发现并选定。
    // 列表是"可寻址集合"（含离线后端），与"当前在线"是两回事，错误要分开说。
    const addressable = await discoverBackends({ gatewayUrl, deviceToken }, fetchImpl);
    if (addressable.length === 0) {
      throw new GatewayError(0, "该凭证没有可寻址的后端，请重新签发设备凭证。", "empty_backend_scope");
    }
    const online = addressable.find((b) => b.online);
    if (!online) {
      throw new GatewayError(0, "凭证可达的后端当前都离线，请等后端上线后再配对。", "backends_offline");
    }
    if (addressable.filter((b) => b.online).length > 1) {
      const ids = addressable.filter((b) => b.online).map((b) => b.backendId).join("、");
      throw new GatewayError(0, `该凭证可达多个在线后端，请在服务 ID 里填一个：${ids}`, "ambiguous_backend");
    }
    backendId = online.backendId;
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
