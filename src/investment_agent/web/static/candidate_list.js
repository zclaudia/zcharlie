// 本周候选列表、忽略和手动提名。列表状态只在这个模块里。

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
  LIST_LIMIT,
  SPARK_DAYS,
  UNFINISHED_VERIFICATION,
  badge,
  draftBadge,
  industryText,
  inlineReason,
  metCount,
  newestVerificationByListing,
  pctChange,
  pctNode,
  reasonLine,
  redirectOn401,
  renderError,
  section,
  showIssues,
  templateBadge,
  truncate,
  verificationText,
  whyModel,
} from "./candidate_shared.js";

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
const MAX_DOTS = 12;

function candidateHref(candidate) {
  return `/app/candidates/${encodeURIComponent(candidate.id)}`;
}


function hitRatio(candidate) {
  const { met, total } = metCount(candidate);
  return total ? met / total : 0;
}

function sortCandidates(items, mode = "opened") {
  return [...items].sort((a, b) => {
    if (mode === "hits") {
      const diff = hitRatio(b) - hitRatio(a);
      if (diff) return diff;
    } else if (mode === "expiry") {
      const expA = Date.parse(a.expires_at) || Infinity;
      const expB = Date.parse(b.expires_at) || Infinity;
      if (expA !== expB) return expA < expB ? -1 : 1;
    }
    const dayA = formatDate(a.opened_at);
    const dayB = formatDate(b.opened_at);
    if (dayA !== dayB) return dayA < dayB ? 1 : -1;
    const rankA = a.latest_screen && typeof a.latest_screen.rank === "number" ? a.latest_screen.rank : Infinity;
    const rankB = b.latest_screen && typeof b.latest_screen.rank === "number" ? b.latest_screen.rank : Infinity;
    if (rankA !== rankB) return rankA - rankB;
    return a.ticker < b.ticker ? -1 : a.ticker > b.ticker ? 1 : 0;
  });
}

function hitDots(met, total) {
  const dots = el("span", { class: "cand-hits", "aria-hidden": "true" });
  for (let index = 0; index < Math.min(total, MAX_DOTS); index += 1) {
    dots.appendChild(el("i", { class: index < met ? "on" : "" }));
  }
  return dots;
}

function daysLeftText(candidate) {
  const end = Date.parse(candidate.expires_at);
  if (candidate.status !== "open" || !Number.isFinite(end)) return "";
  const days = Math.ceil((end - Date.now()) / 86400000);
  return days > 0 ? `剩 ${days} 天` : "已到期";
}

function shortDate(value) {
  const day = formatDate(value);
  return day.length >= 10 ? day.slice(5) : day;
}

function candidateBadges(candidate) {
  const box = el("div", { class: "cluster cand-badges" });
  const refs = Array.isArray(candidate.template_refs) ? candidate.template_refs : [];
  if (refs.length) for (const ref of refs) box.appendChild(templateBadge(ref));
  else box.appendChild(badge("手动提名", "accent"));
  if (candidate.status === "open") box.appendChild(draftBadge(candidate));
  else box.appendChild(badge(label(CANDIDATE_STATUS_LABELS, candidate.status), `state-${candidate.status}`));
  const request = listState.verifications.get(candidate.listing_id);
  if (request && UNFINISHED_VERIFICATION.has(request.status)) box.appendChild(badge("核对未完成", "warn", verificationText(request)));
  if ((candidate.calibration_status || "uncalibrated") === "uncalibrated") {
    box.appendChild(el("span", { class: "badge badge-uncalibrated cand-uncal", title: "模板默认参数尚未经回测校准，只是假设" }, "未校准"));
  }
  return box;
}

// 入选理由块（桌面）：标题 + 命中进度点 + 两列理由 + 更多。
function whyBlock(candidate, model) {
  const block = el("div", { class: "cand-why" });
  const head = el("div", { class: "cand-why-head" });
  head.appendChild(el("strong", {}, "入选理由"));
  if (model.kind === "screen" || model.kind === "draft") {
    head.appendChild(hitDots(model.met, model.total));
    head.appendChild(el("span", { class: "cand-why-count" }, `${model.verb} ${model.met}/${model.total} 项`));
  } else if (model.kind === "text") {
    head.appendChild(el("span", { class: "faint" }, "AI 起草分析"));
  }
  block.appendChild(head);
  if (model.kind === "screen" || model.kind === "draft") {
    const list = el("ul", { class: "reason-list" });
    for (const { reason, ok, prefix } of model.items) {
      list.appendChild(el("li", { class: ok ? "reason-ok" : "reason-warn" }, undefined)).appendChild(reasonLine(reason, prefix));
    }
    block.appendChild(list);
    const text =
      model.more > 0 ? `另有 ${model.more} 项 · 查看详情` : model.kind === "draft" ? "查看完整分析" : "";
    if (text) block.appendChild(el("a", { class: "cand-why-more small", href: candidateHref(candidate) }, text));
  } else if (model.kind === "text") {
    const list = el("ul", { class: "reason-list reason-list-one" });
    for (const sentence of model.sentences) list.appendChild(el("li", { class: "reason-ok" }, truncate(sentence, 72)));
    block.appendChild(list);
    block.appendChild(el("a", { class: "cand-why-more small", href: candidateHref(candidate) }, "查看完整分析"));
  } else {
    block.appendChild(el("p", { class: "muted cand-why-none" }, "无筛选理由（手动提名）。"));
  }
  return block;
}

