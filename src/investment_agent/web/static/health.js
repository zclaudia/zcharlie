// 数据健康页（P1-E2）：快照积累进度、数据新鲜度、各采集任务完整率与缺失清单、
// 成分与行业同步、历史回补、iFinD 预算与核对队列、字段冲突、逾期财报、
// 上游调用（各免费数据源 24 小时调用 / 失败次数与熔断状态）。
// 只渲染 GET /health/data 的确定性结果；不打分、不给操作判断。
// 所有 DOM 经 dom.js 构造（textContent / setAttribute），不写 HTML 字符串；
// 进度条用原生 <meter> / <progress>，不写内联样式。

import { ApiError, get, post } from "./api.js";
import { clear, el, errorText, fmt, stockLink } from "./dom.js";
import { alertList, kpi } from "./ui.js";
import {
  INDUSTRY_SYSTEM_LABELS,
  PROVIDER_LABELS,
  VERIFICATION_STATUS_LABELS,
  VERIFICATION_TRIGGER_LABELS,
  fieldLabel,
  formatDateTime,
  humanizeDetail,
  label,
} from "./format.js";

export const HEALTH_REGIONS = new Set(["data-health"]);

const ENDPOINT = "/health/data";

export const KIND_LABELS = Object.freeze({
  universe_sync: "成分同步",
  industry_sync: "行业同步",
  market_snapshot: "日线快照",
  close_quote: "收盘快照",
  financial_snapshot: "财报快照",
  dividend_snapshot: "分红快照",
  earnings_event: "财报事件",
});

export const PART_LABELS = Object.freeze({
  summary: "按期汇总",
  cash_flow: "单股现金流",
  forecast: "业绩预告",
  schedule: "预约披露",
  segments: "主营构成",
  membership: "成分历史",
  industry: "行业历史",
  prices: "日线",
  financials: "财务",
  dividends: "分红",
});

export const MISSING_KIND_LABELS = Object.freeze({
  failed: "失败",
  stopped: "数据源停止",
  quota: "额度耗尽",
  no_data: "无数据",
  pending: "未披露",
});

export const RUN_STATUS_LABELS = Object.freeze({
  success: "成功",
  partial: "部分成功",
  failed: "失败",
  queued: "排队中",
  running: "运行中",
});

export const WEEK_STATUS_LABELS = Object.freeze({
  qualified: "达标",
  below_threshold: "完整率未达标",
  missing: "缺失",
  no_trading: "无交易日",
  in_progress: "本周进行中",
});

export const LEVEL_LABELS = Object.freeze({ error: "错误", warn: "警告", info: "提示" });

const SECTION_LABELS = Object.freeze({
  accumulation: "快照积累",
  freshness: "数据新鲜度",
  collections: "采集任务",
  universes: "目标池成分",
  industry: "行业分类",
  backfill: "历史回补",
  verification: "iFinD 核对",
  overdue: "逾期财报",
  providers: "上游调用",
  calendar: "交易日历",
});

// 数据源显示名：基础表统一来自 format.js；本页的“上游调用”按渠道统计，
// 需要把 akshare 说清是东财渠道（与 baostock 并列显示时避免混淆），故只在本页覆盖该条。
export const HEALTH_PROVIDER_LABELS = Object.freeze({
  ...PROVIDER_LABELS,
  akshare: "AkShare（东财）",
});

export const STOP_LABELS = Object.freeze({
  quota: "iFinD 额度耗尽，提前停止",
  provider_unavailable: "数据源连续失败，提前停止",
});

// ---- 纯格式化（无 DOM，可在 node 中测试） --------------------------------

export function percent(value, digits = 1) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  const text = (value * 100).toFixed(digits);
  return `${text.replace(/\.0+$/, "")}%`;
}

export function localTime(value) {
  if (typeof value !== "string" || !value) return "—";
  return formatDateTime(value);
}

export function collectionTitle(item) {
  const kind = label(KIND_LABELS, item.kind);
  const extra = [item.provider, item.part ? label(PART_LABELS, item.part) : null, item.scope]
    .filter(Boolean)
    .join(" · ");
  return extra ? `${kind}（${extra}）` : kind;
}

export function missingSummary(byKind) {
  if (!byKind || typeof byKind !== "object") return "";
  return Object.keys(MISSING_KIND_LABELS)
    .filter((kind) => byKind[kind])
    .map((kind) => `${MISSING_KIND_LABELS[kind]} ${byKind[kind]}`)
    .join("，");
}

export function accumulationText(accumulation) {
  if (!accumulation) return "快照积累 — 周";
  return `快照积累 ${accumulation.streak_weeks} / ${accumulation.target_weeks} 周`;
}

// 上游数据源状态：熔断中（含到期时刻）/ 最近失败 / 正常。
export function providerStatus(item, now = Date.now()) {
  if (!item || typeof item !== "object") return { kind: "unknown", text: "—" };
  const until = item.open_until ? new Date(item.open_until).getTime() : Number.NaN;
  if (Number.isFinite(until) && until > now) {
    return { kind: "open", text: `熔断中，至 ${localTime(item.open_until)}` };
  }
  if (item.failed_24h) {
    return { kind: "failed", text: `最近失败 ${localTime(item.last_error_at)}` };
  }
  return { kind: "ok", text: item.calls_24h ? "正常" : "24 小时内无调用" };
}

// ---- DOM 小件 ---------------------------------------------------------------

// 图标来自 health.html 的 <template id="health-icons">（服务端渲染的内联 SVG），克隆即用；
// 页面外（node 测试）取不到时返回 null，调用方直接略过。
function iconNode(name) {
  const tpl = globalThis.document && document.getElementById("health-icons");
  const holder = tpl && tpl.content ? tpl.content.querySelector(`[data-icon="${name}"]`) : null;
  return holder && holder.firstElementChild ? holder.firstElementChild.cloneNode(true) : null;
}

function meter(value, { max = 1, low, optimum = max, labelText, className } = {}) {
  const node = el("meter", {
    class: className,
    min: 0,
    max,
    value: typeof value === "number" && Number.isFinite(value) ? Math.min(Math.max(value, 0), max) : 0,
    low,
    high: low,
    optimum,
    "aria-label": labelText,
  });
  node.textContent = labelText || "";
  return node;
}

function bar(value, total, text, threshold) {
  const wrap = el("div", { class: "health-bar" });
  const ratio = total > 0 ? value / total : null;
  wrap.appendChild(meter(ratio, { low: threshold, labelText: text }));
  wrap.appendChild(el("span", { class: "health-bar-text num" }, text));
  return wrap;
}

function putValue(node, value) {
  if (value instanceof Node) node.appendChild(value);
  else node.textContent = fmt(value);
  return node;
}

