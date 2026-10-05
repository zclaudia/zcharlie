// 页面数据装配：按 data-region 声明加载数据并渲染基础表格/卡片。
// 页面逻辑只消费版本化 API，不在前端解释 score 语义。

import { download, get, loadRegion, lockWhileBusy, post, renderRegion } from "./api.js";

function el(tag, attributes = {}, text) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attributes)) {
    if (value !== undefined && value !== null) node.setAttribute(key, value);
  }
  if (text !== undefined) node.textContent = text;
  return node;
}

function cell(row, key) {
  return el("td", {}, row[key] === null || row[key] === undefined ? "—" : String(row[key]));
}

function table(headers, rows) {
  if (!rows.length) return null;
  const tableNode = el("table");
  const head = el("tr");
  for (const header of headers) head.appendChild(el("th", {}, header));
  tableNode.appendChild(el("thead")).appendChild(head);
  const body = el("tbody");
  for (const row of rows) {
    const tr = el("tr");
    for (const header of headers) tr.appendChild(cell(row, header.key || header));
    body.appendChild(tr);
  }
  tableNode.appendChild(body);
  return tableNode;
}

function statusText(value) {
  const span = el("span", { class: `status status-${value}` }, value);
  return span;
}

// ---- 今日概览 ---------------------------------------------------------

async function loadDashboard(region, data) {
  if (!data || (!data.running && !data.failed && !data.partial && !data.needs_review && !data.meaningful_changes)) {
    renderRegion(region, "empty");
    return;
  }
  const container = el("div");
  const sections = [
    ["运行中", data.running],
    ["失败", data.failed],
    ["部分成功", data.partial],
    ["待复核", data.needs_review],
    ["重要变化", data.meaningful_changes],
  ];
  for (const [title, entries] of sections) {
    if (!entries || !entries.length) continue;
    const card = el("div", { class: "card" });
    card.appendChild(el("h2", {}, `${title}（${entries.length}）`));
    const rows = entries.map((entry) => ({
      ticker: entry.ticker || entry.symbol || "—",
      name: entry.name || "",
      run_id: entry.run_id,
      status: entry.status || "",
    }));
    const tableNode = table(
      [
        { key: "ticker", label: "代码" },
        { key: "name", label: "名称" },
        { key: "run_id", label: "运行" },
        { key: "status", label: "状态" },
      ],
      rows
    );
    if (tableNode) card.appendChild(tableNode);
    container.appendChild(card);
  }
  renderRegion(region, container.children.length ? "ready" : "empty", container.children.length ? container : undefined);
}

// ---- 自选股 ----------------------------------------------------------

async function loadWatchlist(region, items) {
  if (!Array.isArray(items) || !items.length) {
    renderRegion(region, "empty");
    return;
  }
  const rows = items.map((item) => ({
    ticker: item.ticker,
    name: item.name,
    industry: item.industry,
    investment_score: item.latest_score ? item.latest_score.investment_score : null,
    research_confidence_score: item.latest_score ? item.latest_score.research_confidence_score : null,
    freshness: item.latest_score ? (item.latest_score.freshness_ok === false ? "stale" : "ok") : null,
    changed_at: item.changed_at,
    last_refresh_status: item.last_refresh_status,
  }));
  const tableNode = table(
    [
      { key: "ticker", label: "代码" },
      { key: "name", label: "名称" },
      { key: "industry", label: "行业" },
      { key: "investment_score", label: "投资分" },
      { key: "research_confidence_score", label: "置信度" },
      { key: "freshness", label: "freshness" },
      { key: "changed_at", label: "变化时间" },
      { key: "last_refresh_status", label: "刷新状态" },
    ],
    rows
  );
  renderRegion(region, tableNode ? "ready" : "empty", tableNode || undefined);
}

