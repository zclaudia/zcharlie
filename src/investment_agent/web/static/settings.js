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
  raw: new Map(), // key -> control value, survives leaving the topic
  sections: [],
  place: { sectionId: null, topicId: null },
  query: "",
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

function placeTopic() {
  const section = state.sections.find((item) => item.id === state.place.sectionId) || null;
  const topic = section ? section.topics.find((item) => item.id === state.place.topicId) || null : null;
  return { section, topic };
}

function topicDirtyKeys(topic) {
  if (!topic) return [];
  return topicKeys(topic).filter((key) => state.edits.has(key));
}

function sectionDirtyCount(section) {
  const keys = new Set(section.topics.flatMap((topic) => topicKeys(topic)));
  let count = 0;
  for (const key of state.edits.keys()) if (keys.has(key)) count += 1;
  return count;
}

function updateSavebar() {
  const { topic } = placeTopic();
  const count = topic ? topicDirtyKeys(topic).length : 0;
  const counter = document.getElementById("settings-dirty-count");
  const save = document.getElementById("settings-save");
  const discard = document.getElementById("settings-discard");
  const bar = document.getElementById("settings-savebar");
  const result = document.getElementById("settings-save-result");
  const hasResult = Boolean(result && result.textContent.trim());
  if (counter) {
    counter.textContent = count ? `未保存 ${count} 项` : "没有未保存的修改";
    counter.classList.toggle("has-dirty", count > 0);
  }
  if (save) save.disabled = count === 0;
  if (discard) discard.disabled = count === 0;
  if (bar) bar.hidden = count === 0 && !hasResult;
}

function refreshDirtyLabels() {
  renderNav();
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
  state.raw.set(key, node.read());
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
  refreshDirtyLabels();
}

// ---- 控件 -------------------------------------------------------------------

function remembered(item) {
  return state.raw.has(item.key) ? state.raw.get(item.key) : item.value;
}

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
  const value = remembered(item);
  input.value = value === null || value === undefined ? "" : String(value);
  return { control: input, root: input, read: () => input.value };
}

function selectControl(item) {
  const select = el("select", { id: inputId(item.key) });
  const value = remembered(item);
  for (const option of item.input.options || []) {
    const node = el("option", { value: option }, option);
    if (option === value) node.selected = true;
    select.appendChild(node);
  }
  return { control: select, root: select, read: () => select.value };
}

function multiControl(item) {
  const box = el("fieldset", { class: "setting-multi", id: inputId(item.key) });
  box.appendChild(el("legend", { class: "visually-hidden" }, item.label));
  const rememberedValue = remembered(item);
  const current = new Set(Array.isArray(rememberedValue) ? rememberedValue : []);
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
  const value = remembered(item);
  input.value = value === null || value === undefined ? "" : String(value);
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
  const seeded = state.raw.get(item.key);
  if (state.cleared.has(item.key)) input.disabled = true;
  else if (typeof seeded === "string" && seeded) input.value = seeded;
  wrap.appendChild(input);
  const status = el("span", { class: `secret-status${item.decrypt_error ? " is-error" : ""}` }, secretStatusText(item));
  if (item.decrypt_error) status.setAttribute("title", item.decrypt_error);
  wrap.appendChild(status);
  let clearButton = null;
  if (item.configured || item.env_fallback) {
    clearButton = el("button", { type: "button", class: "btn btn-sm btn-danger-ghost danger small-button" }, "清除");
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
    if (state.cleared.has(item.key)) clearButton.textContent = "撤销清除";
    wrap.appendChild(clearButton);
  }
  return { control: input, root: wrap, read: () => input.value, input };
}

function controlFor(item, encryptionReady) {
  const type = item.input && item.input.type;
  if (item.secret) return secretControl(item, encryptionReady);
  if (typeof item.default === "boolean") {
    const control = el("input", {id: inputId(item.key), type: "checkbox"});
    control.checked = Boolean(remembered(item));
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
  const control = controlFor(item, encryptionReady);
  const isSwitch = control.control && control.control.type === "checkbox";
  const root = el("div", { class: `setting set-row${isSwitch ? " is-switch" : ""}`, "data-key": item.key });
  const left = el("div", { class: "set-k" });
  left.appendChild(el("label", { for: inputId(item.key), class: "setting-label" }, item.label));
  left.appendChild(el("p", { class: "setting-help faint" }, item.help));
  const meta = el("p", { class: "setting-meta faint" }, metaLine(item));
  left.appendChild(meta);
  const warning = el("p", { class: "setting-warning faint", hidden: "hidden" });
  left.appendChild(warning);
  root.appendChild(left);

  const right = el("div", { class: "set-c" });
  right.appendChild(control.root);
  const issues = el("ul", { class: "issue-list setting-issues issues" });
  right.appendChild(issues);
  root.appendChild(right);

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
      if (isCron) {
        meta.textContent = dirty ? `${metaLine({ ...item, next_run: null })} · 保存后重新计算下次触发时间` : metaLine(item);
      }
    },
  };
  state.nodes.set(item.key, node);
  node.setDirty(state.edits.has(item.key));
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
  const row = el("div", { class: "settings-test settings-foot" });
  const button = el("button", { type: "button", class: "btn btn-sm secondary" }, "测试连接");
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
      return { text: `连接中${detail}`, ok: false };
    case "replaced":
      return { text: `服务身份被接管${detail}`, ok: false };
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

