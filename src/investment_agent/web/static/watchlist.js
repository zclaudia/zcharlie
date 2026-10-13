// 观察池页面：跟踪列表、规则状态与审阅、手动添加、批量刷新。
// 规则收口契约见 docs/product/watch-holding-plan.md 1.5：
// 手动添加可选模板并自动起草；卡片区分起草中 / 待批准 / 失败 / 驳回；
// 审批（批准 / 改参数 / 驳回）复用规则版本，不创建候选、不触发晋级。

import {
  ApiError,
  get,
  loadRegion,
  lockWhileBusy,
  patch,
  post,
  renderRegion,
} from "./api.js";
import { attachCombobox } from "./combobox.js";
import { el, errorText, fmt, fmtNumber, statusBadge, table } from "./dom.js";
import { CALIBRATION_LABELS, ZONE_LABELS, formatDateTime, label } from "./format.js";
import {
  eventsSummary,
  paramEditor as sharedParamEditor,
  ruleFeedback,
  zoneEventLine,
  zonesBlock,
} from "./rule_review.js";
import { fillSparklines } from "./ui.js";

export const WATCHLIST_REGIONS = new Set(["watchlist-list"]);

// 日线迷你图（B3）：占位先渲染，再按行异步替换；只画收盘价，不评判。
const SPARK_DAYS = 120;

export function loadBars(ticker, days) {
  return get(`/market/${encodeURIComponent(ticker)}/bars?days=${days}`);
}

function sparkSlot(ticker, cls = "spark-slot") {
  return el("span", { class: `spark spark-pending ${cls}`.trim(), "data-spark": ticker, "aria-hidden": "true" });
}

function mountSparklines(root, days, options) {
  const targets = root.querySelectorAll("[data-spark]");
  if (!targets.length) return Promise.resolve();
  return fillSparklines(targets, (ticker) => loadBars(ticker, days), options);
}

// ---- 列表与规则状态 -------------------------------------------------------

function stockCell(ticker, name) {
  const cell = el("td", { class: "stock-cell" });
  const link = el("a", { href: `/app/stocks/${encodeURIComponent(ticker)}`, class: "stock-name" }, name || ticker);
  cell.appendChild(link);
  cell.appendChild(el("span", { class: "stock-code" }, ticker));
  return cell;
}

function freshnessCell(latest) {
  const cell = el("td");
  if (!latest || latest.freshness_ok === null || latest.freshness_ok === undefined) {
    cell.appendChild(el("span", { class: "faint" }, "—"));
    return cell;
  }
  const box = el("span", { class: "fresh-inline" });
  box.appendChild(el("span", { class: `dot ${latest.freshness_ok ? "dot-fresh" : "dot-stale"}`, "aria-hidden": "true" }));
  box.appendChild(document.createTextNode(latest.freshness_ok ? "正常" : "已过期"));
  cell.appendChild(box);
  return cell;
}

function ruleButton(text, onClick) {
  const button = el("button", { type: "button", class: "link-button" }, text);
  button.addEventListener("click", onClick);
  return button;
}

function ruleCell(region, item) {
  const cell = el("td", { class: "rule-cell" });
  const rule = item.rule || {};
  const state = rule.state;
  const active = rule.active;
  const draft = rule.draft;
  if (state === "ready" && draft) {
    cell.appendChild(el("span", { class: "badge warn" }, "规则待批准"));
    cell.appendChild(ruleButton("审阅", () => toggleRuleDetail(region, item, cell)));
  } else if (state === "queued" || state === "drafting") {
    cell.appendChild(el("span", { class: "badge" }, "起草中"));
    cell.appendChild(el("span", { class: "cell-sub" }, "由规则草稿任务生成"));
  } else if (state === "failed") {
    cell.appendChild(el("span", { class: "badge warn" }, "起草失败"));
    cell.appendChild(ruleButton("重试", () => toggleRuleDetail(region, item, cell)));
    if (rule.request && rule.request.error) cell.appendChild(errorText(rule.request.error));
  } else if (state === "rejected") {
    cell.appendChild(el("span", { class: "badge" }, "已驳回"));
    cell.appendChild(ruleButton("重新起草", () => toggleRuleDetail(region, item, cell)));
    if (rule.request && rule.request.reason) {
      cell.appendChild(el("span", { class: "cell-sub" }, `驳回理由：${rule.request.reason}`));
    }
  } else if (state === "needs_template") {
    cell.appendChild(el("span", { class: "badge warn" }, "待选择模板"));
    cell.appendChild(ruleButton("选择模板", () => toggleRuleDetail(region, item, cell)));
  } else if (active) {
    cell.appendChild(el("span", { class: "badge" }, `规则已生效 v${active.version}`));
    cell.appendChild(ruleButton("重新起草", () => toggleRuleDetail(region, item, cell)));
    cell.appendChild(zoneEventLine(rule.zone_events));
  } else {
    cell.appendChild(el("span", { class: "faint" }, "—"));
  }
  return cell;
}

