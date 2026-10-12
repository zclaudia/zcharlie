// 设置页（settings-plan S5）：按分组编辑运行参数（DB 保存，无需重启）、密钥只写不读、
// LLM / iFinD 测试连接、cron 下次触发时间、变更历史与启动配置只读视图。
// 所有 DOM 经 dom.js 构造（textContent / setAttribute），不写 HTML 字符串。
// 校验以服务端为准；客户端只做范围 / 必填预检，服务端 issues 逐项回显。

import { ApiError, get, lockWhileBusy, patch, post, renderRegion } from "./api.js";
import { clear, el, kvTable, table } from "./dom.js";
import { formatDateTime, label } from "./format.js";
import { localTime } from "./health.js";

export const SETTINGS_REGIONS = new Set(["settings-form", "settings-history", "settings-bootstrap"]);

const ENDPOINT = "/settings";
const HISTORY_ENDPOINT = "/settings/history?limit=30";

export const APPLY_LABELS = Object.freeze({
  live: "立即生效",
  next_job: "下个任务生效",
  reschedule: "重新排期",
});

const APPLY_TITLES = Object.freeze({
  live: "下一次读取即使用新值",
  next_job: "worker 在下一个任务开始前加载新值",
  reschedule: "调度器在 1 分钟内按新配置重建任务",
});

export const ACTOR_LABELS = Object.freeze({
  web: "网页",
  api: "API",
  cli: "命令行",
  import: "首次导入",
});

const KEY_GENERATOR = 'python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"';

// ---- 纯函数（无 DOM，可在 node 中测试） ----------------------------------

function boundText(value, exclusive, low) {
  if (typeof value !== "number") return low ? "(-∞" : "+∞)";
  return low ? `${exclusive ? "(" : "["}${value}` : `${value}${exclusive ? ")" : "]"}`;
}

// "范围 [1, 365]" / "范围 (0, 600]"；无约束时为空串。
export function boundsText(meta) {
  if (!meta || (typeof meta.min !== "number" && typeof meta.max !== "number")) return "";
  return `范围 ${boundText(meta.min, meta.min_exclusive, true)}, ${boundText(meta.max, meta.max_exclusive, false)}`;
}

export function sameValue(a, b) {
  return JSON.stringify(a ?? null) === JSON.stringify(b ?? null);
}

export function formatSettingValue(value) {
  if (value === null || value === undefined) return "（停用 / 空）";
  if (Array.isArray(value)) return value.length ? value.join("、") : "（空）";
  if (typeof value === "boolean") return value ? "是" : "否";
  if (value === "") return "（空）";
  return String(value);
}

// 星期字段写数字时提示（0 / 7 = 周日，易与 APScheduler 的周一 = 0 混淆）。
export function cronWarning(raw) {
  if (typeof raw !== "string") return "";
  const parts = raw.trim().split(/\s+/);
  if (parts.length !== 5) return "";
  return /\d/.test(parts[4]) ? "星期字段写了数字（0 / 7 = 周日）；建议改用 sun / mon 等名称，避免误读。" : "";
}

// 把输入框的原始值解析为提交值：{ value } 或 { error }。
export function parseSettingInput(meta, raw) {
  const type = meta && meta.type;
  if (typeof raw === "boolean") return {value: raw};
  if (type === "int" || type === "float") {
    const text = String(raw ?? "").trim();
    if (!text) return { error: "必填" };
    const value = Number(text);
    if (!Number.isFinite(value)) return { error: "不是有效数字" };
    if (type === "int" && !Number.isInteger(value)) return { error: "须为整数" };
    if (typeof meta.min === "number" && (meta.min_exclusive ? value <= meta.min : value < meta.min)) {
      return { error: `超出${boundsText(meta)}` };
    }
    if (typeof meta.max === "number" && (meta.max_exclusive ? value >= meta.max : value > meta.max)) {
      return { error: `超出${boundsText(meta)}` };
    }
    return { value };
  }
  if (type === "multi") {
    const values = Array.isArray(raw) ? raw : [];
    if (typeof meta.min_items === "number" && values.length < meta.min_items) {
      return { error: `至少选择 ${meta.min_items} 项` };
    }
    return { value: values };
  }
  if (type === "select") {
    if (Array.isArray(meta.options) && !meta.options.includes(raw)) return { error: "无效选项" };
    return { value: raw };
  }
  const text = String(raw ?? "").trim();
  if (type === "cron") {
    if (!text) return meta.nullable ? { value: null } : { error: "必填" };
    if (text.split(/\s+/).length !== 5) return { error: "cron 须为 5 段：分 时 日 月 周" };
    return { value: text };
  }
  if ((type === "url" || type === "timezone") && !text) return { error: "必填" };
  if (type === "url" && !/^https?:\/\/\S+$/i.test(text)) return { error: "须为 http(s):// 开头的地址" };
  return { value: text };
}

