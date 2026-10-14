// 持仓池页面：账户、估值派生持仓、事实录入与时间线。
// 契约见 docs/product/watch-holding-plan.md 2.3/2.4/2.10：
// 只有用户确认的事实改变账本；行情只生成不可变估值；读取与录入共用服务端校验。
// AG24：本页记录用户确认的实际买卖事实与资金流，从不提交委托。

import { ApiError, get, lockWhileBusy, post, renderRegion } from "./api.js";
import { el, table } from "./dom.js";
import { describeIssues, formatDateTime } from "./format.js";
import { percent } from "./agent_portfolio.js";
import {
  eventsSummary,
  paramEditor as sharedParamEditor,
  ruleFeedback,
  zoneEventLine,
  zonesBlock,
} from "./rule_review.js";

const PENDING_KEY = "zcharlie.portfolio.pending.v1";
let epoch = 0, busy = false, pending = null, storageBlocked = false, currentSnapshot = null;
let recentActivity = new Set(), lastValuation = null, lastListings = {}, hasAccounts = true;
const CHANGE_KINDS = new Set(["trade", "corporate_action", "opening_balance", "reconciliation"]);

export const PORTFOLIO_REGIONS = new Set([
  "portfolio-holdings",
  "portfolio-events",
  "portfolio-fact-result",
  "portfolio-realized",
  "portfolio-archive",
]);

const KIND_LABELS = Object.freeze({
  opening_balance: "期初余额",
  reconciliation: "对账余额",
  trade: "交易",
  cash_transfer: "资金变动",
  dividend: "分红",
  corporate_action: "公司行动",
  reversal: "冲销",
});

function fmtDecimal(value, digits = 2) {
  if (value === null || value === undefined) return "—";
  const number = Number(value);
  if (!Number.isFinite(number)) return String(value);
  return number.toLocaleString("zh-CN", { maximumFractionDigits: digits, minimumFractionDigits: 0 });
}

function field(form, name) {
  return form.querySelector(`[name="${name}"]`);
}

function text(form, name) {
  const node = field(form, name);
  const value = node && typeof node.value === "string" ? node.value.trim() : "";
  return value === "" ? null : value;
}

function decimal(form, name) {
  const value = text(form, name);
  if (value !== null && !/^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?$/i.test(value)) throw new Error("请输入有效的十进制数值。");
  return value;
}

function factSummary(fact) {
  if (!fact || typeof fact !== "object") return "—";
  if (fact.kind === "trade") {
    const direction = fact.direction === "buy" ? "买入" : "卖出";
    const quantity = fmtDecimal(fact.holding && fact.holding.quantity, 4);
    const price = fmtDecimal(fact.actual_price, 4);
    const fee = fact.fee === null || fact.fee === undefined ? "费用未知" : `费用 ${fmtDecimal(fact.fee)}`;
    return `${direction} ${quantity} @ ${price}（${fee}）`;
  }
  if (fact.kind === "opening_balance" || fact.kind === "reconciliation") {
    const held = Array.isArray(fact.holdings) ? fact.holdings.length : 0;
    const cash = fact.cash === null || fact.cash === undefined ? "现金未知" : `现金 ${fmtDecimal(fact.cash)}`;
    return `${KIND_LABELS[fact.kind]}：${held} 只持仓，${cash}（覆盖 ${fact.coverage}）`;
  }
  if (fact.kind === "cash_transfer") return `资金变动 ${fmtDecimal(fact.amount)}`;
  if (fact.kind === "dividend") return `分红 ${fmtDecimal(fact.amount)}`;
  if (fact.kind === "reversal") return "冲销（纠错）";
  if (fact.kind === "corporate_action") return fact.action === "split" ? `比例变更 ${fmtDecimal(fact.ratio, 6)}` : "待确认公司行动";
  return fact.kind || "—";
}

function renderDisabled() {
  const notice = document.querySelector("#portfolio-disabled");
  if (notice) notice.hidden = false;
}

function closeOverlay() {
  if (globalThis.zcOverlay) globalThis.zcOverlay.close();
}

function openFactDrawer(tab = "fact") {
  showDrawerTab(tab);
  const drawer = document.querySelector("#portfolio-fact-drawer");
  if (drawer && globalThis.zcOverlay) globalThis.zcOverlay.open(drawer);
}

// 页面刚加载时 nav.js 会在 pageshow 里收起所有弹层（bfcache 返回），所以延后一拍再打开。
function openFactDrawerWhenReady() {
  if (!globalThis.zcOverlay) return;
  const open = () => setTimeout(() => openFactDrawer("fact"), 0);
  if (document.readyState === "complete") open();
  else globalThis.addEventListener("pageshow", open, { once: true });
}

// 录入 / 纠错两个面板共用一个抽屉；页签只切换可见面板，两个面板各自的表单与校验不变。
function showDrawerTab(name) {
  for (const tab of document.querySelectorAll("[data-pf-tab]")) {
    const on = tab.getAttribute("data-pf-tab") === name;
    tab.classList.toggle("on", on);
    tab.setAttribute("aria-selected", on ? "true" : "false");
  }
  for (const pane of document.querySelectorAll("[data-pf-pane]")) {
    pane.hidden = pane.getAttribute("data-pf-pane") !== name;
  }
}

// 没有账户时只显示空状态（说明 + 三步 + 新建账户）；有账户后显示 KPI / 持仓 / 时间线等。
function setEmptyState(empty) {
  hasAccounts = !empty;
  const emptyCard = document.querySelector("#portfolio-empty");
  const ledger = document.querySelector("#portfolio-ledger");
  if (emptyCard) emptyCard.hidden = !empty;
  if (ledger) ledger.hidden = empty;
  const refresh = document.querySelector("#portfolio-refresh");
  if (refresh && empty) refresh.disabled = true;
}

function hideKpis() {
  const box = document.querySelector("#portfolio-kpis");
  if (box) {
    box.replaceChildren();
    box.hidden = true;
  }
}

async function loadListings(ids) {
  const unique = [...new Set(ids.filter((id) => Number.isInteger(id)))];
  if (!unique.length) return {};
  const rows = [];
  for (let offset = 0; offset < unique.length; offset += 25) rows.push(...await get(`/portfolio/listings?ids=${unique.slice(offset, offset + 25).join(",")}`));
  return Object.fromEntries(rows.map((row) => [row.listing_id, row]));
}

function stockCell(listing, listingId) {
  const cell = el("td", { class: "stock-cell" });
  const row = listing || {};
  if (row.ticker) {
    cell.appendChild(el("a", { href: `/app/stocks/${encodeURIComponent(row.ticker)}`, class: "stock-name" }, row.name || row.ticker));
    cell.appendChild(el("span", { class: "stock-code" }, row.ticker));
  } else {
    cell.appendChild(el("span", { class: "faint" }, `#${listingId}`));
  }
  return cell;
}

// 列表里（非表格）的股票块：名称 + 代码。
function stockBlock(listing, listingId) {
  const block = el("span", { class: "stock-block" });
  const row = listing || {};
  if (row.ticker) {
    block.appendChild(el("a", { href: `/app/stocks/${encodeURIComponent(row.ticker)}`, class: "stock-name" }, row.name || row.ticker));
    block.appendChild(el("span", { class: "stock-code" }, row.ticker));
  } else {
    block.appendChild(el("span", { class: "faint" }, `#${listingId}`));
  }
  return block;
}

function orderByRecentActivity(holdings) {
  // Change-driven order (plan 2.4): holdings whose ledger facts moved within
  // the last day come first; everything else keeps the ledger order.
  if (!recentActivity.size) return holdings;
  const active = holdings.filter((holding) => recentActivity.has(String(holding.listing_ref.id)));
  const rest = holdings.filter((holding) => !recentActivity.has(String(holding.listing_ref.id)));
  return [...active, ...rest];
}

