// 多视角评审卡片（docs/product/multi-model-review-design.md §7）。
// 只消费服务端结构：立场分布、维度矩阵、席位意见与校验标注。
// 评审意见不是 Agent 正式意见，卡片固定标注、使用独立配色。
// 所有模型文本只经 textContent / el() 写入，绝不拼接 HTML。

import { ApiError, get, post } from "./api.js";
import { el, fmt } from "./dom.js";
import { formatDateTime } from "./format.js";

export const STANCE_LABELS = Object.freeze({
  positive: "偏正面",
  neutral: "中性",
  negative: "偏负面",
  insufficient: "证据不足",
});

export const TRADE_VIEW_LABELS = Object.freeze({
  watch_buy: "关注买入",
  hold_watch: "持有观察",
  consider_reduce: "考虑减持",
  avoid: "回避",
  no_view: "无看法",
});

export const CONFIDENCE_LABELS = Object.freeze({ low: "低", medium: "中", high: "高" });

export const DIMENSION_LABELS = Object.freeze({
  revenue_growth: "营收增长",
  margin: "利润率",
  cash_flow_quality: "现金流质量",
  balance_sheet: "资产负债",
  valuation: "估值水平",
  industry_structure: "行业格局",
  cycle_position: "周期位置",
  governance: "治理与股东",
  policy_regulation: "政策监管",
  data_quality: "数据质量",
  other: "其他",
});

export const DIVERGENCE_LABELS = Object.freeze({ low: "低", medium: "中", high: "大" });

export const TRIGGER_LABELS = Object.freeze({
  manual: "手动发起",
  change: "数据变化触发",
  scheduled: "定期批量",
});

export const ROUND_STATUS_LABELS = Object.freeze({
  queued: "排队中",
  running: "进行中",
  completed: "已完成",
  failed: "失败",
});

export const SEAT_STATUS_LABELS = Object.freeze({
  queued: "等待中",
  running: "分析中",
  succeeded: "已完成",
  partial: "部分完成",
  failed: "失败",
});

export const CELL_LABELS = Object.freeze({
  positive: "利好",
  negative: "利空",
  mixed: "分歧",
  absent: "—",
});

export const REVIEW_DISCLAIMER = "评审意见 · 未经发布校验 · 不是 Agent 正式意见";

const STANCE_ORDER = ["positive", "neutral", "negative", "insufficient"];
const TERMINAL_ROUNDS = new Set(["completed", "failed"]);
const TERMINAL_SEATS = new Set(["succeeded", "partial", "failed"]);
const POLL_INTERVAL_MS = 3000;
const POLL_TIMEOUT_MS = 5 * 60 * 1000;
const MOBILE_QUERY = "(max-width: 700px)";
const SEAT_OPINION_SECTIONS = [
  ["bull_points", "看多要点"],
  ["bear_points", "看空要点"],
  ["falsifiers", "证伪条件"],
  ["open_questions", "需要补查"],
];

// ---- 纯函数（可在 node 中单独验证） ---------------------------------------

export function consensusSegments(distribution) {
  const counts = distribution && typeof distribution === "object" ? distribution : {};
  const total = STANCE_ORDER.reduce((sum, key) => sum + (Number(counts[key]) || 0), 0);
  return STANCE_ORDER.filter((key) => Number(counts[key]) > 0).map((key) => ({
    key,
    label: STANCE_LABELS[key],
    count: Number(counts[key]),
    share: total ? Number(counts[key]) / total : 0,
  }));
}

export function consensusText(distribution) {
  const parts = consensusSegments(distribution);
  return parts.length ? parts.map((part) => `${part.count} ${part.label}`).join(" / ") : "暂无有效席位";
}

export function matrixCellLabel(cell) {
  return CELL_LABELS[cell] || "—";
}

function _isDigit(character) {
  return character >= "0" && character <= "9";
}

function _occurrences(text, token) {
  if (!token) return [];
  const found = [];
  let from = 0;
  for (;;) {
    const index = text.indexOf(token, from);
    if (index < 0) break;
    const before = text[index - 1];
    const after = text[index + token.length];
    const beforeOk = !before || !_isDigit(before);
    const afterOk = !after || !_isDigit(after);
    if (beforeOk && afterOk) found.push(index);
    from = index + token.length;
  }
  return found;
}

