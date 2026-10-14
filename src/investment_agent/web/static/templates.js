// 模板页（只读）：模板列表与模板详情（策略说明 → 筛选条件 → 候选 / 持仓区间 → 参数 → 版本）。
// 展示规则库中已入库的版本（周筛选实际使用的版本）；文件有未同步修改时提示。
// 所有 DOM 经 dom.js 构造（textContent / setAttribute），不写 HTML 字符串。

import { ApiError, get, renderRegion } from "./api.js";
import { el } from "./dom.js";
import {
  CALIBRATION_LABELS,
  INDUSTRY_GROUP_LABELS,
  ZONE_LABELS,
  factorLabel,
  formatDate,
  formatDateTime,
  formatFactorValue,
  formatThreshold,
  label,
  paramName,
} from "./format.js";

const FILE_STATUS_TEXT = Object.freeze({
  pending_sync: "模板文件有未同步的修改：下次同步（周筛选前或 ia rules sync）后生成新版本。",
  file_missing: "已找不到定义该模板的文件，筛选仍会使用这里显示的已入库版本。",
  file_invalid: "模板文件校验失败，筛选仍会使用这里显示的已入库版本；请运行 ia rules validate 查看问题。",
});

function section(id) {
  return document.querySelector(`#${id}`);
}

function badge(text, extraClass = "", title) {
  return el("span", { class: `badge ${extraClass}`.trim(), title }, text);
}

// 卡片标题：左侧标题，右侧可选说明（说明在窄屏换到下一行）。
function cardHead(title, note) {
  const head = el("div", { class: "card-head tpl-card-title" });
  head.appendChild(el("h2", {}, title));
  if (note) head.appendChild(el("span", { class: "faint small" }, note));
  return head;
}

function calibrationBadge(status) {
  const value = status || "uncalibrated";
  return el(
    "span",
    {
      class: `badge badge-${value === "uncalibrated" ? "uncalibrated" : "calibrated"}`,
      title: value === "uncalibrated" ? "模板默认参数尚未经回测校准，只是假设" : undefined,
    },
    label(CALIBRATION_LABELS, value)
  );
}

function groupChips(groups) {
  const box = el("div", { class: "cluster tpl-chips" });
  for (const group of groups || []) box.appendChild(badge(label(INDUSTRY_GROUP_LABELS, group)));
  return box;
}

// 详情页：移动端顶栏与桌面面包屑末项改写成模板名（外壳按路由给的是模板编号）。
function setShellTitle(text) {
  for (const node of document.querySelectorAll("[data-appbar-title], .crumbs > b")) node.textContent = text;
}

function errorText(error) {
  return error instanceof ApiError ? error.message : "加载失败。";
}

// 本页区域没有 data-endpoint，不用 renderRegion 的"重试"按钮（它依赖 loadRegion）。
function showError(region, text) {
  const notice = el("p", { class: "notice notice-error" }, text);
  renderRegion(region, "ready", notice);
  region.dataset.state = "error";
}

// ---- 列表 ---------------------------------------------------------------

function stat(value, text) {
  const node = el("span", { class: "tpl-stat" });
  node.appendChild(el("b", {}, String(value)));
  node.appendChild(document.createTextNode(` ${text}`));
  return node;
}