function signed(value, digits = 2) {
  const text = fmtDecimal(value, digits);
  return Number(value) > 0 ? `+${text}` : text;
}

function direction(value) {
  const number = Number(value);
  return number > 0 ? "up" : number < 0 ? "down" : "";
}

function holdingRow(holding, listings) {
  const tr = el("tr");
  const listingId = holding.listing_ref.id;
  const stock = stockCell(listings[listingId], listingId);
  stock.classList.add("c-stock");
  if (holding.holding_days !== null && holding.holding_days !== undefined) {
    stock.appendChild(el("span", { class: "cell-sub pf-days" }, `持有 ${holding.holding_days} 天`));
  }
  const notes = [];
  if (holding.cost_unknown_reason) notes.push(`成本：${holding.cost_unknown_reason}`);
  if (holding.price_unknown_reason) notes.push(`价格：${holding.price_unknown_reason}`);
  if (notes.length) stock.appendChild(el("span", { class: "cell-sub pf-note" }, notes.join("；")));
  // 手机上数量 / 成本 / 现价 / 权重合并成股票下面的一行小字（桌面隐藏，用独立列）。
  const listing = listings[listingId] || {};
  const meta = listing.ticker ? [listing.ticker, `${fmtDecimal(holding.quantity, 4)} 股`] : [`${fmtDecimal(holding.quantity, 4)} 股`];
  meta.push(holding.average_cost === null ? "成本未知" : `成本 ${fmtDecimal(holding.average_cost, 4)}`);
  if (holding.price !== null && holding.price !== undefined) meta.push(`现价 ${fmtDecimal(holding.price, 4)}`);
  else meta.push(holding.price_unknown_reason === "stale_price" ? "价格过期" : "无价格");
  if (holding.weight !== null && holding.weight !== undefined) meta.push(`权重 ${percent(holding.weight)}`);
  stock.appendChild(el("span", { class: "pf-meta" }, meta.join(" · ")));
  tr.appendChild(stock);

  tr.appendChild(el("td", { class: "num c-qty" }, fmtDecimal(holding.quantity, 4)));
  tr.appendChild(el("td", { class: "num c-cost" }, holding.average_cost === null ? "成本未知" : fmtDecimal(holding.average_cost, 4)));
  const priceCell = el("td", { class: "num c-price" });
  if (holding.price !== null && holding.price !== undefined) {
    priceCell.appendChild(el("span", {}, fmtDecimal(holding.price, 4)));
    if (holding.price_date) priceCell.appendChild(el("span", { class: "cell-sub" }, holding.price_date));
  } else {
    priceCell.appendChild(el("span", { class: "faint" }, holding.price_unknown_reason === "stale_price" ? "价格过期" : "无价格"));
  }
  tr.appendChild(priceCell);
  tr.appendChild(el("td", { class: "num c-mv" }, holding.market_value === null ? "—" : fmtDecimal(holding.market_value)));
  const pnl = el("td", { class: "num c-pnl" });
  if (holding.floating_pnl === null || holding.floating_pnl === undefined) {
    pnl.appendChild(el("span", { class: "faint" }, "—"));
  } else {
    pnl.appendChild(el("span", { class: direction(holding.floating_pnl) }, signed(holding.floating_pnl)));
    if (holding.gain_vs_cost !== null && holding.gain_vs_cost !== undefined) {
      const gain = percent(holding.gain_vs_cost);
      pnl.appendChild(el("span", { class: `cell-sub ${direction(holding.gain_vs_cost)}`.trim() }, Number(holding.gain_vs_cost) > 0 ? `+${gain}` : gain));
    }
  }
  tr.appendChild(pnl);
  tr.appendChild(el("td", { class: "num c-weight" }, holding.weight === null ? "—" : percent(holding.weight)));
  const ruleCell = el("td", { class: "rule-cell c-rule", "data-rule-listing": String(listingId) });
  ruleCell.appendChild(el("span", { class: "cell-sub" }, "加载中…"));
  tr.appendChild(ruleCell);
  return tr;
}

function holdingsTable(valuation, listings) {
  const node = table(
    [
      { label: "股票" },
      { label: "数量" },
      { label: "平均成本" },
      { label: "最新收盘" },
      { label: "市值" },
      { label: "浮动盈亏" },
      { label: "权重" },
      { label: "规则" },
    ],
    orderByRecentActivity(valuation.holdings).map((holding) => holdingRow(holding, listings))
  );
  node.classList.remove("stack-table");
  node.classList.add("watch-table", "pf-table");
  const wrap = el("div", { class: "table-wrap" });
  wrap.appendChild(node);
  return wrap;
}

function kpiCard(labelText, value, unit, foot, tone = "") {
  const card = el("div", { class: "kpi" });
  card.appendChild(el("div", { class: "kpi-label" }, labelText));
  const number = el("div", { class: `kpi-value num ${tone}`.trim() }, value);
  if (unit) number.appendChild(el("small", {}, unit));
  card.appendChild(number);
  card.appendChild(el("div", { class: "kpi-foot" }, foot));
  return card;
}

// KPI 条只用估值里已有的数：总资产 / 现金 / 持仓市值 / 浮动盈亏（未知保持未知，不推断）。
function renderKpis(valuation) {
  const box = document.querySelector("#portfolio-kpis");
  if (!box) return;
  box.replaceChildren();
  const holdings = valuation.holdings || [];
  const total = valuation.total_assets === null || valuation.total_assets === undefined ? null : Number(valuation.total_assets);
  const cash = valuation.known_cash === null || valuation.known_cash === undefined ? null : Number(valuation.known_cash);
  box.appendChild(
    kpiCard(
      "总资产",
      total === null ? "—" : fmtDecimal(valuation.total_assets),
      total === null ? "" : "元",
      total === null ? "不可用（见备注）" : `估值 ${valuation.valuation_date}`
    )
  );
  let cashFoot = "现金未知";
  if (cash !== null) cashFoot = total ? `占总资产 ${((cash / total) * 100).toFixed(1)}%` : "已确认现金";
  box.appendChild(kpiCard("现金", cash === null ? "—" : fmtDecimal(valuation.known_cash), cash === null ? "" : "元", cashFoot));
  const unpriced = holdings.filter((holding) => holding.market_value === null || holding.market_value === undefined).length;
  const subtotal = valuation.market_value_subtotal;
  box.appendChild(
    kpiCard(
      "持仓市值",
      subtotal === null || subtotal === undefined ? "—" : fmtDecimal(subtotal),
      subtotal === null || subtotal === undefined ? "" : "元",
      `${holdings.length} 只${unpriced ? `，${unpriced} 只无价格` : ""}`
    )
  );
  const known = holdings.filter((holding) => holding.floating_pnl !== null && holding.floating_pnl !== undefined);
  let pnlValue = "—";
  let pnlFoot = holdings.length ? "成本未知，不推断" : "没有持仓";
  let tone = "zero";
  if (known.length) {
    const pnl = known.reduce((sum, holding) => sum + Number(holding.floating_pnl), 0);
    const cost = known.reduce((sum, holding) => sum + Number(holding.remaining_cost || 0), 0);
    pnlValue = signed(pnl);
    tone = direction(pnl);
    pnlFoot = cost > 0 ? `${pnl >= 0 ? "+" : ""}${((pnl / cost) * 100).toFixed(2)}% · 相对成本` : "相对成本";
    if (known.length < holdings.length) pnlFoot += `（${holdings.length - known.length} 只成本未知未计入）`;
  }
  box.appendChild(kpiCard("浮动盈亏", pnlValue, "", pnlFoot, tone));
  box.hidden = false;
}

