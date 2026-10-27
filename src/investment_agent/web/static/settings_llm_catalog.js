// Shared Provider/Model editor. Credentials are never returned by the API.
import { post } from "./api.js";
import { clear, el } from "./dom.js";
import { randomUUID } from "./uuid.js";

export function catalogPayload(value) {
  const copy = structuredClone(value);
  for (const provider of copy.providers || []) {
    delete provider.configured;
    delete provider.hint;
  }
  return JSON.stringify(copy);
}

export function catalogSelectionLabel(value, binding) {
  const ref = value?.bindings?.[binding];
  const provider = value?.providers?.find((p) => p.id === ref?.provider_id);
  const model = provider?.models?.find((m) => m.id === ref?.model_id);
  return model ? `${provider.name} / ${model.name}` : "沿用原配置";
}

export function catalogControl(value, encryptionReady) {
  const draft = structuredClone(value || {providers: [], bindings: {}});
  const root = el("fieldset", {id: "setting-llm_catalog", class: "llm-catalog", disabled: !encryptionReady});
  root.appendChild(el("legend", {class: "visually-hidden"}, "Provider 与模型目录"));
  const status = el("p", {class: "notice", "aria-live": "polite", hidden: true});
  const providers = el("div", {class: "llm-provider-list"});
  const bindings = el("div", {class: "llm-bindings"});
  const notify = () => root.dispatchEvent(new Event("change", {bubbles: true}));
  const id = randomUUID;

  function field(parent, label, object, key, {type = "text", options, placeholder, refresh = false} = {}) {
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
      if (refresh) renderBindings();
      notify();
    });
    wrap.appendChild(control);
    parent.appendChild(wrap);
    return control;
  }

  function inUse(provider, model) {
    return Object.values(draft.bindings).some((ref) => ref.provider_id === provider.id && (!model || ref.model_id === model.id));
  }

  function removeButton(label, used, remove) {
    const button = el("button", {type: "button", class: "btn btn-sm btn-danger-ghost", disabled: used,
      title: used ? "请先更改上方正在使用此模型的选择" : undefined}, label);
    button.addEventListener("click", () => {remove(); renderProviders(); renderBindings(); notify();});
    return button;
  }

  function renderBindings() {
    clear(bindings);
    bindings.appendChild(el("h3", {}, "模型选择"));
    const choices = [["", "沿用原配置"]];
    for (const provider of draft.providers) {
      for (const model of provider.models) choices.push([`${provider.id}/${model.id}`, `${provider.name || "新 Provider"} / ${model.name || model.model || "新模型"}`]);
    }
    // Unknown future agent bindings are retained when editing today's two consumers.
    for (const [key, title] of [["agent", "投资 Agent 模型"], ["research", "研究解释与规则草拟模型"]]) {
      const ref = draft.bindings[key];
      const holder = {value: ref ? `${ref.provider_id}/${ref.model_id}` : ""};
      const control = field(bindings, title, holder, "value", {options: choices});
      control.addEventListener("change", () => {
        if (!control.value) delete draft.bindings[key];
        else {
          const [provider_id, model_id] = control.value.split("/");
          draft.bindings[key] = {provider_id, model_id};
        }
        renderProviders();
        notify();
      });
    }
    bindings.appendChild(el("p", {class: "faint"}, "Provider 或模型正在使用时，请先更改选择再删除。保存后从下个任务开始使用。"));
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

  function renderProviders() {
    clear(providers);
    if (!draft.providers.length) providers.appendChild(el("p", {class: "notice"}, "尚未添加 Provider。现有模型配置仍可继续使用。"));
    for (const provider of draft.providers) {
      const card = el("section", {class: "llm-provider"});
      const head = el("div", {class: "llm-card-head"});
      head.appendChild(el("h3", {}, provider.name || "新 Provider"));
      head.appendChild(removeButton("删除 Provider", inUse(provider), () => {
        draft.providers = draft.providers.filter((p) => p !== provider);
      }));
      card.appendChild(head);
      const fields = el("div", {class: "llm-field-grid"});
      field(fields, "Provider 名称", provider, "name", {refresh: true});
      field(fields, "接口类型", provider, "kind", {options: [["openai", "OpenAI 兼容"], ["deepseek", "DeepSeek"]]});
      field(fields, "接口地址", provider, "base_url", {type: "url", placeholder: "https://api.example.com/v1"});
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
        field(grid, "模型显示名称", model, "name", {refresh: true});
        field(grid, "模型标识", model, "model", {placeholder: "接口接受的模型名", refresh: true});
        field(grid, "推理模式", model, "thinking", {options: [["auto", "默认"], ["enabled", "开启"], ["disabled", "关闭"]]});
        field(grid, "请求超时（秒）", model, "timeout_seconds", {type: "number"});
        box.appendChild(grid);
        const advanced = el("details");
        advanced.appendChild(el("summary", {}, "Agent 模型能力与参数"));
        const extra = el("div", {class: "llm-field-grid"});
        field(extra, "能力 Profile", model, "profile", {options: [["openai", "OpenAI"], ["deepseek", "DeepSeek"]]});
        field(extra, "Profile 官方模型名（可选）", model, "profile_model");
        field(extra, "输出模式", model, "output_mode", {options: [["prompted", "JSON Object"], ["tool", "工具调用"], ["native", "JSON Schema"]]});
        field(extra, "支持 JSON Object", model, "supports_json_object", {type: "checkbox"});
        field(extra, "支持 JSON Schema", model, "supports_json_schema", {type: "checkbox"});
        field(extra, "支持强制工具选择", model, "tool_choice_required", {type: "checkbox"});
        field(extra, "Temperature（可选）", model, "temperature", {type: "number"});
        field(extra, "最大输出 Token（可选）", model, "max_tokens", {type: "number"});
        field(extra, "校验重试次数", model, "retries", {type: "number"});
        advanced.appendChild(extra);
        box.appendChild(advanced);
        const actions = el("div", {class: "llm-card-head"});
        const testButton = el("button", {type: "button", class: "btn btn-sm secondary"}, "测试此模型");
        testButton.addEventListener("click", () => test(provider, model, testButton));
        actions.appendChild(testButton);
        actions.appendChild(removeButton("删除模型", inUse(provider, model), () => {
          provider.models = provider.models.filter((m) => m !== model);
        }));
        box.appendChild(actions);
        card.appendChild(box);
      }
      const addModel = el("button", {type: "button", class: "btn btn-sm secondary"}, "添加模型");
      addModel.addEventListener("click", () => {
        provider.models.push({id: id(), name: "", model: "", thinking: "auto", timeout_seconds: 120,
          profile: provider.kind, profile_model: "", output_mode: "prompted", supports_json_schema: false,
          supports_json_object: true, tool_choice_required: false, temperature: null, max_tokens: null, retries: 1});
        renderProviders(); renderBindings(); notify();
      });
      card.appendChild(addModel);
      providers.appendChild(card);
    }
  }

  root.appendChild(bindings);
  root.appendChild(providers);
  const addProvider = el("button", {type: "button", class: "btn btn-sm secondary"}, "添加 Provider");
  addProvider.addEventListener("click", () => {
    draft.providers.push({id: id(), name: "", kind: "openai", base_url: "", api_key: null, models: []});
    renderProviders(); renderBindings(); notify();
  });
  root.appendChild(addProvider);
  root.appendChild(status);
  renderProviders(); renderBindings();
  return {control: root, root, read: () => catalogPayload(draft)};
}
