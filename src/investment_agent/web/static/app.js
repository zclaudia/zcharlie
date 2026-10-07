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
import { CANDIDATE_REGIONS, bootCandidates, candidatesBucket } from "./candidates.js";
import { HEALTH_REGIONS, bootHealth, healthBucket } from "./health.js";
import { el, errorText, fmt, fmtNumber, kvTable, statusBadge, stockLink, table } from "./dom.js";
import {
  FRESHNESS_LABELS,
  QUALITY_FLAG_LABELS,
  RUN_STAGE_LABELS,
  METRIC_UNITS,
  PROVIDER_LABELS,
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
import { aiStamp, bar, ring } from "./ui.js";

// ---- 今日概览 ---------------------------------------------------------

function dashboardSection(title, entries) {
  const card = el("div", { class: "card" });
  card.appendChild(el("h2", {}, `${title}（${entries.length}）`));
  const rows = entries.map((entry) => {
    const tr = el("tr");
    const tickerCell = el("td");
    if (entry.ticker) {
      tickerCell.appendChild(stockLink(entry.ticker));
    } else {
      tickerCell.textContent = "—";
    }
    tr.appendChild(tickerCell);
    tr.appendChild(el("td", {}, fmt(entry.name)));
    tr.appendChild(el("td", {}, label(RUN_STAGE_LABELS, entry.stage || entry.status)));
    const summaryCell = el("td");
    summaryCell.appendChild(errorText(entry.summary));
    tr.appendChild(summaryCell);
    return tr;
  });
  card.appendChild(
    table(
      [
        { key: "ticker", label: "代码" },
        { key: "name", label: "名称" },
        { key: "stage", label: "阶段" },
        { key: "summary", label: "摘要" },
      ],
      rows
    )
  );
  return card;
}

async function loadDashboard(region, data) {
  const sections = [
    ["运行中", data.running],
    ["失败", data.failed],
    ["部分成功", data.partial],
    ["待复核", data.needs_review],
    ["重要变化", data.meaningful_changes],
  ].filter(([, bucket]) => bucket && bucket.count > 0);
  const warnings = Array.isArray(data.warnings) ? data.warnings : [];

  const candidates = data.candidates && typeof data.candidates === "object" ? data.candidates : null;
  const health = data.data_health && typeof data.data_health === "object" ? data.data_health : null;
  if (!sections.length && !warnings.length && !candidates && !health) {
    renderRegion(region, "empty", "今日无待处理项。");
    return;
  }
  const container = el("div");
  if (candidates) container.appendChild(candidatesBucket(candidates));
  if (health) container.appendChild(healthBucket(health));
  if (!sections.length && !warnings.length) {
    container.appendChild(el("p", { class: "notice" }, "今日无待处理的研究运行。"));
  }
  for (const warning of warnings) {
    const level = warning.level === "error" ? "error" : "warn";
    const notice = el("p", { class: `notice notice-${level}`, role: "alert" }, fmt(warning.message));
    if (typeof warning.link === "string" && warning.link.startsWith("/app/")) {
      notice.appendChild(document.createTextNode(" "));
      notice.appendChild(el("a", { href: warning.link }, "查看数据健康"));
    }
    container.appendChild(notice);
  }
  for (const [title, bucket] of sections) {
    container.appendChild(
      dashboardSection(title, bucket.entries)
    );
  }
  renderRegion(region, "ready", container);
}

// ---- 自选股 ----------------------------------------------------------

async function loadWatchlist(region, items) {
  if (!Array.isArray(items) || !items.length) {
    renderRegion(region, "empty");
    return;
  }
  const node = table(
    [
      { label: "代码" },
      { label: "名称" },
      { label: "行业" },
      { label: "投资分" },
      { label: "置信度" },
      { label: "数据新鲜度" },
      { label: "变化时间" },
      { label: "刷新状态" },
      { label: "操作" },
    ],
    items.map((item) => watchlistRow(region, item))
  );
  renderRegion(region, "ready", node);
}

function watchlistRow(region, item) {
  const tr = el("tr");
  const tickerCell = el("td");
  const latest = item.latest_research;
  if (latest && latest.run_id) {
    tickerCell.appendChild(stockLink(item.ticker));
  } else {
    tickerCell.appendChild(el("a", { href: `/app/stocks/${item.ticker}` }, item.ticker));
  }
  tr.appendChild(tickerCell);
  tr.appendChild(el("td", {}, fmt(item.name)));
  tr.appendChild(el("td", {}, fmt(item.industry)));
  tr.appendChild(el("td", {}, fmtNumber(latest && latest.investment_score, 1)));
  tr.appendChild(el("td", {}, fmtNumber(latest && latest.research_confidence_score, 1)));
  const freshnessCell = el("td");
  if (latest) {
    freshnessCell.appendChild(
      statusBadge(latest.freshness_ok === null || latest.freshness_ok === undefined ? "unknown" : latest.freshness_ok ? "ok" : "stale")
    );
  } else {
    freshnessCell.textContent = "—";
  }
  tr.appendChild(freshnessCell);
  tr.appendChild(el("td", {}, fmt(latest && latest.changed_at ? latest.changed_at.slice(0, 16).replace("T", " ") : "—")));
  const statusCell = el("td");
  statusCell.appendChild(statusBadge(item.last_refresh_status));
  tr.appendChild(statusCell);

  const actions = el("td", { class: "row-actions" });
  const toggle = el("button", { type: "button" }, item.enabled ? "停用" : "启用");
  toggle.addEventListener("click", () => {
    const promise = fetch(`/watchlist/${item.listing_id}`, {
      method: "PATCH",
      headers: { ...FETCH_MARKER_HEADERS, "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify({ enabled: !item.enabled }),
    }).then((response) => {
      if (response.ok) {
        loadRegion(region, { render: loadWatchlist });
      } else {
        renderRegion(region, "error", `更新失败（HTTP ${response.status}）。`);
      }
    });
    lockWhileBusy(toggle, promise);
  });
  actions.appendChild(toggle);
  const remove = el("button", { type: "button", class: "danger" }, "删除");
  remove.addEventListener("click", () => {
    if (!globalThis.confirm(`删除自选股 ${item.ticker}？`)) return;
    removeWatchlistItem(region, item, remove);
  });
  actions.appendChild(remove);
  tr.appendChild(actions);
  return tr;
}

async function removeWatchlistItem(region, item, button) {
  const promise = fetch(`/watchlist/${item.listing_id}`, {
    method: "DELETE",
    headers: { ...FETCH_MARKER_HEADERS },
    credentials: "same-origin",
  }).then((response) => {
    if (response.status === 204) {
      loadRegion(region, { render: loadWatchlist });
    } else {
      renderRegion(region, "error", `删除失败（HTTP ${response.status}）。`);
    }
  });
  lockWhileBusy(button, promise);
  await promise;
}

function wireWatchlistForm(form) {
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const submit = form.querySelector('button[type="submit"]');
    const formData = new FormData(form);
    const payload = {};
    for (const key of ["ticker", "industry", "name", "report_year"]) {
      const value = String(formData.get(key) || "").trim();
      if (value) payload[key] = key === "report_year" ? Number(value) : value;
    }
    const promise = post("/watchlist", payload, { idempotency: false })
      .then(() => {
        form.reset();
        const list = document.querySelector("#watchlist-list");
        if (list) loadRegion(list, { render: loadWatchlist });
      })
      .catch((error) => {
        const results = document.querySelector("#watchlist-refresh-results");
        if (results) renderRegion(results, "error", error.message);
      });
    if (submit) lockWhileBusy(submit, promise);
    await promise;
  });
}

