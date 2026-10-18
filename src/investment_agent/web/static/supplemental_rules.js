import { randomUUID } from "./uuid.js";
/* Structured P2 rule and user-event management. Mutation bodies are persisted
 * before POST and replayed byte-for-byte only after an uncertain response. */

const API = Object.freeze({
  context: "/supplemental-rule-context",
  rules: "/supplemental-rules",
  events: "/research-events",
  runs: "/supplemental-rule-evaluations/runs",
});
const PENDING_KEY = "zc-supplemental-rule-pending-v1";
const TRACKED_RUNS_KEY = "zc-supplemental-rule-runs-v1";
const acceptedInMemory = new Map();
const DIMENSION_LABELS = Object.freeze({
  business_demand: "业务需求", competition_durability: "竞争持续性", financial_cash: "财务现金",
  balance_sheet_allocation: "资产负债与配置", price_expectations: "价格预期",
  downside_counter_case: "下行情景", portfolio_alternatives: "组合替代项",
});

const one = (root, selector) => root.querySelector(selector);
const all = (root, selector) => [...root.querySelectorAll(selector)];
const text = (root, value) => { root.textContent = value == null ? "—" : String(value); };
const uuid = randomUUID;
const lines = (value) => value.split(/\r?\n/).map((part) => part.trim()).filter(Boolean);
const localDatetime = (value) => value ? new Date(value).toISOString() : null;

export function buildRuleDefinition(form, context) {
  const data = new FormData(form);
  const selectorKind = data.get("selector_kind");
  let selector;
  if (selectorKind === "listing") {
    const listing = context.listing_refs.find((ref) => String(ref.id) === String(data.get("listing_id")));
    if (!listing) throw new Error("请选择范围内的证券。");
    selector = {kind: "listing", listing_ref: listing};
  } else if (selectorKind === "company") {
    selector = {kind: "company", company_id: Number(data.get("company_id"))};
  } else {
    const sourceId = String(data.get("category_source_id") || "").trim();
    if (!sourceId) throw new Error("类别范围需要填写来源 UUID。");
    const sourceRef = {
      kind: data.get("category_source_kind"), id: sourceId,
      revision: Number(data.get("category_source_revision") || 1),
    };
    let term;
    if (data.get("category_kind") === "industry") {
      term = {
        kind: "industry", system: String(data.get("category_system") || "").trim(),
        scheme_version: String(data.get("scheme_version") || "").trim(),
        code: String(data.get("category_code") || "").trim(), source_ref: sourceRef,
      };
    } else {
      const members = lines(String(data.get("category_members") || ""));
      if (!members.length) throw new Error("产品或地区类别至少需要一项名单成员。");
      term = {kind: data.get("category_kind"), members, complete: data.get("category_complete") === "on", source_ref: sourceRef};
    }
    selector = {kind: "category", expression: {operator: "all", terms: [term]}};
  }

  const type = data.get("rule_type");
  let definition;
  let eventRef = null;
  if (type === "metric_condition") {
    const operator = data.get("operator");
    const raw = String(data.get("metric_value") || "").trim();
    const value = ["between", "in"].includes(operator) ? raw.split(",").map((part) => part.trim()) : raw;
    definition = {kind: type, condition: {kind: "all", conditions: [{kind: "factor", factor_id: data.get("factor_id"), operator, value, unit: data.get("unit") || null}]}};
  } else if (type === "research_requirement") {
    definition = {
      kind: type, question: data.get("question"), required_evidence: lines(data.get("required_evidence")),
      completion_criteria: data.get("completion_criteria"), falsification_criteria: data.get("falsification_criteria"),
      check_by: localDatetime(data.get("check_by")), incomplete_effects: ["require_review"],
    };
  } else {
    const id = String(data.get("event_id") || "").trim();
    if (!id) throw new Error("事件影响规则需要先登记事件线索，再填写事件 UUID。");
    eventRef = {kind: "research_event", id, revision: Number(data.get("event_revision") || 1)};
    definition = {
      kind: type, event_ref: eventRef, affected_scope: lines(data.get("affected_scope")),
      exposure_evidence: lines(data.get("exposure_evidence")), impact_path: data.get("impact_path"),
      release_requirements: lines(data.get("event_release_requirements")),
    };
  }
  let sourceRefs;
  try { sourceRefs = JSON.parse(String(data.get("source_refs") || "[]")); }
  catch { throw new Error("来源引用必须是有效 JSON 数组。"); }
  if (!Array.isArray(sourceRefs)) throw new Error("来源引用必须是 JSON 数组。");
  const effects = all(form, 'input[name="effect"]:checked').map((input) => input.value);
  if (!effects.length) throw new Error("至少选择一种影响方式。");
  return {
    schema_version: 1, name: data.get("name"), rationale: data.get("rationale"), source_refs: sourceRefs,
    selector, contexts: [data.get("context")], rule_type: type, definition,
    dimensions: [data.get("dimension")], effects, severity: data.get("severity"), materiality: data.get("materiality"),
    event_ref: eventRef, issue_key: String(data.get("issue_key") || "").trim() || null,
    valid_from: localDatetime(data.get("valid_from")), valid_until: localDatetime(data.get("valid_until")),
    review_due_at: localDatetime(data.get("review_due_at")), release_requirements: lines(data.get("release_requirements")),
  };
}