function templateCard(item) {
  const card = el("article", { class: "card tpl-card" });
  const head = el("header", { class: "tpl-card-head" });
  const title = el("h2");
  title.appendChild(el("a", { href: `/app/templates/${encodeURIComponent(item.id)}` }, item.name));
  head.appendChild(title);
  head.appendChild(el("span", { class: "code faint tpl-slug" }, `${item.id} · v${item.version}`));
  head.appendChild(el("span", { class: "code faint tpl-ver" }, `v${item.version}`));
  head.appendChild(el("span", { class: "spacer" }));
  const badges = el("span", { class: "tpl-badges" });
  badges.appendChild(calibrationBadge(item.calibration_status));
  if (item.file_status !== "synced") {
    badges.appendChild(badge("待同步", "outline", "模板文件有未同步的修改或问题，详情见模板页"));
  }
  head.appendChild(badges);
  card.appendChild(head);
  card.appendChild(
    el("p", { class: item.thesis ? "tpl-thesis" : "tpl-thesis muted" }, item.thesis || item.description || "（该版本没有策略说明）")
  );
  card.appendChild(groupChips(item.applicable_groups));
  const stats = el("div", { class: "tpl-stats" });
  stats.appendChild(stat(item.screen_condition_count, "条件"));
  stats.appendChild(stat(item.param_count, "参数"));
  const open = stat(item.open_candidates, "候选");
  open.insertBefore(el("span", { class: "tpl-opt" }, "待处理"), open.lastChild);
  stats.appendChild(open);
  stats.appendChild(el("span", { class: "spacer" }));
  const groups = (item.applicable_groups || []).map((group) => label(INDUSTRY_GROUP_LABELS, group)).join(" · ");
  if (groups) stats.appendChild(el("span", { class: "tpl-stat-groups" }, groups));
  stats.appendChild(el("span", { class: "tpl-stat-ver" }, `${item.version_count} 个版本 · 入库 ${formatDate(item.synced_at)}`));
  card.appendChild(stats);
  return card;
}

// 顶部提示条：未校准 / 文件待同步的模板数。
function fillNotice(items) {
  const notice = section("templates-notice");
  if (!notice) return;
  const uncalibrated = items.filter((item) => (item.calibration_status || "uncalibrated") === "uncalibrated").length;
  const unsynced = items.filter((item) => item.file_status !== "synced").length;
  const counts = section("templates-notice-counts");
  const parts = [];
  if (uncalibrated) parts.push(`${uncalibrated} 个模板未校准`);
  if (unsynced) parts.push(`${unsynced} 个文件待同步`);
  if (counts) counts.textContent = parts.join(" · ");
  notice.dataset.counts = String(parts.length);
}

async function loadTemplateList(region) {
  renderRegion(region, "loading");
  try {
    const items = await get("/rule-templates");
    if (!Array.isArray(items) || !items.length) {
      renderRegion(region, "empty", "规则库中还没有模板：运行 ia rules sync 或等待周筛选同步。");
      return;
    }
    fillNotice(items);
    const grid = el("div", { class: "tpl-grid" });
    for (const item of items) grid.appendChild(templateCard(item));
    renderRegion(region, "ready", grid);
  } catch (error) {
    showError(region, errorText(error));
  }
}

// ---- 详情：条件 -----------------------------------------------------------

// 参数 -> 它所约束的因子（用于按因子单位显示参数值）。
function paramFactors(definition) {
  const map = {};
  const walk = (node) => {
    if (!node || typeof node !== "object") return;
    if (typeof node.factor === "string") {
      for (const value of Array.isArray(node.value) ? node.value : [node.value]) {
        const name = paramName(value);
        if (name && !map[name]) map[name] = node.factor;
      }
      return;
    }
    for (const key of ["all", "any"]) for (const child of node[key] || []) walk(child);
  };
  walk(definition.screen);
  for (const key of ["candidate_zones", "holding_zones"]) {
    for (const zone of Object.values(definition[key] || {})) walk(zone);
  }
  return map;
}

function resolve(value, params) {
  if (Array.isArray(value)) return value.map((item) => resolve(item, params));
  const name = paramName(value);
  return name !== null && Object.prototype.hasOwnProperty.call(params, name) ? params[name] : value;
}