// 手机卡片的命中条：进度点 + 计数 + 最靠前的两条理由（单行）。
function hitRow(model) {
  const row = el("div", { class: "cand-hitrow" });
  if (model.kind === "screen" || model.kind === "draft") {
    row.appendChild(hitDots(model.met, model.total));
    row.appendChild(el("b", { class: "cand-hit-n" }, `${model.met}/${model.total}`));
    const picks = (model.items.filter((item) => item.ok).length ? model.items.filter((item) => item.ok) : model.items).slice(0, 2);
    row.appendChild(el("span", { class: "cand-hit-tx" }, picks.map((item) => inlineReason(item.reason, item.prefix)).join(" · ")));
  } else if (model.kind === "text") {
    row.appendChild(el("span", { class: "cand-hit-tag" }, "AI 起草分析"));
    row.appendChild(el("span", { class: "cand-hit-tx" }, truncate(model.sentences[0] || "", 40)));
  } else {
    row.appendChild(el("span", { class: "cand-hit-tx" }, "无筛选理由（手动提名）"));
  }
  return row;
}

function candidateItem(candidate) {
  const href = candidateHref(candidate);
  const model = whyModel(candidate);
  const item = el("article", { class: "cand", "data-candidate-id": candidate.id });
  const main = el("div", { class: "cand-main" });

  const identity = el("div", { class: "cand-id" });
  const name = el("div", { class: "cand-nm" });
  name.appendChild(el("a", { class: "cand-title", href }, candidate.name || candidate.ticker));
  name.appendChild(el("span", { class: "code" }, candidate.ticker));
  identity.appendChild(name);
  main.appendChild(identity);
  main.appendChild(candidateBadges(candidate));

  const screen = candidate.latest_screen;
  const left = daysLeftText(candidate);
  const meta = el("p", { class: "cand-meta" });
  for (const text of [
    industryText(screen && screen.context),
    `开启 ${shortDate(candidate.opened_at)}`,
    `到期 ${shortDate(candidate.expires_at)}${left ? ` · ${left}` : ""}`,
  ]) {
    if (meta.children.length) meta.appendChild(el("i", {}, "/"));
    meta.appendChild(el("span", {}, text));
  }
  main.appendChild(meta);
  main.appendChild(whyBlock(candidate, model));
  main.appendChild(hitRow(model));
  main.appendChild(el("p", { class: "cand-due" }, `到期 ${shortDate(candidate.expires_at)}${left ? ` · ${left}` : ""}`));
  item.appendChild(main);

  // 右侧栏：价格 + 日线 + 区间涨跌 + 操作。
  const side = el("aside", { class: "cand-side" });
  side.appendChild(el("div", { class: "cand-px", "data-price-for": candidate.ticker }));
  side.appendChild(el("span", { class: "spark spark-pending cand-spark", "data-spark": candidate.ticker, "aria-hidden": "true" }));
  side.appendChild(el("div", { class: "cand-rets", "data-rets-for": candidate.ticker }));
  const acts = el("div", { class: "cand-acts" });
  if (candidate.status === "open") {
    const dismiss = el("button", { type: "button", class: "btn btn-sm btn-ghost", "data-action": "dismiss-candidate" }, "忽略");
    dismiss.addEventListener("click", () => openDismissSheet(candidate));
    acts.appendChild(dismiss);
  }
  acts.appendChild(el("a", { class: `btn btn-sm${candidate.status === "open" ? " btn-primary" : ""}`, href }, candidate.status === "open" ? "审阅" : "查看"));
  side.appendChild(acts);
  item.appendChild(side);
  return item;
}

function fillPriceSummary(root, ticker, bars) {
  const closes = (Array.isArray(bars) ? bars : []).filter((bar) => bar && Number.isFinite(bar.close));
  if (closes.length < 2) return;
  const last = closes[closes.length - 1];
  const back = (n) => closes[Math.max(0, closes.length - 1 - n)].close;
  const high = Math.max(...closes.map((bar) => bar.close));
  const periods = [
    ["5日", pctChange(back(5), last.close)],
    ["20日", pctChange(back(20), last.close)],
    [`${closes.length}日`, pctChange(closes[0].close, last.close)],
    ["距高点", pctChange(high, last.close)],
  ];
  const selector = (attr) => `[${attr}="${CSS.escape(ticker)}"]`;
  for (const host of root.querySelectorAll(selector("data-price-for"))) {
    host.replaceChildren(
      el("b", { class: "price-last" }, last.close.toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })),
      pctNode(pctChange(back(1), last.close)),
      el("small", { class: "faint" }, `${String(last.trade_date).slice(5)} 收盘`)
    );
  }
  for (const host of root.querySelectorAll(selector("data-rets-for"))) {
    host.replaceChildren(
      ...periods.map(([text, value]) => {
        const cell = el("div", { class: "cand-ret" });
        cell.appendChild(el("span", { class: "faint" }, text));
        cell.appendChild(pctNode(value));
        return cell;
      })
    );
  }
}

