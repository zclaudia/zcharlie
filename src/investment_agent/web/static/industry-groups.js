// 行业分组页（config-docs-plan §4.6 WP3）：分组定义 + CSRC 大类码映射表 + 申万覆写
// + 覆盖率检查。保存 = PATCH 全文档（服务端解析即校验 CD3），live 生效（API 读取
// 即用新映射）；保存自动推进 version。所有 DOM 经 dom.js 构造，不写 HTML 字符串。

import { ApiError, get, lockWhileBusy, patch, post, renderRegion } from "./api.js";
import { clear, el } from "./dom.js";
import { ACTOR_LABELS, canonicalJson, sameDocument, sameValue } from "./data-sources.js";
import { formatDateTime, label } from "./format.js";
import { registerRefreshGuard } from "./refresh_state.js";

export const INDUSTRY_GROUP_REGIONS = new Set([
  "ig-form",
  "ig-history",
  "ig-coverage",
  "ig-baseline-diff",
]);

const ENDPOINT = "/config-docs/industry_groups";
const HISTORY_ENDPOINT = "/config-docs/industry_groups/history?limit=50";
const BASELINE_IMPORT_ENDPOINT = "/config-docs/industry_groups/baseline-import";
const COVERAGE_ENDPOINT = "/config-docs/industry-groups/coverage-check";

const DOC_KEY_LABELS = Object.freeze({
  version: "版本",
  system: "分类体系",
});

const GROUP_ID_RE = /^[a-z][a-z0-9_]{1,63}$/;
const CSRC_CODE_RE = /^[A-S]\d{2}$/;

// ---- 纯函数（无 DOM，可在 node 中测试） --------------------------------------

export function canonical(value) {
  return canonicalJson(value);
}

export function sameDoc(a, b) {
  return sameDocument(a, b);
}

// 申万代码层级：末四位 0000 = 一级，末两位 00 = 二级，其余三级（镜像 code_level）。
export function shenwanLevel(code) {
  const text = String(code ?? "");
  if (!/^\d{6}$/.test(text)) return 0;
  if (text.endsWith("0000")) return 1;
  if (text.endsWith("00")) return 2;
  return 3;
}

// 保存自动推进 version（§4.6“保存自动 bump version”，客户端实现）：
// “YYYY-MM-DD.n”，同日 n+1；跨日或其他格式回到当日 .1。日期取上海时区。
export function nextVersion(version, today = null) {
  const day =
    today ?? new Date().toLocaleDateString("sv-SE", { timeZone: "Asia/Shanghai" });
  const match = /^(\d{4}-\d{2}-\d{2})\.(\d+)$/.exec(String(version ?? "").trim());
  return match && match[1] === day ? `${day}.${Number(match[2]) + 1}` : `${day}.1`;
}

// 分组被文档内映射引用的次数（删除守卫的客户端面；模板 applicable_groups 的引用
// 由服务端保存时 422 兜底）。
export function groupReferenceCounts(doc) {
  const counts = new Map();
  const bump = (group) => counts.set(group, (counts.get(group) || 0) + 1);
  for (const group of Object.values(doc?.codes || {})) bump(group);
  for (const rule of doc?.legacy_name_rules || []) {
    if (rule && typeof rule === "object" && rule.group) bump(rule.group);
  }
  const shenwan = doc?.shenwan || {};
  for (const group of Object.values(shenwan.level1 || {})) bump(group);
  for (const group of Object.values(shenwan.overrides || {})) bump(group);
  return counts;
}

// 映射表行（搜索 / 分组筛选后按码排序）。
export function codeRows(doc, { search = "", group = "" } = {}) {
  const needle = String(search ?? "").trim().toLowerCase();
  const codes = doc?.codes || {};
  return Object.keys(codes)
    .filter((code) => !needle || code.toLowerCase().includes(needle))
    .filter((code) => !group || codes[code] === group)
    .sort();
}

// 两份全文档的逐项差异（基线漂移、从基线导入预览与历史摘要共用一个视图）。
export function diffIndustryDocuments(oldDoc, newDoc) {
  const before = oldDoc || {};
  const after = newDoc || {};
  const sectionKeys = new Set(["groups", "codes", "legacy_name_rules", "shenwan"]);
  const entries = [];
  for (const key of new Set([...Object.keys(before), ...Object.keys(after)])) {
    if (sectionKeys.has(key)) continue;
    if (!sameValue(before[key], after[key])) {
      entries.push({
        kind: "doc",
        key,
        label: DOC_KEY_LABELS[key] || key,
        from: before[key],
        to: after[key],
      });
    }
  }
  const groupsBefore = before.groups || {};
  const groupsAfter = after.groups || {};
  for (const id of new Set([...Object.keys(groupsBefore), ...Object.keys(groupsAfter)])) {
    if (!(id in groupsAfter)) entries.push({ kind: "group-removed", id });
    else if (!(id in groupsBefore)) entries.push({ kind: "group-added", id });
    else if (!sameValue(groupsBefore[id], groupsAfter[id])) {
      entries.push({ kind: "group-changed", id, from: groupsBefore[id], to: groupsAfter[id] });
    }
  }
  const codesBefore = before.codes || {};
  const codesAfter = after.codes || {};
  for (const code of new Set([...Object.keys(codesBefore), ...Object.keys(codesAfter)])) {
    if (!(code in codesAfter)) entries.push({ kind: "code-removed", code, from: codesBefore[code] });
    else if (!(code in codesBefore)) entries.push({ kind: "code-added", code, to: codesAfter[code] });
    else if (codesBefore[code] !== codesAfter[code]) {
      entries.push({ kind: "code-changed", code, from: codesBefore[code], to: codesAfter[code] });
    }
  }
  // legacy_name_rules 只在内容变化时给一条汇总（编辑面不展开它）。
  if (!sameValue(before.legacy_name_rules, after.legacy_name_rules)) {
    entries.push({
      kind: "legacy-rules",
      from: (before.legacy_name_rules || []).length,
      to: (after.legacy_name_rules || []).length,
    });
  }
  const section = (doc, part) => doc?.shenwan?.[part] || {};
  for (const [part, kind] of [
    ["level1", "level1"],
    ["overrides", "override"],
  ]) {
    const b = section(before, part);
    const a = section(after, part);
    for (const code of new Set([...Object.keys(b), ...Object.keys(a)])) {
      if (!(code in a)) entries.push({ kind: `${kind}-removed`, code, from: b[code] });
      else if (!(code in b)) entries.push({ kind: `${kind}-added`, code, to: a[code] });
      else if (b[code] !== a[code]) {
        entries.push({ kind: `${kind}-changed`, code, from: b[code], to: a[code] });
      }
    }
  }
  return entries;
}

