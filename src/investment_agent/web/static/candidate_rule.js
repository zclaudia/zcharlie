// 规则草稿的区间树和参数表。重新起草和保存由详情页传入，这里不读详情状态。

import { ApiError, get, lockWhileBusy, patch, post, renderRegion } from "./api.js";
import { attachCombobox } from "./combobox.js";
import { clear, el, fmt, stockLink, table } from "./dom.js";
import {
  CALIBRATION_LABELS,
  CANDIDATE_STATUS_LABELS,
  DECISION_KIND_LABELS,
  DRAFT_STATE_LABELS,
  FLAG_LABELS,
  INDUSTRY_GROUP_LABELS,
  OUTCOME_LABELS,
  QUALITY_LABELS,
  RULE_STATUS_LABELS,
  VERIFICATION_STATUS_LABELS,
  VERIFICATION_TRIGGER_LABELS,
  ZONE_LABELS,
  describeIssues,
  factorLabel,
  formatCondition,
  formatDate,
  formatDateTime,
  formatFactorValue,
  formatReason,
  formatParamValue,
  inRange,
  label,
  paramLabel,
  parseParamName,
  parseRationale,
  formatThreshold,
  PERIOD_KIND_LABELS,
  PROVIDER_LABELS,
  SOURCE_VERIFICATION_LABELS,
  TEMPLATE_LABELS,
  templateLabel,
} from "./format.js";
import { humanizeText } from "./research_format.js";
import { loadSparkBars } from "./market.js";
import { mountPriceChart } from "./pricechart.js";
import { aiStamp, fillSparklines, kpi, legend, scale, timeline } from "./ui.js";

import {
  ZONE_ORDER,
  calibrationBadge,
  chip,
  isNumber,
  primaryTemplate,
  section,
  sectionReady,
  templateChip,
  zoneDetectionLine,
} from "./candidate_shared.js";

// 最新筛选中各因子的实际值（用于在区间条件旁标出"本股当前"）。
export function currentFactorValues(candidate) {
  const map = new Map();
  for (const result of candidate.screen_results || []) {
    for (const reason of result.reasons || []) {
      if (isNumber(reason.actual) && !map.has(reason.factor)) map.set(reason.factor, reason);
    }
  }
  return map;
}

export function conditionLeaf(condition, params, units, current) {
  const { text, params: names } = formatCondition(condition, params, units);
  const leaf = el("li", {}, text);
  const now = current && current.get(condition.factor);
  if (now) leaf.appendChild(el("span", { class: "cond-now" }, `本股 ${formatFactorValue(now.actual, now.unit)}`));
  if (names.length) leaf.title = `参数：${names.map(paramLabel).join("、")}`;
  return leaf;
}

export function conditionTree(group, params, units, current) {
  const list = el("ul", { class: "condition-tree" });
  if (!group || typeof group !== "object") return list;
  if (group.factor) {
    list.appendChild(conditionLeaf(group, params, units, current));
    return list;
  }
  const mode = Array.isArray(group.all) ? "all" : "any";
  const children = group[mode] || [];
  const item = el("li");
  item.appendChild(el("span", { class: "muted" }, mode === "all" ? "全部满足：" : "任一满足："));
  const inner = el("ul", { class: "condition-tree" });
  for (const child of children) {
    if (child && child.factor) {
      inner.appendChild(conditionLeaf(child, params, units, current));
    } else {
      for (const nested of conditionTree(child, params, units, current).children) inner.appendChild(nested);
    }
  }
  item.appendChild(inner);
  list.appendChild(item);
  return list;
}

export function paramNames(rule) {
  return [
    ...new Set([
      ...Object.keys(rule.template_params || {}),
      ...Object.keys(rule.param_ranges || {}),
      ...Object.keys(rule.params || {}),
    ]),
  ].sort();
}

export function editedParams(rule) {
  const changes = {};
  for (const input of document.querySelectorAll("input[data-param]")) {
    const name = input.dataset.param;
    const value = Number(input.value);
    if (input.value.trim() === "" || !Number.isFinite(value)) continue;
    if (value !== (rule.effective_params || {})[name]) changes[name] = value;
  }
  return changes;
}

export function invalidParams() {
  return [...document.querySelectorAll("input[data-param]")].filter(
    (input) => input.getAttribute("aria-invalid") === "true"
  );
}