// 标签在左、值在右的两列行（手机上也不折成上下堆叠）。
function rows(pairs) {
  const list = el("dl", { class: "health-rows" });
  for (const [key, value] of pairs) {
    const row = el("div", { class: "health-row" });
    row.appendChild(el("dt", {}, key));
    row.appendChild(putValue(el("dd"), value));
    list.appendChild(row);
  }
  return list;
}

// 小指标格：标签在上、值在下（每格一项），2~4 列自适应。
function facts(pairs) {
  const list = el("dl", { class: "facts health-facts" });
  for (const [key, value] of pairs) {
    const fact = el("div", { class: "fact" });
    fact.appendChild(el("dt", {}, key));
    fact.appendChild(putValue(el("dd", { class: "num" }), value));
    list.appendChild(fact);
  }
  return list;
}

function stat(labelText, value, { foot, kind = "" } = {}) {
  const node = el("div", { class: `stat ${kind}`.trim() });
  node.appendChild(el("div", { class: "stat-label" }, labelText));
  node.appendChild(putValue(el("div", { class: "stat-value num" }), value));
  if (foot) node.appendChild(el("div", { class: "stat-foot" }, foot));
  return node;
}

function subtitle(text) {
  return el("h3", { class: "health-sub" }, text);
}

function statusChip(status) {
  return el("span", { class: `status status-${status || "unknown"}` }, label(RUN_STATUS_LABELS, status));
}

function tickerNode(ticker) {
  return stockLink(ticker) || el("span", {}, "—");
}

function simpleTable(headers, body, { numeric = [] } = {}) {
  const wrap = el("div", { class: "table-wrap" });
  const node = el("table", { class: "health-tbl" });
  const head = el("tr");
  headers.forEach((header, index) => head.appendChild(el("th", { scope: "col", class: numeric.includes(index) ? "num" : undefined }, header)));
  node.appendChild(el("thead")).appendChild(head);
  const tbody = el("tbody");
  for (const cells of body) {
    const tr = el("tr");
    cells.forEach((cell, index) => {
      const td = el("td", { class: numeric.includes(index) ? "num" : undefined });
      putValue(td, cell);
      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  }
  node.appendChild(tbody);
  wrap.appendChild(node);
  return wrap;
}

function empty(text) {
  return el("p", { class: "muted health-empty" }, text);
}

// ---- 各分区 -----------------------------------------------------------------

const ALERT_VISIBLE = 5;

// 提醒文案按第一个“：”拆成标题与细节，与概览页的提醒同一规则。
function splitAlert(message) {
  const text = String(message ?? "");
  const match = /[：:]\s*/.exec(text);
  if (!match || match.index < 4 || match.index + match[0].length >= text.length) return [text, ""];
  return [text.slice(0, match.index), text.slice(match.index + match[0].length)];
}

function alertRow(warning) {
  const isAlert = ["error", "warn"].includes(warning.level);
  const row = el("li", { class: `attn-row health-alert-${warning.level}` });
  const icon = el("span", { class: `list-row-icon${isAlert ? " warn" : ""}`, "aria-hidden": "true" });
  const glyph = iconNode(isAlert ? "alert" : "info");
  if (glyph) icon.appendChild(glyph);
  row.appendChild(icon);
  const main = el("div", { class: "attn-main" });
  const [heading, detail] = splitAlert(warning.message);
  main.appendChild(el("b", { class: "attn-title" }, isAlert ? heading : `${label(LEVEL_LABELS, "info")}：${heading}`));
  if (detail) {
    const detailNode = el("small", { class: "attn-detail", title: "点击展开 / 收起", tabindex: "0" }, detail);
    const toggle = () => detailNode.classList.toggle("is-open");
    detailNode.addEventListener("click", toggle);
    detailNode.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        toggle();
      }
    });
    main.appendChild(detailNode);
  }
  row.appendChild(main);
  const known = warning.section && SECTION_LABELS[warning.section];
  if (known) {
    const link = el("a", { class: "btn btn-sm attn-act", href: `#health-${warning.section}`, "aria-label": `查看${SECTION_LABELS[warning.section]}` }, "查看");
    row.appendChild(link);
  }
  return row;
}

function renderWarnings(warnings) {
  const box = el("div", { class: "health-warnings-body" });
  if (!warnings.length) {
    box.appendChild(el("p", { class: "health-ok" }, "当前没有数据健康提醒。"));
    return box;
  }
  const rank = (level) => (level === "error" ? 0 : level === "warn" ? 1 : 2);
  const sorted = [...warnings].sort((a, b) => rank(a.level) - rank(b.level));
  const list = el("ul", { class: "attn-list" });
  const nodes = sorted.map((warning, index) => {
    const row = alertRow(warning);
    if (index >= ALERT_VISIBLE) row.hidden = true;
    list.appendChild(row);
    return row;
  });
  box.appendChild(list);
  if (nodes.length > ALERT_VISIBLE) {
    const more = el("button", { type: "button", class: "btn btn-ghost btn-sm health-alerts-more", "aria-expanded": "false" }, `展开其余 ${nodes.length - ALERT_VISIBLE} 条`);
    more.addEventListener("click", () => {
      const open = more.getAttribute("aria-expanded") !== "true";
      nodes.forEach((row, index) => {
        if (index >= ALERT_VISIBLE) row.hidden = !open;
      });
      more.setAttribute("aria-expanded", open ? "true" : "false");
      more.textContent = open ? "收起" : `展开其余 ${nodes.length - ALERT_VISIBLE} 条`;
    });
    box.appendChild(more);
  }
  return box;
}

function renderAccumulation(data) {
  const acc = data.accumulation;
  const box = el("div", { class: "stack" });
  const head = el("div", { class: "health-acc-head" });
  head.appendChild(el("span", { class: "health-acc-count num" }, String(acc.streak_weeks)));
  head.appendChild(el("span", { class: "muted" }, `/ ${acc.target_weeks} 周连续达标`));
  if (acc.reached) head.appendChild(el("span", { class: "status status-success" }, "已达成"));
  box.appendChild(head);
  const progress = el("progress", {
    class: "accumulation-progress",
    max: acc.target_weeks,
    value: Math.min(acc.streak_weeks, acc.target_weeks),
    "aria-label": accumulationText(acc),
  });
  progress.textContent = accumulationText(acc);
  box.appendChild(progress);
  box.appendChild(
    facts([
      ["首个实时快照", acc.first_live_trade_date],
      ["最近实时快照", acc.latest_live_trade_date],
      ["有快照的周数", String(acc.weeks_with_snapshots)],
      ["完整率达标的周数", String(acc.weeks_meeting_threshold)],
    ])
  );
  if (acc.weeks.length) {
    box.appendChild(
      simpleTable(
        ["周", "起始", "交易日", "已采", "最低完整率", "状态"],
        acc.weeks.map((week) => [
          week.week,
          week.start,
          String(week.trading_days),
          String(week.collected_days),
          percent(week.min_ratio),
          el("span", { class: `chip week-${week.status}` }, label(WEEK_STATUS_LABELS, week.status)),
        ]),
        { numeric: [2, 3, 4] }
      )
    );
  } else {
    box.appendChild(empty("尚无实时日线快照；计时从第一次日线采集开始。"));
  }
  const basis = el("details", { class: "basis" });
  basis.appendChild(el("summary", {}, "统计口径"));
  basis.appendChild(
    el(
      "p",
      { class: "muted small" },
      `${acc.universe_code} 日线快照（baostock，非回补）按交易日所在 ISO 周统计；` +
        `一周至少有一个交易日的快照、且当周最低完整率不低于 ${percent(acc.threshold, 0)} 即为达标。` +
        "无交易日的周与进行中的本周不计也不中断，其余未达标的周使连续周数归零。"
    )
  );
  box.appendChild(basis);
  return box;
}