export function diffEntryText(entry) {
  switch (entry.kind) {
    case "doc":
      return `${entry.label}：${entry.from ?? "（未设置）"} → ${entry.to ?? "（未设置）"}`;
    case "group-added":
      return `新增分组 ${entry.id}`;
    case "group-removed":
      return `移除分组 ${entry.id}`;
    case "group-changed":
      return `分组 ${entry.id} 说明：${entry.from} → ${entry.to}`;
    case "code-added":
      return `映射 ${entry.code} → ${entry.to}（新增）`;
    case "code-removed":
      return `移除映射 ${entry.code}（原 ${entry.from}）`;
    case "code-changed":
      return `映射 ${entry.code}：${entry.from} → ${entry.to}`;
    case "legacy-rules":
      return `历史名称规则：${entry.from} 条 → ${entry.to} 条`;
    case "level1-added":
      return `一级默认 ${entry.code} → ${entry.to}（新增）`;
    case "level1-removed":
      return `移除一级默认 ${entry.code}（原 ${entry.from}）`;
    case "level1-changed":
      return `一级默认 ${entry.code}：${entry.from} → ${entry.to}`;
    case "override-added":
      return `覆写 ${entry.code} → ${entry.to}（新增）`;
    case "override-removed":
      return `移除覆写 ${entry.code}（原 ${entry.from}）`;
    case "override-changed":
      return `覆写 ${entry.code}：${entry.from} → ${entry.to}`;
    default:
      return JSON.stringify(entry);
  }
}

// ---- 页面状态 -----------------------------------------------------------------

const state = {
  loaded: null, // GET 响应（value / baseline_value / revision / 元数据 / drifted）
  doc: null, // 工作文档（三个编辑区共同编辑的唯一对象）
  original: null, // 保存基准（canonical 对比）
  selection: new Set(), // 映射表勾选的 CSRC 码（批量改组）
  filter: { search: "", group: "" },
};

const refs = {}; // 编辑器子区容器与联动控件（buildEditor 填充，renderXxx 只动这些）

function redirectOn401(error) {
  if (error instanceof ApiError && error.status === 401) {
    globalThis.location.href = "/app/login";
    return true;
  }
  return false;
}

function isDirty() {
  return Boolean(state.doc && state.original && !sameDocument(state.doc, state.original));
}

registerRefreshGuard("industry-groups", () => (isDirty() ? "有尚未保存的行业分组修改" : ""));

function groupIds() {
  return Object.keys(state.doc?.groups || {}).sort();
}

// 分组下拉：占位项可选；每次 renderAll 重建以纳入新增分组（保留当前选择）。
function groupSelect(ariaLabel, placeholder) {
  const select = el("select", { class: "ig-group-select", "aria-label": ariaLabel });
  if (placeholder) select.setAttribute("data-placeholder", placeholder);
  fillGroupOptions(select);
  return select;
}

function fillGroupOptions(select) {
  const placeholder = select.getAttribute("data-placeholder") || "";
  const current = select.value;
  clear(select);
  if (placeholder) select.appendChild(el("option", { value: "" }, placeholder));
  for (const id of groupIds()) select.appendChild(el("option", { value: id }, id));
  if (current) select.value = current;
}

function showIssues(list, messages) {
  if (!list) return;
  clear(list);
  for (const message of messages || []) list.appendChild(el("li", {}, message));
}

// 表格骨架（dom.js table 的行后置版本：行由 renderXxx 渐进填充，正文容器可反复清空）。
function grid(headers) {
  const body = el("tbody");
  const node = el("table", { class: "stack-table" });
  const headRow = el("tr");
  for (const header of headers) headRow.appendChild(el("th", { scope: "col" }, header.label));
  node.appendChild(el("thead")).appendChild(headRow);
  node.appendChild(body);
  return {
    node,
    body,
    row(cells) {
      const tr = el("tr");
      cells.forEach((cell, index) => {
        if (headers[index] && headers[index].label && !cell.hasAttribute("data-label")) {
          cell.setAttribute("data-label", headers[index].label);
        }
        tr.appendChild(cell);
      });
      body.appendChild(tr);
      return tr;
    },
  };
}

