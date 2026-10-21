import { beforeEach, describe, expect, it, vi } from "vitest";
import { removePairing, savePairing } from "./storage";

const calls: string[] = [];
const invoke = vi.fn(async (command: string) => {
  calls.push(`invoke:${command}`);
});

vi.mock("@tauri-apps/api/core", () => ({ invoke: (command: string) => invoke(command) }));
vi.mock("tauri-plugin-secure-keystore", () => ({
  getItem: vi.fn(async () => null),
  setItem: vi.fn(async () => {
    calls.push("setItem");
  }),
  deleteItem: vi.fn(async () => {
    calls.push("deleteItem");
  }),
}));
vi.mock("@tauri-apps/plugin-store", () => ({
  load: vi.fn(async () => ({ get: vi.fn(async () => null), delete: vi.fn(), save: vi.fn() })),
}));

const pairing = { gatewayUrl: "https://gw.example", backendId: "svc-1", deviceToken: "zgd_x" };

describe("pairing storage", () => {
  beforeEach(() => {
    calls.length = 0;
    invoke.mockClear();
  });

  it("clears the native pairing cache after saving a pairing", async () => {
    await savePairing(pairing);
    expect(calls).toEqual(["setItem", "invoke:invalidate_pairing_cache"]);
  });

  it("clears the native pairing cache after removing the pairing", async () => {
    await removePairing();
    expect(calls).toEqual(["deleteItem", "invoke:invalidate_pairing_cache"]);
  });

  it("still saves when the native command is unavailable", async () => {
    invoke.mockRejectedValueOnce(new Error("not in app"));
    await expect(savePairing(pairing)).resolves.toBeUndefined();
  });
});