function watchlistRow(region, item) {
  const latest = item.latest_research;
  const tr = el("tr", { class: item.enabled ? undefined : "row-disabled" });
  const stock = stockCell(item.ticker, item.name);
  if (!item.enabled) stock.appendChild(el("span", { class: "badge" }, "已停用"));
  tr.appendChild(stock);

  const spark = el("td", { class: "spark-cell" });
  spark.appendChild(sparkSlot(item.ticker));
  tr.appendChild(spark);

  const strategy = el("td");
  strategy.appendChild(el("span", {}, (latest && latest.strategy_label) || "—"));
  strategy.appendChild(el("span", { class: "cell-sub" }, fmt(item.industry)));
  tr.appendChild(strategy);

  const score = el("td", { class: "score-cell" });
  if (latest && typeof latest.investment_score === "number") {
    score.appendChild(el("span", { class: "score-cell-value" }, fmtNumber(latest.investment_score, 1)));
  } else {
    score.appendChild(el("span", { class: "faint" }, "未研究"));
  }
  tr.appendChild(score);
  tr.appendChild(el("td", { class: "num" }, fmtNumber(latest && latest.research_confidence_score, 0)));
  tr.appendChild(freshnessCell(latest));

  const changed = el("td");
  changed.appendChild(el("span", {}, latest && latest.changed_at ? formatDateTime(latest.changed_at) : "—"));
  if (latest && latest.meaningful_change_count) {
    changed.appendChild(el("span", { class: "cell-sub" }, `重要变化 ${latest.meaningful_change_count} 项`));
  }
  if (latest && latest.needs_agent_review) changed.appendChild(el("span", { class: "badge warn" }, "需复核"));
  tr.appendChild(changed);

  tr.appendChild(ruleCell(region, item));

  const refresh = el("td");
  refresh.appendChild(statusBadge(item.last_refresh_status));
  if (item.last_refreshed_at) refresh.appendChild(el("span", { class: "cell-sub" }, formatDateTime(item.last_refreshed_at)));
  if (item.last_refresh_error) refresh.appendChild(errorText(item.last_refresh_error));
  tr.appendChild(refresh);

  const actions = el("td", { class: "row-actions" });
  const toggle = el("button", { type: "button" }, item.enabled ? "停用" : "启用");
  toggle.addEventListener("click", () => {
    // 统一走 api.js（超时 + ApiError）；断网时也渲染错误态，不再静默。
    const promise = patch(`/watchlist/${item.listing_id}`, { enabled: !item.enabled })
      .then(() => {
        loadRegion(region, { render: loadWatchlist });
      })
      .catch((error) => {
        renderRegion(region, "error", error.message);
      });
    lockWhileBusy(toggle, promise);
  });
  actions.appendChild(toggle);
  const remove = el("button", { type: "button", class: "danger" }, "移除");
  remove.addEventListener("click", () => {
    removeWatchlistItem(region, item, remove);
  });
  actions.appendChild(remove);
  tr.appendChild(actions);
  return tr;
}