// ---- 分组定义 ------------------------------------------------------------------

function renderGroups() {
  if (!refs.groupsGrid) return;
  clear(refs.groupsGrid.body);
  const counts = groupReferenceCounts(state.doc);
  for (const id of groupIds()) {
    const refsCount = counts.get(id) || 0;
    const actionCell = el("td", { class: "ig-action-cell" });
    const remove = el("button", { type: "button", class: "btn btn-sm btn-danger-ghost" }, "移除");
    if (id === "other") {
      remove.disabled = true;
      remove.setAttribute("title", "other 为必填分组（未分类兜底），不能移除");
    } else if (refsCount) {
      remove.disabled = true;
      remove.setAttribute(
        "title",
        `被 ${refsCount} 处映射引用，先把它们改指其他分组（模板引用由保存时的服务端校验兜底）`
      );
    } else {
      remove.setAttribute("aria-label", `移除分组 ${id}`);
      remove.addEventListener("click", () => {
        delete state.doc.groups[id];
        afterEdit();
        renderAll();
      });
    }
    actionCell.appendChild(remove);
    const descCell = el("td", { class: "ig-desc-cell" });
    const desc = el("input", {
      type: "text",
      class: "ig-desc",
      "aria-label": `${id} 分组说明`,
      value: state.doc.groups[id] ?? "",
    });
    desc.addEventListener("input", () => {
      state.doc.groups[id] = desc.value;
      afterEdit();
    });
    descCell.appendChild(desc);
    const tr = refs.groupsGrid.row([
      el("td", { class: "mono ig-group-id" }, id),
      descCell,
      el("td", { class: "ig-ref-cell" }, refsCount ? `${refsCount} 处引用` : "无引用"),
      actionCell,
    ]);
    tr.setAttribute("data-group", id);
  }
}

function wireGroupsSection(box) {
  box.appendChild(el("h3", { class: "settings-kicker ig-section-title" }, "分组定义"));
  box.appendChild(
    el(
      "p",
      { class: "faint ig-section-note" },
      "分组 id 被映射与模板的 applicable_groups 引用，创建后不可改名；改名 = 新建分组、改指映射后移除旧分组（仍有引用时移除被拦截）。"
    )
  );
  refs.groupsGrid = grid([{ label: "分组 id" }, { label: "说明" }, { label: "引用" }, { label: "操作" }]);
  const wrap = el("div", { class: "table-wrap" });
  wrap.appendChild(refs.groupsGrid.node);
  box.appendChild(wrap);
  const addRow = el("div", { class: "ig-add-row" });
  const idInput = el("input", {
    type: "text",
    class: "mono ig-add-id",
    placeholder: "新分组 id（如 materials）",
    "aria-label": "新分组 id",
  });
  const descInput = el("input", {
    type: "text",
    class: "ig-add-desc",
    placeholder: "分组说明",
    "aria-label": "新分组说明",
  });
  const add = el("button", { type: "button", class: "btn btn-sm" }, "添加分组");
  add.addEventListener("click", () => {
    const id = idInput.value.trim();
    const issues = [];
    if (!GROUP_ID_RE.test(id)) issues.push("分组 id 须为小写字母开头的字母 / 数字 / 下划线。");
    if (id in (state.doc.groups || {})) issues.push(`分组 ${id} 已存在。`);
    showIssues(refs.groupsIssues, issues);
    if (issues.length) return;
    state.doc.groups = { ...(state.doc.groups || {}), [id]: descInput.value.trim() };
    idInput.value = "";
    descInput.value = "";
    showIssues(refs.groupsIssues, []);
    afterEdit();
    renderAll();
  });
  addRow.appendChild(idInput);
  addRow.appendChild(descInput);
  addRow.appendChild(add);
  box.appendChild(addRow);
  refs.groupsIssues = el("ul", { class: "issue-list setting-issues issues" });
  box.appendChild(refs.groupsIssues);
}

// ---- CSRC 映射表（搜索 / 筛选 / 批量改组） --------------------------------------

function filteredCodes() {
  return codeRows(state.doc, state.filter);
}

function renderCodeRows() {
  if (!refs.codesGrid) return;
  clear(refs.codesGrid.body);
  const originalCodes = state.original?.codes || {};
  for (const code of filteredCodes()) {
    const checkCell = el("td", { class: "ig-check-cell" });
    const check = el("input", { type: "checkbox", "aria-label": `选择 ${code}` });
    check.checked = state.selection.has(code);
    check.addEventListener("change", () => {
      if (check.checked) state.selection.add(code);
      else state.selection.delete(code);
      updateBulkBar();
    });
    checkCell.appendChild(check);
    const groupCell = el("td", { class: "ig-edit-cell" });
    const select = groupSelect(`${code} 所属分组`);
    select.value = state.doc.codes[code] ?? "";
    select.addEventListener("change", () => {
      state.doc.codes[code] = select.value;
      afterEdit();
      renderCodeRows();
    });
    groupCell.appendChild(select);
    const actionCell = el("td", { class: "ig-action-cell" });
    const remove = el("button", { type: "button", class: "btn btn-sm btn-danger-ghost" }, "移除");
    remove.addEventListener("click", () => {
      delete state.doc.codes[code];
      state.selection.delete(code);
      afterEdit();
      renderCodeRows();
    });
    actionCell.appendChild(remove);
    const tr = refs.codesGrid.row([
      checkCell,
      el("td", { class: "mono ig-code-cell" }, code),
      groupCell,
      actionCell,
    ]);
    tr.setAttribute("data-code", code);
    if (originalCodes[code] !== state.doc.codes[code]) tr.classList.add("dirty");
  }
  updateBulkBar();
}

