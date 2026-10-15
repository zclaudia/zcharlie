// 页面数据装配：按 data-region 声明加载数据并渲染。
// 页面只消费版本化 API 结构，不在前端解释 score 语义或复算分数。

import {
  ApiError,
  FETCH_MARKER_HEADERS,
  download,
  get,
  loadRegion,
  lockWhileBusy,
  post,
  renderRegion,
} from "./api.js";
import { CANDIDATE_REGIONS, bootCandidates } from "./candidates.js";
import { TEMPLATE_REGIONS, bootTemplates } from "./templates.js";
import { attachCombobox, searchStocks } from "./combobox.js";
import { HEALTH_REGIONS, bootHealth, healthBucket } from "./health.js";
import { bootAgentControls } from "./agent_controls.js";
import { bootAgentViews } from "./agent_views.js";
import { SETTINGS_REGIONS, bootSettings } from "./settings.js";
import { el, errorText, fmt, fmtNumber, kvTable, statusBadge, table } from "./dom.js";
import {
  FRESHNESS_LABELS,
  QUALITY_FLAG_LABELS,
  RUN_KIND_LABELS,
  RUN_STAGE_LABELS,
  METRIC_UNITS,
  PROVIDER_LABELS,
  SOURCE_VERIFICATION_LABELS,
  STATUS_LABELS,
  fieldLabel,
  formatDateTime,
  formatFactorValue,
  label,
  summarizeError,
} from "./format.js";
import {
  RISK_LABELS,
  VALUATION_LABELS,
  formatPathValue,
  humanizeText,
  pathLabel,
  scoreFactorLabel,
} from "./research_format.js";
import { aiStamp, ring, stockIdent, svg, thinBar } from "./ui.js";
import { bootAgent } from "./agent.js";
import { bootAgentPortfolio } from "./agent_portfolio.js";
import { bootFinancials } from "./financials.js";
import { WATCHLIST_REGIONS, bootWatchlist, loadBars } from "./watchlist.js";
import { PORTFOLIO_REGIONS, bootPortfolio } from "./portfolio.js";
import { PRICE_RANGES, mountPriceChart } from "./pricechart.js";

// ---- 共用小件 ---------------------------------------------------------

// 线性图标（路径取自 _icons.html，颜色继承文字色）。
const ICON_PATHS = Object.freeze({
  alert: ["M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z", "M12 9v4M12 17h.01"],
  file: ["M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z", "M14 3v5h5M9 13h6M9 17h4"],
  cal: ["M5 5h14a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2z", "M3 10h18M8 3v4M16 3v4"],
  chev: ["M9 6l6 6-6 6"],
  arrow: ["M5 12h14M13 6l6 6-6 6"],
  funnel: ["M3 4h18l-7 8.5V19l-4 2v-8.5z"],
  eye: ["M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z", "M9 12a3 3 0 1 0 6 0 3 3 0 1 0-6 0"],
  brief: ["M5 7h14a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V9a2 2 0 0 1 2-2z", "M8 7V5a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2M3 13h18"],
  clock: ["M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18z", "M12 7v5l3 2"],
  play: ["M8 5v14l11-7z"],
  msg: ["M21 15a2 2 0 0 1-2 2H8l-5 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"],
  more: ["M5 12h.01M12 12h.01M19 12h.01"],
  ai: ["M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9z", "M19 16v4M17 18h4"],
  info: ["M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18z", "M12 11v5M12 8h.01"],
  check: ["M5 12.5l4.5 4.5L19 7"],
  refresh: ["M21 12a9 9 0 1 1-2.6-6.4L21 8", "M21 3v5h-5"],
});

function icon(name, size = 16) {
  const root = svg("svg", {
    class: "ico",
    viewBox: "0 0 24 24",
    width: size,
    height: size,
    fill: "none",
    stroke: "currentColor",
    "stroke-width": 1.75,
    "stroke-linecap": "round",
    "stroke-linejoin": "round",
    "aria-hidden": "true",
    focusable: "false",
  });
  for (const d of ICON_PATHS[name] || []) root.appendChild(svg("path", { d }));
  return root;
}

// "2026-10-09 19:31" → "10-09 19:31"（北京时间，与 formatDateTime 一致）。
function shortStamp(value) {
  const text = formatDateTime(value);
  return text === "—" ? "—" : text.slice(5);
}

// 只放不含交易含义的研究优先级文案（与后端 SUGGESTION_LABELS 对应）。
const PRIORITY_LABELS = Object.freeze({
  high_research_priority: ["高优先", "accent"],
  research_priority: ["中优先", "accent"],
  monitor: ["持续跟踪", ""],
  defer: ["暂缓", ""],
  risk_review: ["风险复核", "warn"],
  low_priority: ["低优先", ""],
  needs_review: ["待复核", "warn"],
});

function priorityBadge(key) {
  const entry = PRIORITY_LABELS[key];
  if (!entry) return el("span", { class: "faint" }, "—");
  return el("span", { class: `badge ${entry[1]}`.trim() }, entry[0]);
}

// ---- 今日概览 ---------------------------------------------------------

const WEEKDAYS = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"];
const ALERTS_VISIBLE = 4;
const ALERTS_VISIBLE_COMPACT = 3;

function overviewSubtitle(generatedAt) {
  const stamp = formatDateTime(generatedAt);
  if (stamp === "—") return "";
  const [date, time] = stamp.split(" ");
  const day = new Date(`${date}T00:00:00`);
  const week = Number.isNaN(day.getTime()) ? "" : ` ${WEEKDAYS[day.getDay()]}`;
  return `${date}${week} · 上次刷新 ${time}`;
}

function splitMessage(message) {
  const text = String(message ?? "");
  const match = /[：:]\s*/.exec(text);
  if (!match || match.index < 4 || match.index + match[0].length >= text.length) return [text, ""];
  return [text.slice(0, match.index), text.slice(match.index + match[0].length)];
}

function entryNames(bucket) {
  const entries = Array.isArray(bucket.entries) ? bucket.entries : [];
  const names = entries.slice(0, 3).map((entry) => entry.name || entry.ticker || `#${entry.run_id}`);
  const rest = bucket.count - names.length;
  return `${names.join("、")}${rest > 0 ? ` 等 ${bucket.count} 项` : ""}`;
}

function bucketRow(bucket, { title, href, action, iconName = "alert" }) {
  if (!bucket || !(bucket.count > 0)) return null;
  const entries = Array.isArray(bucket.entries) ? bucket.entries : [];
  const single = bucket.count === 1 && entries[0] && entries[0].ticker;
  return {
    level: "warn",
    icon: iconName,
    title: title(bucket.count),
    detail: entryNames(bucket),
    href: single && action.single ? `/app/stocks/${encodeURIComponent(entries[0].ticker)}` : href,
    action: single && action.single ? action.single : action.text,
  };
}

// "需要你处理"的行：数据健康提醒 → 候选草稿 → 研究运行异常。
function overviewAlertRows(data) {
  const rows = [];
  const warnings = Array.isArray(data.warnings) ? data.warnings : [];
  const candidates = data.candidates && typeof data.candidates === "object" ? data.candidates : null;
  const warningRow = (warning) => {
    const [title, detail] = splitMessage(fmt(warning.message));
    return {
      level: warning.level === "error" ? "error" : "warn",
      icon: typeof warning.link === "string" && warning.link.includes("freshness") ? "cal" : "alert",
      title,
      detail,
      href: typeof warning.link === "string" && warning.link.startsWith("/app/") ? warning.link : null,
      action: "查看详情",
    };
  };
  for (const warning of warnings) if (warning.level === "error") rows.push(warningRow(warning));
  if (candidates && candidates.drafts_ready > 0) {
    const pending = candidates.verification_pending > 0 ? ` · iFinD 核对未完成 ${candidates.verification_pending}` : "";
    rows.push({
      level: "ai",
      icon: "file",
      title: `${candidates.drafts_ready} 份候选研究草稿待审阅`,
      detail: `待处理候选 ${candidates.open}${pending}`,
      href: "/app/candidates",
      action: "开始审阅",
      primary: true,
    });
  }
  if (candidates && candidates.draft_failed > 0) {
    rows.push({
      level: "warn",
      icon: "alert",
      title: `${candidates.draft_failed} 份候选起草失败`,
      detail: "可在候选页重新起草。",
      href: "/app/candidates",
      action: "去处理",
    });
  }
  for (const warning of warnings) if (warning.level !== "error") rows.push(warningRow(warning));
  const extra = [
    bucketRow(data.failed, { title: (n) => `${n} 个研究运行失败`, href: "/app/runs?status=failed", action: { text: "查看运行", single: "查看" } }),
    bucketRow(data.partial, { title: (n) => `${n} 个研究运行部分成功`, href: "/app/runs?status=partial", action: { text: "查看运行", single: "查看" } }),
    bucketRow(data.needs_review, { title: (n) => `${n} 只标的需人工复核`, href: "/app/watchlist", action: { text: "查看观察池", single: "去复核" }, iconName: "eye" }),
    bucketRow(data.meaningful_changes, { title: (n) => `${n} 只标的有重要变化`, href: "/app/watchlist", action: { text: "查看观察池", single: "查看" }, iconName: "refresh" }),
  ];
  for (const row of extra) if (row) rows.push(row);
  return rows;
}

function alertRowNode(row) {
  const tone = row.level === "ai" ? "ai" : row.level === "error" ? "error" : "warn";
  const li = el("li", { class: `attn-row ov-alert-${tone}` });
  const ico = el("span", { class: `list-row-icon ${tone === "ai" ? "ai" : "warn"}`, "aria-hidden": "true" });
  ico.appendChild(icon(row.icon, 16));
  li.appendChild(ico);
  const body = el("div", { class: "attn-main" });
  body.appendChild(el("div", { class: "attn-title" }, row.title));
  if (row.detail) body.appendChild(el("div", { class: "attn-detail", title: row.detail }, row.detail));
  li.appendChild(body);
  if (row.href) {
    const act = el("a", { class: `btn btn-sm attn-act ov-alert-act${row.primary ? " btn-primary" : ""}`, href: row.href });
    act.appendChild(el("span", { class: "ov-act-text" }, row.action));
    const chev = icon("chev", 16);
    chev.classList.add("ov-act-chev");
    act.appendChild(chev);
    li.appendChild(act);
  }
  return li;
}

