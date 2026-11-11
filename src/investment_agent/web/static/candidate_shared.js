// 候选列表和详情共用的展示与 DOM 查找。这里不保存列表或详情状态。

import { ApiError, get, lockWhileBusy, patch, post, renderRegion } from "./api.js";
import { attachCombobox } from "./combobox.js";
import { clear, el, fmt, stockLink, table } from "./dom.js";
import {
  CALIBRATION_LABELS,
  CANDIDATE_STATUS_LABELS,
  DECISION_KIND_LABELS,
  DRAFT_STATE_LABELS,
  FLAG_LABELS,
  INDUSTRY_GROUP_LABELS,
  OUTCOME_LABELS,
  QUALITY_LABELS,
  RULE_STATUS_LABELS,
  VERIFICATION_STATUS_LABELS,
  VERIFICATION_TRIGGER_LABELS,
  ZONE_LABELS,
  describeIssues,
  factorLabel,
  formatCondition,
  formatDate,
  formatDateTime,
  formatFactorValue,
  formatReason,
  formatParamValue,
  inRange,
  label,
  paramLabel,
  parseParamName,
  parseRationale,
  formatThreshold,
  PERIOD_KIND_LABELS,
  PROVIDER_LABELS,
  SOURCE_VERIFICATION_LABELS,
  TEMPLATE_LABELS,
  templateLabel,
} from "./format.js";
import { humanizeText } from "./research_format.js";
import { loadSparkBars } from "./market.js";
import { mountPriceChart } from "./pricechart.js";
import { aiStamp, fillSparklines, kpi, legend, scale, timeline } from "./ui.js";

export const LIST_LIMIT = 100;
export const TOP_REASONS = 6;
export const SPARK_DAYS = 120;
export const UNFINISHED_VERIFICATION = new Set(["pending", "deferred", "running"]);
export const ZONE_ORDER = ["buy_zone", "drop", "add", "trim", "exit_sell", "exit_review"];

export function redirectOn401(error) {
  if (error instanceof ApiError && error.status === 401) {
    globalThis.location.href = "/app/login";
    return true;
  }
  return false;
}

// 错误态：自带重试（不走 api.js 的通用 loadRegion，以免按 data-endpoint 渲染原始 JSON）。
export function renderError(node, message, retry) {
  clear(node);
  node.dataset.state = "error";
  node.appendChild(el("p", { class: "notice notice-error", role: "alert" }, message || "加载失败。"));
  if (retry) {
    const button = el("button", { type: "button" }, "重试");
    button.addEventListener("click", retry);
    node.appendChild(button);
  }
}

export function calibrationBadge(status) {
  const value = status || "uncalibrated";
  return el(
    "span",
    {
      class: `badge badge-${value === "uncalibrated" ? "uncalibrated" : "calibrated"}`,
      title: value === "uncalibrated" ? "模板默认参数尚未经回测校准，只是假设" : undefined,
    },
    label(CALIBRATION_LABELS, value)
  );
}

export function chip(text, extraClass = "") {
  return el("span", { class: `chip ${extraClass}`.trim() }, text);
}

export function templateChip(ref) {
  const node = chip(templateLabel(ref), "chip-template");
  node.title = ref;
  return node;
}

// 每日区间检测（WP8）：当前所处区间 + 最近一次进入事件引用的规则版本。
export function zoneDetectionLine(events) {
  const latest = events && events.latest;
  const interval = (events && events.interval) || {};
  if (!latest && !interval.inside) return null;
  const zone = label(ZONE_LABELS, interval.zone || (latest && latest.zone));
  const since = interval.entered_as_of || (latest && latest.as_of);
  const version = latest ? ` · 规则 v${latest.rule_version}` : "";
  const text = interval.inside
    ? `区间检测：${zone} 触发中（${since}）${version}`
    : `区间检测：${zone} 已离开（${since}）${version}`;
  return el("p", { class: interval.inside ? "zone-detection active" : "zone-detection faint" }, text);
}

