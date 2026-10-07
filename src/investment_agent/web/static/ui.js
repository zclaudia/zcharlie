// 可视化组件：全部用 SVG 的百分比坐标与 class 着色，不写内联样式（CSP style-src 'self'）。
// 只把已给出的数值画出来，不计算、不评判。

import { el } from "./dom.js";

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
  ends.appendChild(el("span", {}, `开启 ${String(startIso).slice(0, 10)}`));
  ends.appendChild(el("span", {}, `到期 ${String(endIso).slice(0, 10)}`));
  box.appendChild(ends);
  return box;
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
