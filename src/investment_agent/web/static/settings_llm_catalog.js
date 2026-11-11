// Provider / model editor. Credentials are never returned by the API.
// This page does not record which agent or task uses a model.
import { post } from "./api.js";
import { clear, el } from "./dom.js";
import { randomUUID } from "./uuid.js";

const CAPABILITIES = [
  ["thinking", "推理"],
  ["tools", "工具调用"],
  ["json_object", "JSON Object"],
  ["json_schema", "JSON Schema"],
  ["tool_choice", "强制工具选择"],
];

export function catalogPayload(value) {
  const copy = structuredClone(value);
  delete copy.bindings;
  for (const provider of copy.providers || []) {
    delete provider.configured;
    delete provider.hint;
    for (const model of provider.models || []) {
      model.supports_json_object = (model.capabilities || []).includes("json_object");
      model.supports_json_schema = (model.capabilities || []).includes("json_schema");
      model.tool_choice_required = (model.capabilities || []).includes("tool_choice");
    }
  }
  return JSON.stringify(copy);
}

export function catalogControl(value, encryptionReady) {
  const draft = structuredClone(value || {providers: []});
  delete draft.bindings;
  const root = el("fieldset", {id: "setting-llm_catalog", class: "llm-catalog", disabled: !encryptionReady});
  root.appendChild(el("legend", {class: "visually-hidden"}, "Provider 与模型目录"));
  const status = el("p", {class: "notice", "aria-live": "polite", hidden: true});
  const providers = el("div", {class: "llm-provider-list"});
  const notify = () => root.dispatchEvent(new Event("change", {bubbles: true}));
  const id = randomUUID;

  function field(parent, label, object, key, {type = "text", options, placeholder} = {}) {
    const fieldId = `llm-field-${id()}`;
    const wrap = el("div", {class: "llm-field"});
    wrap.appendChild(el("label", {for: fieldId}, label));
    const control = el(options ? "select" : "input", {id: fieldId, type: options ? undefined : type, placeholder,
      autocomplete: type === "password" ? "new-password" : "off"});
    if (options) {
      for (const [v, text] of options) control.appendChild(el("option", {value: v}, text));
    }
    if (type === "checkbox") control.checked = Boolean(object[key]);
    else control.value = object[key] ?? "";
    control.addEventListener(options || type === "checkbox" ? "change" : "input", () => {
      object[key] = type === "checkbox" ? control.checked : type === "number"
        ? (control.value === "" ? null : Number(control.value)) : control.value;
      notify();
    });
    wrap.appendChild(control);
    parent.appendChild(wrap);
    return control;
  }

  function removeButton(label, remove) {
    const button = el("button", {type: "button", class: "btn btn-sm btn-danger-ghost"}, label);
    button.addEventListener("click", () => {remove(); renderProviders(); notify();});
    return button;
  }

  async function test(provider, model, button) {
    button.disabled = true;
    status.hidden = false;
    status.textContent = "测试连接中…";
    try {
      const result = await post("/settings/test/llm", {
        provider_id: provider.id, base_url: provider.base_url, model: model.model,
        api_key: provider.api_key ?? null, thinking: model.thinking || "auto",
      }, {idempotency: false});
      status.textContent = `${provider.name} / ${model.name}：${result.message}`;
    } catch (error) {
      status.textContent = `测试失败：${error.message}`;
    } finally { button.disabled = false; }
  }

  function capabilityFields(model, parent) {
    const wrap = el("div", {class: "llm-capabilities"});
    wrap.appendChild(el("span", {class: "llm-field-label"}, "能力标签"));
    const caps = new Set(model.capabilities || []);
    for (const [name, label] of CAPABILITIES) {
      const fieldId = `llm-cap-${id()}`;
      const labelNode = el("label", {class: "llm-cap", for: fieldId});
      const box = el("input", {id: fieldId, type: "checkbox"});
      box.checked = caps.has(name);
      box.addEventListener("change", () => {
        const next = new Set(model.capabilities || []);
        if (box.checked) next.add(name);
        else next.delete(name);
        model.capabilities = CAPABILITIES.map(([item]) => item).filter((item) => next.has(item));
        model.supports_json_object = next.has("json_object");
        model.supports_json_schema = next.has("json_schema");
        model.tool_choice_required = next.has("tool_choice");
        notify();
      });
      labelNode.append(box, el("span", {}, label));
      wrap.appendChild(labelNode);
    }
    parent.appendChild(wrap);
  }

  function renderProviders() {
    clear(providers);
    if (!draft.providers.length) providers.appendChild(el("p", {class: "notice"}, "尚未添加 Provider。"));
    for (const provider of draft.providers) {
      const card = el("section", {class: "llm-provider"});
      const head = el("div", {class: "llm-card-head"});
      head.appendChild(el("h3", {}, provider.name || "新 Provider"));
      head.appendChild(removeButton("删除 Provider", () => {
        draft.providers = draft.providers.filter((item) => item !== provider);
      }));
      card.appendChild(head);
      const fields = el("div", {class: "llm-field-grid"});
      field(fields, "Provider name", provider, "name");
      field(fields, "Provider type", provider, "kind", {options: [["openai", "OpenAI 兼容"], ["deepseek", "DeepSeek"]]});
      field(fields, "Base URL", provider, "base_url", {type: "url", placeholder: "完整 URL，例如 api.example.com/v1"});
      const key = field(fields, "API Key", provider, "api_key", {type: "password", placeholder: provider.configured ? `已设置 ···${provider.hint || ""}；留空不修改` : "输入 API Key"});
      key.addEventListener("input", () => {if (!key.value) provider.api_key = null; notify();});
      card.appendChild(fields);
      const credential = el("div", {class: "llm-card-head"});
      credential.appendChild(el("small", {class: "faint"}, provider.api_key === "" ? "保存后清除密钥" : provider.configured ? "密钥已加密保存" : "密钥尚未保存"));
      const clearKey = el("button", {type: "button", class: "btn btn-sm secondary"}, provider.api_key === "" ? "撤销清除密钥" : "清除密钥");
      clearKey.addEventListener("click", () => {provider.api_key = provider.api_key === "" ? null : ""; renderProviders(); notify();});
      credential.appendChild(clearKey);
      card.appendChild(credential);
      for (const model of provider.models) {
        const box = el("div", {class: "llm-model"});
        const grid = el("div", {class: "llm-field-grid"});
        field(grid, "模型显示名称", model, "name");
        field(grid, "Model ID", model, "model", {placeholder: "API 接受的模型名"});
        field(grid, "上下文长度", model, "context_length", {type: "number"});
        field(grid, "启用", model, "enabled", {type: "checkbox"});
        box.appendChild(grid);
        capabilityFields(model, box);
        const advanced = el("details");
        advanced.appendChild(el("summary", {}, "调用参数"));
        const extra = el("div", {class: "llm-field-grid"});
        field(extra, "推理请求", model, "thinking", {options: [["auto", "不发送"], ["enabled", "开启"], ["disabled", "关闭"]]});
        field(extra, "请求超时（秒）", model, "timeout_seconds", {type: "number"});
        field(extra, "Capability profile", model, "profile", {options: [["openai", "OpenAI"], ["deepseek", "DeepSeek"]]});
        field(extra, "Official model name (optional)", model, "profile_model");
        field(extra, "输出模式", model, "output_mode", {options: [["prompted", "JSON Object"], ["tool", "Tool call"], ["native", "JSON Schema"]]});
        field(extra, "Temperature（可选）", model, "temperature", {type: "number"});
        field(extra, "Max output tokens (optional)", model, "max_tokens", {type: "number"});
        field(extra, "校验重试次数", model, "retries", {type: "number"});
        advanced.appendChild(extra);
        box.appendChild(advanced);
        const ref = el("p", {class: "faint"}, `引用 ${provider.id}/${model.id}`);
        box.appendChild(ref);
        const actions = el("div", {class: "llm-card-head"});
        const testButton = el("button", {type: "button", class: "btn btn-sm secondary"}, "测试此模型");
        testButton.addEventListener("click", () => test(provider, model, testButton));
        actions.appendChild(testButton);
        actions.appendChild(removeButton("删除模型", () => {
          provider.models = provider.models.filter((item) => item !== model);
        }));
        box.appendChild(actions);
        card.appendChild(box);
      }
      const addModel = el("button", {type: "button", class: "btn btn-sm secondary"}, "添加模型");
      addModel.addEventListener("click", () => {
        provider.models.push({
          id: id(), name: "", model: "", enabled: true, capabilities: ["json_object"], context_length: null,
          thinking: "auto", timeout_seconds: 120, profile: provider.kind, profile_model: "",
          output_mode: "prompted", supports_json_schema: false, supports_json_object: true,
          tool_choice_required: false, temperature: null, max_tokens: null, retries: 1,
        });
        renderProviders(); notify();
      });
      card.appendChild(addModel);
      providers.appendChild(card);
    }
  }

  root.appendChild(providers);
  const addProvider = el("button", {type: "button", class: "btn btn-sm secondary"}, "添加 Provider");
  addProvider.addEventListener("click", () => {
    draft.providers.push({id: id(), name: "", kind: "openai", base_url: "", api_key: null, models: []});
    renderProviders(); notify();
  });
  root.appendChild(addProvider);
  root.appendChild(status);
  renderProviders();
  return {control: root, root, read: () => catalogPayload(draft)};
}
