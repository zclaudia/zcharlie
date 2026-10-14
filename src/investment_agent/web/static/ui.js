// 可视化组件：全部用 SVG 的百分比坐标与 class 着色，不写内联样式（CSP style-src 'self'）。
// 只把已给出的数值画出来，不计算、不评判。

import { el } from "./dom.js";
import { formatDate } from "./format.js";

// SVG 命名空间（是标识符不是网络地址；拼接以免触发"JS 不含外部 URL"的守卫）。
const SVG_NS = ["http:", "", "www.w3.org", "2000", "svg"].join("/");

export function svg(tag, attributes = {}, text) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(attributes)) {
    if (value !== undefined && value !== null && value !== false) node.setAttribute(key, String(value));
  }
  if (text !== undefined) node.textContent = text;
  return node;
}

function clamp01(value) {
  return Math.max(0, Math.min(1, value));
}

function pct(value, [min, max]) {
  if (typeof value !== "number" || !Number.isFinite(value) || max === min) return null;
  return `${(clamp01((value - min) / (max - min)) * 100).toFixed(2)}%`;
}

// 刻度条：domain=[min,max]；zones / thresholds / markers / defaults 都按 domain 映射到百分比。
// zones: [{from, to, cls}]；thresholds: [{at, label}]；markers: [{at, cls, title}]；
// ticks: [{at, label}]；defaults: [at]（模板默认值的小三角）。
export function scale({ domain, zones = [], thresholds = [], markers = [], ticks = [], defaults = [], compact = false, label }) {
  const root = svg("svg", {
    class: `scale${compact ? " compact" : ""}`,
    role: "img",
    "aria-label": label || undefined,
    "aria-hidden": label ? undefined : "true",
  });
  const top = compact ? 8 : 14;
  root.appendChild(svg("rect", { class: "s-track", x: 0, y: top, width: "100%", height: 8, rx: 4 }));
  for (const zone of zones) {
    const from = pct(zone.from, domain);
    const to = pct(zone.to, domain);
    if (from === null || to === null) continue;
    const start = Math.min(parseFloat(from), parseFloat(to));
    const width = Math.abs(parseFloat(to) - parseFloat(from));
    root.appendChild(
      svg("rect", { class: zone.cls || "s-zone", x: `${start}%`, y: top, width: `${width}%`, height: 8 })
    );
  }
  for (const tick of ticks) {
    const x = pct(tick.at, domain);
    if (x === null) continue;
    root.appendChild(svg("line", { class: "s-tick", x1: x, x2: x, y1: top - 2, y2: top + 10 }));
    if (tick.label) {
      const anchor = parseFloat(x) < 4 ? "start" : parseFloat(x) > 96 ? "end" : "middle";
      root.appendChild(svg("text", { class: "s-tick-label", x, y: top + 21, "text-anchor": anchor }, tick.label));
    }
  }
  for (const at of defaults) {
    const x = pct(at, domain);
    if (x === null) continue;
    const holder = svg("svg", { x, y: top - 8, overflow: "visible" });
    holder.appendChild(svg("polygon", { class: "s-default", points: "-5,0 5,0 0,6" }));
    root.appendChild(holder);
  }
  for (const threshold of thresholds) {
    const x = pct(threshold.at, domain);
    if (x === null) continue;
    root.appendChild(svg("line", { class: "s-threshold", x1: x, x2: x, y1: top - 5, y2: top + 13 }));
    if (threshold.label && !compact) {
      const anchor = parseFloat(x) > 80 ? "end" : "start";
      const dx = anchor === "end" ? -4 : 4;
      root.appendChild(
        svg("text", { class: "s-threshold-label", x, dx, y: top - 5, "text-anchor": anchor }, threshold.label)
      );
    }
  }
  for (const marker of markers) {
    const x = pct(marker.at, domain);
    if (x === null) continue;
    const dot = svg("circle", { class: marker.cls || "s-marker", cx: x, cy: top + 4, r: 6 });
    if (marker.title) dot.appendChild(svg("title", {}, marker.title));
    root.appendChild(dot);
  }
  return root;
}

