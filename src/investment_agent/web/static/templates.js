// 模板页：模板列表与模板详情（说明 → 筛选条件 → 区间 → 参数 → 版本 → 编辑新版本）。
// 展示规则库中已入库的版本（周筛选实际使用的当前版本）。规则库是唯一作者席（CD6）：
// 在详情页起草下一不可变版本（结构化字段 + screen 段 YAML，服务端解析即校验）、
// 激活/回滚旧版本；config/rule_templates 只作首次导入基线，与当前版本不一致时提示
// 漂移并可显式“从基线导入”。所有 DOM 经 dom.js 构造，不写 HTML 字符串。

import { ApiError, get, lockWhileBusy, post, renderRegion } from "./api.js";
import { clear, el } from "./dom.js";
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
  baseline_changed: "基线文件与规则库当前版本不一致（数据库为准）。需要文件内容时请显式“从基线导入”。",
  file_missing: "已找不到定义该模板的基线文件；模板仅在规则库中维护。",
  file_invalid: "基线文件校验失败，规则库当前版本继续使用；请运行 ia rules validate 查看问题。",
});

// 版本作者（created_by 为空 = 文件基线首次导入）。
const ACTOR_LABELS = Object.freeze({
  api: "API",
  web: "网页",
  cli: "命令行",
  import: "首次导入",
});

const CALIBRATION_OPTIONS = Object.freeze(["uncalibrated", "backtested", "validated"]);

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

// 本页区域没有 data-endpoint，不用 renderRegion 的“重试”按钮（它依赖 loadRegion）。
function showError(region, text) {
  const notice = el("p", { class: "notice notice-error" }, text);
  renderRegion(region, "ready", notice);
  region.dataset.state = "error";
}

// ---- 从基线导入（列表卡片与详情页共用） --------------------------------------

function baselineImportControl(templateId, { onDone }) {
  const wrap = el("div", { class: "tpl-import" });
  const note = el("input", {
    type: "text",
    class: "tpl-import-note",
    "aria-label": `${templateId} 从基线导入说明`,
    placeholder: "导入说明（必填，进入版本历史）",
  });
  const button = el("button", { type: "button", class: "btn btn-sm secondary tpl-import-btn" }, "从基线导入");
  let result = null;
  const showResult = (text, kind) => {
    result?.remove();
    result = el("span", { class: `small notice notice-${kind} tpl-import-result` }, text);
    wrap.appendChild(result);
  };
  button.addEventListener("click", () => {
    const text = note.value.trim();
    if (!text) {
      note.focus();
      showResult("请先填写导入说明。", "warn");
      return;
    }
    const promise = post(`/rule-templates/${encodeURIComponent(templateId)}/baseline-import`, { note: text })
      .then(() => onDone())
      .catch((error) => {
        if (error instanceof ApiError && error.status === 401) {
          globalThis.location.href = "/app/login";
          return;
        }
        showResult(`导入失败：${error.message}`, "error");
      });
    lockWhileBusy(button, promise);
  });
  wrap.appendChild(note);
  wrap.appendChild(button);
  return wrap;
}

// ---- 列表 ---------------------------------------------------------------

function stat(value, text) {
  const node = el("span", { class: "tpl-stat" });
  node.appendChild(el("b", {}, String(value)));
  node.appendChild(document.createTextNode(` ${text}`));
  return node;
}