// 参数行：中文名 + 标识符；刻度条（允许范围 / 默认 / 当前 / 本股值）；可编辑输入；起草理由。
export function paramRow(rule, name, rationale, editable, current) {
  const defaults = rule.template_params || {};
  const effective = (rule.effective_params || {})[name];
  const range = (rule.param_ranges || {})[name];
  const units = rule.factor_units || {};
  const show = (value) => formatParamValue(name, value, units);
  const overridden =
    Object.prototype.hasOwnProperty.call(rule.params || {}, name) && rule.params[name] !== defaults[name];
  const byAi = String(rule.drafted_by || "").startsWith("llm:");
  const row = el("li", { class: `param${overridden ? " changed" : ""}`, "data-param-row": name });

  const head = el("div");
  head.appendChild(el("span", { class: "param-name" }, paramLabel(name)));
  head.appendChild(el("code", { class: "param-id" }, name));
  const vals = el("div", { class: "param-vals" });
  if (overridden) {
    vals.appendChild(document.createTextNode(`默认 ${show(defaults[name])} → `));
    vals.appendChild(el("span", { class: "new" }, `${byAi ? "AI 建议 " : ""}${show(effective)}`));
  } else {
    vals.appendChild(document.createTextNode(`默认 ${show(defaults[name])}`));
  }
  if (range) vals.appendChild(document.createTextNode(` · 允许 ${show(range.min)} – ${show(range.max)}`));
  head.appendChild(vals);
  row.appendChild(head);

  const parsed = parseParamName(name);
  const stock = parsed && current ? current.get(parsed.factor) : null;
  if (range && isNumber(range.min) && isNumber(range.max)) {
    const holder = el("div", { class: "param-scale" });
    const markers = [];
    if (stock && stock.actual >= range.min && stock.actual <= range.max) {
      markers.push({ at: stock.actual, cls: "s-marker-hollow", title: `本股 ${formatFactorValue(stock.actual, stock.unit)}` });
    }
    if (isNumber(effective)) {
      markers.push({ at: effective, cls: overridden && byAi ? "s-marker-ai" : "s-marker", title: `当前 ${show(effective)}` });
    }
    holder.appendChild(
      scale({
        domain: [range.min, range.max],
        defaults: isNumber(defaults[name]) ? [defaults[name]] : [],
        markers,
        ticks: [
          { at: range.min, label: show(range.min) },
          { at: range.max, label: show(range.max) },
        ],
        compact: true,
      })
    );
    row.appendChild(holder);
  }

  const edit = el("div", { class: "param-edit" });
  const hint = el("span", { class: "range-hint", id: `param-hint-${name}`, "aria-live": "polite" });
  if (editable) {
    const input = el("input", {
      type: "number",
      step: "any",
      inputmode: "decimal",
      value: effective,
      min: range ? range.min : undefined,
      max: range ? range.max : undefined,
      "data-param": name,
      "aria-label": `参数 ${paramLabel(name)}`,
      "aria-describedby": hint.id,
    });
    input.addEventListener("input", () => {
      const value = Number(input.value);
      const ok = input.value.trim() !== "" && (!range || inRange(value, range));
      input.setAttribute("aria-invalid", ok ? "false" : "true");
      hint.textContent = ok
        ? show(value)
        : range
          ? `超出允许范围 ${show(range.min)} – ${show(range.max)}`
          : "请输入数字";
    });
    edit.appendChild(input);
    hint.textContent = show(effective);
  } else {
    hint.textContent = show(effective);
  }
  edit.appendChild(hint);
  row.appendChild(edit);

  const entry = rationale.params[name];
  if (entry && typeof entry === "object") {
    const reason = el("div", { class: "param-reason" });
    reason.appendChild(el("b", {}, entry.choice === "adjusted" ? "调整：" : "保持默认："));
    reason.appendChild(document.createTextNode(humanizeText(fmt(entry.reason))));
    const cited = Array.isArray(entry.cited_factors) ? entry.cited_factors : [];
    if (cited.length) {
      const chips = el("div");
      for (const factor of cited) chips.appendChild(chip(factorLabel(factor), "chip-factor"));
      reason.appendChild(chips);
    }
    row.appendChild(reason);
  } else if (overridden) {
    row.appendChild(el("div", { class: "param-reason" }, "（无逐项理由）"));
  }
  return row;
}

export function zoneParams(node) {
  if (!node || typeof node !== "object") return [];
  if (node.factor) {
    const values = Array.isArray(node.value) ? node.value : [node.value];
    return values
      .map((value) => (typeof value === "string" && value.startsWith("$") ? value.slice(1) : null))
      .filter(Boolean);
  }
  return [...(node.all || []), ...(node.any || [])].flatMap(zoneParams);
}

export function paramTable(rule, names, rationale, editable, current) {
  const list = el("ul", { class: "param-list" });
  for (const name of names) list.appendChild(paramRow(rule, name, rationale, editable, current));
  return list;
}