function renderValuation(region, valuation, listings) {
  renderKpis(valuation);
  if (!valuation.holdings.length) {
    renderRegion(region, "empty");
  } else {
    lastValuation = valuation;
    lastListings = listings;
    renderRegion(region, "ready", holdingsTable(valuation, listings));
    mountHoldingRules(region, valuation);
  }
  const meta = document.querySelector("#portfolio-valuation-meta");
  if (meta) {
    meta.textContent = `估值日 ${valuation.valuation_date} · 账本 r${valuation.ledger_revision} · ${valuation.total_assets === null ? "总资产不可用（见备注）" : `总资产 ${fmtDecimal(valuation.total_assets)}`}`;
  }
  const notes = document.querySelector("#portfolio-valuation-notes");
  if (notes) {
    notes.replaceChildren();
    if (valuation.unknowns.length) {
      notes.appendChild(el("p", { class: "small faint" }, `未确定项：${valuation.unknowns.join("、")}`));
    }
    notes.appendChild(
      el("p", { class: "small faint" }, "权重与总资产只在覆盖完整、现金与数量已知、价格满足时效时给出；成本未知不阻断市值。")
    );
  }
  // 交易方向（买入/卖出）是用户确认的事实记录，不是本系统的操作建议。
  showFactHint();
}

// ---------------------------------------------------------------------------
// 持仓规则（WP8）：起草 / 审阅 / 批准 / 驳回，以及每日区间检测的当前区间。
// 规则只是研究承诺：批准规则不写入账本，区间事件也不产生任何交易动作。
// ---------------------------------------------------------------------------

const RULE_STATE_TEXT = Object.freeze({
  needs_template: "未选择模板",
  drafting: "起草中",
  ready: "规则待批准",
  failed: "起草失败",
  rejected: "已驳回",
  none: "已生效",
});

function ruleUrl(suffix = "") {
  return `/portfolio/rule${suffix}`;
}

function mountHoldingRules(region, valuation) {
  for (const holding of valuation.holdings) {
    const listingId = holding.listing_ref.id;
    const cell = region.querySelector(`[data-rule-listing="${listingId}"]`);
    if (!cell) continue;
    get(`${ruleUrl()}?listing_id=${listingId}`)
      .then((rule) => cell.replaceChildren(ruleCellContent(region, rule)))
      .catch(() => cell.replaceChildren(el("span", { class: "faint" }, "规则状态不可用")));
  }
}

function ruleCellContent(region, rule) {
  const wrap = el("span");
  const state = rule.state;
  const badgeClass = state === "ready" || state === "needs_template" || state === "failed" ? "badge warn" : "badge";
  if (state === "none" && rule.active) {
    wrap.appendChild(el("span", { class: "badge" }, `规则已生效 v${rule.active.version}`));
  } else {
    wrap.appendChild(el("span", { class: badgeClass }, RULE_STATE_TEXT[state] || state));
  }
  const review = el("button", { type: "button", class: "link-button" }, "审阅");
  review.addEventListener("click", () => toggleHoldingRule(region, rule.listing_id, review));
  wrap.appendChild(review);
  wrap.appendChild(zoneEventLine(rule.zone_events));
  const zones = (rule.zone_events && rule.zone_events.zones) || {};
  if (zones.exit_review && zones.exit_review.inside) {
    // funnel 9: 持仓复核区间是需要复核的提升级事件，行上常驻标记。
    wrap.appendChild(el("span", { class: "badge warn", title: "持仓复核区间触发中" }, "需复核"));
  }
  return wrap;
}

function toggleHoldingRule(region, listingId, button) {
  const row = button.closest("tr");
  const existing = row.nextElementSibling;
  if (existing && existing.classList.contains("rule-detail-row")) {
    existing.remove();
    return;
  }
  for (const open of region.querySelectorAll("tr.rule-detail-row")) open.remove();
  const detail = el("tr", { class: "rule-detail-row" });
  const body = el("td", { colspan: "8" });
  body.appendChild(el("p", { class: "small faint" }, "正在加载规则状态…"));
  detail.appendChild(body);
  row.after(detail);
  // The panel always reads the current slot, so a draft that finished in the
  // background shows up without a page reload; the row's badge is refreshed
  // from the same read so the two can never disagree.
  get(`${ruleUrl()}?listing_id=${listingId}`)
    .then((rule) => {
      const cell = button.closest("td");
      if (cell) cell.replaceChildren(ruleCellContent(region, rule));
      body.replaceChildren(holdingRulePanel(region, rule));
    })
    .catch((error) =>
      body.replaceChildren(
        el("p", { class: "notice notice-warn" }, `规则状态不可用：${error.message}`)
      )
    );
}

function reloadHoldingRules() {
  const region = document.querySelector("#portfolio-holdings");
  if (region && lastValuation) renderValuation(region, lastValuation, lastListings);
}

function holdingRulePanel(region, rule) {
  const box = el("div", { class: "rule-review" });
  const feedback = ruleFeedback(box);
  const listingId = rule.listing_id;
  const heading = el("h3", {}, "持仓规则（add / trim / exit_review）");
  if (rule.requested_template_ref) {
    heading.appendChild(el("span", { class: "badge" }, rule.requested_template_ref));
  }
  box.appendChild(heading);
  if (rule.active) {
    const active = el("section", { class: "card rule-active" });
    active.appendChild(el("h3", {}, `当前生效规则 v${rule.active.version} · ${rule.active.template_ref}`));
    active.appendChild(zoneEventLine(rule.zone_events));
    const latest = rule.zone_events && rule.zone_events.latest;
    if (latest && (latest.conditions_met || []).length) {
      active.appendChild(el("p", { class: "small faint" }, eventsSummary(latest)));
    }
    active.appendChild(zonesBlock(rule.active));
    box.appendChild(active);
  }
  if (rule.draft) {
    const draft = el("section", { class: "card rule-draft" });
    draft.appendChild(el("h3", {}, `规则草稿 v${rule.draft.version} · ${rule.draft.template_ref}`));
    draft.appendChild(
      el("p", { class: "small faint" }, "区间条件按生效参数显示；批准后由每日区间检测按条件产生事件。")
    );
    draft.appendChild(zonesBlock(rule.draft));
    const editor = sharedParamEditor({
      rule: rule.draft,
      onSave: {
        feedback,
        submit(save, changed) {
          const promise = post(
            ruleUrl("/edit"),
            { listing_id: listingId, rule_instance_id: rule.draft.rule_instance_id, params: changed },
            { idempotency: false },
          )
            .then(() => reloadHoldingRules())
            .catch((error) => feedback.error(error.message));
          lockWhileBusy(save, promise);
        },
      },
    });
    if (editor) draft.appendChild(editor);
    const actions = el("div", { class: "rule-actions" });
    const approve = el("button", { type: "button", class: "primary" }, "批准该版本");
    approve.addEventListener("click", () => {
      const promise = post(
        ruleUrl("/approve"),
        {
          listing_id: listingId,
          rule_instance_id: rule.draft.rule_instance_id,
          expected_active_rule_instance_id: rule.active ? rule.active.rule_instance_id : null,
        },
        { idempotency: false },
      )
        .then(() => reloadHoldingRules())
        .catch((error) => feedback.error(error.message));
      lockWhileBusy(approve, promise);
    });
    actions.appendChild(approve);
    const reason = el("input", { type: "text", placeholder: "驳回理由（必填）", maxlength: "500" });
    const reject = el("button", { type: "button", class: "secondary" }, "驳回");
    reject.addEventListener("click", () => {
      if (!reason.value.trim()) {
        feedback.error("驳回需要填写理由。");
        return;
      }
      const promise = post(
        ruleUrl("/reject"),
        { listing_id: listingId, rule_instance_id: rule.draft.rule_instance_id, reason: reason.value.trim() },
        { idempotency: false },
      )
        .then(() => reloadHoldingRules())
        .catch((error) => feedback.error(error.message));
      lockWhileBusy(reject, promise);
    });
    actions.appendChild(reason);
    actions.appendChild(reject);
    draft.appendChild(actions);
    box.appendChild(draft);
  } else if (["needs_template", "failed", "rejected"].includes(rule.state)) {
    if (rule.run && rule.run.error) {
      box.appendChild(el("p", { class: "notice notice-warn" }, `上次起草失败：${rule.run.error}`));
    }
    if (rule.state === "rejected" && rule.last_decision && rule.last_decision.reason) {
      box.appendChild(el("p", { class: "small faint" }, `已驳回：${rule.last_decision.reason}`));
    }
    box.appendChild(holdingTemplateChooser(rule, feedback));
  } else if (rule.state === "drafting") {
    box.appendChild(el("p", { class: "small faint" }, "规则草稿生成中；完成后在持仓池刷新审阅。"));
  } else if (!rule.active) {
    box.appendChild(el("p", { class: "small faint" }, "该持仓还没有规则；选择模板起草一条持仓规则。"));
    box.appendChild(holdingTemplateChooser(rule, feedback));
  }
  if (rule.active) {
    box.appendChild(
      el("p", { class: "small faint" }, "已生效的版本不会被改写：重新起草或修改参数都生成新版本，批准后旧版本退休。")
    );
  }
  return box;
}