function alertsCard(rows, data) {
  const card = el("section", { class: "card card--flush ov-alerts", "aria-label": "需要你处理" });
  const head = el("div", { class: "card-head card-head--rule" });
  head.appendChild(el("h2", {}, "需要你处理"));
  const warnings = Array.isArray(data.warnings) ? data.warnings : [];
  const errors = warnings.filter((warning) => warning.level === "error").length;
  const warns = warnings.length - errors;
  const drafts = data.candidates && data.candidates.drafts_ready > 0 ? data.candidates.drafts_ready : 0;
  const tags = el("div", { class: "cluster ov-alerts-tags" });
  if (errors) tags.appendChild(el("span", { class: "badge warn" }, `${errors} 错误`));
  if (warns) tags.appendChild(el("span", { class: "badge warn outline" }, `${warns} 警告`));
  if (drafts) tags.appendChild(el("span", { class: "badge ai" }, `${drafts} 份草稿待审`));
  head.appendChild(tags);
  head.appendChild(el("a", { class: "card-more", href: "/app/health" }, "数据健康 →"));
  card.appendChild(head);

  const visible = isCompactViewport() ? ALERTS_VISIBLE_COMPACT : ALERTS_VISIBLE;
  const list = el("ul", { class: "attn-list" });
  const nodes = rows.map((row, index) => {
    const node = alertRowNode(row);
    if (index >= visible) node.hidden = true;
    list.appendChild(node);
    return node;
  });
  if (!rows.length) list.appendChild(el("li", { class: "ov-alert-empty" }, "今日无待处理项。"));
  card.appendChild(list);
  if (nodes.length > visible) {
    const foot = el("div", { class: "card-foot" });
    const more = el("button", { type: "button", class: "link-button", "aria-expanded": "false" }, `展开其余 ${nodes.length - visible} 条`);
    more.addEventListener("click", () => {
      const open = more.getAttribute("aria-expanded") !== "true";
      nodes.forEach((node, index) => {
        if (index >= visible) node.hidden = !open;
      });
      more.setAttribute("aria-expanded", open ? "true" : "false");
      more.textContent = open ? "收起" : `展开其余 ${nodes.length - visible} 条`;
    });
    foot.appendChild(more);
    card.appendChild(foot);
  }
  return card;
}

// 手机顶部的数据状态条：一行，点进数据健康。
function statusStrip(data) {
  const warnings = Array.isArray(data.warnings) ? data.warnings : [];
  const lead = warnings.find((warning) => warning.level === "error") || warnings[0];
  if (!lead) return null;
  const health = data.data_health && typeof data.data_health === "object" ? data.data_health : null;
  const errors = health ? health.errors : warnings.filter((warning) => warning.level === "error").length;
  const warns = health ? health.warnings : warnings.length - errors;
  const href = typeof lead.link === "string" && lead.link.startsWith("/app/") ? lead.link : "/app/health";
  const node = el("a", { class: "ov-status only-mobile", href });
  node.appendChild(icon("alert", 16));
  const text = el("span", { class: "ov-status-text" });
  text.appendChild(el("b", {}, splitMessage(fmt(lead.message))[0]));
  text.appendChild(el("small", {}, `${errors} 错误 · ${warns} 警告`));
  node.appendChild(text);
  const chev = icon("chev", 16);
  chev.classList.add("ov-status-chev");
  node.appendChild(chev);
  return node;
}

function funnelStage({ href, iconName, name, big, unit, chips = [], cta, note, muted = false }) {
  const node = el("a", { class: `ov-stage${muted ? " is-muted" : ""}`, href });
  const top = el("div", { class: "ov-stage-top" });
  top.appendChild(icon(iconName, 15));
  top.appendChild(document.createTextNode(name));
  node.appendChild(top);
  const number = el("div", { class: "ov-stage-big" });
  number.appendChild(el("b", { class: "num" }, big));
  if (unit) number.appendChild(el("small", {}, unit));
  node.appendChild(number);
  const chipBox = el("div", { class: "cluster ov-stage-chips" });
  for (const [text, kind] of chips) chipBox.appendChild(el("span", { class: `badge ${kind || ""}`.trim() }, text));
  node.appendChild(chipBox);
  const cue = el("div", { class: "ov-stage-cta" });
  cue.appendChild(document.createTextNode(cta));
  cue.appendChild(icon("chev", 14));
  node.appendChild(cue);
  node.appendChild(el("em", { class: "ov-stage-note" }, note));
  return node;
}

function funnelBlock(data, watch, accounts) {
  const candidates = data.candidates && typeof data.candidates === "object" ? data.candidates : null;
  const wrap = el("section", { class: "ov-funnel-wrap", "aria-label": "研究漏斗" });
  wrap.appendChild(el("h2", { class: "ov-section-title" }, "研究漏斗"));
  const row = el("div", { class: "ov-funnel" });

  if (candidates) {
    const chips = [[`草稿待审 ${candidates.drafts_ready}`, candidates.drafts_ready ? "ai" : ""]];
    chips.push([`起草中 ${candidates.drafting}`, ""]);
    if (candidates.draft_failed) chips.push([`起草失败 ${candidates.draft_failed}`, "warn"]);
    chips.push([`核对未完成 ${candidates.verification_pending}`, candidates.verification_pending ? "warn" : ""]);
    row.appendChild(
      funnelStage({
        href: "/app/candidates",
        iconName: "funnel",
        name: "候选",
        big: String(candidates.open),
        unit: "只待处理",
        chips,
        cta: "逐个审阅，决定是否加入观察",
        note: candidates.drafts_ready ? `${candidates.drafts_ready} 草稿待审` : "无待审草稿",
      })
    );
  } else {
    row.appendChild(funnelStage({ href: "/app/candidates", iconName: "funnel", name: "候选", big: "—", cta: "查看候选池", note: "暂无数据", muted: true }));
  }
  row.appendChild(el("span", { class: "ov-arrow", "aria-hidden": "true" })).appendChild(icon("arrow", 18));

  if (Array.isArray(watch)) {
    const scored = watch.filter((entry) => entry.latest_research).length;
    const changed = watch.filter((entry) => entry.latest_research && entry.latest_research.meaningful_change_count > 0).length;
    const ruled = watch.filter((entry) => entry.rule && entry.rule.active).length;
    const chips = [[`已评分 ${scored}`, scored ? "accent" : ""], [`待研究 ${watch.length - scored}`, ""]];
    if (ruled) chips.push([`规则已生效 ${ruled}`, ""]);
    row.appendChild(
      funnelStage({
        href: "/app/watchlist",
        iconName: "eye",
        name: "观察",
        big: String(watch.length),
        unit: "只跟踪中",
        chips,
        cta: changed ? `${changed} 只有新评分变化` : "查看观察池",
        note: changed ? `${changed} 只有变化` : "无新变化",
        muted: watch.length === 0,
      })
    );
  } else {
    row.appendChild(funnelStage({ href: "/app/watchlist", iconName: "eye", name: "观察", big: "—", cta: "查看观察池", note: "暂无数据", muted: true }));
  }
  row.appendChild(el("span", { class: "ov-arrow", "aria-hidden": "true" })).appendChild(icon("arrow", 18));

  if (Array.isArray(accounts) && accounts.length) {
    row.appendChild(
      funnelStage({
        href: "/app/portfolio",
        iconName: "brief",
        name: "持仓",
        big: String(accounts.length),
        unit: "个账户",
        chips: accounts.slice(0, 2).map((account) => [fmt(account.name), "outline"]),
        cta: "查看持仓与规则",
        note: "查看持仓",
      })
    );
  } else {
    row.appendChild(
      funnelStage({
        href: "/app/portfolio",
        iconName: "brief",
        name: "持仓",
        big: "—",
        unit: "未建账户",
        chips: [["先建账户并录入期初余额", "outline"]],
        cta: "新建账户",
        note: "未建账户",
        muted: true,
      })
    );
  }
  wrap.appendChild(row);
  return wrap;
}

function recentConclusionItems(runs) {
  const seen = new Set();
  const items = [];
  for (const run of Array.isArray(runs) ? runs : []) {
    if (run.status !== "success" && run.status !== "partial") continue;
    if (!run.ticker || seen.has(run.ticker)) continue;
    seen.add(run.ticker);
    items.push(run);
    if (items.length === 5) break;
  }
  return items;
}