// 草稿状态只对待处理候选有意义；已关闭的候选看规则本身的状态。
export function draftChip(candidate) {
  return chip(`草稿：${label(DRAFT_STATE_LABELS, candidate.draft_state)}`, `draft-${candidate.draft_state}`);
}

export function industryText(context) {
  if (!context || typeof context !== "object") return "—";
  const group = context.industry_group;
  let text = label(INDUSTRY_GROUP_LABELS, group);
  const industry = context.industry;
  if (industry && typeof industry === "object") {
    if (industry.fallback) text += "（证监会行业回退）";
    else if (industry.system === "sw2021" && industry.code) {
      text += industry.name ? ` · 申万 ${industry.name}` : `（申万 ${industry.code}）`;
    }
  }
  return text;
}

export function primaryTemplate(candidate) {
  const sources = Array.isArray(candidate.sources) ? candidate.sources : [];
  for (let index = sources.length - 1; index >= 0; index -= 1) {
    if (sources[index].template_id) return sources[index].template_id;
  }
  return null;
}

export function newestVerificationByListing(items) {
  const map = new Map();
  for (const item of items || []) {
    if (!map.has(item.listing_id)) map.set(item.listing_id, item);
  }
  return map;
}

export function verificationText(request) {
  if (!request) return "无核对请求";
  const status = label(VERIFICATION_STATUS_LABELS, request.status);
  if (request.status === "deferred" && request.not_before) {
    return `iFinD ${status}（至 ${formatDateTime(request.not_before)}）`;
  }
  if (request.status === "done" && request.result_summary) {
    const conflicts = request.result_summary.conflicts || [];
    return conflicts.length ? `iFinD 已完成，${conflicts.length} 项冲突` : "iFinD 已完成，无冲突";
  }
  return `iFinD ${status}`;
}

export const NEGATIVE_ZONE = /(^|_)(drop|exit|trim|remove)(_|$)/;
export const DISMISS_NOTE = "30 天内筛选不再自动提名该标的。";

export function metCount(candidate) {
  const reasons = candidate.latest_screen && Array.isArray(candidate.latest_screen.reasons) ? candidate.latest_screen.reasons : [];
  return { met: reasons.filter((reason) => reason.outcome === "met").length, total: reasons.length };
}

export function topReasons(screen) {
  if (!screen || !Array.isArray(screen.reasons)) return [];
  const met = screen.reasons.filter((reason) => reason.outcome === "met");
  return (met.length ? met : screen.reasons).slice(0, TOP_REASONS);
}

// 一条理由拆成「名称 实际值 条件」；缺失 / 不适用时退回 formatReason 的整句。
export function reasonParts(reason) {
  if (!reason || !reason.factor || reason.outcome === "not_applicable") return null;
  if (reason.actual === null || reason.actual === undefined) return null;
  return {
    name: factorLabel(reason.factor),
    actual: formatFactorValue(reason.actual, reason.unit),
    cond: formatThreshold(reason.op, reason.threshold, reason.unit),
  };
}

export function reasonLine(reason, prefix = "") {
  const parts = reasonParts(reason);
  const line = el("span", { class: "cand-reason-text" });
  if (!parts) {
    line.textContent = `${prefix}${formatReason(reason)}`;
    return line;
  }
  line.appendChild(document.createTextNode(`${prefix}${parts.name} `));
  line.appendChild(el("b", {}, parts.actual));
  line.appendChild(el("span", { class: "muted" }, ` ${parts.cond}`));
  return line;
}

export function inlineReason(reason, prefix = "") {
  const parts = reasonParts(reason);
  return parts ? `${prefix}${parts.name} ${parts.actual}` : `${prefix}${formatReason(reason)}`;
}