// 服务端 422 detail → { byKey: {key: [msg]}, general: [msg] }。
export function splitIssues(detail, knownKeys) {
  const byKey = {};
  const general = [];
  if (detail && typeof detail === "object" && Array.isArray(detail.issues)) {
    for (const issue of detail.issues) {
      const messages = Array.isArray(issue.messages) ? issue.messages.map(String) : [];
      if (knownKeys.has(issue.key)) byKey[issue.key] = messages;
      else general.push(`${issue.key}：${messages.join("；")}`);
    }
  } else if (typeof detail === "string") {
    general.push(detail);
  }
  return { byKey, general };
}

export function secretStatusText(item) {
  if (item.decrypt_error) return "已设置，但无法解密（主密钥缺失或已更换）";
  if (item.configured) return item.hint ? `已设置 ···${item.hint}` : "已设置";
  if (item.env_fallback) return item.hint ? `未保存到数据库，使用环境变量 ···${item.hint}` : "未保存到数据库，使用环境变量";
  return "未设置";
}

// ---- 页面状态 ---------------------------------------------------------------

const INVALID = Symbol("invalid");

const state = {
  data: null,
  items: new Map(), // key -> item
  edits: new Map(), // key -> value | INVALID
  nodes: new Map(), // key -> { root, issues, read, setDirty }
  cleared: new Set(), // secret keys marked for clearing
};

function redirectOn401(error) {
  if (error instanceof ApiError && error.status === 401) {
    globalThis.location.href = "/app/login";
    return true;
  }
  return false;
}

function inputId(key) {
  return `setting-${key.replaceAll(".", "-")}`;
}

function dirtyCount() {
  return state.edits.size;
}

function updateSavebar() {
  const count = dirtyCount();
  const counter = document.getElementById("settings-dirty-count");
  const save = document.getElementById("settings-save");
  const discard = document.getElementById("settings-discard");
  if (counter) {
    counter.textContent = count ? `未保存 ${count} 项` : "没有未保存的修改";
    counter.classList.toggle("has-dirty", count > 0);
  }
  if (save) save.disabled = count === 0;
  if (discard) discard.disabled = count === 0;
}

function showIssues(key, messages) {
  const node = state.nodes.get(key);
  if (!node) return;
  clear(node.issues);
  for (const message of messages || []) node.issues.appendChild(el("li", {}, message));
  node.control?.setAttribute("aria-invalid", messages && messages.length ? "true" : "false");
}

function onEdit(key) {
  const item = state.items.get(key);
  const node = state.nodes.get(key);
  if (!item || !node) return;
  if (item.secret) {
    const raw = node.read();
    if (state.cleared.has(key)) state.edits.set(key, "");
    else if (raw) state.edits.set(key, raw);
    else state.edits.delete(key);
    showIssues(key, []);
  } else {
    const parsed = parseSettingInput(item.input, node.read());
    if (parsed.error) {
      state.edits.set(key, INVALID);
      showIssues(key, [parsed.error]);
    } else {
      showIssues(key, []);
      if (sameValue(parsed.value, item.value)) state.edits.delete(key);
      else state.edits.set(key, parsed.value);
    }
  }
  node.setDirty(state.edits.has(key));
  updateSavebar();
}

// ---- 控件 -------------------------------------------------------------------

