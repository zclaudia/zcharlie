import { get, renderRegion } from "./api.js";
import { healthBucket } from "./health.js";
import { el, fmt, fmtNumber, statusBadge, table } from "./dom.js";
import { formatDateTime, label } from "./format.js";
import { stockIdent, thinBar } from "./ui.js";
import { PRIORITY_LABELS, icon, isCompactViewport, priorityBadge, shortStamp } from "./page_widgets.js";

// ---- 今日概览 ---------------------------------------------------------

const WEEKDAYS = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"];
const ALERTS_VISIBLE = 4;
const ALERTS_VISIBLE_COMPACT = 3;

function overviewSubtitle(generatedAt) {
  const stamp = formatDateTime(generatedAt);
  if (stamp === "—") return "";
  const [date, time] = stamp.split(" ");
  const day = new Date(`${date}T00:00:00`);
  const week = Number.isNaN(day.getTime()) ? "" : ` ${WEEKDAYS[day.getDay()]}`;
  return `${date}${week} · 上次刷新 ${time}`;
}

function splitMessage(message) {
  const text = String(message ?? "");
  const match = /[：:]\s*/.exec(text);
  if (!match || match.index < 4 || match.index + match[0].length >= text.length) return [text, ""];
  return [text.slice(0, match.index), text.slice(match.index + match[0].length)];
}

function entryNames(bucket) {
  const entries = Array.isArray(bucket.entries) ? bucket.entries : [];
  const names = entries.slice(0, 3).map((entry) => entry.name || entry.ticker || `#${entry.run_id}`);
  const rest = bucket.count - names.length;
  return `${names.join("、")}${rest > 0 ? ` 等 ${bucket.count} 项` : ""}`;
}

function bucketRow(bucket, { title, href, action, iconName = "alert" }) {
  if (!bucket || !(bucket.count > 0)) return null;
  const entries = Array.isArray(bucket.entries) ? bucket.entries : [];
  const single = bucket.count === 1 && entries[0] && entries[0].ticker;
  return {
    level: "warn",
    icon: iconName,
    title: title(bucket.count),
    detail: entryNames(bucket),
    href: single && action.single ? `/app/stocks/${encodeURIComponent(entries[0].ticker)}` : href,
    action: single && action.single ? action.single : action.text,
  };
}

// "需要你处理"的行：数据健康提醒 → 候选草稿 → 研究运行异常。
function overviewAlertRows(data) {
  const rows = [];
  const warnings = Array.isArray(data.warnings) ? data.warnings : [];
  const candidates = data.candidates && typeof data.candidates === "object" ? data.candidates : null;
  const warningRow = (warning) => {
    const [title, detail] = splitMessage(fmt(warning.message));
    return {
      level: warning.level === "error" ? "error" : "warn",
      icon: typeof warning.link === "string" && warning.link.includes("freshness") ? "cal" : "alert",
      title,
      detail,
      href: typeof warning.link === "string" && warning.link.startsWith("/app/") ? warning.link : null,
      action: "查看详情",
    };
  };
  for (const warning of warnings) if (warning.level === "error") rows.push(warningRow(warning));
  if (candidates && candidates.drafts_ready > 0) {
    const pending = candidates.verification_pending > 0 ? ` · iFinD 核对未完成 ${candidates.verification_pending}` : "";
    rows.push({
      level: "ai",
      icon: "file",
      title: `${candidates.drafts_ready} 份候选研究草稿待审阅`,
      detail: `待处理候选 ${candidates.open}${pending}`,
      href: "/app/candidates",
      action: "开始审阅",
      primary: true,
    });
  }
  if (candidates && candidates.draft_failed > 0) {
    rows.push({
      level: "warn",
      icon: "alert",
      title: `${candidates.draft_failed} 份候选起草失败`,
      detail: "可在候选页重新起草。",
      href: "/app/candidates",
      action: "去处理",
    });
  }
  for (const warning of warnings) if (warning.level !== "error") rows.push(warningRow(warning));
  const extra = [
    bucketRow(data.failed, { title: (n) => `${n} 个研究运行失败`, href: "/app/runs?status=failed", action: { text: "查看运行", single: "查看" } }),
    bucketRow(data.partial, { title: (n) => `${n} 个研究运行部分成功`, href: "/app/runs?status=partial", action: { text: "查看运行", single: "查看" } }),
    bucketRow(data.needs_review, { title: (n) => `${n} 只标的需人工复核`, href: "/app/watchlist", action: { text: "查看观察池", single: "去复核" }, iconName: "eye" }),
    bucketRow(data.meaningful_changes, { title: (n) => `${n} 只标的有重要变化`, href: "/app/watchlist", action: { text: "查看观察池", single: "查看" }, iconName: "refresh" }),
  ];
  for (const row of extra) if (row) rows.push(row);
  return rows;
}