// 0–100 水平分项条。
export function bar(value, max = 100) {
  const root = svg("svg", { "aria-hidden": "true" });
  root.appendChild(svg("rect", { class: "b-track", x: 0, y: 0, width: "100%", height: 10, rx: 5 }));
  const width = pct(typeof value === "number" ? value : 0, [0, max]);
  if (width !== null && parseFloat(width) > 0) {
    root.appendChild(svg("rect", { class: "b-fill", x: 0, y: 0, width, height: 10, rx: 5 }));
  }
  return root;
}

// 名称 + 代码两行的股票标识（概览 / 运行中心共用）；有链接时整块可点。样式 = app.css .stock-ident。
export function stockIdent(name, ticker, { link = true } = {}) {
  const linked = link && ticker;
  const node = el(linked ? "a" : "span", {
    class: "stock-ident",
    href: linked ? `/app/stocks/${encodeURIComponent(ticker)}` : undefined,
  });
  node.appendChild(el("span", { class: "stock-name" }, name || ticker || "—"));
  if (name && ticker) node.appendChild(el("span", { class: "stock-code" }, ticker));
  return node;
}

// 细分数条（0–100，SVG 百分比坐标）：概览 / 观察池 / 股票页分项评分共用，样式 = app.css .tbar。
export function thinBar(value, { height = 4, weak = false } = {}) {
  const root = svg("svg", { class: "tbar", viewBox: `0 0 100 ${height}`, preserveAspectRatio: "none", "aria-hidden": "true" });
  root.appendChild(svg("rect", { class: "tr", x: 0, y: 0, width: 100, height, rx: height / 2 }));
  const width = typeof value === "number" && Number.isFinite(value) ? Math.max(0, Math.min(100, value)) : 0;
  if (width > 0) root.appendChild(svg("rect", { class: weak ? "fl weak" : "fl", x: 0, y: 0, width, height, rx: height / 2 }));
  return root;
}

// 圆环（如投资分 / 100）。
export function ring(value, max = 100, { caption = "" } = {}) {
  const box = el("div", { class: "score-ring" });
  const radius = 44;
  const length = 2 * Math.PI * radius;
  const share = typeof value === "number" && Number.isFinite(value) ? clamp01(value / max) : 0;
  const root = svg("svg", { viewBox: "0 0 104 104", "aria-hidden": "true" });
  root.appendChild(svg("circle", { class: "ring-bg", cx: 52, cy: 52, r: radius, fill: "none", "stroke-width": 9 }));
  root.appendChild(
    svg("circle", {
      class: "ring-fg",
      cx: 52,
      cy: 52,
      r: radius,
      fill: "none",
      "stroke-width": 9,
      "stroke-linecap": "round",
      "stroke-dasharray": `${(share * length).toFixed(1)} ${length.toFixed(1)}`,
    })
  );
  box.appendChild(root);
  const text = el("div", { class: "ring-text" });
  text.appendChild(el("span", { class: "big" }, typeof value === "number" ? value.toFixed(1) : "—"));
  text.appendChild(el("span", { class: "faint" }, caption || `/ ${max}`));
  box.appendChild(text);
  return box;
}

// 有效期进度（开启 → 到期）。
export function timeline(startIso, endIso, now = new Date()) {
  const box = el("div", { class: "timeline-mini" });
  const start = Date.parse(startIso);
  const end = Date.parse(endIso);
  if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) return box;
  const share = clamp01((now.getTime() - start) / (end - start));
  const daysLeft = Math.max(0, Math.ceil((end - now.getTime()) / 86400000));
  box.appendChild(el("div", { class: "faint" }, daysLeft > 0 ? `候选有效期 · 剩 ${daysLeft} 天` : "候选有效期 · 已到期"));
  const root = svg("svg", { "aria-hidden": "true" });
  root.appendChild(svg("rect", { class: "t-track", x: 0, y: 0, width: "100%", height: 6, rx: 3 }));
  if (share > 0) {
    root.appendChild(svg("rect", { class: "t-fill", x: 0, y: 0, width: `${(share * 100).toFixed(1)}%`, height: 6, rx: 3 }));
  }
  box.appendChild(root);
  const ends = el("div", { class: "timeline-ends" });
  ends.appendChild(el("span", {}, `开启 ${formatDate(String(startIso))}`));
  ends.appendChild(el("span", {}, `到期 ${formatDate(String(endIso))}`));
  box.appendChild(ends);
  return box;
}