function numberControl(item) {
  const meta = item.input;
  const input = el("input", {
    id: inputId(item.key),
    type: "number",
    inputmode: meta.type === "int" ? "numeric" : "decimal",
    step: meta.type === "int" ? "1" : "any",
    min: typeof meta.min === "number" && !meta.min_exclusive ? meta.min : undefined,
    max: typeof meta.max === "number" && !meta.max_exclusive ? meta.max : undefined,
  });
  input.value = item.value === null || item.value === undefined ? "" : String(item.value);
  return { control: input, root: input, read: () => input.value };
}

function selectControl(item) {
  const select = el("select", { id: inputId(item.key) });
  for (const option of item.input.options || []) {
    const node = el("option", { value: option }, option);
    if (option === item.value) node.selected = true;
    select.appendChild(node);
  }
  return { control: select, root: select, read: () => select.value };
}

function multiControl(item) {
  const box = el("fieldset", { class: "setting-multi", id: inputId(item.key) });
  box.appendChild(el("legend", { class: "visually-hidden" }, item.label));
  const current = new Set(Array.isArray(item.value) ? item.value : []);
  const boxes = [];
  for (const option of item.input.options || []) {
    const wrap = el("label", { class: "setting-check" });
    const check = el("input", { type: "checkbox", value: option });
    check.checked = current.has(option);
    boxes.push(check);
    wrap.appendChild(check);
    wrap.appendChild(document.createTextNode(` ${option}`));
    box.appendChild(wrap);
  }
  return {
    control: box,
    root: box,
    read: () => boxes.filter((check) => check.checked).map((check) => check.value),
  };
}

function textControl(item) {
  const type = item.input.type;
  const input = el("input", {
    id: inputId(item.key),
    type: type === "url" ? "url" : "text",
    class: type === "cron" ? "mono" : undefined,
    spellcheck: "false",
    autocomplete: "off",
    placeholder: type === "cron" ? "留空 = 停用，例如 0 20 * * sun" : undefined,
  });
  input.value = item.value === null || item.value === undefined ? "" : String(item.value);
  return { control: input, root: input, read: () => input.value };
}

function secretControl(item, encryptionReady) {
  const wrap = el("div", { class: "setting-secret" });
  const input = el("input", {
    id: inputId(item.key),
    type: "password",
    autocomplete: "new-password",
    spellcheck: "false",
    placeholder: encryptionReady ? "留空 = 不修改" : "需先配置 SETTINGS_ENCRYPTION_KEY",
    disabled: encryptionReady ? undefined : "disabled",
  });
  wrap.appendChild(input);
  const status = el("span", { class: `secret-status${item.decrypt_error ? " is-error" : ""}` }, secretStatusText(item));
  if (item.decrypt_error) status.setAttribute("title", item.decrypt_error);
  wrap.appendChild(status);
  let clearButton = null;
  if (item.configured || item.env_fallback) {
    clearButton = el("button", { type: "button", class: "danger small-button" }, "清除");
    clearButton.addEventListener("click", () => {
      if (state.cleared.has(item.key)) {
        state.cleared.delete(item.key);
        clearButton.textContent = "清除";
        input.disabled = !encryptionReady;
      } else {
        state.cleared.add(item.key);
        input.value = "";
        input.disabled = true;
        clearButton.textContent = "撤销清除";
      }
      onEdit(item.key);
    });
    wrap.appendChild(clearButton);
  }
  return { control: input, root: wrap, read: () => input.value, input };
}

function controlFor(item, encryptionReady) {
  const type = item.input && item.input.type;
  if (item.secret) return secretControl(item, encryptionReady);
  if (typeof item.default === "boolean") {
    const control = el("input", {id: inputId(item.key), type: "checkbox"});
    control.checked = Boolean(item.value);
    return {control, root: control, read: () => control.checked};
  }
  if (type === "int" || type === "float") return numberControl(item);
  if (type === "select") return selectControl(item);
  if (type === "multi") return multiControl(item);
  return textControl(item);
}