function statusText(value) {
  const labels = {
    active: "已启用", suspended: "已暂停", retired: "已退役", draft: "草稿", validated: "已校验",
    triggered: "已触发", not_triggered: "未触发", unknown: "未知", not_applicable: "不适用",
    verified: "已核对", pending: "待核对", conflicted: "存在冲突", clear: "无新问题",
    unresolved: "未决", inconclusive: "无法判断", mechanical: "机械评估", stale: "需要刷新",
  };
  return labels[value] || value || "未知";
}

async function request(path, {method = "GET", body} = {}) {
  const response = await fetch(path, {
    method, credentials: "same-origin",
    headers: {"X-Requested-With": "fetch", ...(body ? {"Content-Type": "application/json"} : {})},
    body: body ? JSON.stringify(body) : undefined,
  });
  let data = null;
  try { data = await response.json(); } catch { /* empty response */ }
  if (!response.ok) {
    const detail = data?.detail;
    const message = typeof detail === "string" ? detail : `请求失败（HTTP ${response.status}）。`;
    const error = new Error(message);
    error.status = response.status;
    error.detail = detail;
    throw error;
  }
  return data;
}

function showAlert(root, message, kind = "info") {
  const node = one(root, "#supplemental-alert");
  if (!node) return;
  node.hidden = false;
  node.className = `supplemental-alert supplemental-alert--${kind}`;
  text(node, message);
}

function showDiagnostics(root, title, diagnostics = []) {
  const panel = document.createElement("div");
  panel.className = "supplemental-diagnostics";
  const heading = document.createElement("b"); text(heading, title); panel.append(heading);
  if (diagnostics.length) {
    const list = document.createElement("ul");
    for (const diagnostic of diagnostics) { const item = document.createElement("li"); text(item, diagnostic); list.append(item); }
    panel.append(list);
  }
  return panel;
}

function readPending(storage = sessionStorage) {
  try { return JSON.parse(storage.getItem(PENDING_KEY) || "null"); }
  catch { return null; }
}

function writePending(storage, record) {
  storage.setItem(PENDING_KEY, JSON.stringify(record));
}

export async function submitWithRecovery({path, body, operation, storage = sessionStorage, send = request}) {
  const prior = readPending(storage);
  if (prior) throw Object.assign(new Error("有一项操作尚未确认，请先用相同操作 ID 重试或刷新确认。"), {pending: prior});
  const record = {operation, path, body, phase: "prepared", saved_at: new Date().toISOString()};
  // A storage exception aborts here: no network request is allowed before this succeeds.
  writePending(storage, record);
  let result;
  try {
    result = await send(path, {method: "POST", body});
  } catch (error) {
    if (!error.status || error.status >= 500) {
      record.phase = "uncertain";
      record.failure = error.message;
      error.uncertain = true;
      try { writePending(storage, record); }
      catch { error.message = `${error.message}；本机无法更新待确认标记，请刷新后按相同操作 ID 核对。`; }
    } else {
      try { storage.removeItem(PENDING_KEY); }
      catch { error.cleanupWarning = "服务器已明确拒绝此请求，但本机无法清理待确认标记。"; }
    }
    throw error;
  }
  // Persist the accepted receipt before cleanup so a storage exception cannot
  // turn a known success into an uncertain POST.
  record.phase = "accepted";
  record.accepted_at = new Date().toISOString();
  record.result = result;
  acceptedInMemory.set(String(body.idempotency_key || ""), {record, result});
  let recoveryWarning = null;
  try { writePending(storage, record); }
  catch { recoveryWarning = "服务器已接收操作，但本机无法保存回执；当前页面保留回执，并继续跟踪已返回的运行。"; }
  return {result, recoveryWarning};
}

export async function retryPending({storage = sessionStorage, send = request} = {}) {
  const record = readPending(storage);
  if (!record) throw new Error("没有待确认的原操作。");
  const known = acceptedInMemory.get(String(record.body?.idempotency_key || ""));
  if (known || record.phase === "accepted") {
    const result = known?.result ?? record.result;
    return {record: known?.record || record, result, accepted: true};
  }
  if (!["prepared", "sending", "uncertain"].includes(record.phase)) throw new Error("没有待确认的原操作。");
  const result = await send(record.path, {method: "POST", body: record.body});
  const accepted = {...record, phase: "accepted", accepted_at: new Date().toISOString(), result};
  delete accepted.failure;
  acceptedInMemory.set(String(record.body?.idempotency_key || ""), {record: accepted, result});
  let recoveryWarning = null;
  try { writePending(storage, accepted); }
  catch { recoveryWarning = "服务器已确认原操作，但本机无法保存回执。"; }
  return {record: accepted, result, accepted: true, recoveryWarning};
}

export function clearAcceptedPending(storage = sessionStorage) {
  try { storage.removeItem(PENDING_KEY); return null; }
  catch { return "服务器已接收操作；本机无法清理回执标记，当前页面保留回执并继续跟踪运行。"; }
}

function ordinalRanges(values) {
  const ordinals = [...new Set(values)].filter(Number.isInteger).sort((a, b) => a - b);
  const ranges = [];
  for (const value of ordinals) {
    const previous = ranges[ranges.length - 1];
    if (previous && value === previous[1] + 1) previous[1] = value;
    else ranges.push([value, value]);
  }
  return ranges.map(([start, end]) => start === end ? String(start) : String(start) + "–" + String(end));
}