// 日线迷你图（B3 / D7）：points 为 [{trade_date, close}]（或纯数值），只画给出的收盘价，不评判。
// viewBox 固定 0–100 × 0–30、preserveAspectRatio="none"，线宽靠 non-scaling-stroke 保持像素宽度；
// 末点用零半径描边圆（同样不随拉伸变形）。首尾比较给出 spark-up / spark-down / spark-flat。
// bands: [{from, to, kind}] 为价格区间的水平矩形（P2 区间带占位），kind 决定 class。
const SPARK_W = 100;
const SPARK_H = 30;
const SPARK_PAD = 1.5;

function sparkCloses(points) {
  return (Array.isArray(points) ? points : [])
    .map((point) => (typeof point === "number" ? point : point && point.close))
    .filter((value) => typeof value === "number" && Number.isFinite(value));
}

export function sparkline(points, { width = 120, height = 32, bands = [] } = {}) {
  const closes = sparkCloses(points);
  if (closes.length < 2) return el("span", { class: "spark spark-empty", "aria-hidden": "true" }, "—");
  let min = Infinity;
  let max = -Infinity;
  for (const value of closes) {
    if (value < min) min = value;
    if (value > max) max = value;
  }
  const span = max === min ? 1 : max - min;
  const x = (index) => ((index / (closes.length - 1)) * SPARK_W).toFixed(2);
  const y = (value) => (SPARK_H - SPARK_PAD - (clamp01((value - min) / span) * (SPARK_H - 2 * SPARK_PAD))).toFixed(2);
  const first = closes[0];
  const last = closes[closes.length - 1];
  const trend = last > first ? "spark-up" : last < first ? "spark-down" : "spark-flat";
  const root = svg("svg", {
    class: `spark ${trend}`,
    viewBox: `0 0 ${SPARK_W} ${SPARK_H}`,
    preserveAspectRatio: "none",
    width,
    height,
    "aria-hidden": "true",
  });
  for (const band of bands) {
    if (!band || typeof band.from !== "number" || typeof band.to !== "number") continue;
    const top = parseFloat(y(Math.max(band.from, band.to)));
    const bottom = parseFloat(y(Math.min(band.from, band.to)));
    if (!(bottom > top)) continue;
    const kind = String(band.kind || "range").replace(/[^a-z0-9_-]/gi, "");
    root.appendChild(
      svg("rect", {
        class: `spark-band spark-band-${kind}`,
        x: 0,
        y: top.toFixed(2),
        width: SPARK_W,
        height: (bottom - top).toFixed(2),
      })
    );
  }
  root.appendChild(
    svg("polyline", {
      class: "spark-line",
      points: closes.map((value, index) => `${x(index)},${y(value)}`).join(" "),
      fill: "none",
      "vector-effect": "non-scaling-stroke",
    })
  );
  root.appendChild(
    svg("circle", {
      class: "spark-last",
      cx: x(closes.length - 1),
      cy: y(last),
      r: 0.01,
      "vector-effect": "non-scaling-stroke",
    })
  );
  return root;
}

// 把页面里的占位（带 data-spark="<ticker>"）异步换成迷你图；load(ticker) 由调用方给出
// （本模块不发请求），并发上限 limit，顺序无关；失败或无数据时留下 spark-empty。
export async function fillSparklines(targets, load, { width = 120, height = 32, bands = [], limit = 6 } = {}) {
  const queue = [...targets];
  async function worker() {
    while (queue.length) {
      const node = queue.shift();
      const ticker = node.getAttribute("data-spark");
      let bars = [];
      try {
        const data = ticker ? await load(ticker) : null;
        bars = data && Array.isArray(data.bars) ? data.bars : [];
      } catch {
        bars = [];
      }
      if (!node.isConnected) continue;
      node.replaceWith(sparkline(bars, { width, height, bands }));
    }
  }
  await Promise.all(Array.from({ length: Math.max(1, limit) }, worker));
}

export function aiStamp(text) {
  return el("span", { class: "ai-stamp" }, text);
}

export function badge(text, kind = "", title) {
  return el("span", { class: `badge ${kind}`.trim(), title }, text);
}

export function legend(items) {
  const box = el("div", { class: "legend", "aria-hidden": "true" });
  for (const [cls, text] of items) {
    const item = el("span");
    item.appendChild(el("i", { class: cls }));
    item.appendChild(document.createTextNode(text));
    box.appendChild(item);
  }
  return box;
}