async function loadWatchlist(region, items) {
  const count = document.querySelector("#watchlist-count");
  if (!Array.isArray(items) || !items.length) {
    if (count) count.textContent = "";
    renderRegion(region, "empty");
    return;
  }
  if (count) {
    const enabled = items.filter((item) => item.enabled).length;
    count.textContent = `共 ${items.length} 只${enabled < items.length ? `，启用 ${enabled}` : ""}`;
  }
  const sorted = [...items].sort((a, b) => Number(b.enabled) - Number(a.enabled));
  const node = table(
    [
      { label: "股票" },
      { label: `近 ${SPARK_DAYS} 日` },
      { label: "策略 / 行业" },
      { label: "投资分" },
      { label: "置信度" },
      { label: "数据新鲜度" },
      { label: "最近变化" },
      { label: "规则" },
      { label: "刷新" },
      { label: "操作" },
    ],
    sorted.map((item) => watchlistRow(region, item))
  );
  node.classList.add("watch-table");
  renderRegion(region, "ready", node);
  mountSparklines(node, SPARK_DAYS);
}

async function removeWatchlistItem(region, item, button) {
  const reason = globalThis.prompt(`移出观察池 ${item.ticker}？请填写理由（必填）`);
  if (reason === null) return;
  if (!reason.trim()) {
    renderRegion(region, "error", "移出观察池需要填写理由。");
    return;
  }
  // POST /remove keeps the reason out of a DELETE body, which some proxies
  // drop; api.js keeps the timeout / ApiError wrapping so an offline rejection
  // renders an error state.
  const promise = post(`/watchlist/${item.listing_id}/remove`, { reason: reason.trim() })
    .then(() => {
      loadRegion(region, { render: loadWatchlist });
    })
    .catch((error) => {
      renderRegion(region, "error", error.message);
    });
  lockWhileBusy(button, promise);
  await promise;
}

// ---- 规则审阅（展开行） ----------------------------------------------------

function toggleRuleDetail(region, item, cell) {
  const row = cell.closest("tr");
  const next = row.nextElementSibling;
  if (next && next.classList.contains("rule-detail-row")) {
    next.remove();
    return;
  }
  const detail = el("tr", { class: "rule-detail-row" });
  const body = el("td", { colspan: "10" });
  body.appendChild(ruleReview(region, item));
  detail.appendChild(body);
  row.after(detail);
}

function paramEditor(region, item, draft, feedback) {
  return sharedParamEditor({
    rule: draft,
    onSave: {
      feedback,
      submit(save, changed) {
        const promise = post(
          `/watchlist/${item.listing_id}/rule/edit`,
          { rule_instance_id: draft.rule_instance_id, params: changed },
          { idempotency: false },
        )
          .then(() => loadRegion(document.querySelector("#watchlist-list"), { render: loadWatchlist }))
          .catch((error) => feedback.error(error.message));
        lockWhileBusy(save, promise);
      },
    },
  });
}

function draftBlock(region, item, draft, feedback) {
  const box = el("section", { class: "card rule-draft" });
  const head = el("h3", {}, `规则草稿 v${draft.version} · ${draft.template_ref}`);
  const calibration = draft.calibration_status;
  if (calibration) head.appendChild(el("span", { class: "badge badge-uncalibrated" }, label(CALIBRATION_LABELS, calibration)));
  box.appendChild(head);
  box.appendChild(el("p", { class: "small faint" }, "区间条件按生效参数显示；批准后这条规则对该标的生效，每日区间检测按 buy_zone / drop 条件产生事件。"));
  box.appendChild(zonesBlock(draft));
  const editor = paramEditor(region, item, draft, feedback);
  if (editor) box.appendChild(editor);
  const actions = el("div", { class: "rule-actions" });
  const approve = el("button", { type: "button", class: "primary" }, "批准该版本");
  approve.addEventListener("click", () => {
    const promise = post(`/watchlist/${item.listing_id}/rule/approve`, {
      rule_instance_id: draft.rule_instance_id,
      expected_active_rule_instance_id: item.rule && item.rule.active ? item.rule.active.rule_instance_id : null,
    }, { idempotency: false })
      .then(() => {
        loadRegion(document.querySelector("#watchlist-list"), { render: loadWatchlist });
      })
      .catch((error) => {
        feedback.error(error instanceof ApiError && error.status === 409 ? `${error.message}（请刷新后重新审阅）` : error.message);
      });
    lockWhileBusy(approve, promise);
  });
  actions.appendChild(approve);
  const reject = el("button", { type: "button", class: "danger" }, "驳回");
  reject.addEventListener("click", () => {
    const reason = globalThis.prompt("驳回该规则草稿的理由（必填）");
    if (reason === null) return;
    if (!reason.trim()) {
      feedback.error("驳回需要填写理由。");
      return;
    }
    const promise = post(`/watchlist/${item.listing_id}/rule/reject`, {
      rule_instance_id: draft.rule_instance_id,
      reason: reason.trim(),
    }, { idempotency: false })
      .then(() => {
        loadRegion(document.querySelector("#watchlist-list"), { render: loadWatchlist });
      })
      .catch((error) => feedback.error(error.message));
    lockWhileBusy(reject, promise);
  });
  actions.appendChild(reject);
  box.appendChild(actions);
  return box;
}

