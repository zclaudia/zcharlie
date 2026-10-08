// 候选页（P1-E1）：本周候选列表、候选详情（理由优先 → 规则草稿审阅 → 处理 →
// iFinD 核对 → 决策记录）与今日概览的"本周候选"计数。
// 不展示 0～100 综合分或强度值；排序只用筛选名次与开启时间。
// 所有 DOM 经 dom.js 构造（textContent / setAttribute），不写 HTML 字符串。

import { ApiError, get, lockWhileBusy, patch, post, renderRegion } from "./api.js";
import { clear, el, fmt, stockLink } from "./dom.js";
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
import { aiStamp, fillSparklines, kpi, legend, scale, timeline } from "./ui.js";

export const CANDIDATE_REGIONS = new Set(["candidates-list", "candidate-detail"]);

const LIST_LIMIT = 100;
const TOP_REASONS = 3;
const SPARK_DAYS = 120;
const UNFINISHED_VERIFICATION = new Set(["pending", "deferred", "running"]);
const ZONE_ORDER = ["buy_zone", "drop", "add", "trim", "exit_review"];

function redirectOn401(error) {
  if (error instanceof ApiError && error.status === 401) {
    globalThis.location.href = "/app/login";
    return true;
  }
  return false;
}

// 错误态：自带重试（不走 api.js 的通用 loadRegion，以免按 data-endpoint 渲染原始 JSON）。
function renderError(node, message, retry) {
  clear(node);
  node.dataset.state = "error";
  node.appendChild(el("p", { class: "notice notice-error", role: "alert" }, message || "加载失败。"));
  if (retry) {
    const button = el("button", { type: "button" }, "重试");
    button.addEventListener("click", retry);
    node.appendChild(button);
  }
}