function metaLine(item) {
  const parts = [];
  if (!item.secret) parts.push(`默认：${formatSettingValue(item.default)}`);
  const bounds = boundsText(item.input);
  if (bounds) parts.push(bounds);
  if (item.input && item.input.type === "cron") {
    if (item.value === null || item.value === undefined || item.value === "") parts.push("已停用");
    else if (item.next_run) parts.push(`下次触发：${formatDateTime(item.next_run)}（${item.timezone || ""}）`);
  }
  return parts.join(" · ");
}

function settingRow(item, encryptionReady) {
  const root = el("div", { class: "setting", "data-key": item.key });
  const head = el("div", { class: "setting-head" });
  head.appendChild(el("label", { for: inputId(item.key), class: "setting-label" }, item.label));
  head.appendChild(
    el("span", { class: `badge apply-${item.apply}`, title: label(APPLY_TITLES, item.apply) }, label(APPLY_LABELS, item.apply))
  );
  if (!item.is_default) {
    head.appendChild(el("span", { class: "badge accent", title: "当前值与默认值不同" }, "已修改"));
  }
  const pending = el("span", { class: "badge warn setting-pending", hidden: "hidden" }, "未保存");
  head.appendChild(pending);
  root.appendChild(head);
  root.appendChild(el("p", { class: "setting-help faint" }, item.help));

  const control = controlFor(item, encryptionReady);
  root.appendChild(control.root);
  const meta = el("p", { class: "setting-meta faint" }, metaLine(item));
  root.appendChild(meta);
  const warning = el("p", { class: "setting-warning faint", hidden: "hidden" });
  root.appendChild(warning);
  const issues = el("ul", { class: "issue-list setting-issues issues" });
  root.appendChild(issues);

  const isCron = item.input && item.input.type === "cron";
  const refreshCronHints = () => {
    if (!isCron) return;
    const text = cronWarning(control.read());
    warning.textContent = text;
    warning.hidden = !text;
  };
  refreshCronHints();

  const node = {
    root,
    issues,
    control: control.control,
    read: control.read,
    setDirty(dirty) {
      root.classList.toggle("dirty", dirty);
      pending.hidden = !dirty;
      if (isCron) {
        meta.textContent = dirty ? `${metaLine({ ...item, next_run: null })} · 保存后重新计算下次触发时间` : metaLine(item);
      }
    },
  };
  state.nodes.set(item.key, node);
  const events = control.control.tagName === "SELECT" || control.control.tagName === "FIELDSET" ? ["change"] : ["input", "change"];
  for (const name of events) {
    control.control.addEventListener(name, () => {
      refreshCronHints();
      onEdit(item.key);
    });
  }
  return root;
}

// ---- 测试连接 ---------------------------------------------------------------

function currentValue(key) {
  const item = state.items.get(key);
  const edit = state.edits.get(key);
  if (edit !== undefined && edit !== INVALID) return edit;
  return item ? item.value : undefined;
}

function connectionTest(group) {
  const config =
    group === "llm"
      ? {
          path: "/settings/test/llm",
          body: () => ({
            base_url: currentValue("llm_tier3_base_url"),
            model: currentValue("llm_tier3_model"),
            api_key: state.cleared.has("llm_tier3_api_key") ? null : state.edits.get("llm_tier3_api_key") || null,
          }),
        }
      : {
          path: "/settings/test/ifind",
          body: () => ({
            url: currentValue("ifind_proxy_url"),
            api_key: state.cleared.has("ifind_proxy_api_key") ? null : state.edits.get("ifind_proxy_api_key") || null,
          }),
        };
  const row = el("div", { class: "settings-test" });
  const button = el("button", { type: "button", class: "secondary" }, "测试连接");
  const result = el("span", { class: "settings-test-result", "aria-live": "polite" });
  button.addEventListener("click", () => {
    result.className = "settings-test-result";
    result.textContent = "测试中…（使用当前输入，未保存的值不会写入）";
    const promise = post(config.path, config.body(), { idempotency: false })
      .then((outcome) => {
        result.classList.add(outcome.ok ? "is-ok" : "is-error");
        const latency = typeof outcome.latency_ms === "number" && outcome.latency_ms > 0 ? `（${outcome.latency_ms} ms）` : "";
        result.textContent = `${outcome.ok ? "成功" : "失败"}：${outcome.message}${latency}`;
      })
      .catch((error) => {
        if (redirectOn401(error)) return;
        result.classList.add("is-error");
        result.textContent = `测试请求失败：${error.message}`;
      });
    lockWhileBusy(button, promise);
  });
  row.appendChild(button);
  row.appendChild(result);
  return row;
}