function wireRefreshAll(button) {
  button.addEventListener("click", () => {
    const promise = post("/watchlist/refresh", {}, { idempotency: true })
      .then((data) => {
        const results = document.querySelector("#watchlist-refresh-results");
        if (results) {
          const container = el("div");
          container.appendChild(
            el(
              "p",
              {},
              `共 ${data.total} 只：新排队 ${data.created}，复用运行 ${data.reused_runs}，失败 ${data.failed}。逐项状态：`
            )
          );
          for (const item of data.items || []) {
            container.appendChild(
              el("p", { class: `status status-${item.status}` }, `${item.ticker}：${label(STATUS_LABELS, item.status)}`)
            );
          }
          renderRegion(results, "ready", container);
        }
        const list = document.querySelector("#watchlist-list");
        if (list) loadRegion(list, { render: loadWatchlist });
      })
      .catch((error) => {
        const results = document.querySelector("#watchlist-refresh-results");
        if (results) renderRegion(results, "error", error.message);
      });
    lockWhileBusy(button, promise);
  });
}

// ---- 运行中心 ---------------------------------------------------------

const RUNS_PAGE_SIZE = 20;

function runsQuery(offset, status) {
  const params = new URLSearchParams({ limit: String(RUNS_PAGE_SIZE), offset: String(offset) });
  if (status) params.set("status", status);
  return `/research/runs?${params.toString()}`;
}