export function renderCandidateList(region) {
  const templateFilter = document.querySelector("#candidates-template-filter");
  const template = templateFilter ? templateFilter.value : "";
  const sortSelect = document.querySelector("#candidates-sort");
  const items = sortCandidates(listState.items, sortSelect ? sortSelect.value : "opened").filter(
    (candidate) => !template || (candidate.template_refs || []).includes(template)
  );
  if (!items.length) {
    renderRegion(region, "empty", template ? "该模板下没有候选。" : undefined);
    return;
  }
  const container = el("div", { class: "cand-list" });
  for (const candidate of items) container.appendChild(candidateItem(candidate));
  renderRegion(region, "ready", container);
  const targets = container.querySelectorAll("[data-spark]");
  loadSparkBars(container, SPARK_DAYS).then((byTicker) =>
    fillSparklines(
      targets,
      async (ticker) => {
        const data = byTicker.get(ticker.toUpperCase()) || null;
        if (data) fillPriceSummary(container, ticker, data.bars);
        return data;
      },
      { width: "100%", height: 56 }
    )
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

// 状态分段控件（桌面）与胶囊（手机）与隐藏的 #candidates-status-filter 保持一致。
function syncStatusControls(status, total) {
  for (const button of document.querySelectorAll("[data-status]")) {
    const on = button.dataset.status === status;
    button.classList.toggle("on", on);
    button.setAttribute("aria-pressed", on ? "true" : "false");
    const count = button.querySelector("[data-status-count]");
    if (count) {
      count.hidden = !(on && typeof total === "number");
      if (!count.hidden) count.textContent = String(total);
    }
  }
}

export async function loadCandidateList(region) {
  const statusFilter = document.querySelector("#candidates-status-filter");
  const status = (statusFilter && statusFilter.value) || "open";
  syncStatusControls(status, null);
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
    syncStatusControls(status, page ? page.total : null);
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

// ---- 列表：忽略（底部弹层，理由必填；与详情页同一个接口）---------------------------

const dismissState = { candidate: null, list: null };

function openDismissSheet(candidate) {
  const sheet = section("candidate-dismiss-sheet");
  if (!sheet) return;
  dismissState.candidate = candidate;
  const sub = section("candidate-dismiss-sub");
  if (sub) sub.textContent = `${candidate.ticker} ${candidate.name || ""}：${DISMISS_NOTE}`.replace("  ", " ");
  const reason = section("candidate-dismiss-reason");
  if (reason) reason.value = "";
  const issues = section("candidate-dismiss-issues");
  if (issues) clear(issues);
  if (globalThis.zcOverlay) globalThis.zcOverlay.open(sheet);
}

export function wireDismissSheet(list) {
  const confirmButton = section("candidate-dismiss-confirm");
  if (!confirmButton) return;
  dismissState.list = list;
  confirmButton.addEventListener("click", () => {
    const candidate = dismissState.candidate;
    if (!candidate) return;
    const reasonInput = section("candidate-dismiss-reason");
    const issues = section("candidate-dismiss-issues");
    const reason = reasonInput ? reasonInput.value.trim() : "";
    clear(issues);
    if (!reason) {
      issues.appendChild(el("p", { class: "notice notice-error" }, "忽略候选需要填写理由。"));
      if (reasonInput) reasonInput.focus();
      return;
    }
    const promise = post(`/candidates/${candidate.id}/dismiss`, { reason }, { idempotency: false })
      .then(() => {
        if (reasonInput) reasonInput.value = "";
        dismissState.candidate = null;
        if (globalThis.zcOverlay) globalThis.zcOverlay.close();
        if (dismissState.list) loadCandidateList(dismissState.list);
      })
      .catch((error) => {
        if (redirectOn401(error)) return;
        showIssues(issues, error, "忽略失败");
      });
    lockWhileBusy(confirmButton, promise);
  });
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

export function wireNominateForm(form, list) {
  // 代码框接股票搜索下拉：选中回填 600519.SH 形式的 ticker；normalizeTicker 仍兜底。
  const tickerInput = form.querySelector('input[name="ticker"]');
  if (tickerInput) attachCombobox(tickerInput);
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
        nominateNotice(result, conflict ? `提名失败：${ticker} 已在观察池中，无需再提名。` : `提名失败：${error.message}`, "notice-error");
      });
    if (submit) lockWhileBusy(submit, promise);
    await promise;
  });
}

