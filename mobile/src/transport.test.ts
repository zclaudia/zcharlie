import { beforeEach, describe, expect, it, vi } from "vitest";
import { GatewayError, pairDevice, parseSseFrame, proxyUrl, request } from "./transport";

const savePairing = vi.fn();
const readPairing = vi.fn();
const nativeFetch = vi.fn();

vi.mock("./storage", () => ({
  savePairing: (...args: unknown[]) => savePairing(...args),
  readPairing: (...args: unknown[]) => readPairing(...args),
}));
vi.mock("@tauri-apps/plugin-http", () => ({ fetch: (...args: unknown[]) => nativeFetch(...args) }));
vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn(async () => true) }));

describe("mobile gateway transport", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("builds an API proxy URL under the selected backend", () => {
    expect(proxyUrl("https://gateway.example", "svc-1", "/agent/conversations?limit=10"))
      .toBe("https://gateway.example/api/proxy/svc-1/agent/conversations?limit=10");
  });

  it("pairs with an empty service ID by discovering the single online backend", async () => {
    const fetcher = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/api/discovery/backends")) {
        return new Response(JSON.stringify({
          success: true,
          data: { items: [
            { backendId: "svc-offline", name: "旧栈", online: false, lastSeenAt: 1 },
            { backendId: "svc-9", name: "研究台", online: true, lastSeenAt: 2 },
          ] },
        }), { status: 200, headers: { "content-type": "application/json" } });
      }
      return new Response("ok", { status: 200 });
    });

    const pairing = await pairDevice({
      gatewayUrl: "https://gateway.example",
      backendId: "",
      deviceToken: "zgd_local-test-token",
    }, fetcher as never);

    expect(pairing.backendId).toBe("svc-9");
    const [discoveryUrl, discoveryInit] = fetcher.mock.calls[0] as unknown as [string, RequestInit];
    expect(discoveryUrl).toBe("https://gateway.example/api/discovery/backends");
    expect(new Headers(discoveryInit.headers).get("authorization")).toBe("Bearer zgd_local-test-token");
    // 健康检查仍走代理路径（对选定后端）
    const [healthUrl] = fetcher.mock.calls[1] as unknown as [string, RequestInit];
    expect(healthUrl).toBe("https://gateway.example/api/proxy/svc-9/healthz");
    expect(savePairing).toHaveBeenCalledWith(pairing);
  });

  it("distinguishes an empty addressable scope from backends being offline", async () => {
    // 空列表 = 权限范围为空：提示重新签发凭证
    const emptyFetcher = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/api/discovery/backends")) {
        return new Response(JSON.stringify({ success: true, data: { items: [] } }),
          { status: 200, headers: { "content-type": "application/json" } });
      }
      return new Response("ok", { status: 200 });
    });
    await expect(pairDevice({
      gatewayUrl: "https://gateway.example",
      backendId: "",
      deviceToken: "zgd_local-test-token",
    }, emptyFetcher as never)).rejects.toMatchObject({ code: "empty_backend_scope" });

    // 有可寻址后端但全部离线：不同错误，提示稍后重试
    const offlineFetcher = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/api/discovery/backends")) {
        return new Response(JSON.stringify({
          success: true,
          data: { items: [{ backendId: "svc-1", name: "研究台", online: false, lastSeenAt: null }] },
        }), { status: 200, headers: { "content-type": "application/json" } });
      }
      return new Response("ok", { status: 200 });
    });
    await expect(pairDevice({
      gatewayUrl: "https://gateway.example",
      backendId: "",
      deviceToken: "zgd_local-test-token",
    }, offlineFetcher as never)).rejects.toMatchObject({ code: "backends_offline" });
    expect(savePairing).not.toHaveBeenCalled();
  });

  it("falls back to manual service ID entry on gateways without discovery", async () => {
    const fetcher = vi.fn(async () => new Response("not found", { status: 404 }));
    await expect(pairDevice({
      gatewayUrl: "https://gateway.example",
      backendId: "",
      deviceToken: "zgd_local-test-token",
    }, fetcher as never)).rejects.toMatchObject({
      code: "discovery_unsupported",
      status: 404,
      message: expect.stringContaining("手动填写服务 ID"),
    });
    expect(savePairing).not.toHaveBeenCalled();
  });

  it("reports ambiguity when several reachable backends are online", async () => {
    const fetcher = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/api/discovery/backends")) {
        return new Response(JSON.stringify({
          success: true,
          data: { items: [
            { backendId: "svc-a", name: "a", online: true, lastSeenAt: 1 },
            { backendId: "svc-b", name: "b", online: true, lastSeenAt: 2 },
          ] },
        }), { status: 200, headers: { "content-type": "application/json" } });
      }
      return new Response("ok", { status: 200 });
    });
    await expect(pairDevice({
      gatewayUrl: "https://gateway.example",
      backendId: "",
      deviceToken: "zgd_local-test-token",
    }, fetcher as never)).rejects.toMatchObject({ code: "ambiguous_backend", message: expect.stringContaining("svc-a、svc-b") });
    expect(savePairing).not.toHaveBeenCalled();
  });

  it("verifies a device credential with Bearer auth and saves only after success", async () => {
    const fetcher = vi.fn(async () => new Response("ok", { status: 200 }));
    const pairing = await pairDevice({
      gatewayUrl: "https://gateway.example/",
      backendId: "service-123",
      deviceToken: "zgd_local-test-token",
    }, fetcher as never);

    expect(fetcher).toHaveBeenCalledTimes(1);
    const [url, init] = fetcher.mock.calls[0] as unknown as [string, RequestInit];
    const headers = new Headers(init.headers);
    expect(url).toBe("https://gateway.example/api/proxy/service-123/healthz");
    expect(headers.get("authorization")).toBe("Bearer zgd_local-test-token");
    expect(headers.has("x-api-key")).toBe(false);
    expect(headers.has("cookie")).toBe(false);
    expect(savePairing).toHaveBeenCalledWith(pairing);
  });

  it("reports invalid or expired device credentials and does not persist them", async () => {
    const fetcher = vi.fn(async () => new Response("denied", { status: 403 }));
    await expect(pairDevice({
      gatewayUrl: "https://gateway.example",
      backendId: "service-123",
      deviceToken: "zgd_local-test-token",
    }, fetcher as never)).rejects.toMatchObject({
      status: 403,
      code: "invalid_device_token",
      message: "设备凭证无效或已过期，请重新配对。",
    });
    expect(savePairing).not.toHaveBeenCalled();
  });

  it("routes JSON calls through plugin-http and maps an offline gateway response", async () => {
    readPairing.mockResolvedValue({ gatewayUrl: "https://gateway.example", backendId: "svc", deviceToken: "zgd_test-token" });
    nativeFetch.mockResolvedValue(new Response("offline", { status: 504 }));
    await expect(request("/healthz")).rejects.toMatchObject({
      status: 504,
      code: "backend_offline",
      message: "服务暂时离线，请检查 zcharlie 服务状态。",
    });
    const [url, init] = nativeFetch.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("https://gateway.example/api/proxy/svc/healthz");
    expect(new Headers(init.headers).has("x-api-key")).toBe(false);
  });

  it("parses event ids and the snapshot-expired event", () => {
    expect(parseSseFrame('id: 4\nevent: progress\ndata: {"event_id":4,"task_id":"t1","type":"progress","payload":{"summary":"正在查证"}}'))
      .toMatchObject({ kind: "event", id: 4, event: { event_id: 4, type: "progress" } });
    expect(parseSseFrame('event: cursor_expired\ndata: {"code":"cursor_expired","reload_snapshot":true}'))
      .toMatchObject({ kind: "cursor_expired" });
  });

  it("rejects traversal outside the selected backend route", () => {
    expect(() => proxyUrl("https://gateway.example", "svc", "/../admin"))
      .toThrowError(GatewayError);
  });
});
