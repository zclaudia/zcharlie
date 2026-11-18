import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PairForm } from "./PairForm";
import { discoverBackends, pairDevice } from "../transport";

vi.mock("../transport", () => ({ discoverBackends: vi.fn(), pairDevice: vi.fn(), GatewayError: class extends Error {} }));
afterEach(cleanup);
beforeEach(() => vi.resetAllMocks());

function fillConnection() {
  fireEvent.change(screen.getByLabelText("网关地址"), { target: { value: "https://gateway.example.com" } });
  fireEvent.change(screen.getByLabelText("设备凭证"), { target: { value: "test-device-token" } });
}

describe("配对流程", () => {
  it("默认折叠服务 ID，展开后可指定后端", async () => {
    const onPaired = vi.fn();
    const pairing = { gatewayUrl: "https://gateway.example.com", backendId: "research", deviceToken: "test-device-token" };
    vi.mocked(pairDevice).mockResolvedValue(pairing);
    render(<PairForm onPaired={onPaired} />);
    expect(screen.getByText("高级设置").closest("details")).not.toHaveAttribute("open");
    fireEvent.click(screen.getByText("高级设置"));
    expect(screen.getByLabelText("服务 ID（可选）")).toBeVisible();
    fillConnection();
    fireEvent.change(screen.getByLabelText("服务 ID（可选）"), { target: { value: "research" } });
    fireEvent.click(screen.getByRole("button", { name: "验证并连接" }));
    await waitFor(() => expect(onPaired).toHaveBeenCalledWith(pairing));
    expect(discoverBackends).not.toHaveBeenCalled();
    expect(screen.getByLabelText("设备凭证")).toHaveValue("");
  });

  it("单个在线研究台自动连接", async () => {
    vi.mocked(discoverBackends).mockResolvedValue([{ backendId: "one", name: "研究台一", online: true, lastSeenAt: 0 }]);
    vi.mocked(pairDevice).mockResolvedValue({ gatewayUrl: "https://gateway.example.com", backendId: "one", deviceToken: "test-device-token" });
    const onPaired = vi.fn();
    render(<PairForm onPaired={onPaired} />);
    fillConnection();
    fireEvent.click(screen.getByRole("button", { name: "验证并连接" }));
    await waitFor(() => expect(onPaired).toHaveBeenCalled());
    expect(pairDevice).toHaveBeenCalledWith(expect.objectContaining({ backendId: "one" }));
  });

  it("多个研究台可选，修改凭证清除旧发现结果", async () => {
    vi.mocked(discoverBackends).mockResolvedValue([
      { backendId: "one", name: "研究台一", online: true, lastSeenAt: 0 },
      { backendId: "two", name: "研究台二", online: true, lastSeenAt: 0 },
      { backendId: "offline", name: "离线研究台", online: false, lastSeenAt: 0 },
    ]);
    render(<PairForm onPaired={vi.fn()} />);
    fillConnection();
    fireEvent.click(screen.getByRole("button", { name: "验证并连接" }));
    expect(await screen.findByRole("group", { name: "选择研究台" })).toBeVisible();
    expect(screen.getByRole("button", { name: /离线研究台\s*离线/ })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("设备凭证"), { target: { value: "another-token" } });
    expect(screen.queryByRole("group", { name: "选择研究台" })).not.toBeInTheDocument();
    expect(pairDevice).not.toHaveBeenCalled();
  });

  it("发现失败后停止加载并允许重试", async () => {
    vi.mocked(discoverBackends).mockRejectedValue(new Error("offline"));
    render(<PairForm onPaired={vi.fn()} />);
    fillConnection();
    fireEvent.click(screen.getByRole("button", { name: "验证并连接" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("配对失败");
    expect(screen.getByRole("button", { name: "验证并连接" })).toBeEnabled();
  });
});