function conditionItem(condition, ctx) {
  const item = el("li", { class: "tpl-cond" });
  const head = el("div", { class: "tpl-cond-head" });
  const unit = ctx.units[condition.factor];
  const text = `${factorLabel(condition.factor)} ${formatThreshold(condition.op, resolve(condition.value, ctx.params), unit)}`;
  if (condition.label) {
    head.appendChild(el("strong", { class: "tpl-cond-label" }, condition.label));
    head.appendChild(el("span", { class: "tpl-cond-text" }, text));
  } else {
    head.appendChild(el("strong", { class: "tpl-cond-label" }, text));
  }
  const names = (Array.isArray(condition.value) ? condition.value : [condition.value])
    .map(paramName)
    .filter((name) => name !== null);
  for (const name of names) head.appendChild(el("code", { class: "tpl-param-ref", title: "参数名，可在单股规则中调整" }, name));
  item.appendChild(head);
  if (condition.why) item.appendChild(el("p", { class: "tpl-cond-why" }, condition.why));
  if (condition.data_note) item.appendChild(el("p", { class: "tpl-cond-note" }, `数据：${condition.data_note}`));
  return item;
}

function conditionList(group, ctx) {
  const list = el("ul", { class: "tpl-conds" });
  if (!group || typeof group !== "object") return list;
  const mode = group.all ? "all" : "any";
  for (const child of group[mode] || []) {
    if (child && typeof child.factor === "string") {
      list.appendChild(conditionItem(child, ctx));
      continue;
    }
    const nestedMode = child && child.all ? "all" : "any";
    const nested = el("li", { class: "tpl-cond tpl-cond-group" });
    nested.appendChild(el("p", { class: "faint small" }, nestedMode === "all" ? "以下全部满足：" : "以下满足其一："));
    nested.appendChild(conditionList(child, ctx));
    list.appendChild(nested);
  }
  return list;
}

function modeHint(group) {
  return group && group.any ? "满足任一条即触发" : "全部满足才触发";
}

function zonesCard(title, zones, ctx, note, extraClass = "") {
  const card = el("section", { class: `card ${extraClass}`.trim() });
  card.appendChild(cardHead(title));
  if (note) card.appendChild(el("p", { class: "small faint tpl-card-note" }, note));
  const names = Object.keys(zones || {});
  if (!names.length) {
    card.appendChild(el("p", { class: "muted" }, "未定义。"));
    return card;
  }
  for (const name of names) {
    const block = el("div", { class: "tpl-zone" });
    const head = el("h3", {}, label(ZONE_LABELS, name));
    head.appendChild(el("span", { class: "faint small" }, ` · ${modeHint(zones[name])}`));
    block.appendChild(head);
    block.appendChild(conditionList(zones[name], ctx));
    card.appendChild(block);
  }
  return card;
}

// ---- 详情：说明 / 参数 / 版本 --------------------------------------------

function listBlock(title, items, cls) {
  const box = el("div", { class: `tpl-notes ${cls}`.trim() });
  box.appendChild(el("h3", {}, title));
  const list = el("ul");
  for (const item of items) list.appendChild(el("li", {}, item));
  box.appendChild(list);
  return box;
}

function thesisCard(def) {
  const card = el("section", { class: "card tpl-wide" });
  card.appendChild(cardHead("策略说明"));
  if (!def.thesis && !def.falsification) {
    card.appendChild(el("p", { class: "muted" }, "该版本没有策略说明（模板格式 v3 之前入库）。"));
    if (def.description) card.appendChild(el("p", {}, def.description));
    return card;
  }
  if (def.thesis) card.appendChild(el("p", { class: "tpl-lead" }, def.thesis));
  const grid = el("div", { class: "tpl-notes-grid" });
  if (Array.isArray(def.fits) && def.fits.length) grid.appendChild(listBlock("适合", def.fits, "tpl-fits"));
  if (Array.isArray(def.avoids) && def.avoids.length) grid.appendChild(listBlock("不适合", def.avoids, "tpl-avoids"));
  if (Array.isArray(def.risks) && def.risks.length) grid.appendChild(listBlock("主要风险", def.risks, "tpl-risks"));
  if (grid.children.length) card.appendChild(grid);
  if (def.falsification) {
    const box = el("div", { class: "tpl-falsify" });
    box.appendChild(el("strong", {}, "证伪："));
    box.appendChild(document.createTextNode(def.falsification));
    card.appendChild(box);
  }
  const meta = el("p", { class: "meta-row small" });
  if (def.horizon) meta.appendChild(el("span", {}, `预期持有周期：${def.horizon}`));
  if (Array.isArray(def.examples) && def.examples.length) {
    const examples = el("span", {}, "典型标的：");
    def.examples.forEach((example, index) => {
      if (index) examples.appendChild(document.createTextNode("，"));
      examples.appendChild(el("a", { href: `/app/stocks/${encodeURIComponent(example.ticker)}` }, example.ticker));
      if (example.note) examples.appendChild(document.createTextNode(`（${example.note}）`));
    });
    meta.appendChild(examples);
  }
  if (meta.children.length) card.appendChild(meta);
  return card;
}

