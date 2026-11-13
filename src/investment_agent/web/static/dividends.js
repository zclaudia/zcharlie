import { get, post } from "./api.js";
import { el, table } from "./dom.js";
import { formatDateTime, label, PROVIDER_LABELS, QUALITY_FLAG_LABELS, QUALITY_LABELS } from "./format.js";

const text = (value) => value === null || value === undefined || value === "" ? "未采到" : String(value);
const percent = (value) => value === null || value === undefined ? "未采到" : `${(Number(value) * 100).toFixed(2)}%`;

function facts(items) {
  const box = el("div", { class: "dividend-facts" });
  for (const [name, value] of items) box.appendChild(el("p", {}, `${name}：${value}`));
  return box;
}

export function renderDividends(region, data) {
  region.replaceChildren();
  const cash = data.cash || {};
  region.appendChild(el("h3", {}, "现金股息"));
  region.appendChild(facts([
    ["最近 365 天现金股息率", percent(cash.value)], ["累计每股现金分红", text(cash.cash_per_share)],
    ["计算日", text(data.as_of)], ["窗口（左开右闭）", `${text(cash.window_start)} 至 ${text(cash.window_end)}`],
    ["价格", text(cash.price)], ["价格日期", text(cash.price_date)], ["来源", cash.provider ? label(PROVIDER_LABELS, cash.provider) : "未采到"],
    ["质量", cash.quality ? label(QUALITY_LABELS, cash.quality) : "未采到"], ["质量标记", cash.flags?.length ? cash.flags.map((flag) => label(QUALITY_FLAG_LABELS, flag)).join("、") : "无"],
  ]));
  region.appendChild(el("h3", {}, "分红记录"));
  const events = data.events || [];
  if (!events.length) region.appendChild(el("p", { class: "faint" }, "暂无有效分红记录。"));
  const years = new Map();
  for (const event of events) {
    const year = String(event.ex_date || "未知").slice(0, 4);
    if (!years.has(year)) years.set(year, []);
    years.get(year).push(event);
  }
  for (const [year, rows] of [...years].sort((a, b) => b[0].localeCompare(a[0]))) {
    const detail = el("details");
    detail.appendChild(el("summary", {}, `${year} 年（${rows.length} 项）`));
    const wrap = el("div", { class: "dividend-table-wrap" });
    const cells = rows.map((row) => {
      const tr = el("tr");
      for (const value of [row.ex_date, row.dps, row.pay_date, row.provider ? label(PROVIDER_LABELS, row.provider) : null]) tr.appendChild(el("td", {}, text(value)));
      return tr;
    });
    wrap.appendChild(table(["除权日", "每股税前现金", "派息日", "来源"].map((label) => ({ label })), cells));
    detail.appendChild(wrap); region.appendChild(detail);
  }
  region.appendChild(el("h3", {}, "平台参考"));
  const platform = data.platform;
  if (!platform) region.appendChild(el("p", { class: "faint" }, "尚未采到 wmshare 平台快照。"));
  else {
    region.appendChild(facts([["wmshare 平台 TTM 股息率", percent(platform.yield_ttm)], ["wmshare 平台 TTM 派息率", percent(platform.payout_ttm)], ["平台行情日期", text(platform.quote_date)], ["抓取时间", platform.captured_at ? formatDateTime(platform.captured_at) : "未采到"]]));
    if (data.difference && Math.abs(Number(data.difference.value)) > 0.003) region.appendChild(el("p", { class: "badge warn" }, `口径差异：现金 ${percent(cash.value)}，平台 ${percent(platform.yield_ttm)}（${Number(data.difference.points).toFixed(2)} 个百分点）`));
  }
  region.appendChild(el("p", { class: "faint" }, "平台口径，可能与按除权日累计的现金股息不同。"));
  region.appendChild(el("h3", {}, "待确认记录"));
  const pending = data.pending || [];
  if (!pending.length) region.appendChild(el("p", { class: "faint" }, platform ? "暂无待确认记录。" : "尚未采到待确认记录。"));
  else {
    const list = el("ul");
    for (const row of pending) list.appendChild(el("li", {}, `每股现金 ${text(row.dps)} · 公告日 ${text(row.announce)}${row.sas ? ` · ${row.sas}` : ""}`));
    region.appendChild(list);
  }
}

export async function mountDividends() {
  const card = document.querySelector("#dividend-card");
  if (!card) return;
  const ticker = card.dataset.ticker;
  if (!/^\d{6}\.(SH|SZ|BJ)$/.test(ticker || "")) { card.hidden = true; return; }
  const button = card.querySelector("#dividend-refresh");
  const status = card.querySelector("#dividend-refresh-status");
  const region = card.querySelector("#dividend-content");
  try {
    const data = await get(`/stocks/${encodeURIComponent(ticker)}/dividends`);
    renderDividends(region, data);
    button.disabled = !data.refresh_enabled;
    if (!data.refresh_enabled) status.textContent = data.refresh_disabled_reason || "当前无法刷新分红。";
    button.addEventListener("click", async () => {
      button.disabled = true;
      status.textContent = "正在提交刷新任务…";
      try {
        const run = await post(`/stocks/${encodeURIComponent(ticker)}/dividends/refresh`, {});
        status.replaceChildren(el("span", {}, `已提交分红刷新任务 #${run.run_id}（${run.status}）。`), el("a", { href: "/app/runs" }, "查看运行记录"));
      } catch (error) { status.textContent = error.message || "提交失败。"; }
      finally { button.disabled = false; }
    });
  } catch (error) {
    region.replaceChildren(el("p", { class: "notice notice-error" }, error.message || "分红数据加载失败。"));
  }
}