// 入选理由的统一数据：筛选命中 / 手动提名的草稿检查 / AI 起草分析 / 无理由。
export function whyModel(candidate) {
  const screen = candidate.latest_screen;
  const allReasons = screen && Array.isArray(screen.reasons) ? screen.reasons : [];
  const shown = topReasons(screen);
  if (shown.length) {
    const { met, total } = metCount(candidate);
    return {
      kind: "screen",
      verb: "命中",
      met,
      total,
      items: shown.map((reason) => ({ reason, ok: reason.outcome === "met", prefix: "" })),
      more: allReasons.length - shown.length,
    };
  }
  if (Array.isArray(candidate.draft_checks) && candidate.draft_checks.length) {
    // 手动提名：草稿规则的候选区间 × 本股因子，产出与筛选命中同构的检查项。
    // 移出类区间（drop / exit / trim / remove）"满足"意味着离场信号，✓/! 按是否有利翻转，计数只统计有利项。
    const items = candidate.draft_checks.map((reason) => {
      const zone = String(reason.rule_path || "").split(".")[0];
      const negative = NEGATIVE_ZONE.test(zone);
      const ok = negative ? reason.outcome !== "met" : reason.outcome === "met";
      const zoneLabel = label(ZONE_LABELS, zone);
      return { reason, ok, prefix: zoneLabel !== zone ? `${zoneLabel}·` : "" };
    });
    return {
      kind: "draft",
      verb: "满足",
      met: items.filter((item) => item.ok).length,
      total: items.length,
      items: items.slice(0, TOP_REASONS),
      more: Math.max(0, items.length - TOP_REASONS),
    };
  }
  if (candidate.draft_overall_rationale) {
    // 手动提名的候选：起草完成后用 LLM 的整体分析替代占位文案，按句拆成列表。
    const sentences = candidate.draft_overall_rationale
      .split(/(?<=[。！？；;])/)
      .map((sentence) => sentence.trim())
      .filter(Boolean);
    return { kind: "text", sentences: sentences.slice(0, 4) };
  }
  return { kind: "none" };
}

export function truncate(text, limit) {
  return text.length > limit ? `${text.slice(0, limit)}…` : text;
}

export function badge(text, kind = "", title) {
  return el("span", { class: `badge ${kind}`.trim(), title }, text);
}

export function templateBadge(ref) {
  const [id, version] = String(ref).split("@");
  const node = badge(label(TEMPLATE_LABELS, id), "accent", ref);
  if (version) node.appendChild(el("span", { class: "cand-ver" }, ` v${version}`));
  return node;
}

export function draftBadge(candidate) {
  const kind = { ready: "ai", failed: "warn", pending: "outline", none: "outline" }[candidate.draft_state] || "outline";
  return badge(label(DRAFT_STATE_LABELS, candidate.draft_state), kind);
}

export function pctChange(from, to) {
  return from ? ((to - from) / from) * 100 : null;
}

export function pctNode(value) {
  const cls = value > 0 ? "pc-up" : value < 0 ? "pc-down" : "pc-flat";
  return el("span", { class: `price-chg ${cls}` }, value === null ? "—" : `${value > 0 ? "+" : ""}${value.toFixed(2)}%`);
}

export function section(id) {
  return document.querySelector(`#${id}`);
}

export function sectionReady(node, content) {
  clear(node);
  node.dataset.state = "ready";
  if (content) node.appendChild(content);
}

export function isNumber(value) {
  return typeof value === "number" && Number.isFinite(value);
}

export function showIssues(target, error, prefix) {
  if (!target) return;
  clear(target);
  const lines = error instanceof ApiError ? describeIssues(error.detail) : [];
  target.appendChild(el("p", { class: "notice notice-error" }, `${prefix}：${error.message || "请求失败"}`));
  if (lines.length && !(lines.length === 1 && lines[0] === error.message)) {
    const list = el("ul", { class: "issue-list" });
    for (const line of lines) list.appendChild(el("li", {}, line));
    target.appendChild(list);
  }
}

