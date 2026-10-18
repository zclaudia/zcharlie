// 助手回复的结构化渲染：正文段落 / 关键数值卡 / 分析推断·假设·材料缺口·局限 板块 / 页脚元信息。
// 后端 Message.text 用单个换行分隔节点与板块，板块内条目以“；”相连；数值节点只有证据引用，
// 名称与报告期由证据记录补齐。这里只重排版式：不计算、不改写数值（精确原值保留在 title）。
import { get } from "./api.js";
import { el } from "./dom.js";
import { FIELD_LABELS } from "./format.js";

const SECTIONS = {
  "分析推断": "infer",
  "假设": "assume",
  "材料缺口": "gap",
  "局限": "limit",
};
const QUALITY = {candidate:"候选待核验", verified:"已核验", user_report:"用户提供未核验"};
const BASIS = {semi:"半年累计", annual:"全年", q1:"一季度累计", q3:"前三季度累计"};
const NUMERIC_LINE = /^([-+]?\d[\d,]*(?:\.\d+)?)(?:\s*(%|[A-Za-z/]+))?$/;

const evidence = new Map(); // "id:revision" -> {label, sub} | Promise

function describe(record) {
  const key = String(record.locator || "").replace(/^fields\./, "");
  const period = record.period || {};
  const basis = String(period.basis || "").split("/").pop();
  const sub = [period.end ? `截至 ${period.end}` : "", BASIS[basis] || ""].filter(Boolean).join(" · ");
  return {label:FIELD_LABELS[key] || key || "数值", sub};
}

function paint(tile, info) {
  tile.querySelector(".ai-fig-k").textContent = info.label;
  tile.querySelector(".ai-fig-s").textContent = info.sub;
}

function loadEvidence(ref) {
  const key = `${ref.id}:${ref.revision}`;
  const cached = evidence.get(key);
  if (cached && !(cached instanceof Promise)) return cached;
  if (!cached) {
    evidence.set(key, get(`/investor/evidence/${ref.id}?revision=${ref.revision}`).then(result => {
      const info = describe(result.data || result);
      evidence.set(key, info);
      document.querySelectorAll(`.ai-fig[data-ev="${key}"]`).forEach(tile => paint(tile, info));
    }).catch(() => evidence.delete(key)));
  }
  return null;
}

// 21971104612.00 CNY -> 219.71 亿元；ratio 倍数类不带单位词；其余原样。
function display(raw) {
  const m = NUMERIC_LINE.exec(raw), n = m ? Number(m[1].replace(/,/g, "")) : NaN;
  if (!m || !Number.isFinite(n)) return {num:raw, unit:""};
  const unit = m[2] || "", round = v => v.toLocaleString("zh-CN", {maximumFractionDigits:2});
  if (unit === "%") return {num:m[1], unit:"%"};
  if (unit === "CNY") {
    if (Math.abs(n) >= 1e8) return {num:round(n / 1e8), unit:"亿元"};
    if (Math.abs(n) >= 1e4) return {num:round(n / 1e4), unit:"万元"};
    return {num:round(n), unit:"元"};
  }
  if (unit.toLowerCase() === "ratio") return {num:round(n), unit:""};
  return {num:round(n), unit};
}

function fig(raw, ref) {
  const {num, unit} = display(raw);
  const tile = el("div", {class:"ai-fig", title:`原值 ${raw}`});
  const v = el("div", {class:"ai-fig-v"}, num);
  if (unit) v.append(el("span", {}, unit));
  tile.append(el("div", {class:"ai-fig-k"}, "数值"), v, el("div", {class:"ai-fig-s"}));
  if (ref) {
    tile.dataset.ev = `${ref.id}:${ref.revision}`;
    const info = loadEvidence(ref);
    if (info) paint(tile, info);
  }
  return tile;
}

function section(kind, title, items) {
  const box = el("section", {class:`ai-sec ai-sec-${kind}`});
  const head = el("h4", {class:"ai-sec-h"}, title);
  if (items.length > 1) head.append(el("span", {class:"ai-sec-n"}, String(items.length)));
  const list = el("ul", {class:"ai-sec-l"});
  items.forEach(item => list.append(el("li", {}, item)));
  box.append(head, list);
  return box;
}

export function renderAnswer(text, content = []) {
  const lines = String(text || "").split(/\n+/).map(s => s.trim()).filter(Boolean);
  const claims = content.filter(n => n.type === "numeric_claim");
  const labelled = claims.length === lines.filter(l => NUMERIC_LINE.test(l)).length;
  const root = el("div", {class:"ai-ans"}), meta = el("div", {class:"ai-ans-meta"});
  let figs = null, index = 0;
  for (const line of lines) {
    if (NUMERIC_LINE.test(line)) {
      if (!figs) { figs = el("div", {class:"ai-figs"}); root.append(figs); }
      figs.append(fig(line, labelled ? claims[index].ref : null));
      index += 1;
      continue;
    }
    figs = null;
    const colon = line.indexOf("：");
    const head = colon > 0 ? line.slice(0, colon) : "", rest = line.slice(colon + 1).trim();
    if (SECTIONS[head]) {
      // 条目以“。；”相连；句内的“；”不拆。
      const items = rest.split(/(?<=[。！？.!?])；/).map(s => s.trim()).filter(Boolean);
      root.append(section(SECTIONS[head], head, items.length ? items : [rest]));
    } else if (head === "停止原因") {
      meta.append(el("p", {class:"ai-meta-row"}, line));
    } else if (head === "来源质量") {
      const row = el("p", {class:"ai-meta-row"});
      row.append("来源质量 ", el("span", {class:`ai-chip${rest === "verified" ? "" : " ai-chip-warn"}`}, QUALITY[rest] || rest));
      meta.append(row);
    } else {
      root.append(el("p", {class:"ai-answer"}, line));
    }
  }
  if (meta.childElementCount) root.append(meta);
  return root;
}
