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
  for (const row of runs.map((run) => runRow(run))) body.appendChild(row);
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

function scoreCard(item, scoreMeta) {
  const card = el("div", { class: "card", "data-evidence-section": "evidence-score" });
  card.appendChild(el("h2", {}, "确定性评分"));
  card.appendChild(
    kvTable([
      ["策略分类", item.strategy_label],
      ["投资分", `${fmtNumber(item.investment_score, 1)} / 100`],
      ["业务分", `${fmtNumber(item.business_score, 1)} / 100`],
      ["研究置信度", fmtNumber(item.research_confidence_score, 0)],
      ["风险等级", label(RISK_LABELS, item.risk_level)],
      ["估值水平", label(VALUATION_LABELS, item.valuation_level)],
      ["工作流标签", item.suggestion_label || item.suggestion],
      ["数据质量", typeof item.data_quality_score === "number" ? `${fmtNumber(item.data_quality_score * 100, 0)}%` : "—"],
      ["评分版本", (scoreMeta && scoreMeta.scoring_version) || item.scoring_version],
    ])
  );
  return card;
}

function factorCard(item) {
  const factors = item.factor_scores || {};
  const card = el("div", { class: "card", "data-evidence-section": "evidence-factors" });
  card.appendChild(el("h2", {}, "分项评分"));
  const keys = Object.keys(factors).sort((a, b) => (factors[b] ?? -1) - (factors[a] ?? -1));
  if (!keys.length) {
    card.appendChild(el("p", { class: "muted" }, "无分项评分。"));
    return card;
  }
  const list = el("ul", { class: "score-bars" });
  for (const key of keys) {
    const value = factors[key];
    const item = el("li", { title: key });
    item.appendChild(el("span", { class: "score-bar-label" }, scoreFactorLabel(key)));
    item.appendChild(
      el("meter", {
        class: "score-bar-meter",
        min: 0,
        max: 100,
        value: typeof value === "number" ? Math.max(0, Math.min(100, value)) : 0,
        "aria-label": scoreFactorLabel(key),
      })
    );
    item.appendChild(el("span", { class: "score-bar-value" }, fmtNumber(value, 1)));
    list.appendChild(item);
  }
  card.appendChild(list);
  return card;
}

function metricsCard(item) {
  const metrics = item.key_metrics || {};
  const card = el("div", { class: "card", "data-evidence-section": "evidence-metrics" });
  card.appendChild(el("h2", {}, "关键指标"));
  const rows = Object.entries(metrics).map(([key, value]) => {
    const tr = el("tr");
    tr.appendChild(el("td", { title: key }, fieldLabel(key)));
    tr.appendChild(el("td", { class: "num" }, formatFactorValue(value, METRIC_UNITS[key])));
    return tr;
  });
  card.appendChild(table([{ label: "指标" }, { label: "数值" }], rows));
  return card;
}


// Freshness is judged per data kind: market data in exchange trading days,
// financial data by report period, the rest by hours / days.
function describeFreshness(block) {
  const status = block.is_stale ? "过期" : "正常";
  if (block.age_trading_days !== undefined && block.age_trading_days !== null) {
    return `${status}（${block.age_trading_days} 个交易日 / 上限 ${block.max_age_trading_days ?? block.max_age_days} 个交易日）`;
  }
  if (block.basis === "report_period") {
    const period = block.report_period_end || block.report_year || "未知";
    const expected = block.expected_report_year ? `，应有 ${block.expected_report_year} 年报` : "";
    return `${status}（报告期 ${period}${expected}）`;
  }
  const hours = Number(block.age_hours);
  const age = Number.isFinite(hours) && hours >= 48 ? `${fmtNumber(hours / 24, 1)} 天` : `${fmtNumber(block.age_hours, 1)} 小时`;
  return `${status}（${age} / 上限 ${block.max_age_days} 天）`;
}

