// 股票页收盘价折线：区间可选、悬停显示日期与收盘价、标题区显示最新收盘。
// 只画已入库的不复权日线；DOM 一律 createElement，位置用 CSSOM（CSP 禁止 style 属性）。

import { el } from "./dom.js";
import { svg } from "./ui.js";

const W = 1000;
const H = 200;
const PAD = 6;
export const PRICE_RANGES = [
  { label: "7日", days: 7 },
  { label: "30日", days: 30 },
  { label: "60日", days: 60 },
  { label: "120日", days: 120 },
  { label: "240日", days: 240 },
  { label: "2年", days: 480 },
  { label: "近5年", days: 1000 },
];
const DEFAULT_DAYS = 240;

function price(value) {
  return value.toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function pct(value) {
  return `${value > 0 ? "+" : ""}${value.toFixed(2)}%`;
}

export function mountPriceChart(host, ticker, load, { title = null } = {}) {
  let days = DEFAULT_DAYS;
  let token = 0;

  const head = el("div", { class: "pc-head" });
  const latest = el("div", { class: "pc-latest" });
  const tabs = el("div", { class: "pc-tabs", role: "group", "aria-label": "时间区间" });
  const stage = el("div", { class: "pc-stage" });
  const axis = el("div", { class: "pc-axis", "aria-hidden": "true" });
  const yaxis = el("div", { class: "pc-yaxis", "aria-hidden": "true" });
  const plot = el("div", { class: "pc-plot" });
  plot.append(yaxis, stage, el("div"), axis);
  head.append(latest, tabs);
  host.replaceChildren(head, plot);
  host.removeAttribute("aria-hidden");

  const buttons = PRICE_RANGES.map((range) => {
    const button = el("button", { type: "button", class: "pc-tab" }, range.label);
    button.addEventListener("click", () => {
      if (days === range.days) return;
      days = range.days;
      refresh();
    });
    tabs.appendChild(button);
    return { button, range };
  });

  function syncTabs() {
    for (const { button, range } of buttons) {
      const on = range.days === days;
      button.classList.toggle("is-active", on);
      button.setAttribute("aria-pressed", on ? "true" : "false");
    }
    if (title) title.textContent = `近 ${days} 个交易日收盘价`;
  }

  function drawAxis(bars) {
    const n = bars.length;
    // 点数很少（7日）时逐日标注，否则等分取 5 个（窄屏 3 个）。
    const count = n <= 7 ? n : Math.min(n, host.clientWidth < 520 ? 3 : 5);
    const long = new Date(bars[n - 1].trade_date) - new Date(bars[0].trade_date) > 300 * 86400000;
    const ticks = [];
    for (let k = 0; k < count; k += 1) {
      const i = count === 1 ? 0 : Math.round((k / (count - 1)) * (n - 1));
      const date = bars[i].trade_date;
      const tick = el("span", { class: "pc-tick" }, long ? date.slice(0, 7) : date.slice(5));
      tick.style.left = `${n === 1 ? 0 : (i / (n - 1)) * 100}%`;
      if (k === 0) tick.classList.add("pc-tick-first");
      else if (k === count - 1) tick.classList.add("pc-tick-last");
      ticks.push(tick);
    }
    axis.replaceChildren(...ticks);
  }

  function showEmpty(text) {
    axis.replaceChildren();
    yaxis.replaceChildren();
    latest.replaceChildren();
    stage.replaceChildren(el("p", { class: "faint" }, text));
  }

  function draw(bars) {
    const closes = bars.map((bar) => bar.close);
    const min = Math.min(...closes);
    const max = Math.max(...closes);
    const span = max === min ? 1 : max - min;
    const n = bars.length;
    const px = (i) => (i / (n - 1)) * W;
    const py = (v) => H - PAD - ((v - min) / span) * (H - 2 * PAD);

    const first = bars[0];
    const last = bars[n - 1];
    const change = ((last.close - first.close) / first.close) * 100;
    const trend = last.close > first.close ? "pc-up" : last.close < first.close ? "pc-down" : "pc-flat";
    latest.replaceChildren(
      el("span", { class: "pc-price" }, price(last.close)),
      el("span", { class: "pc-meta" }, `最新收盘 ${last.trade_date}`),
      el("span", { class: `pc-change ${trend}` }, `区间 ${pct(change)}`),
      el("span", { class: "pc-meta" }, `高 ${price(max)} · 低 ${price(min)}`)
    );

    const root = svg("svg", {
      class: `pc-svg ${trend}`,
      viewBox: `0 0 ${W} ${H}`,
      preserveAspectRatio: "none",
      role: "img",
      "aria-label": `${ticker} 近 ${n} 个交易日收盘价曲线`,
    });
    const yTicks = [];
    for (let k = 0; k < 4; k += 1) {
      const value = max - ((max - min) * k) / 3;
      yTicks.push(value);
      root.appendChild(
        svg("line", { class: "pc-grid", x1: 0, x2: W, y1: py(value).toFixed(1), y2: py(value).toFixed(1), "vector-effect": "non-scaling-stroke" })
      );
    }
    yaxis.replaceChildren(
      ...yTicks.map((value, k) => {
        const tick = el("span", { class: "pc-ytick" }, price(value));
        tick.style.top = `${(py(value) / H) * 100}%`;
        if (k === 0) tick.classList.add("pc-ytick-first");
        else if (k === yTicks.length - 1) tick.classList.add("pc-ytick-last");
        return tick;
      })
    );
    root.appendChild(
      svg("polyline", {
        class: "pc-line",
        points: bars.map((bar, i) => `${px(i).toFixed(1)},${py(bar.close).toFixed(1)}`).join(" "),
        fill: "none",
        "vector-effect": "non-scaling-stroke",
      })
    );
    const cross = el("div", { class: "pc-cross", hidden: "" });
    const dot = el("div", { class: "pc-dot", hidden: "" });
    const tip = el("div", { class: "pc-tip", hidden: "" });
    const lastDot = el("div", { class: "pc-dot pc-dot-last" });
    lastDot.style.left = "100%";
    lastDot.style.top = `${(py(last.close) / H) * 100}%`;
    stage.replaceChildren(root, cross, dot, lastDot, tip);
    drawAxis(bars);

    function show(event) {
      const rect = stage.getBoundingClientRect();
      if (!rect.width) return;
      const ratio = Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width));
      const i = Math.round(ratio * (n - 1));
      const bar = bars[i];
      const left = (i / (n - 1)) * 100;
      cross.hidden = dot.hidden = tip.hidden = false;
      cross.style.left = `${left}%`;
      dot.style.left = `${left}%`;
      dot.style.top = `${(py(bar.close) / H) * 100}%`;
      const prev = i > 0 ? bars[i - 1].close : null;
      const day = prev ? ((bar.close - prev) / prev) * 100 : null;
      tip.replaceChildren(
        el("div", { class: "pc-tip-date" }, bar.trade_date),
        el("div", { class: "pc-tip-close" }, price(bar.close)),
        ...(day === null ? [] : [el("div", { class: `pc-tip-day ${day > 0 ? "pc-up" : day < 0 ? "pc-down" : ""}` }, `日 ${pct(day)}`)])
      );
      tip.classList.toggle("pc-tip-left", ratio > 0.7);
      tip.style.left = `${left}%`;
    }
    function hide() {
      cross.hidden = dot.hidden = tip.hidden = true;
    }
    stage.onpointermove = show;
    stage.onpointerdown = show;
    stage.onpointerleave = hide;
    stage.onpointercancel = hide;
  }

  async function refresh() {
    const mine = ++token;
    syncTabs();
    let bars = [];
    try {
      const data = await load(ticker, days);
      bars = data && Array.isArray(data.bars) ? data.bars : [];
    } catch {
      if (mine === token) showEmpty("日线加载失败。");
      return;
    }
    if (mine !== token) return;
    bars = bars.filter((bar) => bar && Number.isFinite(bar.close) && bar.trade_date);
    if (bars.length < 2) showEmpty("暂无足够的已入库日线。");
    else draw(bars);
  }

  refresh();
}