function alertRowNode(row) {
  const tone = row.level === "ai" ? "ai" : row.level === "error" ? "error" : "warn";
  const li = el("li", { class: `attn-row ov-alert-${tone}` });
  const ico = el("span", { class: `list-row-icon ${tone === "ai" ? "ai" : "warn"}`, "aria-hidden": "true" });
  ico.appendChild(icon(row.icon, 16));
  li.appendChild(ico);
  const body = el("div", { class: "attn-main" });
  body.appendChild(el("div", { class: "attn-title" }, row.title));
  if (row.detail) body.appendChild(el("div", { class: "attn-detail", title: row.detail }, row.detail));
  li.appendChild(body);
  if (row.href) {
    const act = el("a", { class: `btn btn-sm attn-act ov-alert-act${row.primary ? " btn-primary" : ""}`, href: row.href });
    act.appendChild(el("span", { class: "ov-act-text" }, row.action));
    const chev = icon("chev", 16);
    chev.classList.add("ov-act-chev");
    act.appendChild(chev);
    li.appendChild(act);
  }
  return li;
}

function alertsCard(rows, data) {
  const card = el("section", { class: "card card--flush ov-alerts", "aria-label": "需要你处理" });
  const head = el("div", { class: "card-head card-head--rule" });
  head.appendChild(el("h2", {}, "需要你处理"));
  const warnings = Array.isArray(data.warnings) ? data.warnings : [];
  const errors = warnings.filter((warning) => warning.level === "error").length;
  const warns = warnings.length - errors;
  const drafts = data.candidates && data.candidates.drafts_ready > 0 ? data.candidates.drafts_ready : 0;
  const tags = el("div", { class: "cluster ov-alerts-tags" });
  if (errors) tags.appendChild(el("span", { class: "badge warn" }, `${errors} 错误`));
  if (warns) tags.appendChild(el("span", { class: "badge warn outline" }, `${warns} 警告`));
  if (drafts) tags.appendChild(el("span", { class: "badge ai" }, `${drafts} 份草稿待审`));
  head.appendChild(tags);
  head.appendChild(el("a", { class: "card-more", href: "/app/health" }, "数据健康 →"));
  card.appendChild(head);

  const visible = isCompactViewport() ? ALERTS_VISIBLE_COMPACT : ALERTS_VISIBLE;
  const list = el("ul", { class: "attn-list" });
  const nodes = rows.map((row, index) => {
    const node = alertRowNode(row);
    if (index >= visible) node.hidden = true;
    list.appendChild(node);
    return node;
  });
  if (!rows.length) list.appendChild(el("li", { class: "ov-alert-empty" }, "今日无待处理项。"));
  card.appendChild(list);
  if (nodes.length > visible) {
    const foot = el("div", { class: "card-foot" });
    const more = el("button", { type: "button", class: "link-button", "aria-expanded": "false" }, `展开其余 ${nodes.length - visible} 条`);
    more.addEventListener("click", () => {
      const open = more.getAttribute("aria-expanded") !== "true";
      nodes.forEach((node, index) => {
        if (index >= visible) node.hidden = !open;
      });
      more.setAttribute("aria-expanded", open ? "true" : "false");
      more.textContent = open ? "收起" : `展开其余 ${nodes.length - visible} 条`;
    });
    foot.appendChild(more);
    card.appendChild(foot);
  }
  return card;
}

// 手机顶部的数据状态条：一行，点进数据健康。
function statusStrip(data) {
  const warnings = Array.isArray(data.warnings) ? data.warnings : [];
  const lead = warnings.find((warning) => warning.level === "error") || warnings[0];
  if (!lead) return null;
  const health = data.data_health && typeof data.data_health === "object" ? data.data_health : null;
  const errors = health ? health.errors : warnings.filter((warning) => warning.level === "error").length;
  const warns = health ? health.warnings : warnings.length - errors;
  const href = typeof lead.link === "string" && lead.link.startsWith("/app/") ? lead.link : "/app/health";
  const node = el("a", { class: "ov-status only-mobile", href });
  node.appendChild(icon("alert", 16));
  const text = el("span", { class: "ov-status-text" });
  text.appendChild(el("b", {}, splitMessage(fmt(lead.message))[0]));
  text.appendChild(el("small", {}, `${errors} 错误 · ${warns} 警告`));
  node.appendChild(text);
  const chev = icon("chev", 16);
  chev.classList.add("ov-status-chev");
  node.appendChild(chev);
  return node;
}