function qualityCard(item) {
  const card = el("div", { class: "card", "data-evidence-section": "evidence-quality" });
  card.appendChild(el("h2", {}, "数据质量"));
  const missing = Array.isArray(item.missing_fields) ? item.missing_fields : [];
  if (item.needs_agent_review) {
    card.appendChild(el("p", { class: "notice notice-warn" }, "该标的已标记为需要人工复核。"));
  }
  card.appendChild(kvTable([["缺失字段", missing.length ? missing.map(fieldLabel).join("、") : "无"]]));
  const flags = Array.isArray(item.quality_flags) ? item.quality_flags : [];
  // A field the selected model does not use is "not applicable", not missing:
  // show that once instead of the data layer's missing-field note.
  const notApplicable = new Set(
    flags.filter((flag) => flag.flag === "model_not_applicable").map((flag) => flag.field)
  );
  const reviewLines = [];
  for (const field of notApplicable) {
    reviewLines.push(`${fieldLabel(field)}：不适用于当前模型（不参与评分）`);
  }
  for (const flag of flags) {
    if (flag.severity !== "review" || notApplicable.has(flag.field)) continue;
    reviewLines.push(`${fieldLabel(flag.field)}：${label(QUALITY_FLAG_LABELS, flag.flag) || flag.message || "需复核"}`);
  }
  if (reviewLines.length) {
    const list = el("ul");
    for (const line of reviewLines) list.appendChild(el("li", {}, line));
    card.appendChild(list);
  }
  const freshness = item.freshness || {};
  const freshnessPairs = Object.entries(freshness).map(([name, block]) => [
    FRESHNESS_LABELS[name] || name,
    block && typeof block === "object" ? describeFreshness(block) : fmt(block),
  ]);
  if (freshnessPairs.length) {
    card.appendChild(el("h3", {}, "数据新鲜度"));
    card.appendChild(kvTable(freshnessPairs));
  }
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
  const sources = scoreItem.field_sources && typeof scoreItem.field_sources === "object" ? scoreItem.field_sources : {};
  const provenance = sources[leaf] || sources[path] || null;
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

function explanationCard(detail, scoreItem, detailRegion) {
  const card = el("div", {
    class: "card explanation-card",
    "data-evidence-section": "evidence-explanation",
  });
  card.appendChild(el("h2", {}, "AI 解释"));
  card.appendChild(el("p", { class: "muted small" }, "由 LLM 根据上方确定性结果生成；点击引用标签可查看证据来源。"));
  const explanation = detail.result && detail.result.explanation;
  if (!explanation) {
    card.appendChild(
      el(
        "p",
        { class: "notice notice-warn" },
        detail.status === "partial"
          ? "解释不可用：评分成功但解释生成失败（partial）。以上确定性结果仍然有效。"
          : "解释不可用。"
      )
    );
    return card;
  }
  card.appendChild(el("p", { class: "explain-summary" }, humanizeText(explanation.summary || "")));
  const broken = new Set();
  const groups = [
    ["分类依据", explanation.classification_rationale],
    ["正面证据", explanation.positive_evidence],
    ["负面证据", explanation.negative_evidence],
    ["风险因素", explanation.risk_factors],
    ["数据质量说明", explanation.data_quality_notes],
  ];
  for (const [title, claims] of groups) {
    if (!Array.isArray(claims) || !claims.length) continue;
    card.appendChild(el("h3", {}, title));
    const list = el("ul");
    for (const claim of claims) {
      const li = el("li", {}, humanizeText(claim.text));
      const chips = el("span", { class: "field-chips" });
      for (const field of claim.supporting_fields || []) {
        const resolved = resolveEvidence(scoreItem, field);
        if (!resolved) broken.add(field);
        const value =
          resolved && (typeof resolved.value === "number" || typeof resolved.value === "string" || typeof resolved.value === "boolean" || Array.isArray(resolved.value))
            ? ` ${formatPathValue(field, resolved.value)}`
            : "";
        const chip = el(
          "button",
          { type: "button", class: "evidence-chip", "data-field-path": field, title: field },
          `${pathLabel(field)}${value}`
        );
        chip.addEventListener("click", () => showEvidence(detailRegion, scoreItem, field));
        chips.appendChild(chip);
      }
      li.appendChild(chips);
      list.appendChild(li);
    }
    card.appendChild(list);
  }
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
    const list = el("ul");
    for (const question of explanation.open_questions) list.appendChild(el("li", {}, humanizeText(question)));
    card.appendChild(list);
  }
  if (explanation.workflow_conclusion) {
    card.appendChild(el("p", {}, `工作流结论：${humanizeText(explanation.workflow_conclusion)}`));
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
    detailRegion.appendChild(el("h2", {}, `最新运行 #${detail.run_id}（${label(STATUS_LABELS, detail.status)}）`));
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
    detailRegion.appendChild(
      scoreCard(scoreItem, detail.result.score.metadata)
    );
    detailRegion.appendChild(factorCard(scoreItem));
    detailRegion.appendChild(metricsCard(scoreItem));
    detailRegion.appendChild(qualityCard(scoreItem));
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