async function templateChooser(region, item, feedback, labelText) {
  const box = el("div", { class: "rule-chooser" });
  box.appendChild(el("p", { class: "small" }, labelText));
  const select = el("select", { "aria-label": "起草模板" });
  select.appendChild(el("option", { value: "" }, "加载模板中…"));
  select.disabled = true;
  box.appendChild(select);
  const start = el("button", { type: "button", class: "primary" }, "起草");
  start.disabled = true;
  box.appendChild(start);
  try {
    const templates = await get("/rule-templates");
    select.replaceChildren();
    select.appendChild(el("option", { value: "" }, "选择起草模板…"));
    for (const template of templates) {
      select.appendChild(el("option", { value: template.id }, `${template.name}（${template.id}）`));
    }
    select.disabled = false;
  } catch (error) {
    feedback.error(`模板列表加载失败：${error.message}`);
    return box;
  }
  select.addEventListener("change", () => {
    start.disabled = !select.value;
  });
  start.addEventListener("click", () => {
    const promise = post(`/watchlist/${item.listing_id}/rule/draft`, { template_id: select.value }, { idempotency: false })
      .then(() => {
        loadRegion(document.querySelector("#watchlist-list"), { render: loadWatchlist });
      })
      .catch((error) => feedback.error(error.message));
    lockWhileBusy(start, promise);
  });
  return box;
}

function activeBlock(active, events) {
  const box = el("section", { class: "card rule-active" });
  box.appendChild(el("h3", {}, `当前生效规则 v${active.version} · ${active.template_ref}`));
  box.appendChild(zoneEventLine(events));
  const latest = events && events.latest;
  if (latest && latest.conditions_met && latest.conditions_met.length) {
    box.appendChild(el("p", { class: "small faint" }, eventsSummary(latest)));
  }
  return box;
}

function ruleReview(region, item) {
  const box = el("div", { class: "rule-review" });
  const rule = item.rule || {};
  const feedback = ruleFeedback(box);
  if (rule.active) box.appendChild(activeBlock(rule.active, rule.zone_events));
  const chooserNote =
    rule.state === "needs_template"
      ? "选择起草模板；本轮不替换已有生效规则。"
      : rule.state === "failed"
        ? "重试起草（可换模板）。"
        : "重新起草会生成新的草稿版本；已驳回的理由会作为偏好输入。";
  if (rule.draft) {
    box.appendChild(draftBlock(region, item, rule.draft, feedback));
  } else if (rule.state === "needs_template" || rule.state === "failed" || rule.state === "rejected") {
    if (rule.state === "failed" && rule.request && rule.request.error) {
      box.appendChild(el("p", { class: "notice notice-warn" }, `上次起草失败：${rule.request.error}`));
    }
    if (rule.state === "rejected" && rule.request && rule.request.reason) {
      box.appendChild(el("p", { class: "small faint" }, `已驳回：${rule.request.reason}`));
    }
    templateChooser(region, item, feedback, chooserNote)
      .then((node) => box.appendChild(node))
      .catch((error) => feedback.error(error.message));
  } else if (!rule.active) {
    box.appendChild(
      el("p", { class: "muted" }, "该条目暂不支持规则起草（仅 A 股标的参与模板筛选与规则）。")
    );
  }
  return box;
}