function conclusionsCard(runs, strategies) {
  const items = recentConclusionItems(runs);
  const card = el("section", { class: "card card--flush ov-conclusions" });
  const head = el("div", { class: "card-head card-head--rule" });
  head.appendChild(el("h2", {}, "最近研究结论"));
  head.appendChild(el("a", { class: "card-more", href: "/app/runs" }, "运行中心 →"));
  card.appendChild(head);
  if (!items.length) {
    card.appendChild(el("p", { class: "ov-empty" }, Array.isArray(runs) ? "尚无已完成的研究运行。" : "运行记录暂时无法加载。"));
    return card;
  }
  const strategyOf = (run) => strategies.get(run.ticker) || run.industry || "—";

  const wrap = el("div", { class: "table-wrap ov-conclusions-table" });
  const tbl = el("table", { class: "tbl" });
  const headRow = el("tr");
  for (const [text, cls] of [["股票"], ["策略 / 行业"], ["业务分"], ["优先级"], ["完成", "num"]]) {
    headRow.appendChild(el("th", { scope: "col", class: cls }, text));
  }
  tbl.appendChild(el("thead")).appendChild(headRow);
  const body = el("tbody");
  for (const run of items) {
    const tr = el("tr");
    const nameCell = el("td");
    nameCell.appendChild(stockIdent(run.company_name, run.ticker));
    tr.appendChild(nameCell);
    tr.appendChild(el("td", {}, strategyOf(run)));
    const scoreCell = el("td");
    const score = el("div", { class: "score-inline" });
    score.appendChild(el("b", { class: "num" }, fmtNumber(run.business_score, 1)));
    score.appendChild(thinBar(run.business_score));
    scoreCell.appendChild(score);
    tr.appendChild(scoreCell);
    const priority = el("td");
    priority.appendChild(priorityBadge(run.workflow_label));
    tr.appendChild(priority);
    tr.appendChild(el("td", { class: "num muted" }, shortStamp(run.finished_at || run.started_at)));
    body.appendChild(tr);
  }
  tbl.appendChild(body);
  wrap.appendChild(tbl);
  card.appendChild(wrap);

  const list = el("div", { class: "ov-conclusions-list" });
  for (const run of items) {
    const row = el("a", { class: "list-row", href: `/app/stocks/${encodeURIComponent(run.ticker)}` });
    const main = el("div", { class: "list-row-main" });
    main.appendChild(el("b", {}, run.company_name || run.ticker));
    main.appendChild(el("small", {}, `${strategyOf(run)} · ${shortStamp(run.finished_at || run.started_at)}`));
    row.appendChild(main);
    const end = el("div", { class: "ov-row-score" });
    end.appendChild(el("b", { class: "num" }, fmtNumber(run.business_score, 1)));
    const entry = PRIORITY_LABELS[run.workflow_label];
    end.appendChild(el("small", {}, entry ? entry[0] : "—"));
    row.appendChild(end);
    list.appendChild(row);
  }
  card.appendChild(list);
  return card;
}

function runningCard(bucket) {
  const card = el("section", { class: "card card--flush ov-running" });
  if (!(bucket && bucket.count > 0)) card.classList.add("is-empty");
  const head = el("div", { class: "card-head card-head--rule" });
  head.appendChild(el("h2", {}, "运行中"));
  const count = bucket && bucket.count > 0 ? bucket.count : 0;
  if (count) head.appendChild(el("span", { class: "badge" }, String(count)));
  head.appendChild(el("a", { class: "card-more", href: "/app/runs" }, "全部 →"));
  card.appendChild(head);
  const entries = bucket && Array.isArray(bucket.entries) ? bucket.entries : [];
  if (!count) {
    card.appendChild(el("p", { class: "ov-empty" }, "当前没有排队或运行中的研究任务。"));
    return card;
  }
  const list = el("ul", { class: "ov-mini-list" });
  for (const entry of entries.slice(0, 5)) {
    const li = el("li");
    li.appendChild(statusBadge(entry.status));
    li.appendChild(stockIdent(entry.name, entry.ticker));
    li.appendChild(el("span", { class: "spacer" }));
    li.appendChild(el("span", { class: "muted num ov-mini-meta" }, `#${entry.run_id} · ${shortStamp(entry.started_at).slice(-5)}`));
    list.appendChild(li);
  }
  card.appendChild(list);
  if (count > 5) card.appendChild(el("div", { class: "card-foot ov-running-foot" }, `还有 ${count - 5} 个任务 · 在运行中心查看`));

  const summary = el("a", { class: "list-row ov-running-summary", href: "/app/runs" });
  const ico = el("span", { class: "list-row-icon", "aria-hidden": "true" });
  ico.appendChild(icon("clock", 16));
  summary.appendChild(ico);
  const main = el("div", { class: "list-row-main" });
  const running = entries.filter((entry) => entry.status === "running").length;
  main.appendChild(el("b", {}, `${count} 个研究任务${running ? "进行中" : "排队"}`));
  const earliest = entries.length ? entries[entries.length - 1].started_at : null;
  main.appendChild(el("small", {}, earliest ? `最早 ${shortStamp(earliest)}` : "在运行中心查看"));
  summary.appendChild(main);
  summary.appendChild(icon("chev", 16));
  card.appendChild(summary);
  return card;
}

function healthDetails(health) {
  const box = el("details", { class: "ov-health" });
  const number = (value) => (typeof value === "number" ? value : 0);
  box.appendChild(
    el(
      "summary",
      {},
      `数据健康概览：${number(health.errors)} 错误 · ${number(health.warnings)} 警告 · 快照积累 ${number(health.streak_weeks)} / ${number(health.target_weeks) || 8} 周`
    )
  );
  box.appendChild(healthBucket(health));
  return box;
}

async function loadDashboard(region, data) {
  const sub = document.querySelector("#overview-sub");
  const subtitle = overviewSubtitle(data.generated_at);
  if (sub && subtitle) sub.textContent = subtitle;

  const candidates = data.candidates && typeof data.candidates === "object" ? data.candidates : null;
  const health = data.data_health && typeof data.data_health === "object" ? data.data_health : null;
  const warnings = Array.isArray(data.warnings) ? data.warnings : [];
  const buckets = [data.running, data.failed, data.partial, data.needs_review, data.meaningful_changes];
  if (!buckets.some((bucket) => bucket && bucket.count > 0) && !warnings.length && !candidates && !health) {
    renderRegion(region, "empty", "今日无待处理项。");
    return;
  }

  // 漏斗与最近结论需要观察池 / 账户 / 运行列表；任何一个失败都只让对应区块显示"—"。
  const [watchResult, accountsResult, runsResult] = await Promise.allSettled([
    get("/watchlist"),
    get("/portfolio/accounts"),
    get("/research/runs?limit=30"),
  ]);
  const watch = watchResult.status === "fulfilled" && Array.isArray(watchResult.value) ? watchResult.value : null;
  const accounts = accountsResult.status === "fulfilled" && Array.isArray(accountsResult.value) ? accountsResult.value : null;
  const runs = runsResult.status === "fulfilled" && Array.isArray(runsResult.value) ? runsResult.value : null;
  const strategies = new Map();
  for (const entry of watch || []) {
    if (entry.latest_research && entry.latest_research.strategy_label) strategies.set(entry.ticker, entry.latest_research.strategy_label);
  }

  const container = el("div", { class: "ov-stack" });
  const strip = statusStrip(data);
  if (strip) container.appendChild(strip);
  container.appendChild(alertsCard(overviewAlertRows(data), data));
  container.appendChild(funnelBlock(data, watch, accounts));
  const pair = el("div", { class: "grid-7-5 ov-pair" });
  pair.appendChild(conclusionsCard(runs, strategies));
  pair.appendChild(runningCard(data.running));
  container.appendChild(pair);
  if (health) container.appendChild(healthDetails(health));
  renderRegion(region, "ready", container);
}

// ---- 运行中心 ---------------------------------------------------------

const RUNS_PAGE_SIZE = 20;
const RUNS_STATS_WINDOW = 100;
const WEEK_MS = 7 * 24 * 3600 * 1000;

function runsQuery(offset, status) {
  const params = new URLSearchParams({ limit: String(RUNS_PAGE_SIZE), offset: String(offset), kind: "all" });
  if (status) params.set("status", status);
  return `/research/runs?${params.toString()}`;
}

function runsLoadMore(region) {
  const button = el("button", { type: "button", class: "btn btn-sm load-more" }, "加载更多");
  button.addEventListener("click", () => {
    const current = region.querySelectorAll("tbody tr[data-run-id]").length;
    const filter = document.querySelector("#runs-status-filter");
    loadRunsPage(region, current, { append: true, status: filter && filter.value });
  });
  return button;
}

const RUN_HEADERS = [
  { label: "运行" },
  { label: "任务" },
  { label: "状态" },
  { label: "阶段" },
  { label: "重试", cls: "num" },
  { label: "开始" },
  { label: "耗时", cls: "num" },
  { label: "" },
];

const RUN_STEP_STAGES = ["fetching", "scoring", "explaining", "persisting", "completed"];

// 阶段时间线：只按运行当前所处阶段推断完成情况，不编造每步耗时。
function runSteps(run) {
  const index = RUN_STEP_STAGES.indexOf(run.stage);
  return RUN_STEP_STAGES.map((stage, position) => {
    let state = "pending";
    if (run.status === "success") state = "done";
    else if (run.status === "partial") state = stage === "explaining" ? "warn" : "done";
    else if (run.status === "running" && index >= 0) state = position < index ? "done" : position === index ? "active" : "pending";
    else if (run.status === "failed" && index >= 0) state = position < index ? "done" : position === index ? "failed" : "pending";
    return { text: label(RUN_STAGE_LABELS, stage), state };
  });
}

// 阶段时间线是个股研究流水线专用；其余任务只有「排队 / 已领取」之类的泛化阶段。
const RESEARCH_RUN_KIND = "ifind_research";

function stepsNode(run) {
  const box = el("ol", { class: "run-steps", "aria-label": "阶段进度" });
  for (const step of runSteps(run)) {
    const item = el("li", { class: `run-step is-${step.state}` });
    item.appendChild(el("i", { "aria-hidden": "true" }));
    item.appendChild(el("span", {}, step.text));
    box.appendChild(item);
  }
  return box;
}