// 把一段模型文本切成普通片段与数字标注片段；未核验数字灰显，已定位数字
// 带来源提示。纯函数便于测试，渲染方只负责创建节点。
export function annotatedSegments(text, path, annotations) {
  if (typeof text !== "string" || !text) return [];
  const numbers = annotations && Array.isArray(annotations.numbers) ? annotations.numbers : [];
  const matches = [];
  for (const annotation of numbers) {
    if (!annotation || annotation.path !== path || typeof annotation.value !== "string") continue;
    for (const start of _occurrences(text, annotation.value)) {
      matches.push({
        start,
        end: start + annotation.value.length,
        kind: annotation.status === "sourced" ? "sourced" : "unverified",
        title:
          annotation.status === "sourced" && annotation.source_ref
            ? `来源：${annotation.source_ref}`
            : "未核验",
      });
    }
  }
  matches.sort((left, right) => left.start - right.start || right.end - left.end);
  const segments = [];
  let cursor = 0;
  for (const match of matches) {
    if (match.start < cursor) continue;
    if (match.start > cursor) {
      segments.push({ text: text.slice(cursor, match.start), kind: "plain", title: "" });
    }
    segments.push({ text: text.slice(match.start, match.end), kind: match.kind, title: match.title });
    cursor = match.end;
  }
  if (cursor < text.length) segments.push({ text: text.slice(cursor), kind: "plain", title: "" });
  return segments;
}

// ---- 渲染工具 -------------------------------------------------------------

function messageOf(error) {
  if (error instanceof ApiError) {
    const detail = error.detail;
    if (detail && typeof detail === "object" && typeof detail.message === "string") return detail.message;
    return error.message;
  }
  return "加载失败，请重试。";
}

function shortDate(value) {
  const full = formatDateTime(value);
  return /^\d{4}-/.test(full) ? full.slice(5) : full;
}

function appendAnnotated(parent, text, path, annotations) {
  for (const segment of annotatedSegments(text, path, annotations)) {
    if (segment.kind === "plain") {
      parent.appendChild(document.createTextNode(segment.text));
      continue;
    }
    const node = el(
      "span",
      { class: `rp-num rp-num-${segment.kind}`, title: segment.title || undefined },
      segment.text
    );
    parent.appendChild(node);
    if (segment.kind === "unverified") {
      parent.appendChild(el("span", { class: "rp-num-flag" }, "未核验"));
    }
  }
}

function annotatedBlock(tag, text, path, annotations, className) {
  const node = el(tag, { class: className || undefined });
  appendAnnotated(node, text, path, annotations);
  return node;
}

function invalidRefMap(basePath, annotations) {
  const map = new Map();
  const refs = annotations && Array.isArray(annotations.invalid_refs) ? annotations.invalid_refs : [];
  const prefix = `${basePath}.evidence_refs[`;
  for (const item of refs) {
    if (item && typeof item.path === "string" && item.path.startsWith(prefix)) {
      map.set(item.path, item.reason || "该引用不在本轮实际读取的材料中");
    }
  }
  return map;
}

function appendRefs(parent, basePath, refs, annotations) {
  if (!Array.isArray(refs) || !refs.length) return;
  const invalid = invalidRefMap(basePath, annotations);
  const wrap = el("span", { class: "rp-refs" });
  refs.forEach((ref, index) => {
    const reason = invalid.get(`${basePath}.evidence_refs[${index}]`);
    wrap.appendChild(
      el(
        "span",
        {
          class: reason ? "rp-ref rp-ref-invalid" : "rp-ref",
          title: reason || String(ref),
        },
        String(ref)
      )
    );
  });
  parent.appendChild(wrap);
}

function statusBadge(text, kind) {
  return el("span", { class: `rp-status rp-status-${kind}` }, text);
}

function seatLabel(seat) {
  return seat.lens_name || seat.lens_id;
}

function metaLine(detail) {
  return [
    `本轮 ${formatDateTime(detail.created_at)}`,
    `证据截止 ${formatDateTime(detail.evidence_cutoff)}`,
    TRIGGER_LABELS[detail.trigger] || detail.trigger,
    ROUND_STATUS_LABELS[detail.status] || detail.status,
  ].join(" · ");
}

