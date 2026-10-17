// 港美财务区块的刷新入口：POST /research/listings/{id}/refresh（会话 cookie）。
// POST 只证明登记结果；它不证明采集开始、成功或资料已更新。

import { ApiError, get, post } from "./api.js";
import { el } from "./dom.js";

const REFRESH_POLL_LIMIT = 6;
const REFRESH_POLL_INTERVAL_MS = 750;

const REFRESH_STATUS_COPY = Object.freeze({
  queued: "排队中",
  running: "执行中",
  success: "已结束",
  partial: "部分完成",
  failed: "失败",
});

const REFRESH_REASON_COPY = Object.freeze({
  user_refresh: "用户点击刷新",
  刷新运行失败: "刷新运行失败",
  部分来源未完成: "部分来源未完成",
});

const SOURCE_REASON_COPY = Object.freeze({
  identity_not_bound: "证券未绑定该来源",
  licensing_unknown: "使用许可尚未确认",
  licensing_scope_mismatch: "许可范围不含该发行人",
  technical_acceptance_missing_or_mismatched: "技术验收尚未完成或版本不匹配",
  operationally_disabled: "运行开关已关闭",
  global_refresh_disabled: "全局在线刷新未开启",
  unknown_source: "来源未列入受控策略",
});

const SOURCE_LABELS = Object.freeze({
  "eastmoney.hk.native.v1": "港股年报来源",
  "sec.companyfacts.v1": "美国发行人申报来源",
});

export function refreshLines(result) {
  const created = Boolean(result && result.created);
  const lines = [];
  if (created) {
    lines.push("已登记采集任务（新建）：资料刷新请求当前仅排队等待处理，同一天重复登记会自动去重。");
  } else {
    lines.push("同一来源组合的采集任务今日已登记过，本次未重复登记（去重）。");
  }
  lines.push(
    result && result.online_refresh_enabled
      ? "至少一个来源当前通过在线采集条件；请求仅已登记，采集结果尚未确认。"
      : "当前没有来源通过全部在线采集条件；登记任务不会绕过拒绝条件。"
  );
  for (const [source, decision] of Object.entries(result?.source_decisions || {})) {
    if (decision.enabled) continue;
    const reasons = (decision.reason_codes || []).map((code) => SOURCE_REASON_COPY[code] || code);
    lines.push(`${SOURCE_LABELS[source] || "受控资料来源"}：${reasons.join("；") || "当前不可在线采集"}。`);
  }
  lines.push("任务登记或去重不授予采集权限，也不代表采集已完成或资料已更新。");
  return lines;
}

function lifecycleLines(view, runId) {
  const refresh = view && view.refresh;
  if (!refresh || refresh.run_id !== runId) return null;
  const status = refresh.status || "queued";
  const sourceStates = Object.values(view.freshness?.sources || {});
  const sourcesDisabled = sourceStates.length > 0 && sourceStates.every((source) => !source.enabled);
  const lines = [`最近任务状态：${REFRESH_STATUS_COPY[status] || "处理中"}。`];
  if (status === "queued") {
    lines.push("任务仍在排队，尚未确认开始采集。");
  } else if (status === "running") {
    lines.push("任务正在处理，尚未确认采集结果。");
  } else if (status === "partial") {
    lines.push("任务部分完成；部分资料仍不可用或待核对。");
  } else if (status === "failed") {
    lines.push("任务失败；资料没有因本次请求自动变为最新。");
  } else if (status === "success") {
    lines.push("任务流程已结束。重新载入页面后可读取当前已保存资料。");
  }
  if (refresh.reason) {
    lines.push(`状态说明：${REFRESH_REASON_COPY[refresh.reason] || "资料刷新请求"}。`);
  }
  if (sourcesDisabled || view.capabilities?.online_refresh === false) {
    lines.push("来源当前已关闭：本次不会联网采集。");
  }
  if (status !== "success" && status !== "partial" && status !== "failed") {
    lines.push("最近采集成功时间保持按已保存记录显示；当前状态未证明资料已更新。");
  }
  return lines;
}

function ensureProof() {
  let proof = document.querySelector("#financial-refresh-proof");
  if (!proof) {
    document.querySelector("#financial-no-refresh-run")?.remove();
    proof = el("details", { class: "financial-refresh-proof", id: "financial-refresh-proof" });
    proof.appendChild(el("summary", {}, "查看最近任务记录"));
    proof.appendChild(el("dl", { class: "kv-list financial-refresh-proof-list" }));
    document.querySelector("#financial-freshness")?.appendChild(proof);
  }
  return proof;
}

