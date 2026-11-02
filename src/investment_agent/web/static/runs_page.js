import { ApiError, download, get, renderRegion } from "./api.js";
import { el, errorText, fmt, fmtNumber, statusBadge, table } from "./dom.js";
import {
  RUN_KIND_LABELS,
  RUN_STAGE_LABELS,
  STATUS_LABELS,
  formatDateTime,
  label,
  summarizeError,
} from "./format.js";
import { stockIdent } from "./ui.js";
import { icon, shortStamp } from "./page_widgets.js";

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

export async function loadRuns(region, runs, { append = false } = {}) {
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

export function loadRunsPage(region, offset, { append = false, status } = {}) {
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

export function setRunsFilter(status, { reload = true } = {}) {
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

export function wireRunsPage() {
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