function consensusBlock(detail) {
  const comparison = detail.comparison;
  const wrap = el("div", { class: "rp-consensus-wrap" });
  const head = el("div", { class: "rp-consensus-head" });
  head.appendChild(el("h3", {}, "共识"));
  if (comparison) {
    const level = comparison.divergence_level || "low";
    head.appendChild(
      el("span", { class: `rp-divergence rp-divergence-${level}` }, `分歧${DIVERGENCE_LABELS[level] || level}`)
    );
  }
  wrap.appendChild(head);
  const distribution = comparison ? comparison.stance_distribution : null;
  const bar = el("div", { class: "rp-consensus", role: "img", "aria-label": consensusText(distribution) });
  const segments = consensusSegments(distribution);
  if (!segments.length) {
    bar.appendChild(el("span", { class: "rp-consensus-none" }, "暂无有效席位"));
  } else {
    for (const segment of segments) {
      const node = el("span", {
        class: `rp-consensus-seg rp-stance-${segment.key}`,
        title: `${segment.count} 席 ${segment.label}`,
      });
      node.style.flexGrow = String(segment.count);
      bar.appendChild(node);
    }
  }
  wrap.appendChild(bar);
  wrap.appendChild(
    el("p", { class: "rp-consensus-text" }, consensusText(distribution))
  );
  if (comparison && comparison.failed_seats && comparison.failed_seats.length) {
    wrap.appendChild(
      el("p", { class: "rp-note" }, `失败席位：${comparison.failed_seats.join("、")}（不计入统计）`)
    );
  }
  return wrap;
}

function cellDetailPanel(detail) {
  const panel = el("div", { class: "rp-cell-detail", hidden: "" });
  return panel;
}

function showCellDetail(panel, detail, dimension, seat) {
  panel.replaceChildren();
  panel.hidden = false;
  const head = el("div", { class: "rp-cell-detail-head" });
  head.appendChild(el("h4", {}, `${DIMENSION_LABELS[dimension] || dimension} · ${seatLabel(seat)}`));
  if (detail.comparison && detail.comparison.divergent_materials && detail.comparison.divergent_materials[dimension]) {
    head.appendChild(el("span", { class: "rp-materials-warn" }, "双方看过的材料不同"));
  }
  panel.appendChild(head);
  const assumptions =
    seat.opinion && Array.isArray(seat.opinion.assumptions)
      ? seat.opinion.assumptions.filter((item) => item.dimension === dimension)
      : [];
  if (!seat.opinion) {
    panel.appendChild(el("p", { class: "rp-note" }, "该席位本轮未产出意见。"));
    return;
  }
  if (!assumptions.length) {
    panel.appendChild(el("p", { class: "rp-note" }, "该席位未提及这个维度。"));
    return;
  }
  seat.opinion.assumptions.forEach((item, index) => {
    if (item.dimension !== dimension) return;
    const row = el("div", { class: "rp-cell-assumption" });
    row.appendChild(
      el(
        "span",
        { class: `rp-direction rp-direction-${item.direction}` },
        item.direction === "positive" ? "利好" : "利空"
      )
    );
    const body = el("div");
    body.appendChild(
      annotatedBlock("p", item.text, `assumptions[${index}].text`, seat.annotations)
    );
    appendRefs(body, `assumptions[${index}]`, item.evidence_refs, seat.annotations);
    row.appendChild(body);
    panel.appendChild(row);
  });
}