function freshRow(what, ok, text, extra) {
  const row = el("li");
  row.appendChild(el("span", { class: `dot ${ok ? "dot-fresh" : "dot-stale"}`, "aria-hidden": "true" }));
  const main = el("span", { class: "health-fresh-main" });
  main.appendChild(el("span", { class: "fresh-what" }, what));
  main.appendChild(el("span", {}, text));
  row.appendChild(main);
  if (extra) row.appendChild(extra instanceof Node ? extra : el("small", { class: "faint health-fresh-note" }, extra));
  return row;
}

function renderFreshness(data) {
  const fresh = data.freshness;
  const market = fresh.market;
  const box = el("div", { class: "stack" });
  const list = el("ul", { class: "fresh-list health-fresh" });
  const lagKnown = market.lag_trading_days !== null && market.lag_trading_days !== undefined;
  list.appendChild(
    freshRow(
      "日线",
      lagKnown && market.lag_trading_days === 0,
      !lagKnown ? "—" : market.lag_trading_days === 0 ? "无落后" : `落后 ${market.lag_trading_days} 个交易日`,
      `最新 ${fmt(market.latest_trade_date)} · 应有 ${fmt(market.expected_trade_date)}`
    )
  );
  list.appendChild(
    freshRow(
      "收盘快照",
      Boolean(market.latest_close_quote_date) && market.latest_close_quote_date === market.last_trading_day,
      market.latest_close_quote_date ? `最新 ${fmt(market.latest_close_quote_date)}` : "尚无",
      "腾讯批量报价，15:10 写入当日收盘；日线到达后读取以 baostock 为准"
    )
  );
  const directory = fresh.directory || {};
  const dirAge = typeof directory.age_days === "number" ? directory.age_days : null;
  list.appendChild(
    freshRow(
      "股票字典",
      directory.synced_on ? dirAge !== null && dirAge <= 14 : false,
      directory.synced_on ? `同步于 ${fmt(directory.synced_on)}${dirAge !== null ? `（${dirAge} 天前）` : ""}` : "尚未同步",
      `${directory.total || 0} 只；随每周成分同步刷新，供代码 / 名称 / 拼音搜索`
    )
  );
  box.appendChild(list);
  const coverage = [
    [
      `日线覆盖（${market.universe_code}，baostock 不复权）`,
      bar(market.members_at_latest, market.members, `${market.members_at_latest}/${market.members} 成分`, data.threshold),
    ],
  ];
  if (fresh.financial.length) {
    for (const period of fresh.financial) {
      coverage.push([
        `财报覆盖 ${period.report_period}`,
        bar(period.listings, period.members, `${period.listings}/${period.members}（${percent(period.coverage)}）`, data.threshold),
      ]);
    }
  } else {
    coverage.push(["财报覆盖（baostock）", "当前成分股尚无财报快照。"]);
  }
  const dividend = fresh.dividend;
  coverage.push([
    `分红覆盖（${dividend.year} 年）`,
    bar(dividend.covered, dividend.members, `${dividend.covered}/${dividend.members}（${percent(dividend.coverage)}）`, data.threshold),
  ]);
  box.appendChild(rows(coverage));
  return box;
}

function missingDetails(run) {
  const details = el("details", { class: "missing-details" });
  const shown = run.missing.length;
  const more = run.missing_total > shown ? `，显示前 ${shown} 只` : "";
  details.appendChild(el("summary", {}, `缺失清单（${run.missing_total} 只${more}）`));
  details.appendChild(
    simpleTable(
      ["代码", "类型", "原因"],
      run.missing.map((item) => [tickerNode(item.ticker), label(MISSING_KIND_LABELS, item.kind), item.reason])
    )
  );
  return details;
}

// 采集任务的展开细节：缺失分类、停止原因、错误摘要、近几次完整率、缺失清单。没有可展开内容返回 null。
function collectionDetail(item) {
  const run = item.last_run;
  if (!run) return null;
  const box = el("div", { class: "health-coll-detail" });
  let any = false;
  const missing = missingSummary(run.missing_by_kind);
  if (missing) {
    box.appendChild(el("p", { class: "small" }, `缺失：${missing}`));
    any = true;
  }
  if (run.stop_reason) {
    box.appendChild(
      el("p", { class: "notice notice-warn small" }, `${label(STOP_LABELS, run.stop_reason)}：${fmt(run.stop_error)}`)
    );
    any = true;
  }
  if (run.error_summary && run.status !== "success") {
    const error = el("div", { class: "small error-text" });
    error.appendChild(errorText(run.error_summary));
    box.appendChild(error);
    any = true;
  }
  if (item.trend.length > 1) {
    const trend = el("p", { class: "trend small" });
    trend.appendChild(el("span", { class: "faint" }, `近 ${item.trend.length} 次`));
    for (const point of item.trend) {
      const text = typeof point.ratio === "number" ? percent(point.ratio) : label(RUN_STATUS_LABELS, point.status);
      trend.appendChild(
        el("span", { class: `trend-cell run-${point.status}`, title: `#${point.run_id} ${fmt(point.subject)}：${text}` }, text)
      );
    }
    box.appendChild(trend);
    any = true;
  }
  if (run.missing.length) {
    box.appendChild(missingDetails(run));
    any = true;
  }
  return any ? box : null;
}

function collectionName(item) {
  const kind = label(KIND_LABELS, item.kind);
  return item.part ? `${kind} · ${label(PART_LABELS, item.part)}` : kind;
}

function collectionSource(item) {
  return [item.provider, item.scope].filter(Boolean).join(" · ") || "—";
}

function runPercent(item) {
  const run = item.last_run;
  if (!run) return null;
  return typeof run.ratio === "number" ? run.ratio : null;
}