export function summarizeRunProgress(view) {
  const members = view.members || [];
  return {
    terminal: ["success", "partial", "failed", "cancelled"].includes(view.status),
    status: view.status || "unknown",
    completed: view.completed_count || 0,
    declared: view.declared_count || 0,
    pending: ordinalRanges(members.filter((member) => member.state === "pending").map((member) => member.ordinal)),
    failed: ordinalRanges(members.filter((member) => member.state === "item_failed").map((member) => member.ordinal)),
    diagnostics: [...(view.scan_diagnostics || []), ...(view.unbound_event_refs || []).map((ref) => "未绑定事件 " + ref.kind + ":" + ref.id + "@" + ref.revision)],
    failureCode: view.failure_code || null,
    coverageComplete: view.coverage_complete === true,
  };
}

export function renderRunSummary(target, item, view) {
  const summary = summarizeRunProgress(view);
  target.replaceChildren();
  const makeText = (tag, value) => {
    const element = document.createElement(tag);
    element.textContent = String(value);
    return element;
  };
  const title = document.createElement("h3");
  text(title, (item.operation || "操作") + " · 运行 " + item.run_ref.id);
  target.append(title);
  const status = document.createElement("p");
  text(status, (summary.terminal ? "终态 " : "处理中 ") + summary.status + " · 已完成 " + summary.completed + "/" + summary.declared);
  target.append(status);
  target.append(makeText("p", summary.coverageComplete ? "覆盖完整" : "覆盖尚未完整"));
  if (summary.pending.length) target.append(makeText("p", "待处理成员：" + summary.pending.join("、") + "。"));
  if (summary.failed.length) target.append(makeText("p", "失败成员：" + summary.failed.join("、") + "。"));
  if (summary.diagnostics.length) {
    const list = document.createElement("ul");
    for (const diagnostic of summary.diagnostics) list.append(makeText("li", diagnostic));
    target.append(list);
  }
  if (summary.failureCode) target.append(makeText("p", "失败代码：" + summary.failureCode));
  if (summary.terminal && !summary.coverageComplete) target.append(makeText("p", "覆盖未完整；终态不表示安全结论。"));
  return summary;
}

function readTrackedRuns(storage) {
  try {
    const parsed = JSON.parse(storage.getItem(TRACKED_RUNS_KEY) || "[]");
    return Array.isArray(parsed) ? parsed.filter((item) => item?.run_ref?.kind === "pipeline_run" && item.run_ref.revision === 1) : [];
  } catch { return []; }
}