function holdingTemplateChooser(rule, feedback) {
  const box = el("div", { class: "card rule-chooser" });
  box.appendChild(el("h4", {}, "选择起草模板"));
  box.appendChild(
    el("p", { class: "small faint" }, "未选择模板时不会提交起草任务；留空并选择模板后即可起草。")
  );
  const select = el("select", { "aria-label": "起草模板" });
  select.appendChild(el("option", { value: "" }, "请选择模板…"));
  const start = el("button", { type: "button", class: "primary", disabled: "" }, "起草");
  const submit = () => {
    if (!select.value) {
      feedback.error("请先选择模板。");
      return;
    }
    const promise = post(
      ruleUrl("/draft"),
      { listing_id: rule.listing_id, template_id: select.value },
      { idempotency: false },
    )
      .then(() => reloadHoldingRules())
      .catch((error) => feedback.error(error.message));
    lockWhileBusy(start, promise);
  };
  start.addEventListener("click", submit);
  select.addEventListener("change", () => {
    if (select.value) start.removeAttribute("disabled");
    else start.setAttribute("disabled", "");
  });
  box.appendChild(select);
  box.appendChild(start);
  get("/rule-templates")
    .then((templates) => {
      for (const item of templates || []) {
        const ref = `${item.id}@v${item.version}`;
        select.appendChild(el("option", { value: item.id, title: `${ref} · ${item.calibration_status}` }, `${item.name || item.id}（${ref}）`));
      }
      if (rule.active) select.value = rule.active.template_id;
      if (select.value) start.removeAttribute("disabled");
    })
    .catch((error) => feedback.error(error.message));
  return box;
}

function realizedTable(snapshot, listings) {
  const rows = [];
  for (const record of snapshot.realized || []) {
    const tr = el("tr");
    tr.appendChild(stockCell(listings[record.listing_ref.id], record.listing_ref.id));
    tr.appendChild(el("td", {}, "卖出结转"));
    tr.appendChild(el("td", { class: "num" }, fmtDecimal(record.quantity, 4)));
    tr.appendChild(el("td", { class: "num" }, fmtDecimal(record.gross)));
    tr.appendChild(el("td", { class: "num" }, record.fee === null ? "费用未知" : fmtDecimal(record.fee)));
    tr.appendChild(el("td", { class: "num" }, record.cost_carried === null ? "成本未知" : fmtDecimal(record.cost_carried)));
    const realizedCell = el("td", { class: "num" });
    if (record.realized === null) realizedCell.appendChild(el("span", { class: "faint" }, "—"));
    else realizedCell.appendChild(el("span", { class: direction(record.realized) }, signed(record.realized)));
    tr.appendChild(realizedCell);
    tr.appendChild(el("td", { class: "small faint" }, record.cycle_opened_on ? `建仓 ${record.cycle_opened_on}` : "建仓日未知"));
    rows.push(tr);
  }
  for (const dividend of snapshot.dividends || []) {
    const tr = el("tr");
    tr.appendChild(stockCell(dividend.listing_ref ? listings[dividend.listing_ref.id] : undefined, dividend.listing_ref ? dividend.listing_ref.id : ""));
    tr.appendChild(el("td", {}, "现金分红"));
    tr.appendChild(el("td", { class: "num" }, "—"));
    tr.appendChild(el("td", { class: "num" }, fmtDecimal(dividend.amount)));
    tr.appendChild(el("td", { class: "num" }, "—"));
    tr.appendChild(el("td", { class: "num" }, "—"));
    tr.appendChild(el("td", { class: "num" }, "—"));
    tr.appendChild(el("td", { class: "small faint" }, "分红不降低取得成本"));
    rows.push(tr);
  }
  const node = table(
    [
      { label: "股票" },
      { label: "类型" },
      { label: "数量" },
      { label: "金额" },
      { label: "费用" },
      { label: "结转成本" },
      { label: "已实现盈亏" },
      { label: "周期" },
    ],
    rows
  );
  node.classList.add("pf-realized");
  const wrap = el("div", { class: "table-wrap" });
  wrap.appendChild(node);
  return wrap;
}

function archiveList(snapshot, listings) {
  // Cleared cycles end with a zero quantity: they leave the holdings table and
  // stay reviewable here with their realized result and cycle reference.
  const cleared = new Map();
  for (const record of snapshot.realized || []) {
    const key = String(record.listing_ref.id);
    cleared.set(key, {
      listingId: record.listing_ref.id,
      quantity: record.quantity,
      realized: record.realized,
      cycle: record.cycle_opened_on,
      dividends: 0,
    });
  }
  for (const dividend of snapshot.dividends || []) {
    const key = dividend.listing_ref ? String(dividend.listing_ref.id) : "";
    const entry = cleared.get(key);
    if (entry) entry.dividends += Number(dividend.amount);
  }
  const list = el("ul", { class: "portfolio-timeline pf-archive" });
  for (const entry of cleared.values()) {
    const item = el("li", { class: "pf-archive-row" });
    item.appendChild(stockBlock(listings[entry.listingId], entry.listingId));
    const facts = el("span", { class: "pf-archive-facts" });
    facts.appendChild(el("span", {}, `清仓数量 ${fmtDecimal(entry.quantity, 4)}`));
    if (entry.realized === null) {
      facts.appendChild(el("span", {}, "已实现盈亏不可用（成本未知）"));
    } else {
      const realized = el("span", {});
      realized.appendChild(document.createTextNode("已实现 "));
      realized.appendChild(el("b", { class: direction(entry.realized) }, signed(entry.realized)));
      facts.appendChild(realized);
    }
    if (entry.dividends) facts.appendChild(el("span", {}, `累计分红 ${fmtDecimal(entry.dividends)}`));
    facts.appendChild(el("span", {}, entry.cycle ? `原建仓 ${entry.cycle}` : "建仓日未知"));
    item.appendChild(facts);
    list.appendChild(item);
  }
  return list;
}

function renderRealized(snapshot, listings) {
  const region = document.querySelector("#portfolio-realized");
  if (!region) return;
  const empty = !(snapshot.realized || []).length && !(snapshot.dividends || []).length;
  if (empty) renderRegion(region, "empty");
  else renderRegion(region, "ready", realizedTable(snapshot, listings));
  // Never present a ledger that only has an opening balance as a full history.
  const note = el("p", { class: "small faint" }, snapshot.coverage === "complete"
    ? "已实现盈亏与分红来自用户确认的事实；未知金额不会以行情推断。"
    : "资产范围不完整：已实现与分红只覆盖已确认的事实，不代表完整历史收益。");
  region.appendChild(note);
  const archive = document.querySelector("#portfolio-archive");
  if (archive) {
    if (!(snapshot.realized || []).length) renderRegion(archive, "empty");
    else renderRegion(archive, "ready", archiveList(snapshot, listings));
  }
}