function duration(startIso, endIso) {
  const start = Date.parse(startIso);
  const end = Date.parse(endIso);
  if (!Number.isFinite(start) || !Number.isFinite(end) || end < start) return "";
  const seconds = Math.round((end - start) / 1000);
  if (seconds < 60) return `${seconds} 秒`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分 ${String(seconds % 60).padStart(2, "0")} 秒`;
  return `${fmtNumber(seconds / 3600, 1)} 小时`;
}

// 耗时从被 worker 领取算起；started_at 是入队时间，积压的任务会把排队等待也算进去。
function runDuration(run) {
  return duration(run.claimed_at || run.started_at, run.finished_at);
}

function runTitle(run) {
  return run.company_name || run.ticker || label(RUN_KIND_LABELS, run.kind);
}

// 渲染详情主体：阶段时间线 + 事实 + 错误 + 制品 + 研究结论入口。detail 可为空（尚未加载）。
function renderRunDetail(container, run, detail) {
  container.replaceChildren();
  if (run.kind === RESEARCH_RUN_KIND) container.appendChild(stepsNode(run));
  const source = detail || run;
  const list = el("dl", { class: "kv-list run-facts" });
  for (const [key, value] of [
    ["类型", label(RUN_KIND_LABELS, run.kind)],
    ["股票", source.ticker],
    ["股票池", source.universe_code],
    ["参数", source.subtitle],
    ["执行进程", source.worker_id],
    ["入队时间", formatDateTime(source.started_at)],
    ["领取时间", formatDateTime(source.claimed_at)],
    ["结束时间", formatDateTime(source.finished_at)],
    ["工作流标签", source.workflow_label],
    ["尝试", `${run.attempt_count}/${run.max_attempts}`],
  ]) {
    list.appendChild(el("dt", {}, key));
    list.appendChild(el("dd", {}, fmt(value)));
  }
  container.appendChild(list);
  if (source.error_summary) container.appendChild(errorText(source.error_summary));
  const artifacts = detail && detail.result && Array.isArray(detail.result.artifacts) ? detail.result.artifacts : [];
  if (artifacts.length) {
    container.appendChild(el("h4", {}, "制品"));
    const links = el("p", { class: "artifact-links" });
    for (const artifact of artifacts) {
      const link = el("a", { href: artifact.download_url, class: "chip" }, artifact.name);
      link.addEventListener("click", (event) => {
        event.preventDefault();
        download(artifact.download_url);
      });
      links.appendChild(link);
    }
    container.appendChild(links);
  }
  if (run.ticker && (run.status === "success" || run.status === "partial")) {
    const links = el("p", { class: "run-detail-links" });
    links.appendChild(el("a", { href: `/app/stocks/${encodeURIComponent(run.ticker)}` }, "查看研究结论 →"));
    container.appendChild(links);
  }
}

async function loadRunDetail(container, run) {
  renderRunDetail(container, run, null);
  const hint = el("p", { class: "muted run-detail-loading" }, "加载详情…");
  container.appendChild(hint);
  try {
    const detail = await get(`/research/runs/${run.run_id}`);
    renderRunDetail(container, run, detail);
    container.dataset.loaded = "true";
  } catch (error) {
    hint.className = "notice notice-error";
    hint.textContent = error instanceof ApiError ? error.message : "详情加载失败。";
  }
}

function runStatusDot(status) {
  return el("span", { class: `run-dot run-dot-${status}`, "aria-hidden": "true" });
}

// 桌面：主行 + 隐藏的展开行；手机：一行一个任务的列表行，点按打开底部弹层。
function runRows(run) {
  const tr = el("tr", { "data-run-id": run.run_id });
  tr.appendChild(el("td", { class: "muted" }, `#${run.run_id}`));

  const what = el("td", { class: "stock-cell" });
  if (run.ticker) what.appendChild(stockIdent(run.company_name, run.ticker));
  else {
    what.appendChild(el("b", {}, label(RUN_KIND_LABELS, run.kind)));
    const hint = [run.universe_code, run.subtitle].filter(Boolean).join(" · ");
    if (hint) what.appendChild(el("small", { class: "muted" }, hint));
  }
  if (run.needs_agent_review) what.appendChild(el("span", { class: "badge warn" }, "需复核"));
  if (run.meaningful_change_count) what.appendChild(el("span", { class: "badge accent" }, `重要变化 ${run.meaningful_change_count}`));
  if (run.error_summary) what.appendChild(el("small", { class: "run-err" }, summarizeError(run.error_summary).summary));
  tr.appendChild(what);

  const statusCell = el("td");
  statusCell.appendChild(statusBadge(run.status));
  tr.appendChild(statusCell);
  tr.appendChild(el("td", { class: run.stage === "queued" ? "muted" : "" }, run.status === "queued" ? "—" : label(RUN_STAGE_LABELS, run.stage)));
  tr.appendChild(el("td", { class: "num" }, `${run.attempt_count}/${run.max_attempts}`));
  tr.appendChild(el("td", {}, shortStamp(run.started_at)));
  tr.appendChild(el("td", { class: "num muted" }, runDuration(run) || "—"));

  const toggleCell = el("td", { class: "run-toggle-cell" });
  const toggle = el("button", { type: "button", class: "btn btn-ghost btn-sm icon-btn run-toggle", "aria-expanded": "false", "aria-label": "展开详情" });
  toggle.appendChild(icon("chev", 16));
  toggleCell.appendChild(toggle);
  tr.appendChild(toggleCell);

  const detailRow = el("tr", { class: "run-exp-row", hidden: "" });
  detailRow.appendChild(el("td"));
  const detailCell = el("td", { colspan: RUN_HEADERS.length - 1 });
  const detailBody = el("div", { class: "run-detail" });
  detailCell.appendChild(detailBody);
  detailRow.appendChild(detailCell);
  const flip = () => {
    const open = detailRow.hidden;
    detailRow.hidden = !open;
    tr.classList.toggle("is-open", open);
    toggle.setAttribute("aria-expanded", open ? "true" : "false");
    toggle.setAttribute("aria-label", open ? "收起详情" : "展开详情");
    if (open && !detailBody.dataset.loaded) loadRunDetail(detailBody, run);
  };
  toggle.addEventListener("click", flip);

  const mobile = el("button", { type: "button", class: "list-row run-row", "data-run-id": run.run_id });
  mobile.appendChild(runStatusDot(run.status));
  const main = el("div", { class: "list-row-main" });
  const title = el("b", {}, runTitle(run));
  title.appendChild(el("span", { class: "muted num run-id" }, ` #${run.run_id}`));
  main.appendChild(title);
  const failed = run.status === "failed";
  const bits = [label(STATUS_LABELS, run.status)];
  if (!run.ticker) bits.unshift(label(RUN_KIND_LABELS, run.kind));
  if (run.status !== "queued") bits.push(label(RUN_STAGE_LABELS, run.stage));
  bits.push(`${run.attempt_count}/${run.max_attempts} 次`);
  main.appendChild(el("small", { class: failed ? "warn-text" : "" }, bits.join(" · ")));
  mobile.appendChild(main);
  mobile.appendChild(el("span", { class: "list-row-end num" }, shortStamp(run.started_at)));
  mobile.addEventListener("click", () => openRunSheet(run));

  return { rows: [tr, detailRow], mobile };
}

function openRunSheet(run) {
  const sheet = document.querySelector("#run-sheet");
  if (!sheet) return;
  const title = sheet.querySelector("#run-sheet-title");
  const sub = sheet.querySelector("#run-sheet-sub");
  const status = sheet.querySelector("#run-sheet-status");
  const body = sheet.querySelector("#run-sheet-body");
  if (title) title.textContent = `${runTitle(run)} · #${run.run_id}`;
  if (sub) {
    const took = runDuration(run);
    sub.textContent = [formatDateTime(run.started_at), took].filter((part) => part && part !== "—").join(" · ");
  }
  if (status) status.replaceChildren(statusBadge(run.status));
  if (body) loadRunDetail(body, run);
  if (window.zcOverlay) window.zcOverlay.open(sheet);
}

function appendRuns(region, runs) {
  const body = region.querySelector(".runs-table tbody");
  const mobile = region.querySelector(".runs-mobile");
  for (const run of runs) {
    const built = runRows(run);
    for (const row of built.rows) body.appendChild(row);
    mobile.appendChild(built.mobile);
  }
  const shown = region.querySelectorAll("tbody tr[data-run-id]").length;
  const count = region.querySelector(".runs-count");
  if (count) count.textContent = `已显示 ${shown} 条`;
}

function renderRunsTable(region, runs) {
  const container = el("div", { class: "runs-body" });
  const wrap = el("div", { class: "table-wrap runs-desktop" });
  const node = el("table", { class: "tbl runs-table" });
  const headRow = el("tr");
  for (const header of RUN_HEADERS) {
    headRow.appendChild(el("th", { scope: "col", class: header.cls || "" }, header.label));
    if (!header.label) headRow.lastChild.appendChild(el("span", { class: "visually-hidden" }, "详情"));
  }
  node.appendChild(el("thead")).appendChild(headRow);
  node.appendChild(el("tbody"));
  wrap.appendChild(node);
  container.appendChild(wrap);
  container.appendChild(el("div", { class: "list-group runs-mobile" }));
  const foot = el("div", { class: "card-foot runs-foot" });
  foot.appendChild(el("span", { class: "runs-count" }));
  foot.appendChild(el("span", { class: "spacer" }));
  foot.appendChild(runsLoadMore(region));
  container.appendChild(foot);
  renderRegion(region, "ready", container);
  appendRuns(region, runs);
  if (runs.length < RUNS_PAGE_SIZE) region.querySelector(".load-more")?.remove();
}

async function loadRuns(region, runs, { append = false } = {}) {
  if (!append) {
    if (!Array.isArray(runs) || !runs.length) {
      renderRegion(region, "empty");
      return;
    }
    renderRunsTable(region, runs);
    return;
  }
  if (!Array.isArray(runs) || !runs.length) {
    region.querySelector(".load-more")?.remove();
    return;
  }
  if (!region.querySelector(".runs-table tbody")) {
    renderRunsTable(region, runs);
    return;
  }
  appendRuns(region, runs);
  if (runs.length < RUNS_PAGE_SIZE) region.querySelector(".load-more")?.remove();
}

function loadRunsPage(region, offset, { append = false, status } = {}) {
  const promise = get(runsQuery(offset, status || ""))
    .then((runs) => loadRuns(region, runs, { append }))
    .catch((error) => renderRegion(region, "error", error.message));
  return promise;
}

