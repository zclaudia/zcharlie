// 候选详情：概要、理由、处理、决策、主营和核对。规则表在 candidate_rule.js。

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

import {
  DISMISS_NOTE,
  UNFINISHED_VERIFICATION,
  badge,
  calibrationBadge,
  chip,
  draftBadge,
  industryText,
  isNumber,
  pctChange,
  pctNode,
  reasonLine,
  redirectOn401,
  renderError,
  section,
  sectionReady,
  showIssues,
  templateBadge,
  truncate,
  whyModel,
  zoneDetectionLine,
} from "./candidate_shared.js";
import { editedParams, invalidParams, renderRule } from "./candidate_rule.js";

// ---- 候选详情 ---------------------------------------------------------

const detailState = { candidate: null, busy: false };

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

// 外壳按路由只给了「候选 #编号」：拿到数据后把移动端顶栏与桌面面包屑末项改成股票名。
function setShellTitle(text) {
  for (const node of document.querySelectorAll("[data-appbar-title], .crumbs > b")) node.textContent = text;
  document.title = `${text} · 候选 · zcharlie`;
}

// 身份区的最新收盘与日涨跌：只取最近几根日线，失败时留空（价格图里仍有完整行情）。
async function fillHeaderPrice(ticker, host) {
  try {
    const data = await get(`/market/${encodeURIComponent(ticker)}/bars?days=5`);
    const closes = (data && Array.isArray(data.bars) ? data.bars : []).filter((bar) => bar && Number.isFinite(bar.close));
    if (!closes.length || !host.isConnected) return;
    const last = closes[closes.length - 1];
    const prev = closes.length > 1 ? closes[closes.length - 2].close : null;
    host.replaceChildren(
      el("b", { class: "price-last" }, last.close.toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })),
      pctNode(prev ? pctChange(prev, last.close) : null),
      el("small", { class: "faint" }, `${String(last.trade_date).slice(5)} 收盘`)
    );
  } catch {
    // 行情缺失不影响审阅。
  }
}

function renderHeader(candidate) {
  setShellTitle(candidate.name || `候选 #${candidate.id}`);
  const title = section("candidate-title");
  if (title) {
    clear(title);
    title.appendChild(document.createTextNode(`${candidate.name || "候选"} `));
    title.appendChild(el("span", { class: "code" }, candidate.ticker));
  }
  const node = section("candidate-header");
  if (!node) return;
  const box = el("div", { class: "cand-idhead" });
  const left = el("div", { class: "cand-id-left" });
  const price = el("div", { class: "cand-id-price" });
  left.appendChild(price);
  const line = el("div", { class: "cluster cand-id-badges" });
  line.appendChild(badge(label(CANDIDATE_STATUS_LABELS, candidate.status), `state-${candidate.status}`));
  if (candidate.status === "open") line.appendChild(draftBadge(candidate));
  for (const ref of candidate.template_refs || []) line.appendChild(templateBadge(ref));
  if ((candidate.calibration_status || "uncalibrated") === "uncalibrated") {
    line.appendChild(el("span", { class: "badge badge-uncalibrated", title: "模板默认参数尚未经回测校准，只是假设" }, "未校准"));
  }
  left.appendChild(line);
  box.appendChild(left);
  if (candidate.status === "open") {
    box.appendChild(timeline(candidate.opened_at, candidate.expires_at));
  } else {
    box.appendChild(el("p", { class: "faint small cand-id-closed" }, `开启 ${formatDate(candidate.opened_at)} · 到期 ${formatDate(candidate.expires_at)}`));
  }
  sectionReady(node, box);
  fillHeaderPrice(candidate.ticker, price);
}

// ---- 候选详情：分区（概要 / 入选理由 / 规则草稿 / 主营 / 核对 / 记录）------------------

const TAB_NAMES = ["summary", "reasons", "rule", "segments", "verification", "decisions"];

