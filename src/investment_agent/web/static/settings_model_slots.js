// Slot dropdowns. A slot stores provider/model and never asks the catalog who uses it.
import { el } from "./dom.js";

export const MODEL_UNAVAILABLE = "模型不可用";

export function parseSlots(value) {
  if (!value) return {};
  if (typeof value === "object") return structuredClone(value);
  try {
    const parsed = JSON.parse(value);
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch {
    return {};
  }
}

export function canonicalSlots(value) {
  const parsed = parseSlots(value);
  const consumers = Object.keys(parsed).sort();
  const ordered = {};
  for (const consumer of consumers) {
    const slots = parsed[consumer] && typeof parsed[consumer] === "object" ? parsed[consumer] : {};
    ordered[consumer] = {};
    for (const slot of Object.keys(slots).sort()) ordered[consumer][slot] = slots[slot] || "";
  }
  return JSON.stringify(ordered);
}

export function modelChoices(catalog, required) {
  const choices = [];
  for (const provider of catalog?.providers || []) {
    for (const model of provider.models || []) {
      if (model.enabled === false) continue;
      const caps = new Set(model.capabilities || []);
      if ((required || []).every((cap) => caps.has(cap))) {
        choices.push({
          ref: `${provider.id}/${model.id}`,
          label: `${provider.name || provider.id} / ${model.name || model.model || model.id}`,
        });
      }
    }
  }
  return choices;
}

export function slotStatus(catalog, ref, required) {
  if (!ref) return {state: "empty", label: "未选择"};
  const choice = modelChoices(catalog, required).find((item) => item.ref === ref);
  if (!choice) return {state: "unavailable", label: MODEL_UNAVAILABLE};
  return {state: "ok", label: choice.label};
}

export function slotControl(slotsValue, catalog, consumer) {
  const draft = parseSlots(slotsValue);
  draft[consumer.id] = {...(draft[consumer.id] || {})};
  const root = el("fieldset", {class: "llm-slots"});
  root.appendChild(el("legend", {class: "visually-hidden"}, `${consumer.title} 模型槽位`));
  const notify = () => root.dispatchEvent(new Event("change", {bubbles: true}));
  for (const slot of consumer.slots || []) {
    const wrap = el("div", {class: "llm-field"});
    const fieldId = `slot-${consumer.id}-${slot.id}`;
    wrap.appendChild(el("label", {for: fieldId}, slot.label));
    if (slot.help) wrap.appendChild(el("p", {class: "faint"}, slot.help));
    const select = el("select", {id: fieldId});
    const current = draft[consumer.id][slot.id] || "";
    const choices = modelChoices(catalog, slot.required_capabilities);
    select.appendChild(el("option", {value: ""}, "未选择"));
    const known = new Set(choices.map((item) => item.ref));
    if (current && !known.has(current)) {
      select.appendChild(el("option", {value: current}, MODEL_UNAVAILABLE));
    }
    for (const choice of choices) select.appendChild(el("option", {value: choice.ref}, choice.label));
    select.value = current;
    const status = el("p", {class: current && !known.has(current) ? "notice notice-warn" : "faint"});
    const paint = () => {
      const view = slotStatus(catalog, select.value, slot.required_capabilities);
      status.textContent = view.state === "unavailable" ? MODEL_UNAVAILABLE : view.state === "empty" ? "未选择" : view.label;
      status.className = view.state === "unavailable" ? "notice notice-warn" : "faint";
    };
    select.addEventListener("change", () => {
      draft[consumer.id][slot.id] = select.value;
      paint();
      notify();
    });
    wrap.append(select, status);
    paint();
    root.appendChild(wrap);
  }
  return {control: root, root, read: () => canonicalSlots(draft)};
}