function updateBulkBar() {
  if (!refs.bulkLabel || !refs.bulkButton) return;
  const shown = filteredCodes();
  const selected = shown.filter((code) => state.selection.has(code));
  refs.bulkLabel.textContent = selected.length
    ? `已选 ${selected.length} 个码`
    : `共 ${shown.length} 个码`;
  refs.bulkButton.disabled = !selected.length;
}

function wireCodesSection(box) {
  box.appendChild(
    el("h3", { class: "settings-kicker ig-section-title" }, "映射表（CSRC 大类码 → 分组）")
  );
  const toolbar = el("div", { class: "ig-toolbar" });
  const search = el("input", {
    type: "search",
    class: "ig-search",
    placeholder: "搜索码（如 J66 / C3）",
    "aria-label": "搜索 CSRC 码",
  });
  search.addEventListener("input", () => {
    state.filter.search = search.value;
    renderCodeRows();
  });
  const groupFilter = el("select", { class: "ig-filter", "aria-label": "按分组筛选" });
  groupFilter.addEventListener("change", () => {
    state.filter.group = groupFilter.value;
    renderCodeRows();
  });
  refs.groupFilter = groupFilter;
  toolbar.appendChild(search);
  toolbar.appendChild(groupFilter);
  const selectAll = el("button", { type: "button", class: "btn btn-sm" }, "全选当前结果");
  selectAll.addEventListener("click", () => {
    for (const code of filteredCodes()) state.selection.add(code);
    renderCodeRows();
  });
  toolbar.appendChild(selectAll);
  const clearAll = el("button", { type: "button", class: "btn btn-sm" }, "清空选择");
  clearAll.addEventListener("click", () => {
    state.selection.clear();
    renderCodeRows();
  });
  toolbar.appendChild(clearAll);
  box.appendChild(toolbar);

  refs.codesGrid = grid([
    { label: "选择" },
    { label: "CSRC 码" },
    { label: "分组" },
    { label: "操作" },
  ]);
  const wrap = el("div", { class: "table-wrap" });
  wrap.appendChild(refs.codesGrid.node);
  box.appendChild(wrap);

  const bulkBar = el("div", { class: "ig-bulk-bar" });
  refs.bulkLabel = el("span", { class: "faint" }, "");
  refs.bulkSelect = el("select", {
    class: "ig-group-select",
    "aria-label": "批量改组目标分组",
    "data-placeholder": "改组为…",
  });
  refs.bulkButton = el(
    "button",
    { type: "button", class: "btn btn-sm btn-primary primary" },
    "批量改组"
  );
  refs.bulkButton.disabled = true;
  refs.bulkButton.addEventListener("click", () => {
    const target = refs.bulkSelect.value;
    if (!target) return;
    for (const code of state.selection) state.doc.codes[code] = target;
    state.selection.clear();
    afterEdit();
    renderCodeRows();
  });
  bulkBar.appendChild(refs.bulkLabel);
  bulkBar.appendChild(refs.bulkSelect);
  bulkBar.appendChild(refs.bulkButton);
  box.appendChild(bulkBar);

  const addRow = el("div", { class: "ig-add-row" });
  const codeInput = el("input", {
    type: "text",
    class: "mono ig-add-code",
    placeholder: "新码（如 J66）",
    "aria-label": "新 CSRC 码",
  });
  refs.addCodeGroup = groupSelect("新 CSRC 码的分组", "分组…");
  const add = el("button", { type: "button", class: "btn btn-sm" }, "添加映射");
  add.addEventListener("click", () => {
    const code = codeInput.value.trim().toUpperCase();
    const issues = [];
    if (!CSRC_CODE_RE.test(code)) issues.push("CSRC 码形如 门类字母+两位数字（J66、C15）。");
    if (code in (state.doc.codes || {})) issues.push(`码 ${code} 已有映射。`);
    if (!refs.addCodeGroup.value) issues.push("请先选择新码的分组。");
    showIssues(refs.codesIssues, issues);
    if (issues.length) return;
    state.doc.codes = { ...(state.doc.codes || {}), [code]: refs.addCodeGroup.value };
    codeInput.value = "";
    showIssues(refs.codesIssues, []);
    afterEdit();
    renderCodeRows();
  });
  addRow.appendChild(codeInput);
  addRow.appendChild(refs.addCodeGroup);
  addRow.appendChild(add);
  box.appendChild(addRow);
  refs.codesIssues = el("ul", { class: "issue-list setting-issues issues" });
  box.appendChild(refs.codesIssues);
}

// ---- 申万（一级默认 + 二三级覆写） ----------------------------------------------

function ensureShenwan() {
  if (!state.doc.shenwan || typeof state.doc.shenwan !== "object") {
    state.doc.shenwan = { level1: {}, overrides: {} };
  } else {
    if (!state.doc.shenwan.level1) state.doc.shenwan.level1 = {};
    if (!state.doc.shenwan.overrides) state.doc.shenwan.overrides = {};
  }
  return state.doc.shenwan;
}