// 目录 → 主题 → 字段。未列入的新配置落到该分组的「其他」，避免被藏起来。

const NAV_GROUPS = ["研究", "数据", "运行", "系统", "其他"];
const CRON_SUMMARIES = Object.freeze({
  "0 8 * * 1": "每周一 08:00",
  "0 8 * * *": "每天 08:00",
  "30 18 * * 1-5": "工作日 18:30",
  "10 15 * * 1-5": "工作日 15:10",
  "0 16 * * 1-5": "工作日 16:00",
  "0 9 * * 6": "周六 09:00",
  "0 19 * * *": "每天 19:00",
  "0 19 * * 1-5": "工作日 19:00",
  "0 20 * * sun": "周日 20:00",
  "0 7 * * *": "每天 07:00",
  "*/30 * * * *": "每 30 分钟",
  "0 21 * * 0": "周日 21:00",
});
const SNAPSHOT_TZ = "schedule.snapshot.timezone";
const VERIFY_TZ = "schedule.verification.timezone";

function blocks(title, keys) {
  return { title, keys };
}

function job(id, title, note, keys) {
  return { id, title, note, keys };
}

export const SETTINGS_SECTIONS = Object.freeze([
  {
    id: "agent",
    navGroup: "研究",
    groups: ["agent"],
    title: "投资 Agent",
    blurb: "模型、价格、开关分开保存。保存只更新配置，不代表供应商或研究质量已经验证。",
    topicGroups: [
      { label: "模型与计费", topicIds: ["model", "price"] },
      { label: "能不能跑", topicIds: ["switches", "quota"] },
      { label: "不常改", topicIds: ["advanced"] },
    ],
    links: [{ href: "/app/agent", title: "投资约定", detail: "金额上限、readiness 与当前配置指纹" }],
    topics: [
      {
        id: "switches",
        title: "开关",
        note: "后台研究和投资约定仍要各自明确打开。",
        keys: [
          "investor_agent_enabled",
          "investor_agent_background_enabled",
          "disclosure_originals_enabled",
          "portfolio_ledger_enabled",
          "account_fit_enabled",
          "candidate_ranking_enabled",
          "supplemental_rules_enabled",
          "portfolio_valuation_max_staleness_days",
        ],
      },
      {
        id: "model",
        title: "模型",
        note: "与解释模型独立。密钥加密保存，只写不读。",
        keys: ["agent_provider", "agent_base_url", "agent_model", "agent_api_key", "agent_profile", "agent_profile_model", "agent_output_mode"],
      },
      {
        id: "price",
        title: "计费核验",
        note: "价格未知就留空，计费保持停用。绑定模型必须和执行模型相同。",
        keys: [
          "investor_agent_input_usd_per_million",
          "investor_agent_output_usd_per_million",
          "investor_agent_price_model",
          "investor_agent_price_version",
          "investor_agent_price_config_version",
        ],
      },
      {
        id: "quota",
        title: "额度",
        note: "仍与投资约定取较小值。每批上限不是产出目标。",
        blocks: [
          blocks("单任务", ["investor_agent_task_requests", "investor_agent_task_tokens", "investor_agent_task_seconds", "investor_agent_task_tool_calls"]),
          blocks("每日", ["investor_agent_daily_requests", "investor_agent_daily_tokens", "investor_agent_daily_tool_calls"]),
          blocks("每批", ["investor_agent_max_leads_per_batch", "investor_agent_max_new_tasks_per_batch"]),
        ],
      },
      {
        id: "advanced",
        title: "队列与端点",
        note: "队列满时拒绝新任务，不重建已有任务。",
        blocks: [
          blocks("队列", ["agent_queue_max_running", "agent_queue_interactive_capacity", "agent_queue_review_capacity", "agent_queue_exploration_capacity"]),
          blocks("端点", [
            "agent_credential_ref", "agent_supports_json_schema", "agent_supports_json_object", "agent_tool_choice_required",
            "agent_thinking", "agent_timeout_seconds", "agent_temperature", "agent_max_tokens", "agent_retries",
          ]),
        ],
      },
    ],
  },
  {
    id: "llm",
    navGroup: "研究",
    groups: ["llm"],
    title: "解释模型",
    blurb: "研究解释和规则草拟用这一组，与投资 Agent 的模型无关。",
    topics: [{
      id: "llm",
      title: "解释模型",
      note: "未配置 Key 时，解释和规则草拟会直接失败。",
      keys: ["llm_tier3_base_url", "llm_tier3_model", "llm_tier3_api_key", "llm_tier3_thinking", "llm_rule_draft_thinking", "llm_request_timeout_seconds"],
    }],
  },
  {
    id: "notify",
    navGroup: "研究",
    groups: ["notifications"],
    title: "通知",
    blurb: "默认只在站内提醒。打开外部推送后，只发送新变化的状态和引用。",
    topics: [{
      id: "notify",
      title: "通知",
      note: "关闭、静音或暂停会抑制还没发出的提醒，恢复后不补发。",
      keys: ["notification_external_enabled", "notification_webhook_url", "notification_webhook_token", "notification_webhook_idempotent"],
    }],
  },
  {
    id: "candidates",
    navGroup: "研究",
    groups: ["candidates"],
    title: "候选池",
    blurb: "未处理多久过期，以及忽略之后多久才能再被提名。过期扫描的时刻在定时任务里。",
    topics: [{
      id: "candidates",
      title: "候选池",
      keys: ["candidate_expiry_days", "candidate_dismiss_cooldown_days"],
    }],
  },
  {
    id: "data",
    navGroup: "数据",
    groups: ["ifind", "collection", "verification", "peers"],
    title: "数据源",
    blurb: "连接、采集速度、核验预算、同行参照。各管一件事。",
    topics: [
      { id: "ifind", title: "iFinD 连接", note: "地址和密钥改完，用这一屏的测试。", keys: ["ifind_proxy_url", "ifind_proxy_api_key"] },
      {
        id: "collection",
        title: "采集",
        keys: ["ifind_collect_rate_per_minute", "ifind_collect_batch_retries", "akshare_min_interval_seconds", "snapshot_completeness_threshold"],
      },
      {
        id: "verification",
        title: "核验预算",
        note: "超出预算的核验任务顺延。派发时刻在定时任务里。",
        keys: ["ifind_budget_per_5h", "ifind_budget_window_hours", "ifind_verify_max_requests"],
      },
      {
        id: "peers",
        title: "同行参照",
        note: "开关立即生效。间隔、重试和冷却在下个任务生效。",
        keys: ["industry_peer_context_enabled", "peer_collect_min_interval_seconds", "peer_collect_retries", "peer_collect_admission_cooldown_seconds"],
      },
    ],
  },
  {
    id: "schedule",
    navGroup: "运行",
    groups: ["schedule"],
    title: "定时任务",
    blurb: "一个任务一行。点进去才改 cron 和时区。留空即停用。",
    topicGroups: [
      { label: "范围", topicIds: ["pools"] },
      { label: "日常", topicIds: ["watchlist", "portfolio", "zone", "expiry"] },
      { label: "行情与财务", topicIds: ["universe", "industry", "market", "close", "close-retry", "financial-week", "financial-deadline", "earnings", "calendar"] },
      { label: "研究", topicIds: ["screen", "dispatch", "calibration"] },
    ],
    topics: [
      job("pools", "股票池", "时区不在这里改，跟在各自的任务里。", ["schedule.snapshot.universes", "schedule.screen.universes"]),
      job("watchlist", "观察池刷新", "留空即停用。", ["schedule.watchlist.cron", "schedule.watchlist.timezone"]),
      job("portfolio", "持仓估值", "留空即停用。", ["schedule.portfolio.cron", "schedule.portfolio.timezone"]),
      job("zone", "区间检测", "留空即停用。", ["schedule.zone_detection.cron", "schedule.zone_detection.timezone"]),
      job("expiry", "候选过期扫描", "每次筛选开始时也会扫一次。", ["schedule.candidates.expiry_sweep_cron", "schedule.candidates.timezone"]),
      job("universe", "成分股同步", "按 cron 触发。", ["schedule.snapshot.universe_sync_cron", SNAPSHOT_TZ]),
      job("industry", "行业同步", "仅在每月首个交易日执行。", ["schedule.snapshot.industry_sync_cron", SNAPSHOT_TZ]),
      job("market", "行情快照", "仅在交易日执行。", ["schedule.snapshot.market_snapshot_cron", SNAPSHOT_TZ]),
      job("close", "收盘快照", "仅在交易日、收盘之后执行。", ["schedule.snapshot.close_quote_cron", SNAPSHOT_TZ]),
      job("close-retry", "收盘补采", "补仍未收盘或未报价的标的。", ["schedule.snapshot.close_quote_retry_cron", SNAPSHOT_TZ]),
      job("financial-week", "财务周采", "按 cron 触发。", ["schedule.snapshot.financial_weekly_cron", SNAPSHOT_TZ]),
      job("financial-deadline", "披露截止后采集", "仅在披露截止日后的首个交易日执行，并检查逾期报告。", ["schedule.snapshot.financial_deadline_cron", SNAPSHOT_TZ]),
      job("earnings", "财报事件", "仅在 1、4、7、10 月的交易日执行。", ["schedule.snapshot.earnings_event_cron", SNAPSHOT_TZ]),
      job("calendar", "交易日历核对", "只在日历到期前 60 天内真正核对。", ["schedule.snapshot.calendar_probe_cron", SNAPSHOT_TZ]),
      job("screen", "每周筛选", "星期建议写 sun、mon。", ["schedule.screen.cron", "schedule.screen.timezone", "schedule.screen.hit_cap"]),
      job("dispatch", "核验派发", "有到期请求时派发一次核验。", ["schedule.verification.dispatch_cron", VERIFY_TZ]),
      job("calibration", "每周校准", "每周抽观察池股票做校准。", ["schedule.verification.calibration_cron", VERIFY_TZ, "schedule.verification.calibration_count"]),
    ],
  },
  {
    id: "gateway",
    navGroup: "运行",
    groups: ["gateway"],
    title: "手机网关",
    blurb: "手机持网关签发的设备凭证进来。本服务的 API Key 不下发。",
    topics: [{
      id: "gateway",
      title: "手机网关",
      note: "保存后大约 10 秒内连接、断开或更换凭证。",
      keys: ["gateway_enabled", "gateway_url", "gateway_credential", "gateway_namespace", "gateway_instance_id"],
    }],
  },
  {
    id: "system",
    navGroup: "系统",
    groups: [],
    title: "记录与启动配置",
    blurb: "变更历史和启动配置都是只读。",
    topics: [
      { id: "history", title: "最近变更", kind: "history", note: "密钥只显示已设置和末 4 位。" },
      { id: "bootstrap", title: "启动配置", kind: "bootstrap", note: "来自环境变量，修改后需重启服务。" },
    ],
  },
]);