// KPI 条与筛选计数：取最近 RUNS_STATS_WINDOW 条运行自行统计（接口没有汇总端点）。
function kpiCell(text, value, foot, { kind = "", tone = "" } = {}) {
  const node = el("div", { class: `kpi ${kind}`.trim() });
  const labelNode = el("div", { class: "kpi-label" });
  labelNode.appendChild(el("span", { class: `sdot ${tone}`.trim(), "aria-hidden": "true" }));
  labelNode.appendChild(document.createTextNode(text));
  node.appendChild(labelNode);
  node.appendChild(el("div", { class: "kpi-value num" }, String(value)));
  node.appendChild(el("div", { class: "kpi-foot" }, foot));
  return node;
}

function averageDuration(runs) {
  const spans = runs
    .map((run) => Date.parse(run.finished_at) - Date.parse(run.claimed_at || run.started_at))
    .filter((span) => Number.isFinite(span) && span >= 0);
  if (!spans.length) return "";
  const seconds = Math.round(spans.reduce((sum, span) => sum + span, 0) / spans.length / 1000);
  return seconds < 60 ? `${seconds} 秒` : `${Math.floor(seconds / 60)} 分 ${String(seconds % 60).padStart(2, "0")} 秒`;
}

function renderRunsStats(runs) {
  const box = document.querySelector("#runs-kpis");
  if (!box) return;
  const list = Array.isArray(runs) ? runs : [];
  const now = Date.now();
  const recent = (run) => {
    const at = Date.parse(run.started_at);
    return Number.isFinite(at) && now - at <= WEEK_MS;
  };
  const queued = list.filter((run) => run.status === "queued");
  const running = list.filter((run) => run.status === "running");
  const success = list.filter((run) => run.status === "success" && recent(run));
  const failed = list.filter((run) => run.status === "failed" && recent(run));
  const partial = list.filter((run) => run.status === "partial" && recent(run));
  const earliest = queued.length ? queued.reduce((a, b) => (Date.parse(a.started_at) <= Date.parse(b.started_at) ? a : b)) : null;
  const avg = averageDuration(success);
  box.replaceChildren(
    kpiCell("排队中", queued.length, earliest ? `最早 ${shortStamp(earliest.started_at)}` : "无排队", { tone: "mute" }),
    kpiCell("运行中", running.length, running.length ? "执行中" : "worker 空闲"),
    kpiCell("近 7 日成功", success.length, avg ? `平均 ${avg}` : "—"),
    kpiCell("近 7 日失败", failed.length, partial.length ? `另有 ${partial.length} 个部分成功` : failed.length ? "见失败筛选" : "无失败", {
      kind: failed.length ? "attention" : "",
      tone: failed.length ? "warn" : "",
    })
  );
  const counts = { queued: queued.length, running: running.length, partial: list.filter((run) => run.status === "partial").length, failed: list.filter((run) => run.status === "failed").length };
  for (const node of document.querySelectorAll("#runs-seg [data-count]")) {
    const value = counts[node.getAttribute("data-count")];
    node.textContent = value ? ` ${value}` : "";
  }
}

async function loadRunsStats() {
  try {
    // 排队 / 运行中的任务单独取，避免被最近 100 条已完成任务挤出统计窗口。
    const query = (extra) => get(`/research/runs?kind=all&limit=${RUNS_STATS_WINDOW}${extra}`);
    const [recent, queued, running] = await Promise.all([query(""), query("&status=queued"), query("&status=running")]);
    const byId = new Map();
    for (const run of [...recent, ...queued, ...running]) byId.set(run.run_id, run);
    renderRunsStats([...byId.values()]);
  } catch {
    renderRunsStats([]);
  }
}

function setRunsFilter(status, { reload = true } = {}) {
  const filter = document.querySelector("#runs-status-filter");
  if (filter) filter.value = status;
  for (const button of document.querySelectorAll("#runs-seg button")) {
    const on = button.getAttribute("data-status") === status;
    button.classList.toggle("on", on);
    button.setAttribute("aria-pressed", on ? "true" : "false");
  }
  if (!reload) return;
  const region = document.querySelector("#runs-list");
  if (region) loadRunsPage(region, 0, { status });
}

function wireRunsPage() {
  const filter = document.querySelector("#runs-status-filter");
  if (!filter) return;
  filter.addEventListener("change", () => setRunsFilter(filter.value));
  for (const button of document.querySelectorAll("#runs-seg button")) {
    button.addEventListener("click", () => setRunsFilter(button.getAttribute("data-status") || ""));
  }
  const refresh = () => {
    loadRunsStats();
    const region = document.querySelector("#runs-list");
    if (region) loadRunsPage(region, 0, { status: filter.value });
  };
  for (const id of ["#runs-refresh", "#runs-refresh-m"]) document.querySelector(id)?.addEventListener("click", refresh);
  loadRunsStats();
}

// ---- 单股详情 ---------------------------------------------------------

// 字段来源：field_sources 按数据域分组（stock / market_data / financial_data）。
function sourceOf(item, field) {
  const sources = item.field_sources && typeof item.field_sources === "object" ? item.field_sources : {};
  if (sources[field] && typeof sources[field] === "object" && "provider" in sources[field]) return sources[field];
  for (const domain of Object.values(sources)) {
    if (domain && typeof domain === "object" && domain[field] && typeof domain[field] === "object") return domain[field];
  }
  return null;
}

function verifyTag(source) {
  if (!source) return null;
  if (source.verification === "verified") return el("span", { class: "verify verify-ok", title: "iFinD 已核对" }, "已核对");
  if (source.quality === "derived") return el("span", { class: "verify verify-derived", title: "由快照字段派生" }, "派生");
  if (source.verification === "unverified") return el("span", { class: "verify verify-no", title: "免费源，未经 iFinD 核对" }, "未核对");
  return null;
}

function fact(term, value) {
  const node = el("div", { class: "fact" });
  node.appendChild(el("dt", {}, term));
  const dd = el("dd");
  if (value instanceof Node) dd.appendChild(value);
  else dd.textContent = fmt(value);
  node.appendChild(dd);
  return node;
}

function isCompactViewport() {
  return typeof window !== "undefined" && window.matchMedia("(max-width: 56rem)").matches;
}

// 页头状态：名称 / 行业 / 观察池 / 策略 / 最新一次运行；价格由收盘价折线回填。
const stockHead = { name: "", industry: "", watch: false, strategy: "", run: null };

function paintStockHead() {
  const nameNode = document.querySelector("#stock-name");
  if (nameNode && stockHead.name) nameNode.textContent = stockHead.name;
  const avatar = document.querySelector("#stock-avatar");
  if (avatar && stockHead.name) avatar.textContent = stockHead.name.slice(0, 2);
  const title = document.querySelector("[data-appbar-title]");
  if (title && stockHead.name) title.textContent = stockHead.name;
  const badges = document.querySelector("#stock-badges");
  if (!badges) return;
  badges.replaceChildren();
  if (stockHead.strategy) badges.appendChild(el("span", { class: "badge accent" }, stockHead.strategy));
  if (stockHead.industry) badges.appendChild(el("span", { class: "badge" }, stockHead.industry));
  if (stockHead.watch) badges.appendChild(el("span", { class: "badge" }, "观察池"));
  if (stockHead.run) {
    const ok = stockHead.run.status === "success";
    const bad = stockHead.run.status === "failed";
    badges.appendChild(
      el("span", { class: `badge dot${ok ? " accent" : bad ? " warn" : ""}` }, `运行 #${stockHead.run.id} ${label(STATUS_LABELS, stockHead.run.status)}`)
    );
  }
}

function setStockHead(patch) {
  Object.assign(stockHead, patch);
  paintStockHead();
}

async function loadStockIdentity(ticker) {
  try {
    const items = await searchStocks(ticker, 5);
    const hit = Array.isArray(items) ? items.find((item) => item.ticker === ticker) : null;
    if (hit) setStockHead({ name: hit.name || "", industry: stockHead.industry || hit.industry || "", watch: Boolean(hit.on_watchlist) });
  } catch {
    // 名称只是装饰：搜索不可用时页面照常显示代码。
  }
}

function setStockPrice(bars) {
  const box = document.querySelector("#stock-px");
  if (!box || !Array.isArray(bars) || !bars.length) return;
  const last = bars[bars.length - 1];
  const prev = bars.length > 1 ? bars[bars.length - 2] : null;
  const price = box.querySelector("#stock-px-last");
  const change = box.querySelector("#stock-px-chg");
  const meta = box.querySelector("#stock-px-meta");
  if (price) price.textContent = last.close.toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  if (change) {
    if (prev && prev.close) {
      const pct = ((last.close - prev.close) / prev.close) * 100;
      change.textContent = `${pct > 0 ? "+" : ""}${pct.toFixed(2)}%`;
      change.className = `num ${pct > 0 ? "up" : pct < 0 ? "down" : "flat"}`;
    } else {
      change.textContent = "";
    }
  }
  if (meta) meta.textContent = `${last.trade_date.slice(5)} 收盘`;
  box.hidden = false;
}

// 分项按分值排序；最低两项且低于 60 标为"主要拖累"（只描述分项相对位置）。
function rankedFactors(item) {
  const factors = item.factor_scores || {};
  const keys = Object.keys(factors).sort((a, b) => (factors[b] ?? -1) - (factors[a] ?? -1));
  const drags = new Set(keys.filter((key) => typeof factors[key] === "number" && factors[key] < 60).slice(-2));
  return { factors, keys, drags };
}