function renderShenwan() {
  if (!refs.level1Grid || !refs.overridesGrid) return;
  clear(refs.level1Grid.body);
  clear(refs.overridesGrid.body);
  const shenwan = state.doc?.shenwan || {};
  const originalShenwan = state.original?.shenwan || {};
  for (const code of Object.keys(shenwan.level1 || {}).sort()) {
    const groupCell = el("td", { class: "ig-edit-cell" });
    const select = groupSelect(`申万 ${code} 默认分组`);
    select.value = shenwan.level1[code] ?? "";
    select.addEventListener("change", () => {
      ensureShenwan().level1[code] = select.value;
      afterEdit();
      renderShenwan();
    });
    groupCell.appendChild(select);
    const tr = refs.level1Grid.row([
      el("td", { class: "mono ig-code-cell" }, code),
      el("td", { class: "faint ig-level-cell" }, "一级"),
      groupCell,
      swRemoveCell(code, "level1"),
    ]);
    tr.setAttribute("data-sw", code);
    if ((originalShenwan.level1 || {})[code] !== shenwan.level1[code]) tr.classList.add("dirty");
  }
  for (const code of Object.keys(shenwan.overrides || {}).sort()) {
    const groupCell = el("td", { class: "ig-edit-cell" });
    const select = groupSelect(`申万 ${code} 覆写分组`);
    select.value = shenwan.overrides[code] ?? "";
    select.addEventListener("change", () => {
      ensureShenwan().overrides[code] = select.value;
      afterEdit();
      renderShenwan();
    });
    groupCell.appendChild(select);
    const tr = refs.overridesGrid.row([
      el("td", { class: "mono ig-code-cell" }, code),
      el("td", { class: "faint ig-level-cell" }, shenwanLevel(code) === 2 ? "二级" : "三级"),
      groupCell,
      swRemoveCell(code, "overrides"),
    ]);
    tr.setAttribute("data-sw", code);
    if ((originalShenwan.overrides || {})[code] !== shenwan.overrides[code]) {
      tr.classList.add("dirty");
    }
  }
}

function swRemoveCell(code, part) {
  const actionCell = el("td", { class: "ig-action-cell" });
  const remove = el("button", { type: "button", class: "btn btn-sm btn-danger-ghost" }, "移除");
  remove.setAttribute("aria-label", `移除申万 ${code}`);
  if (part === "level1") {
    // 删除一级默认会孤悬其下的覆写（服务端 parse 也拒绝）；有覆写时先移除覆写。
    const children = Object.keys(state.doc?.shenwan?.overrides || {}).filter(
      (item) => item.slice(0, 2) === code.slice(0, 2)
    );
    if (children.length) {
      remove.disabled = true;
      remove.setAttribute(
        "title",
        `其下仍有 ${children.length} 个覆写（${children.join("、")}），先移除它们`
      );
    } else {
      remove.addEventListener("click", () => {
        delete state.doc.shenwan.level1[code];
        afterEdit();
        renderShenwan();
      });
    }
  } else {
    remove.addEventListener("click", () => {
      delete state.doc.shenwan.overrides[code];
      afterEdit();
      renderShenwan();
    });
  }
  actionCell.appendChild(remove);
  return actionCell;
}

