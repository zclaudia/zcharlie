// 数据源页（config-docs-plan §4.6）：标签 A 字段来源优先级、标签 B 来源信任策略。
// 标签 A：按 table 分组的字段表，每行可编辑调度面（primary / fallback / tolerance / key），
// 口径与校准证据只读（CD1）；高级编辑折叠区是全文档 YAML 编辑器。
// 标签 B：每来源一卡——licensing（状态 / 公司范围 / 引用）与 technical_acceptance
// （状态 / 公司范围 / 适配器版本 / 验收报告行编辑器）；文档顶层 revision id 是受控
// 记录标识，展示在卡片头部。
// 两个标签共用一套控制器（加载 / 全文档 PATCH / 422·409 处理 / 脏标记 / 漂移横幅 /
// 基线差异与导入 / 变更历史抽屉），差异只通过 kind 钩子表达；保存统一 PATCH 全文档，
// 校验以服务端为准（CD3）。所有 DOM 经 dom.js 构造，不写 HTML 字符串。

import { ApiError, get, lockWhileBusy, patch, post, renderRegion } from "./api.js";
import { clear, el, table } from "./dom.js";
import { formatDateTime, label } from "./format.js";
import { registerRefreshGuard } from "./refresh_state.js";

export const DATA_SOURCE_REGIONS = new Set(["ds-form", "dsp-form", "ds-history", "ds-baseline-diff"]);

const ENDPOINT = "/config-docs/field_sources";
const HISTORY_ENDPOINT = "/config-docs/field_sources/history?limit=50";
const BASELINE_IMPORT_ENDPOINT = "/config-docs/field_sources/baseline-import";
const POLICY_ENDPOINT = "/config-docs/source_policy";
const POLICY_HISTORY_ENDPOINT = "/config-docs/source_policy/history?limit=50";
const POLICY_BASELINE_IMPORT_ENDPOINT = "/config-docs/source_policy/baseline-import";

export const TABLE_LABELS = Object.freeze({
  financial: "财务",
  market: "行情",
  earnings: "业绩事件",
  dividend: "分红",
  report_overdue: "披露逾期",
  derived: "派生",
});

// 展示顺序按数据流（financial → derived → market → 披露 / 分红 / 业绩事件）；未知表排最后。
const TABLE_ORDER = ["financial", "derived", "market", "report_overdue", "dividend", "earnings"];

export const DOC_KEY_LABELS = Object.freeze({
  version: "版本",
  default_tolerance: "默认容差",
});

export const FIELD_KEY_LABELS = Object.freeze({
  table: "所属表",
  primary: "主来源",
  fallback: "备用顺序",
  tolerance: "容差",
  key: "关键字段",
  definition: "口径",
  calibration: "校准",
  sanity: "合理性",
  approx: "近似",
  formula: "公式",
});

export const POLICY_DOC_KEY_LABELS = Object.freeze({
  schema_version: "结构版本",
  revision: "修订标识",
});

export const POLICY_SECTION_LABELS = Object.freeze({
  licensing: "许可",
  technical_acceptance: "技术验收",
});

export const POLICY_ROW_KEY_LABELS = Object.freeze({
  status: "状态",
  company_keys: "公司范围",
  citation: "引用",
  adapter_version: "适配器版本",
  reports: "验收报告",
});

// 评估器只特判 approved / accepted（其余取值一律按未批准 / 未验收处理），
// 选项集就是这两个受控取值；文档里的其他历史取值保留为附加选项，不丢失。
export const LICENSING_STATUSES = Object.freeze(["unknown", "approved"]);
export const TECHNICAL_STATUSES = Object.freeze(["not_accepted", "accepted"]);

export const ACTOR_LABELS = Object.freeze({
  web: "网页",
  api: "API",
  cli: "命令行",
  import: "首次导入",
});

// ---- 纯函数（无 DOM，可在 node 中测试） --------------------------------------

export function tableLabel(name) {
  return label(TABLE_LABELS, name);
}

