// DOM 构造工具：只用 createElement / textContent / setAttribute，
// 绝不以 HTML 字符串写入数据（CSP + XSS 基线）。

import { STATUS_LABELS, label, summarizeError } from "./format.js";

export function el(tag, attributes = {}, text) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attributes)) {
    if (value !== undefined && value !== null && value !== false) node.setAttribute(key, String(value));
  }
  if (text !== undefined) node.textContent = text;
  return node;
}

export function fmt(value) {
  if (value === null || value === undefined || value === "") return "—";
  return String(value);
}

export function fmtNumber(value, digits = 2) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  return value.toLocaleString("zh-CN", { maximumFractionDigits: digits });
}

export function stockLink(ticker) {
  if (!ticker) return null;
  return el("a", { href: `/app/stocks/${encodeURIComponent(ticker)}` }, ticker);
}

export function table(headers, rows) {
  const node = el("table");
  const headRow = el("tr");
  for (const header of headers) headRow.appendChild(el("th", { scope: "col" }, header.label));
  node.appendChild(el("thead")).appendChild(headRow);
  const body = el("tbody");
  for (const row of rows) body.appendChild(row);
  node.appendChild(body);
  return node;
}

export function kvTable(pairs) {
  const node = el("table");
  const body = el("tbody");
  for (const [key, value] of pairs) {
    const tr = el("tr");
    tr.appendChild(el("td", { class: "kv-key" }, key));
    tr.appendChild(el("td", {}, fmt(value)));
    body.appendChild(tr);
  }
  node.appendChild(body);
  return node;
}

export function statusBadge(value) {
  if (!value) return el("span", {}, "—");
  return el("span", { class: `status status-${value}`, title: value }, label(STATUS_LABELS, value));
}

// 原始错误：一行人话，原文收进 <details>（不直接铺满表格）。
export function errorText(raw) {
  const { summary, raw: original } = summarizeError(raw);
  if (!original) return el("span", {}, summary);
  const box = el("details", { class: "error-detail" });
  box.appendChild(el("summary", {}, summary));
  box.appendChild(el("pre", { class: "raw-error" }, original));
  return box;
}

export function clear(node) {
  node.replaceChildren();
  return node;
}