export function topicKeys(topic) {
  if (Array.isArray(topic.blocks) && topic.blocks.length) return topic.blocks.flatMap((block) => block.keys || []);
  return topic.keys || [];
}

export function settingsCatalogKeys() {
  return [...new Set(SETTINGS_SECTIONS.flatMap((section) => section.topics.flatMap((topic) => topicKeys(topic))))];
}

export function parseSettingsHash(hash) {
  const raw = String(hash || "").replace(/^#/, "");
  if (raw === "agent-settings") return { sectionId: "agent", topicId: null };
  const [sectionId, topicId] = raw.split("/").filter(Boolean);
  return { sectionId: sectionId || null, topicId: topicId || null };
}

export function resolveSettingsPlace(hash, sections = SETTINGS_SECTIONS) {
  const parsed = parseSettingsHash(hash);
  const section = sections.find((item) => item.id === parsed.sectionId);
  if (!section) return { sectionId: null, topicId: null };
  if (parsed.topicId && section.topics.some((topic) => topic.id === parsed.topicId)) {
    return { sectionId: section.id, topicId: parsed.topicId };
  }
  if (section.topics.length === 1) return { sectionId: section.id, topicId: section.topics[0].id };
  return { sectionId: section.id, topicId: null };
}

export function cronSummary(raw) {
  if (raw === null || raw === undefined || String(raw).trim() === "") return "已停用";
  const text = String(raw).trim();
  return CRON_SUMMARIES[text] || text;
}

function blankValue(value) {
  return value === null || value === undefined || value === "";
}

export function topicSummary(topic, read) {
  const value = (key) => read(key);
  switch (topic.id) {
    case "switches": {
      const toggles = ["investor_agent_enabled", "investor_agent_background_enabled", "disclosure_originals_enabled", "portfolio_ledger_enabled", "account_fit_enabled", "candidate_ranking_enabled", "supplemental_rules_enabled"];
      const on = toggles.filter((key) => value(key) === true).length;
      return on === 0 ? "全部关闭" : `${on} 项开启`;
    }
    case "model":
      return String(value("agent_model") || "").trim() || "未填写";
    case "price":
      return blankValue(value("investor_agent_input_usd_per_million")) ? "为空则停用" : "价格已填";
    case "quota":
      return `每批 ${value("investor_agent_max_leads_per_batch")} 条线索`;
    case "advanced":
      return `并发 ${value("agent_queue_max_running")}`;
    case "llm":
      return String(value("llm_tier3_model") || "").trim() || "未填模型";
    case "notify":
      return value("notification_external_enabled") ? "外部推送" : "仅站内";
    case "candidates":
      return `${value("candidate_expiry_days")} 天过期`;
    case "ifind":
      return String(value("ifind_proxy_url") || "").replace(/^https?:\/\//, "") || "未填写";
    case "collection":
      return `${value("ifind_collect_rate_per_minute")} 次/分钟`;
    case "verification":
      return `${value("ifind_budget_window_hours")} 小时 ${value("ifind_budget_per_5h")} 次`;
    case "peers":
      return value("industry_peer_context_enabled") ? "已开启" : "关闭";
    case "pools": {
      const pools = value("schedule.snapshot.universes");
      return Array.isArray(pools) && pools.length ? pools.join("、") : "未选";
    }
    case "gateway":
      return value("gateway_enabled") ? "已启用" : "未启用";
    case "history":
      return "只读";
    case "bootstrap":
      return "需重启";
    default: {
      const cronKey = topicKeys(topic).find((key) => key.endsWith("cron"));
      return cronKey ? cronSummary(value(cronKey)) : "";
    }
  }
}

export function sectionHome(section, read, lookup) {
  const value = (key) => read(key);
  const row = (key) => lookup(key);
  switch (section.id) {
    case "agent": {
      const model = String(value("agent_model") || "").trim();
      const price = blankValue(value("investor_agent_input_usd_per_million")) ? "价格为空" : "价格已填";
      return { value: value("investor_agent_enabled") === true ? "已开启" : "已关闭", detail: `${model || "模型未填"} · ${price}` };
    }
    case "llm":
      return {
        value: String(value("llm_tier3_model") || "未填模型"),
        detail: row("llm_tier3_api_key")?.configured ? "Key 已设置" : "Key 未配置",
      };
    case "notify":
      return { value: value("notification_external_enabled") ? "外部推送" : "仅站内", detail: "只发新变化的状态和引用" };
    case "data":
      return { value: value("industry_peer_context_enabled") ? "同行已开" : "同行关闭", detail: "连接、采集、核验、同行" };
    case "candidates":
      return { value: `${value("candidate_expiry_days")} 天过期`, detail: `忽略冷却 ${value("candidate_dismiss_cooldown_days")} 天` };
    case "schedule": {
      const jobs = section.topics.filter((topic) => topicKeys(topic).some((key) => key.endsWith("cron")));
      const off = jobs.filter((topic) => blankValue(value(topicKeys(topic).find((key) => key.endsWith("cron"))))).length;
      return { value: `${jobs.length} 个任务`, detail: off ? `${off} 个停用` : "全部已排期" };
    }
    case "gateway":
      return { value: value("gateway_enabled") ? "已启用" : "未启用", detail: "凭证只留在服务端" };
    default:
      return { value: "只读", detail: "变更历史与启动配置" };
  }
}

function materializeSections() {
  const sections = SETTINGS_SECTIONS.map((section) => ({
    ...section,
    topicGroups: section.topicGroups ? section.topicGroups.map((group) => ({ ...group, topicIds: [...group.topicIds] })) : undefined,
    topics: section.topics.map((topic) => ({
      ...topic,
      keys: [...topicKeys(topic)],
      blocks: topic.blocks ? topic.blocks.map((block) => ({ ...block, keys: [...block.keys] })) : undefined,
    })),
  }));
  const placed = new Set(sections.flatMap((section) => section.topics.flatMap((topic) => topic.keys)));
  for (const item of state.items.values()) {
    if (placed.has(item.key)) continue;
    let section = sections.find((entry) => (entry.groups || []).includes(item.group));
    if (!section) {
      section = { id: item.group, navGroup: "其他", title: item.group, blurb: "", groups: [item.group], topics: [] };
      sections.push(section);
    }
    let extra = section.topics.find((topic) => topic.id === "extra");
    if (!extra) {
      extra = { id: "extra", title: "其他", keys: [], note: "这些配置还没有单独的主题。" };
      section.topics.push(extra);
      section.topicGroups = [...(section.topicGroups || []), { label: "其他", topicIds: ["extra"] }];
    }
    extra.keys.push(item.key);
    placed.add(item.key);
  }
  return sections;
}

function indexItems(data) {
  state.items = new Map();
  for (const group of data.groups || []) {
    for (const item of group.items || []) state.items.set(item.key, item);
  }
  state.sections = materializeSections();
}

function readNow(key) {
  return currentValue(key);
}

function shownValue(item) {
  if (item.secret) {
    if (state.cleared.has(item.key)) return "将清除";
    if (state.edits.has(item.key) && state.edits.get(item.key)) return "已填写";
    return secretStatusText(item);
  }
  return formatSettingValue(currentValue(item.key));
}

function jumpRow({ title, detail, value, dirty, href, onClick }) {
  const node = href ? el("a", { class: "list-row settings-jump", href }) : el("button", { type: "button", class: "list-row settings-jump" });
  const text = el("span", { class: "list-row-main settings-jump-text" });
  text.appendChild(el("b", { class: "settings-jump-title" }, title));
  if (detail) text.appendChild(el("small", { class: "settings-jump-detail" }, detail));
  node.appendChild(text);
  if (value) node.appendChild(el("span", { class: dirty ? "list-row-end settings-jump-value is-dirty" : "list-row-end settings-jump-value" }, value));
  node.appendChild(el("span", { class: "list-row-chev settings-jump-chevron", "aria-hidden": "true" }));
  if (onClick) node.addEventListener("click", onClick);
  return node;
}

function groupedTopics(section) {
  if (!section.topicGroups) return [{ label: "", topics: section.topics }];
  return section.topicGroups
    .map((group) => ({
      label: group.label,
      topics: group.topicIds.map((id) => section.topics.find((topic) => topic.id === id)).filter(Boolean),
    }))
    .filter((group) => group.topics.length);
}

function renderDirectory() {
  const root = el("div", { class: "settings-browse" });
  for (const name of NAV_GROUPS) {
    const sections = state.sections.filter((section) => section.navGroup === name);
    if (!sections.length) continue;
    root.appendChild(el("h2", { class: "settings-kicker" }, name));
    const list = el("div", { class: "list-group settings-jump-list" });
    for (const section of sections) {
      const home = sectionHome(section, readNow, (key) => state.items.get(key));
      const dirty = sectionDirtyCount(section);
      list.appendChild(jumpRow({
        title: section.title,
        detail: home.detail,
        value: dirty ? `未保存 ${dirty} 项` : home.value,
        dirty: dirty > 0,
        onClick: () => openPlace(section.id, section.topics.length === 1 ? section.topics[0].id : null),
      }));
    }
    root.appendChild(list);
  }
  return root;
}

function renderTopicList(section) {
  const root = el("div", { class: "settings-topic settings-topic-list" });
  if (section.id === "agent") root.id = "agent-settings";
  root.appendChild(cardHead(section.title, section.blurb));
  const body = el("div", { class: "settings-card-body" });
  root.appendChild(body);
  if (section.id === "schedule") {
    for (const notice of groupNotices({ key: "schedule", items: [] })) body.appendChild(notice);
  }
  for (const group of groupedTopics(section)) {
    if (group.label) body.appendChild(el("h2", { class: "settings-kicker" }, group.label));
    const list = el("div", { class: "list-group settings-jump-list" });
    for (const topic of group.topics) {
      const dirty = topicDirtyKeys(topic).length;
      list.appendChild(jumpRow({
        title: topic.title,
        value: dirty ? `未保存 ${dirty} 项` : topicSummary(topic, readNow),
        dirty: dirty > 0,
        onClick: () => openPlace(section.id, topic.id),
      }));
    }
    body.appendChild(list);
  }
  if (section.links && section.links.length) {
    body.appendChild(el("h2", { class: "settings-kicker" }, "相关"));
    const list = el("div", { class: "list-group settings-jump-list" });
    for (const link of section.links) {
      list.appendChild(jumpRow({ title: link.title, detail: link.detail, href: link.href }));
    }
    body.appendChild(list);
  }
  return root;
}

function topicNoticeKey(topic) {
  if (topic.id === "llm") return "llm";
  if (topic.id === "ifind") return "ifind";
  if (topic.id === "notify") return "notifications";
  if (topic.id === "gateway") return "gateway";
  return null;
}

// 右侧详情卡的头部（桌面显示；手机上标题已在页头，隐藏）：标题 + 生效方式徽章 + 说明。
function cardHead(title, hint, applyKey) {
  const head = el("div", { class: "card-head card-head--rule settings-card-head" });
  head.appendChild(el("h2", {}, title));
  if (applyKey) head.appendChild(el("span", { class: `badge ${applyKey === "live" ? "accent" : "ai"} dot` }, label(APPLY_LABELS, applyKey)));
  if (hint) head.appendChild(el("span", { class: "faint settings-card-hint" }, hint));
  return head;
}

function renderTopic(topic, section) {
  const root = el("div", { class: "settings-topic" });
  if (section.id === "agent") root.id = "agent-settings";
  const keys = topicKeys(topic);
  const applies = new Set(keys.map((key) => state.items.get(key)?.apply).filter(Boolean));
  const apply = applies.size === 1 ? [...applies][0] : null;
  root.appendChild(cardHead(topic.title, apply ? label(APPLY_TITLES, apply) : "", apply));
  const body = el("div", { class: "settings-card-body" });
  root.appendChild(body);
  if (topic.kind === "history" || topic.kind === "bootstrap") {
    if (topic.note) body.appendChild(el("p", { class: "faint" }, topic.note));
    return root;
  }
  if (apply) {
    body.appendChild(el("p", { class: "settings-apply" }, `生效：${label(APPLY_LABELS, apply)}。${label(APPLY_TITLES, apply)}`));
  }
  if (topic.note) body.appendChild(el("p", { class: "faint settings-note" }, topic.note));
  const noticeKey = topicNoticeKey(topic);
  if (noticeKey) {
    const items = keys.map((key) => state.items.get(key)).filter(Boolean);
    for (const notice of groupNotices({ key: noticeKey, items })) body.appendChild(notice);
  }
  const encryptionReady = Boolean(state.data && state.data.encryption_key_configured);
  const fieldBlocks = topic.blocks && topic.blocks.length ? topic.blocks : [{ keys }];
  for (const block of fieldBlocks) {
    if (block.title) body.appendChild(el("h2", { class: "settings-kicker" }, block.title));
    const list = el("div", { class: "setting-list settings-fields" });
    for (const key of block.keys || []) {
      const item = state.items.get(key);
      if (item) list.appendChild(settingRow(item, encryptionReady));
    }
    body.appendChild(list);
  }
  if (topic.id === "llm" || topic.id === "ifind") root.appendChild(connectionTest(topic.id));
  return root;
}

function renderHits() {
  const root = el("div", { class: "settings-topic settings-hits" });
  root.appendChild(cardHead("搜索结果", "点一条进入那个主题"));
  const body = el("div", { class: "settings-card-body" });
  root.appendChild(body);
  const needle = state.query.trim().toLowerCase();
  const hits = [];
  const seen = new Set();
  for (const section of state.sections) {
    for (const topic of section.topics) {
      for (const key of topicKeys(topic)) {
        if (seen.has(key)) continue;
        const item = state.items.get(key);
        if (!item) continue;
        const hay = `${item.label} ${item.help || ""} ${section.title} ${topic.title} ${key}`.toLowerCase();
        if (!hay.includes(needle)) continue;
        seen.add(key);
        hits.push({ key, item, section, topic });
        if (hits.length >= 24) break;
      }
      if (hits.length >= 24) break;
    }
    if (hits.length >= 24) break;
  }
  if (!hits.length) {
    body.appendChild(el("p", { class: "notice" }, "没有这项配置。"));
    return root;
  }
  const list = el("div", { class: "list-group settings-jump-list" });
  for (const hit of hits) {
    list.appendChild(jumpRow({
      title: hit.item.label,
      detail: `${hit.section.title} / ${hit.topic.title}`,
      value: shownValue(hit.item),
      onClick: () => openPlace(hit.section.id, hit.topic.id),
    }));
  }
  body.appendChild(list);
  return root;
}

// 桌面左侧：按 研究 / 数据 / 运行 / 系统 分组的目录，每项带一行说明和当前值摘要。
function renderNav() {
  const nav = document.getElementById("settings-nav");
  if (!nav) return;
  clear(nav);
  const searching = state.query.trim().length > 0;
  for (const name of NAV_GROUPS) {
    const sections = state.sections.filter((section) => section.navGroup === name);
    if (!sections.length) continue;
    nav.appendChild(el("div", { class: "settings-group-label" }, name));
    for (const section of sections) {
      const home = sectionHome(section, readNow, (key) => state.items.get(key));
      const dirty = sectionDirtyCount(section);
      const button = el("button", { type: "button", class: "settings-nav-item", "data-dirty-for": section.id });
      if (section.id === state.place.sectionId && !searching) {
        button.classList.add("is-current");
        button.setAttribute("aria-current", "true");
      }
      const text = el("span", { class: "settings-nav-text" });
      text.appendChild(el("b", {}, section.title));
      text.appendChild(el("small", {}, home.detail));
      button.appendChild(text);
      button.appendChild(el("span", { class: dirty ? "settings-nav-value settings-nav-dirty" : "settings-nav-value" }, dirty ? `未保存 ${dirty} 项` : home.value));
      button.addEventListener("click", () => openPlace(section.id, section.topics.length === 1 ? section.topics[0].id : null));
      nav.appendChild(button);
    }
  }
}

const DESKTOP_QUERY = "(min-width: 56rem)";

function isDesktop() {
  return Boolean(globalThis.matchMedia && globalThis.matchMedia(DESKTOP_QUERY).matches);
}

function syncSearchVisibility(searching) {
  const wrap = document.getElementById("settings-search-wrap");
  if (!wrap) return;
  wrap.hidden = !(isDesktop() || searching || !state.place.sectionId);
}

function updateChrome(searching) {
  const { section, topic } = placeTopic();
  const desktop = isDesktop();
  const title = document.getElementById("settings-title");
  const lead = document.getElementById("settings-lead");
  const back = document.getElementById("settings-back");
  if (title) {
    title.textContent = searching ? "搜索" : desktop ? "设置" : topic ? topic.title : section ? section.title : "设置";
    // 手机顶栏已写“设置”；进入子页后用页头标题显示当前主题。
    title.classList.toggle("keep", !desktop && (searching || Boolean(section)));
  }
  if (lead) {
    const text = searching
      ? "点一条进入那个主题。"
      : desktop || !section
        ? "改完不用重启。生效方式写在每个主题里，只写一次。"
        : !topic
          ? section.blurb || ""
          : "";
    lead.textContent = text;
    lead.hidden = !text;
  }
  if (back) {
    const multi = Boolean(topic && section && section.topics.length > 1);
    back.hidden = searching || !section || (desktop && !multi);
    back.textContent = multi ? section.title : "设置";
  }
  syncSearchVisibility(searching);
}

function captureMounted() {
  for (const [key, node] of state.nodes) state.raw.set(key, node.read());
}

function paint() {
  captureMounted();
  state.nodes = new Map();
  const hash = globalThis.location ? globalThis.location.hash : "";
  state.place = resolveSettingsPlace(hash, state.sections);
  // 桌面是两栏：没有指定目录时默认落在第一项，右侧不留空。
  if (!state.place.sectionId && isDesktop() && state.sections.length) {
    state.place = resolveSettingsPlace(`#${state.sections[0].id}`, state.sections);
  }
  const searching = state.query.trim().length > 0;
  updateChrome(searching);
  renderNav();
  const region = document.getElementById("settings-form");
  if (!region || !state.data) return;
  const { section, topic } = placeTopic();
  let root;
  if (searching) root = renderHits();
  else if (!section) root = renderDirectory();
  else if (!topic) root = renderTopicList(section);
  else root = renderTopic(topic, section);
  renderRegion(region, "ready", root);
  const history = document.getElementById("settings-history-card");
  const bootstrap = document.getElementById("settings-bootstrap-card");
  if (history) history.hidden = !(topic && topic.kind === "history");
  if (bootstrap) bootstrap.hidden = !(topic && topic.kind === "bootstrap");
  updateSavebar();
  refreshDirtyLabels();
}

function openPlace(sectionId, topicId) {
  state.query = "";
  const input = document.getElementById("settings-search");
  if (input) input.value = "";
  const next = topicId ? `${sectionId}/${topicId}` : sectionId || "";
  if ((globalThis.location.hash || "").replace(/^#/, "") === next) paint();
  else globalThis.location.hash = next;
}

function goBack() {
  const { section, topic } = placeTopic();
  if (topic && section && section.topics.length > 1) openPlace(section.id, null);
  else openPlace("", null);
}

function adoptSettings(data, { resetEdits }) {
  if (resetEdits) {
    state.edits = new Map();
    state.raw = new Map();
    state.cleared = new Set();
    state.nodes = new Map();
  }
  state.data = data;
  indexItems(data);
  renderEncryptionNotice(data);
  renderBootstrap(data.bootstrap || []);
  paint();
}

function commitSettings(data, savedKeys) {
  // Drop the mounted controls first so paint does not copy the old input back into raw.
  state.nodes = new Map();
  for (const key of savedKeys) {
    state.edits.delete(key);
    state.raw.delete(key);
    state.cleared.delete(key);
  }
  adoptSettings(data, { resetEdits: false });
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
    adoptSettings(await get(ENDPOINT), { resetEdits: true });
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
  const { topic } = placeTopic();
  const keys = topicDirtyKeys(topic);
  const invalid = keys.filter((key) => state.edits.get(key) === INVALID);
  if (!keys.length) return;
  if (invalid.length) {
    setSaveResult([`有 ${invalid.length} 项输入不合法，请先修正。`], "warn");
    state.nodes.get(invalid[0])?.control?.focus();
    return;
  }
  const values = Object.fromEntries(keys.map((key) => [key, state.edits.get(key)]));
  const noteInput = document.getElementById("settings-note");
  const note = noteInput && noteInput.value.trim() ? noteInput.value.trim() : null;
  const promise = patch(ENDPOINT, { values, note })
    .then((result) => {
      commitSettings(result.settings, keys);
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
  const { topic } = placeTopic();
  state.nodes = new Map();
  for (const key of topic ? topicKeys(topic) : []) {
    state.edits.delete(key);
    state.raw.delete(key);
    state.cleared.delete(key);
  }
  setSaveResult([], "");
  paint();
}

export function bootSettings() {
  const region = document.getElementById("settings-form");
  if (!region) return;
  const saveButton = document.getElementById("settings-save");
  if (saveButton) saveButton.addEventListener("click", () => save(saveButton));
  const discardButton = document.getElementById("settings-discard");
  if (discardButton) discardButton.addEventListener("click", discard);
  const back = document.getElementById("settings-back");
  if (back) back.addEventListener("click", goBack);
  const search = document.getElementById("settings-search");
  if (search) {
    search.addEventListener("input", () => {
      state.query = search.value;
      paint();
    });
  }
  globalThis.addEventListener("hashchange", () => {
    setSaveResult([], "");
    paint();
  });
  if (globalThis.matchMedia) {
    const query = globalThis.matchMedia(DESKTOP_QUERY);
    if (query.addEventListener) query.addEventListener("change", () => paint());
  }
  globalThis.addEventListener("beforeunload", (event) => {
    if (dirtyCount()) event.preventDefault();
  });
  loadForm();
  loadHistory();
}
