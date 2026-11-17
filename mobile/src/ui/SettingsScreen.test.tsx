import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SWRConfig } from "swr";
import { SettingsScreen } from "./SettingsScreen";
import { readPairing } from "../storage";
import { request } from "../transport";

vi.mock("../storage", () => ({ readPairing: vi.fn() }));
vi.mock("../transport", () => ({ request: vi.fn(), GatewayError: class extends Error {} }));
afterEach(cleanup);
const settings = {
  revision: 1,
  encryption_key_configured: true,
  groups: [{ key: "research", title: "研究设置", items: [
    { key: "frequency", label: "研究频率", help: "每天的任务次数", apply: "next_job", secret: false, value: 2, default: 2, input: { type: "int", min: 1, max: 10 } },
    { key: "api_key", label: "模型密钥", help: "留空不修改", apply: "live", secret: true, configured: true, input: { type: "text" } },
  ] }],
  bootstrap: [],
};
beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(readPairing).mockResolvedValue({ gatewayUrl: "https://gateway.example.com", backendId: "research", deviceToken: "test-device-token" });
  vi.mocked(request).mockImplementation(async (path) => path.includes("history") ? { items: [] } : settings);
});
function mount(onUnpair = vi.fn().mockResolvedValue(undefined)) {
  render(<SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}><SettingsScreen onPaired={vi.fn()} onUnpair={onUnpair} /></SWRConfig>);
  return onUnpair;
}
async function editFrequency(value = "3") {
  fireEvent.change(await screen.findByLabelText("研究频率"), { target: { value } });
}

describe("设置反馈", () => {
  it("读取失败展示重试而非永久加载", async () => {
    vi.mocked(request).mockImplementation(async (path) => { if (path === "/settings") throw new Error("offline"); return { items: [] }; });
    mount();
    expect(await screen.findByText("设置读取失败")).toBeVisible();
    expect(screen.queryByText("正在读取设置…")).not.toBeInTheDocument();
    vi.mocked(request).mockImplementation(async (path) => path.includes("history") ? { items: [] } : settings);
    fireEvent.click(screen.getByRole("button", { name: "重试读取设置" }));
    expect(await screen.findByLabelText("研究频率")).toHaveValue(2);
    expect(screen.queryByText("设置读取失败")).not.toBeInTheDocument();
  });

  it("历史失败不阻塞设置读取", async () => {
    vi.mocked(request).mockImplementation(async (path) => { if (path.includes("history")) throw new Error("offline"); return settings; });
    mount();
    expect(await screen.findByLabelText("研究频率")).toBeEnabled();
    expect(await screen.findByText("变更记录暂时无法读取，不影响已保存的设置。")).toBeVisible();
  });

  it("空历史有说明，恢复原值不计入待保存项", async () => {
    mount();
    expect(await screen.findByText("暂无变更记录，保存设置后会在这里显示。")).toBeVisible();
    await editFrequency();
    expect(screen.getByText("1 项待保存")).toBeVisible();
    await editFrequency("2");
    expect(screen.getByRole("button", { name: "保存设置" })).toBeDisabled();
  });

  it("密钥清除计数不重复，重新输入可取消清除", async () => {
    mount();
    await screen.findByLabelText("模型密钥");
    fireEvent.change(screen.getByLabelText("模型密钥"), { target: { value: "replacement" } });
    fireEvent.click(screen.getByRole("button", { name: "清除" }));
    expect(screen.getByText("1 项待保存")).toBeVisible();
    expect(screen.getByText("此密钥将在保存后清除，可在保存前撤销。")).toBeVisible();
    fireEvent.change(screen.getByLabelText("模型密钥"), { target: { value: "new-value" } });
    expect(screen.queryByRole("button", { name: "撤销清除" })).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("模型密钥"), { target: { value: "" } });
    expect(screen.getByRole("button", { name: "保存设置" })).toBeDisabled();
  });

  it("保存失败保留输入并使用错误语义", async () => {
    vi.mocked(request).mockImplementation(async (path, options) => {
      if (options?.method === "PATCH") throw new Error("offline");
      return path.includes("history") ? { items: [] } : settings;
    });
    mount();
    await editFrequency();
    fireEvent.click(screen.getByRole("button", { name: "保存设置" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("保存失败");
    expect(screen.getByLabelText("研究频率")).toHaveValue(3);
    expect(screen.getByText("1 项待保存")).toBeVisible();
  });

  it("保存成功不会被历史请求失败误报，保存中禁止修改", async () => {
    let finish!: (value: unknown) => void;
    let saved = false;
    vi.mocked(request).mockImplementation(async (path, options) => {
      if (options?.method === "PATCH") return await new Promise((resolve) => { finish = resolve; });
      if (path.includes("history")) { if (saved) throw new Error("history unavailable"); return { items: [] }; }
      return settings;
    });
    mount();
    await editFrequency();
    fireEvent.click(screen.getByRole("button", { name: "保存设置" }));
    expect(screen.getByLabelText("研究频率")).toBeDisabled();
    saved = true;
    await act(async () => finish({ changed: ["frequency"], settings: { ...settings, groups: [{ ...settings.groups[0], items: settings.groups[0].items.map((item) => item.key === "frequency" ? { ...item, value: 3 } : item) }] } }));
    expect(await screen.findByRole("status")).toHaveTextContent("保存成功");
    expect(await screen.findByText("变更记录暂时无法读取，不影响已保存的设置。")).toBeVisible();
    expect(screen.getByRole("button", { name: "保存设置" })).toBeDisabled();
    expect(request).toHaveBeenCalledWith("/settings", { method: "PATCH", body: { values: { frequency: 3 }, note: null } });
  });

  it("解除配对必须确认，取消不调用删除", async () => {
    const onUnpair = mount();
    fireEvent.click(await screen.findByRole("button", { name: "解除配对" }));
    expect(onUnpair).not.toHaveBeenCalled();
    expect(screen.getByText(/不会删除研究台数据/)).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(screen.queryByRole("button", { name: "确认解除配对" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "解除配对" }));
    fireEvent.click(screen.getByRole("button", { name: "确认解除配对" }));
    await waitFor(() => expect(onUnpair).toHaveBeenCalledTimes(1));
    expect(await screen.findByRole("heading", { name: "连接你的研究台" })).toBeVisible();
  });

  it("解除配对失败可重试且不丢失当前连接", async () => {
    const onUnpair = vi.fn().mockRejectedValue(new Error("keystore unavailable"));
    mount(onUnpair);
    fireEvent.click(await screen.findByRole("button", { name: "解除配对" }));
    fireEvent.click(screen.getByRole("button", { name: "确认解除配对" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("解除配对失败");
    expect(screen.getByText("https://gateway.example.com")).toBeVisible();
    expect(screen.getByRole("button", { name: "确认解除配对" })).toBeEnabled();
  });
});