function renderRunsTable(region, runs) {
  const node = table(
    [
      { label: "ID" },
      { label: "状态" },
      { label: "阶段" },
      { label: "尝试" },
      { label: "开始时间" },
      { label: "错误摘要" },
      { label: "详情" },
    ],
    runs.map((run) => runRow(run))
  );
  const container = el("div");
  container.appendChild(node);
  const loadMore = el("button", { type: "button", class: "secondary load-more" }, "加载更多");
  loadMore.addEventListener("click", () => {
    const current = region.querySelectorAll("tbody tr").length;
    const filter = document.querySelector("#runs-status-filter");
    loadRunsPage(region, current, { append: true, status: filter && filter.value });
  });
  container.appendChild(loadMore);
  return container;
}

async function loadRuns(region, runs, { append = false } = {}) {
  if (!append) {
    if (!Array.isArray(runs) || !runs.length) {
      renderRegion(region, "empty");
      return;
    }
    renderRegion(region, "ready", renderRunsTable(region, runs));
    return;
  }
  if (!Array.isArray(runs) || !runs.length) {
    region.querySelector(".load-more")?.remove();
    return;
  }
  const existingTable = region.querySelector("table");
  const body = existingTable && existingTable.querySelector("tbody");
  if (!body) {
    renderRegion(region, "ready", renderRunsTable(region, runs));
    return;
  }
  const loadMore = region.querySelector(".load-more");
  const labels = [...existingTable.querySelectorAll("thead th")].map((th) => th.textContent);
  for (const row of runs.map((run) => runRow(run))) {
    [...row.children].forEach((cell, index) => labels[index] && cell.setAttribute("data-label", labels[index]));
    body.appendChild(row);
  }
  if (loadMore) {
    const fresh = el("button", { type: "button", class: "secondary load-more" }, "加载更多");
    fresh.addEventListener("click", () => {
      const current = region.querySelectorAll("tbody tr").length;
      const filter = document.querySelector("#runs-status-filter");
      loadRunsPage(region, current, { append: true, status: filter && filter.value });
    });
    loadMore.replaceWith(fresh);
  }
}

function runRow(run) {
  const tr = el("tr");
  tr.appendChild(el("td", {}, String(run.run_id)));
  const statusCell = el("td");
  statusCell.appendChild(statusBadge(run.status));
  tr.appendChild(statusCell);
  tr.appendChild(el("td", {}, label(RUN_STAGE_LABELS, run.stage)));
  tr.appendChild(el("td", {}, `${run.attempt_count}/${run.max_attempts}`));
  tr.appendChild(
    el("td", {}, run.started_at ? run.started_at.slice(0, 19).replace("T", " ") : "—")
  );
  const errorCell = el("td", { class: "error-cell" });
  errorCell.appendChild(errorText(run.error_summary));
  tr.appendChild(errorCell);

  const detailCell = el("td");
  const details = el("details");
  details.appendChild(el("summary", {}, "详情"));
  const detailBody = el("div", { class: "run-detail" });
  detailBody.appendChild(el("p", { class: "muted" }, "加载详情…"));
  details.addEventListener("toggle", () => {
    if (details.open && !detailBody.dataset.loaded) loadRunDetail(detailBody, run.run_id);
  });
  details.appendChild(detailBody);
  detailCell.appendChild(details);
  tr.appendChild(detailCell);
  return tr;
}

async function loadRunDetail(container, runId) {
  try {
    const detail = await get(`/research/runs/${runId}`);
    container.replaceChildren();
    container.appendChild(
      kvTable([
        ["股票", detail.ticker],
        ["执行进程", detail.worker_id],
        ["开始时间", formatDateTime(detail.started_at)],
        ["结束时间", formatDateTime(detail.finished_at)],
        ["错误", detail.error_summary ? errorText(detail.error_summary).textContent : null],
        ["工作流标签", detail.workflow_label],
      ])
    );
    const artifacts =
      detail.result && Array.isArray(detail.result.artifacts) ? detail.result.artifacts : [];
    if (artifacts.length) {
      container.appendChild(el("h3", {}, "制品"));
      for (const artifact of artifacts) {
        const link = el("a", { href: artifact.download_url }, artifact.name);
        link.addEventListener("click", (event) => {
          event.preventDefault();
          download(artifact.download_url);
        });
        container.appendChild(link);
        container.appendChild(document.createTextNode(" "));
      }
    }
    container.dataset.loaded = "true";
  } catch (error) {
    container.replaceChildren();
    container.appendChild(
      el(
        "p",
        { class: "notice notice-error" },
        error instanceof ApiError ? error.message : "详情加载失败。"
      )
    );
  }
}