function verdictCard(item, scoreMeta, detail) {
  const card = el("section", { class: "card verdict-card", id: "stock-verdict", "data-evidence-section": "evidence-score" });
  const head = el("div", { class: "card-head" });
  head.appendChild(el("h2", {}, "研究结论"));
  head.appendChild(el("span", { class: "spacer" }));
  if (item.suggestion_label || item.suggestion) {
    head.appendChild(el("span", { class: "badge" }, item.suggestion_label || humanizeText(item.suggestion)));
  }
  card.appendChild(head);

  const body = el("div", { class: "verdict" });
  body.appendChild(ring(item.investment_score, 100, { caption: "投资分" }));
  const facts = el("dl", { class: "facts verdict-facts" });
  facts.appendChild(fact("估值水平", label(VALUATION_LABELS, item.valuation_level)));
  facts.appendChild(fact("风险等级", label(RISK_LABELS, item.risk_level)));
  facts.appendChild(fact("研究置信度", fmtNumber(item.research_confidence_score, 0)));
  facts.appendChild(
    fact("数据质量", typeof item.data_quality_score === "number" ? `${fmtNumber(item.data_quality_score * 100, 0)}%` : "—")
  );
  body.appendChild(facts);
  card.appendChild(body);

  const tags = el("div", { class: "cluster verdict-tags" });
  if (item.strategy_label) tags.appendChild(el("span", { class: "badge accent" }, item.strategy_label));
  if (item.needs_agent_review) tags.appendChild(el("span", { class: "badge warn" }, "需人工复核"));
  if (tags.children.length) card.appendChild(tags);

  const { factors, keys, drags } = rankedFactors(item);
  const meta = `评分 ${(scoreMeta && scoreMeta.scoring_version) || item.scoring_version || "—"} · 业务分 ${fmtNumber(item.business_score, 1)}`;
  const line = el("div", { class: "verdict-line callout" });
  line.appendChild(icon("info", 16));
  const text = el("span");
  if (keys.length) {
    const best = keys[0];
    text.appendChild(document.createTextNode("最强分项 "));
    text.appendChild(el("b", {}, `${scoreFactorLabel(best)} ${fmtNumber(factors[best], 1)}`));
    if (drags.size) {
      text.appendChild(document.createTextNode("；主要拖累 "));
      text.appendChild(el("b", {}, [...drags].map((key) => `${scoreFactorLabel(key)} ${fmtNumber(factors[key], 1)}`).join("、")));
    }
    text.appendChild(document.createTextNode(`。${meta}`));
  } else {
    text.textContent = meta;
  }
  line.appendChild(text);
  card.appendChild(line);

  const run = el("p", { class: "verdict-run faint" }, `运行 #${detail.run_id} · `);
  run.appendChild(statusBadge(detail.status));
  card.appendChild(run);
  return card;
}

function factorCard(item, scoreMeta) {
  const card = el("section", { class: "card", id: "stock-scores", "data-evidence-section": "evidence-factors" });
  const head = el("div", { class: "card-head" });
  head.appendChild(el("h2", {}, "分项评分"));
  const version = (scoreMeta && scoreMeta.scoring_version) || item.scoring_version;
  if (version) head.appendChild(el("span", { class: "faint" }, `评分 ${version}`));
  card.appendChild(head);
  const { factors, keys, drags } = rankedFactors(item);
  if (!keys.length) {
    card.appendChild(el("p", { class: "muted" }, "无分项评分。"));
    return card;
  }
  const list = el("ul", { class: "score-bars" });
  for (const key of keys) {
    const value = factors[key];
    const row = el("li", { class: `score-bar${drags.has(key) ? " drag" : ""}`, title: key });
    const name = el("span", { class: "score-bar-label" }, scoreFactorLabel(key));
    if (drags.has(key)) name.appendChild(el("small", { class: "score-bar-tag" }, "主要拖累"));
    row.appendChild(name);
    row.appendChild(thinBar(value, { height: 6, weak: drags.has(key) }));
    row.appendChild(el("span", { class: "score-bar-value num" }, fmtNumber(value, 1)));
    list.appendChild(row);
  }
  card.appendChild(list);
  return card;
}

function notApplicableFields(item) {
  const flags = Array.isArray(item.quality_flags) ? item.quality_flags : [];
  return [...new Set(flags.filter((flag) => flag.flag === "model_not_applicable").map((flag) => flag.field))];
}

function metricsCard(item) {
  const metrics = item.key_metrics || {};
  const card = el("section", { class: "card", id: "stock-metrics", "data-evidence-section": "evidence-metrics" });
  const head = el("div", { class: "card-head" });
  head.appendChild(el("h2", {}, "关键指标"));
  head.appendChild(el("span", { class: "faint" }, "已核对 = iFinD 核对；未核对 = 免费源；派生 = 由快照计算"));
  card.appendChild(head);
  const grid = el("div", { class: "metrics" });
  for (const [key, value] of Object.entries(metrics)) {
    const tile = el("div", { class: `metric${value === null || value === undefined ? " na" : ""}`, title: key });
    const top = el("div", { class: "metric-top" });
    top.appendChild(el("span", {}, fieldLabel(key)));
    const tag = verifyTag(sourceOf(item, key));
    if (tag) top.appendChild(tag);
    tile.appendChild(top);
    tile.appendChild(
      el("div", { class: "metric-value" }, value === null || value === undefined ? "缺失" : formatFactorValue(value, METRIC_UNITS[key]))
    );
    grid.appendChild(tile);
  }
  const model = item.selected_model_label || "当前模型";
  for (const field of notApplicableFields(item)) {
    if (field in metrics) continue;
    const tile = el("div", { class: "metric na", title: field });
    const top = el("div", { class: "metric-top" });
    top.appendChild(el("span", {}, fieldLabel(field)));
    tile.appendChild(top);
    tile.appendChild(el("div", { class: "metric-value" }, "不适用"));
    tile.appendChild(el("div", { class: "metric-note" }, `${model}不使用该字段，未参与评分`));
    grid.appendChild(tile);
  }
  card.appendChild(grid);
  return card;
}

// Freshness is judged per data kind: market data in exchange trading days,
// financial data by report period, the rest by hours / days.
function describeFreshness(block) {
  if (block.age_trading_days !== undefined && block.age_trading_days !== null) {
    return `${block.age_trading_days} 个交易日前（上限 ${block.max_age_trading_days ?? block.max_age_days}）`;
  }
  if (block.basis === "report_period") {
    const period = block.report_period_end || block.report_year || "未知";
    const expected = block.expected_report_year ? `，应有 ${block.expected_report_year} 年报` : "";
    return `报告期 ${period}${expected}`;
  }
  const hours = Number(block.age_hours);
  const age = Number.isFinite(hours) && hours >= 48 ? `${fmtNumber(hours / 24, 1)} 天前` : `${fmtNumber(block.age_hours, 0)} 小时前`;
  return `${age}（上限 ${block.max_age_days} 天）`;
}

function qualityCard(item) {
  const card = el("section", { class: "card", id: "stock-quality", "data-evidence-section": "evidence-quality" });
  const freshness = item.freshness || {};
  const blocks = Object.values(freshness);
  const stale = blocks.filter((block) => block && typeof block === "object" && block.is_stale).length;
  const head = el("div", { class: "card-head" });
  head.appendChild(el("h2", {}, "数据新鲜度与质量"));
  if (blocks.length) {
    head.appendChild(el("span", { class: `badge dot ${stale ? "warn" : "accent"}` }, stale ? `${stale} 项已过期` : "全部正常"));
  }
  card.appendChild(head);
  if (item.needs_agent_review) {
    card.appendChild(el("p", { class: "notice notice-warn" }, "该标的已标记为需要人工复核。"));
  }
  const list = el("ul", { class: "fresh-list" });
  for (const [name, block] of Object.entries(freshness)) {
    const row = el("li");
    const isStale = block && typeof block === "object" ? Boolean(block.is_stale) : false;
    row.appendChild(el("span", { class: `dot ${isStale ? "dot-stale" : "dot-fresh"}`, "aria-hidden": "true" }));
    row.appendChild(el("span", { class: "fresh-what" }, FRESHNESS_LABELS[name] || name));
    row.appendChild(el("span", {}, isStale ? "已过期" : "正常"));
    row.appendChild(el("span", { class: "faint" }, block && typeof block === "object" ? describeFreshness(block) : fmt(block)));
    list.appendChild(row);
  }
  if (list.children.length) card.appendChild(list);

  const missing = Array.isArray(item.missing_fields) ? item.missing_fields : [];
  const flags = Array.isArray(item.quality_flags) ? item.quality_flags : [];
  // A field the selected model does not use is "not applicable", not missing:
  // show that once instead of the data layer's missing-field note.
  const notApplicable = new Set(notApplicableFields(item));
  const notes = [`缺失字段：${missing.length ? missing.map(fieldLabel).join("、") : "无"}`];
  for (const field of notApplicable) notes.push(`${fieldLabel(field)}：不适用于当前模型（不参与评分）`);
  for (const flag of flags) {
    if (flag.severity !== "review" || notApplicable.has(flag.field)) continue;
    notes.push(`${fieldLabel(flag.field)}：${label(QUALITY_FLAG_LABELS, flag.flag) || flag.message || "需复核"}`);
  }
  const noteList = el("ul", { class: "quality-notes" });
  for (const note of notes) noteList.appendChild(el("li", {}, note));
  card.appendChild(noteList);
  return card;
}

// ---- 证据导航（MVP-403） -----------------------------------------------

// 把 supporting_fields 的点路径解析到确定性 score item 中的值与 provenance。
// 路径不存在时返回 null，由调用方显式显示"引用失效"，绝不静默隐藏。
function resolveEvidence(scoreItem, path) {
  if (typeof path !== "string" || !path.length) return null;
  let node = scoreItem;
  for (const part of path.split(".")) {
    if (node && typeof node === "object" && part in node) {
      node = node[part];
    } else {
      return null;
    }
  }
  const leaf = path.split(".").pop();
  const provenance = sourceOf(scoreItem, leaf);
  return { path, value: node, provenance };
}