// 手机网关连接状态（GET /gateway/status）的一行文字与样式；纯函数，可在 node 中测试。
export function gatewayStatusText(status) {
  const detail = status.detail ? `：${status.detail}` : "";
  switch (status.state) {
    case "connected":
      return { text: `已连接，服务 ID ${status.backend_id}（命名空间 ${status.namespace}）`, ok: true };
    case "connecting":
      return { text: "连接中", ok: false };
    case "incomplete":
      return { text: `已启用但配置不完整${detail}`, ok: false };
    case "error":
      return { text: `读取配置失败${detail}`, ok: false };
    default:
      return { text: "未启用", ok: false, neutral: true };
  }
}

function gatewayStatusLine() {
  const line = el("p", { class: "notice notice-loading" }, "连接状态：读取中…");
  get("/gateway/status")
    .then((status) => {
      const view = gatewayStatusText(status);
      line.textContent = `连接状态：${view.text}`;
      line.className = view.ok || view.neutral ? "notice" : "notice notice-warn";
    })
    .catch((error) => {
      if (redirectOn401(error)) return;
      line.className = "notice notice-warn";
      line.textContent = `连接状态读取失败：${error.message}`;
    });
  return line;
}

function groupNotices(group) {
  const notices = [];
  if (group.key === "gateway") {
    notices.push(gatewayStatusLine());
    notices.push(
      el(
        "p",
        { class: "faint" },
        "手机持网关签发的设备凭证访问；本服务的 API Key 只保存在这里，不会下发到手机。保存后约 10 秒内自动连接 / 断开 / 换凭证。"
      )
    );
  }
  if (group.key === "llm") {
    const key = group.items.find((item) => item.key === "llm_tier3_api_key");
    if (key && key.decrypt_error) {
      notices.push(el("p", { class: "notice notice-warn" }, `已保存的 API Key 无法解密：${key.decrypt_error}`));
    } else if (key && !key.configured && !key.env_fallback) {
      notices.push(el("p", { class: "notice notice-warn" }, "未配置大模型 API Key：研究解释与规则草拟会直接失败。"));
    }
  }
  if (group.key === "notifications") {
    notices.push(el("p", {class: "faint"}, "默认只在站内提醒。明确启用后，仅向你保存的 HTTPS 地址发送新变化的状态及引用；私有提示词和原文不会发送。关闭、静音或暂停会抑制尚未发送的提醒，恢复后不会补发历史提醒。"));
    notices.push(el("p", {class: "faint"}, "确认未知时，只有接收端保证 Idempotency-Key 去重才会重试同一份内容；不会重新研究或生成观点。"));
  }
  if (group.key === "schedule") {
    notices.push(
      el(
        "p",
        { class: "faint" },
        "cron 为标准 5 段（分 时 日 月 周），留空 = 停用该任务。星期请写 sun / mon / … / sat 名称，数字 0 和 7 都表示周日。"
      )
    );
  }
  return notices;
}

// ---- 渲染 ---------------------------------------------------------------------

function renderEncryptionNotice(data) {
  const box = document.getElementById("settings-encryption");
  if (!box) return;
  clear(box);
  if (data.encryption_key_configured) {
    box.hidden = true;
    return;
  }
  box.hidden = false;
  box.appendChild(
    el(
      "p",
      {},
      "未配置可用的 SETTINGS_ENCRYPTION_KEY：API Key 类配置无法保存（输入框已禁用，其他配置不受影响）。"
    )
  );
  if (data.encryption_key_error) box.appendChild(el("p", { class: "faint" }, data.encryption_key_error));
  const how = el("p", {}, "生成主密钥：");
  how.appendChild(el("code", {}, KEY_GENERATOR));
  box.appendChild(how);
  box.appendChild(
    el("p", { class: "faint" }, "在 api / worker / scheduler 的环境变量（.env）中设置后重启；放在数据目录之外并妥善备份，丢失后已存密钥无法恢复。")
  );
}