function runIsBad(item) {
  const run = item.last_run;
  return Boolean(run) && ["failed", "partial"].includes(run.status);
}

function statusCell(item) {
  const run = item.last_run;
  if (!run) return el("span", { class: "badge dot hollow" }, "未运行");
  return statusChip(run.status);
}

function coverageCell(item, threshold) {
  const run = item.last_run;
  if (!run) return el("span", { class: "muted" }, "—");
  const ratio = runPercent(item);
  if (ratio === null) {
    return el("span", { class: "muted num" }, typeof run.collected === "number" ? `${run.collected} 只` : "—");
  }
  const cell = el("div", { class: "health-cov" });
  cell.appendChild(el("b", { class: `num${ratio < threshold ? " below" : ""}` }, percent(ratio)));
  cell.appendChild(
    meter(ratio, { low: threshold, labelText: `${fmt(run.collected)}/${fmt(run.expected)}`, className: "health-cov-meter" })
  );
  return cell;
}

function runStamp(item) {
  const run = item.last_run;
  return run ? `#${run.run_id} · ${localTime(run.finished_at)}` : "—";
}

function collectionTableRow(item, threshold, tbody) {
  const tr = el("tr");
  tr.appendChild(el("td", { class: "health-coll-name" }, collectionName(item)));
  tr.appendChild(el("td", { class: "muted" }, collectionSource(item)));
  tr.appendChild(el("td")).appendChild(statusCell(item));
  tr.appendChild(el("td")).appendChild(coverageCell(item, threshold));
  tr.appendChild(el("td", { class: "num" }, runStamp(item)));
  tr.appendChild(el("td", { class: "num" }, item.in_flight ? String(item.in_flight) : "—"));
  const action = el("td", { class: "health-coll-action" });
  tbody.appendChild(tr);
  const detail = collectionDetail(item);
  if (detail) {
    const row = el("tr", { class: "health-detail-row", hidden: "hidden" });
    const cell = el("td", { colspan: "7" });
    cell.appendChild(detail);
    row.appendChild(cell);
    const toggle = el("button", { type: "button", class: "btn btn-ghost btn-sm", "aria-expanded": "false" }, "详情");
    toggle.addEventListener("click", () => {
      const open = row.hidden;
      row.hidden = !open;
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
      toggle.textContent = open ? "收起" : "详情";
    });
    action.appendChild(toggle);
    tr.appendChild(action);
    tbody.appendChild(row);
  } else {
    tr.appendChild(action);
  }
}

function collectionListRow(item, threshold) {
  const detail = collectionDetail(item);
  const wrap = el(detail ? "details" : "div", { class: "health-coll-m" });
  const head = el(detail ? "summary" : "div", { class: "list-row" });
  const run = item.last_run;
  const bad = runIsBad(item);
  head.appendChild(el("span", { class: `sdot${bad ? " warn" : run ? "" : " mute"}`, "aria-hidden": "true" }));
  const main = el("div", { class: "list-row-main" });
  main.appendChild(el("b", {}, collectionName(item)));
  const stamp = run ? `${collectionSource(item)} · #${run.run_id} · ${localTime(run.finished_at).slice(5, 10)}` : `${collectionSource(item)} · 未运行`;
  const sub = el("small", { class: bad ? "warn-text" : undefined }, bad ? `${collectionSource(item)} · ${label(RUN_STATUS_LABELS, run.status)}` : stamp);
  main.appendChild(sub);
  head.appendChild(main);
  const ratio = runPercent(item);
  const endText = ratio !== null ? percent(ratio) : run && typeof run.collected === "number" ? `${run.collected} 只` : "—";
  head.appendChild(el("span", { class: `list-row-end num${bad ? " warn-text" : ""}` }, endText));
  if (detail) {
    const chev = iconNode("chev");
    if (chev) {
      chev.classList.add("list-row-chev", "health-chev");
      head.appendChild(chev);
    }
  }
  wrap.appendChild(head);
  if (detail) {
    const body = el("div", { class: "health-coll-m-body" });
    body.appendChild(detail);
    wrap.appendChild(body);
  }
  return wrap;
}

function renderCollections(data) {
  const box = el("div");
  if (!data.collections.length) {
    box.appendChild(empty("尚无采集运行记录（历史回补见下方“历史回补”）。"));
    return box;
  }
  const wrap = el("div", { class: "table-wrap health-desktop" });
  const node = el("table", { class: "health-tbl health-coll-table" });
  const head = el("tr");
  ["任务", "来源 · 范围", "状态", "覆盖", "最近运行", "排队", ""].forEach((header, index) =>
    head.appendChild(el("th", { scope: "col", class: index === 4 || index === 5 ? "num" : undefined }, header))
  );
  node.appendChild(el("thead")).appendChild(head);
  const tbody = el("tbody");
  for (const item of data.collections) collectionTableRow(item, data.threshold, tbody);
  node.appendChild(tbody);
  wrap.appendChild(node);
  box.appendChild(wrap);
  const list = el("div", { class: "list-group health-mobile health-coll-list" });
  for (const item of data.collections) list.appendChild(collectionListRow(item, data.threshold));
  box.appendChild(list);
  return box;
}

function tickerList(title, tickers, total) {
  const details = el("details");
  details.appendChild(el("summary", {}, `${title} ${total}`));
  const list = el("p", { class: "ticker-list" });
  for (const ticker of tickers) {
    list.appendChild(tickerNode(ticker));
    list.appendChild(document.createTextNode(" "));
  }
  if (total > tickers.length) list.appendChild(el("span", { class: "muted" }, `……共 ${total} 只`));
  details.appendChild(list);
  return details;
}

function renderUniverses(data) {
  const box = el("div", { class: "stack" });
  for (const universe of data.universes) {
    const headline = el("div", { class: "health-acc-head" });
    headline.appendChild(el("span", { class: "health-acc-count num" }, String(universe.members)));
    headline.appendChild(el("span", { class: "muted" }, `只 · ${universe.code} 当前成员`));
    box.appendChild(headline);
    const sync = universe.last_sync;
    if (sync) {
      box.appendChild(
        rows([
          ["最近同步", `${localTime(sync.at)}（#${sync.run_id}，${label(RUN_STATUS_LABELS, sync.status)}）`],
          ["成分日期", sync.as_of],
          ["生效日期", sync.effective_date],
          ["本次变化", `调入 ${sync.added_count}，调出 ${sync.removed_count}`],
        ])
      );
      if (sync.added_count) box.appendChild(tickerList("调入", sync.added, sync.added_count));
      if (sync.removed_count) box.appendChild(tickerList("调出", sync.removed, sync.removed_count));
    } else {
      box.appendChild(empty("尚无成分同步记录。"));
    }
    if (universe.changes.length) {
      const history = el("details", { class: "basis" });
      history.appendChild(el("summary", {}, `成分变动历史（最近 ${universe.changes.length} 个生效日）`));
      history.appendChild(
        simpleTable(
          ["生效日", "调入", "调出"],
          universe.changes.map((change) => [change.date, String(change.added), String(change.removed)]),
          { numeric: [1, 2] }
        )
      );
      box.appendChild(history);
    }
  }
  return box;
}