function evidenceSectionFor(path) {
  if (path.startsWith("factor_scores")) return "evidence-factors";
  if (path.startsWith("key_metrics")) return "evidence-metrics";
  if (/^(missing_fields|quality_flags|freshness|warnings|provider_conflicts|needs_agent_review|data_quality_score)/.test(path)) {
    return "evidence-quality";
  }
  return "evidence-score";
}

function formatEvidenceValue(path, value) {
  if (value === null || value === undefined) return "无值（未知，不参与推断）";
  return formatPathValue(path, value);
}

function showEvidence(detailRegion, scoreItem, path) {
  const resolved = resolveEvidence(scoreItem, path);
  const inspector = detailRegion.querySelector("#evidence-inspector");
  if (!inspector) return;
  inspector.replaceChildren();
  if (!resolved) {
    inspector.appendChild(
      el("p", { class: "notice notice-error" }, `引用失效：确定性结果中不存在字段 ${path}。`)
    );
    inspector.hidden = false;
    return;
  }
  inspector.appendChild(el("h3", { title: resolved.path }, `证据：${pathLabel(resolved.path)}`));
  const provenance = resolved.provenance || {};
  inspector.appendChild(
    kvTable([
      ["字段值", formatEvidenceValue(resolved.path, resolved.value)],
      ["数据提供方", provenance.provider ? label(PROVIDER_LABELS, provenance.provider) : null],
      ["来源接口", provenance.source],
      ["更新时间", formatDateTime(provenance.updated_at)],
      ["质量", provenance.quality],
      ["核对", provenance.verification ? label(SOURCE_VERIFICATION_LABELS, provenance.verification) : null],
      ["备注", provenance.note],
      ["字段路径", resolved.path],
    ])
  );
  inspector.hidden = false;

  const targetId = evidenceSectionFor(resolved.path);
  for (const section of detailRegion.querySelectorAll("[data-evidence-section]")) {
    section.classList.remove("evidence-flash");
  }
  const target = detailRegion.querySelector(`[data-evidence-section="${targetId}"]`);
  if (target) {
    target.classList.add("evidence-flash");
    target.scrollIntoView({ behavior: "smooth", block: "start" });
  }
}

const EXPLANATION_GROUPS = [
  ["positive_evidence", "正面证据", "pos"],
  ["negative_evidence", "负面证据", "neg"],
  ["risk_factors", "风险因素", "risk"],
  ["classification_rationale", "分类依据", "class"],
  ["data_quality_notes", "数据质量说明", "quality"],
];

// 折叠分组：桌面默认展开，手机默认收起（证据多时先给摘要）。
function evidenceGroup(kind, title, count) {
  const box = el("details", { class: `ev-group ${kind}` });
  if (!isCompactViewport()) box.open = true;
  const summary = el("summary");
  summary.appendChild(el("i", { class: "ev-dot", "aria-hidden": "true" }));
  summary.appendChild(el("span", { class: "ev-title" }, title));
  summary.appendChild(el("span", { class: "c" }, count === null || count === undefined ? "" : String(count)));
  summary.appendChild(icon("chevd", 16));
  box.appendChild(summary);
  return box;
}

// 引用依据：低调的文字标记（可点开证据详情，手机上不可点，见 pages/stock.css）。
function evidenceRef(field, scoreItem, detailRegion, broken) {
  const resolved = resolveEvidence(scoreItem, field);
  if (!resolved) broken.add(field);
  const value =
    resolved && resolved.value !== null && (typeof resolved.value !== "object" || Array.isArray(resolved.value))
      ? ` ${formatPathValue(field, resolved.value)}`
      : "";
  const ref = el(
    "button",
    { type: "button", class: "ev-ref", "data-field-path": field, title: field, "aria-pressed": "false" },
    `${pathLabel(field)}${value}`
  );
  ref.addEventListener("click", () => {
    for (const other of detailRegion.querySelectorAll(".ev-ref[aria-pressed='true']")) {
      other.setAttribute("aria-pressed", "false");
    }
    ref.setAttribute("aria-pressed", "true");
    showEvidence(detailRegion, scoreItem, field);
  });
  return ref;
}

function explanationCard(detail, scoreItem, detailRegion) {
  const card = el("section", {
    class: "card card--ai explanation-card",
    id: "stock-ai",
    "data-evidence-section": "evidence-explanation",
  });
  const head = el("div", { class: "card-head" });
  const sparkle = icon("ai", 16);
  sparkle.classList.add("ai-text");
  head.appendChild(sparkle);
  head.appendChild(el("h2", {}, "AI 解释"));
  head.appendChild(aiStamp("LLM 生成 · 只复述上方确定性结果"));
  card.appendChild(head);

  const groups = el("div", { class: "ev-groups" });
  const explanation = detail.result && detail.result.explanation;
  const broken = new Set();
  if (!explanation) {
    card.appendChild(
      el(
        "p",
        { class: "notice notice-warn" },
        detail.status === "partial"
          ? "解释不可用：评分成功但解释生成失败（部分成功）。以上确定性结果仍然有效。"
          : "解释不可用。"
      )
    );
  } else {
    if (explanation.summary) card.appendChild(el("p", { class: "explain-summary" }, humanizeText(explanation.summary)));
    for (const [key, title, kind] of EXPLANATION_GROUPS) {
      const claims = explanation[key];
      if (!Array.isArray(claims) || !claims.length) continue;
      const group = evidenceGroup(kind, title, claims.length);
      const list = el("ul", { class: "ev" });
      for (const claim of claims) {
        const li = el("li", {}, humanizeText(claim.text));
        const refs = el("div", { class: "ev-refs" });
        for (const field of claim.supporting_fields || []) refs.appendChild(evidenceRef(field, scoreItem, detailRegion, broken));
        if (refs.children.length) li.appendChild(refs);
        list.appendChild(li);
      }
      group.appendChild(list);
      groups.appendChild(group);
    }
    if (Array.isArray(explanation.open_questions) && explanation.open_questions.length) {
      const group = evidenceGroup("risk", "待核查问题", explanation.open_questions.length);
      const list = el("ul", { class: "qs open-questions" });
      for (const question of explanation.open_questions) list.appendChild(el("li", {}, humanizeText(question)));
      group.appendChild(list);
      groups.appendChild(group);
    }
  }

  // 自上次以来的变化：#stock-changes 区域原本在页面模板里，这里搬进 AI 卡片（加载逻辑不变）。
  const changes = evidenceGroup("class", "自上次以来的变化", null);
  changes.classList.add("ev-changes");
  const stash = document.querySelector("#stock-changes") || el("div", { id: "stock-changes", class: "data-region" });
  changes.appendChild(stash);
  groups.appendChild(changes);
  card.appendChild(groups);

  if (broken.size) {
    // 引用必须能定位到唯一字段；失效引用必须显式可见，不允许静默隐藏。
    card.appendChild(
      el(
        "p",
        { class: "notice notice-error", "data-evidence-broken": [...broken].join(",") },
        `引用失效：${[...broken].map(pathLabel).join("、")}`
      )
    );
  }
  if (explanation && explanation.workflow_conclusion) {
    card.appendChild(el("p", { class: "faint" }, `工作流结论：${humanizeText(explanation.workflow_conclusion)}`));
  }
  return card;
}

// 表格（非堆叠）：手机上由页面 CSS 隐藏次要列，保持一行一条。
function plainTable(headers, rows, className) {
  const node = el("table", { class: `tbl ${className}`.trim() });
  const headRow = el("tr");
  for (const header of headers) headRow.appendChild(el("th", { scope: "col", class: header.cls || "" }, header.label));
  node.appendChild(el("thead")).appendChild(headRow);
  const body = el("tbody");
  for (const row of rows) body.appendChild(row);
  node.appendChild(body);
  const wrap = el("div", { class: "table-wrap" });
  wrap.appendChild(node);
  return wrap;
}

async function loadStockHistory(region, snapshots) {
  const tabCount = document.querySelector('[data-tab-count="history"]');
  if (tabCount) tabCount.textContent = Array.isArray(snapshots) && snapshots.length ? String(snapshots.length) : "";
  if (!Array.isArray(snapshots) || !snapshots.length) {
    renderRegion(region, "empty");
    return null;
  }
  const rows = snapshots.map((snapshot) => {
    const tr = el("tr");
    const runCell = el("td", { class: "hist-run" });
    runCell.appendChild(el("a", { href: `/app/runs` }, `#${snapshot.run_id}`));
    tr.appendChild(runCell);
    tr.appendChild(el("td", { class: "hist-date" }, fmt(snapshot.as_of)));
    tr.appendChild(el("td", { class: "hist-strategy" }, fmt(snapshot.strategy_label)));
    tr.appendChild(el("td", { class: "num" }, fmtNumber(snapshot.investment_score, 2)));
    tr.appendChild(el("td", { class: "num hist-conf" }, fmtNumber(snapshot.research_confidence_score, 1)));
    const reviewCell = el("td");
    reviewCell.appendChild(statusBadge(snapshot.needs_agent_review ? "needs_review" : "ok"));
    tr.appendChild(reviewCell);
    return tr;
  });
  renderRegion(
    region,
    "ready",
    plainTable(
      [
        { label: "运行" },
        { label: "数据日期" },
        { label: "策略", cls: "hist-strategy" },
        { label: "投资分", cls: "num" },
        { label: "置信度", cls: "num hist-conf" },
        { label: "复核" },
      ],
      rows,
      "hist-table"
    )
  );
  return snapshots[0];
}

// 股票页顶部：收盘价曲线（区间可选），与研究快照无关（无快照也画）。
const STOCK_RANGE_DAYS = [30, 60, 120, 240, 480, 1000];
const STOCK_RANGE_DAYS_COMPACT = [60, 240, 480, 1000];