export function renderRule(candidate, hooks) {
  const node = section("candidate-rule");
  if (!node) return;
  const rule = candidate.draft_rule_detail;
  const box = el("div");
  if (!rule) {
    const messages = {
      none: "尚无规则草稿（待起草）。",
      pending: "规则草稿起草中，完成后刷新本页即可审阅。",
      failed: "起草失败：草稿不可用。",
    };
    box.appendChild(
      el(
        "p",
        { class: `notice ${candidate.draft_state === "failed" ? "notice-error" : ""}`.trim() },
        messages[candidate.draft_state] || `草稿状态：${label(DRAFT_STATE_LABELS, candidate.draft_state)}`
      )
    );
    const template = primaryTemplate(candidate);
    if (candidate.status === "open" && ["none", "failed"].includes(candidate.draft_state) && template) {
      box.appendChild(hooks.redraftButton(candidate, template));
    }
    sectionReady(node, box);
    return;
  }

  const editable = candidate.status === "open" && rule.status === "draft";
  const current = currentFactorValues(candidate);
  const byAi = String(rule.drafted_by || "").startsWith("llm:");
  const heading = el("div", { class: "rule-head" });
  if (byAi) heading.appendChild(aiStamp(`AI 草稿 · ${rule.drafted_by.slice(4)} · ${formatDateTime(rule.created_at)}`));
  else heading.appendChild(chip(`起草：${fmt(rule.drafted_by)} · ${formatDateTime(rule.created_at)}`));
  heading.appendChild(chip(`规则 #${rule.rule_instance_id} v${rule.version}`));
  heading.appendChild(chip(label(RULE_STATUS_LABELS, rule.status), `rule-${rule.status}`));
  heading.appendChild(templateChip(rule.template_ref));
  heading.appendChild(calibrationBadge(rule.calibration_status));
  if (candidate.status === "open" && rule.status === "draft") {
    const redraft = hooks.redraftButton(candidate, rule.template_ref);
    redraft.classList.add("redraft");
    heading.appendChild(redraft);
  }
  box.appendChild(heading);
  box.appendChild(
    el("p", { class: "faint" }, `范围：${rule.scope === "candidate" ? "候选" : rule.scope === "holding" ? "持仓" : fmt(rule.scope)} · 区间条件按生效参数显示；灰字为本股最新筛选值。`)
  );
  const detection = zoneDetectionLine(candidate.zone_events);
  if (detection) box.appendChild(detection);

  const zones = rule.zones && typeof rule.zones === "object" ? rule.zones : {};
  const names = Object.keys(zones).sort(
    (a, b) => (ZONE_ORDER.indexOf(a) + 1 || 99) - (ZONE_ORDER.indexOf(b) + 1 || 99)
  );
  if (names.length) {
    const summary = el("div", { class: "zone-summary" });
    for (const zone of names) {
      const block = el("div", { class: `zone-block zone-${zone}` });
      block.appendChild(el("h4", { title: zone }, label(ZONE_LABELS, zone)));
      block.appendChild(conditionTree(zones[zone], rule.effective_params || {}, rule.factor_units || {}, current));
      summary.appendChild(block);
    }
    box.appendChild(summary);
  }

  const rationale = parseRationale(rule.draft_rationale);
  const params = paramNames(rule);
  // 先列本规则区间实际引用、被覆盖或有起草理由的参数；其余模板参数（筛选 / 持仓区间用）收起。
  const used = new Set(names.flatMap((zone) => zoneParams(zones[zone])));
  const primary = params.filter(
    (name) =>
      used.has(name) ||
      Object.prototype.hasOwnProperty.call(rule.params || {}, name) ||
      Object.prototype.hasOwnProperty.call(rationale.params, name)
  );
  // 被覆盖的参数排在最前。
  const changedFirst = (a, b) => {
    const changed = (name) =>
      Object.prototype.hasOwnProperty.call(rule.params || {}, name) &&
      rule.params[name] !== (rule.template_params || {})[name];
    return Number(changed(b)) - Number(changed(a));
  };
  primary.sort(changedFirst);
  const secondary = params.filter((name) => !primary.includes(name));
  if (primary.length) {
    box.appendChild(el("h3", {}, "参数"));
    box.appendChild(
      legend([
        ["lg-def", "模板默认"],
        ["lg-cur", "当前值"],
        ["lg-ai", "AI 建议值"],
        ["lg-stock", "本股最新值"],
      ])
    );
    box.appendChild(paramTable(rule, primary, rationale, editable, current));
  }
  if (secondary.length) {
    const more = el("details", { class: "more-params" });
    more.appendChild(el("summary", {}, `其他模板参数（${secondary.length}，本规则的候选区间未引用）`));
    more.appendChild(paramTable(rule, secondary, rationale, editable, current));
    box.appendChild(more);
  }
  if (editable && params.length) {
    const row = el("div", { class: "btn-row" });
    const save = el("button", { type: "button", class: "secondary", "data-action": "save-params" }, "保存参数修改");
    save.addEventListener("click", () => hooks.saveParams(candidate, rule, save));
    row.appendChild(save);
    box.appendChild(row);
  }
  box.appendChild(el("div", { id: "rule-issues", class: "issues", role: "alert" }));

  if (rationale.overall || rationale.text || rationale.watch.length) {
    const notes = el("div", { class: "draft-notes" });
    if (rationale.overall || rationale.text) {
      notes.appendChild(el("h3", {}, "整体起草思路"));
      notes.appendChild(el("p", {}, humanizeText(rationale.overall || rationale.text)));
    }
    if (rationale.watch.length) {
      notes.appendChild(el("h3", {}, "后续复核点"));
      const list = el("ul");
      for (const point of rationale.watch) list.appendChild(el("li", {}, humanizeText(point)));
      notes.appendChild(list);
    }
    box.appendChild(notes);
  }
  sectionReady(node, box);
}
