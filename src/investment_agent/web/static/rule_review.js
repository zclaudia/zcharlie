// 规则审阅组件（观察池 / 候选 / 持仓共用）：区间条件、参数编辑、区间事件。
// 三处入口（/watchlist/{id}/rule/*、/portfolio/rule/*）复用同一套渲染，避免
// 规则卡片在不同页面上漂移；区间名是固定枚举，文案只用中性区间名。
import { renderRegion } from "./api.js";
import { el } from "./dom.js";
import { CALIBRATION_LABELS, FACTOR_LABELS, ZONE_LABELS, formatCondition, label } from "./format.js";

export function ruleFeedback(container) {
  const node = el("div", { class: "rule-feedback", "aria-live": "polite" });
  container.appendChild(node);
  return {
    error(message) {
      renderRegion(node, "error", message);
    },
    note(message) {
      renderRegion(node, "ready", el("p", { class: "small" }, message));
    },
  };
}

export function conditionLines(group, params, units) {
  const list = el("ul", { class: "tpl-conds" });
  const mode = group && group.all ? "all" : "any";
  for (const child of (group && group[mode]) || []) {
    if (child && typeof child.factor === "string") {
      const formatted = formatCondition(child, params, units);
      const li = el("li", { class: "tpl-cond" });
      li.appendChild(el("span", { class: "tpl-cond-text" }, formatted.text));
      for (const name of formatted.params) {
        li.appendChild(el("code", { class: "tpl-param-ref", title: "参数名，可在下方调整" }, name));
      }
      list.appendChild(li);
    } else if (child) {
      const nested = el("li", { class: "tpl-cond" });
      nested.appendChild(
        el("span", { class: "faint small" }, child.all ? "以下全部满足：" : "以下满足其一：")
      );
      nested.appendChild(conditionLines(child, params, units));
      list.appendChild(nested);
    }
  }
  return list;
}

// 规则卡片里的区间区块：生效参数 + 区间条件（模板默认已由服务端合并）。
export function zonesBlock(block) {
  const definition = block.definition || {};
  const zones = block.zones || definition.zones || {};
  const params = block.params || {};
  const units = block.factor_units || {};
  const box = el("div", { class: "rule-zones" });
  const names = Object.keys(zones).sort();
  if (!names.length) {
    box.appendChild(el("p", { class: "muted" }, "没有区间条件。"));
    return box;
  }
  for (const name of names) {
    const zone = el("div", { class: "tpl-zone" });
    zone.appendChild(el("h4", {}, label(ZONE_LABELS, name)));
    zone.appendChild(conditionLines(zones[name], params, units));
    box.appendChild(zone);
  }
  return box;
}

// 参数编辑器：保存后由调用方生成新的草稿版本（不改写已生效的版本）。
export function paramEditor({ rule, title, onSave }) {
  const definition = rule.definition || {};
  const ranges = rule.param_ranges || definition.param_ranges || {};
  const params = rule.params || {};
  const entries = Object.entries(params);
  if (!entries.length) return null;
  const box = el("details", { class: "rule-params" });
  box.appendChild(
    el("summary", {}, title || `调整参数（${entries.length} 项，保存后生成新的草稿版本）`)
  );
  const inputs = {};
  for (const [name, value] of entries) {
    const row = el("label", { class: "rule-param-row" });
    row.appendChild(el("span", { class: "rule-param-name" }, name));
    const input = el("input", {
      type: "number",
      step: "any",
      value: String(value),
      "aria-label": name,
    });
    if (ranges[name]) {
      input.min = String(ranges[name].min);
      input.max = String(ranges[name].max);
      row.appendChild(
        el("span", { class: "faint small" }, `允许范围 ${ranges[name].min}–${ranges[name].max}`)
      );
    }
    row.appendChild(input);
    inputs[name] = input;
    box.appendChild(row);
  }
  const save = el("button", { type: "button", class: "secondary" }, "保存为新草稿版本");
  save.addEventListener("click", () => {
    const changed = {};
    for (const [name, input] of Object.entries(inputs)) {
      const value = Number(input.value);
      if (Number.isFinite(value) && value !== params[name]) changed[name] = value;
    }
    if (!Object.keys(changed).length) {
      onSave.feedback.note("没有参数变化。");
      return;
    }
    onSave.submit(save, changed);
  });
  box.appendChild(save);
  return box;
}

// 每日区间检测（WP8）：当前所处区间、进入日期与最近一次进入事件引用的规则版本。
export function zoneEventLine(events) {
  const latest = events && events.latest;
  const interval = (events && events.interval) || {};
  if (!latest && !interval.inside) {
    return el("span", { class: "cell-sub" }, "区间：未触发");
  }
  const zone = label(ZONE_LABELS, interval.zone || (latest && latest.zone));
  const since = interval.entered_as_of || (latest && latest.as_of);
  const version = latest ? ` · 规则 v${latest.rule_version}` : "";
  const text = interval.inside
    ? `区间：${zone} 触发中（${since}）${version}`
    : `区间：${zone} 已离开（${since}）${version}`;
  return el("span", { class: interval.inside ? "cell-sub badge-info" : "cell-sub" }, text);
}

// 触发条件与校准状态（funnel 9：每条区间通知都带规则引用与实际值）。
export function eventsSummary(event) {
  const parts = (event.conditions_met || []).map(
    (reason) =>
      `${label(FACTOR_LABELS, reason.factor)} ${reason.op} ${reason.threshold}（实际 ${reason.actual}）`
  );
  const calibration = label(CALIBRATION_LABELS, event.calibration_status);
  return `触发条件：${parts.length ? parts.join("；") : "—"} · 校准状态：${calibration}`;
}

export const HOLDING_ZONE_ORDER = ["add", "trim", "exit_review"];