function wireShenwanSection(box) {
  box.appendChild(el("h3", { class: "settings-kicker ig-section-title" }, "申万 2021（先于 CSRC 表）"));
  box.appendChild(
    el(
      "p",
      { class: "faint ig-section-note" },
      "申万一级给默认分组；二级 / 三级覆写优先于一级默认。覆写码的一级必须已有默认。"
    )
  );
  refs.level1Grid = grid([
    { label: "申万码" },
    { label: "层级" },
    { label: "默认分组" },
    { label: "操作" },
  ]);
  const wrap1 = el("div", { class: "table-wrap" });
  wrap1.appendChild(refs.level1Grid.node);
  box.appendChild(wrap1);
  const add1 = el("div", { class: "ig-add-row" });
  const level1Input = el("input", {
    type: "text",
    class: "mono ig-add-sw",
    placeholder: "新一级码（如 480000）",
    "aria-label": "新申万一级码",
  });
  refs.addLevel1Group = groupSelect("新申万一级码的分组", "分组…");
  const addLevel1 = el("button", { type: "button", class: "btn btn-sm" }, "添加默认");
  addLevel1.addEventListener("click", () => {
    const code = level1Input.value.trim();
    const issues = [];
    if (shenwanLevel(code) !== 1) issues.push("一级码形如 480000（前两位数字 + 0000）。");
    if (code in (state.doc?.shenwan?.level1 || {})) issues.push(`一级码 ${code} 已有默认。`);
    if (!refs.addLevel1Group.value) issues.push("请先选择默认分组。");
    showIssues(refs.swIssues, issues);
    if (issues.length) return;
    ensureShenwan().level1[code] = refs.addLevel1Group.value;
    level1Input.value = "";
    showIssues(refs.swIssues, []);
    afterEdit();
    renderShenwan();
  });
  add1.appendChild(level1Input);
  add1.appendChild(refs.addLevel1Group);
  add1.appendChild(addLevel1);
  box.appendChild(add1);

  refs.overridesGrid = grid([
    { label: "申万码" },
    { label: "层级" },
    { label: "覆写分组" },
    { label: "操作" },
  ]);
  const wrap2 = el("div", { class: "table-wrap" });
  wrap2.appendChild(refs.overridesGrid.node);
  box.appendChild(wrap2);
  const add2 = el("div", { class: "ig-add-row" });
  const overrideInput = el("input", {
    type: "text",
    class: "mono ig-add-sw",
    placeholder: "新覆写码（二级 490100 / 三级 630601）",
    "aria-label": "新申万覆写码",
  });
  refs.addOverrideGroup = groupSelect("新申万覆写码的分组", "分组…");
  const addOverride = el("button", { type: "button", class: "btn btn-sm" }, "添加覆写");
  addOverride.addEventListener("click", () => {
    const code = overrideInput.value.trim();
    const issues = [];
    if (shenwanLevel(code) < 2) issues.push("覆写码是二级（490100）或三级（630601）6 位码。");
    if (code in (state.doc?.shenwan?.overrides || {})) issues.push(`覆写码 ${code} 已存在。`);
    const parent = `${code.slice(0, 2)}0000`;
    if (shenwanLevel(code) >= 2 && !(parent in (state.doc?.shenwan?.level1 || {}))) {
      issues.push(`其一级 ${parent} 还没有默认分组，先添加默认。`);
    }
    if (!refs.addOverrideGroup.value) issues.push("请先选择覆写分组。");
    showIssues(refs.swIssues, issues);
    if (issues.length) return;
    ensureShenwan().overrides[code] = refs.addOverrideGroup.value;
    overrideInput.value = "";
    showIssues(refs.swIssues, []);
    afterEdit();
    renderShenwan();
  });
  add2.appendChild(overrideInput);
  add2.appendChild(refs.addOverrideGroup);
  add2.appendChild(addOverride);
  box.appendChild(add2);
  refs.swIssues = el("ul", { class: "issue-list setting-issues issues" });
  box.appendChild(refs.swIssues);
}

// ---- 覆盖率检查 -----------------------------------------------------------------

function coverageGapRow(gap, kind) {
  const row = el("div", { class: `ig-gap ig-gap-${kind}` });
  const head = el("span", { class: "ig-gap-head" });
  head.appendChild(el("b", { class: "mono" }, gap.code));
  if (gap.name) head.appendChild(el("span", { class: "faint" }, gap.name));
  if (Array.isArray(gap.tickers) && gap.tickers.length) {
    head.appendChild(
      el(
        "span",
        { class: "faint" },
        `${gap.tickers.length} 只成分（${gap.tickers.slice(0, 6).join("、")}${
          gap.tickers.length > 6 ? "…" : ""
        }）`
      )
    );
  }
  row.appendChild(head);
  if (kind === "override") {
    const remove = el("button", { type: "button", class: "btn btn-sm btn-danger-ghost" }, "移除覆写");
    remove.addEventListener("click", () => {
      delete state.doc.shenwan.overrides[gap.code];
      afterEdit();
      renderAll();
      runCoverage();
    });
    row.appendChild(remove);
    return row;
  }
  const select = groupSelect(`${gap.code} 指派分组`, "分组…");
  const assign = el(
    "button",
    { type: "button", class: "btn btn-sm" },
    kind === "csrc" ? "指派" : "设默认"
  );
  assign.addEventListener("click", () => {
    if (!select.value) return;
    if (kind === "csrc") {
      state.doc.codes = { ...(state.doc.codes || {}), [gap.code]: select.value };
    } else {
      ensureShenwan().level1[gap.code] = select.value;
    }
    afterEdit();
    renderAll();
    runCoverage();
  });
  row.appendChild(select);
  row.appendChild(assign);
  return row;
}

function renderCoverage(payload) {
  const region = document.getElementById("ig-coverage");
  if (!region) return;
  clear(region);
  const box = el("div", { class: "ig-coverage-box" });
  box.appendChild(
    el("p", { class: payload.ok ? "notice" : "notice notice-warn" }, payload.summary)
  );
  const sections = [
    ["未显式映射的 CSRC 码", payload.unmapped_csrc, "csrc"],
    ["缺默认的申万一级", payload.missing_level1_defaults, "level1"],
    ["目录外的覆写", payload.invalid_overrides, "override"],
  ];
  for (const [title, gaps, kind] of sections) {
    if (!Array.isArray(gaps) || !gaps.length) continue;
    box.appendChild(el("h4", { class: "ig-coverage-sub" }, `${title}（${gaps.length}）`));
    const list = el("div", { class: "ig-gap-list" });
    for (const gap of gaps) list.appendChild(coverageGapRow(gap, kind));
    box.appendChild(list);
  }
  region.appendChild(box);
}

async function runCoverage(button = null) {
  const promise = post(COVERAGE_ENDPOINT, { value: state.doc })
    .then(renderCoverage)
    .catch((error) => {
      if (redirectOn401(error)) return;
      const region = document.getElementById("ig-coverage");
      if (region) {
        clear(region);
        region.appendChild(
          el(
            "p",
            { class: "notice notice-error" },
            `覆盖率检查失败：${error instanceof Error ? error.message : String(error)}`
          )
        );
      }
    });
  if (button) lockWhileBusy(button, promise);
  await promise;
}