function renderIndustry(data) {
  const industry = data.industry;
  const box = el("div", { class: "stack" });
  box.appendChild(
    rows([
      ["当前成员", `${industry.members} 只（${industry.universe_code}）`],
      ["申万 2021 已分类", bar(industry.sw2021, industry.members, `${industry.sw2021}/${industry.members}`, data.threshold)],
      ["仅证监会分类（回退）", String(industry.csrc_fallback)],
      ["未分类", String(industry.unclassified)],
    ])
  );
  box.appendChild(
    simpleTable(
      ["体系", "最近同步", "状态", "说明"],
      industry.systems.map((system) => [
        label(INDUSTRY_SYSTEM_LABELS, system.system),
        localTime(system.at),
        system.status ? statusChip(system.status) : "—",
        humanizeDetail(system.detail),
      ])
    )
  );
  return box;
}

function renderBackfill(data) {
  const backfill = data.backfill;
  const box = el("div", { class: "stack" });
  if (!backfill.as_of) {
    box.appendChild(empty(`${backfill.universe_code} 尚无历史回补计划。`));
  } else {
    box.appendChild(el("p", { class: "faint health-note" }, `${backfill.universe_code} 回补计划 · 截至 ${backfill.as_of}`));
    box.appendChild(
      simpleTable(
        ["部分", "状态", "进度", "任务", "说明"],
        backfill.parts.map((part) => {
          const statuses = Object.entries(part.statuses)
            .map(([status, count]) => `${label(RUN_STATUS_LABELS, status)} ${count}`)
            .join("，");
          const note = el("div", { class: "health-backfill-note" });
          if (part.detail) note.appendChild(el("span", { class: "small" }, humanizeDetail(part.detail)));
          if (part.failed) note.appendChild(tickerList("失败", part.failed_symbols, part.failed));
          if (!note.children.length) note.textContent = "—";
          return [
            label(PART_LABELS, part.part),
            el(
              "span",
              { class: `status status-${part.done >= part.total ? "success" : part.failed ? "failed" : "running"}` },
              part.done >= part.total ? "完成" : `${part.done}/${part.total}`
            ),
            bar(part.done, part.total, `${part.done}/${part.total}`, 1),
            `${part.jobs}${statuses ? `（${statuses}）` : ""}`,
            note,
          ];
        })
      )
    );
  }
  const akshare = Object.entries(backfill.akshare_financials || {});
  if (akshare.length) {
    box.appendChild(
      el(
        "p",
        { class: "small" },
        `东财财务回补运行：${akshare.map(([status, count]) => `${label(RUN_STATUS_LABELS, status)} ${count}`).join("，")}`
      )
    );
  }
  return box;
}

function renderVerification(data) {
  const verification = data.verification;
  const budget = verification.budget;
  const box = el("div", { class: "stack" });
  const stats = el("div", { class: "health-stats" });
  stats.appendChild(
    stat(`iFinD 预算（${budget.window_hours} 小时滚动）`, String(budget.remaining), {
      foot: `/ ${budget.cap} 次 · 已用 ${budget.used}`,
      kind: budget.remaining > 0 ? "" : "attention",
    })
  );
  stats.appendChild(stat("下次可用", budget.remaining > 0 ? "现在" : localTime(budget.next_free_at)));
  stats.appendChild(
    stat("字段冲突", String(verification.conflicts_total), { kind: verification.conflicts_total ? "attention" : "zero" })
  );
  box.appendChild(stats);
  box.appendChild(
    bar(budget.remaining, budget.cap, `剩余 ${budget.remaining} / ${budget.cap} 次（已用 ${budget.used}）`, 0.1)
  );
  const statuses = Object.entries(verification.by_status);
  const queue = [];
  if (statuses.length) {
    const cluster = el("div", { class: "cluster" });
    for (const [status, count] of statuses) {
      cluster.appendChild(el("span", { class: "chip" }, `${label(VERIFICATION_STATUS_LABELS, status)} ${count}`));
    }
    queue.push(["核对队列", cluster]);
  } else {
    queue.push(["核对队列", "核对队列为空。"]);
  }
  const triggers = Object.entries(verification.open_by_trigger);
  if (triggers.length) {
    queue.push([
      "未完成按触发原因",
      triggers.map(([trigger, count]) => `${label(VERIFICATION_TRIGGER_LABELS, trigger)} ${count}`).join("，"),
    ]);
  }
  box.appendChild(rows(queue));
  box.appendChild(subtitle(`字段冲突（${verification.conflicts_total}）`));
  if (!verification.conflicts.length) {
    box.appendChild(empty("近期没有数据源字段冲突。"));
  } else {
    const conflicts = el("details", { class: "conflicts basis" });
    const shown = verification.conflicts.length;
    conflicts.appendChild(
      el("summary", {}, `展开冲突清单${verification.conflicts_total > shown ? `（显示最近 ${shown} 条）` : ""}`)
    );
    box.appendChild(conflicts);
    conflicts.appendChild(
      simpleTable(
        ["代码", "字段", "报告期", "来源", "时间"],
        verification.conflicts.map((item) => [
          tickerNode(item.ticker),
          item.fields.map(fieldLabel).join("、"),
          item.report_period,
          item.source === "verification" ? "iFinD 核对" : item.backfill ? "东财交叉校验（回补）" : "东财交叉校验",
          localTime(item.at),
        ])
      )
    );
  }
  return box;
}

function renderOverdue(data) {
  const overdue = data.overdue;
  const box = el("div", { class: "stack" });
  if (!overdue.open) {
    box.appendChild(empty("没有逾期未披露的定期报告。"));
    return box;
  }
  box.appendChild(el("p", {}, `${overdue.open} 份报告逾期未披露${overdue.items.length < overdue.open ? `（显示 ${overdue.items.length} 份）` : ""}。`));
  box.appendChild(
    simpleTable(
      ["代码", "报告期", "法定截止", "发现日"],
      overdue.items.map((item) => [tickerNode(item.ticker), item.report_period, item.deadline, item.detected_on])
    )
  );
  return box;
}