function calibrationBadge(status) {
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

function chip(text, extraClass = "") {
  return el("span", { class: `chip ${extraClass}`.trim() }, text);
}

function templateChip(ref) {
  const node = chip(templateLabel(ref), "chip-template");
  node.title = ref;
  return node;
}

// 草稿状态只对待处理候选有意义；已关闭的候选看规则本身的状态。
function draftChip(candidate) {
  return chip(`草稿：${label(DRAFT_STATE_LABELS, candidate.draft_state)}`, `draft-${candidate.draft_state}`);
}

function industryText(context) {
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

function primaryTemplate(candidate) {
  const sources = Array.isArray(candidate.sources) ? candidate.sources : [];
  for (let index = sources.length - 1; index >= 0; index -= 1) {
    if (sources[index].template_id) return sources[index].template_id;
  }
  return null;
}

function newestVerificationByListing(items) {
  const map = new Map();
  for (const item of items || []) {
    if (!map.has(item.listing_id)) map.set(item.listing_id, item);
  }
  return map;
}

function verificationText(request) {
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

// ---- 今日概览：本周候选计数 --------------------------------------------

export function candidatesBucket(counts) {
  const card = el("section", { class: "card candidates-bucket" });
  const heading = el("div", { class: "card-head bucket-title" });
  heading.appendChild(el("h2", {}, "本周候选"));
  heading.appendChild(el("a", { href: "/app/candidates" }, "查看候选与规则草稿 →"));
  card.appendChild(heading);
  const grid = el("div", { class: "kpis" });
  const number = (value) => (typeof value === "number" ? value : 0);
  for (const [text, value, kind] of [
    ["待处理候选", number(counts.open), ""],
    ["草稿待审阅", number(counts.drafts_ready), number(counts.drafts_ready) ? "ai" : "zero"],
    ["起草中", number(counts.drafting), number(counts.drafting) ? "" : "zero"],
    ["起草失败", number(counts.draft_failed), number(counts.draft_failed) ? "attention" : "zero"],
    ["iFinD 核对未完成", number(counts.verification_pending), number(counts.verification_pending) ? "" : "zero"],
  ]) {
    grid.appendChild(kpi(text, value, { href: "/app/candidates", kind }));
  }
  card.appendChild(grid);
  return card;
}

// ---- 候选列表 ---------------------------------------------------------

const listState = { items: [], verifications: new Map() };

function sortCandidates(items) {
  return [...items].sort((a, b) => {
    const dayA = formatDate(a.opened_at);
    const dayB = formatDate(b.opened_at);
    if (dayA !== dayB) return dayA < dayB ? 1 : -1;
    const rankA = a.latest_screen && typeof a.latest_screen.rank === "number" ? a.latest_screen.rank : Infinity;
    const rankB = b.latest_screen && typeof b.latest_screen.rank === "number" ? b.latest_screen.rank : Infinity;
    if (rankA !== rankB) return rankA - rankB;
    return a.ticker < b.ticker ? -1 : a.ticker > b.ticker ? 1 : 0;
  });
}

function topReasons(screen) {
  if (!screen || !Array.isArray(screen.reasons)) return [];
  const met = screen.reasons.filter((reason) => reason.outcome === "met");
  return (met.length ? met : screen.reasons).slice(0, TOP_REASONS);
}

function candidateItem(candidate) {
  const item = el("article", { class: "candidate-item", "data-candidate-id": candidate.id });
  const head = el("header", { class: "candidate-head" });
  const title = el("h2", { class: "candidate-title" });
  title.appendChild(
    el(
      "a",
      { href: `/app/candidates/${encodeURIComponent(candidate.id)}` },
      `${candidate.ticker} ${candidate.name || ""}`.trim()
    )
  );
  head.appendChild(title);
  if (candidate.status !== "open") {
    head.appendChild(chip(label(CANDIDATE_STATUS_LABELS, candidate.status), `state-${candidate.status}`));
  }
  // 右上角日线迷你图（B3）：先占位，列表渲染后异步替换。
  head.appendChild(
    el("span", { class: "spark spark-pending spark-slot", "data-spark": candidate.ticker, "aria-hidden": "true" })
  );
  item.appendChild(head);

  const screen = candidate.latest_screen;
  const meta = el("p", { class: "candidate-meta muted" });
  meta.appendChild(el("span", {}, `行业组：${industryText(screen && screen.context)}`));
  meta.appendChild(
    el("span", {}, `开启 ${formatDate(candidate.opened_at)} · 到期 ${formatDate(candidate.expires_at)}`)
  );
  item.appendChild(meta);

  const templates = el("p", { class: "candidate-templates" });
  const refs = Array.isArray(candidate.template_refs) ? candidate.template_refs : [];
  if (refs.length) {
    for (const ref of refs) templates.appendChild(templateChip(ref));
  } else {
    templates.appendChild(chip("手动提名", "chip-template"));
  }
  templates.appendChild(calibrationBadge(candidate.calibration_status));
  item.appendChild(templates);

  const reasons = topReasons(screen);
  if (reasons.length) {
    const list = el("ul", { class: "reason-list" });
    for (const reason of reasons) list.appendChild(el("li", {}, formatReason(reason)));
    item.appendChild(list);
  } else {
    item.appendChild(el("p", { class: "muted" }, "无筛选理由（手动提名）。"));
  }

  const states = el("p", { class: "candidate-states" });
  if (candidate.status === "open") states.appendChild(draftChip(candidate));
  const request = listState.verifications.get(candidate.listing_id);
  states.appendChild(
    chip(verificationText(request), request && UNFINISHED_VERIFICATION.has(request.status) ? "chip-pending" : "")
  );
  item.appendChild(states);
  return item;
}

function renderCandidateList(region) {
  const templateFilter = document.querySelector("#candidates-template-filter");
  const template = templateFilter ? templateFilter.value : "";
  const items = sortCandidates(listState.items).filter(
    (candidate) => !template || (candidate.template_refs || []).includes(template)
  );
  if (!items.length) {
    renderRegion(region, "empty", template ? "该模板下没有候选。" : undefined);
    return;
  }
  const container = el("div", { class: "candidate-list" });
  for (const candidate of items) container.appendChild(candidateItem(candidate));
  renderRegion(region, "ready", container);
  fillSparklines(
    container.querySelectorAll("[data-spark]"),
    (ticker) => get(`/market/${encodeURIComponent(ticker)}/bars?days=${SPARK_DAYS}`)
  );
}

function refreshTemplateOptions() {
  const select = document.querySelector("#candidates-template-filter");
  if (!select) return;
  const current = select.value;
  const refs = new Set();
  for (const candidate of listState.items) {
    for (const ref of candidate.template_refs || []) refs.add(ref);
  }
  clear(select);
  select.appendChild(el("option", { value: "" }, "全部模板"));
  for (const ref of [...refs].sort()) select.appendChild(el("option", { value: ref }, templateLabel(ref)));
  select.value = refs.has(current) ? current : "";
}

async function loadCandidateList(region) {
  const statusFilter = document.querySelector("#candidates-status-filter");
  const status = (statusFilter && statusFilter.value) || "open";
  renderRegion(region, "loading");
  try {
    const params = new URLSearchParams({ status, limit: String(LIST_LIMIT) });
    const [page, verifications] = await Promise.all([
      get(`/candidates?${params.toString()}`),
      get("/verification/requests?limit=200").catch((error) => {
        if (error instanceof ApiError && error.status === 401) throw error;
        return null;
      }),
    ]);
    listState.items = Array.isArray(page && page.items) ? page.items : [];
    listState.verifications = newestVerificationByListing(verifications && verifications.items);
    refreshTemplateOptions();
    renderCandidateList(region);
    const total = document.querySelector("#candidates-total");
    if (total && page) {
      total.textContent = `共 ${page.total} 个${label(CANDIDATE_STATUS_LABELS, status)}候选${
        page.total > listState.items.length ? `，显示最新 ${listState.items.length} 个` : ""
      }。`;
    }
  } catch (error) {
    if (redirectOn401(error)) return;
    renderError(region, error.message, () => loadCandidateList(region));
  }
}

// ---- 手动提名 ---------------------------------------------------------

// "600900" -> "600900.SH"：6 开头沪市，0/3 深市，4/8/9 北交所；已带后缀的只转大写。
export function normalizeTicker(raw) {
  const text = String(raw || "").trim().toUpperCase();
  if (/^\d{6}\.(SH|SZ|BJ)$/.test(text)) return text;
  if (!/^\d{6}$/.test(text)) return null;
  const exchange = { 6: "SH", 0: "SZ", 3: "SZ", 4: "BJ", 8: "BJ", 9: "BJ" }[text[0]];
  return exchange ? `${text}.${exchange}` : null;
}

function nominateNotice(region, content, level = "") {
  if (!region) return;
  const node = typeof content === "string" ? el("p", { class: `notice ${level}`.trim() }, content) : content;
  renderRegion(region, "ready", node);
}

function wireNominateForm(form, list) {
  const select = form.querySelector('select[name="template_id"]');
  if (select) {
    for (const [id, name] of Object.entries(TEMPLATE_LABELS)) {
      select.appendChild(el("option", { value: id }, name));
    }
  }
  const result = section("candidate-nominate-result");
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = new FormData(form);
    const ticker = normalizeTicker(data.get("ticker"));
    if (!ticker) {
      nominateNotice(result, "代码格式不对：填 6 位代码，或带 .SH / .SZ / .BJ 后缀。", "notice-error");
      return;
    }
    const note = String(data.get("note") || "").trim();
    const payload = { ticker, template_id: String(data.get("template_id") || "") || null };
    if (note) payload.note = note;
    const submit = form.querySelector('button[type="submit"]');
    const promise = post("/candidates", payload, { idempotency: false })
      .then((body) => {
        form.reset();
        const candidate = body.candidate;
        const box = el("p", { class: "notice" });
        box.appendChild(
          document.createTextNode(
            `${body.created ? "已提名" : "已有待处理候选，已追加手动来源："}${candidate.ticker} ${candidate.name || ""}`.trim()
          )
        );
        box.appendChild(document.createTextNode(body.draft_run_id ? "，规则草稿起草中。 " : "。 "));
        box.appendChild(el("a", { href: `/app/candidates/${encodeURIComponent(candidate.id)}` }, "查看候选"));
        nominateNotice(result, box);
        const statusFilter = section("candidates-status-filter");
        if (statusFilter) statusFilter.value = "open";
        if (list) loadCandidateList(list);
      })
      .catch((error) => {
        if (redirectOn401(error)) return;
        const conflict = error instanceof ApiError && error.status === 409;
        nominateNotice(result, conflict ? `提名失败：${ticker} 已在自选股中，无需再提名。` : `提名失败：${error.message}`, "notice-error");
      });
    if (submit) lockWhileBusy(submit, promise);
    await promise;
  });
}

// ---- 候选详情 ---------------------------------------------------------

const detailState = { candidate: null, busy: false };

function section(id) {
  return document.querySelector(`#${id}`);
}

function sectionReady(node, content) {
  clear(node);
  node.dataset.state = "ready";
  if (content) node.appendChild(content);
}

function showInspector(reason, context) {
  const inspector = section("candidate-inspector");
  if (!inspector) return;
  clear(inspector);
  const head = el("div", { class: "inspector-head" });
  head.appendChild(el("h3", { title: reason.factor }, `来源：${factorLabel(reason.factor)}`));
  const close = el("button", { type: "button", class: "link-button" }, "关闭");
  close.addEventListener("click", () => {
    inspector.hidden = true;
  });
  head.appendChild(close);
  inspector.appendChild(head);

  inspector.appendChild(
    kvRows([
      ["条件", formatReason(reason)],
      ["参数", Array.isArray(reason.param) ? reason.param.filter(Boolean).map(paramLabel).join("、") : reason.param ? paramLabel(reason.param) : null],
      ["实际值", formatFactorValue(reason.actual, reason.unit)],
      ["判定", label(OUTCOME_LABELS, reason.outcome)],
      ["质量", label(QUALITY_LABELS, reason.quality)],
      ["标记", (reason.flags || []).map((flag) => label(FLAG_LABELS, flag)).join("、") || "无"],
      ["数据提供方", context && context.provider ? label(PROVIDER_LABELS, context.provider) : null],
      ["快照日期", context && context.as_of],
    ])
  );

  const ref = reason.snapshot_ref && typeof reason.snapshot_ref === "object" ? reason.snapshot_ref : {};
  const periods = Array.isArray(ref.financial) ? ref.financial : [];
  for (const period of periods) {
    inspector.appendChild(el("h4", {}, `财报期 ${fmt(period.report_period)}`));
    const rows = [
      ["报告期", period.report_period],
      ["类型", label(PERIOD_KIND_LABELS, period.period_kind)],
      ["可得日期", period.available_from],
      ["可得性", period.availability === "actual" ? "已披露" : period.availability],
    ];
    const sources = period.sources && typeof period.sources === "object" ? period.sources : {};
    for (const [field, source] of Object.entries(sources)) {
      if (!source || typeof source !== "object") continue;
      const parts = [`来源 ${label(PROVIDER_LABELS, source.provider)}`, label(SOURCE_VERIFICATION_LABELS, source.verification)];
      if (source.approx) parts.push("近似");
      if (Array.isArray(source.conflicts) && source.conflicts.length) parts.push(`冲突 ${source.conflicts.length} 项`);
      rows.push([factorLabel(field), parts.join(" · ")]);
    }
    inspector.appendChild(kvRows(rows));
  }
  const market = ref.market && typeof ref.market === "object" ? ref.market : null;
  if (market) {
    inspector.appendChild(el("h4", {}, "行情"));
    inspector.appendChild(kvRows([["交易日", market.trade_date]]));
  }
  const rest = Object.entries(ref).filter(([key]) => key !== "financial" && key !== "market");
  if (rest.length) {
    inspector.appendChild(el("h4", {}, "计算口径"));
    inspector.appendChild(kvRows(rest.map(([key, value]) => refRow(key, value))));
  }
  if (!periods.length && !market && !rest.length) {
    inspector.appendChild(el("p", { class: "notice notice-warn" }, "该理由没有快照引用（因子缺失或不适用）。"));
  }
  inspector.hidden = false;
  inspector.focus();
}

// snapshot_ref 中除财报 / 行情外的口径字段（分位窗口、派生来源等）。
const REF_LABELS = Object.freeze({
  column: "估值字段",
  current: "当前值",
  current_date: "当前值日期",
  window: "统计窗口",
  sessions: "窗口交易日",
  valid_sessions: "有效交易日",
  excluded_non_positive: "剔除非正值",
  first_valid: "首个有效日",
  years_covered: "覆盖年数",
  basis: "口径",
  field: "字段",
  total_shares: "总股本来源",
});

function refRow(key, value) {
  const name = label(REF_LABELS, key);
  if (key === "column" || key === "field" || key === "basis") return [name, factorLabel(String(value))];
  if (key === "window" && Array.isArray(value)) return [name, value.join(" – ")];
  if (key === "current" && typeof value === "number") return [name, value.toFixed(2)];
  if (value && typeof value === "object") {
    const parts = [value.report_period && `报告期 ${value.report_period}`, value.available_from && `可得 ${value.available_from}`];
    return [name, parts.filter(Boolean).join(" · ") || "—"];
  }
  return [name, value];
}

function kvRows(pairs) {
  const list = el("dl", { class: "kv-list" });
  for (const [key, value] of pairs) {
    list.appendChild(el("dt", {}, key));
    list.appendChild(el("dd", {}, fmt(value)));
  }
  return list;
}

function renderHeader(candidate) {
  const title = section("candidate-title");
  if (title) {
    clear(title);
    title.appendChild(document.createTextNode(`${candidate.name || "候选"} `));
    title.appendChild(el("span", { class: "code" }, candidate.ticker));
  }
  const node = section("candidate-header");
  if (!node) return;
  const box = el("div", { class: "entity-head" });
  const left = el("div");
  const line = el("p", { class: "candidate-states" });
  line.appendChild(chip(label(CANDIDATE_STATUS_LABELS, candidate.status), `state-${candidate.status}`));
  if (candidate.status === "open") line.appendChild(draftChip(candidate));
  for (const ref of candidate.template_refs || []) line.appendChild(templateChip(ref));
  line.appendChild(calibrationBadge(candidate.calibration_status));
  left.appendChild(line);
  const meta = el("p", { class: "meta-row" });
  const screen = candidate.latest_screen;
  if (screen && screen.context) meta.appendChild(el("span", {}, `行业组 ${industryText(screen.context)}`));
  const sources = (candidate.sources || []).map((source) =>
    source.kind === "screen"
      ? `筛选 #${fmt(source.screen_run_id)}（${label(TEMPLATE_LABELS, source.template_id)}，数据日 ${fmt(source.as_of)}）`
      : `手动提名${source.template_id ? `（${label(TEMPLATE_LABELS, source.template_id)}）` : ""}`
  );
  if (sources.length) meta.appendChild(el("span", {}, `来源：${sources.join("；")}`));
  if (candidate.closed_at) meta.appendChild(el("span", {}, `关闭 ${formatDate(candidate.closed_at)}`));
  const research = el("span");
  research.appendChild(el("span", {}, "单股研究："));
  research.appendChild(stockLink(candidate.ticker));
  meta.appendChild(research);
  left.appendChild(meta);
  if (candidate.note) left.appendChild(el("p", {}, `备注：${candidate.note}`));
  box.appendChild(left);
  if (candidate.status === "open") {
    box.appendChild(timeline(candidate.opened_at, candidate.expires_at));
  } else {
    box.appendChild(el("p", { class: "faint" }, `开启 ${formatDate(candidate.opened_at)} · 到期 ${formatDate(candidate.expires_at)}`));
  }
  sectionReady(node, box);
}

function isNumber(value) {
  return typeof value === "number" && Number.isFinite(value);
}

// 一条理由的刻度条：满足区间着色、阈值竖线、实际值圆点；只画 API 给出的数。
function reasonScale(reason) {
  const unit = typeof reason.unit === "string" ? reason.unit : "";
  if (!isNumber(reason.actual) || unit.startsWith("bool")) return null;
  const bounds =
    reason.op === "between" && Array.isArray(reason.threshold) ? reason.threshold.filter(isNumber) : [reason.threshold];
  if (!bounds.length || !bounds.every(isNumber)) return null;
  const percentile = unit.includes("(0,1]");
  const values = [reason.actual, ...bounds];
  const domain = percentile
    ? [0, 1]
    : [Math.min(0, ...values), Math.max(...values.map((value) => value * (value >= 0 ? 1.35 : 0.65)), 0) || 1];
  const show = (value) => formatFactorValue(value, unit, { precise: false });
  let zone = null;
  if (reason.op === "between" && bounds.length === 2) zone = { from: bounds[0], to: bounds[1] };
  else if (reason.op === ">=" || reason.op === ">") zone = { from: bounds[0], to: domain[1] };
  else if (reason.op === "<=" || reason.op === "<") zone = { from: domain[0], to: bounds[0] };
  return scale({
    domain,
    zones: zone ? [zone] : [],
    thresholds: bounds.map((at) => ({ at, label: show(at) })),
    markers: [{ at: reason.actual, title: `本股 ${formatFactorValue(reason.actual, unit)}` }],
    ticks: percentile
      ? [
          { at: 0, label: "0" },
          { at: 0.2 },
          { at: 0.5, label: "50%" },
          { at: 0.8 },
          { at: 1, label: "100%" },
        ]
      : [],
    label: formatReason(reason),
  });
}

function reasonSource(reason, context) {
  const ref = reason.snapshot_ref && typeof reason.snapshot_ref === "object" ? reason.snapshot_ref : {};
  const parts = [];
  const period = Array.isArray(ref.financial) && ref.financial.length ? ref.financial[0] : null;
  if (period && period.report_period) parts.push(`报告期 ${period.report_period}`);
  if (ref.market && ref.market.trade_date) parts.push(`交易日 ${ref.market.trade_date}`);
  if (ref.current_date && ref.years_covered) parts.push(`分位窗口 ${fmt(ref.years_covered)} 年`);
  const provider = context && context.provider;
  if (provider) parts.push(label(PROVIDER_LABELS, provider));
  const sources = period && period.sources && typeof period.sources === "object" ? Object.values(period.sources) : [];
  if (sources.some((source) => source && source.verification === "verified")) parts.push("iFinD 已核对");
  else if (sources.length) parts.push("未经 iFinD 核对");
  return parts.join(" · ");
}

function renderReasons(candidate) {
  const node = section("candidate-reasons");
  if (!node) return;
  const results = Array.isArray(candidate.screen_results) ? candidate.screen_results : [];
  if (!results.length) {
    renderRegion(node, "empty", "手动提名：没有筛选理由。");
    return;
  }
  const box = el("div");
  for (const result of results) {
    const block = el("div", { class: "reason-block" });
    const heading = el("h3");
    heading.appendChild(el("span", { title: result.template_ref }, templateLabel(result.template_ref)));
    heading.appendChild(calibrationBadge(result.calibration_status));
    block.appendChild(heading);
    block.appendChild(
      el(
        "p",
        { class: "faint" },
        `筛选 #${result.screen_run_id} · 数据日 ${fmt(result.as_of)} · 来源 ${label(PROVIDER_LABELS, result.context && result.context.provider)} · 行业组 ${industryText(result.context)}`
      )
    );
    const list = el("ul", { class: "checklist" });
    for (const reason of result.reasons || []) {
      const item = el("li", { class: "check-item" });
      const mark =
        reason.outcome === "met"
          ? el("span", { class: "check-mark", "aria-label": "满足" }, "✓")
          : reason.outcome === "not_applicable"
            ? el("span", { class: "check-mark na", "aria-label": "不适用" }, "–")
            : el("span", { class: "check-mark no", "aria-label": label(OUTCOME_LABELS, reason.outcome) }, "!");
      item.appendChild(mark);
      const main = el("div", { class: "check-main" });
      const button = el(
        "button",
        { type: "button", class: "reason-link", "aria-controls": "candidate-inspector" },
        factorLabel(reason.factor)
      );
      button.addEventListener("click", () => showInspector(reason, result.context));
      main.appendChild(button);
      main.appendChild(document.createElement("br"));
      main.appendChild(
        el(
          "span",
          { class: "check-value" },
          isNumber(reason.actual) || typeof reason.actual === "boolean"
            ? formatFactorValue(reason.actual, reason.unit)
            : label(OUTCOME_LABELS, reason.outcome)
        )
      );
      main.appendChild(
        el(
          "span",
          { class: "check-rule" },
          reason.outcome === "not_applicable" ? "不适用，条件跳过" : `条件 ${formatThreshold(reason.op, reason.threshold, reason.unit)}`
        )
      );
      const flags = el("span", { class: "check-flags" });
      if (reason.quality && reason.quality !== "ok") flags.appendChild(chip(label(QUALITY_LABELS, reason.quality), "chip-warn"));
      for (const flag of reason.flags || []) flags.appendChild(chip(label(FLAG_LABELS, flag) , "chip-warn"));
      if (flags.children.length) main.appendChild(flags);
      item.appendChild(main);
      const meter = reasonScale(reason);
      if (meter) {
        const holder = el("div", { class: "check-scale" });
        holder.appendChild(meter);
        item.appendChild(holder);
      }
      const source = reasonSource(reason, result.context);
      if (source) item.appendChild(el("p", { class: "check-src" }, source));
      list.appendChild(item);
    }
    block.appendChild(list);
    box.appendChild(block);
  }
  box.appendChild(el("p", { class: "faint" }, "点击因子名查看快照来源（报告期、可得日期、提供方、字段核对状态）。"));
  sectionReady(node, box);
}

// 最新筛选中各因子的实际值（用于在区间条件旁标出"本股当前"）。
function currentFactorValues(candidate) {
  const map = new Map();
  for (const result of candidate.screen_results || []) {
    for (const reason of result.reasons || []) {
      if (isNumber(reason.actual) && !map.has(reason.factor)) map.set(reason.factor, reason);
    }
  }
  return map;
}

function conditionLeaf(condition, params, units, current) {
  const { text, params: names } = formatCondition(condition, params, units);
  const leaf = el("li", {}, text);
  const now = current && current.get(condition.factor);
  if (now) leaf.appendChild(el("span", { class: "cond-now" }, `本股 ${formatFactorValue(now.actual, now.unit)}`));
  if (names.length) leaf.title = `参数：${names.map(paramLabel).join("、")}`;
  return leaf;
}

function conditionTree(group, params, units, current) {
  const list = el("ul", { class: "condition-tree" });
  if (!group || typeof group !== "object") return list;
  if (group.factor) {
    list.appendChild(conditionLeaf(group, params, units, current));
    return list;
  }
  const mode = Array.isArray(group.all) ? "all" : "any";
  const children = group[mode] || [];
  const item = el("li");
  item.appendChild(el("span", { class: "muted" }, mode === "all" ? "全部满足：" : "任一满足："));
  const inner = el("ul", { class: "condition-tree" });
  for (const child of children) {
    if (child && child.factor) {
      inner.appendChild(conditionLeaf(child, params, units, current));
    } else {
      for (const nested of conditionTree(child, params, units, current).children) inner.appendChild(nested);
    }
  }
  item.appendChild(inner);
  list.appendChild(item);
  return list;
}

function paramNames(rule) {
  return [
    ...new Set([
      ...Object.keys(rule.template_params || {}),
      ...Object.keys(rule.param_ranges || {}),
      ...Object.keys(rule.params || {}),
    ]),
  ].sort();
}

function editedParams(rule) {
  const changes = {};
  for (const input of document.querySelectorAll("input[data-param]")) {
    const name = input.dataset.param;
    const value = Number(input.value);
    if (input.value.trim() === "" || !Number.isFinite(value)) continue;
    if (value !== (rule.effective_params || {})[name]) changes[name] = value;
  }
  return changes;
}

function invalidParams() {
  return [...document.querySelectorAll("input[data-param]")].filter(
    (input) => input.getAttribute("aria-invalid") === "true"
  );
}

// 参数行：中文名 + 标识符；刻度条（允许范围 / 默认 / 当前 / 本股值）；可编辑输入；起草理由。
function paramRow(rule, name, rationale, editable, current) {
  const defaults = rule.template_params || {};
  const effective = (rule.effective_params || {})[name];
  const range = (rule.param_ranges || {})[name];
  const units = rule.factor_units || {};
  const show = (value) => formatParamValue(name, value, units);
  const overridden =
    Object.prototype.hasOwnProperty.call(rule.params || {}, name) && rule.params[name] !== defaults[name];
  const byAi = String(rule.drafted_by || "").startsWith("llm:");
  const row = el("li", { class: `param${overridden ? " changed" : ""}`, "data-param-row": name });

  const head = el("div");
  head.appendChild(el("span", { class: "param-name" }, paramLabel(name)));
  head.appendChild(el("code", { class: "param-id" }, name));
  const vals = el("div", { class: "param-vals" });
  if (overridden) {
    vals.appendChild(document.createTextNode(`默认 ${show(defaults[name])} → `));
    vals.appendChild(el("span", { class: "new" }, `${byAi ? "AI 建议 " : ""}${show(effective)}`));
  } else {
    vals.appendChild(document.createTextNode(`默认 ${show(defaults[name])}`));
  }
  if (range) vals.appendChild(document.createTextNode(` · 允许 ${show(range.min)} – ${show(range.max)}`));
  head.appendChild(vals);
  row.appendChild(head);

  const parsed = parseParamName(name);
  const stock = parsed && current ? current.get(parsed.factor) : null;
  if (range && isNumber(range.min) && isNumber(range.max)) {
    const holder = el("div", { class: "param-scale" });
    const markers = [];
    if (stock && stock.actual >= range.min && stock.actual <= range.max) {
      markers.push({ at: stock.actual, cls: "s-marker-hollow", title: `本股 ${formatFactorValue(stock.actual, stock.unit)}` });
    }
    if (isNumber(effective)) {
      markers.push({ at: effective, cls: overridden && byAi ? "s-marker-ai" : "s-marker", title: `当前 ${show(effective)}` });
    }
    holder.appendChild(
      scale({
        domain: [range.min, range.max],
        defaults: isNumber(defaults[name]) ? [defaults[name]] : [],
        markers,
        ticks: [
          { at: range.min, label: show(range.min) },
          { at: range.max, label: show(range.max) },
        ],
        compact: true,
      })
    );
    row.appendChild(holder);
  }

  const edit = el("div", { class: "param-edit" });
  const hint = el("span", { class: "range-hint", id: `param-hint-${name}`, "aria-live": "polite" });
  if (editable) {
    const input = el("input", {
      type: "number",
      step: "any",
      inputmode: "decimal",
      value: effective,
      min: range ? range.min : undefined,
      max: range ? range.max : undefined,
      "data-param": name,
      "aria-label": `参数 ${paramLabel(name)}`,
      "aria-describedby": hint.id,
    });
    input.addEventListener("input", () => {
      const value = Number(input.value);
      const ok = input.value.trim() !== "" && (!range || inRange(value, range));
      input.setAttribute("aria-invalid", ok ? "false" : "true");
      hint.textContent = ok
        ? show(value)
        : range
          ? `超出允许范围 ${show(range.min)} – ${show(range.max)}`
          : "请输入数字";
    });
    edit.appendChild(input);
    hint.textContent = show(effective);
  } else {
    hint.textContent = show(effective);
  }
  edit.appendChild(hint);
  row.appendChild(edit);

  const entry = rationale.params[name];
  if (entry && typeof entry === "object") {
    const reason = el("div", { class: "param-reason" });
    reason.appendChild(el("b", {}, entry.choice === "adjusted" ? "调整：" : "保持默认："));
    reason.appendChild(document.createTextNode(humanizeText(fmt(entry.reason))));
    const cited = Array.isArray(entry.cited_factors) ? entry.cited_factors : [];
    if (cited.length) {
      const chips = el("div");
      for (const factor of cited) chips.appendChild(chip(factorLabel(factor), "chip-factor"));
      reason.appendChild(chips);
    }
    row.appendChild(reason);
  } else if (overridden) {
    row.appendChild(el("div", { class: "param-reason" }, "（无逐项理由）"));
  }
  return row;
}

function zoneParams(node) {
  if (!node || typeof node !== "object") return [];
  if (node.factor) {
    const values = Array.isArray(node.value) ? node.value : [node.value];
    return values
      .map((value) => (typeof value === "string" && value.startsWith("$") ? value.slice(1) : null))
      .filter(Boolean);
  }
  return [...(node.all || []), ...(node.any || [])].flatMap(zoneParams);
}

function paramTable(rule, names, rationale, editable, current) {
  const list = el("ul", { class: "param-list" });
  for (const name of names) list.appendChild(paramRow(rule, name, rationale, editable, current));
  return list;
}

function renderRule(candidate) {
  const node = section("candidate-rule");
  if (!node) return;
  const rule = candidate.draft_rule_detail;
  const box = el("div");
  if (!rule) {
    const messages = {
      none: "尚无规则草稿（待起草）。",
      pending: "规则草稿起草中，完成后刷新本页即可审阅。",
      failed: "起草失败：草稿不可用。",
    };
    box.appendChild(
      el(
        "p",
        { class: `notice ${candidate.draft_state === "failed" ? "notice-error" : ""}`.trim() },
        messages[candidate.draft_state] || `草稿状态：${label(DRAFT_STATE_LABELS, candidate.draft_state)}`
      )
    );
    const template = primaryTemplate(candidate);
    if (candidate.status === "open" && ["none", "failed"].includes(candidate.draft_state) && template) {
      box.appendChild(redraftButton(candidate, template));
    }
    sectionReady(node, box);
    return;
  }

  const editable = candidate.status === "open" && rule.status === "draft";
  const current = currentFactorValues(candidate);
  const byAi = String(rule.drafted_by || "").startsWith("llm:");
  const heading = el("div", { class: "rule-head" });
  if (byAi) heading.appendChild(aiStamp(`AI 草稿 · ${rule.drafted_by.slice(4)} · ${formatDateTime(rule.created_at)}`));
  else heading.appendChild(chip(`起草：${fmt(rule.drafted_by)} · ${formatDateTime(rule.created_at)}`));
  heading.appendChild(chip(`规则 #${rule.rule_instance_id} v${rule.version}`));
  heading.appendChild(chip(label(RULE_STATUS_LABELS, rule.status), `rule-${rule.status}`));
  heading.appendChild(templateChip(rule.template_ref));
  heading.appendChild(calibrationBadge(rule.calibration_status));
  if (candidate.status === "open" && rule.status === "draft") {
    const redraft = redraftButton(candidate, rule.template_ref);
    redraft.classList.add("redraft");
    heading.appendChild(redraft);
  }
  box.appendChild(heading);
  box.appendChild(
    el("p", { class: "faint" }, `范围：${rule.scope === "candidate" ? "候选" : rule.scope === "holding" ? "持仓" : fmt(rule.scope)} · 区间条件按生效参数显示；灰字为本股最新筛选值。`)
  );

  const zones = rule.zones && typeof rule.zones === "object" ? rule.zones : {};
  const names = Object.keys(zones).sort(
    (a, b) => (ZONE_ORDER.indexOf(a) + 1 || 99) - (ZONE_ORDER.indexOf(b) + 1 || 99)
  );
  if (names.length) {
    const summary = el("div", { class: "zone-summary" });
    for (const zone of names) {
      const block = el("div", { class: `zone-block zone-${zone}` });
      block.appendChild(el("h4", { title: zone }, label(ZONE_LABELS, zone)));
      block.appendChild(conditionTree(zones[zone], rule.effective_params || {}, rule.factor_units || {}, current));
      summary.appendChild(block);
    }
    box.appendChild(summary);
  }

  const rationale = parseRationale(rule.draft_rationale);
  const params = paramNames(rule);
  // 先列本规则区间实际引用、被覆盖或有起草理由的参数；其余模板参数（筛选 / 持仓区间用）收起。
  const used = new Set(names.flatMap((zone) => zoneParams(zones[zone])));
  const primary = params.filter(
    (name) =>
      used.has(name) ||
      Object.prototype.hasOwnProperty.call(rule.params || {}, name) ||
      Object.prototype.hasOwnProperty.call(rationale.params, name)
  );
  // 被覆盖的参数排在最前。
  const changedFirst = (a, b) => {
    const changed = (name) =>
      Object.prototype.hasOwnProperty.call(rule.params || {}, name) &&
      rule.params[name] !== (rule.template_params || {})[name];
    return Number(changed(b)) - Number(changed(a));
  };
  primary.sort(changedFirst);
  const secondary = params.filter((name) => !primary.includes(name));
  if (primary.length) {
    box.appendChild(el("h3", {}, "参数"));
    box.appendChild(
      legend([
        ["lg-def", "模板默认"],
        ["lg-cur", "当前值"],
        ["lg-ai", "AI 建议值"],
        ["lg-stock", "本股最新值"],
      ])
    );
    box.appendChild(paramTable(rule, primary, rationale, editable, current));
  }
  if (secondary.length) {
    const more = el("details", { class: "more-params" });
    more.appendChild(el("summary", {}, `其他模板参数（${secondary.length}，本规则的候选区间未引用）`));
    more.appendChild(paramTable(rule, secondary, rationale, editable, current));
    box.appendChild(more);
  }
  if (editable && params.length) {
    const row = el("div", { class: "btn-row" });
    const save = el("button", { type: "button", class: "secondary", "data-action": "save-params" }, "保存参数修改");
    save.addEventListener("click", () => saveParams(candidate, rule, save));
    row.appendChild(save);
    box.appendChild(row);
  }
  box.appendChild(el("div", { id: "rule-issues", class: "issues", role: "alert" }));

  if (rationale.overall || rationale.text || rationale.watch.length) {
    const notes = el("div", { class: "draft-notes" });
    if (rationale.overall || rationale.text) {
      notes.appendChild(el("h3", {}, "整体起草思路"));
      notes.appendChild(el("p", {}, humanizeText(rationale.overall || rationale.text)));
    }
    if (rationale.watch.length) {
      notes.appendChild(el("h3", {}, "后续复核点"));
      const list = el("ul");
      for (const point of rationale.watch) list.appendChild(el("li", {}, humanizeText(point)));
      notes.appendChild(list);
    }
    box.appendChild(notes);
  }
  sectionReady(node, box);
}

function showIssues(target, error, prefix) {
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

function setBusy(busy) {
  detailState.busy = busy;
  for (const button of document.querySelectorAll("#candidate-detail button[data-action]")) {
    button.disabled = busy || button.dataset.unavailable === "true";
    button.setAttribute("aria-busy", busy ? "true" : "false");
  }
}

function redraftButton(candidate, templateRef) {
  const button = el(
    "button",
    { type: "button", class: "secondary", "data-action": "redraft" },
    "重新起草"
  );
  button.addEventListener("click", () => {
    const template = String(templateRef || "").split("@")[0];
    if (!globalThis.confirm(`按模板「${label(TEMPLATE_LABELS, template)}」重新起草 ${candidate.ticker} 的规则？现有草稿会被新草稿替换。`)) return;
    runAction(button, async () => {
      try {
        await post(`/candidates/${candidate.id}/redraft`, { template_id: template || null }, { idempotency: false });
        flash("已提交重新起草，完成后刷新本页即可审阅。");
        await loadCandidateDetail();
      } catch (error) {
        if (redirectOn401(error)) return;
        flash(error instanceof Error ? `重新起草失败：${error.message}` : "重新起草失败", "notice-error");
      }
    });
  });
  return button;
}

async function runAction(button, work) {
  if (detailState.busy) return;
  const original = button.textContent;
  setBusy(true);
  button.textContent = "处理中…";
  try {
    await work();
  } finally {
    button.textContent = original;
    setBusy(false);
  }
}

function flash(content, level = "") {
  const node = section("candidate-flash");
  if (!node) return;
  clear(node);
  if (!content) return;
  if (typeof content === "string") {
    node.appendChild(el("p", { class: `notice ${level}`.trim() }, content));
  } else {
    node.appendChild(content);
  }
  node.scrollIntoView({ block: "nearest" });
}

async function saveParams(candidate, rule, button) {
  const issues = section("rule-issues");
  if (invalidParams().length) {
    if (issues) {
      clear(issues);
      issues.appendChild(el("p", { class: "notice notice-error" }, "有参数超出允许范围，请先修正。"));
    }
    return;
  }
  const changes = editedParams(rule);
  if (!Object.keys(changes).length) {
    if (issues) {
      clear(issues);
      issues.appendChild(el("p", { class: "notice" }, "参数没有改动。"));
    }
    return;
  }
  await runAction(button, async () => {
    try {
      const reasonInput = section("candidate-reason");
      const reason = reasonInput ? reasonInput.value.trim() : "";
      const result = await patch(`/rules/${rule.rule_instance_id}`, reason ? { params: changes, reason } : { params: changes });
      flash(`参数已保存：规则草稿 #${result.rule.rule_instance_id} v${result.rule.version}。`);
      await loadCandidateDetail();
    } catch (error) {
      if (redirectOn401(error)) return;
      showIssues(issues, error, "保存失败");
    }
  });
}

function promoteSummary(result) {
  const box = el("div", { class: "notice" });
  box.appendChild(
    el(
      "p",
      {},
      `已晋级到观察池：规则 #${result.rule.rule_instance_id} v${result.rule.version} 生效；` +
        `自选股 ${result.watchlist.ticker}（行业 ${fmt(result.watchlist.industry)}，报告年度 ${fmt(result.watchlist.report_year)}）。`
    )
  );
  box.appendChild(
    el(
      "p",
      { class: "small" },
      `iFinD 晋级核对：${result.verification_requested ? "已排队" : "未排队"}；单股研究：${
        result.research_run_id ? `运行 #${result.research_run_id} 已排队` : "未排队"
      }。`
    )
  );
  const warnings = Array.isArray(result.warnings) ? result.warnings : [];
  if (warnings.length) {
    box.appendChild(el("p", { class: "small" }, "提示："));
    const list = el("ul", { class: "issue-list" });
    for (const warning of warnings) list.appendChild(el("li", {}, warning));
    box.appendChild(list);
    box.classList.add("notice-warn");
  }
  return box;
}

function reasonValue() {
  const input = section("candidate-reason");
  return input ? input.value.trim() : "";
}

function requireReason(target, actionName) {
  const reason = reasonValue();
  if (!reason) {
    clear(target);
    target.appendChild(el("p", { class: "notice notice-error" }, `${actionName}需要填写理由。`));
    const input = section("candidate-reason");
    if (input) input.focus();
    return null;
  }
  return reason;
}

function renderActions(candidate) {
  const node = section("candidate-actions");
  if (!node) return;
  if (candidate.status !== "open") {
    renderRegion(node, "empty", `候选${label(CANDIDATE_STATUS_LABELS, candidate.status)}，无需处理。`);
    return;
  }
  const rule = candidate.draft_rule_detail;
  const draftReady = Boolean(rule && rule.status === "draft");
  const box = el("div");
  const field = el("div", { class: "field" });
  field.appendChild(el("label", { for: "candidate-reason" }, "理由（忽略候选、驳回草稿必填；晋级可选）"));
  field.appendChild(el("textarea", { id: "candidate-reason", rows: "3", maxlength: "2000" }));
  box.appendChild(field);
  const feedback = el("div", { id: "candidate-action-issues", class: "issues", role: "alert" });

  const buttons = el("div", { class: "toolbar" });
  const promote = el(
    "button",
    { type: "button", class: "primary", "data-action": "promote", "data-unavailable": draftReady ? "false" : "true" },
    "批准并晋级"
  );
  promote.disabled = !draftReady;
  promote.addEventListener("click", () => {
    if (invalidParams().length) {
      clear(feedback);
      feedback.appendChild(el("p", { class: "notice notice-error" }, "有参数超出允许范围，请先修正。"));
      return;
    }
    const changes = editedParams(rule);
    const changed = Object.keys(changes);
    const message =
      `批准规则草稿 #${rule.rule_instance_id} 并将 ${candidate.ticker} 晋级到观察池？` +
      (changed.length ? `\n将同时采用修改后的参数：${changed.map((name) => `${name}=${changes[name]}`).join("，")}` : "");
    if (!globalThis.confirm(message)) return;
    runAction(promote, async () => {
      try {
        const body = { rule_instance_id: rule.rule_instance_id };
        if (changed.length) body.params_override = changes;
        const reason = reasonValue();
        if (reason) body.reason = reason;
        const result = await post(`/candidates/${candidate.id}/promote`, body, { idempotency: false });
        flash(promoteSummary(result));
        await loadCandidateDetail();
      } catch (error) {
        if (redirectOn401(error)) return;
        showIssues(feedback, error, "晋级失败");
      }
    });
  });

  const reject = el(
    "button",
    { type: "button", class: "danger", "data-action": "reject", "data-unavailable": draftReady ? "false" : "true" },
    "驳回草稿"
  );
  reject.disabled = !draftReady;
  reject.addEventListener("click", () => {
    const reason = requireReason(feedback, "驳回草稿");
    if (!reason) return;
    if (!globalThis.confirm(`驳回规则草稿 #${rule.rule_instance_id}？理由将作为后续起草的偏好输入。`)) return;
    runAction(reject, async () => {
      try {
        await post(`/rules/${rule.rule_instance_id}/reject`, { reason }, { idempotency: false });
        flash(`已驳回规则草稿 #${rule.rule_instance_id}。`);
        await loadCandidateDetail();
      } catch (error) {
        if (redirectOn401(error)) return;
        showIssues(feedback, error, "驳回失败");
      }
    });
  });

  const dismiss = el("button", { type: "button", class: "danger", "data-action": "dismiss" }, "忽略候选");
  dismiss.addEventListener("click", () => {
    const reason = requireReason(feedback, "忽略候选");
    if (!reason) return;
    if (!globalThis.confirm(`忽略候选 ${candidate.ticker}？30 天内筛选不再自动提名该标的。`)) return;
    runAction(dismiss, async () => {
      try {
        await post(`/candidates/${candidate.id}/dismiss`, { reason }, { idempotency: false });
        flash(`已忽略候选 ${candidate.ticker}。`);
        await loadCandidateDetail();
      } catch (error) {
        if (redirectOn401(error)) return;
        showIssues(feedback, error, "忽略失败");
      }
    });
  });

  buttons.appendChild(promote);
  buttons.appendChild(reject);
  buttons.appendChild(dismiss);
  box.appendChild(buttons);
  if (!draftReady) {
    box.appendChild(el("p", { class: "muted small" }, "没有可审阅的规则草稿：晋级需要先批准一份草稿。"));
  }
  box.appendChild(feedback);
  sectionReady(node, box);
}

function renderDecisions(candidate) {
  const node = section("candidate-decisions");
  if (!node) return;
  const decisions = Array.isArray(candidate.decisions) ? candidate.decisions : [];
  if (!decisions.length) {
    renderRegion(node, "empty", "暂无决策记录。");
    return;
  }
  const list = el("ol", { class: "timeline" });
  for (const decision of [...decisions].reverse()) {
    const item = el("li");
    item.appendChild(el("time", { datetime: decision.at }, formatDateTime(decision.at)));
    const line = el("p");
    line.appendChild(el("strong", {}, label(DECISION_KIND_LABELS, decision.kind)));
    line.appendChild(el("span", { class: "muted" }, ` · ${decision.actor === "user" ? "用户" : decision.actor === "system" ? "系统" : fmt(decision.actor)}`));
    if (decision.rule_ref) line.appendChild(el("span", { class: "muted" }, ` · ${decision.rule_ref}`));
    item.appendChild(line);
    if (decision.reason_text) item.appendChild(el("p", { class: "small" }, `理由：${humanizeText(decision.reason_text).replace(/^screen hit:\s*/, "筛选命中：")}`));
    list.appendChild(item);
  }
  sectionReady(node, list);
}

function verificationItem(request) {
  const item = el("li", { class: "verification-item" });
  const line = el("p");
  line.appendChild(el("strong", {}, label(VERIFICATION_TRIGGER_LABELS, request.trigger)));
  line.appendChild(
    chip(label(VERIFICATION_STATUS_LABELS, request.status), UNFINISHED_VERIFICATION.has(request.status) ? "chip-pending" : `verify-${request.status}`)
  );
  item.appendChild(line);
  const meta = [`请求 ${formatDateTime(request.requested_at)}`];
  if (request.report_period) meta.push(`报告期 ${request.report_period}`);
  meta.push(`字段 ${Array.isArray(request.fields) ? request.fields.join(", ") : "全部关键字段"}`);
  if (request.finished_at) meta.push(`完成 ${formatDateTime(request.finished_at)}`);
  item.appendChild(el("p", { class: "muted small" }, meta.join(" · ")));
  const summary = request.result_summary;
  if (request.status === "deferred") {
    item.appendChild(
      el(
        "p",
        { class: "small" },
        `延后至 ${formatDateTime(request.not_before)}${summary && summary.deferred ? `（${summary.deferred}）` : ""}`
      )
    );
  }
  if (summary && request.status === "done") {
    item.appendChild(el("p", { class: "small" }, `已核对一致：${summary.verified.length ? summary.verified.join(", ") : "无"}`));
    if (summary.conflicts.length) {
      const list = el("ul", { class: "issue-list" });
      for (const name of summary.conflicts) {
        const field = summary.conflict_fields[name] || {};
        list.appendChild(
          el(
            "li",
            {},
            `冲突 ${name}：iFinD ${formatFactorValue(field.ifind, "")} / 免费源 ${formatFactorValue(field.free, "")}${field.free_provider ? `（${field.free_provider}）` : ""}`
          )
        );
      }
      item.appendChild(list);
    }
    if (summary.unavailable.length) {
      item.appendChild(el("p", { class: "small muted" }, `iFinD 无数据：${summary.unavailable.join(", ")}`));
    }
  }
  if (summary && summary.error) item.appendChild(el("p", { class: "small notice-error" }, `错误：${summary.error}`));
  return item;
}

async function loadVerification(candidate) {
  const node = section("candidate-verification");
  if (!node) return;
  renderRegion(node, "loading");
  try {
    const page = await get(`/verification/requests?listing_id=${encodeURIComponent(candidate.listing_id)}&limit=20`);
    const items = Array.isArray(page && page.items) ? page.items : [];
    if (!items.length) {
      renderRegion(node, "empty", "暂无 iFinD 核对请求（晋级时会自动排队一次晋级核对）。");
      return;
    }
    const box = el("div");
    if (items.some((item) => UNFINISHED_VERIFICATION.has(item.status))) {
      box.appendChild(el("p", { class: "notice notice-warn" }, "核对中：iFinD 核对尚未完成，不阻塞晋级。"));
    }
    const list = el("ul", { class: "verification-list" });
    for (const item of items) list.appendChild(verificationItem(item));
    box.appendChild(list);
    sectionReady(node, box);
  } catch (error) {
    if (redirectOn401(error)) return;
    renderError(node, error.message, () => loadVerification(candidate));
  }
}

async function loadCandidateDetail() {
  const root = section("candidate-detail");
  if (!root) return;
  const id = root.dataset.candidateId;
  try {
    const candidate = await get(`/candidates/${encodeURIComponent(id)}`);
    detailState.candidate = candidate;
    renderHeader(candidate);
    renderReasons(candidate);
    renderRule(candidate);
    renderActions(candidate);
    renderDecisions(candidate);
    await loadVerification(candidate);
  } catch (error) {
    if (redirectOn401(error)) return;
    const header = section("candidate-header");
    if (header) {
      renderError(
        header,
        error instanceof ApiError && error.status === 404 ? "候选不存在。" : error.message,
        error instanceof ApiError && error.status === 404 ? null : () => loadCandidateDetail()
      );
    }
    for (const id of ["candidate-reasons", "candidate-rule", "candidate-actions", "candidate-verification", "candidate-decisions"]) {
      const node = section(id);
      if (node) renderRegion(node, "empty", "—");
    }
  }
}

// ---- 启动 -------------------------------------------------------------

export function bootCandidates() {
  const list = section("candidates-list");
  if (list) {
    loadCandidateList(list);
    const statusFilter = section("candidates-status-filter");
    if (statusFilter) statusFilter.addEventListener("change", () => loadCandidateList(list));
    const templateFilter = section("candidates-template-filter");
    if (templateFilter) templateFilter.addEventListener("change", () => renderCandidateList(list));
  }
  const nominateForm = section("candidate-nominate-form");
  if (nominateForm) wireNominateForm(nominateForm, list);
  if (section("candidate-detail")) loadCandidateDetail();
}