// ---- 头部 / 漂移横幅 / 保存 ------------------------------------------------------

function renderHeader() {
  const meta = document.getElementById("ig-meta");
  if (!meta || !state.loaded) return;
  const parts = [`版本 ${state.doc?.version ?? "—"}`, `修订 ${state.loaded.revision}`];
  if (state.loaded.updated_at) {
    parts.push(formatDateTime(state.loaded.updated_at));
    parts.push(label(ACTOR_LABELS, state.loaded.updated_by));
  } else {
    parts.push("未导入（按基线文件显示）");
  }
  meta.textContent = parts.join(" · ");
}

function renderDrift() {
  const banner = document.getElementById("ig-drift");
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
  const show = el("button", { type: "button", class: "btn btn-sm ig-drift-link" }, "查看差异");
  show.addEventListener("click", openBaselineDrawer);
  banner.appendChild(show);
}

function setSaveResult(lines, kind) {
  const box = document.getElementById("ig-save-result");
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
  const counter = document.getElementById("ig-dirty-count");
  const save = document.getElementById("ig-save");
  const discard = document.getElementById("ig-discard");
  const bar = document.getElementById("ig-savebar");
  const result = document.getElementById("ig-save-result");
  const dirty = isDirty();
  if (counter) {
    counter.textContent = dirty ? "有未保存的修改" : "没有未保存的修改";
    if (dirty) counter.classList.add("has-dirty");
    else counter.classList.remove("has-dirty");
  }
  if (save) save.disabled = !dirty;
  if (discard) discard.disabled = !dirty;
  if (bar) bar.hidden = !dirty && !(result && String(result.textContent || "").trim());
}

function afterEdit() {
  renderHeader();
  updateSavebar();
}

function renderAll() {
  renderGroups();
  renderCodeRows();
  renderShenwan();
  // 联动的分组下拉重建以纳入新增分组（保留当前选择）。
  if (refs.groupFilter) {
    const current = state.filter.group;
    clear(refs.groupFilter);
    refs.groupFilter.appendChild(el("option", { value: "" }, "全部分组"));
    for (const id of groupIds()) refs.groupFilter.appendChild(el("option", { value: id }, id));
    refs.groupFilter.value = current;
  }
  for (const select of [refs.bulkSelect, refs.addCodeGroup, refs.addLevel1Group, refs.addOverrideGroup]) {
    if (select) fillGroupOptions(select);
  }
  updateBulkBar();
  afterEdit();
}