function renderHoldingsEmpty(region, message) {
  renderRegion(region, "empty", message);
}

async function loadHoldings(account, token = epoch) {
  const region = document.querySelector("#portfolio-holdings");
  if (!region || !account) return;
  renderRegion(region, "loading");
  try {
    const valuation = await get(`/portfolio/valuations/current?account_id=${encodeURIComponent(account)}`);
    const listings = await loadListings(valuation.holdings.map((item) => item.listing_ref.id));
    if (token !== epoch) return;
    renderValuation(region, valuation, listings);
  } catch (error) {
    if (token !== epoch) return;
    if (error instanceof ApiError && error.status === 404) {
      renderHoldingsEmpty(region, "该账户还没有余额事实，请先录入期初余额。");
      return;
    }
    if (error instanceof ApiError && error.status === 403) {
      renderDisabled();
      renderHoldingsEmpty(region, "持仓账本写入关闭；已有事实仍可查看。");
      return;
    }
    if (error instanceof ApiError && error.status === 409) {
      renderRegion(region, "error", "估值更新中：账本刚变化，请刷新重试。");
      return;
    }
    renderRegion(region, "error", error.message);
  }
}

async function loadEvents(account, token = epoch, afterRevision = 0, list = null, count = 0, observedRevision = null) {
  const region = document.querySelector("#portfolio-events");
  if (!region || !account) return;
  if (!list) renderRegion(region, "loading");
  try {
    const page = await get(`/portfolio/events?account_id=${encodeURIComponent(account)}&limit=20&after_revision=${afterRevision}`);
    if (token !== epoch) return;
    if (observedRevision !== null && page.observed_revision !== observedRevision) throw new Error("账本已变化，请刷新后重新查看完整时间线。");
    const meta = document.querySelector("#portfolio-events-meta");
    count += page.items.length;
    if (meta) meta.textContent = `账本 r${page.observed_revision} · 按录入顺序已加载 ${count} 条`;
    if (!page.items.length) {
      renderRegion(region, "empty");
      return;
    }
    list = list || el("ul", { class: "portfolio-timeline pf-events" });
    for (const item of page.items) {
      const row = el("li", { class: "pf-event" });
      const check = el("input", { type: "checkbox", "aria-label": `选择事实 r${item.revision} 用于纠错` });
      check.checked = selectedEvents.has(item.id);
      check.addEventListener("change", () => {
        if (check.checked) selectedEvents.set(item.id, item.request.fact.occurred_at);
        else selectedEvents.delete(item.id);
        refreshCorrectionSelection();
      });
      row.appendChild(check);
      // 发生时间是主信息；录入时间放在下面一行小字。
      const when = el("span", { class: "pf-when" });
      when.appendChild(el("span", { class: "num", title: "发生时间" }, formatDateTime(item.request.fact.occurred_at)));
      when.appendChild(el("small", {}, `录入 ${formatDateTime(item.recorded_at)}`));
      row.appendChild(when);
      row.appendChild(el("span", { class: "badge pf-kind" }, KIND_LABELS[item.request.fact.kind] || item.request.fact.kind));
      row.appendChild(el("span", { class: "pf-sum" }, factSummary(item.request.fact)));
      row.appendChild(el("span", { class: "pf-rev faint small" }, `r${item.revision}`));
      list.appendChild(row);
    }
    // The newest page carries the change-driven ordering signal: facts whose
    // recorded time is within the last day mark their listing as recently moved.
    const cutoff = Date.now() - 24 * 60 * 60 * 1000;
    const moved = new Set();
    for (const item of page.items) {
      const recorded = Date.parse(item.recorded_at);
      if (Number.isFinite(recorded) && recorded >= cutoff && CHANGE_KINDS.has(item.request.fact.kind)) {
        for (const ref of [item.request.fact.holding?.listing_ref, item.request.fact.listing_ref, ...(item.request.fact.holdings || []).map((h) => h.listing_ref)]) {
          if (ref) moved.add(String(ref.id));
        }
        if (item.request.fact.kind === "trade" && item.request.fact.holding) moved.add(String(item.request.fact.holding.listing_ref.id));
      }
    }
    if (moved.size !== recentActivity.size || [...moved].some((id) => !recentActivity.has(id))) {
      recentActivity = moved;
      // Re-render through renderValuation (not just the table) so the inline
      // rule cells are remounted; the fast path used to leave them on 加载中….
      const holdingsRegion = document.querySelector("#portfolio-holdings");
      if (lastValuation && holdingsRegion) renderValuation(holdingsRegion, lastValuation, lastListings);
    }
    const block = el("div");
    block.append(list);
    if (page.next_after_revision !== null) {
      const more = el("button", { type: "button", class: "secondary pf-more" }, "加载更多事实");
      more.addEventListener("click", () => lockWhileBusy(more, loadEvents(account, token, page.next_after_revision, list, count, page.observed_revision)));
      block.append(more);
    }
    renderRegion(region, "ready", block);
  } catch (error) {
    if (token !== epoch) return;
    if (error instanceof ApiError && error.status === 403) {
      renderDisabled();
      renderRegion(region, "empty");
      return;
    }
    renderRegion(region, "error", error.message);
  }
}

async function resolveListing(ticker, name) {
  const query = `ticker=${encodeURIComponent(ticker)}${name ? `&name=${encodeURIComponent(name)}` : ""}`;
  return get(`/portfolio/listing?${query}`);
}

function showFactHint() {
  const hint = document.querySelector("#portfolio-fact-hint");
  if (!hint) return;
  hint.textContent =
    "提交前请核对：事实按发生时间回放；确定数量不足的卖出会被拒绝；费用留空表示未知（会影响现金与已实现盈亏，但不会阻断市值）。";
}

export function buildFact(form) {
  const kind = text(form, "kind");
  const time = text(form, "occurred_at");
  if (!time) throw new Error("请选择真实发生日期与时间。");
  const at = `${time}${text(form, "timezone") || "+08:00"}`;
  const source = text(form, "source");
  if (!source || !field(form, "user_confirmed").checked) throw new Error("请填写来源并明确确认已发生事实。");
  if (kind === "opening_balance" || kind === "reconciliation") {
    if (!field(form, "balance_confirmed").checked) throw new Error("请核对余额时点、资产范围和全部已知持仓。");
    const holdings = [];
    for (const row of form.querySelectorAll("[data-balance-row]")) {
      const ticker = text(row, "balance_ticker");
      const quantity = decimal(row, "balance_quantity");
      const cost = decimal(row, "balance_cost");
      const opened = text(row, "balance_opened_on");
      if (!ticker) {
        if (quantity !== null || cost !== null || opened !== null) throw new Error("持仓行有数值但未填写标的。");
        continue;
      }
      if (quantity === null) throw new Error("录入持仓需要数量。");
      holdings.push({ ticker, quantity, average_cost: cost, opened_on: opened });
    }
    if (!text(form, "asset_scope")) throw new Error("请明确余额覆盖的资产范围。");
    const difference = text(form, "unexplained_difference");
    if (kind === "reconciliation" && difference === null) throw new Error("对账需要填写差异说明（无差异填 none）。");
    return {
      kind,
      occurred_at: at,
      source,
      user_confirmed: true,
      coverage: text(form, "coverage") || "partial",
      asset_scope: text(form, "asset_scope"),
      holdings,
      cash: decimal(form, "cash"),
      unexplained_difference: kind === "reconciliation" ? difference : null,
      prior_history: "included_in_confirmed_balance",
    };
  }
  if (kind === "trade") {
    const ticker = text(form, "ticker");
    if (!ticker) throw new Error("交易需要标的。");
    const quantity = decimal(form, "quantity");
    const price = decimal(form, "price");
    if (quantity === null || price === null) throw new Error("交易需要数量与价格。");
    return {
      kind,
      occurred_at: at,
      source,
      user_confirmed: true,
      ticker,
      direction: text(form, "direction") || "buy",
      quantity,
      actual_price: price,
      fee: decimal(form, "fee"),
    };
  }
  if (kind === "corporate_action") {
    const ticker = text(form, "ticker");
    if (!ticker) throw new Error("公司行动需要标的。");
    const action = text(form, "action") || "split";
    const ratio = action === "split" ? decimal(form, "ratio") : null;
    if (action === "split" && ratio === null) throw new Error("比例变更需要拆分比例。");
    const affects = [];
    if (action === "split" || field(form, "affects_quantity").checked) affects.push("quantity");
    if (action === "pending" && field(form, "affects_cash").checked) affects.push("cash");
    return { kind, occurred_at: at, source, user_confirmed: true, ticker, action, ratio, affects };
  }
  if (kind === "cash_transfer" || kind === "dividend") {
    const amount = decimal(form, "amount");
    if (amount === null) throw new Error("需要金额。");
    const ticker = text(form, "ticker");
    if (kind === "dividend" && !ticker) throw new Error("分红需要标的。");
    return { kind, occurred_at: at, source, user_confirmed: true, ticker, amount };
  }
  throw new Error(`不支持的事实类型：${kind}`);
}