function loadRunsPage(region, offset, { append = false, status } = {}) {
  const promise = get(runsQuery(offset, status || ""))
    .then((runs) => loadRuns(region, runs, { append }))
    .catch((error) => renderRegion(region, "error", error.message));
  return promise;
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

// 分项按分值排序；最低两项且低于 60 标为"主要拖累"（只描述分项相对位置）。
function rankedFactors(item) {
  const factors = item.factor_scores || {};
  const keys = Object.keys(factors).sort((a, b) => (factors[b] ?? -1) - (factors[a] ?? -1));
  const drags = new Set(keys.filter((key) => typeof factors[key] === "number" && factors[key] < 60).slice(-2));
  return { factors, keys, drags };
}

function verdictCard(item, scoreMeta, detail) {
  const card = el("section", { class: "card", "data-evidence-section": "evidence-score" });
  const head = el("div", { class: "verdict-head" });
  head.appendChild(el("h2", {}, "研究结论"));
  if (item.strategy_label) head.appendChild(el("span", { class: "badge accent" }, item.strategy_label));
  if (item.suggestion_label || item.suggestion) {
    head.appendChild(el("span", { class: "badge" }, item.suggestion_label || humanizeText(item.suggestion)));
  }
  if (item.needs_agent_review) head.appendChild(el("span", { class: "badge warn" }, "需人工复核"));
  const run = el("span", { class: "faint" }, `运行 #${detail.run_id} · `);
  run.appendChild(statusBadge(detail.status));
  head.appendChild(run);
  card.appendChild(head);

  const body = el("div", { class: "verdict" });
  body.appendChild(ring(item.investment_score, 100, { caption: "投资分 / 100" }));
  const facts = el("dl", { class: "facts" });
  facts.appendChild(fact("研究置信度", fmtNumber(item.research_confidence_score, 0)));
  facts.appendChild(fact("风险等级", label(RISK_LABELS, item.risk_level)));
  facts.appendChild(fact("估值水平", label(VALUATION_LABELS, item.valuation_level)));
  facts.appendChild(fact("业务分", fmtNumber(item.business_score, 1)));
  facts.appendChild(
    fact("数据质量", typeof item.data_quality_score === "number" ? `${fmtNumber(item.data_quality_score * 100, 0)}%` : "—")
  );
  facts.appendChild(fact("评分版本", (scoreMeta && scoreMeta.scoring_version) || item.scoring_version));
  body.appendChild(facts);

  const { factors, keys, drags } = rankedFactors(item);
  if (keys.length) {
    const line = el("p", { class: "verdict-line" });
    const best = keys[0];
    line.appendChild(document.createTextNode("最强分项："));
    line.appendChild(el("b", {}, `${scoreFactorLabel(best)} ${fmtNumber(factors[best], 1)}`));
    if (drags.size) {
      line.appendChild(document.createTextNode("；主要拖累："));
      line.appendChild(
        el("b", {}, [...drags].map((key) => `${scoreFactorLabel(key)} ${fmtNumber(factors[key], 1)}`).join("、"))
      );
    }
    line.appendChild(document.createTextNode("。"));
    body.appendChild(line);
  }
  card.appendChild(body);
  return card;
}

function factorCard(item) {
  const card = el("section", { class: "card", "data-evidence-section": "evidence-factors" });
  card.appendChild(el("h2", {}, "分项评分"));
  const { factors, keys, drags } = rankedFactors(item);
  if (!keys.length) {
    card.appendChild(el("p", { class: "muted" }, "无分项评分。"));
    return card;
  }
  const list = el("ul", { class: "score-bars" });
  for (const key of keys) {
    const value = factors[key];
    const row = el("li", { class: `score-bar${drags.has(key) ? " drag" : ""}`, title: key });
    row.appendChild(el("span", { class: "score-bar-label" }, scoreFactorLabel(key)));
    row.appendChild(bar(value));
    row.appendChild(el("span", { class: "score-bar-value" }, fmtNumber(value, 1)));
    if (drags.has(key)) row.appendChild(el("span", { class: "score-bar-tag" }, "主要拖累"));
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
  const card = el("section", { class: "card", "data-evidence-section": "evidence-metrics" });
  const head = el("div", { class: "card-head" });
  head.appendChild(el("h2", {}, "关键指标"));
  head.appendChild(el("span", { class: "faint" }, "徽标：已核对 = iFinD 核对；未核对 = 免费源；派生 = 由快照计算"));
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
  const card = el("section", { class: "card", "data-evidence-section": "evidence-quality" });
  card.appendChild(el("h2", {}, "数据新鲜度与质量"));
  if (item.needs_agent_review) {
    card.appendChild(el("p", { class: "notice notice-warn" }, "该标的已标记为需要人工复核。"));
  }
  const freshness = item.freshness || {};
  const list = el("ul", { class: "fresh-list" });
  for (const [name, block] of Object.entries(freshness)) {
    const row = el("li");
    const stale = block && typeof block === "object" ? Boolean(block.is_stale) : false;
    row.appendChild(el("span", { class: `dot ${stale ? "dot-stale" : "dot-fresh"}`, "aria-hidden": "true" }));
    row.appendChild(el("span", { class: "fresh-what" }, FRESHNESS_LABELS[name] || name));
    row.appendChild(el("span", {}, stale ? "已过期" : "正常"));
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

function explanationCard(detail, scoreItem, detailRegion) {
  const card = el("section", {
    class: "card explanation-card",
    "data-evidence-section": "evidence-explanation",
  });
  const head = el("div", { class: "card-head" });
  head.appendChild(el("h2", {}, "AI 解释"));
  head.appendChild(aiStamp("LLM 生成 · 只复述上方确定性结果"));
  card.appendChild(head);
  const explanation = detail.result && detail.result.explanation;
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
    return card;
  }
  if (explanation.summary) card.appendChild(el("p", { class: "explain-summary" }, humanizeText(explanation.summary)));
  const broken = new Set();
  const groups = el("div", { class: "ev-groups" });
  for (const [key, title, kind] of EXPLANATION_GROUPS) {
    const claims = explanation[key];
    if (!Array.isArray(claims) || !claims.length) continue;
    const group = el("div", { class: `ev-group ${kind}` });
    group.appendChild(el("h3", {}, title));
    const list = el("ul");
    for (const claim of claims) {
      const li = el("li", {}, humanizeText(claim.text));
      const chips = el("span", { class: "field-chips" });
      for (const field of claim.supporting_fields || []) {
        const resolved = resolveEvidence(scoreItem, field);
        if (!resolved) broken.add(field);
        const value =
          resolved && resolved.value !== null && (typeof resolved.value !== "object" || Array.isArray(resolved.value))
            ? ` ${formatPathValue(field, resolved.value)}`
            : "";
        const chip = el(
          "button",
          { type: "button", class: "evidence-chip", "data-field-path": field, title: field, "aria-pressed": "false" },
          `${pathLabel(field)}${value}`
        );
        chip.addEventListener("click", () => {
          for (const other of detailRegion.querySelectorAll(".evidence-chip[aria-pressed='true']")) {
            other.setAttribute("aria-pressed", "false");
          }
          chip.setAttribute("aria-pressed", "true");
          showEvidence(detailRegion, scoreItem, field);
        });
        chips.appendChild(chip);
      }
      li.appendChild(chips);
      list.appendChild(li);
    }
    group.appendChild(list);
    groups.appendChild(group);
  }
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
  if (Array.isArray(explanation.open_questions) && explanation.open_questions.length) {
    card.appendChild(el("h3", {}, "待核查问题"));
    const list = el("ul", { class: "open-questions" });
    for (const question of explanation.open_questions) list.appendChild(el("li", {}, humanizeText(question)));
    card.appendChild(list);
  }
  if (explanation.workflow_conclusion) {
    card.appendChild(el("p", { class: "faint" }, `工作流结论：${humanizeText(explanation.workflow_conclusion)}`));
  }
  return card;
}

async function loadStockHistory(region, snapshots) {
  if (!Array.isArray(snapshots) || !snapshots.length) {
    renderRegion(region, "empty");
    return null;
  }
  const rows = snapshots.map((snapshot) => {
    const tr = el("tr");
    const runCell = el("td");
    runCell.appendChild(el("a", { href: `/app/runs` }, String(snapshot.run_id)));
    tr.appendChild(runCell);
    tr.appendChild(el("td", {}, fmt(snapshot.as_of)));
    tr.appendChild(el("td", {}, fmt(snapshot.strategy_label)));
    tr.appendChild(el("td", {}, fmtNumber(snapshot.investment_score, 2)));
    tr.appendChild(el("td", {}, fmtNumber(snapshot.research_confidence_score, 1)));
    const reviewCell = el("td");
    reviewCell.appendChild(statusBadge(snapshot.needs_agent_review ? "needs_review" : "ok"));
    tr.appendChild(reviewCell);
    return tr;
  });
  renderRegion(region, "ready", table(
    [
      { label: "运行" },
      { label: "数据日期" },
      { label: "策略" },
      { label: "投资分" },
      { label: "置信度" },
      { label: "复核" },
    ],
    rows
  ));
  return snapshots[0];
}

async function loadStockDetail(detailRegion, latestSnapshot) {
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
    if (!scoreItem) {
      detailRegion.appendChild(
        el("p", { class: "notice notice-warn" }, "运行尚未产生确定性评分。")
      );
      return;
    }
    detailRegion.appendChild(verdictCard(scoreItem, detail.result.score.metadata, detail));
    const pair = el("div", { class: "grid-2" });
    pair.appendChild(factorCard(scoreItem));
    pair.appendChild(qualityCard(scoreItem));
    detailRegion.appendChild(pair);
    detailRegion.appendChild(metricsCard(scoreItem));
    const inspector = el("aside", {
      id: "evidence-inspector",
      class: "card evidence-inspector",
      hidden: "",
    });
    detailRegion.appendChild(inspector);
    detailRegion.appendChild(explanationCard(detail, scoreItem, detailRegion));
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

async function loadStockChanges(region, runId) {
  try {
    const changes = await get(`/research/runs/${runId}/changes`);
    if (!changes || (!changes.changes || !changes.changes.length)) {
      renderRegion(region, "empty", changes && changes.baseline ? "这是首次研究（baseline），暂无对比。" : "与上次快照无重要变化。");
      return;
    }
    const container = el("div");
    container.appendChild(
      el(
        "p",
        {},
        `对比运行 #${changes.previous_run_id ?? "—"} → #${changes.current_run_id}；重要变化 ${changes.meaningful_change_count}/${changes.change_count}。`
      )
    );
    const list = el("ul");
    for (const change of changes.changes) {
      list.appendChild(
        el(
          "li",
          {},
          `${pathLabel(change.field || change.path) || "字段"}：${formatPathValue(change.field || change.path, change.before)} → ${formatPathValue(change.field || change.path, change.after)}${change.meaningful ? "（重要）" : ""}`
        )
      );
    }
    container.appendChild(list);
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
    if (!industry) {
      const result = document.querySelector("#stock-run-result");
      if (result) renderRegion(result, "error", "行业为必填项。");
      return;
    }
    const promise = post(
      "/research/single-stock",
      { ticker, industry, report_year: new Date().getFullYear() - 1 },
      { idempotency: true }
    )
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

// ---- 启动 -------------------------------------------------------------

function boot() {
  for (const region of document.querySelectorAll(".data-region[data-endpoint]")) {
    const id = region.id;
    if (CANDIDATE_REGIONS.has(id) || HEALTH_REGIONS.has(id)) continue;
    if (id === "dashboard-today") loadRegion(region, { render: loadDashboard });
    else if (id === "watchlist-list") loadRegion(region, { render: loadWatchlist });
    else if (id === "runs-list") loadRunsPage(region, 0);
    else if (id === "stock-history")
      loadRegion(region, { render: (node, data) => loadStockHistory(node, data).then((latest) => {
        const detailRegion = document.querySelector("#stock-detail");
        if (detailRegion && latest) loadStockDetail(detailRegion, latest);
      }) });
    else loadRegion(region);
  }

  const addForm = document.querySelector("#watchlist-add-form");
  if (addForm) wireWatchlistForm(addForm);
  const refreshAll = document.querySelector("#watchlist-refresh-all");
  if (refreshAll) wireRefreshAll(refreshAll);
  const stockRunForm = document.querySelector("#stock-run-form");
  if (stockRunForm) wireStockRunForm(stockRunForm);

  const filter = document.querySelector("#runs-status-filter");
  if (filter) {
    filter.addEventListener("change", () => loadRunsPage(document.querySelector("#runs-list"), 0));
  }

  bootCandidates();
  bootHealth();
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", boot);
} else {
  boot();
}

export { loadDashboard, loadRuns, loadStockChanges, loadStockDetail, loadStockHistory, loadWatchlist, table };
