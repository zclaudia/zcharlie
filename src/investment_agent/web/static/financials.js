// 港美财务区块的刷新入口：POST /research/listings/{id}/refresh（会话 cookie）。
// 只登记采集任务：展示本次是否去重与在线刷新的开启状态；
// 不显示运行内部标识与来源名，也不声称数据已更新。

import { ApiError, post } from "./api.js";
import { el } from "./dom.js";

function sourceCount(result) {
  const versions = result && result.adapter_versions;
  if (!versions || typeof versions !== "object") return 0;
  return Object.keys(versions).length;
}

export function refreshLines(result) {
  const created = Boolean(result && result.created);
  const lines = [];
  if (created) {
    lines.push(
      `已登记采集任务（新建）：批次含 ${sourceCount(result)} 个来源，同一天同一组合重复提交会自动去重。`
    );
  } else {
    lines.push("同一来源组合的采集任务今日已登记过，本次未重复登记（去重）。");
  }
  lines.push(
    result && result.online_refresh_enabled
      ? "在线刷新已开启：任务由采集流程执行，完成后页面数据才会更新。"
      : "在线刷新未开启：登记的任务不会联网采集，也不会更新本页数据。"
  );
  return lines;
}

function renderLines(region, lines) {
  region.dataset.state = "ready";
  region.replaceChildren();
  const list = el("ul", { class: "financial-refresh-lines" });
  for (const line of lines) list.appendChild(el("li", {}, line));
  region.appendChild(list);
}

function renderFailure(region, error) {
  region.dataset.state = "error";
  region.replaceChildren();
  region.appendChild(
    el(
      "p",
      { class: "notice notice-error" },
      error instanceof ApiError ? error.message : "登记失败，请重试。"
    )
  );
}

export async function requestRefresh(listingId, { render, onFailure, onUnauthorized } = {}) {
  try {
    const result = await post(`/research/listings/${encodeURIComponent(listingId)}/refresh`, {});
    if (render) render(refreshLines(result));
    return result;
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) {
      if (onUnauthorized) onUnauthorized();
      return null;
    }
    if (onFailure) onFailure(error);
    return null;
  }
}

export function bootFinancials() {
  const button = document.querySelector("#financial-refresh-button");
  const region = document.querySelector("#financial-refresh-result");
  if (!button || !region) return;
  button.addEventListener("click", async () => {
    const listingId = button.getAttribute("data-listing-id");
    if (!listingId) return;
    button.disabled = true;
    try {
      await requestRefresh(listingId, {
        render: (lines) => renderLines(region, lines),
        onFailure: (error) => renderFailure(region, error),
        onUnauthorized: () => {
          globalThis.location.href = "/app/login";
        },
      });
    } finally {
      button.disabled = false;
    }
  });
}