function matrixBlock(detail) {
  const comparison = detail.comparison;
  const wrap = el("div", { class: "rp-matrix-wrap" });
  wrap.appendChild(el("h3", {}, "维度分歧矩阵"));
  const matrix = comparison.dimension_matrix || {};
  const dimensions = Object.keys(matrix);
  if (!dimensions.length) {
    wrap.appendChild(el("p", { class: "rp-note" }, "各席位尚未给出可比对的维度假设。"));
    return wrap;
  }
  const divergent = new Set(comparison.divergent_dimensions || []);
  const panel = cellDetailPanel(detail);
  const table = el("table", { class: "rp-matrix" });
  const head = el("tr");
  head.appendChild(el("th", { scope: "col" }, "维度"));
  for (const seat of detail.seats) {
    const th = el("th", { scope: "col" });
    th.appendChild(el("span", {}, seatLabel(seat)));
    th.appendChild(el("small", {}, seat.lens_ref || ""));
    head.appendChild(th);
  }
  table.appendChild(el("thead")).appendChild(head);
  const body = el("tbody");
  for (const dimension of dimensions) {
    const row = el("tr", { class: divergent.has(dimension) ? "rp-divergent-row" : undefined });
    const label = el("th", { scope: "row" });
    label.appendChild(
      el("span", {}, DIMENSION_LABELS[dimension] || dimension)
    );
    if (comparison.divergent_materials && comparison.divergent_materials[dimension]) {
      label.appendChild(el("span", { class: "rp-materials-warn" }, "双方看过的材料不同"));
    }
    row.appendChild(label);
    for (const seat of detail.seats) {
      const cell = matrix[dimension] && matrix[dimension][seat.seat_id] ? matrix[dimension][seat.seat_id] : "absent";
      const td = el("td");
      const button = el(
        "button",
        {
          type: "button",
          class: `rp-cell rp-cell-${cell}`,
          "aria-label": `${seatLabel(seat)} 在${DIMENSION_LABELS[dimension] || dimension}的假设：${matrixCellLabel(cell)}`,
        },
        matrixCellLabel(cell)
      );
      button.addEventListener("click", () => {
        const isOpen = button.getAttribute("aria-expanded") === "true";
        for (const other of table.querySelectorAll(".rp-cell[aria-expanded='true']")) {
          other.setAttribute("aria-expanded", "false");
        }
        if (isOpen) {
          panel.hidden = true;
          return;
        }
        button.setAttribute("aria-expanded", "true");
        showCellDetail(panel, detail, dimension, seat);
      });
      td.appendChild(button);
      row.appendChild(td);
    }
    body.appendChild(row);
  }
  table.appendChild(body);
  wrap.appendChild(table);
  wrap.appendChild(panel);
  return wrap;
}

function seatCard(seat) {
  const card = el("article", { class: `rp-seat rp-seat-${seat.status}` });
  const head = el("div", { class: "rp-seat-head" });
  const title = el("div", { class: "rp-seat-title" });
  title.appendChild(el("h3", {}, seatLabel(seat)));
  title.appendChild(el("small", {}, `${seat.lens_ref || seat.lens_id} · ${seat.model_ref}`));
  head.appendChild(title);
  head.appendChild(statusBadge(SEAT_STATUS_LABELS[seat.status] || seat.status, seat.status));
  card.appendChild(head);

  if (seat.error) {
    card.appendChild(el("p", { class: "rp-seat-error" }, seat.error));
  }
  const opinion = seat.opinion;
  if (!opinion) {
    const reason =
      seat.status === "failed"
        ? "本席位没有产出意见。"
        : seat.status === "queued"
          ? "等待运行…"
          : "正在分析…";
    card.appendChild(el("p", { class: "rp-note" }, reason));
    return card;
  }

  const labels = el("div", { class: "rp-seat-labels" });
  const stance = opinion.company_stance;
  labels.appendChild(
    el("span", { class: `rp-chip rp-stance-${stance}` }, STANCE_LABELS[stance] || stance)
  );
  const trade = opinion.trade_view;
  labels.appendChild(el("span", { class: "rp-chip" }, TRADE_VIEW_LABELS[trade] || trade));
  const confidence = opinion.confidence;
  labels.appendChild(
    el("span", { class: "rp-chip" }, `信心 ${CONFIDENCE_LABELS[confidence] || confidence}`)
  );
  card.appendChild(labels);

  if (opinion.summary) {
    card.appendChild(annotatedBlock("p", opinion.summary, "summary", seat.annotations, "rp-summary"));
  }

  if (Array.isArray(opinion.trade_conditions) && opinion.trade_conditions.length) {
    const section = sectionBlock("买卖条件");
    const list = el("ul", { class: "rp-list" });
    opinion.trade_conditions.forEach((condition, index) => {
      const item = el("li");
      const text = annotatedBlock(
        "span",
        condition.text || "",
        `trade_conditions[${index}].text`,
        seat.annotations
      );
      item.appendChild(text);
      if (condition.metric && condition.threshold !== null && condition.threshold !== undefined) {
        item.appendChild(
          el("span", { class: "rp-threshold" }, ` ${condition.metric} ${fmt(condition.threshold)}`)
        );
      }
      const basis = condition.basis_ref
        ? `依据 ${condition.basis_ref}`
        : condition.is_assumption
          ? "依据：假设"
          : "阈值无证据依据";
      item.appendChild(el("span", { class: "rp-basis" }, `（${basis}）`));
      list.appendChild(item);
    });
    section.appendChild(list);
    card.appendChild(section);
  }

  for (const [key, heading] of SEAT_OPINION_SECTIONS) {
    const items = opinion[key];
    if (!Array.isArray(items) || !items.length) continue;
    const section = sectionBlock(heading);
    const list = el("ul", { class: "rp-list" });
    items.forEach((item, index) => {
      const li = el("li");
      if (key === "open_questions") {
        li.appendChild(
          annotatedBlock("span", String(item), `open_questions[${index}]`, seat.annotations)
        );
      } else {
        li.appendChild(
          annotatedBlock("span", item.text || "", `${key}[${index}].text`, seat.annotations)
        );
        if (item.dimension) {
          li.appendChild(
            el("span", { class: "rp-dim-tag" }, DIMENSION_LABELS[item.dimension] || item.dimension)
          );
        }
        appendRefs(li, `${key}[${index}]`, item.evidence_refs, seat.annotations);
      }
      list.appendChild(li);
    });
    section.appendChild(list);
    card.appendChild(section);
  }

  const missing = seat.annotations && Array.isArray(seat.annotations.missing_required_dimensions)
    ? seat.annotations.missing_required_dimensions
    : [];
  if (missing.length) {
    card.appendChild(
      el(
        "p",
        { class: "rp-note rp-note-warn" },
        `缺少视角要求的维度：${missing.map((item) => DIMENSION_LABELS[item] || item).join("、")}`
      )
    );
  }
  return card;
}