function renderProviders(data) {
  const providers = Array.isArray(data.providers) ? data.providers : [];
  const box = el("div", { class: "stack" });
  if (!providers.length) {
    box.appendChild(empty("24 小时内没有上游调用记录。"));
    return box;
  }
  box.appendChild(
    simpleTable(
      ["数据源", "24h 调用", "失败", "熔断拒绝", "状态", "最近错误"],
      providers.map((item) => {
        const status = providerStatus(item);
        return [
          label(HEALTH_PROVIDER_LABELS, item.provider),
          String(item.calls_24h),
          String(item.failed_24h),
          String(item.skipped_24h ?? 0),
          el("span", { class: `status status-provider-${status.kind}` }, status.text),
          item.last_error ? `${item.last_error}（${localTime(item.last_error_at)}）` : "—",
        ];
      }),
      { numeric: [1, 2, 3] }
    )
  );
  const basis = el("details", { class: "basis" });
  basis.appendChild(el("summary", {}, "统计口径"));
  basis.appendChild(
    el(
      "p",
      { class: "muted small" },
      "按 provider_call 台账统计最近 24 小时：一次逻辑调用（含客户端内部重试）记一行；" +
        "熔断器打开后窗口内的调用不打上游，记为“熔断拒绝”；窗口 5 秒起按 5s / 15s / 45s / 2 分 / 5 分递增，成功即复位。"
    )
  );
  box.appendChild(basis);
  return box;
}

const RENDERERS = Object.freeze({
  warnings: (data) => renderWarnings(Array.isArray(data.warnings) ? data.warnings : []),
  accumulation: renderAccumulation,
  freshness: renderFreshness,
  collections: renderCollections,
  universes: renderUniverses,
  industry: renderIndustry,
  backfill: renderBackfill,
  verification: renderVerification,
  overdue: renderOverdue,
  providers: renderProviders,
});

// 折叠分区的一行摘要（手机上折叠时显示在标题下面，桌面显示在卡片标题右侧）。
const SUMMARIES = Object.freeze({
  accumulation: (data) => {
    const acc = data.accumulation;
    const last = acc.weeks.length ? acc.weeks[0] : null;
    return `${acc.streak_weeks} / ${acc.target_weeks} 周连续达标${last ? ` · ${last.week} ${label(WEEK_STATUS_LABELS, last.status)}` : ""}`;
  },
  freshness: (data) => {
    const lag = data.freshness.market.lag_trading_days;
    const text = lag === null || lag === undefined ? "日线 —" : lag === 0 ? "日线无落后" : `日线落后 ${lag} 日`;
    return `${text} · 收盘快照 ${fmt(data.freshness.market.latest_close_quote_date)}`;
  },
  universes: (data) => {
    const universe = data.universes[0];
    if (!universe) return "尚无成分";
    return `${universe.members} 只 · ${universe.code}${universe.last_sync ? ` · 同步于 ${localTime(universe.last_sync.at).slice(0, 10)}` : ""}`;
  },
  industry: (data) => `申万 2021 已分类 ${data.industry.sw2021}/${data.industry.members}`,
  backfill: (data) => {
    const backfill = data.backfill;
    if (!backfill.as_of) return "尚无回补计划";
    const done = backfill.parts.filter((part) => part.done >= part.total).length;
    return `${done}/${backfill.parts.length} 部分完成 · 截至 ${backfill.as_of}`;
  },
  verification: (data) => {
    const v = data.verification;
    return `预算 ${v.budget.remaining}/${v.budget.cap} · 字段冲突 ${v.conflicts_total}`;
  },
  overdue: (data) => (data.overdue.open ? `${data.overdue.open} 份逾期未披露` : "没有逾期未披露"),
  providers: (data) => {
    const providers = Array.isArray(data.providers) ? data.providers : [];
    if (!providers.length) return "24 小时内无调用";
    const failed = providers.reduce((sum, item) => sum + (item.failed_24h || 0), 0);
    return `${providers.length} 个数据源 · 24 小时失败 ${failed} 次`;
  },
});

// ---- 装配 -------------------------------------------------------------------

function sectionBodies() {
  return [...document.querySelectorAll("[data-health-section]")];
}

function setSummary(id, text) {
  const node = document.querySelector(`[data-health-sum="${id}"]`);
  if (node) node.textContent = text || "";
}

function applyHeads(data) {
  for (const [id, summarize] of Object.entries(SUMMARIES)) {
    try {
      setSummary(id, summarize(data));
    } catch {
      setSummary(id, "");
    }
  }
  const warnings = Array.isArray(data.warnings) ? data.warnings : [];
  const errors = warnings.filter((warning) => warning.level === "error").length;
  const warns = warnings.filter((warning) => warning.level === "warn").length;
  const counts = document.querySelector("#health-warnings [data-health-counts]");
  if (counts) {
    clear(counts);
    if (errors) counts.appendChild(el("span", { class: "badge warn" }, `${errors} 错误`));
    if (warns) counts.appendChild(el("span", { class: "badge warn" }, `${warns} 警告`));
    if (!errors && !warns) counts.appendChild(el("span", { class: "badge accent dot" }, "全部正常"));
  }
  const collections = document.querySelector("#health-collections [data-health-counts]");
  if (collections) {
    const items = Array.isArray(data.collections) ? data.collections : [];
    const bad = items.filter(runIsBad).length;
    collections.textContent = `${items.length} 项${bad ? ` · 异常 ${bad}` : ""}`;
  }
}

function renderStatus(region, state, message, retry) {
  clear(region);
  region.dataset.state = state;
  if (state === "loading") {
    region.appendChild(el("p", { class: "notice notice-loading" }, "正在加载…"));
  } else if (state === "error") {
    region.appendChild(el("p", { class: "notice notice-error", role: "alert" }, message || "加载失败。"));
    const button = el("button", { type: "button", class: "btn btn-sm" }, "重试");
    button.addEventListener("click", retry);
    region.appendChild(button);
  } else if (state === "ready") {
    region.appendChild(el("p", { class: "muted small" }, message));
  }
}

function summaryKpi(labelText, value, { href, suffix, foot, kind = "" } = {}) {
  const node = el(href ? "a" : "div", { class: `kpi ${kind}`.trim(), href });
  node.appendChild(el("div", { class: "kpi-label" }, labelText));
  const valueNode = el("div", { class: "kpi-value" }, value === null || value === undefined ? "—" : String(value));
  if (suffix) valueNode.appendChild(el("small", {}, suffix));
  node.appendChild(valueNode);
  if (foot) node.appendChild(el("div", { class: "kpi-foot" }, foot));
  return node;
}

