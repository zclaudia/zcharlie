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