function sectionBlock(title) {
  const section = el("div", { class: "rp-seat-section" });
  section.appendChild(el("h4", {}, title));
  return section;
}

function seatsBlock(detail, state) {
  const wrap = el("div", { class: "rp-seats-wrap" });
  wrap.appendChild(el("h3", {}, "席位意见"));
  const narrow = typeof globalThis.matchMedia === "function" && globalThis.matchMedia(MOBILE_QUERY).matches;
  if (narrow) {
    const tabs = el("div", { class: "rp-seat-tabs", role: "tablist", "aria-label": "席位切换" });
    const panels = el("div", { class: "rp-seat-panels" });
    detail.seats.forEach((seat, index) => {
      const active = index === state.selectedSeat;
      const tab = el(
        "button",
        {
          type: "button",
          role: "tab",
          class: active ? "rp-seat-tab on" : "rp-seat-tab",
          "aria-selected": active ? "true" : "false",
        },
        seatLabel(seat)
      );
      tab.addEventListener("click", () => {
        state.selectedSeat = index;
        rerender();
      });
      tabs.appendChild(tab);
      const card = seatCard(seat);
      card.hidden = !active;
      panels.appendChild(card);
    });
    wrap.appendChild(tabs);
    wrap.appendChild(panels);
    return wrap;
  }
  const row = el("div", { class: "rp-seats" });
  for (const seat of detail.seats) row.appendChild(seatCard(seat));
  wrap.appendChild(row);
  return wrap;
}

function falsifiersBlock(comparison) {
  if (!comparison || !Array.isArray(comparison.falsifiers_by_dimension) || !comparison.falsifiers_by_dimension.length) {
    return null;
  }
  const wrap = el("div", { class: "rp-falsifiers" });
  wrap.appendChild(el("h3", {}, "需要盯住的信号"));
  for (const group of comparison.falsifiers_by_dimension) {
    const section = el("div", { class: "rp-falsifier-group" });
    section.appendChild(
      el("h4", {}, DIMENSION_LABELS[group.dimension] || group.dimension)
    );
    const list = el("ul", { class: "rp-list" });
    for (const item of group.items || []) {
      const li = el("li");
      li.appendChild(el("span", { class: "rp-falsifier-seat" }, `${item.seat_id}：`));
      li.appendChild(document.createTextNode(String(item.text || "")));
      list.appendChild(li);
    }
    section.appendChild(list);
    wrap.appendChild(section);
  }
  return wrap;
}

function progressLine(detail) {
  if (TERMINAL_ROUNDS.has(detail.status)) return null;
  const terminal = detail.seats.filter((seat) => TERMINAL_SEATS.has(seat.status)).length;
  return el(
    "p",
    { class: "rp-progress", "aria-live": "polite" },
    `本轮进行中：${terminal} / ${detail.seats.length} 席已结束，页面每 ${POLL_INTERVAL_MS / 1000} 秒自动更新。`
  );
}

// ---- 卡片挂载 -------------------------------------------------------------