async function toFact(fact) {
  if (fact.ticker && fact.kind === "trade") {
    const listing = await resolveListing(fact.ticker);
    return {
      kind: "trade",
      occurred_at: fact.occurred_at,
      source: fact.source,
      user_confirmed: true,
      holding: { listing_ref: listing.listing_ref, quantity: String(fact.quantity) },
      direction: fact.direction,
      actual_price: String(fact.actual_price),
      fee: fact.fee === null ? null : String(fact.fee),
    };
  }
  if (fact.holdings) {
    const resolved = [];
    for (const holding of fact.holdings) {
      const listing = await resolveListing(holding.ticker);
      resolved.push({
        listing_ref: listing.listing_ref,
        quantity: String(holding.quantity),
        average_cost: holding.average_cost === null ? null : String(holding.average_cost),
        opened_on: holding.opened_on,
      });
    }
    return {
      kind: fact.kind,
      occurred_at: fact.occurred_at,
      source: fact.source,
      user_confirmed: true,
      coverage: fact.coverage,
      asset_scope: fact.asset_scope,
      holdings: resolved,
      cash: fact.cash === null ? null : String(fact.cash),
      unexplained_difference: fact.unexplained_difference,
      prior_history: fact.prior_history,
    };
  }
  if (fact.kind === "corporate_action") {
    const listing = await resolveListing(fact.ticker);
    return {
      kind: "corporate_action",
      occurred_at: fact.occurred_at,
      source: fact.source,
      user_confirmed: true,
      listing_ref: listing.listing_ref,
      action: fact.action,
      ratio: fact.ratio === null ? null : String(fact.ratio),
      affects: fact.affects,
      description: fact.action === "split" ? "用户确认的比例变更（拆股/送股）" : "用户确认的待处理公司行动",
    };
  }
  if (fact.kind === "dividend") {
    const listing = await resolveListing(fact.ticker);
    return {
      kind: "dividend",
      occurred_at: fact.occurred_at,
      source: fact.source,
      user_confirmed: true,
      amount: String(fact.amount),
      listing_ref: listing.listing_ref,
    };
  }
  return {
    kind: "cash_transfer",
    occurred_at: fact.occurred_at,
    source: fact.source,
    user_confirmed: true,
    amount: String(fact.amount),
  };
}

function randomKey() {
  return crypto.randomUUID();
}

const selectedEvents = new Map();

function refreshCorrectionSelection() {
  const label = document.querySelector("#portfolio-correction-selected");
  if (label) label.textContent = selectedEvents.size ? `已选择 ${selectedEvents.size} 条事实` : "未选择事实";
  const submit = document.querySelector("#portfolio-correction-submit");
  if (submit) submit.disabled = !selectedEvents.size || busy || !!pending || storageBlocked;
  const open = document.querySelector("#portfolio-correct-open");
  if (open) {
    open.disabled = !selectedEvents.size;
    open.textContent = selectedEvents.size ? `纠错所选事实（${selectedEvents.size}）` : "纠错所选事实";
  }
}

function currentRevision() {
  const select = document.querySelector("#portfolio-account");
  if (!select || !select.selectedOptions.length) return null;
  const option = select.selectedOptions[0];
  return option.dataset.revision ? Number(option.dataset.revision) : null;
}

async function loadAccounts(preselect) {
  const select = document.querySelector("#portfolio-account");
  if (!select) return;
  const token = ++epoch;
  currentSnapshot = null;
  for (const id of ["portfolio-valuation-meta", "portfolio-valuation-notes", "portfolio-events-meta", "portfolio-account-meta"]) document.getElementById(id)?.replaceChildren();
  hideKpis();
  try {
    const accounts = await get("/portfolio/accounts");
    if (token !== epoch) return;
    select.replaceChildren();
    for (const account of accounts) {
      const option = el("option", { value: account.id }, account.name);
      option.dataset.revision = String(account.revision);
      select.appendChild(option);
    }
    if (!accounts.length) {
      select.appendChild(el("option", { value: "" }, "（还没有账户）"));
      const region = document.querySelector("#portfolio-holdings");
      if (region) renderRegion(region, "empty", "先创建一个账户，然后录入期初余额。");
      setEmptyState(true);
      locks();
      return;
    }
    setEmptyState(false);
    if (preselect && accounts.some(item => item.id === preselect)) select.value = preselect;
    const account = select.value;
    syncFactSub(select);
    try {
      const snapshot = await get(`/portfolio/snapshot?account_id=${encodeURIComponent(account)}`);
      if (token !== epoch) return;
      currentSnapshot = snapshot;
      select.selectedOptions[0].dataset.revision = String(snapshot.revision);
      renderAccountMeta(snapshot);
    } catch (error) {
      if (token !== epoch) return;
      if (!(error instanceof ApiError && error.status === 404)) throw error;
    }
    await Promise.all([loadHoldings(account, token), loadEvents(account, token), renderSnapshotDetails(account, token)]);
  } catch (error) {
    if (token !== epoch) return;
    if (error instanceof ApiError && error.status === 403) {
      renderDisabled();
      return;
    }
    const region = document.querySelector("#portfolio-holdings");
    if (region) renderRegion(region, "error", error.message);
  }
}

function renderAccountMeta(snapshot) {
  const meta = document.getElementById("portfolio-account-meta");
  if (!meta) return;
  const parts = [`账本 r${snapshot.revision}`, snapshot.coverage === "complete" ? "覆盖完整" : "覆盖不完整（部分资产）"];
  if (snapshot.asset_scope) parts.push(snapshot.asset_scope);
  meta.textContent = parts.join(" · ");
}

// 抽屉副标题显示当前账户名，避免录入到别的账户。
function syncFactSub(select) {
  const sub = document.getElementById("portfolio-fact-sub");
  if (!sub || !select.selectedOptions || !select.selectedOptions.length) return;
  sub.textContent = `${select.selectedOptions[0].textContent} · 事实一经确认不可修改`;
}

async function renderSnapshotDetails(account, token) {
  try {
    const snapshot = await get(`/portfolio/snapshot?account_id=${encodeURIComponent(account)}`);
    if (token !== epoch) return;
    const refs = [
      ...(snapshot.holdings || []).map((item) => item.listing_ref.id),
      ...(snapshot.realized || []).map((item) => item.listing_ref.id),
      ...(snapshot.dividends || []).filter((item) => item.listing_ref).map((item) => item.listing_ref.id),
    ];
    const listings = await loadListings(refs);
    if (token !== epoch) return;
    renderRealized(snapshot, listings);
  } catch (error) {
    if (token !== epoch) return;
    if (error instanceof ApiError && [403, 404].includes(error.status)) {
      const region = document.querySelector("#portfolio-realized");
      if (region) renderRegion(region, "empty");
      return;
    }
    const region = document.querySelector("#portfolio-realized");
    if (region) renderRegion(region, "error", error.message);
  }
}