function renderForm(region, data) {
  state.data = data;
  state.items = new Map();
  state.edits = new Map();
  state.nodes = new Map();
  state.cleared = new Set();
  renderEncryptionNotice(data);
  const encryptionReady = Boolean(data.encryption_key_configured);
  const root = el("div", { class: "settings-groups" });
  for (const group of data.groups || []) {
    const card = el("section", { class: "card settings-group", "data-group": group.key });
    const head = el("div", { class: "card-head" });
    head.appendChild(el("h2", {}, group.title));
    head.appendChild(el("span", { class: "faint" }, `${group.items.length} 项`));
    card.appendChild(head);
    if (group.key === "agent") {
      card.id = "agent-settings";
      card.appendChild(el("p", {class: "notice"}, "Agent 模型与旧解释模型独立。保存仅更新配置，不代表供应商网络或研究质量已经验证；后台运行开关与投资约定授权须分别明确开启。"));
      card.appendChild(el("a", {href: "/app/agent"}, "查看投资约定、USD 金额上限与本地 readiness / 当前配置指纹"));
    }
    for (const notice of groupNotices(group)) card.appendChild(notice);
    if (group.key === "llm" || group.key === "ifind") card.appendChild(connectionTest(group.key));
    const list = el("div", { class: "setting-list" });
    const advanced = el("details");
    advanced.appendChild(el("summary", {}, "高级模型参数与执行限额"));
    const basicAgent = new Set(["agent_provider", "agent_base_url", "agent_model", "agent_api_key",
      "agent_profile", "agent_profile_model", "agent_output_mode", "investor_agent_enabled",
      "investor_agent_background_enabled", "investor_agent_input_usd_per_million",
      "investor_agent_output_usd_per_million", "investor_agent_price_model",
      "investor_agent_price_version", "investor_agent_price_config_version"]);
    for (const item of group.items) {
      state.items.set(item.key, item);
      const row = settingRow(item, encryptionReady);
      if (group.key === "agent" && !basicAgent.has(item.key)) advanced.appendChild(row);
      else list.appendChild(row);
    }
    if (group.key === "agent") list.appendChild(advanced);
    card.appendChild(list);
    root.appendChild(card);
  }
  renderRegion(region, "ready", root);
  const bar = document.getElementById("settings-savebar");
  if (bar) bar.hidden = false;
  updateSavebar();
  renderBootstrap(data.bootstrap || []);
}

function renderBootstrap(items) {
  const region = document.getElementById("settings-bootstrap");
  if (!region) return;
  const pairs = items.map((item) => [
    `${item.label}（${item.key}）`,
    item.secret
      ? item.configured
        ? item.hint
          ? `已设置 ···${item.hint}`
          : "已设置"
        : "未设置"
      : formatSettingValue(item.value),
  ]);
  renderRegion(region, "ready", kvTable(pairs));
}

function historyValue(value) {
  return el("td", { class: "mono setting-history-value" }, formatSettingValue(value));
}

function renderHistory(region, payload) {
  const items = (payload && payload.items) || [];
  if (!items.length) {
    renderRegion(region, "empty");
    return;
  }
  const rows = items.map((change) => {
    const tr = el("tr");
    tr.appendChild(el("td", {}, localTime(change.changed_at)));
    const name = el("td");
    name.appendChild(el("div", {}, change.label || change.key));
    name.appendChild(el("div", { class: "faint mono" }, change.key));
    tr.appendChild(name);
    tr.appendChild(historyValue(change.old_value));
    tr.appendChild(historyValue(change.new_value));
    tr.appendChild(el("td", {}, label(ACTOR_LABELS, change.actor)));
    tr.appendChild(el("td", {}, change.note || "—"));
    return tr;
  });
  const wrap = el("div", { class: "table-wrap" });
  wrap.appendChild(
    table(
      [{ label: "时间" }, { label: "配置项" }, { label: "原值" }, { label: "新值" }, { label: "来源" }, { label: "说明" }],
      rows
    )
  );
  renderRegion(region, "ready", wrap);
}