function funnelStage({ href, iconName, name, big, unit, chips = [], cta, note, muted = false }) {
  const node = el("a", { class: `ov-stage${muted ? " is-muted" : ""}`, href });
  const top = el("div", { class: "ov-stage-top" });
  top.appendChild(icon(iconName, 15));
  top.appendChild(document.createTextNode(name));
  node.appendChild(top);
  const number = el("div", { class: "ov-stage-big" });
  number.appendChild(el("b", { class: "num" }, big));
  if (unit) number.appendChild(el("small", {}, unit));
  node.appendChild(number);
  const chipBox = el("div", { class: "cluster ov-stage-chips" });
  for (const [text, kind] of chips) chipBox.appendChild(el("span", { class: `badge ${kind || ""}`.trim() }, text));
  node.appendChild(chipBox);
  const cue = el("div", { class: "ov-stage-cta" });
  cue.appendChild(document.createTextNode(cta));
  cue.appendChild(icon("chev", 14));
  node.appendChild(cue);
  node.appendChild(el("em", { class: "ov-stage-note" }, note));
  return node;
}

function funnelBlock(data, watch, accounts) {
  const candidates = data.candidates && typeof data.candidates === "object" ? data.candidates : null;
  const wrap = el("section", { class: "ov-funnel-wrap", "aria-label": "研究漏斗" });
  wrap.appendChild(el("h2", { class: "ov-section-title" }, "研究漏斗"));
  const row = el("div", { class: "ov-funnel" });

  if (candidates) {
    const chips = [[`草稿待审 ${candidates.drafts_ready}`, candidates.drafts_ready ? "ai" : ""]];
    chips.push([`起草中 ${candidates.drafting}`, ""]);
    if (candidates.draft_failed) chips.push([`起草失败 ${candidates.draft_failed}`, "warn"]);
    chips.push([`核对未完成 ${candidates.verification_pending}`, candidates.verification_pending ? "warn" : ""]);
    row.appendChild(
      funnelStage({
        href: "/app/candidates",
        iconName: "funnel",
        name: "候选",
        big: String(candidates.open),
        unit: "只待处理",
        chips,
        cta: "逐个审阅，决定是否加入观察",
        note: candidates.drafts_ready ? `${candidates.drafts_ready} 草稿待审` : "无待审草稿",
      })
    );
  } else {
    row.appendChild(funnelStage({ href: "/app/candidates", iconName: "funnel", name: "候选", big: "—", cta: "查看候选池", note: "暂无数据", muted: true }));
  }
  row.appendChild(el("span", { class: "ov-arrow", "aria-hidden": "true" })).appendChild(icon("arrow", 18));

  if (Array.isArray(watch)) {
    const scored = watch.filter((entry) => entry.latest_research).length;
    const changed = watch.filter((entry) => entry.latest_research && entry.latest_research.meaningful_change_count > 0).length;
    const ruled = watch.filter((entry) => entry.rule && entry.rule.active).length;
    const chips = [[`已评分 ${scored}`, scored ? "accent" : ""], [`待研究 ${watch.length - scored}`, ""]];
    if (ruled) chips.push([`规则已生效 ${ruled}`, ""]);
    row.appendChild(
      funnelStage({
        href: "/app/watchlist",
        iconName: "eye",
        name: "观察",
        big: String(watch.length),
        unit: "只跟踪中",
        chips,
        cta: changed ? `${changed} 只有新评分变化` : "查看观察池",
        note: changed ? `${changed} 只有变化` : "无新变化",
        muted: watch.length === 0,
      })
    );
  } else {
    row.appendChild(funnelStage({ href: "/app/watchlist", iconName: "eye", name: "观察", big: "—", cta: "查看观察池", note: "暂无数据", muted: true }));
  }
  row.appendChild(el("span", { class: "ov-arrow", "aria-hidden": "true" })).appendChild(icon("arrow", 18));

  if (Array.isArray(accounts) && accounts.length) {
    row.appendChild(
      funnelStage({
        href: "/app/portfolio",
        iconName: "brief",
        name: "持仓",
        big: String(accounts.length),
        unit: "个账户",
        chips: accounts.slice(0, 2).map((account) => [fmt(account.name), "outline"]),
        cta: "查看持仓与规则",
        note: "查看持仓",
      })
    );
  } else {
    row.appendChild(
      funnelStage({
        href: "/app/portfolio",
        iconName: "brief",
        name: "持仓",
        big: "—",
        unit: "未建账户",
        chips: [["先建账户并录入期初余额", "outline"]],
        cta: "新建账户",
        note: "未建账户",
        muted: true,
      })
    );
  }
  wrap.appendChild(row);
  return wrap;
}