function paramsCard(def, ctx) {
  const card = el("section", { class: "card tpl-wide" });
  card.appendChild(cardHead("参数", "默认值用于筛选；单股规则草稿只能在允许范围内调整。"));
  const names = Object.keys(def.params || {});
  if (!names.length) {
    card.appendChild(el("p", { class: "muted" }, "没有参数。"));
    return card;
  }
  const meta = def.param_meta || {};
  const ranges = def.param_ranges || {};
  const factors = paramFactors(def);
  const show = (name, value) => {
    const unit = ctx.units[factors[name]];
    return unit ? formatFactorValue(value, unit, { precise: false }) : String(value);
  };
  const tableNode = el("table", { class: "stack-table tpl-params" });
  const headRow = el("tr");
  for (const text of ["参数", "默认值", "允许范围", "说明"]) headRow.appendChild(el("th", { scope: "col" }, text));
  tableNode.appendChild(el("thead")).appendChild(headRow);
  const body = el("tbody");
  for (const name of names) {
    const tr = el("tr");
    const nameCell = el("td", { "data-label": "参数" });
    nameCell.appendChild(el("div", {}, (meta[name] && meta[name].label) || factorLabel(factors[name]) || name));
    nameCell.appendChild(el("code", { class: "faint small" }, name));
    tr.appendChild(nameCell);
    tr.appendChild(el("td", { "data-label": "默认值", class: "num" }, show(name, def.params[name])));
    const range = ranges[name];
    tr.appendChild(
      el("td", { "data-label": "允许范围", class: "num" }, range ? `${show(name, range.min)} – ${show(name, range.max)}` : "—")
    );
    tr.appendChild(el("td", { "data-label": "说明", class: "small" }, (meta[name] && meta[name].help) || ""));
    body.appendChild(tr);
  }
  tableNode.appendChild(body);
  card.appendChild(tableNode);
  return card;
}

function versionsCard(detail) {
  const card = el("section", { class: "card tpl-wide" });
  card.appendChild(cardHead("版本", "内容（含说明文字）变化即生成新版本，旧版本不改；已批准的单股规则仍指向当时的版本。"));
  const tableNode = el("table", { class: "stack-table" });
  const headRow = el("tr");
  for (const text of ["版本", "入库时间", "校准", "内容哈希"]) headRow.appendChild(el("th", { scope: "col" }, text));
  tableNode.appendChild(el("thead")).appendChild(headRow);
  const body = el("tbody");
  for (const item of detail.versions || []) {
    const tr = el("tr", { class: item.version === detail.version ? "is-current" : undefined });
    const cell = el("td", { "data-label": "版本" });
    if (item.version === detail.version) {
      cell.appendChild(el("strong", {}, `v${item.version}（当前查看）`));
    } else {
      cell.appendChild(
        el("a", { href: `/app/templates/${encodeURIComponent(detail.id)}?version=${item.version}` }, `v${item.version}`)
      );
    }
    if (item.version === detail.latest_version) cell.appendChild(el("span", { class: "faint small" }, " · 最新"));
    tr.appendChild(cell);
    tr.appendChild(el("td", { "data-label": "入库时间" }, formatDateTime(item.created_at)));
    const calibration = el("td", { "data-label": "校准" });
    calibration.appendChild(calibrationBadge(item.calibration_status));
    tr.appendChild(calibration);
    const hash = el("td", { "data-label": "内容哈希" });
    hash.appendChild(el("code", { class: "faint" }, String(item.hash).slice(0, 12)));
    tr.appendChild(hash);
    body.appendChild(tr);
  }
  tableNode.appendChild(body);
  card.appendChild(tableNode);
  return card;
}