function wireWatchlistForm(form) {
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const submit = form.querySelector('button[type="submit"]');
    const formData = new FormData(form);
    const payload = {};
    for (const key of ["ticker", "industry", "name"]) {
      const value = String(formData.get(key) || "").trim();
      if (value) payload[key] = value;
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
        if (!results) return;
        const container = el("div");
        container.appendChild(
          el("p", {}, `已排队 ${data.created}，复用 ${data.reused_runs}，失败 ${data.failed}。`)
        );
        for (const item of data.items || []) {
          container.appendChild(
            el("p", { class: `status status-${item.status}` }, `${item.ticker}: ${item.status}`)
          );
        }
        renderRegion(results, "ready", container);
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

async function loadRuns(region, runs) {
  if (!Array.isArray(runs) || !runs.length) {
    renderRegion(region, "empty");
    return;
  }
  const rows = runs.map((run) => ({
    id: run.id,
    kind: run.kind,
    status: run.status,
    stage: run.stage,
    attempt_count: run.attempt_count,
    worker_id: run.worker_id,
    started_at: run.started_at,
    error_summary: run.error_summary,
  }));
  const tableNode = table(
    [
      { key: "id", label: "ID" },
      { key: "kind", label: "类型" },
      { key: "status", label: "状态" },
      { key: "stage", label: "阶段" },
      { key: "attempt_count", label: "尝试" },
      { key: "worker_id", label: "Worker" },
      { key: "started_at", label: "开始" },
      { key: "error_summary", label: "错误" },
    ],
    rows
  );
  renderRegion(region, tableNode ? "ready" : "empty", tableNode || undefined);
}

// ---- 单股详情 ---------------------------------------------------------

async function loadStockHistory(region, snapshots) {
  if (!Array.isArray(snapshots) || !snapshots.length) {
    renderRegion(region, "empty");
    return;
  }
  const rows = snapshots.map((snapshot) => ({
    run_id: snapshot.run_id,
    as_of: snapshot.as_of,
    strategy_label: snapshot.strategy_label,
    investment_score: snapshot.investment_score,
    research_confidence_score: snapshot.research_confidence_score,
    needs_review: snapshot.needs_review ? "needs_review" : "ok",
  }));
  const tableNode = table(
    [
      { key: "run_id", label: "运行" },
      { key: "as_of", label: "as_of" },
      { key: "strategy_label", label: "策略" },
      { key: "investment_score", label: "投资分" },
      { key: "research_confidence_score", label: "置信度" },
      { key: "needs_review", label: "复核" },
    ],
    rows
  );
  renderRegion(region, tableNode ? "ready" : "empty", tableNode || undefined);
}

// ---- 启动 -------------------------------------------------------------

function boot() {
  for (const region of document.querySelectorAll("[data-region][data-endpoint]")) {
    const id = region.id;
    if (id === "dashboard-today") loadRegion(region, { render: loadDashboard });
    else if (id === "watchlist-list") loadRegion(region, { render: loadWatchlist });
    else if (id === "runs-list") loadRegion(region, { render: loadRuns });
    else if (id === "stock-history") loadRegion(region, { render: loadStockHistory });
    else if (id === "stock-latest") loadRegion(region, { render: loadStockHistory });
    else loadRegion(region);
  }

  const addForm = document.querySelector("#watchlist-add-form");
  if (addForm) wireWatchlistForm(addForm);
  const refreshAll = document.querySelector("#watchlist-refresh-all");
  if (refreshAll) wireRefreshAll(refreshAll);

  const filter = document.querySelector("#runs-status-filter");
  if (filter) {
    filter.addEventListener("change", () => {
      const region = document.querySelector("#runs-list");
      if (!region) return;
      const status = filter.value;
      region.dataset.endpoint = status ? `/research/runs?status=${encodeURIComponent(status)}` : "/research/runs";
      loadRegion(region, { render: loadRuns });
    });
  }
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", boot);
} else {
  boot();
}

export { loadDashboard, loadRuns, loadStockHistory, loadWatchlist, table };
