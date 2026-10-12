// 统一浏览器 API client：JSON/204/下载、401/403/409/422/503、超时、
// loading/empty/error/retry 四态、提交锁定与幂等键。
// 页面不解释 score 语义，只渲染版本化 API 返回的结构。

const TIMEOUT_MS = 15000;

// CSRF 标记：服务端要求 cookie 认证的写请求携带同源 Origin 或此请求头。
export const FETCH_MARKER_HEADERS = Object.freeze({ "X-Requested-With": "fetch" });

export class ApiError extends Error {
  constructor(status, message, { retryable = false, detail = null } = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.retryable = retryable;
    // 原始 detail（如 422 规则校验的 issues 列表），供页面逐条展示。
    this.detail = detail;
  }
}

function idempotencyKey() {
  if (globalThis.crypto && crypto.randomUUID) {
    return crypto.randomUUID();
  }
  return `idem-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

async function parseBody(response) {
  if (response.status === 204) return null;
  const contentType = response.headers.get("content-type") || "";
  if (contentType.includes("application/json")) {
    return response.json();
  }
  return response.text();
}

async function request(path, { method = "GET", body, idempotency = false, signal } = {}) {
  const headers = { ...FETCH_MARKER_HEADERS };
  let payload;
  if (body !== undefined) {
    if (body instanceof FormData) {
      payload = body;
    } else {
      headers["Content-Type"] = "application/json";
      payload = JSON.stringify(body);
    }
  }
  if (idempotency && method !== "GET") {
    headers["Idempotency-Key"] = idempotencyKey();
  }

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
  if (signal) signal.addEventListener("abort", () => controller.abort());
  let response;
  try {
    response = await fetch(path, {
      method,
      headers,
      body: payload,
      credentials: "same-origin",
      signal: controller.signal,
    });
  } catch (error) {
    throw new ApiError(0, "网络请求失败或超时，请重试。", { retryable: true });
  } finally {
    clearTimeout(timer);
  }

  if (response.status === 401) {
    throw new ApiError(401, "登录已过期，请重新登录。");
  }
  let data;
  try {
    data = await parseBody(response);
  } catch {
    data = null;
  }
  if (!response.ok) {
    const detail =
      data && typeof data === "object" && "detail" in data ? data.detail : null;
    const text =
      typeof detail === "string"
        ? detail
        : `请求失败（HTTP ${response.status}）。`;
    throw new ApiError(response.status, text, {
      retryable: response.status >= 500 || response.status === 409,
      detail,
    });
  }
  return data;
}

export function get(path, options = {}) {
  return request(path, { ...options, method: "GET" });
}

export function post(path, body, options = {}) {
  return request(path, { ...options, method: "POST", body, idempotency: options.idempotency ?? true });
}

export function patch(path, body, options = {}) {
  return request(path, { ...options, method: "PATCH", body, idempotency: options.idempotency ?? false });
}

export function del(path, body, options = {}) {
  return request(path, { ...options, method: "DELETE", body, idempotency: options.idempotency ?? false });
}

export async function download(path) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
  let response;
  try {
    response = await fetch(path, {
      headers: { ...FETCH_MARKER_HEADERS },
      credentials: "same-origin",
      signal: controller.signal,
    });
  } catch {
    throw new ApiError(0, "下载失败或超时，请重试。", { retryable: true });
  } finally {
    clearTimeout(timer);
  }
  if (response.status === 401) throw new ApiError(401, "登录已过期，请重新登录。");
  if (!response.ok) throw new ApiError(response.status, `下载失败（HTTP ${response.status}）。`);
  const blob = await response.blob();
  const disposition = response.headers.get("content-disposition") || "";
  const match = /filename\*?=(?:UTF-8''|")?([^\";]+)/i.exec(disposition);
  const filename = match ? decodeURIComponent(match[1]) : "download";
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

// ---- 数据区域四态渲染 -------------------------------------------------

export function renderRegion(region, state, content) {
  region.dataset.state = state;
  region.replaceChildren();
  if (state === "loading") {
    const notice = document.createElement("p");
    notice.className = "notice notice-loading";
    notice.textContent = "正在加载…";
    region.appendChild(notice);
  } else if (state === "empty") {
    const notice = document.createElement("p");
    notice.className = "notice";
    notice.textContent = content || region.dataset.emptyText || "暂无数据。";
    region.appendChild(notice);
  } else if (state === "error") {
    const notice = document.createElement("p");
    notice.className = "notice notice-error";
    notice.textContent = content || "加载失败。";
    region.appendChild(notice);
    if (content && content.retryable !== false) {
      const retry = document.createElement("button");
      retry.type = "button";
      retry.textContent = "重试";
      retry.addEventListener("click", () => loadRegion(region));
      region.appendChild(retry);
    }
  } else if (state === "ready") {
    region.appendChild(content);
  }
}

export async function loadRegion(region, { render } = {}) {
  const endpoint = region.dataset.endpoint;
  if (!endpoint) return null;
  renderRegion(region, "loading");
  try {
    const data = await get(endpoint);
    if (render) {
      render(region, data);
    } else {
      const isEmpty =
        data === null ||
        data === undefined ||
        (Array.isArray(data) && data.length === 0) ||
        (data && typeof data === "object" && Object.keys(data).length === 0);
      if (isEmpty) {
        renderRegion(region, "empty");
      } else if (data instanceof Element) {
        renderRegion(region, "ready", data);
      } else {
        const pre = document.createElement("pre");
        pre.className = "raw-json";
        pre.textContent = JSON.stringify(data, null, 2);
        renderRegion(region, "ready", pre);
      }
    }
    return data;
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) {
      globalThis.location.href = "/app/login";
      return null;
    }
    renderRegion(region, "error", error.message);
    return null;
  }
}

export function lockWhileBusy(button, promise) {
  button.disabled = true;
  const original = button.textContent;
  button.textContent = "处理中…";
  return promise.finally(() => {
    button.disabled = false;
    button.textContent = original;
  });
}

export { idempotencyKey };
