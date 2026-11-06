// 评审组编辑器（多视角评审「评审组」主题）。
// 每个评审组是一组席位：席位 = 视角 + provider/model。字段名与
// review_panel/panels.py 的 parse_review_panels 完全一致，服务端校验仍是最终依据。
// 模型下拉只列出已启用且具备 tools + json_object 的模型，与模型槽位页一致。
import { clear, el } from "./dom.js";
import { MODEL_UNAVAILABLE, modelChoices } from "./settings_model_slots.js";

export const REVIEW_PANEL_ID_PATTERN = /^[a-z][a-z0-9_]{0,39}$/;
export const REVIEW_PANEL_MAX_SEATS = 6;
export const REVIEW_PANEL_MAX_NAME = 80;
const REQUIRED_CAPABILITIES = ["tools", "json_object"];
const MODEL_REF_PATTERN = /^[^/]+\/[^/]+$/;

function emptyDocument() {
  return {default_panel: "", panels: {}};
}

function text(value) {
  return typeof value === "string" ? value : value === null || value === undefined ? "" : String(value);
}

export function parseReviewPanels(value) {
  let parsed = value;
  if (typeof parsed === "string") {
    if (!parsed.trim()) return emptyDocument();
    try {
      parsed = JSON.parse(parsed);
    } catch {
      return emptyDocument();
    }
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return emptyDocument();
  const panels = {};
  for (const [id, panel] of Object.entries(parsed.panels || {})) {
    const rawSeats = panel && Array.isArray(panel.seats) ? panel.seats : [];
    panels[id] = {
      name: text(panel && panel.name),
      seats: rawSeats.map((seat) => ({
        seat_id: text(seat && seat.seat_id),
        lens_id: text(seat && seat.lens_id),
        model_ref: text(seat && seat.model_ref),
      })),
    };
  }
  return {default_panel: text(parsed.default_panel), panels};
}

// Canonical JSON in the key order Python's sort_keys emits: top level,
// panel, then seat. An empty document serializes to "" (not configured).
export function canonicalReviewPanels(value) {
  const document = parseReviewPanels(value);
  const ids = Object.keys(document.panels).sort();
  if (!ids.length) return "";
  const panels = {};
  for (const id of ids) {
    const panel = document.panels[id];
    panels[id] = {
      name: panel.name,
      seats: panel.seats.map((seat) => ({
        lens_id: seat.lens_id,
        model_ref: seat.model_ref,
        seat_id: seat.seat_id,
      })),
    };
  }
  return JSON.stringify({default_panel: document.default_panel, panels});
}

export function reviewPanelChoices(catalog) {
  return modelChoices(catalog, REQUIRED_CAPABILITIES);
}

function choiceFor(choices, ref) {
  return choices.find((item) => item.ref === ref) || null;
}

export function reviewSeatModelStatus(catalog, ref) {
  if (!ref) return {state: "empty", label: "未选择"};
  const choice = choiceFor(reviewPanelChoices(catalog), ref);
  if (!choice) return {state: "unavailable", label: MODEL_UNAVAILABLE};
  return {state: "ok", label: choice.label};
}

export function reviewSeatLensStatus(lenses, lensId) {
  if (!lensId) return {state: "empty", label: "未选择"};
  const lens = (lenses || []).find((item) => item.id === lensId);
  if (!lens) return {state: "unavailable", label: "视角未配置"};
  return {state: "ok", label: `${lens.name || lens.id}（${lens.ref || `${lens.id}.v${lens.version}`}）`};
}

// Mirrors parse_review_panels: 1..6 seats, id pattern, unique ids, default
// panel exists. Lens existence is not a panel-document rule (a round rejects
// an unknown lens), so it is surfaced as a warning, not a save blocker.
export function validateReviewPanels(value) {
  const document = parseReviewPanels(value);
  const ids = Object.keys(document.panels);
  if (!ids.length) return [];
  const errors = [];
  if (!REVIEW_PANEL_ID_PATTERN.test(document.default_panel)) {
    errors.push("默认评审组 id 必须小写字母开头，只能包含小写字母、数字和下划线，最长 40 个字符");
  } else if (!ids.includes(document.default_panel)) {
    errors.push("默认评审组不存在");
  }
  for (const id of ids) {
    if (!REVIEW_PANEL_ID_PATTERN.test(id)) {
      errors.push("评审组 id 必须小写字母开头，只能包含小写字母、数字和下划线，最长 40 个字符");
    }
    const panel = document.panels[id];
    const name = panel.name.trim();
    if (!name) errors.push(`评审组 ${id} 的名称不能为空`);
    else if (name.length > REVIEW_PANEL_MAX_NAME) errors.push(`评审组 ${id} 的名称过长`);
    if (panel.seats.length < 1 || panel.seats.length > REVIEW_PANEL_MAX_SEATS) {
      errors.push(`每个评审组需要 1–${REVIEW_PANEL_MAX_SEATS} 个席位`);
    }
    const seen = new Set();
    for (const seat of panel.seats) {
      if (!REVIEW_PANEL_ID_PATTERN.test(seat.seat_id)) {
        errors.push("席位 id 必须小写字母开头，只能包含小写字母、数字和下划线，最长 40 个字符");
      }
      if (!REVIEW_PANEL_ID_PATTERN.test(seat.lens_id)) {
        errors.push("视角 id 必须小写字母开头，只能包含小写字母、数字和下划线，最长 40 个字符");
      }
      if (!MODEL_REF_PATTERN.test(seat.model_ref.trim())) {
        errors.push("每个席位都要选择一个模型（引用格式 provider/model）");
      }
      if (seen.has(seat.seat_id)) errors.push(`评审组 ${id} 内的席位 id 不能重复`);
      seen.add(seat.seat_id);
    }
  }
  return [...new Set(errors)];
}

export function reviewPanelSummary(value, catalog) {
  const document = parseReviewPanels(value);
  const ids = Object.keys(document.panels);
  if (!ids.length) return "尚未配置";
  let seats = 0;
  let unavailable = 0;
  for (const id of ids) {
    for (const seat of document.panels[id].seats) {
      seats += 1;
      if (reviewSeatModelStatus(catalog, seat.model_ref).state === "unavailable") unavailable += 1;
    }
  }
  if (unavailable) return `${ids.length} 组 · 模型不可用`;
  return `${ids.length} 组 · ${seats} 席`;
}

function nextPanelId(document) {
  const base = "panel";
  if (!document.panels[base]) return base;
  for (let index = 2; ; index += 1) {
    const id = `panel${index}`;
    if (!document.panels[id]) return id;
  }
}

function nextSeatId(panel, preferred) {
  const base = REVIEW_PANEL_ID_PATTERN.test(preferred) ? preferred : "seat";
  if (!panel.seats.some((seat) => seat.seat_id === base)) return base;
  for (let index = 2; ; index += 1) {
    const suffix = `_${index}`;
    const id = `${base.slice(0, 40 - suffix.length)}${suffix}`;
    if (!panel.seats.some((seat) => seat.seat_id === id)) return id;
  }
}

function firstLensId(lenses) {
  return (lenses || [])[0]?.id || "";
}

function field(parent, labelText, control) {
  const wrap = el("div", {class: "llm-field"});
  wrap.appendChild(el("label", {}, labelText));
  wrap.appendChild(control);
  parent.appendChild(wrap);
  return control;
}

export function reviewPanelsControl(value, catalog, lenses) {
  const draft = parseReviewPanels(value);
  const choices = reviewPanelChoices(catalog);
  const root = el("fieldset", {id: "setting-review_panels", class: "review-panels"});
  root.appendChild(el("legend", {class: "visually-hidden"}, "评审组"));
  const body = el("div", {class: "review-panel-list"});
  root.appendChild(body);
  const notify = () => root.dispatchEvent(new Event("change", {bubbles: true}));
  const input = (control, apply) => {
    control.addEventListener("input", () => {apply(); notify();});
    control.addEventListener("change", () => {apply(); notify();});
  };
  const button = (labelText, className, onClick) => {
    const node = el("button", {type: "button", class: className}, labelText);
    node.addEventListener("click", onClick);
    return node;
  };

  function paintStatus(status, ref) {
    const view = reviewSeatModelStatus(catalog, ref);
    status.textContent = view.label;
    status.className = view.state === "unavailable"
      ? "review-seat-status notice notice-warn"
      : "review-seat-status faint";
    status.hidden = view.state === "empty";
  }

  function modelSelect(seat, status) {
    const select = el("select");
    select.appendChild(el("option", {value: ""}, "未选择"));
    const known = new Set(choices.map((item) => item.ref));
    if (seat.model_ref && !known.has(seat.model_ref)) {
      select.appendChild(el("option", {value: seat.model_ref}, MODEL_UNAVAILABLE));
    }
    for (const choice of choices) select.appendChild(el("option", {value: choice.ref}, choice.label));
    select.value = seat.model_ref;
    select.addEventListener("change", () => {
      seat.model_ref = select.value;
      paintStatus(status, select.value);
      notify();
    });
    return select;
  }

  function lensSelect(seat) {
    const select = el("select");
    select.appendChild(el("option", {value: ""}, "未选择"));
    const known = new Set((lenses || []).map((item) => item.id));
    if (seat.lens_id && !known.has(seat.lens_id)) {
      select.appendChild(el("option", {value: seat.lens_id}, "视角未配置"));
    }
    for (const lens of lenses || []) {
      select.appendChild(el("option", {value: lens.id}, lens.name || lens.id));
    }
    select.value = seat.lens_id;
    select.addEventListener("change", () => {
      seat.lens_id = select.value;
      notify();
    });
    return select;
  }

  function seatRow(panel, seat, index) {
    const box = el("div", {class: "llm-model review-seat"});
    const head = el("div", {class: "llm-card-head"});
    head.appendChild(el("h4", {}, `席位 ${index + 1}`));
    const remove = button("删除席位", "btn btn-sm btn-danger-ghost", () => {
      panel.seats = panel.seats.filter((item) => item !== seat);
      render();
      notify();
    });
    remove.disabled = panel.seats.length <= 1;
    head.appendChild(remove);
    box.appendChild(head);
    const grid = el("div", {class: "llm-field-grid"});
    const seatId = el("input", {type: "text", autocomplete: "off", spellcheck: "false", placeholder: "例如 value"});
    seatId.value = seat.seat_id;
    input(seatId, () => {seat.seat_id = seatId.value;});
    field(grid, "席位 id", seatId);
    field(grid, "视角", lensSelect(seat));
    const status = el("p", {class: "review-seat-status faint", "aria-live": "polite"});
    const model = modelSelect(seat, status);
    field(grid, "模型", model);
    paintStatus(status, model.value);
    box.append(grid, status);
    return box;
  }

  function panelCard(id) {
    const panel = draft.panels[id];
    const card = el("section", {class: "llm-provider review-panel", "data-panel": id});
    const head = el("div", {class: "llm-card-head"});
    head.appendChild(el("h3", {}, panel.name || id));
    const actions = el("div", {class: "llm-card-head"});
    const defaultLabel = el("label", {class: "llm-cap"});
    const defaultBox = el("input", {type: "radio", name: "review-panel-default"});
    defaultBox.checked = draft.default_panel === id;
    defaultBox.addEventListener("change", () => {
      if (defaultBox.checked) {
        draft.default_panel = id;
        notify();
      }
    });
    defaultLabel.append(defaultBox, el("span", {}, "默认评审组"));
    actions.appendChild(defaultLabel);
    actions.appendChild(button("删除评审组", "btn btn-sm btn-danger-ghost", () => {
      delete draft.panels[id];
      if (draft.default_panel === id) draft.default_panel = Object.keys(draft.panels)[0] || "";
      render();
      notify();
    }));
    head.appendChild(actions);
    card.appendChild(head);

    const grid = el("div", {class: "llm-field-grid"});
    const name = el("input", {type: "text", autocomplete: "off", placeholder: "评审组名称"});
    name.value = panel.name;
    name.addEventListener("input", () => {
      panel.name = name.value;
      head.querySelector("h3").textContent = name.value || id;
      notify();
    });
    field(grid, "名称", name);
    const idText = el("p", {class: "faint"}, `id：${id}`);
    grid.appendChild(idText);
    card.appendChild(grid);

    card.appendChild(el("h4", {}, "席位"));
    for (const [index, seat] of panel.seats.entries()) card.appendChild(seatRow(panel, seat, index));
    const addSeat = button("添加席位", "btn btn-sm secondary", () => {
      const used = new Set(panel.seats.map((seat) => seat.seat_id));
      const lensId = firstLensId(lenses);
      const preferred = used.has(lensId) || !lensId ? "seat" : lensId;
      panel.seats.push({seat_id: nextSeatId(panel, preferred), lens_id: lensId, model_ref: ""});
      render();
      notify();
    });
    addSeat.disabled = panel.seats.length >= REVIEW_PANEL_MAX_SEATS;
    card.appendChild(addSeat);
    return card;
  }

  function render() {
    clear(body);
    const ids = Object.keys(draft.panels);
    if (!ids.length) {
      body.appendChild(el("p", {class: "notice"}, "尚未配置评审组。添加评审组后保存即可启用。"));
    }
    for (const id of ids) body.appendChild(panelCard(id));
    const add = button("添加评审组", "btn btn-sm secondary", () => {
      const id = nextPanelId(draft);
      draft.panels[id] = {
        name: `评审组 ${Object.keys(draft.panels).length + 1}`,
        seats: [{seat_id: "seat", lens_id: firstLensId(lenses), model_ref: ""}],
      };
      if (!draft.default_panel) draft.default_panel = id;
      render();
      notify();
    });
    body.appendChild(add);
    body.appendChild(el("p", {class: "faint"},
      `每个评审组 1–${REVIEW_PANEL_MAX_SEATS} 个席位；席位 id 与评审组 id 使用小写字母、数字和下划线。模型只列出已启用且具备工具与 JSON 能力的模型。`));
  }

  render();
  return {
    control: root,
    root,
    read: () => canonicalReviewPanels(draft),
    validate: () => validateReviewPanels(draft),
  };
}