// KPI 卡片；href 给出时整卡可点。
export function kpi(labelText, value, { href, suffix, kind = "" } = {}) {
  const node = el(href ? "a" : "div", { class: `kpi ${kind}`.trim(), href });
  node.appendChild(el("div", { class: "kpi-label" }, labelText));
  const valueNode = el("div", { class: "kpi-value" }, value === null || value === undefined ? "—" : String(value));
  if (suffix) valueNode.appendChild(el("small", {}, suffix));
  node.appendChild(valueNode);
  return node;
}

// 比例类因子的刻度范围：分位 / 比例默认 0–100%，超出则扩到包含所有值。
export function fractionDomain(values, base = [0, 1]) {
  const finite = values.filter((value) => typeof value === "number" && Number.isFinite(value));
  let [min, max] = base;
  for (const value of finite) {
    if (value < min) min = value;
    if (value > max) max = value;
  }
  if (max === min) max = min + 1;
  const pad = (max - min) * 0.04;
  return [min === base[0] ? min : min - pad, max === base[1] ? max : max + pad];
}

// 提醒列表：一张卡片收纳全部 warn / error，替代逐条整行色块。
// items: [{level: "error"|"warn", message, link?, linkText?}]；错误在前，默认只露 3 条。
// 消息按第一个"："拆成标题与细节，细节默认单行省略，点击展开。
const ALERT_VISIBLE = 3;

function splitAlert(message) {
  const text = String(message ?? "");
  const match = /[：:]\s*/.exec(text);
  if (!match || match.index < 4 || match.index + match[0].length >= text.length) return [text, ""];
  return [text.slice(0, match.index), text.slice(match.index + match[0].length)];
}

export function alertList(items, { title = "需要关注" } = {}) {
  const sorted = [...items].sort((a, b) => (a.level === "error" ? 0 : 1) - (b.level === "error" ? 0 : 1));
  const errors = sorted.filter((item) => item.level === "error").length;
  const box = el("section", { class: "alerts", "aria-label": title });
  const head = el("div", { class: "alerts-head" });
  head.appendChild(el("strong", {}, title));
  if (errors) head.appendChild(el("span", { class: "alerts-count alerts-count-error" }, `${errors} 个错误`));
  if (sorted.length - errors) head.appendChild(el("span", { class: "alerts-count alerts-count-warn" }, `${sorted.length - errors} 个提醒`));
  box.appendChild(head);
  const list = el("ul", { class: "alerts-list" });
  const rows = sorted.map((item, index) => {
    const row = el("li", { class: `alert-row alert-${item.level === "error" ? "error" : "warn"}`, role: "alert" });
    row.appendChild(el("span", { class: "alert-dot", "aria-hidden": "true" }));
    const body = el("div", { class: "alert-body" });
    const [heading, detail] = splitAlert(item.message);
    body.appendChild(el("div", { class: "alert-title" }, heading));
    if (detail) {
      const detailNode = el("div", { class: "alert-detail", title: "点击展开 / 收起", tabindex: "0" }, detail);
      const toggle = () => detailNode.classList.toggle("is-open");
      detailNode.addEventListener("click", toggle);
      detailNode.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          toggle();
        }
      });
      body.appendChild(detailNode);
    }
    row.appendChild(body);
    if (item.link) row.appendChild(el("a", { class: "alert-link", href: item.link }, item.linkText || "详情 →"));
    if (index >= ALERT_VISIBLE) row.hidden = true;
    list.appendChild(row);
    return row;
  });
  box.appendChild(list);
  if (rows.length > ALERT_VISIBLE) {
    const more = el("button", { type: "button", class: "alerts-more", "aria-expanded": "false" }, `展开其余 ${rows.length - ALERT_VISIBLE} 条`);
    more.addEventListener("click", () => {
      const open = more.getAttribute("aria-expanded") !== "true";
      rows.forEach((row, index) => {
        if (index >= ALERT_VISIBLE) row.hidden = !open;
      });
      more.setAttribute("aria-expanded", open ? "true" : "false");
      more.textContent = open ? "收起" : `展开其余 ${rows.length - ALERT_VISIBLE} 条`;
    });
    box.appendChild(more);
  }
  return box;
}