export function mountReviewCard(container, { listingId, ticker } = {}) {
  if (!container || !listingId) return null;
  const previousDestroy = container.__reviewPanelDestroy;
  if (typeof previousDestroy === "function") previousDestroy();

  let destroyed = false;
  let pollTimer = null;
  let pollDeadline = 0;
  let panelsState = { enabled: false, panels: [], lenses: [], configuration_error: null };
  let detail = null;
  let summaries = [];
  let selectedRoundId = null;
  const state = { selectedSeat: 0 };
  const narrowQuery =
    typeof globalThis.matchMedia === "function" ? globalThis.matchMedia(MOBILE_QUERY) : null;

  const root = el("section", {
    class: "card review-panel-card",
    id: "stock-review",
    "aria-labelledby": "stock-review-title",
  });
  root.hidden = true;

  const head = el("div", { class: "card-head rp-head" });
  const heading = el("div", { class: "rp-heading" });
  heading.appendChild(el("h2", { id: "stock-review-title" }, "多视角评审"));
  heading.appendChild(el("span", { class: "rp-stamp" }, REVIEW_DISCLAIMER));
  head.appendChild(heading);
  head.appendChild(el("span", { class: "spacer" }));

  const history = el("select", {
    class: "rp-history",
    "aria-label": "切换评审轮次",
    hidden: "",
  });
  history.addEventListener("change", () => {
    const roundId = Number(history.value);
    if (Number.isInteger(roundId) && roundId > 0) selectRound(roundId);
  });
  head.appendChild(history);

  const launch = el("button", { type: "button", class: "btn btn-sm rp-launch" }, "发起新一轮");
  launch.addEventListener("click", () => openLaunchDialog());
  head.appendChild(launch);
  root.appendChild(head);

  const meta = el("p", { class: "rp-meta" });
  root.appendChild(meta);
  const body = el("div", { class: "rp-body" });
  root.appendChild(body);
  container.appendChild(root);

  function destroy() {
    destroyed = true;
    if (pollTimer) clearTimeout(pollTimer);
    pollTimer = null;
    if (narrowQuery && typeof narrowQuery.removeEventListener === "function") {
      narrowQuery.removeEventListener("change", rerender);
    }
  }
  container.__reviewPanelDestroy = destroy;

  function rerender() {
    if (!destroyed && detail) renderDetail(detail);
  }

  if (narrowQuery && typeof narrowQuery.addEventListener === "function") {
    narrowQuery.addEventListener("change", rerender);
  }

  function setBusy(message) {
    body.replaceChildren(el("p", { class: "notice notice-loading" }, message || "正在加载…"));
  }

  function showError(error) {
    body.replaceChildren(
      el("p", { class: "notice notice-error" }, messageOf(error))
    );
  }

  function renderEmpty() {
    meta.textContent = "";
    body.replaceChildren();
    body.appendChild(
      el("p", { class: "rp-note" }, "这只股票还没有多视角评审轮次。")
    );
    if (!panelsState.enabled) {
      body.appendChild(
        el("p", { class: "rp-note" }, "多视角评审功能未启用；可在设置页开启后发起。")
      );
      launch.hidden = true;
    } else if (panelsState.configuration_error) {
      body.appendChild(
        el("p", { class: "rp-note rp-note-warn" }, panelsState.configuration_error)
      );
    }
  }

  function renderHistory() {
    if (!summaries.length) {
      history.hidden = true;
      return;
    }
    history.hidden = false;
    history.replaceChildren();
    for (const summary of summaries) {
      const option = el(
        "option",
        { value: String(summary.id) },
        `#${summary.id} · ${shortDate(summary.created_at)} · ${TRIGGER_LABELS[summary.trigger] || summary.trigger} · ${ROUND_STATUS_LABELS[summary.status] || summary.status}`
      );
      history.appendChild(option);
    }
    if (selectedRoundId) history.value = String(selectedRoundId);
  }

  function renderDetail(current) {
    meta.textContent = metaLine(current);
    body.replaceChildren();
    const notices = [];
    if (current.status === "failed") {
      notices.push("本轮没有席位产出意见，评审失败。");
    }
    if (!panelsState.enabled) {
      notices.push("多视角评审功能当前未启用，本轮为历史记录。");
    }
    for (const notice of notices) {
      body.appendChild(el("p", { class: "rp-note rp-note-warn" }, notice));
    }
    body.appendChild(consensusBlock(current));
    if (current.comparison) {
      body.appendChild(matrixBlock(current));
    } else if (!TERMINAL_ROUNDS.has(current.status)) {
      body.appendChild(
        el("p", { class: "rp-note" }, "所有席位结束后才会计算共识与分歧。")
      );
    }
    const progress = progressLine(current);
    if (progress) body.appendChild(progress);
    if (current.seats.length) body.appendChild(seatsBlock(current, state));
    const falsifiers = falsifiersBlock(current.comparison);
    if (falsifiers) body.appendChild(falsifiers);
    launch.hidden = !panelsState.enabled;
  }

  async function selectRound(roundId) {
    selectedRoundId = roundId;
    if (history.value !== String(roundId)) history.value = String(roundId);
    stopPolling();
    setBusy("正在加载评审轮次…");
    try {
      const loaded = await get(`/review-rounds/${roundId}`);
      if (destroyed) return;
      detail = loaded;
      state.selectedSeat = 0;
      renderDetail(loaded);
      if (!TERMINAL_ROUNDS.has(loaded.status)) startPolling();
    } catch (error) {
      if (destroyed) return;
      if (error instanceof ApiError && error.status === 401) {
        globalThis.location.href = "/app/login";
        return;
      }
      showError(error);
    }
  }

  function stopPolling() {
    if (pollTimer) clearTimeout(pollTimer);
    pollTimer = null;
  }

  function startPolling() {
    stopPolling();
    pollDeadline = Date.now() + POLL_TIMEOUT_MS;
    const tick = async () => {
      if (destroyed || !root.isConnected) return;
      if (Date.now() > pollDeadline) {
        body.appendChild(
          el("p", { class: "rp-note" }, "自动刷新已超时；请稍后重新打开页面查看。")
        );
        return;
      }
      if (document.hidden) {
        pollTimer = setTimeout(tick, POLL_INTERVAL_MS);
        return;
      }
      try {
        const loaded = await get(`/review-rounds/${selectedRoundId}`);
        if (destroyed) return;
        detail = loaded;
        renderDetail(loaded);
        if (TERMINAL_ROUNDS.has(loaded.status)) {
          await refreshSummaries();
          return;
        }
        pollTimer = setTimeout(tick, POLL_INTERVAL_MS);
      } catch {
        // A transient polling failure must not lose the rendered round.
        pollTimer = setTimeout(tick, POLL_INTERVAL_MS);
      }
    };
    pollTimer = setTimeout(tick, POLL_INTERVAL_MS);
  }

  async function refreshSummaries() {
    try {
      const loaded = await get(`/watchlist/${listingId}/review-rounds?limit=20`);
      if (destroyed) return;
      summaries = Array.isArray(loaded) ? loaded : [];
      renderHistory();
    } catch {
      // Keep the previous history on a transient error.
    }
  }

  function option(value, text) {
    return el("option", { value }, text);
  }

  function openLaunchDialog() {
    if (!panelsState.enabled) return;
    const dialog = el("dialog", { class: "rp-dialog", "aria-labelledby": "rp-dialog-title" });
    const form = el("form", { method: "dialog", class: "rp-dialog-form" });
    const dialogHead = el("div", { class: "rp-dialog-head" });
    dialogHead.appendChild(el("h2", { id: "rp-dialog-title" }, "发起多视角评审"));
    const close = el("button", { type: "button", class: "icon-btn", "aria-label": "关闭" }, "×");
    close.addEventListener("click", () => dialog.close());
    dialogHead.appendChild(close);
    form.appendChild(dialogHead);

    const panelField = el("div", { class: "field" });
    panelField.appendChild(el("label", { for: "rp-panel" }, "评审组"));
    const panelSelect = el("select", { id: "rp-panel", name: "panel_id" });
    for (const panel of panelsState.panels) {
      panelSelect.appendChild(option(panel.id, panel.name));
    }
    if (panelsState.default_panel) panelSelect.value = panelsState.default_panel;
    panelField.appendChild(panelSelect);
    form.appendChild(panelField);

    const seatBox = el("fieldset", { class: "rp-seat-choices" });
    seatBox.appendChild(el("legend", {}, "本次席位（可临时增减）"));
    form.appendChild(seatBox);

    function syncSeats() {
      const panel =
        panelsState.panels.find((item) => item.id === panelSelect.value) || panelsState.panels[0];
      seatBox.replaceChildren(el("legend", {}, "本次席位（可临时增减）"));
      if (!panel) {
        seatBox.appendChild(el("p", { class: "rp-note" }, "尚未配置评审组。"));
        return;
      }
      for (const seat of panel.seats) {
        const label = el("label", { class: "rp-seat-choice" });
        const input = el("input", {
          type: "checkbox",
          name: "seat_ids",
          value: seat.seat_id,
          checked: "",
        });
        label.appendChild(input);
        const text = el("span");
        text.appendChild(el("b", {}, seat.lens_name || seat.lens_id));
        text.appendChild(el("small", {}, `${seat.model_ref}${seat.lens_focus ? ` · ${seat.lens_focus}` : ""}`));
        label.appendChild(text);
        seatBox.appendChild(label);
      }
    }
    syncSeats();
    panelSelect.addEventListener("change", syncSeats);

    const feedback = el("p", { class: "rp-dialog-error", hidden: "" });
    if (!panelsState.panels.length) {
      feedback.hidden = false;
      feedback.textContent =
        panelsState.configuration_error || "尚未配置评审组，请先在设置页配置。";
    }
    form.appendChild(feedback);

    const actions = el("div", { class: "rp-dialog-actions" });
    const cancel = el("button", { type: "button", class: "btn" }, "取消");
    cancel.addEventListener("click", () => dialog.close());
    const submit = el("button", { type: "submit", class: "btn btn-primary" }, "开始评审");
    if (!panelsState.panels.length) submit.disabled = true;
    actions.appendChild(cancel);
    actions.appendChild(submit);
    form.appendChild(actions);

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const selected = [...seatBox.querySelectorAll("input[name='seat_ids']:checked")].map(
        (input) => input.value
      );
      if (!selected.length) {
        feedback.hidden = false;
        feedback.textContent = "请至少选择一个席位。";
        return;
      }
      submit.disabled = true;
      submit.textContent = "正在创建…";
      feedback.hidden = true;
      try {
        const detailResponse = await post(
          `/watchlist/${listingId}/review-rounds`,
          { panel_id: panelSelect.value, seat_ids: selected }
        );
        dialog.close();
        await refreshSummaries();
        selectedRoundId = detailResponse.id;
        await selectRound(detailResponse.id);
      } catch (error) {
        feedback.hidden = false;
        feedback.textContent = messageOf(error);
        submit.disabled = false;
        submit.textContent = "开始评审";
      }
    });

    dialog.addEventListener("close", () => dialog.remove());
    dialog.appendChild(form);
    document.body.appendChild(dialog);
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "");
  }

  async function loadPanels() {
    try {
      const payload = await get("/review-panels");
      panelsState = {
        enabled: Boolean(payload && payload.enabled),
        default_panel: payload && payload.default_panel ? payload.default_panel : null,
        panels: payload && Array.isArray(payload.panels) ? payload.panels : [],
        lenses: payload && Array.isArray(payload.lenses) ? payload.lenses : [],
        configuration_error:
          payload && typeof payload.configuration_error === "string"
            ? payload.configuration_error
            : null,
      };
    } catch {
      panelsState = {
        enabled: false,
        default_panel: null,
        panels: [],
        lenses: [],
        configuration_error: null,
      };
    }
    launch.hidden = !panelsState.enabled;
  }

  async function load() {
    setBusy("正在加载多视角评审…");
    await loadPanels();
    if (destroyed) return;
    try {
      const loaded = await get(`/watchlist/${listingId}/review-rounds?limit=20`);
      if (destroyed) return;
      summaries = Array.isArray(loaded) ? loaded : [];
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) {
        globalThis.location.href = "/app/login";
        return;
      }
      root.hidden = false;
      showError(error);
      return;
    }
    if (!summaries.length && !panelsState.enabled) {
      destroy();
      root.remove();
      return;
    }
    root.hidden = false;
    renderHistory();
    if (!summaries.length) {
      renderEmpty();
      return;
    }
    await selectRound(summaries[0].id);
  }

  // A visibility change should refresh an active round immediately instead of
  // waiting for the next interval (mobile browsers pause timers in background).
  const onVisibility = () => {
    if (document.hidden || destroyed || !detail) return;
    if (!TERMINAL_ROUNDS.has(detail.status) && selectedRoundId) {
      stopPolling();
      startPolling();
    }
  };
  document.addEventListener("visibilitychange", onVisibility);
  container.__reviewPanelDestroy = () => {
    document.removeEventListener("visibilitychange", onVisibility);
    destroy();
  };

  load();
  return { destroy: container.__reviewPanelDestroy, root };
}