async function save(button) {
  if (!isDirty()) return;
  const noteInput = document.getElementById("ig-note");
  const note = noteInput && noteInput.value.trim() ? noteInput.value.trim() : "";
  if (!note) {
    setSaveResult(["变更说明必填：写明这次调整的来源与原因（会进入变更历史）。"], "warn");
    noteInput?.focus();
    return;
  }
  // 保存自动推进 version（§4.6）：只随请求体发出，失败时本地版本不动。
  const value = { ...state.doc, version: nextVersion(state.doc.version) };
  const promise = patch(ENDPOINT, { value, note, expected_revision: state.loaded.revision })
    .then((data) => {
      adopt(data);
      if (noteInput) noteInput.value = "";
      setSaveResult(
        ["已保存。API 读取立即用新映射（live）；已入库的快照与规则结果保持当时的分组。"],
        ""
      );
    })
    .catch((error) => {
      if (redirectOn401(error)) return;
      if (error instanceof ApiError && error.status === 422) {
        const lines = ["服务端校验未通过（解析即校验），未保存任何修改："];
        const issues = error.detail && typeof error.detail === "object" ? error.detail.issues : null;
        if (Array.isArray(issues)) {
          for (const issue of issues) {
            const messages = Array.isArray(issue.messages)
              ? issue.messages.map(String)
              : [JSON.stringify(issue)];
            lines.push(...messages);
          }
        }
        setSaveResult(lines, "warn");
        return;
      }
      if (error instanceof ApiError && error.status === 409) {
        setSaveResult(
          [
            `保存失败：${error.message}`,
            "文档已在别处被修改（修订号不一致）。可拉取最新版本后重新应用改动。",
          ],
          "error"
        );
        const box = document.getElementById("ig-save-result");
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
  state.selection = new Set();
  state.filter = { search: "", group: "" };
  setSaveResult([], "");
  showIssues(refs.groupsIssues, []);
  showIssues(refs.codesIssues, []);
  showIssues(refs.swIssues, []);
  renderAll();
}

// ---- 基线漂移 / 导入 / 历史 -------------------------------------------------------

function renderDiffRegion(region, entries, emptyText) {
  if (!entries.length) {
    renderRegion(region, "empty", emptyText);
    return;
  }
  const list = el("ul", { class: "ig-diff-list" });
  for (const entry of entries) {
    list.appendChild(el("li", { class: `ig-diff-item ig-diff-${entry.kind}` }, diffEntryText(entry)));
  }
  const wrap = el("div", { class: "ig-diff" });
  wrap.appendChild(el("p", { class: "faint" }, `共 ${entries.length} 处差异。`));
  wrap.appendChild(list);
  renderRegion(region, "ready", wrap);
}

function openBaselineDrawer() {
  const drawer = document.getElementById("ig-baseline-drawer");
  if (!drawer || !state.loaded) return;
  const region = document.getElementById("ig-baseline-diff");
  const baseline = state.loaded.baseline_value;
  if (!baseline) {
    renderRegion(region, "error", "基线文件不可读或不是映射，无法对比。");
  } else {
    // 方向：基线 → 当前文档（与历史行的 old → new 一致）。
    renderDiffRegion(region, diffIndustryDocuments(baseline, state.doc), "当前文档与基线一致。");
  }
  if (globalThis.window?.zcOverlay) globalThis.window.zcOverlay.open(drawer);
}

async function importBaseline(button) {
  const noteInput = document.getElementById("ig-baseline-note");
  const note = noteInput && noteInput.value.trim() ? noteInput.value.trim() : "";
  if (!note) {
    noteInput?.focus();
    setSaveResult(["从基线导入需要填写导入说明。"], "warn");
    return;
  }
  const promise = post(BASELINE_IMPORT_ENDPOINT, { note })
    .then((data) => {
      adopt(data);
      if (noteInput) noteInput.value = "";
      setSaveResult(["已用基线文件覆盖当前文档（已记录一条变更历史）。"], "");
      const region = document.getElementById("ig-baseline-diff");
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

function renderHistory(region, payload) {
  const items = (payload && payload.items) || [];
  if (!items.length) {
    renderRegion(region, "empty");
    return;
  }
  const list = el("ul", { class: "ig-history-list" });
  for (const change of items) {
    const item = el("li", { class: "ig-history-item" });
    const head = el("div", { class: "ig-history-head" });
    head.appendChild(el("span", { class: "num" }, formatDateTime(change.changed_at)));
    head.appendChild(el("span", { class: "badge" }, label(ACTOR_LABELS, change.actor)));
    head.appendChild(el("span", { class: "ig-history-note" }, change.note || "—"));
    item.appendChild(head);
    const body = el("div", { class: "ig-history-body" });
    if (change.old_value === null || change.old_value === undefined) {
      body.appendChild(el("p", { class: "faint" }, "首次导入 / 创建文档。"));
    } else {
      const entries = diffIndustryDocuments(change.old_value, change.new_value);
      if (!entries.length) {
        body.appendChild(el("p", { class: "faint" }, "内容无差异（仅元数据变更）。"));
      } else {
        const details = el("details", { class: "ig-history-details" });
        details.appendChild(el("summary", {}, `${entries.length} 处变更`));
        const changes = el("ul", { class: "ig-diff-list" });
        for (const entry of entries.slice(0, 30)) {
          changes.appendChild(
            el("li", { class: `ig-diff-item ig-diff-${entry.kind}` }, diffEntryText(entry))
          );
        }
        if (entries.length > 30) {
          changes.appendChild(el("li", { class: "faint" }, `…其余 ${entries.length - 30} 处`));
        }
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
  const region = document.getElementById("ig-history");
  if (!region) return;
  renderRegion(region, "loading");
  try {
    renderHistory(region, await get(HISTORY_ENDPOINT));
  } catch (error) {
    if (redirectOn401(error)) return;
    renderRegion(region, "error", error.message);
  }
}

// ---- 装配 -----------------------------------------------------------------------

function buildEditor() {
  const container = el("div", { class: "ig-editor" });
  wireGroupsSection(container);
  wireCodesSection(container);
  wireShenwanSection(container);
  return container;
}

function adopt(data) {
  state.loaded = data;
  state.doc =
    data.value && typeof data.value === "object"
      ? structuredClone(data.value)
      : { version: "", groups: { other: "" }, codes: {} };
  state.original = structuredClone(state.doc);
  state.selection = new Set();
  renderHeader();
  renderDrift();
  renderAll();
  updateSavebar();
}

async function load() {
  const region = document.getElementById("ig-form");
  if (!region) return;
  renderRegion(region, "loading");
  try {
    const data = await get(ENDPOINT);
    renderRegion(region, "ready", buildEditor());
    adopt(data);
  } catch (error) {
    if (redirectOn401(error)) return;
    renderRegion(region, "error", error.message);
  }
}

export function bootIndustryGroups() {
  const region = document.getElementById("ig-form");
  if (!region) return;
  const saveButton = document.getElementById("ig-save");
  if (saveButton) saveButton.addEventListener("click", () => save(saveButton));
  const discardButton = document.getElementById("ig-discard");
  if (discardButton) discardButton.addEventListener("click", discard);
  const historyButton = document.getElementById("ig-history-open");
  if (historyButton) {
    historyButton.addEventListener("click", () => {
      const drawer = document.getElementById("ig-history-drawer");
      if (drawer && globalThis.window?.zcOverlay) globalThis.window.zcOverlay.open(drawer);
      loadHistory();
    });
  }
  const baselineButton = document.getElementById("ig-baseline-open");
  if (baselineButton) baselineButton.addEventListener("click", openBaselineDrawer);
  const importButton = document.getElementById("ig-baseline-import");
  if (importButton) importButton.addEventListener("click", () => importBaseline(importButton));
  const coverageButton = document.getElementById("ig-coverage-run");
  if (coverageButton) coverageButton.addEventListener("click", () => runCoverage(coverageButton));
  // beforeunload 在非浏览器环境（node 驱动的模块测试）缺席时跳过。
  globalThis.addEventListener?.("beforeunload", (event) => {
    if (isDirty()) event.preventDefault();
  });
  load();
}