function factError(message) {
  renderRegion(document.querySelector("#portfolio-fact-result"), "error", message);
}

function createError(message) {
  renderRegion(document.querySelector("#portfolio-create-result"), "error", message);
}

function locks() {
  const locked = busy || !!pending || storageBlocked;
  document.querySelectorAll("#portfolio-create-form input, #portfolio-create-form button, #portfolio-fact-form input, #portfolio-fact-form select, #portfolio-fact-form button, #portfolio-account, #portfolio-refresh").forEach(node => { node.disabled = locked; });
  document.querySelectorAll("#portfolio-correction-reason, #portfolio-correction-ticker, #portfolio-correction-quantity, #portfolio-correction-price, #portfolio-correction-fee, #portfolio-correction-direction").forEach(node => { node.disabled = locked; });
  if (!hasAccounts) {
    const refresh = document.querySelector("#portfolio-refresh");
    if (refresh) refresh.disabled = true;
  }
  const retry = document.querySelector("#portfolio-pending-retry");
  retry.hidden = !pending;
  retry.disabled = busy || storageBlocked;
  const audit = document.querySelector("#portfolio-pending-audit");
  audit.hidden = !pending;
  audit.querySelector("pre").textContent = pending ? JSON.stringify(pending.request, null, 2) : "";
  if (!locked) updateKind();
}

function persist(path, request) {
  const fixed = { path, method: "POST", request: structuredClone(request) };
  // A failed storage write means the request is not admitted to the network.
  sessionStorage.setItem(PENDING_KEY, JSON.stringify(fixed));
  pending = fixed;
}

async function transmit() {
  if (!pending || busy || storageBlocked) return;
  const fixed = pending;
  busy = true;
  locks();
  try {
    const result = await post(fixed.path, fixed.request, { idempotency: false });
    sessionStorage.removeItem(PENDING_KEY);
    pending = null;
    if (fixed.path === "/portfolio/accounts") {
      document.querySelector("#portfolio-create-form").reset();
      document.querySelector("#portfolio-create-result")?.replaceChildren();
    }
    const form = document.querySelector("#portfolio-fact-form");
    field(form, "user_confirmed").checked = false;
    field(form, "balance_confirmed").checked = false;
    if (fixed.path === "/portfolio/corrections") {
      const ids = (fixed.request.reversals || []).map(id => `${id.slice(0, 8)}…`).join("、");
      selectedEvents.clear();
      document.querySelector("#portfolio-correction-reason").value = "";
      refreshCorrectionSelection();
      renderRegion(document.querySelector("#portfolio-correction-result"), "ready", el("p", {}, `纠错已提交并原子写入（冲销 ${ids || "无"}）；历史保留，页面只显示最终状态。`));
    } else if (fixed.path === "/portfolio/accounts") {
      // 账户创建成功：关闭抽屉，页面从空状态切到账户视图。
      document.querySelector("#portfolio-fact-result")?.replaceChildren();
      closeOverlay();
    } else {
      renderRegion(document.querySelector("#portfolio-fact-result"), "ready", el("p", {}, "服务器已确认保存账本事实；估值显示下方更新时间。"));
    }
    await loadAccounts(fixed.request.account_id || result.id);
  } catch (error) {
    const reporting = fixed.path === "/portfolio/corrections" ? correctionError : fixed.path === "/portfolio/accounts" ? createError : factError;
    if (error instanceof ApiError && [409, 422].includes(error.status)) {
      sessionStorage.removeItem(PENDING_KEY);
      pending = null;
      field(document.querySelector("#portfolio-fact-form"), "user_confirmed").checked = false;
      field(document.querySelector("#portfolio-fact-form"), "balance_confirmed").checked = false;
      if (error.status === 409) await loadAccounts(fixed.request.account_id);
      // renderRegion 的 error 态只接受字符串：必须传人话文案，不能传 errorText()
      // 的 DOM 节点（否则用户看到 "[object HTMLDetailsElement]"）。
      reporting(
        error.status === 409
          ? "账本已变化。原请求被拒绝；请按最新账户版本重新核对并确认，系统不会自动重发。"
          : describeIssues(error.detail).join("；") || error.message
      );
    } else {
      reporting(`${error.message} 原请求已保留，请重取待确认请求；不要重新录入同一事实。`);
    }
  } finally {
    busy = false;
    locks();
  }
}

function addBalanceRow(value = {}) {
  field(document.querySelector("#portfolio-fact-form"), "balance_confirmed").checked = false;
  const row = el("div", { class: "balance-row", "data-balance-row": "true" });
  for (const [name, label, type, initial] of [
    ["balance_ticker", "余额持仓标的", "text", value.ticker || ""],
    ["balance_quantity", "余额持仓数量", "number", value.quantity ?? ""],
    ["balance_cost", "余额平均取得成本（含费用，可空）", "number", value.average_cost ?? ""],
    ["balance_opened_on", "本轮建仓日（可空）", "date", value.opened_on || ""],
  ]) {
    const wrap = el("label", {}, label);
    wrap.append(el("input", { name, type, value: initial, step: type === "number" ? "any" : undefined, min: type === "number" ? "0" : undefined, "aria-label": label }));
    row.append(wrap);
  }
  const remove = el("button", { type: "button", class: "secondary" }, "删除持仓行");
  remove.addEventListener("click", () => { row.remove(); field(document.querySelector("#portfolio-fact-form"), "balance_confirmed").checked = false; });
  row.append(remove);
  document.querySelector("#portfolio-balance-rows").append(row);
}

async function populateBalance() {
  const snapshot = currentSnapshot, token = epoch;
  if (!snapshot) throw new Error("请先选择有余额事实的账户。");
  const listings = await loadListings(snapshot.holdings.map(item => item.listing_ref.id));
  if (token !== epoch || !["opening_balance", "reconciliation"].includes(text(document.querySelector("#portfolio-fact-form"), "kind"))) return;
  const form = document.querySelector("#portfolio-fact-form");
  document.querySelector("#portfolio-balance-rows").replaceChildren();
  for (const holding of snapshot.holdings) {
    if (holding.confirmed_quantity === "0") continue;
    addBalanceRow({ ticker: listings[holding.listing_ref.id]?.ticker || "", quantity: holding.confirmed_quantity, opened_on: holding.opened_on });
  }
  // Unchanged quantities retain exact server-side costs; never round a derived
  // repeating average and then submit the rounded value as a confirmed fact.
  field(form, "cash").value = snapshot.known_cash ?? "";
  field(form, "coverage").value = snapshot.coverage;
  field(form, "asset_scope").value = snapshot.asset_scope;
  field(form, "balance_confirmed").checked = false;
}

function updateKind() {
  const form = document.querySelector("#portfolio-fact-form");
  const kind = text(form, "kind"), balance = ["opening_balance", "reconciliation"].includes(kind);
  for (const chip of form.querySelectorAll("[data-kind-chip]")) {
    const on = chip.getAttribute("data-kind-chip") === kind;
    chip.classList.toggle("on", on);
    chip.setAttribute("aria-pressed", on ? "true" : "false");
  }
  document.querySelector("#portfolio-balance-editor").hidden = !balance;
  document.querySelectorAll("#portfolio-balance-editor input, #portfolio-balance-editor button").forEach(node => { node.disabled = !balance; });
  for (const name of ["ticker", "quantity", "price", "fee", "cash", "amount", "direction", "asset_scope", "coverage", "unexplained_difference", "action", "ratio", "affects_quantity", "affects_cash"]) {
    const node = field(form, name);
    const show = balance ? ["cash", "asset_scope", "coverage", "unexplained_difference"].includes(name)
      : kind === "trade" ? ["ticker", "quantity", "price", "fee", "direction"].includes(name)
      : kind === "corporate_action" ? ["ticker", "action", "ratio", "affects_quantity", "affects_cash"].includes(name)
      : name === "amount" || (kind === "dividend" && name === "ticker");
    node.hidden = !show;
    node.disabled = !show;
    if (node.id) form.querySelector(`label[for="${node.id}"]`).hidden = !show;
    const wrapping = node.closest("label");
    if (wrapping && !node.id) wrapping.hidden = !show;
    const fieldBox = node.closest(".field");
    if (fieldBox) fieldBox.hidden = !show;
  }
}

