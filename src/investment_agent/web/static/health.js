// 数据健康页（P1-E2）：快照积累进度、数据新鲜度、各采集任务完整率与缺失清单、
// 成分与行业同步、历史回补、iFinD 预算与核对队列、字段冲突、逾期财报、
// 上游调用（各免费数据源 24 小时调用 / 失败次数与熔断状态）。
// 只渲染 GET /health/data 的确定性结果；不打分、不给操作判断。
// 所有 DOM 经 dom.js 构造（textContent / setAttribute），不写 HTML 字符串；
// 进度条用原生 <meter> / <progress>，不写内联样式。

import { ApiError, get } from "./api.js";
import { clear, el, errorText, fmt, stockLink } from "./dom.js";
import { alertList, kpi } from "./ui.js";
import {
  INDUSTRY_SYSTEM_LABELS,
  VERIFICATION_STATUS_LABELS,
  VERIFICATION_TRIGGER_LABELS,
  fieldLabel,
  humanizeDetail,
  label,
} from "./format.js";

export const HEALTH_REGIONS = new Set(["data-health"]);

const ENDPOINT = "/health/data";
const TIMEZONE = "Asia/Shanghai";

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
});

export const PROVIDER_LABELS = Object.freeze({
  baostock: "baostock",
  akshare: "AkShare（东财）",
  tencent: "腾讯行情",
  ifind: "iFinD",
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
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  const parts = new Intl.DateTimeFormat("zh-CN", {
    timeZone: TIMEZONE,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).formatToParts(parsed);
  const pick = (type) => (parts.find((part) => part.type === type) || {}).value || "";
  return `${pick("year")}-${pick("month")}-${pick("day")} ${pick("hour")}:${pick("minute")}`;
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

function meter(value, { max = 1, low, optimum = max, labelText } = {}) {
  const node = el("meter", {
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
  wrap.appendChild(el("span", { class: "health-bar-text" }, text));
  return wrap;
}

function kvList(pairs) {
  const list = el("dl", { class: "kv-list" });
  for (const [key, value] of pairs) {
    list.appendChild(el("dt", {}, key));
    const dd = el("dd");
    if (value instanceof Node) dd.appendChild(value);
    else dd.textContent = fmt(value);
    list.appendChild(dd);
  }
  return list;
}

function statusChip(status) {
  return el("span", { class: `status status-${status || "unknown"}` }, label(RUN_STATUS_LABELS, status));
}

function tickerNode(ticker) {
  return stockLink(ticker) || el("span", {}, "—");
}

function simpleTable(headers, rows) {
  const node = el("table", { class: "health-table" });
  const head = el("tr");
  for (const header of headers) head.appendChild(el("th", { scope: "col" }, header));
  node.appendChild(el("thead")).appendChild(head);
  const body = el("tbody");
  for (const cells of rows) {
    const tr = el("tr");
    cells.forEach((cell, index) => {
      const td = el("td", { "data-label": headers[index] });
      if (cell instanceof Node) td.appendChild(cell);
      else td.textContent = fmt(cell);
      tr.appendChild(td);
    });
    body.appendChild(tr);
  }
  node.appendChild(body);
  return node;
}

function empty(text) {
  return el("p", { class: "muted" }, text);
}

// ---- 各分区 -----------------------------------------------------------------

function renderWarnings(warnings) {
  const box = el("div", { class: "health-warnings" });
  if (!warnings.length) {
    box.appendChild(el("p", { class: "notice" }, "当前没有数据健康提醒。"));
    return box;
  }
  const alerts = warnings.filter((warning) => ["error", "warn"].includes(warning.level));
  if (alerts.length) {
    box.appendChild(
      alertList(
        alerts.map((warning) => ({
          level: warning.level,
          message: fmt(warning.message),
          link: warning.section && SECTION_LABELS[warning.section] ? `#health-${warning.section}` : null,
          linkText: warning.section && SECTION_LABELS[warning.section] ? `查看${SECTION_LABELS[warning.section]} →` : null,
        })),
        { title: "数据健康提醒" }
      )
    );
  }
  for (const warning of warnings.filter((item) => !["error", "warn"].includes(item.level))) {
    box.appendChild(el("p", { class: "notice" }, `${label(LEVEL_LABELS, "info")}：${fmt(warning.message)}`));
  }
  return box;
}

function renderAccumulation(data) {
  const acc = data.accumulation;
  const box = el("div");
  const head = el("div", { class: "accumulation-headline" });
  const count = el("span", { class: "accumulation-count" }, String(acc.streak_weeks));
  head.appendChild(count);
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
    kvList([
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
        ])
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
  row.appendChild(el("span", { class: "fresh-what" }, what));
  row.appendChild(el("span", {}, text));
  if (extra) row.appendChild(extra instanceof Node ? extra : el("span", { class: "faint" }, extra));
  return row;
}

function renderFreshness(data) {
  const fresh = data.freshness;
  const market = fresh.market;
  const box = el("div");
  const list = el("ul", { class: "fresh-list" });
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
  box.appendChild(el("h3", {}, `日线覆盖（${market.universe_code}，baostock 不复权）`));
  box.appendChild(
    bar(market.members_at_latest, market.members, `${market.members_at_latest}/${market.members} 成分`, data.threshold)
  );
  box.appendChild(el("h3", {}, "财报覆盖（baostock）"));
  if (fresh.financial.length) {
    box.appendChild(
      kvList(
        fresh.financial.map((period) => [
          period.report_period,
          bar(period.listings, period.members, `${period.listings}/${period.members}（${percent(period.coverage)}）`, data.threshold),
        ])
      )
    );
  } else {
    box.appendChild(empty("当前成分股尚无财报快照。"));
  }
  const dividend = fresh.dividend;
  box.appendChild(el("h3", {}, `分红覆盖（${dividend.year} 年）`));
  box.appendChild(
    bar(dividend.covered, dividend.members, `${dividend.covered}/${dividend.members}（${percent(dividend.coverage)}）`, data.threshold)
  );
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

function renderCollection(item, threshold) {
  const block = el("article", { class: "collection-item" });
  const head = el("div", { class: "collection-head" });
  head.appendChild(el("h3", { class: "collection-title" }, collectionTitle(item)));
  const run = item.last_run;
  if (run) head.appendChild(statusChip(run.status));
  if (item.in_flight) head.appendChild(el("span", { class: "status status-running" }, `${item.in_flight} 个排队/运行中`));
  block.appendChild(head);
  if (!run) {
    block.appendChild(empty("尚无已结束的运行。"));
    return block;
  }
  const meta = el("p", { class: "collection-meta muted small" });
  meta.textContent = `#${run.run_id} · 数据日 ${fmt(run.subject)} · 结束于 ${localTime(run.finished_at)}`;
  block.appendChild(meta);
  if (typeof run.ratio === "number") {
    const ratio = el("p", { class: "collection-ratio" });
    ratio.appendChild(el("span", { class: `collection-ratio-value${run.ratio < threshold ? " below" : ""}` }, percent(run.ratio)));
    ratio.appendChild(el("span", { class: "faint" }, ` 完整率 · ${fmt(run.collected)}/${fmt(run.expected)}`));
    block.appendChild(ratio);
    block.appendChild(
      bar(run.collected ?? 0, (run.expected ?? 0) - (run.missing_by_kind.pending || 0), `${fmt(run.collected)}/${fmt(run.expected)}`, threshold)
    );
  }
  const missing = missingSummary(run.missing_by_kind);
  if (missing) block.appendChild(el("p", { class: "small" }, `缺失：${missing}`));
  if (run.stop_reason) {
    block.appendChild(
      el("p", { class: "notice notice-warn small" }, `${label(STOP_LABELS, run.stop_reason)}：${fmt(run.stop_error)}`)
    );
  }
  if (run.error_summary && run.status !== "success") {
    const error = el("div", { class: "small error-text" });
    error.appendChild(errorText(run.error_summary));
    block.appendChild(error);
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
    block.appendChild(trend);
  }
  if (run.missing.length) block.appendChild(missingDetails(run));
  return block;
}

function renderCollections(data) {
  const box = el("div", { class: "collection-list" });
  if (!data.collections.length) {
    box.appendChild(empty("尚无采集运行记录（历史回补见下方“历史回补”）。"));
    return box;
  }
  for (const item of data.collections) box.appendChild(renderCollection(item, data.threshold));
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
  const box = el("div");
  for (const universe of data.universes) {
    const headline = el("p", { class: "accumulation-headline" });
    headline.appendChild(el("span", { class: "accumulation-count" }, String(universe.members)));
    headline.appendChild(el("span", { class: "muted" }, `只 · ${universe.code} 当前成员`));
    box.appendChild(headline);
    const sync = universe.last_sync;
    if (sync) {
      box.appendChild(
        kvList([
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
      box.appendChild(el("h4", {}, "成分变动历史（最近生效日）"));
      box.appendChild(
        simpleTable(
          ["生效日", "调入", "调出"],
          universe.changes.map((change) => [change.date, String(change.added), String(change.removed)])
        )
      );
    }
  }
  return box;
}

function renderIndustry(data) {
  const industry = data.industry;
  const box = el("div");
  box.appendChild(
    kvList([
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
  const box = el("div");
  if (!backfill.as_of) {
    box.appendChild(empty(`${backfill.universe_code} 尚无历史回补计划。`));
  } else {
    box.appendChild(el("p", { class: "faint" }, `${backfill.universe_code} 回补计划 · 截至 ${backfill.as_of}`));
    const list = el("div", { class: "backfill-list" });
    for (const part of backfill.parts) {
      const item = el("div", { class: "backfill-item" });
      const partHead = el("div", { class: "collection-head" });
      partHead.appendChild(el("h4", {}, label(PART_LABELS, part.part)));
      partHead.appendChild(
        el("span", { class: `status status-${part.done >= part.total ? "success" : part.failed ? "failed" : "running"}` }, part.done >= part.total ? "完成" : `${part.done}/${part.total}`)
      );
      item.appendChild(partHead);
      item.appendChild(bar(part.done, part.total, `${part.done}/${part.total}`, 1));
      const statuses = Object.entries(part.statuses)
        .map(([status, count]) => `${label(RUN_STATUS_LABELS, status)} ${count}`)
        .join("，");
      item.appendChild(el("p", { class: "small muted" }, `任务 ${part.jobs}${statuses ? `（${statuses}）` : ""}`));
      if (part.detail) item.appendChild(el("p", { class: "small" }, humanizeDetail(part.detail)));
      if (part.failed) item.appendChild(tickerList("失败", part.failed_symbols, part.failed));
      list.appendChild(item);
    }
    box.appendChild(list);
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
  const box = el("div");
  const kpis = el("div", { class: "kpis" });
  kpis.appendChild(
    kpi(`iFinD 预算（${budget.window_hours} 小时滚动）`, budget.remaining, {
      suffix: `/ ${budget.cap} 次`,
      kind: budget.remaining > 0 ? "" : "attention",
    })
  );
  kpis.appendChild(kpi("下次可用", budget.remaining > 0 ? "现在" : localTime(budget.next_free_at)));
  kpis.appendChild(kpi("字段冲突", verification.conflicts_total, { kind: verification.conflicts_total ? "attention" : "zero" }));
  box.appendChild(kpis);
  box.appendChild(
    bar(budget.remaining, budget.cap, `剩余 ${budget.remaining} / ${budget.cap} 次（已用 ${budget.used}）`, 0.1)
  );
  box.appendChild(el("h3", {}, "核对队列"));
  const statuses = Object.entries(verification.by_status);
  box.appendChild(
    statuses.length
      ? el("p", {}, statuses.map(([status, count]) => `${label(VERIFICATION_STATUS_LABELS, status)} ${count}`).join("，"))
      : empty("核对队列为空。")
  );
  const triggers = Object.entries(verification.open_by_trigger);
  if (triggers.length) {
    box.appendChild(
      el(
        "p",
        { class: "small" },
        `未完成按触发原因：${triggers.map(([trigger, count]) => `${label(VERIFICATION_TRIGGER_LABELS, trigger)} ${count}`).join("，")}`
      )
    );
  }
  box.appendChild(el("h3", {}, `字段冲突（${verification.conflicts_total}）`));
  if (!verification.conflicts.length) {
    box.appendChild(empty("近期没有数据源字段冲突。"));
  } else {
    const conflicts = el("details", { class: "conflicts" });
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
  const box = el("div");
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
  const box = el("div");
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
          label(PROVIDER_LABELS, item.provider),
          String(item.calls_24h),
          String(item.failed_24h),
          String(item.skipped_24h ?? 0),
          el("span", { class: `status status-provider-${status.kind}` }, status.text),
          item.last_error ? `${item.last_error}（${localTime(item.last_error_at)}）` : "—",
        ];
      })
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

// ---- 装配 -------------------------------------------------------------------

function sectionBodies() {
  return [...document.querySelectorAll("[data-health-section]")];
}

function renderStatus(region, state, message, retry) {
  clear(region);
  region.dataset.state = state;
  if (state === "loading") {
    region.appendChild(el("p", { class: "notice notice-loading" }, "正在加载…"));
  } else if (state === "error") {
    region.appendChild(el("p", { class: "notice notice-error", role: "alert" }, message || "加载失败。"));
    const button = el("button", { type: "button" }, "重试");
    button.addEventListener("click", retry);
    region.appendChild(button);
  } else if (state === "ready") {
    region.appendChild(el("p", { class: "muted small" }, message));
  }
}

// 顶部摘要：各分区一张 KPI 卡，点击跳到对应分区。
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
  const grid = el("div", { class: "kpis" });
  grid.appendChild(
    kpi("提醒", warnings.length, {
      href: warnings.length ? "#health-warnings" : undefined,
      suffix: warnings.length ? `错误 ${errors} · 警告 ${warns}` : "全部正常",
      kind: errors || warns ? "attention" : "zero",
    })
  );
  const weeks = kpi("快照积累", acc.streak_weeks, { href: "#health-accumulation", suffix: `/ ${acc.target_weeks} 周` });
  const cells = el("div", { class: "week-progress", "aria-hidden": "true" });
  for (let index = 0; index < acc.target_weeks; index += 1) {
    cells.appendChild(el("i", { class: index < acc.streak_weeks ? "on" : undefined }));
  }
  weeks.appendChild(cells);
  grid.appendChild(weeks);
  grid.appendChild(
    kpi("日线新鲜度", lag === null || lag === undefined ? "—" : lag === 0 ? "无落后" : `落后 ${lag} 日`, {
      href: "#health-freshness",
      kind: lag ? "attention" : "",
    })
  );
  if (budget) {
    grid.appendChild(
      kpi("iFinD 预算剩余", budget.remaining, {
        href: "#health-verification",
        suffix: `/ ${budget.cap}`,
        kind: budget.remaining > 0 ? "" : "attention",
      })
    );
  }
  grid.appendChild(kpi("逾期财报", overdue, { href: "#health-overdue", kind: overdue ? "attention" : "zero" }));
  region.appendChild(grid);
  region.appendChild(
    el(
      "p",
      { class: "faint summary-meta" },
      `生成于 ${localTime(data.generated_at)}（${data.as_of}） · 完整率阈值 ${percent(data.threshold, 0)} · 目标池 ${data.target_universe}`
    )
  );
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
  if (!Array.isArray(data.warnings) || !data.warnings.length) {
    document.getElementById("health-warnings")?.setAttribute("hidden", "");
  }
  if (globalThis.location.hash) {
    document.querySelector(globalThis.location.hash)?.scrollIntoView();
  }
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

export function bootHealth() {
  const region = document.getElementById("data-health");
  if (region) loadHealth(region);
}