async function loadHistory() {
  const region = document.getElementById("settings-history");
  if (!region) return;
  try {
    renderHistory(region, await get(HISTORY_ENDPOINT));
  } catch (error) {
    if (redirectOn401(error)) return;
    renderRegion(region, "error", error.message);
  }
}

async function loadForm() {
  const region = document.getElementById("settings-form");
  if (!region) return;
  renderRegion(region, "loading");
  try {
    renderForm(region, await get(ENDPOINT));
  } catch (error) {
    if (redirectOn401(error)) return;
    renderRegion(region, "error", error.message);
    const bootstrap = document.getElementById("settings-bootstrap");
    if (bootstrap) renderRegion(bootstrap, "error", error.message);
  }
}

// ---- 保存 / 放弃 ------------------------------------------------------------

function setSaveResult(lines, kind) {
  const box = document.getElementById("settings-save-result");
  if (!box) return;
  clear(box);
  if (!lines.length) return;
  const notice = el("div", { class: `notice${kind ? ` notice-${kind}` : ""}` });
  if (lines.length === 1) notice.textContent = lines[0];
  else {
    const list = el("ul", { class: "issue-list" });
    for (const line of lines) list.appendChild(el("li", {}, line));
    notice.appendChild(list);
  }
  box.appendChild(notice);
}

async function save(button) {
  const invalid = [...state.edits].filter(([, value]) => value === INVALID).map(([key]) => key);
  if (invalid.length) {
    setSaveResult([`有 ${invalid.length} 项输入不合法，请先修正。`], "warn");
    state.nodes.get(invalid[0])?.control?.focus();
    return;
  }
  const values = Object.fromEntries(state.edits);
  const noteInput = document.getElementById("settings-note");
  const note = noteInput && noteInput.value.trim() ? noteInput.value.trim() : null;
  const promise = patch(ENDPOINT, { values, note })
    .then((result) => {
      const region = document.getElementById("settings-form");
      renderForm(region, result.settings);
      if (noteInput) noteInput.value = "";
      const changed = result.changed || [];
      const labels = changed.map((key) => state.items.get(key)?.label || key);
      const lines = [changed.length ? `已保存 ${changed.length} 项：${labels.join("、")}。` : "已保存（值未变化）。"];
      if (changed.some((key) => state.items.get(key)?.apply === "reschedule")) {
        lines.push("调度器将在 1 分钟内按新配置重新排期。");
      }
      if (changed.some((key) => state.items.get(key)?.apply === "next_job")) {
        lines.push("worker 将在下一个任务开始前加载新值。");
      }
      setSaveResult(lines, "");
      loadHistory();
    })
    .catch((error) => {
      if (redirectOn401(error)) return;
      if (error instanceof ApiError && error.status === 422) {
        const { byKey, general } = splitIssues(error.detail, new Set(state.items.keys()));
        for (const [key, messages] of Object.entries(byKey)) showIssues(key, messages);
        const count = Object.keys(byKey).length;
        const lines = [];
        if (count) lines.push(`服务端校验未通过：${count} 项（见各项下方提示），未保存任何修改。`);
        setSaveResult([...lines, ...general], "warn");
        const first = Object.keys(byKey)[0];
        if (first) state.nodes.get(first)?.control?.focus();
      } else {
        setSaveResult([`保存失败：${error.message}`], "error");
      }
    });
  lockWhileBusy(button, promise);
  await promise;
  updateSavebar();
}

function discard() {
  const region = document.getElementById("settings-form");
  if (region && state.data) renderForm(region, state.data);
  setSaveResult([], "");
}

export function bootSettings() {
  const region = document.getElementById("settings-form");
  if (!region) return;
  const saveButton = document.getElementById("settings-save");
  if (saveButton) saveButton.addEventListener("click", () => save(saveButton));
  const discardButton = document.getElementById("settings-discard");
  if (discardButton) discardButton.addEventListener("click", discard);
  globalThis.addEventListener("beforeunload", (event) => {
    if (dirtyCount()) event.preventDefault();
  });
  loadForm();
  loadHistory();
}