function headerBlock(detail) {
  const title = section("template-title");
  if (title) {
    title.replaceChildren(document.createTextNode(`${detail.name} `));
    title.appendChild(el("span", { class: "code" }, `${detail.id} · v${detail.version}`));
  }
  const box = el("section", { class: "card tpl-wide tpl-idcard" });
  const line = el("div", { class: "cluster tpl-chips" });
  line.appendChild(calibrationBadge(detail.calibration_status));
  if (detail.file_status !== "synced") line.appendChild(badge("待同步", "outline"));
  for (const group of detail.applicable_groups || []) line.appendChild(badge(label(INDUSTRY_GROUP_LABELS, group)));
  box.appendChild(line);
  const meta = el("p", { class: "meta-row small" });
  meta.appendChild(el("span", {}, `入库 ${formatDateTime(detail.synced_at)}`));
  meta.appendChild(el("span", {}, `${detail.version_count} 个版本`));
  const candidates = el("span");
  candidates.appendChild(document.createTextNode(`待处理候选 ${detail.open_candidates} `));
  if (detail.open_candidates) candidates.appendChild(el("a", { href: "/app/candidates" }, "查看候选 →"));
  meta.appendChild(candidates);
  box.appendChild(meta);
  if (detail.version !== detail.latest_version) {
    const notice = el("p", { class: "notice notice-warn" }, `正在查看历史版本 v${detail.version}。`);
    notice.appendChild(el("a", { href: `/app/templates/${encodeURIComponent(detail.id)}` }, `查看最新 v${detail.latest_version}`));
    box.appendChild(notice);
  }
  if (detail.file_status !== "synced") {
    box.appendChild(el("p", { class: "notice notice-warn" }, FILE_STATUS_TEXT[detail.file_status] || detail.file_status));
  }
  return box;
}

async function loadTemplateDetail(root) {
  const id = root.dataset.templateId;
  const version = new URLSearchParams(window.location.search).get("version");
  const query = version && /^[1-9]\d*$/.test(version) ? `?version=${version}` : "";
  renderRegion(root, "loading");
  try {
    const detail = await get(`/rule-templates/${encodeURIComponent(id)}${query}`);
    const def = detail.definition || {};
    const ctx = { params: def.params || {}, units: detail.factor_units || {} };
    document.title = `${detail.name} · 模板 · zcharlie`;
    setShellTitle(detail.name);
    const container = el("div", { class: "tpl-d-grid" });
    container.appendChild(headerBlock(detail));
    container.appendChild(thesisCard(def));
    const screen = el("section", { class: "card" });
    screen.appendChild(cardHead("筛选条件（进入候选）"));
    screen.appendChild(el("p", { class: "small faint tpl-card-note" }, `${modeHint(def.screen)}；数值为默认参数。`));
    screen.appendChild(conditionList(def.screen, ctx));
    container.appendChild(screen);
    container.appendChild(zonesCard("候选区间", def.candidate_zones, ctx));
    container.appendChild(zonesCard("持仓区间", def.holding_zones, ctx, "P1 只定义、不执行；仓位类因子在 P2 才有数据。", "tpl-wide"));
    container.appendChild(paramsCard(def, ctx));
    container.appendChild(versionsCard(detail));
    renderRegion(root, "ready", container);
  } catch (error) {
    showError(root, error instanceof ApiError && error.status === 404 ? "模板不存在。" : errorText(error));
  }
}

export const TEMPLATE_REGIONS = new Set(["templates-list", "template-detail"]);

export function bootTemplates() {
  const list = section("templates-list");
  if (list) loadTemplateList(list);
  const detail = section("template-detail");
  if (detail) loadTemplateDetail(detail);
}