function templateCard(item, { reload }) {
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
  if (item.file_status === "baseline_changed") {
    const drifted = badge("基线漂移", "outline", FILE_STATUS_TEXT.baseline_changed);
    drifted.setAttribute("data-file-status", item.file_status);
    badges.appendChild(drifted);
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
  if (item.file_status === "baseline_changed") {
    const row = el("div", { class: "tpl-card-import" });
    const which = item.baseline_version
      ? `基线内容与已入库的 v${item.baseline_version} 一致，当前为 v${item.version}。`
      : "基线文件的内容未在规则库中出现过。";
    row.appendChild(el("p", { class: "small faint tpl-import-hint" }, `${which}${FILE_STATUS_TEXT.baseline_changed}`));
    row.appendChild(baselineImportControl(item.id, { onDone: reload }));
    card.appendChild(row);
  }
  return card;
}

// 顶部提示条：未校准 / 基线漂移的模板数。
function fillNotice(items) {
  const notice = section("templates-notice");
  if (!notice) return;
  const uncalibrated = items.filter((item) => (item.calibration_status || "uncalibrated") === "uncalibrated").length;
  const drifted = items.filter((item) => item.file_status === "baseline_changed").length;
  const counts = section("templates-notice-counts");
  const parts = [];
  if (uncalibrated) parts.push(`${uncalibrated} 个模板未校准`);
  if (drifted) parts.push(`${drifted} 个模板基线漂移`);
  if (counts) counts.textContent = parts.join(" · ");
  notice.dataset.counts = String(parts.length);
}

async function loadTemplateList(region) {
  renderRegion(region, "loading");
  const reload = () => loadTemplateList(region);
  try {
    const items = await get("/rule-templates");
    if (!Array.isArray(items) || !items.length) {
      renderRegion(region, "empty", "规则库中还没有模板：运行 ia rules sync 或在空库时用 ia screen run --sync 完成首次导入。");
      return;
    }
    fillNotice(items);
    const grid = el("div", { class: "tpl-grid" });
    for (const item of items) grid.appendChild(templateCard(item, { reload }));
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

function actorText(createdBy) {
  return createdBy ? label(ACTOR_LABELS, createdBy) : "文件基线";
}

function versionsCard(detail, { reload }) {
  const card = el("section", { class: "card tpl-wide" });
  card.appendChild(cardHead("版本", "内容（含说明文字）变化即生成新版本，旧版本不改；已批准的单股规则仍指向当时的版本。"));
  const tableNode = el("table", { class: "stack-table" });
  const headRow = el("tr");
  for (const text of ["版本", "入库时间", "校准", "操作人 / 备注", "内容哈希"]) headRow.appendChild(el("th", { scope: "col" }, text));
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
    if (item.version === detail.latest_version) cell.appendChild(el("span", { class: "faint small" }, " · 最新（生效）"));
    tr.appendChild(cell);
    tr.appendChild(el("td", { "data-label": "入库时间" }, formatDateTime(item.created_at)));
    const calibration = el("td", { "data-label": "校准" });
    calibration.appendChild(calibrationBadge(item.calibration_status));
    tr.appendChild(calibration);
    const who = el("td", { "data-label": "操作人 / 备注", class: "tpl-version-meta" });
    who.appendChild(el("span", { class: "badge outline" }, actorText(item.created_by)));
    if (item.note) who.appendChild(el("p", { class: "small tpl-version-note" }, item.note));
    tr.appendChild(who);
    const hash = el("td", { "data-label": "内容哈希" });
    hash.appendChild(el("code", { class: "faint" }, String(item.hash).slice(0, 12)));
    tr.appendChild(hash);
    body.appendChild(tr);
  }
  tableNode.appendChild(body);
  card.appendChild(tableNode);
  // 激活 / 回滚：把某个历史版本的内容重新存为下一版本（当前 = 最大版本号）。
  const older = (detail.versions || []).filter((item) => item.version !== detail.latest_version);
  if (older.length) {
    const bar = el("div", { class: "tpl-activate-bar" });
    const select = el("select", { "aria-label": `选择要激活的 ${detail.id} 版本` });
    for (const item of older) {
      select.appendChild(el("option", { value: String(item.version) }, `v${item.version}`));
    }
    const note = el("input", {
      type: "text",
      "aria-label": `${detail.id} 激活说明`,
      placeholder: "激活说明（必填，进入版本历史）",
    });
    const button = el("button", { type: "button", class: "btn btn-sm secondary" }, "激活此版本（回滚）");
    const result = el("span", { class: "small tpl-activate-result" });
    button.addEventListener("click", () => {
      const text = note.value.trim();
      if (!text) {
        result.textContent = "请先填写激活说明。";
        note.focus();
        return;
      }
      const promise = post(`/rule-templates/${encodeURIComponent(detail.id)}/activate`, {
        version: Number(select.value),
        note: text,
      })
        .then(() => reload())
        .catch((error) => {
          if (error instanceof ApiError && error.status === 401) {
            globalThis.location.href = "/app/login";
            return;
          }
          result.textContent = `激活失败：${error.message}`;
        });
      lockWhileBusy(button, promise);
    });
    bar.appendChild(el("span", { class: "small faint" }, "激活历史版本 = 把它的内容重新存为下一版本并生效。"));
    bar.appendChild(select);
    bar.appendChild(note);
    bar.appendChild(button);
    bar.appendChild(result);
    card.appendChild(bar);
  }
  return card;
}

// ---- 详情：screen 段 YAML（受限子集，服务端解析即校验兜底） --------------------
// 只需覆盖条件树的形状：{all|any: [...]}，节点是条件映射或嵌套组；值为标量或标量
// 流式列表。沿用 data-sources.js 高级编辑的“自产文本可往返”原则，不实现完整 YAML。

const SAFE_KEY = /^[A-Za-z_][A-Za-z0-9_-]*$/;
const NUMBER_RE = /^[-+]?(\d+\.?\d*([eE][-+]?\d+)?|\.\d+)([eE][-+]?\d+)?$/;

function isPlainSafe(text) {
  if (text === "" || /^[\s]|[\s]$/.test(text)) return false;
  if (/^[-?:,[\]{}#&*!|>'"%@`]/.test(text)) return false;
  if (text.includes(": ") || text.endsWith(":") || text.includes(" #")) return false;
  if (/^(true|false|null|yes|no|on|off|~)$/i.test(text)) return false;
  if (NUMBER_RE.test(text)) return false;
  return true;
}

function quoteScalar(text) {
  const escaped = String(text)
    .replace(/\\/g, "\\\\")
    .replace(/"/g, '\\"')
    .replace(/\n/g, "\\n")
    .replace(/\t/g, "\\t");
  return `"${escaped}"`;
}

function scalarText(value) {
  if (value === null || value === undefined) return "null";
  if (typeof value === "boolean") return value ? "true" : "false";
  if (typeof value === "number") {
    if (!Number.isFinite(value)) throw new Error(`不支持序列化数字 ${value}`);
    return String(value);
  }
  const text = String(value);
  return isPlainSafe(text) ? text : quoteScalar(text);
}

function flowList(items) {
  return `[${items.map((item) => scalarText(item)).join(", ")}]`;
}

// 节点 = 条件映射（factor/op/value/label/why/data_note）或 {all|any: [节点…]}。
// 映射列表逐项一块：首行“- 首键”，续键与首键对齐（与解析端约定一致）。
function emitMapping(node, keyLevel, lines, itemized = false) {
  const keys = Object.keys(node || {}).filter((key) => node[key] !== undefined && node[key] !== null);
  keys.forEach((key, index) => {
    if (!SAFE_KEY.test(key)) throw new Error(`不支持序列化键名：${key}`);
    const lead =
      itemized && index === 0
        ? `${"  ".repeat(keyLevel - 1)}- ${key}`
        : `${"  ".repeat(keyLevel)}${key}`;
    const value = node[key];
    if (Array.isArray(value)) {
      const objects = value.every((item) => item && typeof item === "object" && !Array.isArray(item));
      if (!value.length) lines.push(`${lead}: []`);
      else if (objects) {
        lines.push(`${lead}:`);
        for (const item of value) emitMapping(item, keyLevel + 2, lines, true);
      } else {
        lines.push(`${lead}: ${flowList(value)}`);
      }
      return;
    }
    lines.push(`${lead}: ${scalarText(value)}`);
  });
}

export function screenYamlText(tree) {
  const lines = [];
  emitMapping(tree || {}, 0, lines);
  return lines.length ? `${lines.join("\n")}\n` : "";
}

function tokenizeYaml(text) {
  const lines = [];
  for (const raw of String(text ?? "").split(/\r?\n/)) {
    const line = raw.replace(/\t/g, "  ");
    if (!line.trim() || /^\s*#/.test(line)) continue;
    lines.push({
      indent: line.length - line.trimStart().length,
      text: line.trim(),
      raw: line.trimEnd(),
    });
  }
  return lines;
}

function stripComment(text) {
  const match = /(^|\s)#/.exec(text);
  return match ? text.slice(0, match.index).trimEnd() : text;
}

function parseQuoted(text, quote) {
  for (let i = 1; i < text.length; i += 1) {
    if (quote === '"' && text[i] === "\\") {
      i += 1;
      continue;
    }
    if (text[i] !== quote) continue;
    if (quote === "'" && text[i + 1] === "'") {
      i += 1;
      continue;
    }
    const inner = text.slice(1, i);
    if (quote === "'") return inner.replace(/''/g, "'");
    return inner
      .replace(/\\u([0-9a-fA-F]{4})/g, (_, code) => String.fromCharCode(parseInt(code, 16)))
      .replace(/\\n/g, "\n")
      .replace(/\\t/g, "\t")
      .replace(/\\"/g, '"')
      .replace(/\\\\/g, "\\");
  }
  throw new Error(`无法解析的引号标量：${text}`);
}

export function parseScalarToken(raw) {
  const text = String(raw).trim();
  if (text === "") return null;
  if (text.startsWith('"')) return parseQuoted(text, '"');
  if (text.startsWith("'")) return parseQuoted(text, "'");
  const plain = stripComment(text);
  if (plain === "") return null;
  if (/^null$|^~$/i.test(plain)) return null;
  if (/^true$/i.test(plain)) return true;
  if (/^false$/i.test(plain)) return false;
  if (NUMBER_RE.test(plain)) return Number(plain);
  return plain;
}

function parseFlowList(raw) {
  const text = String(raw).trim();
  if (!text.startsWith("[") || !text.endsWith("]")) throw new Error(`无法解析的列表：${text}`);
  const inner = text.slice(1, -1).trim();
  if (!inner) return [];
  const items = [];
  let current = "";
  let quote = "";
  for (const char of inner) {
    if (quote) {
      current += char;
      if (char === quote) quote = "";
      continue;
    }
    if (char === '"' || char === "'") {
      quote = char;
      current += char;
      continue;
    }
    if (char === ",") {
      items.push(current);
      current = "";
      continue;
    }
    current += char;
  }
  if (current.trim()) items.push(current);
  return items.map((item) => parseScalarToken(item));
}

// 游标式解析（映射 / 映射列表 / 标量 / 流式列表），返回 {node, next}。
function parseBlock(lines, start, indent) {
  if (start >= lines.length || lines[start].indent < indent) return { node: null, next: start };
  const isItem = (line) => line.text === "-" || line.text.startsWith("- ");
  if (isItem(lines[start])) {
    const items = [];
    let i = start;
    while (i < lines.length && lines[i].indent === indent && isItem(lines[i])) {
      const rest = lines[i].text === "-" ? "" : lines[i].text.slice(2).trim();
      if (rest === "") throw new Error(`第 ${i + 1} 行：列表项不能为空`);
      if (!/^([A-Za-z_][A-Za-z0-9_-]*)\s*:/.test(rest)) {
        throw new Error(`第 ${i + 1} 行：列表项必须是映射（${rest}）`);
      }
      // 列表项 = 首键折回为更深的一行 + 其后所有更深缩进的行，统一按映射块解析。
      const inner = [{ indent: indent + 2, text: rest, raw: rest }];
      let j = i + 1;
      while (j < lines.length && lines[j].indent > indent) {
        inner.push(lines[j]);
        j += 1;
      }
      const child = parseBlock(inner, 0, indent + 2);
      if (child.next < inner.length) {
        throw new Error(`第 ${i + 1} 行附近：无法解析（${inner[child.next].text.trim()}）`);
      }
      items.push(child.node);
      i = j;
    }
    return { node: items, next: i };
  }
  const node = {};
  let i = start;
  while (i < lines.length && lines[i].indent === indent) {
    const match = /^([A-Za-z_][A-Za-z0-9_-]*)\s*:(.*)$/.exec(lines[i].text);
    if (!match) throw new Error(`第 ${i + 1} 行附近：无法解析（${lines[i].text.trim()}）`);
    const key = match[1];
    if (Object.prototype.hasOwnProperty.call(node, key)) throw new Error(`第 ${i + 1} 行：键 ${key} 重复`);
    const raw = match[2].trim();
    // 引号标量可能包含 #，先于注释剥离判断；其余先去注释再分流（空 = 子块）。
    if (raw.startsWith('"') || raw.startsWith("'")) {
      node[key] = parseScalarToken(raw);
      i += 1;
      continue;
    }
    const rest = stripComment(raw);
    if (rest === "") {
      if (i + 1 < lines.length && lines[i + 1].indent > indent) {
        const child = parseBlock(lines, i + 1, lines[i + 1].indent);
        node[key] = child.node;
        i = child.next;
        continue;
      }
      node[key] = null;
      i += 1;
      continue;
    }
    if (rest.startsWith("[")) {
      node[key] = parseFlowList(rest);
      i += 1;
      continue;
    }
    node[key] = parseScalarToken(rest);
    i += 1;
  }
  return { node, next: i };
}

export function parseScreenYaml(text) {
  try {
    const lines = tokenizeYaml(text);
    if (!lines.length) return { error: "条件树为空" };
    if (lines[0].indent !== 0) return { error: "首行必须顶格" };
    const { node, next } = parseBlock(lines, 0, 0);
    if (next < lines.length) return { error: `第 ${next + 1} 行附近：缩进不一致（${lines[next].text.trim()}）` };
    if (!node || typeof node !== "object" || Array.isArray(node)) return { error: "条件树必须是 {all: …} 或 {any: …}" };
    const has = Object.keys(node);
    if (has.length !== 1 || !["all", "any"].includes(has[0]) || !Array.isArray(node[has[0]])) {
      return { error: "条件树必须恰好一个顶层键：all 或 any，值为条件列表" };
    }
    return { value: node };
  } catch (error) {
    return { error: error instanceof Error ? error.message : String(error) };
  }
}

// ---- 详情：编辑新版本 ---------------------------------------------------------

// 结构化编辑的字段 -> 校验 issue path 前缀。
const ISSUE_FIELDS = Object.freeze([
  { keys: ["name"], label: "名称" },
  { keys: ["description"], label: "描述" },
  { keys: ["thesis"], label: "策略说明" },
  { keys: ["falsification"], label: "证伪条件" },
  { keys: ["horizon"], label: "预期持有周期" },
  { keys: ["fits"], label: "适合" },
  { keys: ["avoids"], label: "不适合" },
  { keys: ["risks"], label: "主要风险" },
  { keys: ["examples"], label: "典型标的" },
  { keys: ["calibration_status"], label: "校准状态" },
  { keys: ["applicable_groups"], label: "适用行业分组" },
  { keys: ["screen"], label: "筛选条件（screen YAML）" },
]);

const DOC_KEY_LABELS = Object.freeze({
  name: "名称",
  description: "描述",
  thesis: "策略说明",
  falsification: "证伪条件",
  horizon: "预期持有周期",
  fits: "适合",
  avoids: "不适合",
  risks: "主要风险",
  examples: "典型标的",
  calibration_status: "校准状态",
  applicable_groups: "适用行业分组",
  screen: "筛选条件",
});

function canonicalJson(value) {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (value && typeof value === "object") {
    const parts = Object.keys(value)
      .sort()
      .map((key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`);
    return `{${parts.join(",")}}`;
  }
  return JSON.stringify(value ?? null);
}

function briefValue(value) {
  if (value === undefined) return "（未设置）";
  if (value === null) return "（空）";
  if (Array.isArray(value)) return value.length ? `${value.length} 项` : "（空）";
  if (typeof value === "object") return `${Object.keys(value).length} 个键`;
  return String(value);
}

// 草稿 vs 当前版本的逐键差异（保存前确认）。
export function diffAgainstCurrent(base, draft) {
  const entries = [];
  for (const key of new Set([...Object.keys(base || {}), ...Object.keys(draft || {})])) {
    if (canonicalJson(base?.[key]) === canonicalJson(draft?.[key])) continue;
    entries.push({ key, label: DOC_KEY_LABELS[key] || key, from: base?.[key], to: draft?.[key] });
  }
  return entries;
}

function parseLines(text) {
  return String(text ?? "")
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);
}

function parseExamples(text) {
  const items = [];
  for (const line of parseLines(text)) {
    const [ticker, ...rest] = line.split(/\s+/);
    items.push({ ticker, note: rest.join(" ") || null });
  }
  return items;
}

async function loadGroupOptions() {
  // 分组多选项来自 DB 的行业分组文档（config-docs WP3）。
  try {
    const doc = await get("/config-docs/industry_groups");
    const groups = doc && doc.value && doc.value.groups;
    if (groups && typeof groups === "object") {
      return Object.keys(groups).sort().map((id) => ({ id, desc: groups[id] }));
    }
  } catch {
    // 读不到文档时退回静态标签表里的已知分组。
  }
  return Object.keys(INDUSTRY_GROUP_LABELS).map((id) => ({ id, desc: "" }));
}

function editorCard(detail, { reload }) {
  const card = el("section", { class: "card tpl-wide", id: "template-edit" });
  card.appendChild(cardHead("编辑新版本", "保存 = 写入下一不可变版本并立即生效；需填写变更说明。"));
  if (detail.version !== detail.latest_version) {
    const notice = el("p", { class: "notice notice-warn" }, `正在查看历史版本 v${detail.version}；起草以最新 v${detail.latest_version} 为起点。`);
    notice.appendChild(el("a", { href: `/app/templates/${encodeURIComponent(detail.id)}` }, "查看最新版本"));
    card.appendChild(notice);
    return card;
  }
  const base = structuredClone(detail.definition || {});
  const chosenGroups = new Set(base.applicable_groups || []);
  const container = el("div", { class: "tpl-edit" });
  const result = el("div", { class: "tpl-edit-result" });
  const diffBox = el("div", { class: "tpl-edit-diff" });
  const fieldIssues = new Map(); // 字段名 -> <ul>

  function issueListFor(fieldLabel) {
    if (!fieldIssues.has(fieldLabel)) {
      fieldIssues.set(fieldLabel, el("ul", { class: "issue-list tpl-field-issues" }));
    }
    return fieldIssues.get(fieldLabel);
  }

  function clearIssues() {
    for (const list of fieldIssues.values()) clear(list);
  }

  // 把服务端 issue 归到结构化字段；不属于编辑面的（params / zones 等沿用部分）汇总返回。
  function renderIssues(issues) {
    clearIssues();
    const general = [];
    for (const issue of issues || []) {
      const head = String(issue.path ?? "").split(/[.\[]/, 1)[0] || "$";
      const field = ISSUE_FIELDS.find((item) => item.keys.includes(head));
      const line = `[${issue.code}] ${issue.path}：${issue.message}`;
      if (field) issueListFor(field.label).appendChild(el("li", {}, line));
      else general.push(line);
    }
    return general;
  }

  function textInput(labelText, value, { rows = 1, placeholder } = {}) {
    const fieldId = `tpl-edit-${labelText}`;
    const wrap = el("div", { class: "tpl-field" });
    wrap.appendChild(el("label", { for: fieldId }, labelText));
    const control = el(rows > 1 ? "textarea" : "input", {
      id: fieldId,
      rows: rows > 1 ? rows : undefined,
      "aria-label": `${detail.id} ${labelText}`,
      placeholder,
    });
    control.value = value ?? "";
    wrap.appendChild(control);
    wrap.appendChild(issueListFor(labelText));
    container.appendChild(wrap);
    return control;
  }

  const name = textInput("名称", base.name ?? "");
  const description = textInput("描述", base.description ?? "", { rows: 2 });
  const thesis = textInput("策略说明", base.thesis ?? "", { rows: 3, placeholder: "一句话投资逻辑" });
  const falsification = textInput("证伪条件", base.falsification ?? "", { rows: 2, placeholder: "什么情况说明该逻辑不成立" });
  const horizon = textInput("预期持有周期", base.horizon ?? "", { placeholder: "如 1–3 年" });
  const fits = textInput("适合", (base.fits || []).join("\n"), { rows: 3, placeholder: "每行一条" });
  const avoids = textInput("不适合", (base.avoids || []).join("\n"), { rows: 2, placeholder: "每行一条" });
  const risks = textInput("主要风险", (base.risks || []).join("\n"), { rows: 2, placeholder: "每行一条" });
  const examples = textInput("典型标的", (base.examples || []).map((item) => [item.ticker, item.note].filter(Boolean).join(" ")).join("\n"), {
    rows: 2,
    placeholder: "每行一个：600519.SH 可附一句话说明",
  });

  const calibrationWrap = el("div", { class: "tpl-field" });
  calibrationWrap.appendChild(el("label", {}, "校准状态"));
  const calibration = el("select", { "aria-label": `${detail.id} 校准状态` });
  const currentStatus = base.calibration_status || "uncalibrated";
  const statusOptions = CALIBRATION_OPTIONS.includes(currentStatus)
    ? CALIBRATION_OPTIONS
    : [currentStatus, ...CALIBRATION_OPTIONS];
  for (const option of statusOptions) {
    calibration.appendChild(el("option", { value: option }, label(CALIBRATION_LABELS, option)));
  }
  calibration.value = currentStatus;
  calibrationWrap.appendChild(calibration);
  calibrationWrap.appendChild(issueListFor("校准状态"));
  container.appendChild(calibrationWrap);

  const groupsWrap = el("div", { class: "tpl-field tpl-field-groups" });
  groupsWrap.appendChild(el("label", {}, "适用行业分组"));
  const groupsBox = el("div", { class: "tpl-groups" });
  groupsWrap.appendChild(groupsBox);
  groupsWrap.appendChild(issueListFor("适用行业分组"));
  container.appendChild(groupsWrap);

  const screenWrap = el("div", { class: "tpl-field tpl-field-screen" });
  screenWrap.appendChild(el("label", { for: "tpl-edit-screen" }, "筛选条件（screen YAML）"));
  screenWrap.appendChild(el("p", { class: "small faint" }, "条件树：一个顶层 all / any，条件为 {factor, op, value}，value 可为数字、$参数引用或 [低, 高]。"));
  const screenInput = el("textarea", { id: "tpl-edit-screen", rows: 10, class: "tpl-yaml", "aria-label": `${detail.id} 筛选条件 YAML`, spellcheck: "false" });
  screenInput.value = screenYamlText(base.screen);
  const screenStatus = el("ul", { class: "issue-list tpl-field-issues" });
  screenInput.addEventListener("input", () => {
    const parsed = parseScreenYaml(screenInput.value);
    clear(screenStatus);
    if (parsed.error) screenStatus.appendChild(el("li", {}, `YAML 无法解析：${parsed.error}`));
  });
  screenWrap.appendChild(screenInput);
  screenWrap.appendChild(screenStatus);
  screenWrap.appendChild(issueListFor("筛选条件（screen YAML）"));
  container.appendChild(screenWrap);

  card.appendChild(el("p", { class: "small faint" }, "候选 / 持仓区间与参数沿用当前版本不变（本次编辑面为叙事字段、分组与筛选条件）。"));

  function buildDocument() {
    const doc = structuredClone(base);
    delete doc.version;
    doc.name = name.value.trim() || base.name;
    doc.description = description.value.trim() || "";
    const setText = (key, value) => {
      if (value) doc[key] = value;
      else delete doc[key];
    };
    setText("thesis", thesis.value.trim() || null);
    setText("falsification", falsification.value.trim() || null);
    setText("horizon", horizon.value.trim() || null);
    const setList = (key, text) => {
      const items = parseLines(text);
      if (items.length) doc[key] = items;
      else delete doc[key];
    };
    setList("fits", fits.value);
    setList("avoids", avoids.value);
    setList("risks", risks.value);
    const exampleItems = parseExamples(examples.value);
    if (exampleItems.length) doc.examples = exampleItems;
    else delete doc.examples;
    doc.calibration_status = calibration.value;
    doc.applicable_groups = [...chosenGroups].sort();
    const parsed = parseScreenYaml(screenInput.value);
    if (parsed.error) throw new Error(parsed.error);
    doc.screen = parsed.value;
    return doc;
  }

  function showResult(lines, kind) {
    clear(result);
    if (!lines.length) return;
    const notice = el("div", { class: `notice${kind ? ` notice-${kind}` : ""}` });
    if (lines.length === 1) notice.textContent = lines[0];
    else {
      const list = el("ul", { class: "issue-list" });
      for (const line of lines) list.appendChild(el("li", {}, line));
      notice.appendChild(list);
    }
    result.appendChild(notice);
  }

  const bar = el("div", { class: "tpl-edit-bar" });
  const check = el("button", { type: "button", class: "btn btn-sm secondary" }, "校验");
  const diffButton = el("button", { type: "button", class: "btn btn-sm secondary" }, "与当前版本对比");
  const note = el("input", {
    type: "text",
    class: "tpl-edit-note",
    "aria-label": `${detail.id} 变更说明`,
    placeholder: "变更说明（必填，进入版本历史）",
  });
  const save = el("button", { type: "button", class: "btn btn-sm" }, "保存为新版本");

  check.addEventListener("click", () => {
    let document_;
    try {
      document_ = buildDocument();
    } catch (error) {
      showResult([`YAML 无法解析，未校验：${error.message}`], "warn");
      return;
    }
    const promise = post(`/rule-templates/${encodeURIComponent(detail.id)}/validate`, { document: document_ })
      .then((data) => {
        if (data.ok) {
          clearIssues();
          showResult(["校验通过，可保存为新版本。"], "");
        } else {
          const general = renderIssues(data.issues);
          showResult(["服务端校验未通过，逐字段问题如下：", ...general], "warn");
        }
      })
      .catch((error) => {
        if (error instanceof ApiError && error.status === 401) {
          globalThis.location.href = "/app/login";
          return;
        }
        showResult([`校验失败：${error.message}`], "error");
      });
    lockWhileBusy(check, promise);
  });

  diffButton.addEventListener("click", () => {
    let document_;
    try {
      document_ = buildDocument();
    } catch (error) {
      showResult([`YAML 无法解析，无法对比：${error.message}`], "warn");
      return;
    }
    const entries = diffAgainstCurrent(base, document_);
    clear(diffBox);
    if (!entries.length) {
      diffBox.appendChild(el("p", { class: "small faint" }, "与当前版本无差异；保存不会生成新版本。"));
    } else {
      const head = el("p", { class: "small" }, `与当前版本对比：${entries.length} 处差异`);
      diffBox.appendChild(head);
      const list = el("ul", { class: "tpl-diff-list" });
      for (const entry of entries) {
        list.appendChild(el("li", { class: "tpl-diff-item" }, `${entry.label}：${briefValue(entry.from)} → ${briefValue(entry.to)}`));
      }
      diffBox.appendChild(list);
    }
  });

  save.addEventListener("click", () => {
    let document_;
    try {
      document_ = buildDocument();
    } catch (error) {
      showResult([`YAML 无法解析，未保存：${error.message}`], "warn");
      return;
    }
    const text = note.value.trim();
    if (!text) {
      note.focus();
      showResult(["变更说明必填：写明这次调整的来源与原因（会进入版本历史）。"], "warn");
      return;
    }
    const promise = post(`/rule-templates/${encodeURIComponent(detail.id)}/versions`, { document: document_, note: text })
      .then((data) => {
        if (data.action === "unchanged") {
          showResult(["内容与当前版本一致，未生成新版本。"], "");
          return;
        }
        reload();
      })
      .catch((error) => {
        if (error instanceof ApiError && error.status === 401) {
          globalThis.location.href = "/app/login";
          return;
        }
        if (error instanceof ApiError && error.status === 422) {
          const issues = error.detail && typeof error.detail === "object" ? error.detail.issues : null;
          const general = renderIssues(Array.isArray(issues) ? issues : []);
          showResult(["服务端校验未通过，未保存任何修改：", ...general], "warn");
          return;
        }
        showResult([`保存失败：${error.message}`], "error");
      });
    lockWhileBusy(save, promise);
  });

  bar.appendChild(check);
  bar.appendChild(diffButton);
  bar.appendChild(note);
  bar.appendChild(save);
  card.appendChild(container);
  card.appendChild(bar);
  card.appendChild(diffBox);
  card.appendChild(result);

  // 分组多选：DB 行业分组文档就绪后填充（含当前版本引用但文档没有的分组）。
  loadGroupOptions().then((options) => {
    const known = new Set(options.map((option) => option.id));
    for (const group of chosenGroups) {
      if (!known.has(group)) options.push({ id: group, desc: "（当前版本引用；不在分组文档中）" });
    }
    for (const option of options) {
      const item = el("label", { class: "tpl-group-option" });
      const box = el("input", { type: "checkbox", "data-group": option.id });
      box.checked = chosenGroups.has(option.id);
      box.addEventListener("change", () => {
        if (box.checked) chosenGroups.add(option.id);
        else chosenGroups.delete(option.id);
      });
      item.appendChild(box);
      item.appendChild(document.createTextNode(` ${label(INDUSTRY_GROUP_LABELS, option.id)}${option.desc ? ` · ${option.desc}` : ""}`));
      groupsBox.appendChild(item);
    }
  });

  return card;
}

function headerBlock(detail, { reload }) {
  const title = section("template-title");
  if (title) {
    title.replaceChildren(document.createTextNode(`${detail.name} `));
    title.appendChild(el("span", { class: "code" }, `${detail.id} · v${detail.version}`));
  }
  const box = el("section", { class: "card tpl-wide tpl-idcard" });
  const line = el("div", { class: "cluster tpl-chips" });
  line.appendChild(calibrationBadge(detail.calibration_status));
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
    const notice = el("p", { class: "notice notice-warn" }, FILE_STATUS_TEXT[detail.file_status] || detail.file_status);
    box.appendChild(notice);
    if (detail.file_status === "baseline_changed") {
      if (detail.baseline_version) {
        notice.appendChild(el("span", {}, ` 基线内容与已入库的 v${detail.baseline_version} 一致，当前为 v${detail.version}。`));
      }
      box.appendChild(baselineImportControl(detail.id, { onDone: reload }));
    }
  }
  return box;
}

async function loadTemplateDetail(root) {
  const id = root.dataset.templateId;
  const version = new URLSearchParams(window.location.search).get("version");
  const query = version && /^[1-9]\d*$/.test(version) ? `?version=${version}` : "";
  renderRegion(root, "loading");
  const reload = () => loadTemplateDetail(root);
  try {
    const detail = await get(`/rule-templates/${encodeURIComponent(id)}${query}`);
    const def = detail.definition || {};
    const ctx = { params: def.params || {}, units: detail.factor_units || {} };
    document.title = `${detail.name} · 模板 · zcharlie`;
    setShellTitle(detail.name);
    const container = el("div", { class: "tpl-d-grid" });
    container.appendChild(headerBlock(detail, { reload }));
    container.appendChild(thesisCard(def));
    const screen = el("section", { class: "card" });
    screen.appendChild(cardHead("筛选条件（进入候选）"));
    screen.appendChild(el("p", { class: "small faint tpl-card-note" }, `${modeHint(def.screen)}；数值为默认参数。`));
    screen.appendChild(conditionList(def.screen, ctx));
    container.appendChild(screen);
    container.appendChild(zonesCard("候选区间", def.candidate_zones, ctx));
    container.appendChild(zonesCard("持仓区间", def.holding_zones, ctx, "P1 只定义、不执行；仓位类因子在 P2 才有数据。", "tpl-wide"));
    container.appendChild(paramsCard(def, ctx));
    container.appendChild(versionsCard(detail, { reload }));
    container.appendChild(editorCard(detail, { reload }));
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
