import { ApiError, get, lockWhileBusy, post, renderRegion } from "./api.js";
import { searchStocks } from "./combobox.js";
import { el, fmt, fmtNumber, kvTable, statusBadge, table } from "./dom.js";
import {
  FRESHNESS_LABELS,
  METRIC_UNITS,
  PROVIDER_LABELS,
  QUALITY_FLAG_LABELS,
  SOURCE_VERIFICATION_LABELS,
  STATUS_LABELS,
  fieldLabel,
  formatDateTime,
  formatFactorValue,
  label,
} from "./format.js";
import {
  RISK_LABELS,
  VALUATION_LABELS,
  formatPathValue,
  humanizeText,
  pathLabel,
  scoreFactorLabel,
} from "./research_format.js";
import { aiStamp, ring, thinBar } from "./ui.js";
import { loadBars } from "./market.js";
import { PRICE_RANGES, mountPriceChart } from "./pricechart.js";
import { icon, isCompactViewport } from "./page_widgets.js";
import { mountDividends } from "./dividends.js";
import { mountReviewCard } from "./review_panel.js";

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

export async function loadStockHistory(region, snapshots) {
  const tabCount = document.querySelector('[data-tab-count="history"]');
  if (tabCount) tabCount.textContent = Array.isArray(snapshots) && snapshots.length ? String(snapshots.length) : "";
  if (!Array.isArray(snapshots) || !snapshots.length) {
    renderRegion(region, "empty");
    return null;
  }
  const rows = snapshots.map((snapshot) => {
    const tr = el("tr");
    const runCell = el("td", { class: "hist-run" });
    const ticker = document.querySelector("#stock-detail")?.dataset.ticker || "";
    runCell.appendChild(el("a", { href: `/app/stocks/${encodeURIComponent(ticker)}?run_id=${encodeURIComponent(snapshot.run_id)}` }, `#${snapshot.run_id}`));
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
const STOCK_RANGE_DAYS = [7, 30, 60, 120, 240, 480, 1000];
const STOCK_RANGE_DAYS_COMPACT = [7, 60, 240, 480, 1000];

export function loadStockSpark() {
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

// 多视角评审卡片：放在 AI 解释之后，由 review_panel.js 自行加载轮次。
function mountStockReview(detailRegion) {
  const listingId = Number(detailRegion.dataset.listingId || 0);
  if (!listingId) return;
  mountReviewCard(detailRegion, {
    listingId,
    ticker: detailRegion.dataset.ticker || "",
  });
}

export function selectedStockRun() {
  const params = new URLSearchParams(globalThis.location?.search || "");
  if (!params.has("run_id")) return null;
  const value = params.get("run_id");
  return /^[1-9]\d*$/.test(value || "") && Number.isSafeInteger(Number(value)) ? Number(value) : 0;
}

function publishSelectedStockRun(detail) {
  globalThis.stockHistoricalRun = detail;
  document.dispatchEvent(new CustomEvent("stock-historical-run", { detail }));
}

export async function loadStockDetail(detailRegion, latestSnapshot, { historical = false } = {}) {
  loadStockSpark();
  if (!latestSnapshot) {
    detailRegion.hidden = true;
    return;
  }
  try {
    if (historical && !latestSnapshot.run_id) throw new Error("历史运行编号无效。");
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
    if (historical && (!detail.result || detail.result.ticker !== detailRegion.dataset.ticker || !scoreItem)) {
      throw new Error("该运行不属于这只股票，或没有保存的评分。");
    }
    if (historical) {
      const banner = el("p", { class: "notice", id: "stock-historical-banner" }, `正在查看历史研究 #${detail.run_id} · 数据日期 ${fmt(detail.result.as_of)}。`);
      banner.appendChild(el("a", { href: `/app/stocks/${encodeURIComponent(detailRegion.dataset.ticker)}` }, "返回最新研究"));
      detailRegion.appendChild(banner);
      publishSelectedStockRun(detail);
    }
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
      mountStockReview(detailRegion);
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
    if (!historical) mountStockReview(detailRegion);
    detailRegion.appendChild(qualityCard(scoreItem));
    const changesRegion = document.querySelector("#stock-changes");
    if (changesRegion) await loadStockChanges(changesRegion, detail.run_id);
  } catch (error) {
    detailRegion.hidden = false;
    detailRegion.replaceChildren();
    if (historical) publishSelectedStockRun(null);
    const message = historical
      ? (error instanceof ApiError ? "无法读取指定的历史研究。" : error.message || "无法读取指定的历史研究。")
      : (error instanceof ApiError ? error.message : "详情加载失败。");
    const notice = el("p", { class: "notice notice-error" }, message);
    if (historical) notice.appendChild(el("a", { href: `/app/stocks/${encodeURIComponent(detailRegion.dataset.ticker)}` }, "返回最新研究"));
    detailRegion.appendChild(notice);
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

export async function loadStockChanges(region, runId) {
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

export function wireStockRunForm(form) {
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
export function wireStockPage() {
  mountDividends();
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