// 事实类型芯片与隐藏的 <select name="kind"> 双向同步：select 仍是唯一的数据来源。
function wireKindChips() {
  const group = document.querySelector("#portfolio-fact-form .pf-kinds");
  if (!group) return;
  group.addEventListener("click", (event) => {
    const chip = event.target.closest ? event.target.closest("[data-kind-chip]") : null;
    if (!chip || chip.disabled) return;
    const select = field(document.querySelector("#portfolio-fact-form"), "kind");
    if (!select || select.value === chip.getAttribute("data-kind-chip")) return;
    select.value = chip.getAttribute("data-kind-chip");
    select.dispatchEvent(new Event("change", { bubbles: true }));
  });
}

function wireDrawerTabs() {
  for (const tab of document.querySelectorAll("[data-pf-tab]")) {
    tab.addEventListener("click", () => showDrawerTab(tab.getAttribute("data-pf-tab")));
  }
  const fact = document.querySelector("#portfolio-fact-open");
  if (fact) fact.addEventListener("click", () => showDrawerTab("fact"));
  const correct = document.querySelector("#portfolio-correct-open");
  if (correct) correct.addEventListener("click", () => showDrawerTab("correction"));
}

function wireCreateForm() {
  const form = document.querySelector("#portfolio-create-form");
  if (!form) return;
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const name = text(form, "name");
    if (!name || busy || pending || storageBlocked) return;
    try {
      persist("/portfolio/accounts", { idempotency_key: randomKey(), name });
      await transmit();
    } catch (error) { createError(`未发送请求：${error.message}`); }
  });
}

function correctionError(message) {
  renderRegion(document.querySelector("#portfolio-correction-result"), "error", message);
}

function wireCorrectionForm() {
  const submit = document.querySelector("#portfolio-correction-submit");
  if (!submit) return;
  refreshCorrectionSelection();
  submit.addEventListener("click", async () => {
    if (busy || pending || storageBlocked) return;
    const account = document.querySelector("#portfolio-account");
    const accountId = account ? account.value : "";
    if (!accountId) {
      correctionError("请先创建并选择账户。");
      return;
    }
    const ids = [...selectedEvents.keys()];
    const reason = document.querySelector("#portfolio-correction-reason").value.trim();
    if (!ids.length) {
      correctionError("请先在时间线勾选要冲销的事实。");
      return;
    }
    if (!reason) {
      correctionError("纠错理由必填。");
      return;
    }
    const ticker = document.querySelector("#portfolio-correction-ticker").value.trim();
    try {
      const replacements = [];
      if (ticker) {
        const quantity = document.querySelector("#portfolio-correction-quantity").value.trim();
        const price = document.querySelector("#portfolio-correction-price").value.trim();
        if (!quantity || !price) throw new Error("替代事实需要数量与价格。");
        const fee = document.querySelector("#portfolio-correction-fee").value.trim();
        const listing = await resolveListing(ticker);
        const occurred = [...selectedEvents.values()].sort()[0];
        replacements.push({
          kind: "trade",
          occurred_at: occurred,
          source: "user correction",
          user_confirmed: true,
          holding: { listing_ref: listing.listing_ref, quantity },
          direction: document.querySelector("#portfolio-correction-direction").value,
          actual_price: price,
          fee: fee === "" ? null : fee,
        });
      }
      const snapshot = await get(`/portfolio/snapshot?account_id=${encodeURIComponent(accountId)}`);
      persist("/portfolio/corrections", {
        account_id: accountId,
        expected_revision: snapshot.revision,
        operation_id: randomKey(),
        reason,
        reversals: ids,
        replacements,
      });
      await transmit();
    } catch (error) {
      correctionError(`未发送请求：${error.message}`);
    }
  });
}

function wireFactForm() {
  const form = document.querySelector("#portfolio-fact-form");
  if (!form) return;
  showFactHint();
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (busy || pending || storageBlocked) return;
    const account = document.querySelector("#portfolio-account");
    const accountId = account ? account.value : "";
    if (!accountId) {
      renderRegion(document.querySelector("#portfolio-fact-result"), "error", "请先创建并选择账户。");
      return;
    }
    const token = epoch, expected = currentRevision();
    busy = true;
    try {
      const input = buildFact(form); // Capture every field before asynchronous lookup.
      locks();
      const body = await toFact(input);
      if (token !== epoch || account.value !== accountId) throw new Error("账户版本或页面已变化，请重新核对。");
      persist("/portfolio/events", { account_id: accountId, expected_revision: expected, idempotency_key: randomKey(), fact: body });
    } catch (error) { factError(`未发送事实：${error.message}`); }
    finally { busy = false; locks(); }
    if (pending) await transmit();
  });
}

export function bootPortfolio() {
  if (!document.querySelector("#portfolio-account")) return;
  const select = document.querySelector("#portfolio-account");
  try {
    pending = JSON.parse(sessionStorage.getItem(PENDING_KEY) || "null");
    if (pending && (!pending.request || pending.method !== "POST" || !["/portfolio/accounts", "/portfolio/events", "/portfolio/corrections", "/portfolio/purposes", "/investor/actions", "/investor/decisions"].includes(pending.path))) throw new Error("待确认请求格式错误");
  } catch {
    storageBlocked = true;
    factError("浏览器待确认请求无法读取。录入已锁定，请先恢复存储并核对原请求。");
  }
  select.addEventListener("change", () => {
    const form = document.querySelector("#portfolio-fact-form");
    form.reset();
    field(form, "user_confirmed").checked = false;
    field(form, "balance_confirmed").checked = false;
    document.querySelector("#portfolio-balance-rows").replaceChildren();
    addBalanceRow();
    updateKind();
    syncFactSub(select);
    loadAccounts(select.value);
  });
  const refresh = document.querySelector("#portfolio-refresh");
  if (refresh) {
    refresh.addEventListener("click", () => {
      const promise = loadAccounts(select.value);
      lockWhileBusy(refresh, promise);
    });
  }
  wireCreateForm();
  wireFactForm();
  wireCorrectionForm();
  addBalanceRow();
  document.querySelector("#portfolio-balance-add").addEventListener("click", () => addBalanceRow());
  document.querySelector("#portfolio-balance-load").addEventListener("click", () => populateBalance().catch(error => factError(error.message)));
  field(document.querySelector("#portfolio-fact-form"), "kind").addEventListener("change", () => {
    updateKind();
    field(document.querySelector("#portfolio-fact-form"), "balance_confirmed").checked = false;
    if (text(document.querySelector("#portfolio-fact-form"), "kind") === "reconciliation" && currentSnapshot) populateBalance().catch(error => factError(error.message));
  });
  document.querySelector("#portfolio-pending-retry").addEventListener("click", transmit);
  wireKindChips();
  wireDrawerTabs();
  locks();
  if (pending) {
    factError("有上次待确认的请求。请重取原请求后再录入新事实。");
    // 录入已锁定：直接打开抽屉，让“重取待确认请求”可见。
    openFactDrawerWhenReady();
  }
  loadAccounts(pending?.request?.account_id);
}