// 键序无关的规范序列化：表单只增删 tolerance / key 时不会产生假的“未保存”。
export function canonicalJson(value) {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (value && typeof value === "object") {
    const parts = Object.keys(value)
      .sort()
      .map((key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`);
    return `{${parts.join(",")}}`;
  }
  return JSON.stringify(value ?? null);
}

export function sameValue(a, b) {
  return canonicalJson(a) === canonicalJson(b);
}

export function sameDocument(a, b) {
  return canonicalJson(a) === canonicalJson(b);
}

// 文档 → 分组视图：[{table, label, names}]，组内保持文档里的字段顺序。
export function tableGroups(doc) {
  const fields = (doc && doc.fields) || {};
  const order = [];
  const byTable = new Map();
  for (const [name, entry] of Object.entries(fields)) {
    const key = (entry && typeof entry === "object" && entry.table) || "unknown";
    if (!byTable.has(key)) {
      byTable.set(key, []);
      order.push(key);
    }
    byTable.get(key).push(name);
  }
  // 已知表按数据流排；未知表排在最后并保持出现顺序（sort 稳定）。
  order.sort((a, b) => {
    const ia = TABLE_ORDER.indexOf(a);
    const ib = TABLE_ORDER.indexOf(b);
    if (ia !== -1 && ib !== -1) return ia - ib;
    if (ia !== -1) return -1;
    if (ib !== -1) return 1;
    return 0;
  });
  return order.map((key) => ({ table: key, label: tableLabel(key), names: byTable.get(key) }));
}

// 某表可选的来源集合：从当前文档该表所有字段的 primary / fallback 出现值推导，
// 不硬编码枚举（例如 market 表自然得到 baostock / tencent）。
export function providerOptions(doc, tableName) {
  const fields = (doc && doc.fields) || {};
  const seen = [];
  for (const entry of Object.values(fields)) {
    if (!entry || entry.table !== tableName) continue;
    const providers = [entry.primary, ...(Array.isArray(entry.fallback) ? entry.fallback : [])];
    for (const provider of providers) {
      if (typeof provider === "string" && !seen.includes(provider)) seen.push(provider);
    }
  }
  return seen;
}

export function formatDocValue(value) {
  if (value === undefined) return "（未设置）";
  if (value === null) return "null";
  if (Array.isArray(value)) return value.length ? value.join("、") : "（空）";
  if (typeof value === "object") return JSON.stringify(value);
  if (typeof value === "boolean") return value ? "是" : "否";
  return String(value);
}

// 策略来源排序：保持文档里的键顺序（受控记录不重排）。
export function policySourceNames(doc) {
  const sources = (doc && doc.sources) || {};
  return Object.keys(sources).filter((name) => sources[name] && typeof sources[name] === "object");
}

function formatPolicyValue(key, value) {
  if (key.endsWith(".reports")) return `${Array.isArray(value) ? value.length : 0} 条`;
  return formatDocValue(value);
}

// 两份全文档的逐项差异（基线漂移、从基线导入预览与历史摘要共用一个视图）。
// kind 决定集合键与标签表：field_sources → fields（字段表）；source_policy →
// sources（每来源的许可 / 技术验收审批行）。
export function diffDocuments(oldDoc, newDoc, kind = "field_sources") {
  const collectionKey = kind === "source_policy" ? "sources" : "fields";
  const keyLabels = kind === "source_policy" ? POLICY_DOC_KEY_LABELS : DOC_KEY_LABELS;
  const before = oldDoc || {};
  const after = newDoc || {};
  const entries = [];
  for (const key of new Set([...Object.keys(before), ...Object.keys(after)])) {
    if (key === collectionKey) continue;
    if (!sameValue(before[key], after[key])) {
      entries.push({
        target: "doc",
        kind: "changed",
        key,
        label: keyLabels[key] || key,
        from: before[key],
        to: after[key],
      });
    }
  }
  const beforeItems = before[collectionKey] || {};
  const afterItems = after[collectionKey] || {};
  for (const [name, entry] of Object.entries(beforeItems)) {
    if (name in afterItems) continue;
    entries.push({
      target: kind === "source_policy" ? "source" : "field",
      kind: "removed",
      name,
      table: entry?.table,
      field: entry,
    });
  }
  for (const [name, entry] of Object.entries(afterItems)) {
    if (!(name in beforeItems)) {
      entries.push({
        target: kind === "source_policy" ? "source" : "field",
        kind: "added",
        name,
        table: entry?.table,
        field: entry,
      });
      continue;
    }
    const changes =
      kind === "source_policy"
        ? policyEntryChanges(beforeItems[name], entry)
        : fieldChanges(beforeItems[name], entry);
    if (changes.length) {
      entries.push({
        target: kind === "source_policy" ? "source" : "field",
        kind: "changed",
        name,
        table: entry?.table,
        changes,
      });
    }
  }
  return entries;
}

function fieldChanges(a, b) {
  const changes = [];
  for (const key of new Set([...Object.keys(a || {}), ...Object.keys(b || {})])) {
    const from = a?.[key];
    const to = b?.[key];
    if (!sameValue(from, to)) changes.push({ key, label: FIELD_KEY_LABELS[key] || key, from, to });
  }
  return changes;
}

// 一个来源的审批差异：licensing / technical_acceptance 两段内逐键 old → new。
function policyEntryChanges(a, b) {
  const changes = [];
  for (const section of ["licensing", "technical_acceptance"]) {
    const before = a?.[section] || {};
    const after = b?.[section] || {};
    for (const key of new Set([...Object.keys(before), ...Object.keys(after)])) {
      if (sameValue(before[key], after[key])) continue;
      changes.push({
        key: `${section}.${key}`,
        label: `${POLICY_SECTION_LABELS[section]} ${POLICY_ROW_KEY_LABELS[key] || key}`,
        from: before[key],
        to: after[key],
      });
    }
  }
  return changes;
}

export function diffEntryText(entry) {
  if (entry.target === "doc") {
    return `${entry.label}：${formatDocValue(entry.from)} → ${formatDocValue(entry.to)}`;
  }
  if (entry.target === "source") {
    if (entry.kind === "added") return `新增来源 ${entry.name}`;
    if (entry.kind === "removed") return `移除来源 ${entry.name}`;
    const changes = entry.changes.map(
      (change) => `${change.label} ${formatPolicyValue(change.key, change.from)} → ${formatPolicyValue(change.key, change.to)}`
    );
    return `${entry.name}：${changes.join("；")}`;
  }
  const where = entry.table ? `（${tableLabel(entry.table)}）` : "";
  if (entry.kind === "added") return `新增字段 ${entry.name}${where}：主来源 ${formatDocValue(entry.field?.primary)}`;
  if (entry.kind === "removed") return `移除字段 ${entry.name}${where}`;
  const changes = entry.changes.map(
    (change) => `${change.label} ${formatDocValue(change.from)} → ${formatDocValue(change.to)}`
  );
  return `${entry.name}${where}：${changes.join("；")}`;
}

// ---- 受限 YAML 子集（高级编辑，仅标签 A） --------------------------------------
// 只需覆盖本文档形状：嵌套映射、标量块列表、流式空列表、双引号 / 单引号 / 纯标量、
// |- 与 >- 块标量、整行与行尾注释。文档值来自 JSON（日期已是 ISO 字符串），不需要完整
// YAML 实现；保存仍有服务端“解析即校验”兜底（CD3），这里只保证自产文本可往返。

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
  if (text.includes("\n")) return null; // 由调用方落块标量
  return isPlainSafe(text) ? text : quoteScalar(text);
}

function emitMapping(map, indent, lines) {
  const pad = "  ".repeat(indent);
  for (const [key, value] of Object.entries(map)) {
    if (typeof key !== "string" || !SAFE_KEY.test(key)) throw new Error(`不支持序列化键名：${key}`);
    if (value !== null && typeof value === "object" && !Array.isArray(value)) {
      lines.push(`${pad}${key}:`);
      emitMapping(value, indent + 1, lines);
      continue;
    }
    if (Array.isArray(value)) {
      if (!value.length) {
        lines.push(`${pad}${key}: []`);
        continue;
      }
      lines.push(`${pad}${key}:`);
      for (const item of value) {
        if (item !== null && typeof item === "object") throw new Error(`${key}：列表项只能是标量`);
        lines.push(`${pad}  - ${scalarText(item)}`);
      }
      continue;
    }
    if (typeof value === "string" && value.includes("\n")) {
      lines.push(`${pad}${key}: |-`);
      for (const line of value.replace(/\n+$/, "").split("\n")) lines.push(line ? `${pad}  ${line}` : "");
      continue;
    }
    lines.push(`${pad}${key}: ${scalarText(value)}`);
  }
}

export function yamlText(document) {
  if (!document || typeof document !== "object" || Array.isArray(document)) {
    throw new Error("文档必须是映射（键值结构）");
  }
  const lines = [];
  emitMapping(document, 0, lines);
  return `${lines.join("\n")}\n`;
}

function tokenize(text) {
  const lines = [];
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.replace(/\t/g, "  ");
    if (!line.trim() || /^\s*#/.test(line)) continue;
    // text = 去缩进后的内容（解析用）；raw = 保留缩进的原文（块标量还原用）。
    lines.push({ indent: line.length - line.trimStart().length, text: line.trim(), raw: line.trimEnd() });
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

function parseScalar(raw) {
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
  return items.map((item) => parseScalar(item));
}

// 游标式解析：返回 {node, next}（next = 下一条未消费行的下标）。
function parseBlock(lines, start, indent) {
  if (start >= lines.length || lines[start].indent < indent) return { node: null, next: start };
  const isItem = (line) => line.text === "-" || line.text.startsWith("- ");
  if (isItem(lines[start])) {
    const items = [];
    let i = start;
    while (i < lines.length && lines[i].indent === indent && isItem(lines[i])) {
      const rest = lines[i].text === "-" ? "" : lines[i].text.slice(2).trim();
      if (rest === "") throw new Error(`第 ${i + 1} 行：列表项必须是标量`);
      items.push(parseScalar(rest));
      i += 1;
    }
    return { node: items, next: i };
  }
  const node = {};
  let i = start;
  while (i < lines.length && lines[i].indent === indent) {
    const match = /^([A-Za-z_][A-Za-z0-9_-]*)\s*:(.*)$/.exec(lines[i].text);
    if (!match) throw new Error(`第 ${i + 1} 行附近：无法解析（${lines[i].text.trim()}）`);
    const key = match[1];
    const rest = match[2].trim();
    if (Object.prototype.hasOwnProperty.call(node, key)) throw new Error(`第 ${i + 1} 行：键 ${key} 重复`);
    if (rest === "|" || rest === "|-" || rest === ">" || rest === ">-") {
      // 块标量：取更深缩进的连续行（行内的空行在分词时已折叠）。
      const block = [];
      let j = i + 1;
      let blockIndent = null;
      while (j < lines.length && lines[j].indent > indent) {
        if (blockIndent === null) blockIndent = lines[j].indent;
        block.push(lines[j].raw.slice(Math.min(blockIndent, lines[j].raw.length)));
        j += 1;
      }
      node[key] = block.length ? (rest.startsWith(">") ? block.join(" ") : block.join("\n")) : "";
      i = j;
      continue;
    }
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
    node[key] = parseScalar(rest);
    i += 1;
  }
  return { node, next: i };
}

export function parseYamlDocument(text) {
  try {
    const lines = tokenize(String(text ?? ""));
    if (!lines.length) return { error: "文档为空" };
    if (lines[0].indent !== 0) return { error: "首行必须顶格" };
    const { node, next } = parseBlock(lines, 0, 0);
    if (next < lines.length) return { error: `第 ${next + 1} 行附近：缩进不一致（${lines[next].text.trim()}）` };
    if (!node || typeof node !== "object" || Array.isArray(node)) return { error: "文档必须是映射（键值结构）" };
    return { value: node };
  } catch (error) {
    return { error: error instanceof Error ? error.message : String(error) };
  }
}

// ---- 共享控制器 ----------------------------------------------------------------
// 一个 config 文档标签的全部行为：加载 / 表单渲染钩子 / 全文档保存（必填备注 +
// expected_revision 乐观锁，422 逐条 / 409 冲突）/ 脏标记 / 漂移横幅 / 基线差异与
// 导入 / 变更历史。标签间只差 kind 钩子（表单、头部元信息、行校验、行脏判定）。

let activeTab = null;
// 当前激活标签的控制器（共享抽屉的操作对象）。模块级而非闭包：抽屉是页面级单例。

function redirectOn401(error) {
  if (error instanceof ApiError && error.status === 401) {
    globalThis.location.href = "/app/login";
    return true;
  }
  return false;
}

function configDocTab(config) {
  // config: {kind, endpoint, historyEndpoint, baselineImportEndpoint, elements,
  //          emptyDoc, metaParts(ctl), renderForm(ctl, region),
  //          rowIssues(ctl, name), rowDirty(ctl, name), savedMessage, advanced}
  const elements = config.elements;
  const node = (name) => document.getElementById(elements[name]);

  const state = {
    loaded: null, // GET 响应（value / baseline_value / revision / 元数据 / drifted）
    doc: null, // 工作文档（表单与高级编辑共同编辑的唯一对象）
    original: null, // 保存基准（canonical 对比）
    rows: new Map(), // 行标识 -> { root, issues, setDirty }（标签 A 字段行 / 标签 B 来源卡）
    fieldIssues: new Map(), // 行标识 -> [信息]（行内预检 / 服务端 422）
  };

  function isDirty() {
    return Boolean(state.doc && state.original && !sameDocument(state.doc, state.original));
  }

  function renderForm() {
    const region = node("form");
    if (!region || !state.doc) return;
    config.renderForm(ctl, region);
    updateSavebar();
  }

  function afterEdit(name) {
    const entry = state.rows.get(name);
    const messages = config.rowIssues ? config.rowIssues(ctl, name) : null;
    if (messages && messages.length) state.fieldIssues.set(name, messages);
    else state.fieldIssues.delete(name);
    if (entry) {
      clear(entry.issues);
      for (const message of messages || []) entry.issues.appendChild(el("li", {}, message));
      entry.setDirty(config.rowDirty(ctl, name));
    }
    updateSavebar();
  }

  // ---- 头部 / 漂移横幅 ----------------------------------------------------------

  function renderHeader() {
    const meta = node("meta");
    if (!meta || !state.loaded) return;
    meta.textContent = config.metaParts(ctl).join(" · ");
  }

  function renderDrift() {
    const banner = node("drift");
    if (!banner || !state.loaded) return;
    clear(banner);
    if (!state.loaded.drifted) {
      banner.hidden = true;
      return;
    }
    banner.hidden = false;
    banner.appendChild(
      document.createTextNode("当前文档与仓库基线文件存在漂移（基线只作首次导入，运行时以这里为准）。")
    );
    const show = el("button", { type: "button", class: "btn btn-sm ds-drift-link" }, "查看差异");
    show.addEventListener("click", openBaselineDrawer);
    banner.appendChild(show);
  }

  // ---- 保存 / 放弃 --------------------------------------------------------------

  function setSaveResult(lines, kind) {
    const box = node("saveResult");
    if (!box) return;
    clear(box);
    if (!lines.length) {
      updateSavebar();
      return;
    }
    const notice = el("div", { class: `notice${kind ? ` notice-${kind}` : ""}` });
    if (lines.length === 1) notice.textContent = lines[0];
    else {
      const list = el("ul", { class: "issue-list" });
      for (const line of lines) list.appendChild(el("li", {}, line));
      notice.appendChild(list);
    }
    box.appendChild(notice);
    updateSavebar();
  }

  function updateSavebar() {
    const counter = node("dirtyCount");
    const save = node("save");
    const discard = node("discard");
    const bar = node("savebar");
    const result = node("saveResult");
    const dirty = isDirty();
    if (counter) {
      counter.textContent = dirty ? "有未保存的修改" : "没有未保存的修改";
      if (dirty) counter.classList.add("has-dirty");
      else counter.classList.remove("has-dirty");
    }
    if (save) save.disabled = !dirty;
    if (discard) discard.disabled = !dirty;
    if (bar) bar.hidden = !dirty && !(result && String(result.textContent || "").trim());
    for (const [name, entry] of state.rows) entry.setDirty(config.rowDirty(ctl, name));
  }

  function advancedOpen() {
    const details = config.advanced ? node("advanced") : null;
    return Boolean(details && details.open);
  }

  function showYamlIssues(messages) {
    const box = node("yamlIssues");
    if (!box) return;
    clear(box);
    for (const message of messages || []) box.appendChild(el("li", {}, message));
  }

  async function save(button) {
    if (!isDirty()) return;
    const noteInput = node("note");
    const note = noteInput && noteInput.value.trim() ? noteInput.value.trim() : "";
    if (!note) {
      setSaveResult(["变更说明必填：写明这次调整的来源与原因（会进入变更历史）。"], "warn");
      noteInput?.focus();
      return;
    }
    for (const [name] of state.rows) {
      const messages = config.rowIssues ? config.rowIssues(ctl, name) : null;
      if (messages) {
        setSaveResult([`${name}：${messages.join("；")}`], "warn");
        return;
      }
    }
    if (advancedOpen()) {
      const editor = node("yaml");
      const parsed = parseYamlDocument(editor ? editor.value : "");
      if (parsed.error) {
        showYamlIssues([parsed.error]);
        setSaveResult(["高级编辑里的 YAML 无法解析，未保存。"], "warn");
        return;
      }
      state.doc = parsed.value;
      if (!isDirty()) {
        setSaveResult(["内容与已保存版本一致，无需保存。"], "");
        return;
      }
    }
    const promise = patch(config.endpoint, { value: state.doc, note, expected_revision: state.loaded.revision })
      .then((data) => {
        adopt(data);
        if (noteInput) noteInput.value = "";
        setSaveResult([config.savedMessage], "");
      })
      .catch((error) => {
        if (redirectOn401(error)) return;
        if (error instanceof ApiError && error.status === 422) {
          const lines = ["服务端校验未通过（解析即校验），未保存任何修改："];
          const issues = error.detail && typeof error.detail === "object" ? error.detail.issues : null;
          if (Array.isArray(issues)) {
            for (const issue of issues) {
              const messages = Array.isArray(issue.messages) ? issue.messages.map(String) : [JSON.stringify(issue)];
              lines.push(...messages);
            }
          }
          if (advancedOpen()) showYamlIssues(lines.slice(1));
          setSaveResult(lines, "warn");
          return;
        }
        if (error instanceof ApiError && error.status === 409) {
          setSaveResult(
            [`保存失败：${error.message}`, "文档已在别处被修改（修订号不一致）。可拉取最新版本后重新应用改动。"],
            "error"
          );
          const box = node("saveResult");
          const refresh = el("button", { type: "button", class: "btn btn-sm" }, "拉取最新版本（放弃本地修改）");
          refresh.addEventListener("click", () => {
            setSaveResult([], "");
            load();
          });
          box?.appendChild(refresh);
          return;
        }
        setSaveResult([`保存失败：${error.message}`], "error");
      });
    lockWhileBusy(button, promise);
    await promise;
    updateSavebar();
  }

  function discard() {
    if (!state.original) return;
    state.doc = structuredClone(state.original);
    state.fieldIssues = new Map();
    setSaveResult([], "");
    showYamlIssues([]);
    renderForm();
    renderHeader();
    if (advancedOpen()) {
      const editor = node("yaml");
      if (editor) editor.value = yamlText(state.doc);
    }
  }

  // ---- 高级编辑（表单 ↔ YAML 的一致性，仅标签 A） --------------------------------
  // 确定性规则：表单的每次改动即时写回工作文档；打开高级编辑时总是用工作文档重建编辑器
  // 文本（手写的注释 / 字段顺序不跨开关保留）；编辑过程中每次输入都尝试解析，解析成功即
  // 成为工作文档（未保存标记随之准确），失败只提示不写回；关闭时若仍解析失败则阻止关闭。
  // 两个编辑面永远只有一份真相（state.doc）。

  function wireAdvanced() {
    if (!config.advanced) return;
    const details = node("advanced");
    if (!details) return;
    const editor = node("yaml");
    details.addEventListener("toggle", () => {
      const form = node("form");
      if (details.open) {
        if (editor) editor.value = yamlText(state.doc || {});
        showYamlIssues([]);
        if (form) form.hidden = true;
        updateSavebar();
        return;
      }
      const parsed = parseYamlDocument(editor ? editor.value : "");
      if (parsed.error) {
        showYamlIssues([parsed.error]);
        details.open = true; // 解析失败：重新展开，留在高级模式。
        return;
      }
      state.doc = parsed.value;
      showYamlIssues([]);
      if (form) form.hidden = false;
      renderForm();
      renderHeader();
    });
    if (editor) {
      editor.addEventListener("input", () => {
        const parsed = parseYamlDocument(editor.value);
        if (parsed.error) {
          showYamlIssues([parsed.error]);
          return;
        }
        showYamlIssues([]);
        state.doc = parsed.value;
        renderHeader();
        updateSavebar();
      });
    }
  }

  // ---- 基线漂移 / 导入 ------------------------------------------------------------

  function renderDiffRegion(region, entries, emptyText) {
    if (!entries.length) {
      renderRegion(region, "empty", emptyText);
      return;
    }
    const list = el("ul", { class: "ds-diff-list" });
    for (const entry of entries) {
      list.appendChild(el("li", { class: `ds-diff-item ds-diff-${entry.kind}` }, diffEntryText(entry)));
    }
    const wrap = el("div", { class: "ds-diff" });
    wrap.appendChild(el("p", { class: "faint" }, `共 ${entries.length} 处差异。`));
    wrap.appendChild(list);
    renderRegion(region, "ready", wrap);
  }

  function openBaselineDrawer() {
    const drawer = document.getElementById("ds-baseline-drawer");
    if (!drawer || !state.loaded) return;
    const region = document.getElementById("ds-baseline-diff");
    const baseline = state.loaded.baseline_value;
    if (!baseline) {
      renderRegion(region, "error", "基线文件不可读或不是映射，无法对比。");
    } else {
      // 方向：基线 → 当前文档（与历史行的 old → new 一致）。
      renderDiffRegion(region, diffDocuments(baseline, state.doc, config.kind), "当前文档与基线一致。");
    }
    if (globalThis.window?.zcOverlay) globalThis.window.zcOverlay.open(drawer);
  }

  async function importBaseline(button) {
    const noteInput = document.getElementById("ds-baseline-note");
    const note = noteInput && noteInput.value.trim() ? noteInput.value.trim() : "";
    if (!note) {
      noteInput?.focus();
      setSaveResult(["从基线导入需要填写导入说明。"], "warn");
      return;
    }
    const promise = post(config.baselineImportEndpoint, { note })
      .then((data) => {
        adopt(data);
        if (noteInput) noteInput.value = "";
        setSaveResult(["已用基线文件覆盖当前文档（已记录一条变更历史）。"], "");
        const region = document.getElementById("ds-baseline-diff");
        if (region) renderDiffRegion(region, [], "导入完成：当前文档即基线。");
        if (globalThis.window?.zcOverlay) globalThis.window.zcOverlay.close();
      })
      .catch((error) => {
        if (redirectOn401(error)) return;
        setSaveResult([`从基线导入失败：${error.message}`], "error");
      });
    lockWhileBusy(button, promise);
    await promise;
  }

  // ---- 变更历史 ----------------------------------------------------------------

  function renderHistory(region, payload) {
    const items = (payload && payload.items) || [];
    if (!items.length) {
      renderRegion(region, "empty");
      return;
    }
    const list = el("ul", { class: "ds-history-list" });
    for (const change of items) {
      const item = el("li", { class: "ds-history-item" });
      const head = el("div", { class: "ds-history-head" });
      head.appendChild(el("span", { class: "num" }, formatDateTime(change.changed_at)));
      head.appendChild(el("span", { class: "badge" }, label(ACTOR_LABELS, change.actor)));
      head.appendChild(el("span", { class: "ds-history-note" }, change.note || "—"));
      item.appendChild(head);
      const body = el("div", { class: "ds-history-body" });
      if (change.old_value === null || change.old_value === undefined) {
        body.appendChild(el("p", { class: "faint" }, "首次导入 / 创建文档。"));
      } else {
        const entries = diffDocuments(change.old_value, change.new_value, config.kind);
        if (!entries.length) {
          body.appendChild(el("p", { class: "faint" }, "内容无差异（仅元数据变更）。"));
        } else {
          const details = el("details", { class: "ds-history-details" });
          details.appendChild(el("summary", {}, `${entries.length} 处变更`));
          const changes = el("ul", { class: "ds-diff-list" });
          for (const entry of entries.slice(0, 30)) {
            changes.appendChild(el("li", { class: `ds-diff-item ds-diff-${entry.kind}` }, diffEntryText(entry)));
          }
          if (entries.length > 30) changes.appendChild(el("li", { class: "faint" }, `…其余 ${entries.length - 30} 处`));
          details.appendChild(changes);
          body.appendChild(details);
        }
      }
      item.appendChild(body);
      list.appendChild(item);
    }
    renderRegion(region, "ready", list);
  }

  async function loadHistory() {
    const region = document.getElementById("ds-history");
    if (!region) return;
    renderRegion(region, "loading");
    try {
      renderHistory(region, await get(config.historyEndpoint));
    } catch (error) {
      if (redirectOn401(error)) return;
      renderRegion(region, "error", error.message);
    }
  }

  // ---- 装配 --------------------------------------------------------------------

  function adopt(data) {
    state.loaded = data;
    state.doc = data.value && typeof data.value === "object" ? structuredClone(data.value) : config.emptyDoc();
    state.original = structuredClone(state.doc);
    state.fieldIssues = new Map();
    renderHeader();
    renderDrift();
    renderForm();
    const advanced = config.advanced ? node("advanced") : null;
    if (advanced) {
      if (advanced.open) {
        const editor = node("yaml");
        if (editor) editor.value = yamlText(state.doc);
      } else {
        const form = node("form");
        if (form) form.hidden = false;
      }
      advanced.hidden = false;
    }
    updateSavebar();
  }

  async function load() {
    const region = node("form");
    if (!region) return;
    renderRegion(region, "loading");
    try {
      adopt(await get(config.endpoint));
    } catch (error) {
      if (redirectOn401(error)) return;
      renderRegion(region, "error", error.message);
    }
  }

  function wire() {
    const saveButton = node("save");
    if (saveButton) saveButton.addEventListener("click", () => save(saveButton));
    const discardButton = node("discard");
    if (discardButton) discardButton.addEventListener("click", discard);
    // 历史与基线按钮开的是共享抽屉，内容按当前激活标签渲染。
    const historyButton = node("historyOpen");
    if (historyButton) {
      historyButton.addEventListener("click", () => {
        const drawer = document.getElementById("ds-history-drawer");
        if (drawer && globalThis.window?.zcOverlay) globalThis.window.zcOverlay.open(drawer);
        activeTab?.loadHistory();
      });
    }
    const baselineButton = node("baselineOpen");
    if (baselineButton) baselineButton.addEventListener("click", openBaselineDrawer);
    wireAdvanced();
  }

  const ctl = {
    state,
    isDirty,
    load,
    save,
    discard,
    wire,
    afterEdit,
    openBaselineDrawer,
    loadHistory,
    importBaseline,
  };
  return ctl;
}

// ---- 标签 A：字段来源优先级 -------------------------------------------------------

function createFieldSourcesTab() {
  const ctl = configDocTab({
    kind: "field_sources",
    endpoint: ENDPOINT,
    historyEndpoint: HISTORY_ENDPOINT,
    baselineImportEndpoint: BASELINE_IMPORT_ENDPOINT,
    advanced: true,
    elements: {
      form: "ds-form",
      drift: "ds-drift",
      meta: "ds-meta",
      historyOpen: "ds-history-open",
      baselineOpen: "ds-baseline-open",
      savebar: "ds-savebar",
      dirtyCount: "ds-dirty-count",
      note: "ds-note",
      discard: "ds-discard",
      save: "ds-save",
      saveResult: "ds-save-result",
      advanced: "ds-advanced",
      yaml: "ds-yaml",
      yamlIssues: "ds-yaml-issues",
    },
    emptyDoc: () => ({ version: "", fields: {} }),
    metaParts: (self) => {
      const value = self.state.doc || {};
      const parts = [`版本 ${formatDocValue(value.version)}`, `修订 ${self.state.loaded.revision}`];
      if (self.state.loaded.updated_at) {
        parts.push(formatDateTime(self.state.loaded.updated_at));
        parts.push(label(ACTOR_LABELS, self.state.loaded.updated_by));
      } else {
        parts.push("未导入（按基线文件显示）");
      }
      return parts;
    },
    rowIssues: (self, name) => {
      const messages = self.state.fieldIssues.get(name);
      return messages && messages.length ? messages : null;
    },
    rowDirty: (self, name) => {
      const current = self.state.doc.fields?.[name];
      const original = self.state.original.fields?.[name];
      if (!current || !original) return true;
      for (const key of ["primary", "fallback", "tolerance", "key"]) {
        const has = Object.prototype.hasOwnProperty.call(current, key);
        const had = Object.prototype.hasOwnProperty.call(original, key);
        if (has !== had || !sameValue(current[key], original[key])) return true;
      }
      return false;
    },
    savedMessage: "已保存。API 下次读取即用新顺序，worker 在下一个任务开始前加载新值。",
    renderForm: renderFieldsForm,
  });
  return ctl;
}

// tolerance 输入 → 写回工作文档；空 = 继承默认容差（删除该键）。
function applyTolerance(ctl, name, raw) {
  const field = ctl.state.doc.fields[name];
  const text = String(raw ?? "").trim();
  if (text === "") {
    delete field.tolerance;
    ctl.state.fieldIssues.delete(name);
    return;
  }
  const value = Number(text);
  if (!Number.isFinite(value) || value <= 0 || value > 1) {
    ctl.state.fieldIssues.set(name, ["容差须在 (0, 1] 内（相对值）"]);
    return;
  }
  field.tolerance = value;
  ctl.state.fieldIssues.delete(name);
}

// key 勾选写回规则（保持文档形状稳定，避免假变更）：勾选 → key: true；取消勾选 →
// 删除该键（缺省即非关键）；仅当原文档显式写了 key: false 时保留 false。
function applyKey(ctl, name, checked) {
  const field = ctl.state.doc.fields[name];
  const original = ctl.state.original.fields?.[name];
  const explicitFalse =
    original && Object.prototype.hasOwnProperty.call(original, "key") && original.key === false;
  if (checked) field.key = true;
  else if (explicitFalse) field.key = false;
  else delete field.key;
}

// 只读证据面（CD1）：口径文本、校准徽章（展开为方法记录）、合理性 / 近似摘要。
function chipsCell(entry) {
  const box = el("div", { class: "cluster ds-chips" });
  const calibration = entry.calibration && typeof entry.calibration === "object" ? entry.calibration : null;
  if (calibration && Object.keys(calibration).length) {
    const details = el("details", { class: "ds-chip-details" });
    details.appendChild(
      el("summary", { class: "badge accent ds-chip-summary" }, `校准 ${Object.keys(calibration).length}`)
    );
    const list = el("ul", { class: "ds-calibration-list" });
    for (const [provider, record] of Object.entries(calibration)) {
      const item = el("li");
      item.appendChild(el("b", { class: "mono" }, provider));
      const meta = [
        record?.checked_on ? `核对于 ${record.checked_on}` : null,
        record?.method || null,
        record?.note || null,
      ].filter(Boolean);
      item.appendChild(document.createTextNode(meta.length ? `：${meta.join("；")}` : "：（无记录）"));
      list.appendChild(item);
    }
    details.appendChild(list);
    box.appendChild(details);
  }
  const sanity = entry.sanity && typeof entry.sanity === "object" ? entry.sanity : null;
  if (sanity && Object.keys(sanity).length) {
    const parts = Object.entries(sanity).map(
      ([provider, rule]) => `${provider} ${rule?.min ?? "−∞"} ~ ${rule?.max ?? "+∞"}`
    );
    box.appendChild(
      el("span", { class: "badge outline ds-sanity", title: parts.join("\n") }, `合理性 ${Object.keys(sanity).length}`)
    );
  }
  const approx = entry.approx && typeof entry.approx === "object" ? entry.approx : null;
  const approxProviders = approx
    ? Object.entries(approx)
        .filter(([, flag]) => flag)
        .map(([provider]) => provider)
    : [];
  if (approxProviders.length) {
    box.appendChild(
      el("span", { class: "badge outline warn", title: "该来源只近似该字段（口径有偏差）" }, `近似 ${approxProviders.join("、")}`)
    );
  }
  if (typeof entry.formula === "string" && entry.formula) {
    box.appendChild(el("span", { class: "badge outline mono", title: "派生公式" }, entry.formula));
  }
  return box;
}

// fallback 有序列表：上移 / 下移 / 移除 / 添加（拖拽暂不做，按钮语义等价且可测）。
function fallbackCell(ctl, name, options) {
  const field = ctl.state.doc.fields[name];
  const wrap = el("div", { class: "ds-fallback" });
  const list = Array.isArray(field.fallback) ? [...field.fallback] : [];
  const commit = () => {
    if (list.length) field.fallback = [...list];
    else delete field.fallback;
    renderFallback();
    afterFieldEdit(ctl, name);
  };
  const renderFallback = () => {
    clear(wrap);
    const box = el("ol", { class: "ds-fallback-list" });
    list.forEach((provider, index) => {
      const item = el("li");
      item.appendChild(el("span", { class: "mono ds-fallback-item" }, provider));
      const up = el("button", {
        type: "button",
        class: "btn btn-sm",
        "aria-label": `上移 ${provider}`,
        disabled: index === 0 ? "disabled" : undefined,
      }, "上移");
      up.addEventListener("click", () => {
        [list[index - 1], list[index]] = [list[index], list[index - 1]];
        commit();
      });
      const down = el("button", {
        type: "button",
        class: "btn btn-sm",
        "aria-label": `下移 ${provider}`,
        disabled: index === list.length - 1 ? "disabled" : undefined,
      }, "下移");
      down.addEventListener("click", () => {
        [list[index + 1], list[index]] = [list[index], list[index + 1]];
        commit();
      });
      const remove = el("button", { type: "button", class: "btn btn-sm btn-danger-ghost", "aria-label": `移除 ${provider}` }, "移除");
      remove.addEventListener("click", () => {
        list.splice(index, 1);
        commit();
      });
      item.appendChild(up);
      item.appendChild(down);
      item.appendChild(remove);
      box.appendChild(item);
    });
    wrap.appendChild(box);
    // 添加候选 = 该表现有来源 - 已用（primary 与 fallback 均不可重复，服务端同规则）。
    const taken = new Set([field.primary, ...list]);
    const addable = options.filter((provider) => !taken.has(provider));
    if (addable.length) {
      const select = el("select", { class: "ds-fallback-add", "aria-label": `为 ${name} 添加备用来源` });
      select.appendChild(el("option", { value: "" }, "添加备用来源…"));
      for (const provider of addable) select.appendChild(el("option", { value: provider }, provider));
      select.addEventListener("change", () => {
        if (!select.value) return;
        list.push(select.value);
        commit();
      });
      wrap.appendChild(select);
    }
  };
  renderFallback();
  return { root: wrap, refresh: renderFallback };
}

function fieldRow(ctl, name) {
  const field = ctl.state.doc.fields[name];
  const options = [...providerOptions(ctl.state.doc, field.table)];
  if (field.primary !== undefined && !options.includes(field.primary)) options.unshift(field.primary);

  const tr = el("tr", { "data-field": name });
  const main = el("td", { class: "ds-field-cell" });
  const title = el("div", { class: "ds-field-name" });
  title.appendChild(el("b", { class: "mono" }, name));
  if (field.key === true) {
    title.appendChild(el("span", { class: "badge warn", title: "冲突或缺值会触发 iFinD 核对" }, "关键"));
  }
  main.appendChild(title);
  main.appendChild(el("p", { class: "faint ds-definition" }, field.definition || "（无口径记录）"));
  main.appendChild(chipsCell(field));
  const issues = el("ul", { class: "issue-list setting-issues issues" });
  main.appendChild(issues);
  tr.appendChild(main);

  const primaryCell = el("td", { class: "ds-edit-cell" });
  const primary = el("select", { "aria-label": `${name} 主来源` });
  for (const provider of options) primary.appendChild(el("option", { value: provider }, provider));
  primary.value = String(field.primary ?? "");
  primary.addEventListener("change", () => {
    field.primary = primary.value;
    // 主来源变化后，备用列表的“添加候选”要排除新主来源（与移除按钮的重算一致）。
    fallback.refresh();
    afterFieldEdit(ctl, name);
  });
  primaryCell.appendChild(primary);
  tr.appendChild(primaryCell);

  const fallbackTableCell = el("td", { class: "ds-edit-cell ds-fallback-cell" });
  const fallback = fallbackCell(ctl, name, options);
  fallbackTableCell.appendChild(fallback.root);
  tr.appendChild(fallbackTableCell);

  const toleranceCell = el("td", { class: "ds-edit-cell ds-tolerance-cell" });
  const tolerance = el("input", {
    type: "number",
    step: "any",
    min: "0",
    max: "1",
    inputmode: "decimal",
    class: "ds-tolerance",
    "aria-label": `${name} 容差`,
    placeholder: String(ctl.state.doc.default_tolerance ?? ""),
    title: "留空 = 继承默认容差",
  });
  tolerance.value = field.tolerance === undefined || field.tolerance === null ? "" : String(field.tolerance);
  tolerance.addEventListener("input", () => {
    applyTolerance(ctl, name, tolerance.value);
    afterFieldEdit(ctl, name);
  });
  toleranceCell.appendChild(tolerance);
  tr.appendChild(toleranceCell);

  const keyCell = el("td", { class: "ds-edit-cell ds-key-cell" });
  const key = el("input", { type: "checkbox", class: "ds-key", "aria-label": `${name} 关键字段` });
  key.checked = field.key === true;
  key.addEventListener("change", () => {
    applyKey(ctl, name, key.checked);
    afterFieldEdit(ctl, name);
  });
  keyCell.appendChild(key);
  tr.appendChild(keyCell);

  ctl.state.rows.set(name, {
    tr,
    issues,
    setDirty(dirty) {
      if (dirty) tr.classList.add("dirty");
      else tr.classList.remove("dirty");
    },
  });
  return tr;
}

function renderFieldsForm(ctl, region) {
  ctl.state.rows = new Map();
  const groups = tableGroups(ctl.state.doc);
  if (!groups.length) {
    renderRegion(region, "empty", "文档里还没有字段。可用高级编辑粘贴一份完整文档。");
    return;
  }
  const container = el("div", { class: "ds-groups" });
  for (const group of groups) {
    container.appendChild(
      el("h3", { class: "settings-kicker ds-group-title" }, `${group.label} · ${group.names.length} 个字段`)
    );
    const rows = group.names
      .filter((name) => ctl.state.doc.fields[name] && typeof ctl.state.doc.fields[name] === "object")
      .map((name) => fieldRow(ctl, name));
    const wrap = el("div", { class: "table-wrap" });
    wrap.appendChild(
      table([{ label: "字段" }, { label: "主来源" }, { label: "备用顺序" }, { label: "容差" }, { label: "关键" }], rows)
    );
    container.appendChild(wrap);
  }
  renderRegion(region, "ready", container);
}

// ---- 标签 B：来源信任策略 -------------------------------------------------------

// 状态选择：受控取值 + 文档当前值（历史自定义取值不丢失、不误改）。
function statusSelect(current, choices, ariaLabel) {
  const select = el("select", { class: "dsp-status", "aria-label": ariaLabel });
  const options = [...choices];
  const value = typeof current === "string" && current ? current : choices[0];
  if (!options.includes(value)) options.push(value);
  for (const option of options) select.appendChild(el("option", { value: option }, option));
  select.value = value;
  return select;
}

// company_keys chip 编辑器：空列表 = 未授权任何公司（fail-closed 语义）。
function keysCell(list, ariaPrefix, onChange) {
  const wrap = el("div", { class: "dsp-keys" });
  const render = () => {
    clear(wrap);
    const box = el("ul", { class: "dsp-key-list" });
    for (const key of list) {
      const chip = el("li", { class: "dsp-key-chip" });
      chip.appendChild(el("span", { class: "mono" }, key));
      const remove = el("button", {
        type: "button",
        class: "btn btn-sm",
        "aria-label": `移除公司 ${key}`,
        title: `移除公司 ${key}`,
      }, "×");
      remove.addEventListener("click", () => {
        const index = list.indexOf(key);
        if (index >= 0) list.splice(index, 1);
        render();
        onChange();
      });
      chip.appendChild(remove);
      box.appendChild(chip);
    }
    wrap.appendChild(box);
    const add = el("div", { class: "dsp-key-add" });
    const input = el("input", {
      type: "text",
      class: "mono",
      "aria-label": `${ariaPrefix}公司`,
      placeholder: "公司标识（company_key）",
    });
    const button = el("button", { type: "button", class: "btn btn-sm", "aria-label": `添加${ariaPrefix}公司` }, "添加");
    const commit = () => {
      const value = input.value.trim();
      if (!value || list.includes(value)) {
        input.value = "";
        return;
      }
      list.push(value);
      input.value = "";
      render();
      onChange();
    };
    button.addEventListener("click", commit);
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        commit();
      }
    });
    add.appendChild(input);
    add.appendChild(button);
    wrap.appendChild(add);
  };
  render();
  return wrap;
}

// 验收报告行编辑器：observed_on（ISO 日期）+ citation；空行会被行内预检拦下。
function reportsCell(reports, sourceId, onChange) {
  const wrap = el("div", { class: "dsp-reports-wrap dsp-wide" });
  const render = () => {
    clear(wrap);
    const list = el("ul", { class: "dsp-reports" });
    if (!reports.length) {
      list.appendChild(el("li", { class: "dsp-report-empty" }, "还没有验收报告（技术验收要求至少三条带引用的报告）。"));
    }
    reports.forEach((report, index) => {
      const item = el("li", { class: "dsp-report" });
      const day = el("input", {
        type: "date",
        "aria-label": `${sourceId} 报告 ${index + 1} 观测日期`,
      });
      day.value = typeof report.observed_on === "string" ? report.observed_on : "";
      day.addEventListener("change", () => {
        report.observed_on = day.value;
        onChange();
      });
      const citation = el("input", {
        type: "text",
        class: "dsp-report-citation",
        "aria-label": `${sourceId} 报告 ${index + 1} 引用`,
        placeholder: "网页链接或 scheme:path 引用",
      });
      citation.value = typeof report.citation === "string" ? report.citation : "";
      citation.addEventListener("input", () => {
        report.citation = citation.value;
        onChange();
      });
      const remove = el("button", {
        type: "button",
        class: "btn btn-sm btn-danger-ghost",
        "aria-label": `移除报告 ${index + 1}`,
      }, "移除");
      remove.addEventListener("click", () => {
        reports.splice(index, 1);
        render();
        onChange();
      });
      item.appendChild(day);
      item.appendChild(citation);
      item.appendChild(remove);
      list.appendChild(item);
    });
    wrap.appendChild(list);
    const add = el("button", { type: "button", class: "btn btn-sm", "aria-label": `为 ${sourceId} 添加验收报告` }, "添加报告");
    add.addEventListener("click", () => {
      reports.push({ observed_on: "", citation: "" });
      render();
      onChange();
    });
    wrap.appendChild(add);
  };
  render();
  return wrap;
}

function policyRowIssues(ctl, sourceId) {
  const entry = ctl.state.doc.sources[sourceId];
  if (!entry) return null;
  const messages = [];
  const reports = entry.technical_acceptance?.reports;
  (Array.isArray(reports) ? reports : []).forEach((report, index) => {
    if (!report || typeof report !== "object") {
      messages.push(`验收报告第 ${index + 1} 条格式不正确`);
      return;
    }
    if (!String(report.observed_on ?? "").trim()) messages.push(`验收报告第 ${index + 1} 条缺少观测日期`);
    if (!String(report.citation ?? "").trim()) messages.push(`验收报告第 ${index + 1} 条缺少引用`);
  });
  return messages.length ? messages : null;
}

function policyRowDirty(ctl, sourceId) {
  const current = ctl.state.doc.sources?.[sourceId];
  const original = ctl.state.original.sources?.[sourceId];
  if (!current || !original) return true;
  return !sameValue(current, original);
}

// 一个来源一卡：许可（状态 / 公司范围 / 引用）+ 技术验收（状态 / 公司范围 /
// 适配器版本 / 验收报告）。所有控件即时写回工作文档。
function policyCard(ctl, sourceId) {
  const entry = ctl.state.doc.sources[sourceId];
  // GET 的值都经 parse_source_policy 校验；这里只兜住手工构造的畸形文档。
  if (!entry.licensing || typeof entry.licensing !== "object") {
    entry.licensing = { status: "unknown", company_keys: [], citation: "" };
  }
  if (!entry.technical_acceptance || typeof entry.technical_acceptance !== "object") {
    entry.technical_acceptance = { status: "not_accepted", company_keys: [], adapter_version: "", reports: [] };
  }
  if (!Array.isArray(entry.technical_acceptance.reports)) entry.technical_acceptance.reports = [];
  const edit = () => afterPolicyEdit(ctl, sourceId);

  const card = el("section", { class: "dsp-source-card", "data-source": sourceId });
  const head = el("div", { class: "dsp-source-head" });
  head.appendChild(el("h3", { class: "mono dsp-source-title" }, sourceId));
  card.appendChild(head);

  const body = el("div", { class: "dsp-source-body" });

  const licensing = el("div", { class: "dsp-grid" });
  licensing.appendChild(el("h4", { class: "settings-kicker dsp-section-title dsp-wide" }, "许可（licensing）"));
  licensing.appendChild(el("label", {}, "状态"));
  const licensingStatus = statusSelect(entry.licensing?.status, LICENSING_STATUSES, `${sourceId} 许可状态`);
  licensingStatus.addEventListener("change", () => {
    entry.licensing.status = licensingStatus.value;
    edit();
  });
  licensing.appendChild(licensingStatus);
  licensing.appendChild(el("label", {}, "公司范围"));
  const licensingKeys = Array.isArray(entry.licensing?.company_keys) ? [...entry.licensing.company_keys] : [];
  licensing.appendChild(
    keysCell(licensingKeys, `${sourceId} 许可`, () => {
      entry.licensing.company_keys = [...licensingKeys];
      edit();
    })
  );
  licensing.appendChild(el("label", {}, "引用"));
  const licensingCitation = el("input", {
    type: "text",
    class: "dsp-citation",
    "aria-label": `${sourceId} 许可引用`,
    placeholder: "许可证据引用（网页链接或 scheme:path）",
  });
  licensingCitation.value = typeof entry.licensing?.citation === "string" ? entry.licensing.citation : "";
  licensingCitation.addEventListener("input", () => {
    entry.licensing.citation = licensingCitation.value;
    edit();
  });
  licensing.appendChild(licensingCitation);
  body.appendChild(licensing);

  const technical = el("div", { class: "dsp-grid" });
  technical.appendChild(el("h4", { class: "settings-kicker dsp-section-title dsp-wide" }, "技术验收（technical_acceptance）"));
  technical.appendChild(el("label", {}, "状态"));
  const technicalStatus = statusSelect(
    entry.technical_acceptance?.status, TECHNICAL_STATUSES, `${sourceId} 技术验收状态`
  );
  technicalStatus.addEventListener("change", () => {
    entry.technical_acceptance.status = technicalStatus.value;
    edit();
  });
  technical.appendChild(technicalStatus);
  technical.appendChild(el("label", {}, "公司范围"));
  const technicalKeys = Array.isArray(entry.technical_acceptance?.company_keys)
    ? [...entry.technical_acceptance.company_keys]
    : [];
  technical.appendChild(
    keysCell(technicalKeys, `${sourceId} 技术验收`, () => {
      entry.technical_acceptance.company_keys = [...technicalKeys];
      edit();
    })
  );
  technical.appendChild(el("label", {}, "适配器版本"));
  const adapter = el("input", {
    type: "text",
    class: "dsp-adapter",
    "aria-label": `${sourceId} 适配器版本`,
    placeholder: "与采集适配器版本一致才通过验收",
  });
  adapter.value = typeof entry.technical_acceptance?.adapter_version === "string"
    ? entry.technical_acceptance.adapter_version
    : "";
  adapter.addEventListener("input", () => {
    entry.technical_acceptance.adapter_version = adapter.value;
    edit();
  });
  technical.appendChild(adapter);
  technical.appendChild(el("label", {}, "验收报告"));
  const reports = Array.isArray(entry.technical_acceptance?.reports) ? [...entry.technical_acceptance.reports] : [];
  const reportsNode = reportsCell(reports, sourceId, () => {
    entry.technical_acceptance.reports = reports.map((report) => ({ ...report }));
    edit();
  });
  technical.appendChild(reportsNode);
  body.appendChild(technical);

  const issues = el("ul", { class: "issue-list setting-issues issues" });
  body.appendChild(issues);
  card.appendChild(body);

  ctl.state.rows.set(sourceId, {
    root: card,
    issues,
    setDirty(dirty) {
      if (dirty) card.classList.add("dirty");
      else card.classList.remove("dirty");
    },
  });
  return card;
}

function renderPolicyForm(ctl, region) {
  ctl.state.rows = new Map();
  const names = policySourceNames(ctl.state.doc);
  if (!names.length) {
    renderRegion(region, "empty", "文档里还没有来源审批记录。");
    return;
  }
  const container = el("div", { class: "dsp-sources" });
  for (const name of names) container.appendChild(policyCard(ctl, name));
  renderRegion(region, "ready", container);
}

// 标签 A / B 的行编辑回调：字段行与来源卡只是行标识不同。
function afterFieldEdit(ctl, name) {
  ctl.afterEdit(name);
}

function afterPolicyEdit(ctl, sourceId) {
  ctl.afterEdit(sourceId);
}

function createSourcePolicyTab() {
  return configDocTab({
    kind: "source_policy",
    endpoint: POLICY_ENDPOINT,
    historyEndpoint: POLICY_HISTORY_ENDPOINT,
    baselineImportEndpoint: POLICY_BASELINE_IMPORT_ENDPOINT,
    advanced: false,
    elements: {
      form: "dsp-form",
      drift: "dsp-drift",
      meta: "dsp-meta",
      historyOpen: "dsp-history-open",
      baselineOpen: "dsp-baseline-open",
      savebar: "dsp-savebar",
      dirtyCount: "dsp-dirty-count",
      note: "dsp-note",
      discard: "dsp-discard",
      save: "dsp-save",
      saveResult: "dsp-save-result",
    },
    emptyDoc: () => ({ schema_version: 1, revision: "", sources: {} }),
    metaParts: (self) => {
      // 顶层 revision id 是受控记录标识（§4.6：revision id 展示）。
      const parts = [`策略版本 ${formatDocValue(self.state.doc?.revision)}`, `修订 ${self.state.loaded.revision}`];
      if (self.state.loaded.updated_at) {
        parts.push(formatDateTime(self.state.loaded.updated_at));
        parts.push(label(ACTOR_LABELS, self.state.loaded.updated_by));
      } else {
        parts.push("未导入（按基线文件显示）");
      }
      return parts;
    },
    rowIssues: policyRowIssues,
    rowDirty: policyRowDirty,
    savedMessage: "已保存。来源评估在下一个采集任务读取新策略。",
    renderForm: renderPolicyForm,
  });
}

// ---- 标签切换 ------------------------------------------------------------------
// 沿用候选详情页的 tab 模式（candidates.js）：role=tab 按钮 + aria-controls 面板，
// 左右方向键切换；标签 B 首次激活时才加载（惰性，避免无谓请求）。

function wireConfigDocTabs(tabs) {
  const entries = tabs
    .map((tab) => ({
      ...tab,
      button: document.getElementById(tab.tabId),
      panel: document.getElementById(tab.panelId),
    }))
    .filter((entry) => entry.button && entry.panel);
  if (!entries.length) return;
  const select = (name, { focus = false } = {}) => {
    const entry = entries.find((item) => item.name === name) || entries[0];
    for (const item of entries) {
      const on = item === entry;
      if (on) item.button.classList.add("on");
      else item.button.classList.remove("on");
      item.button.setAttribute("aria-selected", on ? "true" : "false");
      item.button.tabIndex = on ? 0 : -1;
      item.panel.hidden = !on;
      if (on && focus) item.button.focus();
    }
    activeTab = entry.ctl;
    if (!entry.ctl.state.loaded) entry.ctl.load();
  };
  for (const entry of entries) {
    entry.button.addEventListener("click", () => select(entry.name));
    entry.button.addEventListener("keydown", (event) => {
      const index = entries.indexOf(entry);
      const step = event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0;
      if (!step) return;
      event.preventDefault();
      select(entries[(index + step + entries.length) % entries.length].name, { focus: true });
    });
  }
  select(entries[0].name);
}

// ---- 装配 --------------------------------------------------------------------

const tabs = [];

function isAnyDirty() {
  return tabs.some((ctl) => ctl.isDirty());
}

export function bootDataSources() {
  const region = document.getElementById("ds-form");
  if (!region) return;
  const fields = createFieldSourcesTab();
  const policy = createSourcePolicyTab();
  tabs.push(fields, policy);
  fields.wire();
  policy.wire();
  wireConfigDocTabs([
    { name: "fields", tabId: "ds-tab-fields", panelId: "ds-panel-fields", ctl: fields },
    { name: "policy", tabId: "ds-tab-policy", panelId: "ds-panel-policy", ctl: policy },
  ]);
  // 共享抽屉的导入动作作用于当前激活标签的文档。
  const importButton = document.getElementById("ds-baseline-import");
  if (importButton) importButton.addEventListener("click", () => activeTab?.importBaseline(importButton));
  registerRefreshGuard("data-sources", () => (isAnyDirty() ? "有尚未保存的数据源配置" : ""));
  // beforeunload 在非浏览器环境（node 驱动的模块测试）缺席时跳过。
  globalThis.addEventListener?.("beforeunload", (event) => {
    if (isAnyDirty()) event.preventDefault();
  });
  // 标签切换已加载首个标签；没有标签栏（异常 DOM）时仍直接加载标签 A。
  if (!activeTab) fields.load();
}