function recentConclusionItems(runs) {
  const seen = new Set();
  const items = [];
  for (const run of Array.isArray(runs) ? runs : []) {
    if (run.status !== "success" && run.status !== "partial") continue;
    if (!run.ticker || seen.has(run.ticker)) continue;
    seen.add(run.ticker);
    items.push(run);
    if (items.length === 5) break;
  }
  return items;
}

function conclusionsCard(runs, strategies) {
  const items = recentConclusionItems(runs);
  const card = el("section", { class: "card card--flush ov-conclusions" });
  const head = el("div", { class: "card-head card-head--rule" });
  head.appendChild(el("h2", {}, "最近研究结论"));
  head.appendChild(el("a", { class: "card-more", href: "/app/runs" }, "运行中心 →"));
  card.appendChild(head);
  if (!items.length) {
    card.appendChild(el("p", { class: "ov-empty" }, Array.isArray(runs) ? "尚无已完成的研究运行。" : "运行记录暂时无法加载。"));
    return card;
  }
  const strategyOf = (run) => strategies.get(run.ticker) || run.industry || "—";

  const wrap = el("div", { class: "table-wrap ov-conclusions-table" });
  const tbl = el("table", { class: "tbl" });
  const headRow = el("tr");
  for (const [text, cls] of [["股票"], ["策略 / 行业"], ["业务分"], ["优先级"], ["完成", "num"]]) {
    headRow.appendChild(el("th", { scope: "col", class: cls }, text));
  }
  tbl.appendChild(el("thead")).appendChild(headRow);
  const body = el("tbody");
  for (const run of items) {
    const tr = el("tr");
    const nameCell = el("td");
    nameCell.appendChild(stockIdent(run.company_name, run.ticker));
    tr.appendChild(nameCell);
    tr.appendChild(el("td", {}, strategyOf(run)));
    const scoreCell = el("td");
    const score = el("div", { class: "score-inline" });
    score.appendChild(el("b", { class: "num" }, fmtNumber(run.business_score, 1)));
    score.appendChild(thinBar(run.business_score));
    scoreCell.appendChild(score);
    tr.appendChild(scoreCell);
    const priority = el("td");
    priority.appendChild(priorityBadge(run.workflow_label));
    tr.appendChild(priority);
    tr.appendChild(el("td", { class: "num muted" }, shortStamp(run.finished_at || run.started_at)));
    body.appendChild(tr);
  }
  tbl.appendChild(body);
  wrap.appendChild(tbl);
  card.appendChild(wrap);

  const list = el("div", { class: "ov-conclusions-list" });
  for (const run of items) {
    const row = el("a", { class: "list-row", href: `/app/stocks/${encodeURIComponent(run.ticker)}` });
    const main = el("div", { class: "list-row-main" });
    main.appendChild(el("b", {}, run.company_name || run.ticker));
    main.appendChild(el("small", {}, `${strategyOf(run)} · ${shortStamp(run.finished_at || run.started_at)}`));
    row.appendChild(main);
    const end = el("div", { class: "ov-row-score" });
    end.appendChild(el("b", { class: "num" }, fmtNumber(run.business_score, 1)));
    const entry = PRIORITY_LABELS[run.workflow_label];
    end.appendChild(el("small", {}, entry ? entry[0] : "—"));
    row.appendChild(end);
    list.appendChild(row);
  }
  card.appendChild(list);
  return card;
}