function stableJson(value) {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${stableJson(value[key])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

export function findTrackedRunForPending(pending, records) {
  const operationId = pending?.body?.idempotency_key;
  if (!operationId || !pending.path || !pending.operation || !pending.body) return null;
  return (records || []).find((record) =>
    record?.run_ref?.kind === "pipeline_run" && record.run_ref.revision === 1
    && record.operation_id === operationId
    && typeof record.source_manifest_hash === "string" && /^[0-9a-f]{64}$/.test(record.source_manifest_hash)
    && record.operation === pending.operation
    && record.path === pending.path
    && stableJson(record.body) === stableJson(pending.body)
  ) || null;
}

export function matchesTrackedRunIdentity(view, expected) {
  return Boolean(expected?.operation_id && expected?.source_manifest_hash
    && view?.operation_id === expected.operation_id
    && view?.manifest_hash === expected.source_manifest_hash);
}

function lineList(value) { return value.map((item) => String(item)).join("\n"); }

function fillListingOptions(select, refs) {
  select.replaceChildren();
  const placeholder = document.createElement("option"); placeholder.value = ""; text(placeholder, "请选择证券代码"); select.append(placeholder);
  for (const ref of refs || []) {
    const option = document.createElement("option"); option.value = String(ref.id); text(option, `证券 ${ref.id}`); select.append(option);
  }
}

function setRuleTypeFields(root) {
  const type = one(root, 'select[name="rule_type"]')?.value;
  for (const block of all(root, ".rule-type-fields")) block.hidden = block.dataset.ruleType !== type;
  const selector = one(root, 'select[name="selector_kind"]')?.value;
  one(root, ".selector-listing").hidden = selector !== "listing";
  one(root, ".selector-company").hidden = selector !== "company";
  const category = selector === "category";
  one(root, ".selector-category").hidden = !category;
  one(root, ".selector-category-fields").hidden = !category;
  const kind = one(root, 'select[name="category_kind"]')?.value;
  for (const item of all(root, ".industry-field")) item.hidden = !category || kind !== "industry";
  for (const item of all(root, ".membership-field")) item.hidden = !category || kind === "industry";
}

function displayRule(rule) {
  const scope = rule.selector?.kind === "listing" ? `证券 ${rule.selector.listing_ref.id}`
    : rule.selector?.kind === "company" ? `公司 ${rule.selector.company_id}` : "类别规则";
  return `${rule.name} · ${scope} · ${rule.revision} 版`;
}

export function bootSupplementalRules(root = document.querySelector("#supplemental-rules-root")) {
  if (!root) return;
  const list = one(root, "#supplemental-rule-list");
  const detail = one(root, "#supplemental-rule-detail");
  const form = one(root, "#supplemental-rule-form");
  const eventForm = one(root, "#supplemental-event-form");
  const state = {context: null, rules: [], events: [], selectedId: null, selectedDetail: null, selectionGeneration: 0, editingId: null, editingEvent: null, busy: false, trackedRuns: new Map()};
  const pending = readPending();
  const runContainer = one(root, "#supplemental-run-status");

  function persistTrackedRuns() {
    try {
      const entries = [...state.trackedRuns.values()]
        .map(({timer, polling, card, terminal, ...item}) => item);
      const pending = readPending(sessionStorage);
      const pinned = findTrackedRunForPending(pending, entries);
      const values = entries.slice(-100);
      if (pinned && !values.includes(pinned)) values.splice(0, values.length === 100 ? 1 : 0, pinned);
      sessionStorage.setItem(TRACKED_RUNS_KEY, JSON.stringify(values));
      return true;
    } catch { return false; }
  }

  function runNode(runId) {
    let entry = state.trackedRuns.get(runId);
    if (!entry) {
      if (runContainer.querySelector(".empty-note")) runContainer.replaceChildren();
      const card = document.createElement("article"); card.className = "supplemental-run-item"; card.dataset.runId = runId;
      entry = {card, timer: null};
      state.trackedRuns.set(runId, entry);
      runContainer.append(card);
    }
    return entry;
  }

  async function readRunProgress(runRef, expected) {
    let cursor = null;
    let base = null;
    const members = [];
    for (let page = 0; page < 20; page++) {
      const query = new URLSearchParams({limit: "100"});
      if (cursor) query.set("cursor", cursor);
      const view = await request(API.runs + "/" + encodeURIComponent(runRef.id) + "?" + query);
      if (!matchesTrackedRunIdentity(view, expected)) {
        throw new Error("服务器运行身份或清单摘要与已保存收据不一致。");
      }
      if (!base) base = view;
      else if (view.manifest_hash !== base.manifest_hash) throw new Error("运行清单在分页期间发生变化。");
      members.push(...(view.members || []));
      cursor = view.next_cursor || null;
      if (!cursor) break;
      if (page === 19) throw new Error("运行成员分页超过客户端安全上限。");
    }
    return {...base, members};
  }

  async function pollRun(runId) {
    const entry = state.trackedRuns.get(runId);
    if (!entry || entry.polling || entry.terminal) return;
    entry.polling = true;
    try {
      const view = await readRunProgress(entry.run_ref, entry);
      if (entry.recoveryPending) {
        const current = readPending(sessionStorage);
        if (findTrackedRunForPending(current, [entry])) {
          const cleanupWarning = clearAcceptedPending();
          entry.recoveryPending = null;
          showAlert(root, cleanupWarning || "已从本机保存的运行记录恢复此前已接受的操作；不会重新发送请求。", cleanupWarning ? "conflict" : "success");
        }
      }
      const summary = summarizeRunProgress(view);
      entry.terminal = summary.terminal;
      renderRunSummary(entry.card, entry, view);
      if (entry.terminal) {
        persistTrackedRuns();
        return;
      }
    } catch (error) {
      entry.card.replaceChildren();
      const message = document.createElement("p"); text(message, "运行 " + entry.run_ref.id + " 暂时无法读取：" + error.message + "；将继续重试。"); entry.card.append(message);
      if (String(error.message).includes("运行身份或清单摘要")) {
        entry.identityMismatch = true;
        if (entry.recoveryPending) showAlert(root, "本机保存的运行与待确认操作不匹配；原请求仍保留，不会把此运行当作已接受操作。", "conflict");
      }
    } finally {
      entry.polling = false;
      if (!entry.terminal && !entry.identityMismatch) entry.timer = setTimeout(() => pollRun(runId), 1200);
    }
  }

  function trackAcceptedRun(result, operation, path, body) {
    const runRef = result?.run_ref || result?.receipt?.run_ref;
    if (!runRef) return null;
    if (runRef.kind !== "pipeline_run" || runRef.revision !== 1) {
      showAlert(root, "服务器返回了无法跟踪的运行引用；已保存操作回执，请刷新并核对。", "conflict");
      return {persisted: false};
    }
    const runId = String(runRef.id);
    const item = {
      run_ref: runRef, operation, path, body,
      operation_id: result.operation_id || result.receipt?.operation_id || body?.idempotency_key || null,
      source_manifest_hash: result.source_manifest_hash || result.receipt?.source_manifest_hash || null,
      accepted_at: result.accepted_at || result.receipt?.accepted_at || new Date().toISOString(),
    };
    if (item.operation_id !== body?.idempotency_key || typeof item.source_manifest_hash !== "string" || !/^[0-9a-f]{64}$/.test(item.source_manifest_hash)) {
      showAlert(root, "服务器回执缺少与原操作匹配的运行身份或清单摘要；原回执保留，等待核对。", "conflict");
      return {persisted: false};
    }
    const existing = state.trackedRuns.get(runId);
    if (existing) {
      const sameIdentity = existing.operation_id === item.operation_id
        && existing.source_manifest_hash === item.source_manifest_hash
        && existing.operation === item.operation
        && existing.path === item.path
        && stableJson(existing.body) === stableJson(item.body)
        && stableJson(existing.run_ref) === stableJson(item.run_ref);
      if (!sameIdentity) {
        showAlert(root, "本机已有相同运行编号但操作身份不同；原回执继续保留，等待核对。", "conflict");
        return {entry: existing, persisted: false};
      }
      const persisted = persistTrackedRuns();
      if (!persisted) showAlert(root, "服务器回执已收到，但本机无法保存运行跟踪记录；原操作回执仍保留。", "conflict");
      return {entry: existing, persisted};
    }
    const entry = runNode(runId);
    Object.assign(entry, item);
    const persisted = persistTrackedRuns();
    if (!persisted) showAlert(root, "服务器已接收并返回重评运行；本机无法保存跟踪记录，当前页面仍会继续跟踪。", "conflict");
    void pollRun(runId);
    return {entry, persisted};
  }

  const savedTrackedRuns = readTrackedRuns(sessionStorage);
  for (const item of savedTrackedRuns) {
    const runId = String(item.run_ref.id);
    const entry = runNode(runId); Object.assign(entry, item); void pollRun(runId);
  }
  if (pending?.phase === "accepted") {
    const tracked = trackAcceptedRun(pending.result, pending.operation, pending.path, pending.body);
    let recoveryWarning = null;
    if (tracked?.persisted || (!tracked && !(pending.result?.run_ref || pending.result?.receipt?.run_ref))) recoveryWarning = clearAcceptedPending();
    else if (tracked && !tracked.persisted) recoveryWarning = "服务器回执已保存，但运行身份/摘要或本机运行跟踪无法核验；原回执继续保留。";
    showAlert(root, recoveryWarning || "上一项操作已收到服务器回执；正在恢复运行跟踪。", recoveryWarning ? "conflict" : "success");
  } else if (pending) {
    const recovered = findTrackedRunForPending(pending, savedTrackedRuns);
    if (recovered) {
      const entry = state.trackedRuns.get(String(recovered.run_ref.id));
      if (entry) {
        entry.recoveryPending = pending;
        showAlert(root, "正在核验本机保存的运行身份；核验完成前不会重新发送请求。", "conflict");
        if (!entry.polling) void pollRun(String(recovered.run_ref.id));
      } else addRetryButton();
    } else addRetryButton();
  }

  async function refresh() {
    const [ctx, rules, eventPage] = await Promise.all([
      request(API.context), request(`${API.rules}?state=all&limit=100`), request(`${API.events}?limit=100`),
    ]);
    state.context = ctx;
    state.rules = Array.isArray(rules) ? rules : (rules.items || rules.rules || []);
    state.registryRevision = rules.registry_revision;
    state.events = Array.isArray(eventPage) ? eventPage : (eventPage.items || eventPage.events || []);
    text(one(root, "#supplemental-feature-state"), ctx.enabled ? "已开启" : "当前关闭（默认关闭）");
    text(one(root, "#supplemental-rules-generation"), ctx.rules_generation ?? 0);
    text(one(root, "#supplemental-events-generation"), ctx.events_generation ?? 0);
    fillListingOptions(one(root, "#supplemental-listing-select"), ctx.listing_refs);
    renderList();
    renderEvents();
    if (state.selectedId) await loadDetail(state.selectedId);
    const submit = one(root, "#supplemental-save-rule");
    submit.disabled = !ctx.enabled;
    one(root, '#supplemental-event-form button[type="submit"]').disabled = !ctx.enabled;
  }

  function renderList() {
    list.replaceChildren();
    if (!state.rules.length) {
      const empty = document.createElement("li"); empty.className = "empty-note"; text(empty, "暂无规则。可在右侧创建第一条草稿。"); list.append(empty); return;
    }
    for (const entry of state.rules) {
      const rule = entry.active_rule || entry.draft_rule || entry.active || entry.draft || entry.definition || entry;
      const id = rule.id || entry.rule_id;
      const item = document.createElement("li");
      const button = document.createElement("button"); button.type = "button";
      button.className = "supplemental-rule-choice"; button.dataset.ruleId = String(id);
      text(button, displayRule(rule)); button.addEventListener("click", () => loadDetail(id));
      item.append(button);
      const stateLabel = document.createElement("small");
      text(stateLabel, `${statusText(entry.active_state || (entry.active_rule ? "active" : ""))}${entry.draft_rule || entry.draft ? ` · ${statusText(entry.draft_state || "draft")}` : ""}`);
      item.append(stateLabel); list.append(item);
    }
  }

  function renderEvents() {
    const target = one(root, "#supplemental-event-list");
    target.replaceChildren();
    if (!state.events.length) { const empty = document.createElement("p"); empty.className = "empty-note"; text(empty, "暂无已登记事件线索。"); target.append(empty); return; }
    for (const item of state.events) {
      const eventValue = item.event || item;
      const card = document.createElement("article"); card.className = "supplemental-event-row";
      const title = document.createElement("b"); text(title, eventValue.title || eventValue.event_key); card.append(title);
      const meta = document.createElement("small");
      text(meta, `${eventValue.verification === "user_report" ? "用户报告 · 尚未核验" : statusText(eventValue.verification)} · ${eventValue.policy_stage || "阶段未知"} · ${eventValue.revision} 版`);
      card.append(meta);
      const exact = document.createElement("small"); text(exact, `精确版本：research_event:${eventValue.id}@${eventValue.revision}`); card.append(exact);
      if (eventValue.verification === "user_report") {
        const revise = document.createElement("button"); revise.type = "button"; revise.className = "btn btn-sm"; text(revise, "修订此用户报告");
        revise.addEventListener("click", async () => {
          try {
            const exactEvent = await request(`/research-events/${encodeURIComponent(eventValue.id)}?revision=${eventValue.revision}`);
            hydrateEventForm(eventForm, exactEvent);
            state.editingEvent = exactEvent;
            eventForm.querySelector('button[type="submit"]').textContent = `提交 ${exactEvent.revision + 1} 版用户报告`;
            eventForm.scrollIntoView({behavior: "smooth", block: "center"});
          } catch (error) { showAlert(root, error.message, "error"); }
        });
        card.append(revise);
      }
      target.append(card);
    }
  }

  function hydrateEventForm(targetForm, value) {
    const eventValue = value.event || value;
    const set = (name, inputValue) => { const input = targetForm.querySelector(`[name="${name}"]`); if (input) input.value = inputValue ?? ""; };
    set("event_key", eventValue.event_key); set("title", eventValue.title); set("event_type", eventValue.event_type);
    set("jurisdiction", eventValue.jurisdiction); set("policy_stage", eventValue.policy_stage);
    set("affected_products", lineList(eventValue.affected_products || [])); set("affected_subjects", lineList(eventValue.affected_subjects || []));
    set("published_at", eventValue.published_at?.slice(0, 16)); set("effective_from", eventValue.effective_from?.slice(0, 16)); set("effective_to", eventValue.effective_to?.slice(0, 16));
    set("source_refs", JSON.stringify(eventValue.source_refs || []));
  }

  async function loadDetail(id) {
    state.selectedId = String(id);
    const generation = ++state.selectionGeneration;
    try {
      const [value, history] = await Promise.all([
        request(`${API.rules}/${encodeURIComponent(id)}`),
        request(`${API.rules}/${encodeURIComponent(id)}/history?limit=100`),
      ]);
      if (generation !== state.selectionGeneration || state.selectedId !== String(id)) return;
      value.audit = history.events || history.items || history.audit || [];
      state.selectedDetail = value;
      renderDetail(value, generation);
    } catch (error) { showAlert(root, error.message, "error"); }
  }

  function renderDetail(value, generation) {
    detail.replaceChildren();
    const title = document.createElement("h3"); text(title, `规则 ${state.selectedId}`); detail.append(title);
    const active = value.active_rule || value.active;
    const draft = value.draft_rule || value.draft;
    const summary = document.createElement("p");
    text(summary, `当前状态：${statusText(value.active_state)}${draft ? `；草稿：${statusText(value.draft_state)}（${draft.revision} 版）` : "；没有待处理草稿"}`);
    detail.append(summary);
    if (active) detail.append(showDiagnostics(root, `当前启用版本 ${active.revision}`, [`内容摘要 ${active.content_hash}`, `有效期 ${active.valid_from} 至 ${active.valid_until || "未设截止"}`, `复核日期 ${active.review_due_at}`]));
    if (draft) {
      detail.append(showDiagnostics(root, `草稿版本 ${draft.revision}`, [`内容摘要 ${draft.content_hash}`, `规则类型 ${draft.rule_type}`, `适用场景 ${(draft.contexts || []).join("、")}`]));
      const validate = document.createElement("button"); validate.type = "button"; validate.className = "btn"; text(validate, "校验此草稿版本");
      validate.disabled = !state.context?.enabled || state.busy;
      validate.addEventListener("click", async () => {
        if (generation !== state.selectionGeneration || state.selectedId !== String(draft.id)) return;
        await mutate(`${API.rules}/${draft.id}/validations`, {
          idempotency_key: uuid(), expected_head_revision: value.head_revision,
          rule_ref: {kind: "supplemental_rule", id: draft.id, revision: draft.revision}, content_hash: draft.content_hash,
        }, "validate", generation);
      });
      detail.append(validate);
      const validation = value.last_validation;
      if (validation) {
        detail.append(showDiagnostics(root, `最近校验：${statusText(validation.outcome)}`, validation.diagnostics || []));
        if (validation.outcome === "pass" && validation.content_hash === draft.content_hash) {
          const reason = document.createElement("textarea"); reason.placeholder = "批准理由（说明你核对了哪些内容）"; reason.rows = 2; reason.className = "decision-reason";
          const approve = document.createElement("button"); approve.type = "button"; approve.className = "btn btn-primary"; text(approve, `明确批准 ${draft.revision} 版 · ${draft.content_hash.slice(0, 12)}`);
          approve.disabled = !state.context?.enabled || state.busy;
          approve.addEventListener("click", async () => {
            if (generation !== state.selectionGeneration || state.selectedId !== String(draft.id)) return;
            if (!reason.value.trim()) { showAlert(root, "请填写批准理由后再提交。", "error"); reason.focus(); return; }
            await mutate(`${API.rules}/${draft.id}/decisions`, {
              idempotency_key: uuid(), expected_head_revision: value.head_revision,
              rule_ref: {kind: "supplemental_rule", id: draft.id, revision: draft.revision},
              content_hash: draft.content_hash, validation_hash: validation.validation_hash,
              action: "approve", reason: reason.value.trim(),
            }, "approve", generation);
          });
          detail.append(reason, approve);
        }
      }
      const rejectReason = document.createElement("input"); rejectReason.placeholder = "拒绝理由（必填）"; rejectReason.className = "decision-reason";
      const reject = document.createElement("button"); reject.type = "button"; reject.className = "btn btn-danger"; text(reject, "拒绝此草稿");
      reject.disabled = !state.context?.enabled || state.busy;
      reject.addEventListener("click", async () => {
        if (!rejectReason.value.trim()) { showAlert(root, "拒绝草稿需要填写理由。", "error"); rejectReason.focus(); return; }
        await mutate(`${API.rules}/${draft.id}/decisions`, {
          idempotency_key: uuid(), expected_head_revision: value.head_revision,
          rule_ref: {kind: "supplemental_rule", id: draft.id, revision: draft.revision},
          content_hash: draft.content_hash, action: "reject", reason: rejectReason.value.trim(),
        }, "reject", generation);
      });
      detail.append(rejectReason, reject);
    }
    if (active && value.active_state === "active") {
      const reason = document.createElement("input"); reason.placeholder = "暂停或退役理由（必填）"; reason.className = "decision-reason";
      for (const action of ["suspend", "retire"]) {
        const button = document.createElement("button"); button.type = "button"; button.className = "btn";
        text(button, action === "suspend" ? "暂停规则" : "退役规则");
        button.disabled = !state.context?.enabled || state.busy;
        button.addEventListener("click", async () => {
          if (!reason.value.trim()) { showAlert(root, "生命周期决策需要填写理由。", "error"); reason.focus(); return; }
          await mutate(`${API.rules}/${active.id}/decisions`, {
            idempotency_key: uuid(), expected_head_revision: value.head_revision,
            rule_ref: {kind: "supplemental_rule", id: active.id, revision: active.revision},
            content_hash: active.content_hash, action, reason: reason.value.trim(),
          }, action, generation);
        });
        detail.append(button);
      }
      detail.append(reason);
    }
    if (value.audit?.length) {
      const audit = document.createElement("details"); const summaryEl = document.createElement("summary"); text(summaryEl, `决策历史（${value.audit.length}）`); audit.append(summaryEl);
      const listEl = document.createElement("ul");
      for (const row of value.audit) { const li = document.createElement("li"); text(li, `${row.created_at} · ${row.command} · ${JSON.stringify(row.payload)}`); listEl.append(li); }
      audit.append(listEl); detail.append(audit);
    }
    const revise = document.createElement("button"); revise.type = "button"; revise.className = "btn"; text(revise, "基于此规则创建新草稿");
    revise.addEventListener("click", () => {
      const source = draft || active;
      if (!source) return;
      state.editingId = String(source.id);
      one(root, "#supplemental-editor-title").textContent = `修订 ${source.id} · 现有规则已保留`;
      hydrateRuleForm(form, source);
      one(root, "#supplemental-save-rule").textContent = "提交新草稿版本";
      form.scrollIntoView({behavior: "smooth", block: "start"});
    });
    detail.append(revise);
  }

  async function mutate(path, body, operation, generation = state.selectionGeneration) {
    if (state.busy) return;
    state.busy = true;
    for (const button of all(root, "button")) if (button.type !== "button" || button.closest("#supplemental-rule-detail")) button.disabled = true;
    try {
      const accepted = await submitWithRecovery({path, body, operation});
      const result = accepted.result;
      const tracked = trackAcceptedRun(result, operation, path, body);
      const hasRun = Boolean(result?.run_ref || result?.receipt?.run_ref);
      const cleanupWarning = (!hasRun || tracked?.persisted) ? clearAcceptedPending() : null;
      const warning = accepted.recoveryWarning || cleanupWarning || (hasRun && !tracked?.persisted ? "服务器已接收操作；本机未能保存运行跟踪回执，原操作回执仍保留。" : null);
      showAlert(root, warning || (operation + " 已保存。正在读取服务器确认的版本…"), warning ? "conflict" : "success");
      if (state.selectedId && generation === state.selectionGeneration) await loadDetail(state.selectedId);
      await refreshListOnly();
      return result;
    } catch (error) {
      showAlert(root, error.cleanupWarning || (error.uncertain ? (error.message + " 请求已保存在本机，可用同一操作 ID 重试。") : error.status === 409 ? (error.message + " 页面未自动改写请求；请刷新后核对当前版本。") : error.message), error.cleanupWarning || error.status === 409 ? "conflict" : "error");
      if (error.uncertain) addRetryButton();
      else if (state.selectedId && generation === state.selectionGeneration) await loadDetail(state.selectedId);
    } finally {
      state.busy = false;
      for (const button of all(root, "button")) button.disabled = false;
      if (!state.context?.enabled) {
        for (const button of all(root, "#supplemental-save-rule, #supplemental-event-form button[type=submit], #supplemental-rule-detail button")) button.disabled = true;
      }
    }
  }

  async function refreshListOnly() {
    const rules = await request(`${API.rules}?state=all&limit=100`);
    state.rules = Array.isArray(rules) ? rules : (rules.items || rules.rules || []);
    renderList();
  }

  function addRetryButton() {
    const alert = one(root, "#supplemental-alert");
    if (one(alert, "[data-retry-pending]")) return;
    const button = document.createElement("button"); button.type = "button"; button.dataset.retryPending = "1"; button.className = "btn"; text(button, "用相同操作 ID 重试确认");
    button.addEventListener("click", async () => {
      try {
        const recovered = await retryPending();
        const tracked = trackAcceptedRun(recovered.result, recovered.record.operation, recovered.record.path, recovered.record.body);
        const hasRun = Boolean(recovered.result?.run_ref || recovered.result?.receipt?.run_ref);
        const cleanupWarning = (!hasRun || tracked?.persisted) ? clearAcceptedPending() : null;
        const warning = recovered.recoveryWarning || cleanupWarning || (hasRun && !tracked?.persisted ? "服务器已接收操作；本机未能保存运行跟踪回执，原操作回执仍保留。" : null);
        showAlert(root, warning || (recovered.record.operation + " 已确认。"), warning ? "conflict" : "success");
        await refresh();
      }
      catch (error) { showAlert(root, error.message, "error"); }
    }); alert.append(button);
  }

  function hydrateRuleForm(targetForm, rule) {
    const set = (name, value) => { const input = one(targetForm, `[name="${name}"]`); if (input && value != null) input.value = value; };
    set("name", rule.name); set("rationale", rule.rationale); set("materiality", rule.materiality); set("severity", rule.severity);
    set("context", rule.contexts?.[0]); set("rule_type", rule.rule_type);
    set("selector_kind", rule.selector?.kind);
    if (rule.selector?.kind === "listing") set("listing_id", rule.selector.listing_ref.id);
    if (rule.selector?.kind === "company") set("company_id", rule.selector.company_id);
    if (rule.selector?.kind === "category") {
      const term = rule.selector.expression.terms[0]; set("category_kind", term.kind); set("category_system", term.system); set("scheme_version", term.scheme_version); set("category_code", term.code);
      set("category_members", lineList(term.members || [])); set("category_source_id", term.source_ref.id); set("category_source_kind", term.source_ref.kind); set("category_source_revision", term.source_ref.revision);
      const complete = one(targetForm, '[name="category_complete"]'); if (complete) complete.checked = term.complete === true;
    }
    for (const checkbox of all(targetForm, 'input[name="effect"]')) checkbox.checked = rule.effects.includes(checkbox.value);
    set("dimension", rule.dimensions?.[0]); set("valid_from", rule.valid_from?.slice(0, 16)); set("valid_until", rule.valid_until?.slice(0, 16)); set("review_due_at", rule.review_due_at?.slice(0, 16));
    set("release_requirements", lineList(rule.release_requirements || [])); set("issue_key", rule.issue_key);
    const def = rule.definition;
    if (def.kind === "metric_condition") {
      const leaf = def.condition.conditions?.[0]; set("factor_id", leaf?.factor_id); set("operator", leaf?.operator); set("metric_value", Array.isArray(leaf?.value) ? leaf.value.join(",") : leaf?.value); set("unit", leaf?.unit);
    } else if (def.kind === "research_requirement") {
      for (const key of ["question", "completion_criteria", "falsification_criteria"]) set(key, def[key]);
      set("required_evidence", lineList(def.required_evidence || [])); set("check_by", def.check_by?.slice(0, 16));
    } else {
      set("event_id", def.event_ref.id); set("event_revision", def.event_ref.revision); set("affected_scope", lineList(def.affected_scope));
      set("exposure_evidence", lineList(def.exposure_evidence)); set("impact_path", def.impact_path); set("event_release_requirements", lineList(def.release_requirements));
    }
    setRuleTypeFields(root);
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!state.context?.enabled) return showAlert(root, "功能当前关闭；不会发送任何写入请求。", "error");
    try {
      const definition = buildRuleDefinition(form, state.context);
      const operationId = uuid();
      const editingId = state.editingId;
      const selectedGeneration = state.selectionGeneration;
      const expectedHeadRevision = editingId ? state.selectedDetail?.head_revision : 0;
      const body = {idempotency_key: operationId, expected_head_revision: expectedHeadRevision, definition};
      const path = editingId ? `${API.rules}/${editingId}/revisions` : API.rules;
      if (state.editingId && !Number.isInteger(body.expected_head_revision)) throw new Error("请先重新读取规则当前版本后再修订。");
      const result = await mutate(path, body, editingId ? "draft revision" : "draft create");
      if (result?.rule_ref) {
        if (editingId !== state.editingId || (editingId && selectedGeneration !== state.selectionGeneration)) return;
        state.selectedId = String(result.rule_ref.id);
        state.editingId = null;
        await loadDetail(state.selectedId);
        await refreshListOnly();
      }
    } catch (error) { showAlert(root, error.message, "error"); }
  });

  eventForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!state.context?.enabled) return showAlert(root, "功能当前关闭；不会发送任何写入请求。", "error");
    const data = new FormData(eventForm);
    let sourceRefs;
    try { sourceRefs = JSON.parse(data.get("source_refs") || "[]"); }
    catch { return showAlert(root, "来源引用必须是有效 JSON 数组。", "error"); }
    if (!Array.isArray(sourceRefs)) return showAlert(root, "来源引用必须是 JSON 数组。", "error");
    const eventBody = {
      event_key: data.get("event_key"), title: data.get("title"), event_type: data.get("event_type"), jurisdiction: data.get("jurisdiction"),
      affected_products: lines(data.get("affected_products")), affected_subjects: lines(data.get("affected_subjects")),
      policy_stage: data.get("policy_stage"), published_at: localDatetime(data.get("published_at")),
      effective_from: localDatetime(data.get("effective_from")), effective_to: localDatetime(data.get("effective_to")), source_refs: sourceRefs,
    };
    const current = state.editingEvent;
    const requestBody = current
      ? {idempotency_key: uuid(), expected_event_revision: current.revision, previous_ref: {kind: "research_event", id: current.id, revision: current.revision}, event: eventBody}
      : {idempotency_key: uuid(), expected_event_revision: 0, event: eventBody};
    const path = current ? `${API.events}/${encodeURIComponent(current.id)}/revisions` : API.events;
    const result = await mutate(path, requestBody, current ? "user event revision" : "user event report");
    if (result) { eventForm.reset(); state.editingEvent = null; eventForm.querySelector('button[type="submit"]').textContent = "登记用户线索"; await refresh(); }
  });

  one(root, "#supplemental-new-rule").addEventListener("click", () => {
    form.reset(); state.editingId = null; one(root, "#supplemental-editor-title").textContent = "创建规则草稿";
    one(root, "#supplemental-save-rule").textContent = "保存规则草稿"; setRuleTypeFields(root);
  });
  one(root, "#supplemental-refresh").addEventListener("click", () => refresh().catch((error) => showAlert(root, error.message, "error")));
  one(root, 'select[name="rule_type"]').addEventListener("change", () => setRuleTypeFields(root));
  one(root, 'select[name="selector_kind"]').addEventListener("change", () => setRuleTypeFields(root));
  one(root, 'select[name="category_kind"]').addEventListener("change", () => setRuleTypeFields(root));
  refresh().catch((error) => showAlert(root, error.message, "error"));
  setRuleTypeFields(root);
}

export {PENDING_KEY, request as supplementalRequest};