// ---- 添加表单 -------------------------------------------------------------

const AUTO_TEMPLATE = "auto";

function templateNote(form) {
  let note = form.querySelector(".template-note");
  if (!note) {
    note = el("p", { class: "small faint template-note" });
    const select = form.querySelector('select[name="template_id"]');
    if (select) select.after(note);
  }
  return note;
}

async function populateTemplateSelect(form) {
  const select = form.querySelector('select[name="template_id"]');
  if (!select) return;
  try {
    const templates = await get("/rule-templates");
    for (const template of templates) {
      select.appendChild(el("option", { value: template.id }, template.name));
    }
  } catch (error) {
    templateNote(form).textContent = `模板列表加载失败：${error.message}`;
  }
}

function wireWatchlistForm(form) {
  // 选中股票后回填行业和最新已披露年报年度，缺失的字段只在选中后查询。
  // 提交时 ticker 的校验规则不变（服务端 WatchlistAdd）。
  const tickerInput = form.querySelector('input[name="ticker"]');
  const nameInput = form.querySelector('input[name="name"]');
  const industryInput = form.querySelector('input[name="industry"]');
  const reportYearInput = form.querySelector('input[name="report_year"]');
  const templateSelect = form.querySelector('select[name="template_id"]');
  const submit = form.querySelector('button[type="submit"]');
  let selectionId = 0;
  let metadataPending = false;
  let suggestedTemplate = null;
  const industryPlaceholder = industryInput ? industryInput.placeholder : "";
  const yearPlaceholder = reportYearInput ? reportYearInput.placeholder : "";
  const yearTitle = reportYearInput ? reportYearInput.title : "";
  function cancelMetadataLookup() {
    selectionId += 1;
    suggestedTemplate = null;
    if (industryInput) {
      industryInput.value = "";
      industryInput.placeholder = industryPlaceholder;
    }
    if (reportYearInput) {
      reportYearInput.value = "";
      reportYearInput.placeholder = yearPlaceholder;
      reportYearInput.title = yearTitle;
    }
    if (templateSelect && templateSelect.value !== AUTO_TEMPLATE) {
      templateSelect.value = AUTO_TEMPLATE;
      const note = form.querySelector(".template-note");
      if (note) note.textContent = "";
    }
    if (metadataPending && submit) submit.disabled = false;
    metadataPending = false;
  }
  form.addEventListener("reset", cancelMetadataLookup);
  if (tickerInput) {
    tickerInput.addEventListener("input", cancelMetadataLookup);
    attachCombobox(tickerInput, {
      onSelect: async (item) => {
        cancelMetadataLookup();
        if (nameInput && item.name) nameInput.value = item.name;
        if (industryInput) industryInput.value = item.industry || "";
        if (reportYearInput && Number.isInteger(item.report_year)) {
          reportYearInput.value = String(item.report_year);
          if (item.disclosed_on) reportYearInput.title = `${yearTitle}；披露于 ${item.disclosed_on}`;
        }
        const id = selectionId;
        const queries = [];
        const errors = [];
        async function lookup(field, key, path, placeholder) {
          field.placeholder = placeholder;
          try {
            const data = await get(`/stocks/${encodeURIComponent(item.ticker)}/${path}`);
            if (id === selectionId && tickerInput.value === item.ticker && !field.value.trim()) {
              field.value = data[key] == null ? "" : String(data[key]);
              if (key === "report_year" && data.disclosed_on) field.title = `${yearTitle}；披露于 ${data.disclosed_on}`;
              if (key === "report_year" && data.report_year == null) errors.push("未找到已披露的年报年度，请确认后填写。");
            }
          } catch (error) {
            errors.push(error.message);
          }
        }
        if (industryInput && !item.industry) queries.push(lookup(industryInput, "industry", "industry", "正在获取行业…"));
        if (reportYearInput && !Number.isInteger(item.report_year)) queries.push(lookup(reportYearInput, "report_year", "report-year", "正在查询年报…"));
        // 模板推荐：仅在用户保留“AI 推荐”时请求一次；失败不阻塞添加。
        if (templateSelect && templateSelect.value === AUTO_TEMPLATE) {
          const note = templateNote(form);
          note.textContent = "正在推荐起草模板…";
          queries.push(
            get(`/watchlist/template-suggestion?ticker=${encodeURIComponent(item.ticker)}`)
              .then((suggestion) => {
                if (id !== selectionId || tickerInput.value !== item.ticker) return;
                if (suggestion && suggestion.template_id) {
                  suggestedTemplate = suggestion.template_id;
                  templateSelect.value = suggestion.template_id;
                  note.textContent = `已推荐模板：${suggestion.name || suggestion.template_id}${suggestion.reason ? `（${suggestion.reason}）` : ""}；可修改。`;
                } else {
                  note.textContent = "未能自动推荐模板，可手动选择；留空则稍后再起草。";
                }
              })
              .catch(() => {
                if (id === selectionId) note.textContent = "模板推荐失败，可手动选择；留空则稍后再起草。";
              })
          );
        }
        if (!queries.length) return;
        metadataPending = true;
        if (submit) submit.disabled = true;
        try {
          await Promise.all(queries);
        } finally {
          if (id === selectionId) {
            metadataPending = false;
            if (industryInput) industryInput.placeholder = industryPlaceholder;
            if (reportYearInput) reportYearInput.placeholder = yearPlaceholder;
            if (submit) submit.disabled = false;
            if (errors.length) {
              const results = document.querySelector("#watchlist-refresh-results");
              if (results) renderRegion(results, "error", errors.join(" "));
            }
          }
        }
      },
    });
  }
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (metadataPending) return;
    const submitButton = form.querySelector('button[type="submit"]');
    const formData = new FormData(form);
    const payload = {};
    for (const key of ["ticker", "industry", "name", "report_year"]) {
      const value = String(formData.get(key) || "").trim();
      if (value) payload[key] = key === "report_year" ? Number(value) : value;
    }
    if (templateSelect) {
      const chosen = templateSelect.value;
      if (chosen && chosen !== AUTO_TEMPLATE) payload.template_id = chosen;
      else if (chosen === AUTO_TEMPLATE && suggestedTemplate) payload.template_id = suggestedTemplate;
    }
    const promise = post("/watchlist", payload, { idempotency: false })
      .then(() => {
        form.reset();
        suggestedTemplate = null;
        const list = document.querySelector("#watchlist-list");
        if (list) loadRegion(list, { render: loadWatchlist });
      })
      .catch((error) => {
        const results = document.querySelector("#watchlist-refresh-results");
        if (results) renderRegion(results, "error", error.message);
      });
    if (submitButton) lockWhileBusy(submitButton, promise);
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
              `共 ${data.total} 只：新排队 ${data.created_runs}，复用运行 ${data.reused_runs}，失败 ${data.failed}。`
            )
          );
          const list = el("ul", { class: "refresh-results" });
          for (const item of data.items || []) {
            const row = el("li");
            row.appendChild(el("span", { class: "stock-code" }, fmt(item.ticker)));
            row.appendChild(statusBadge(item.status));
            if (item.run_id) row.appendChild(el("span", { class: "faint" }, `运行 #${item.run_id}${item.created ? "（新建）" : "（复用）"}`));
            if (item.error) row.appendChild(errorText(item.error));
            list.appendChild(row);
          }
          container.appendChild(list);
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

// ---- 启动 -----------------------------------------------------------------

export function bootWatchlist() {
  const region = document.querySelector("#watchlist-list");
  if (!region) return;
  loadRegion(region, { render: loadWatchlist });
  const form = document.querySelector("#watchlist-add-form");
  if (form) {
    populateTemplateSelect(form);
    wireWatchlistForm(form);
  }
  const refreshAll = document.querySelector("#watchlist-refresh-all");
  if (refreshAll) wireRefreshAll(refreshAll);
}