function runningCard(bucket) {
  const card = el("section", { class: "card card--flush ov-running" });
  if (!(bucket && bucket.count > 0)) card.classList.add("is-empty");
  const head = el("div", { class: "card-head card-head--rule" });
  head.appendChild(el("h2", {}, "运行中"));
  const count = bucket && bucket.count > 0 ? bucket.count : 0;
  if (count) head.appendChild(el("span", { class: "badge" }, String(count)));
  head.appendChild(el("a", { class: "card-more", href: "/app/runs" }, "全部 →"));
  card.appendChild(head);
  const entries = bucket && Array.isArray(bucket.entries) ? bucket.entries : [];
  if (!count) {
    card.appendChild(el("p", { class: "ov-empty" }, "当前没有排队或运行中的研究任务。"));
    return card;
  }
  const list = el("ul", { class: "ov-mini-list" });
  for (const entry of entries.slice(0, 5)) {
    const li = el("li");
    li.appendChild(statusBadge(entry.status));
    li.appendChild(stockIdent(entry.name, entry.ticker));
    li.appendChild(el("span", { class: "spacer" }));
    li.appendChild(el("span", { class: "muted num ov-mini-meta" }, `#${entry.run_id} · ${shortStamp(entry.started_at).slice(-5)}`));
    list.appendChild(li);
  }
  card.appendChild(list);
  if (count > 5) card.appendChild(el("div", { class: "card-foot ov-running-foot" }, `还有 ${count - 5} 个任务 · 在运行中心查看`));

  const summary = el("a", { class: "list-row ov-running-summary", href: "/app/runs" });
  const ico = el("span", { class: "list-row-icon", "aria-hidden": "true" });
  ico.appendChild(icon("clock", 16));
  summary.appendChild(ico);
  const main = el("div", { class: "list-row-main" });
  const running = entries.filter((entry) => entry.status === "running").length;
  main.appendChild(el("b", {}, `${count} 个研究任务${running ? "进行中" : "排队"}`));
  const earliest = entries.length ? entries[entries.length - 1].started_at : null;
  main.appendChild(el("small", {}, earliest ? `最早 ${shortStamp(earliest)}` : "在运行中心查看"));
  summary.appendChild(main);
  summary.appendChild(icon("chev", 16));
  card.appendChild(summary);
  return card;
}

function healthDetails(health) {
  const box = el("details", { class: "ov-health" });
  const number = (value) => (typeof value === "number" ? value : 0);
  box.appendChild(
    el(
      "summary",
      {},
      `数据健康概览：${number(health.errors)} 错误 · ${number(health.warnings)} 警告 · 快照积累 ${number(health.streak_weeks)} / ${number(health.target_weeks) || 8} 周`
    )
  );
  box.appendChild(healthBucket(health));
  return box;
}

export async function loadDashboard(region, data) {
  const sub = document.querySelector("#overview-sub");
  const subtitle = overviewSubtitle(data.generated_at);
  if (sub && subtitle) sub.textContent = subtitle;

  const candidates = data.candidates && typeof data.candidates === "object" ? data.candidates : null;
  const health = data.data_health && typeof data.data_health === "object" ? data.data_health : null;
  const warnings = Array.isArray(data.warnings) ? data.warnings : [];
  const buckets = [data.running, data.failed, data.partial, data.needs_review, data.meaningful_changes];
  if (!buckets.some((bucket) => bucket && bucket.count > 0) && !warnings.length && !candidates && !health) {
    renderRegion(region, "empty", "今日无待处理项。");
    return;
  }

  // 漏斗与最近结论需要观察池 / 账户 / 运行列表；任何一个失败都只让对应区块显示"—"。
  const [watchResult, accountsResult, runsResult] = await Promise.allSettled([
    get("/watchlist"),
    get("/portfolio/accounts"),
    get("/research/runs?limit=30"),
  ]);
  const watch = watchResult.status === "fulfilled" && Array.isArray(watchResult.value) ? watchResult.value : null;
  const accounts = accountsResult.status === "fulfilled" && Array.isArray(accountsResult.value) ? accountsResult.value : null;
  const runs = runsResult.status === "fulfilled" && Array.isArray(runsResult.value) ? runsResult.value : null;
  const strategies = new Map();
  for (const entry of watch || []) {
    if (entry.latest_research && entry.latest_research.strategy_label) strategies.set(entry.ticker, entry.latest_research.strategy_label);
  }

  const container = el("div", { class: "ov-stack" });
  const strip = statusStrip(data);
  if (strip) container.appendChild(strip);
  container.appendChild(alertsCard(overviewAlertRows(data), data));
  container.appendChild(funnelBlock(data, watch, accounts));
  const pair = el("div", { class: "grid-7-5 ov-pair" });
  pair.appendChild(conclusionsCard(runs, strategies));
  pair.appendChild(runningCard(data.running));
  container.appendChild(pair);
  if (health) container.appendChild(healthDetails(health));
  renderRegion(region, "ready", container);
}