// 顶部摘要：5 张 KPI 卡，点击跳到对应分区；生成时间等写进页头副标题。
function renderSummary(region, data) {
  clear(region);
  region.dataset.state = "ready";
  const warnings = Array.isArray(data.warnings) ? data.warnings : [];
  const errors = warnings.filter((warning) => warning.level === "error").length;
  const warns = warnings.filter((warning) => warning.level === "warn").length;
  const acc = data.accumulation;
  const market = data.freshness && data.freshness.market;
  const lag = market ? market.lag_trading_days : null;
  const budget = data.verification && data.verification.budget;
  const overdue = data.overdue ? data.overdue.open : 0;
  const grid = el("div", { class: "kpis kpis--5 health-kpis" });
  grid.appendChild(
    summaryKpi("提醒", warnings.length, {
      href: warnings.length ? "#health-warnings" : undefined,
      foot: warnings.length ? `错误 ${errors} · 警告 ${warns}` : "全部正常",
      kind: errors || warns ? "attention" : "zero",
    })
  );
  grid.appendChild(
    summaryKpi("日线新鲜度", lag === null || lag === undefined ? "—" : lag === 0 ? "无落后" : `落后 ${lag} 日`, {
      href: "#health-freshness",
      foot: market && market.latest_trade_date ? `最新 ${market.latest_trade_date}` : undefined,
      kind: lag ? "attention" : "",
    })
  );
  const weeks = summaryKpi("快照积累", acc.streak_weeks, { href: "#health-accumulation", suffix: `/ ${acc.target_weeks} 周` });
  const cells = el("div", { class: "week-progress", "aria-hidden": "true" });
  for (let index = 0; index < acc.target_weeks; index += 1) {
    cells.appendChild(el("i", { class: index < acc.streak_weeks ? "on" : undefined }));
  }
  weeks.appendChild(cells);
  grid.appendChild(weeks);
  if (budget) {
    grid.appendChild(
      summaryKpi("iFinD 预算", budget.remaining, {
        href: "#health-verification",
        suffix: `/ ${budget.cap}`,
        foot: budget.used ? `${budget.window_hours} 小时内已用 ${budget.used}` : "窗口内未使用",
        kind: budget.remaining > 0 ? "" : "attention",
      })
    );
  }
  grid.appendChild(
    summaryKpi("逾期财报", overdue, {
      href: "#health-overdue",
      foot: overdue ? "逾期未披露" : "无逾期未披露",
      kind: overdue ? "attention" : "zero",
    })
  );
  region.appendChild(grid);
  const meta = document.getElementById("health-meta");
  if (meta) {
    meta.textContent = `生成于 ${localTime(data.generated_at)}（${data.as_of}） · 完整率阈值 ${percent(data.threshold, 0)} · 目标池 ${data.target_universe}`;
  }
}

// 手机上折叠成一行列表、桌面展开成卡片；点提醒里的链接（#health-xxx）时自动展开目标分区。
const DESKTOP_QUERY = "(min-width: 56rem)";

function initFolds() {
  const query = globalThis.matchMedia ? globalThis.matchMedia(DESKTOP_QUERY) : null;
  const apply = () => {
    for (const details of document.querySelectorAll("details.health-details")) details.open = query ? query.matches : true;
  };
  apply();
  if (query && query.addEventListener) query.addEventListener("change", apply);
  globalThis.addEventListener("hashchange", openHashTarget);
}

function openHashTarget() {
  const hash = globalThis.location.hash;
  if (!hash || hash.length < 2) return;
  const target = document.getElementById(decodeURIComponent(hash.slice(1)));
  if (!target) return;
  const details = target.querySelector("details.health-details");
  if (details) details.open = true;
  target.scrollIntoView();
}

async function loadHealth(region) {
  renderStatus(region, "loading");
  for (const body of sectionBodies()) body.closest("section")?.setAttribute("hidden", "");
  let data;
  try {
    data = await get(region.dataset.endpoint || ENDPOINT);
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) {
      globalThis.location.href = "/app/login";
      return;
    }
    renderStatus(region, "error", error.message, () => loadHealth(region));
    return;
  }
  if (!data || typeof data !== "object" || !data.accumulation) {
    renderStatus(region, "error", "数据健康接口返回了无法识别的结构。", () => loadHealth(region));
    return;
  }
  for (const body of sectionBodies()) {
    const render = RENDERERS[body.dataset.healthSection];
    clear(body);
    if (!render) continue;
    try {
      body.appendChild(render(data));
    } catch (error) {
      body.appendChild(el("p", { class: "notice notice-error" }, "此分区渲染失败。"));
    }
    body.closest("section")?.removeAttribute("hidden");
  }
  renderSummary(region, data);
  applyHeads(data);
  openHashTarget();
}

// ---- 今日概览的"数据健康"入口 -------------------------------------------------

export function healthBucket(counts) {
  const href = counts.link || "/app/health";
  const card = el("section", { class: "card health-bucket" });
  const heading = el("div", { class: "card-head bucket-title" });
  heading.appendChild(el("h2", {}, "数据健康"));
  heading.appendChild(el("a", { href }, "查看数据健康 →"));
  card.appendChild(heading);
  const grid = el("div", { class: "kpis" });
  const number = (value) => (typeof value === "number" ? value : 0);
  for (const [text, value, alert] of [
    ["错误", number(counts.errors), true],
    ["警告", number(counts.warnings), true],
    ["提示", number(counts.infos), false],
  ]) {
    grid.appendChild(kpi(text, value, { href, kind: value ? (alert ? "attention" : "") : "zero" }));
  }
  const weeks = kpi("快照积累", number(counts.streak_weeks), { href, suffix: `/ ${number(counts.target_weeks) || 8} 周` });
  const progress = el("div", { class: "week-progress", "aria-hidden": "true" });
  for (let index = 0; index < (number(counts.target_weeks) || 8); index += 1) {
    progress.appendChild(el("i", { class: index < number(counts.streak_weeks) ? "on" : undefined }));
  }
  weeks.appendChild(progress);
  weeks.appendChild(
    el("div", { class: "faint" }, accumulationText({ streak_weeks: counts.streak_weeks, target_weeks: counts.target_weeks }))
  );
  grid.appendChild(weeks);
  card.appendChild(grid);
  return card;
}

// ---- 交易日历卡片（页面上唯一会写文件的操作） --------------------------------

const CALENDAR_ENDPOINT = "/calendar/status";
const CALENDAR_POLL_MS = 4000;
const CALENDAR_POLL_TRIES = 15;
const PROBE_OUTCOME_LABELS = Object.freeze({
  candidate: "发现新年度，已生成候选",
  not_published: "两源均未公布该年度",
  not_due: "未到核对窗口",
  up_to_date: "已是最新",
  conflict: "两源分歧，未写入",
  calendar_unavailable: "日历不可读",
  queued: "已排队",
  running: "核对中",
});

function calendarYearLine(status) {
  return status.years
    .map((item) =>
      `${item.year}：${item.closed_dates} 个休市日${
        item.verified_on
          ? `（${(item.verified_by || []).join("+")} 核对 ${fmt(item.verified_on)}）`
          : ""
      }`
    )
    .join(" · ");
}

function calendarVerified(status) {
  return status.years.length > 0 && status.years.every((item) => Boolean(item.verified_on));
}