function formatFinancialTimestamp(value) {
  if (!value) return "未知";
  const millis = Date.parse(value);
  if (Number.isNaN(millis)) return String(value);
  return `${new Date(millis).toISOString().slice(0, 19).replace("T", " ")} UTC`;
}

function updateProof(view) {
  const refresh = view?.refresh;
  if (!refresh) return;
  const proof = ensureProof();
  const list = proof.querySelector(".financial-refresh-proof-list");
  if (!list) return;
  const values = [
    ["financial-current-run-id", "任务编号", refresh.run_id],
    ["financial-current-run-status", "任务状态", REFRESH_STATUS_COPY[refresh.status] || "处理中"],
    ["financial-current-run-reason", "状态说明", REFRESH_REASON_COPY[refresh.reason] || (refresh.reason ? "资料刷新请求" : null)],
    ["financial-current-run-started-at", "开始时间", refresh.started_at],
    ["financial-current-run-completed-at", "结束时间", refresh.completed_at],
  ];
  for (const [id, label, value] of values) {
    if (value === null || value === undefined || value === "") continue;
    let target = document.getElementById(id);
    if (!target) {
      const row = el("div", { class: "fin-item" });
      row.appendChild(el("dt", {}, label));
      target = el("dd", { id });
      row.appendChild(target);
      list.appendChild(row);
    }
    target.textContent = id === "financial-current-run-status"
      ? (REFRESH_STATUS_COPY[value] || "处理中")
      : id === "financial-current-run-reason"
        ? (REFRESH_REASON_COPY[value] || "资料刷新请求")
        : id === "financial-current-run-started-at" || id === "financial-current-run-completed-at"
          ? formatFinancialTimestamp(value)
          : String(value);
  }
}

function updateFreshness(view) {
  const freshness = view?.freshness;
  if (!freshness) return;
  const fields = [
    ["financial-last-verified-on", freshness.last_verified_on, false],
    ["financial-latestness-checked-on", freshness.latestness_checked_on, false],
    ["financial-last-imported-at", freshness.last_imported_at, true],
    ["financial-last-requested-at", freshness.last_requested_at, true],
    ["financial-last-attempt-at", freshness.last_attempt_at, true],
    ["financial-last-success-at", freshness.last_success_at, true],
    ["financial-latest-period-end", freshness.latest_period_end, false],
  ];
  for (const [id, value, isTimestamp] of fields) {
    const node = document.getElementById(id);
    if (node) node.textContent = isTimestamp ? formatFinancialTimestamp(value) : (value || "未知");
  }
  const badge = document.querySelector("#financial-freshness [data-latestness]");
  if (badge && freshness.latestness) {
    badge.dataset.latestness = freshness.latestness;
    badge.textContent = { unknown: "未知", checked_through: "已核对至记录日期", stale: "有更新待核" }[freshness.latestness] || "未知";
  }
}

function renderRefreshState(region, lines) {
  renderLines(region, lines);
  region.dataset.lifecycle = "observed";
}

export async function pollRefreshLifecycle(contextUrl, runId, {
  render,
  onUnauthorized,
  pollLimit = REFRESH_POLL_LIMIT,
  intervalMs = REFRESH_POLL_INTERVAL_MS,
} = {}) {
  let latest = null;
  for (let attempt = 0; attempt < pollLimit; attempt += 1) {
    await new Promise((resolve) => setTimeout(resolve, intervalMs));
    try {
      latest = await get(contextUrl);
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) {
        if (onUnauthorized) onUnauthorized();
        return null;
      }
      if (render) render(["请求已登记，但暂时无法读取任务状态；完成情况尚未确认。"]);
      return null;
    }
    if (!latest || latest.refresh?.run_id !== runId) continue;
    updateProof(latest);
    updateFreshness(latest);
    const lines = lifecycleLines(latest, runId);
    if (render && lines) render(lines);
    if (["success", "partial", "failed"].includes(latest.refresh?.status)) return latest;
  }
  if (render) {
    render(["请求已登记；最近任务仍未结束或尚未读取到，采集结果尚未确认。"]);
  }
  return latest;
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
      const result = await requestRefresh(listingId, {
        render: (lines) => renderRefreshState(region, lines),
        onFailure: (error) => renderFailure(region, error),
        onUnauthorized: () => {
          globalThis.location.href = "/app/login";
        },
      });
      const contextUrl = button.getAttribute("data-context-url");
      if (result && contextUrl && Number.isInteger(result.run_id)) {
        await pollRefreshLifecycle(contextUrl, result.run_id, {
          render: (lines) => renderRefreshState(region, lines),
          onUnauthorized: () => {
            globalThis.location.href = "/app/login";
          },
        });
      }
    } finally {
      button.disabled = false;
    }
  });
}
