import { el } from "./dom.js";
import { formatDateTime } from "./format.js";
import { svg } from "./ui.js";

// ---- 共用小件 ---------------------------------------------------------

// 线性图标（路径取自 _icons.html，颜色继承文字色）。
const ICON_PATHS = Object.freeze({
  alert: ["M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z", "M12 9v4M12 17h.01"],
  file: ["M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z", "M14 3v5h5M9 13h6M9 17h4"],
  cal: ["M5 5h14a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2z", "M3 10h18M8 3v4M16 3v4"],
  chev: ["M9 6l6 6-6 6"],
  arrow: ["M5 12h14M13 6l6 6-6 6"],
  funnel: ["M3 4h18l-7 8.5V19l-4 2v-8.5z"],
  eye: ["M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z", "M9 12a3 3 0 1 0 6 0 3 3 0 1 0-6 0"],
  brief: ["M5 7h14a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V9a2 2 0 0 1 2-2z", "M8 7V5a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2M3 13h18"],
  clock: ["M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18z", "M12 7v5l3 2"],
  play: ["M8 5v14l11-7z"],
  msg: ["M21 15a2 2 0 0 1-2 2H8l-5 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"],
  more: ["M5 12h.01M12 12h.01M19 12h.01"],
  ai: ["M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9z", "M19 16v4M17 18h4"],
  info: ["M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18z", "M12 11v5M12 8h.01"],
  check: ["M5 12.5l4.5 4.5L19 7"],
  refresh: ["M21 12a9 9 0 1 1-2.6-6.4L21 8", "M21 3v5h-5"],
});

export function icon(name, size = 16) {
  const root = svg("svg", {
    class: "ico",
    viewBox: "0 0 24 24",
    width: size,
    height: size,
    fill: "none",
    stroke: "currentColor",
    "stroke-width": 1.75,
    "stroke-linecap": "round",
    "stroke-linejoin": "round",
    "aria-hidden": "true",
    focusable: "false",
  });
  for (const d of ICON_PATHS[name] || []) root.appendChild(svg("path", { d }));
  return root;
}

// "2026-10-09 19:31" → "10-09 19:31"（北京时间，与 formatDateTime 一致）。
export function shortStamp(value) {
  const text = formatDateTime(value);
  return text === "—" ? "—" : text.slice(5);
}

// 只放不含交易含义的研究优先级文案（与后端 SUGGESTION_LABELS 对应）。
const PRIORITY_LABELS = Object.freeze({
  high_research_priority: ["高优先", "accent"],
  research_priority: ["中优先", "accent"],
  monitor: ["持续跟踪", ""],
  defer: ["暂缓", ""],
  risk_review: ["风险复核", "warn"],
  low_priority: ["低优先", ""],
  needs_review: ["待复核", "warn"],
});

export function priorityBadge(key) {
  const entry = PRIORITY_LABELS[key];
  if (!entry) return el("span", { class: "faint" }, "—");
  return el("span", { class: `badge ${entry[1]}`.trim() }, entry[0]);
}


export function isCompactViewport() {
  return typeof window !== "undefined" && window.matchMedia("(max-width: 56rem)").matches;
}