function loadStockSpark() {
  const host = document.querySelector("#stock-spark");
  if (!host || host.dataset.sparkMounted) return;
  host.dataset.sparkMounted = "1";
  const ticker = host.getAttribute("data-ticker");
  if (!ticker) return;
  const wanted = isCompactViewport() ? STOCK_RANGE_DAYS_COMPACT : STOCK_RANGE_DAYS;
  mountPriceChart(host, ticker, loadBars, {
    title: document.querySelector("#stock-spark-title"),
    ranges: PRICE_RANGES.filter((range) => wanted.includes(range.days)),
    onData: ({ bars }) => setStockPrice(bars),
  });
}

async function loadStockDetail(detailRegion, latestSnapshot) {
  loadStockSpark();
  if (!latestSnapshot) {
    detailRegion.hidden = true;
    return;
  }
  try {
    const detail = await get(`/research/runs/${latestSnapshot.run_id}`);
    detailRegion.hidden = false;
    detailRegion.replaceChildren();
    const score = detail.result && detail.result.score;
    const scoreItem =
      score && Array.isArray(score.items) && score.items.length
        ? score.items[0]
        : score && ("investment_score" in score || "strategy_label" in score)
          ? score
          : null;
    setStockHead({
      name: detail.company_name || stockHead.name,
      industry: detail.industry || stockHead.industry,
      strategy: (scoreItem && scoreItem.strategy_label) || latestSnapshot.strategy_label || "",
      run: { id: detail.run_id, status: detail.status },
    });
    if (!scoreItem) {
      detailRegion.appendChild(
        el("p", { class: "notice notice-warn" }, "运行尚未产生确定性评分。")
      );
      return;
    }
    const meta = detail.result.score.metadata;
    detailRegion.appendChild(verdictCard(scoreItem, meta, detail));
    detailRegion.appendChild(metricsCard(scoreItem));
    detailRegion.appendChild(factorCard(scoreItem, meta));
    const inspector = el("aside", {
      id: "evidence-inspector",
      class: "card evidence-inspector",
      hidden: "",
    });
    detailRegion.appendChild(inspector);
    detailRegion.appendChild(explanationCard(detail, scoreItem, detailRegion));
    detailRegion.appendChild(qualityCard(scoreItem));
    const changesRegion = document.querySelector("#stock-changes");
    if (changesRegion) await loadStockChanges(changesRegion, detail.run_id);
  } catch (error) {
    detailRegion.hidden = false;
    detailRegion.replaceChildren();
    detailRegion.appendChild(
      el("p", { class: "notice notice-error" }, error instanceof ApiError ? error.message : "详情加载失败。")
    );
  }
}

const CHANGES_VISIBLE = 6;

function diffRow(change) {
  const field = change.field || change.path;
  const li = el("li", { class: change.meaningful ? "is-meaningful" : "" });
  li.appendChild(el("span", { class: "diff-name" }, pathLabel(field) || "字段"));
  li.appendChild(el("span", { class: "was num" }, formatPathValue(field, change.before)));
  li.appendChild(icon("arrow", 14));
  li.appendChild(el("span", { class: "now num" }, formatPathValue(field, change.after)));
  if (change.meaningful) li.appendChild(el("span", { class: "badge warn" }, "重要"));
  return li;
}

async function loadStockChanges(region, runId) {
  const countNode = region.closest("details")?.querySelector("summary .c");
  try {
    const changes = await get(`/research/runs/${runId}/changes`);
    if (!changes || (!changes.changes || !changes.changes.length)) {
      if (countNode) countNode.textContent = "";
      renderRegion(region, "empty", changes && changes.baseline ? "这是首次研究（baseline），暂无对比。" : "与上次快照无重要变化。");
      return;
    }
    if (countNode) countNode.textContent = String(changes.change_count ?? changes.changes.length);
    const container = el("div", { class: "diff-box" });
    container.appendChild(
      el(
        "p",
        { class: "faint diff-meta" },
        `对比运行 #${changes.previous_run_id ?? "—"} → #${changes.current_run_id}；重要变化 ${changes.meaningful_change_count}/${changes.change_count}。`
      )
    );
    // 重要变化排前面，其余按原顺序；默认只露前几项。
    const ordered = [...changes.changes].sort((a, b) => Number(Boolean(b.meaningful)) - Number(Boolean(a.meaningful)));
    const list = el("ul", { class: "diff num" });
    const rows = ordered.map((change, index) => {
      const row = diffRow(change);
      if (index >= CHANGES_VISIBLE) row.hidden = true;
      list.appendChild(row);
      return row;
    });
    container.appendChild(list);
    if (rows.length > CHANGES_VISIBLE) {
      const more = el("button", { type: "button", class: "link-button", "aria-expanded": "false" }, `查看全部 ${rows.length} 项`);
      more.addEventListener("click", () => {
        const open = more.getAttribute("aria-expanded") !== "true";
        rows.forEach((row, index) => {
          if (index >= CHANGES_VISIBLE) row.hidden = !open;
        });
        more.setAttribute("aria-expanded", open ? "true" : "false");
        more.textContent = open ? "收起" : `查看全部 ${rows.length} 项`;
      });
      container.appendChild(more);
    }
    renderRegion(region, "ready", container);
  } catch (error) {
    renderRegion(region, "error", error instanceof ApiError ? error.message : "变化加载失败。");
  }
}

function wireStockRunForm(form) {
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const submit = form.querySelector('button[type="submit"]');
    const formData = new FormData(form);
    const ticker = String(formData.get("ticker") || "");
    const industry = String(formData.get("industry") || "").trim();
    // Snapshot research falls back to the data industry group, so industry
    // is optional; only send it (with the fiscal year) when the user gave one.
    const body = industry
      ? { ticker, industry, report_year: new Date().getFullYear() - 1 }
      : { ticker };
    const promise = post("/research/single-stock", body, { idempotency: true })
      .then((accepted) => {
        const result = document.querySelector("#stock-run-result");
        if (result) {
          const container = el("div");
          container.appendChild(
            el(
              "p",
              { class: "notice" },
              `已提交研究运行 #${accepted.run_id}（${accepted.status}）；页面将在运行完成后展示结果。`
            )
          );
          renderRegion(result, "ready", container);
        }
      })
      .catch((error) => {
        const result = document.querySelector("#stock-run-result");
        if (result) renderRegion(result, "error", error.message);
      });
    if (submit) lockWhileBusy(submit, promise);
    await promise;
  });
}

// 分区标签：锚点链接 + 滚动时高亮当前分区；⋯ 菜单点外部收起。
function wireStockPage() {
  const tabs = [...document.querySelectorAll(".stock-tabs a[href^='#']")];
  if (tabs.length) {
    let frame = 0;
    const update = () => {
      frame = 0;
      const line = (document.querySelector(".stock-tabs")?.getBoundingClientRect().bottom ?? 0) + 24;
      let current = tabs[0];
      for (const tab of tabs) {
        const section = document.getElementById(tab.getAttribute("href").slice(1));
        if (!section || section.hidden || section.offsetParent === null) continue;
        if (section.getBoundingClientRect().top <= line) current = tab;
      }
      for (const tab of tabs) {
        const on = tab === current;
        tab.classList.toggle("on", on);
        if (on) tab.setAttribute("aria-current", "location");
        else tab.removeAttribute("aria-current");
      }
    };
    window.addEventListener("scroll", () => {
      if (!frame) frame = window.requestAnimationFrame(update);
    }, { passive: true });
    update();
  }
  const menu = document.querySelector(".stock-more");
  if (menu) {
    document.addEventListener("click", (event) => {
      if (menu.open && !menu.contains(event.target)) menu.open = false;
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && menu.open) menu.open = false;
    });
  }
  const head = document.querySelector("#stock-spark");
  const ticker = head && head.getAttribute("data-ticker");
  if (ticker) loadStockIdentity(ticker);
}

// ---- 启动 -------------------------------------------------------------

function refreshOverview() {
  const region = document.querySelector("#dashboard-today");
  if (region) loadRegion(region, { render: loadDashboard });
}

function boot() {
  for (const region of document.querySelectorAll(".data-region[data-endpoint]")) {
    const id = region.id;
    if (CANDIDATE_REGIONS.has(id) || TEMPLATE_REGIONS.has(id) || HEALTH_REGIONS.has(id) || SETTINGS_REGIONS.has(id)) continue;
    if (WATCHLIST_REGIONS.has(id)) continue;
    if (PORTFOLIO_REGIONS.has(id)) continue;
    if (id === "dashboard-today") loadRegion(region, { render: loadDashboard });
    else if (id === "runs-list") {
      const wanted = new URLSearchParams(globalThis.location.search).get("status") || "";
      const known = ["queued", "running", "success", "partial", "failed"].includes(wanted) ? wanted : "";
      setRunsFilter(known, { reload: false });
      loadRunsPage(region, 0, { status: known });
    }
    else if (id === "stock-history")
      loadRegion(region, { render: (node, data) => loadStockHistory(node, data).then((latest) => {
        const detailRegion = document.querySelector("#stock-detail");
        if (detailRegion) loadStockDetail(detailRegion, latest);
      }) });
    else loadRegion(region);
  }

  const stockRunForm = document.querySelector("#stock-run-form");
  if (stockRunForm) wireStockRunForm(stockRunForm);

  wireRunsPage();
  document.querySelector("#overview-refresh")?.addEventListener("click", refreshOverview);
  document.querySelector("#overview-refresh-m")?.addEventListener("click", refreshOverview);
  wireStockPage();
  loadStockSpark();

  bootWatchlist();
  bootPortfolio();
  bootAgentControls();
  bootAgentViews();
  bootAgentPortfolio();
  bootAgent();
  bootCandidates();
  bootTemplates();
  bootHealth();
  bootSettings();
  bootFinancials();
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", boot);
} else {
  boot();
}

export { loadDashboard, loadRuns, loadStockChanges, loadStockDetail, loadStockHistory, table };