function selectTab(name, { focus = false } = {}) {
  if (!TAB_NAMES.includes(name)) return;
  for (const tab of document.querySelectorAll("#candidate-tabs [data-tab]")) {
    const on = tab.dataset.tab === name;
    tab.classList.toggle("on", on);
    tab.setAttribute("aria-selected", on ? "true" : "false");
    tab.tabIndex = on ? 0 : -1;
    if (on) {
      if (focus) tab.focus();
      tab.scrollIntoView({ block: "nearest", inline: "nearest" });
    }
  }
  for (const panel of document.querySelectorAll("[data-panel]")) panel.hidden = panel.dataset.panel !== name;
}

export function wireTabs() {
  const tabs = [...document.querySelectorAll("#candidate-tabs [data-tab]")];
  if (!tabs.length) return;
  for (const tab of tabs) {
    tab.addEventListener("click", () => selectTab(tab.dataset.tab));
    tab.addEventListener("keydown", (event) => {
      const index = tabs.indexOf(tab);
      const step = event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0;
      if (!step) return;
      event.preventDefault();
      selectTab(tabs[(index + step + tabs.length) % tabs.length].dataset.tab, { focus: true });
    });
  }
  const hash = globalThis.location.hash.replace(/^#/, "");
  selectTab(TAB_NAMES.includes(hash) ? hash : "summary");
}

function summaryCard(titleText, aside) {
  const card = el("section", { class: "card cand-sum-card" });
  const head = el("div", { class: "card-head" });
  head.appendChild(el("h2", {}, titleText));
  if (aside) head.appendChild(aside);
  card.appendChild(head);
  return card;
}

function tabLink(text, tab) {
  const button = el("button", { type: "button", class: "link-button card-more" }, text);
  button.addEventListener("click", () => selectTab(tab));
  return button;
}

function renderSummary(candidate) {
  const node = section("candidate-summary");
  if (!node) return;
  clear(node);
  node.dataset.state = "ready";

  const model = whyModel(candidate);
  const reasons = summaryCard("入选理由", tabLink("全部 →", "reasons"));
  if (model.kind === "screen" || model.kind === "draft") {
    reasons.querySelector(".card-head").insertBefore(
      badge(`${model.verb} ${model.met}/${model.total}`, "accent"),
      reasons.querySelector(".card-more")
    );
    const list = el("ul", { class: "reason-list" });
    for (const { reason, ok, prefix } of model.items.slice(0, 3)) {
      list.appendChild(el("li", { class: ok ? "reason-ok" : "reason-warn" })).appendChild(reasonLine(reason, prefix));
    }
    reasons.appendChild(list);
  } else if (model.kind === "text") {
    reasons.appendChild(el("p", { class: "small" }, truncate(model.sentences[0] || "", 120)));
  } else {
    reasons.appendChild(el("p", { class: "muted small" }, "手动提名：没有筛选理由。"));
  }
  node.appendChild(reasons);

  const side = el("div", { class: "cand-sum-side" });
  const rule = candidate.draft_rule_detail;
  const draft = summaryCard("规则草稿", tabLink("去审阅 →", "rule"));
  const draftLine = el("div", { class: "cluster" });
  if (rule) {
    if (String(rule.drafted_by || "").startsWith("llm:")) draftLine.appendChild(aiStamp(`AI 草稿 · ${rule.drafted_by.slice(4)}`));
    draftLine.appendChild(badge(`规则 #${rule.rule_instance_id} v${rule.version}`));
    draftLine.appendChild(badge(label(RULE_STATUS_LABELS, rule.status), rule.status === "draft" ? "ai" : "accent"));
  } else {
    draftLine.appendChild(draftBadge(candidate));
  }
  draft.appendChild(draftLine);
  const detection = zoneDetectionLine(candidate.zone_events);
  if (detection) draft.appendChild(detection);
  side.appendChild(draft);

  const info = summaryCard("基本信息");
  const screen = candidate.latest_screen;
  const sources = (candidate.sources || []).map((source) =>
    source.kind === "screen"
      ? `筛选 #${fmt(source.screen_run_id)}（${label(TEMPLATE_LABELS, source.template_id)}，数据日 ${fmt(source.as_of)}）`
      : `手动提名${source.template_id ? `（${label(TEMPLATE_LABELS, source.template_id)}）` : ""}`
  );
  const research = el("span");
  const link = stockLink(candidate.ticker);
  if (link) research.appendChild(link);
  info.appendChild(
    kvRows([
      ["行业组", screen && screen.context ? industryText(screen.context) : null],
      ["来源", sources.length ? sources.join("；") : null],
      ["开启 / 到期", `${formatDate(candidate.opened_at)} → ${formatDate(candidate.expires_at)}`],
      ["关闭", candidate.closed_at ? formatDate(candidate.closed_at) : null],
      ["备注", candidate.note || null],
    ].filter(([, value]) => value !== null))
  );
  const dl = info.querySelector("dl");
  if (dl) {
    dl.appendChild(el("dt", {}, "单股研究"));
    const dd = el("dd");
    dd.appendChild(research);
    dl.appendChild(dd);
  }
  side.appendChild(info);
  node.appendChild(side);

  const count = document.querySelector("#cand-tab-reasons-count");
  if (count) {
    const total = (candidate.screen_results || []).reduce((sum, result) => sum + (result.reasons || []).length, 0) || (model.total || 0);
    count.hidden = !total;
    count.textContent = String(total);
  }
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
    const checks = Array.isArray(candidate.draft_checks) ? candidate.draft_checks : [];
    if (checks.length) {
      // 手动提名：展示草稿规则 × 本股因子的检查项（与筛选命中同构）。
      const box = el("div", { class: "reason-block" });
      box.appendChild(el("h3", {}, "AI 起草分析"));
      const list = el("ul", { class: "reason-list" });
      for (const reason of checks) list.appendChild(el("li", {}, formatReason(reason)));
      box.appendChild(list);
      sectionReady(node, box);
    } else {
      renderRegion(
        node,
        "empty",
        candidate.draft_overall_rationale
          ? "手动提名：没有筛选命中；AI 起草分析见下方规则草稿。"
          : "手动提名：没有筛选理由。"
      );
    }
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
  if (detailState.busy) return false;
  const original = button.textContent;
  setBusy(true);
  button.textContent = "处理中…";
  try {
    return await work();
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
      const result = await patch(`/rules/${rule.rule_instance_id}`, { params: changes });
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
        `观察池 ${result.watchlist.ticker}（行业 ${fmt(result.watchlist.industry)}，报告年度 ${fmt(result.watchlist.report_year)}）。`
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

// 处理理由弹层（底部弹层 / 桌面居中）：驳回、忽略必填，批准并晋级可选；失败时留在弹层里显示。
const reasonSheet = { pending: null };

function openReasonSheet({ title, sub, field, required, confirmText, placeholder, run }) {
  const sheet = section("candidate-reason-sheet");
  const input = section("candidate-reason");
  if (!sheet || !input) return;
  section("candidate-reason-title").textContent = title;
  section("candidate-reason-sub").textContent = sub;
  section("candidate-reason-label").textContent = `${field}${required ? "（必填）" : "（可选）"}`;
  section("candidate-reason-confirm").textContent = confirmText;
  input.value = "";
  input.setAttribute("placeholder", placeholder);
  clear(section("candidate-sheet-issues"));
  reasonSheet.pending = { title, required, run };
  if (globalThis.zcOverlay) globalThis.zcOverlay.open(sheet);
}

export function wireReasonSheet() {
  const confirmButton = section("candidate-reason-confirm");
  if (!confirmButton) return;
  confirmButton.addEventListener("click", async () => {
    const pending = reasonSheet.pending;
    if (!pending) return;
    const input = section("candidate-reason");
    const issues = section("candidate-sheet-issues");
    const reason = input.value.trim();
    clear(issues);
    if (pending.required && !reason) {
      issues.appendChild(el("p", { class: "notice notice-error" }, `${pending.title}需要填写理由。`));
      input.focus();
      return;
    }
    const done = await pending.run(reason, issues, confirmButton);
    if (done) {
      reasonSheet.pending = null;
      input.value = "";
      if (globalThis.zcOverlay) globalThis.zcOverlay.close();
    }
  });
}

function renderActions(candidate) {
  const node = section("candidate-actions");
  if (!node) return;
  // 待处理时整张卡变成固定在视口底部的操作条；已关闭则回到文档流。
  const open = candidate.status === "open";
  const card = section("candidate-actions-card");
  if (card) {
    card.classList.toggle("action-bar", open);
    card.classList.toggle("card", !open);
  }
  if (!open) {
    renderRegion(node, "empty", `候选${label(CANDIDATE_STATUS_LABELS, candidate.status)}，无需处理。`);
    return;
  }
  const rule = candidate.draft_rule_detail;
  const draftReady = Boolean(rule && rule.status === "draft");
  const box = el("div");
  const row = el("div", { class: "cand-action-row" });
  row.appendChild(
    el(
      "p",
      { class: "cand-action-info" },
      draftReady
        ? `规则草稿 #${rule.rule_instance_id} v${rule.version} 待审阅`
        : "没有可审阅的规则草稿：晋级需要先批准一份草稿。"
    )
  );
  const feedback = el("div", { id: "candidate-action-issues", class: "issues", role: "alert" });
  const buttons = el("div", { class: "cand-action-btns" });

  const dismiss = el("button", { type: "button", class: "btn btn-danger-ghost btn-lg", "data-action": "dismiss" }, "忽略候选");
  dismiss.addEventListener("click", () => {
    clear(feedback);
    openReasonSheet({
      title: "忽略候选",
      sub: `忽略候选 ${candidate.ticker}？${DISMISS_NOTE}`,
      field: "理由",
      required: true,
      confirmText: "确认忽略",
      placeholder: "例如：行业景气度已转弱，本轮不再研究",
      run: (reason, issues, confirmButton) =>
        runAction(confirmButton, async () => {
          try {
            await post(`/candidates/${candidate.id}/dismiss`, { reason }, { idempotency: false });
            flash(`已忽略候选 ${candidate.ticker}。`);
            await loadCandidateDetail();
            return true;
          } catch (error) {
            if (redirectOn401(error)) return false;
            showIssues(issues, error, "忽略失败");
            return false;
          }
        }),
    });
  });

  const reject = el(
    "button",
    { type: "button", class: "btn btn-danger-ghost btn-lg", "data-action": "reject", "data-unavailable": draftReady ? "false" : "true" },
    "驳回草稿"
  );
  reject.disabled = !draftReady;
  reject.addEventListener("click", () => {
    clear(feedback);
    openReasonSheet({
      title: "驳回草稿",
      sub: `驳回规则草稿 #${rule.rule_instance_id}？候选保留在池中，理由将作为后续起草的偏好输入。`,
      field: "理由",
      required: true,
      confirmText: "确认驳回",
      placeholder: "例如：参数区间偏宽，复核点没有覆盖分红政策变化",
      run: (reason, issues, confirmButton) =>
        runAction(confirmButton, async () => {
          try {
            await post(`/rules/${rule.rule_instance_id}/reject`, { reason }, { idempotency: false });
            flash(`已驳回规则草稿 #${rule.rule_instance_id}。`);
            await loadCandidateDetail();
            return true;
          } catch (error) {
            if (redirectOn401(error)) return false;
            showIssues(issues, error, "驳回失败");
            return false;
          }
        }),
    });
  });

  const promote = el(
    "button",
    { type: "button", class: "btn btn-primary btn-lg", "data-action": "promote", "data-unavailable": draftReady ? "false" : "true" },
    "批准并晋级"
  );
  promote.disabled = !draftReady;
  promote.addEventListener("click", () => {
    clear(feedback);
    if (invalidParams().length) {
      feedback.appendChild(el("p", { class: "notice notice-error" }, "有参数超出允许范围，请先修正。"));
      return;
    }
    const changes = editedParams(rule);
    const changed = Object.keys(changes);
    const message =
      `批准规则草稿 #${rule.rule_instance_id} 并将 ${candidate.ticker} 晋级到观察池？` +
      (changed.length ? `\n将同时采用修改后的参数：${changed.map((name) => `${name}=${changes[name]}`).join("，")}` : "");
    openReasonSheet({
      title: "批准并晋级",
      sub: message,
      field: "理由",
      required: false,
      confirmText: "确认晋级",
      placeholder: "可选：为什么批准这份草稿",
      run: (reason, issues, confirmButton) =>
        runAction(confirmButton, async () => {
          try {
            const body = { rule_instance_id: rule.rule_instance_id };
            if (changed.length) body.params_override = changes;
            if (reason) body.reason = reason;
            const result = await post(`/candidates/${candidate.id}/promote`, body, { idempotency: false });
            flash(promoteSummary(result));
            await loadCandidateDetail();
            return true;
          } catch (error) {
            if (redirectOn401(error)) return false;
            showIssues(issues, error, "晋级失败");
            return false;
          }
        }),
    });
  });

  buttons.appendChild(dismiss);
  buttons.appendChild(reject);
  buttons.appendChild(promote);
  row.appendChild(buttons);
  box.appendChild(row);
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

// ---- 主营构成（事实背景，不参与筛选与规则）---------------------------------

const SEGMENT_KIND_LABELS = Object.freeze({ product: "按产品", industry: "按行业", region: "按地区" });

function cny(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  if (Math.abs(value) >= 1e8) return `${(value / 1e8).toLocaleString("zh-CN", { maximumFractionDigits: 2 })} 亿元`;
  return `${(value / 1e4).toLocaleString("zh-CN", { maximumFractionDigits: 0 })} 万元`;
}

function ratio(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  return `${(value * 100).toFixed(1)}%`;
}

export function segmentRows(segments) {
  return segments.map((item) => {
    const row = el("tr");
    row.appendChild(el("td", {}, item.name || "—"));
    row.appendChild(el("td", { class: "num" }, cny(item.revenue)));
    row.appendChild(el("td", { class: "num" }, ratio(item.share)));
    row.appendChild(el("td", { class: "num" }, ratio(item.margin)));
    return row;
  });
}

function renderSegments(candidate) {
  const node = section("candidate-segments");
  if (!node) return;
  const segments = Array.isArray(candidate.segments) ? candidate.segments : [];
  if (!segments.length) {
    renderRegion(node, "empty", "暂无主营构成数据（披露季内每周采集）。");
    return;
  }
  const box = el("div");
  const head = segments[0];
  box.appendChild(
    el("p", { class: "meta-row" }, `报告期 ${fmt(head.report_period)} · ${label(SEGMENT_KIND_LABELS, head.kind)}`)
  );
  box.appendChild(
    table(
      [{ label: "构成" }, { label: "收入" }, { label: "收入占比" }, { label: "毛利率" }],
      segmentRows(segments)
    )
  );
  sectionReady(node, box);
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

export async function loadCandidateDetail() {
  const root = section("candidate-detail");
  if (!root) return;
  const id = root.dataset.candidateId;
  try {
    const candidate = await get(`/candidates/${encodeURIComponent(id)}`);
    detailState.candidate = candidate;
    renderHeader(candidate);
    const chartHost = section("candidate-price-chart");
    if (chartHost) {
      mountPriceChart(chartHost, candidate.ticker, (ticker, days) => get(`/market/${encodeURIComponent(ticker)}/bars?days=${days}`), {
        title: section("candidate-price-title"),
      });
    }
    renderReasons(candidate);
    renderSummary(candidate);
    renderRule(candidate, { redraftButton, saveParams });
    renderActions(candidate);
    renderSegments(candidate);
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
    for (const id of ["candidate-summary", "candidate-reasons", "candidate-rule", "candidate-actions", "candidate-segments", "candidate-verification", "candidate-decisions"]) {
      const node = section(id);
      if (node) renderRegion(node, "empty", "—");
    }
  }
}