function calendarCandidateBlock(candidate) {
  const block = el("div", { class: "calendar-candidate" });
  block.appendChild(
    el(
      "p",
      {},
      `候选 ${candidate.year}：${candidate.trading_days} 个交易日，${candidate.closed_dates.length} 个工作日休市`
    )
  );
  block.appendChild(
    el(
      "p",
      { class: "small muted" },
      `双源核对 ${(candidate.verified_by || []).join(" + ")}` +
        `${candidate.verified_on ? `（${fmt(candidate.verified_on)}）` : ""} · 文件 ${candidate.path}`
    )
  );
  if (candidate.closed_dates.length) {
    const details = el("details");
    details.appendChild(el("summary", {}, "休市日明细"));
    details.appendChild(el("p", { class: "small" }, candidate.closed_dates.join("、")));
    block.appendChild(details);
  }
  return block;
}

function calendarActions(status, reload) {
  const row = el("div", { class: "calendar-actions" });
  const messages = el("div", { class: "calendar-messages" });
  const say = (text, kind = "notice") => {
    clear(messages);
    messages.appendChild(el("p", { class: kind }, text));
  };
  const busy = (button, value) => {
    button.disabled = value;
    button.setAttribute("aria-busy", value ? "true" : "false");
  };

  const applyButton = el("button", { type: "button", class: "btn btn-sm btn-primary primary" }, "应用候选");
  const checkButton = el("button", { type: "button", class: "btn btn-sm secondary" }, "检查更新");
  const candidate = status.candidate;
  applyButton.disabled = !candidate || !status.writable;
  if (candidate && !status.writable) {
    applyButton.title = "数据目录不可写，无法应用候选";
  }

  applyButton.addEventListener("click", async () => {
    if (!candidate) return;
    const question =
      `把 ${candidate.year} 年写入交易日历？\n` +
      `${candidate.trading_days} 个交易日、${candidate.closed_dates.length} 个工作日休市（双源已核对）。`;
    if (!globalThis.confirm(question)) return;
    busy(applyButton, true);
    say("正在写入…");
    try {
      const result = await post("/calendar/apply", {});
      say(
        `已应用 ${result.added_years.join("、")} 年：写入 ${result.path}，候选文件已删除。` +
          "调度器下次触发即用新日历（数据目录优先，无需提交）。"
      );
      await reload();
    } catch (error) {
      busy(applyButton, false);
      say(error.message || "应用失败。", "notice notice-error");
    }
  });

  checkButton.addEventListener("click", async () => {
    busy(checkButton, true);
    say("已排队核对（baostock + AkShare/Sina）…");
    let queued;
    try {
      queued = await post("/calendar/check", {});
    } catch (error) {
      busy(checkButton, false);
      say(error.message || "排队失败。", "notice notice-error");
      return;
    }
    const deadline = Date.now() + CALENDAR_POLL_MS * CALENDAR_POLL_TRIES;
    while (Date.now() < deadline) {
      await new Promise((resolve) => setTimeout(resolve, CALENDAR_POLL_MS));
      let latest = null;
      try {
        latest = await get(CALENDAR_ENDPOINT);
      } catch {
        continue;
      }
      const probe = latest && latest.last_probe;
      if (probe && probe.run_id >= queued.run_id && !["queued", "running"].includes(probe.status)) {
        busy(checkButton, false);
        await reload();
        return;
      }
    }
    busy(checkButton, false);
    say("核对仍在进行（worker 可能没在运行）；稍后刷新本页查看结果。", "notice");
  });

  row.appendChild(applyButton);
  row.appendChild(checkButton);
  return { row, messages };
}

function renderCalendar(card, status, reload) {
  clear(card);
  const coverage = status.coverage;
  const end = coverage.end ? fmt(coverage.end) : "—";
  const remaining = coverage.expired ? "已过期" : `${coverage.days_remaining} 天`;
  const verified = calendarVerified(status);
  const badge = coverage.expired
    ? el("span", { class: "badge warn dot" }, "已过期")
    : status.candidate
      ? el("span", { class: "badge ai dot" }, "有待应用的候选")
      : verified
        ? el("span", { class: "badge accent dot" }, "已核对")
        : el("span", { class: "badge dot hollow" }, "未核对");
  const lead = el("div", { class: "health-cal-lead" });
  lead.appendChild(badge);
  card.appendChild(lead);
  card.appendChild(facts([["覆盖至", end], ["剩余", remaining]]));
  const years = calendarYearLine(status);
  const source = status.source === "data_dir" ? "数据目录副本" : "仓库基线";
  card.appendChild(
    el(
      "p",
      { class: "small muted health-cal-note" },
      `已审年份 ${status.years.map((item) => item.year).join(" / ") || "—"}。${years ? `${years}。` : ""}` +
        `生效文件 ${status.path}（${source}）· 基线 ${status.baseline}`
    )
  );
  if (status.candidate) {
    card.appendChild(calendarCandidateBlock(status.candidate));
  } else {
    card.appendChild(
      el("p", { class: "small muted" }, "没有待应用的候选：系统每周自动核对，也可以点「检查更新」立即核对。")
    );
  }
  const actions = calendarActions(status, reload);
  card.appendChild(actions.row);
  card.appendChild(actions.messages);
  const probe = status.last_probe;
  if (probe) {
    card.appendChild(
      el(
        "p",
        { class: "small muted" },
        `上次核对：${label(PROBE_OUTCOME_LABELS, probe.outcome || probe.status)}` +
          `${probe.finished_at ? `（${formatDateTime(probe.finished_at)}）` : ""}` +
          `${probe.summary ? ` · ${probe.summary}` : ""}`
      )
    );
  }
  setSummary("calendar", `覆盖至 ${end} · ${coverage.expired ? "已过期" : status.candidate ? "有候选" : verified ? "已核对" : `剩余 ${remaining}`}`);
}

async function loadCalendar() {
  const card = document.getElementById("calendar-card");
  if (!card) return;
  document.getElementById("health-calendar")?.removeAttribute("hidden");
  const reload = () => loadCalendar();
  try {
    const status = await get(CALENDAR_ENDPOINT);
    renderCalendar(card, status, reload);
  } catch (error) {
    clear(card);
    card.appendChild(
      el(
        "p",
        { class: "notice notice-error" },
        error instanceof ApiError ? error.message : "交易日历状态加载失败。"
      )
    );
  }
}

export function bootHealth() {
  const region = document.getElementById("data-health");
  if (region) initFolds();
  if (region) loadHealth(region);
  loadCalendar();
  const refresh = document.getElementById("health-refresh");
  if (refresh && region) {
    refresh.addEventListener("click", () => {
      loadHealth(region);
      loadCalendar();
    });
  }
}
