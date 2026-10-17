import { get, post } from "./api.js";
import { el } from "./dom.js";
import { supplementalReasonLabel } from "./supplemental_items.js";

const SLOT = "zc-investment-assessment-pending-fit-v1";
const SAVED = "zc-investment-assessment-last-fit-v1";
const WAIT_MS = 1200;

export function operationRecord(operationId, body, context, phase = "prepared") {
  return { operation_id: operationId, body, context, phase, accepted: null, saved_at: new Date().toISOString() };
}

export function saveOperation(storage, key, record) {
  storage.setItem(key, JSON.stringify(record));
}

export function readOperation(storage, key) {
  try {
    const raw = storage.getItem(key);
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}

function ref(id, kind, revision = 1) {
  return id ? { kind, id, revision, field_path: null } : null;
}

function accountRows(value) {
  return Array.isArray(value) ? value : Array.isArray(value?.items) ? value.items : [];
}

function chosenAccountId(account) {
  return String(account?.id ?? account?.account_id ?? "");
}

function amount(value) {
  const raw = value.trim();
  return raw === "" ? null : raw;
}

function unknowns(values) {
  return Object.entries(values).filter(([, value]) => value === null).map(([name]) => name);
}

function fitKey(accountId) {
  return `${SLOT}:${accountId}`;
}

function fitBasis(context) {
  const exact = (value) => value?.id ? `${value.id}@${value.revision || 1}` : "missing";
  return `${exact(context.snapshot?.reference)} / ${exact(context.mandate_ref)} / ${context.valuation_binding?.state || "unknown"}:${exact(context.valuation_binding?.valuation_ref)}`;
}

function savedKey(accountId) {
  return `${SAVED}:${accountId}`;
}

function say(region, text, className = "notice") {
  region.replaceChildren(el("p", { class: className }, text));
}

function fitRefFromRun(run) {
  const refs = run?.fit_refs || [];
  return refs.length ? refs[0] : null;
}

function isTerminal(status) {
  return ["success", "partial", "failed", "cancelled", "completed"].includes(status);
}

function identity(refValue) {
  return refValue && typeof refValue.id === "string" ? refValue.id : null;
}

function purposeBinding(context, selectedPurpose) {
  const version = context.purpose_version;
  const base = context.purpose_binding || { state: "none_recorded" };
  if (!version) return base;
  const purposes = version.purposes || [];
  if (!purposes.length) return { state: "confirmed_empty", purpose_ref: version.reference, confirmed_at: version.confirmed_at };
  if (!selectedPurpose) return { state: "unselected" };
  return {
    state: "selected", purpose_ref: version.reference,
    purpose_id: selectedPurpose, confirmed_at: version.confirmed_at,
  };
}

function actionFor(context, listingRef) {
  const snapshot = context.snapshot;
  if (!snapshot) return "open";
  const holding = (snapshot.holdings || []).find((item) =>
    item.listing_ref?.kind === listingRef.kind && String(item.listing_ref?.id) === String(listingRef.id));
  if (holding) {
    const quantity = Number(holding.confirmed_quantity);
    return Number.isFinite(quantity) && quantity > 0 ? "add" : "open";
  }
  return snapshot.coverage === "complete" ? "open" : "hold";
}

function actionKnown(context, listingRef) {
  const snapshot = context.snapshot;
  if (!snapshot) return false;
  const holding = (snapshot.holdings || []).find((item) =>
    item.listing_ref?.kind === listingRef.kind && String(item.listing_ref?.id) === String(listingRef.id));
  return holding ? holding.confirmed_quantity !== null && holding.confirmed_quantity !== undefined : snapshot.coverage === "complete";
}

// Exported for read-only browser probes: the same renderer the panel uses.
export function renderFitView(region, view, basis, staleScenario = false) {
  const box = el("div", { class: "fit-result" });
  if (staleScenario) {
    box.dataset.staleScenario = "true";
    box.appendChild(el("p", { class: "notice notice-warn fit-stale-scenario" }, "以下状态来自上次已提交情景；当前表单已编辑。"));
  }
  const payload = view?.payload;
  const limits = view?.current_limits;
  const statusLabels = { pass: "财务约束检查：pass（已知条件通过）", warning: "财务约束检查：warning（有需注意条件）", blocked: "财务约束检查：blocked（存在已知约束冲突）", unknown: "财务约束检查：unknown（必要输入不足）" };
  const bucketLabels = {
    incomplete: "整体状态：incomplete（仍有未集成检查）",
    blocked: "整体状态：blocked（存在已知约束阻断）",
    ready: "整体状态：ready（满足本次已检查条件；不构成买卖建议）",
    limited: "整体状态：limited（存在非阻断限制）",
  };
  const financialStatus = el("p", { class: "fit-financial-status" }, statusLabels[payload?.financial_status] || `财务约束检查：${payload?.financial_status || "unknown"}`);
  if (staleScenario) financialStatus.textContent = `上次情景：${financialStatus.textContent}`;
  box.appendChild(financialStatus);
  box.appendChild(el("p", { class: "fit-status" }, bucketLabels[payload?.ranking_bucket] || `整体状态：${payload?.ranking_bucket || "incomplete"}`));
  // P3 supplemental composition: only integrated rows carry these fields, so
  // pre-P3 records (not_integrated) keep their previous display untouched.
  const bindingLabels = {
    bound: "规则绑定：bound（已绑定研究规则覆盖）",
    unknown: "规则绑定：unknown（绑定状态未知）",
  };
  const researchLabels = {
    current: "研究结论状态：current（当前有效）",
    review_required: "研究结论状态：REVIEW_REQUIRED（需要人工复核）",
    unavailable: "研究结论状态：unavailable（当前不可读取）",
  };
  const alignmentLabels = {
    supportive: "公司论据对齐：supportive（支持性，不构成买卖建议）",
    restrictive: "公司论据对齐：restrictive（限制性）",
    undetermined: "公司论据对齐：undetermined（未定）",
  };
  const supplementalBinding = payload?.supplemental_binding;
  if (supplementalBinding && supplementalBinding !== "not_integrated") {
    box.appendChild(el("p", { class: "fit-supplemental-binding" }, bindingLabels[supplementalBinding] || `规则绑定：${supplementalBinding}`));
    if (supplementalBinding === "unknown" && payload?.unknown_reason) {
      box.appendChild(el("p", { class: "fit-supplemental-unknown unknown-copy" }, `未知原因：${supplementalReasonLabel(payload.unknown_reason)}`));
    }
    if (supplementalBinding === "bound") {
      const generations = payload?.rules_generation != null && payload?.events_generation != null
        ? ` · 规则世代 R${payload.rules_generation} · 事件世代 E${payload.events_generation}` : "";
      box.appendChild(el("p", { class: "faint" }, `规则覆盖：${payload?.coverage_ref?.id || "未提供"}${generations}`));
    }
  }
  if (payload?.research_status && payload.research_status !== "not_assessed") {
    box.appendChild(el("p", { class: "fit-research-status" }, researchLabels[payload.research_status] || `研究结论状态：${payload.research_status}`));
  }
  if (payload?.company_alignment && payload.company_alignment !== "not_assessed") {
    box.appendChild(el("p", { class: "faint" }, alignmentLabels[payload.company_alignment] || `公司论据对齐：${payload.company_alignment}`));
  }
  const checks = payload?.checks || [];
  if (checks.length) {
    const list = el("ul", { class: "fit-checks" });
    for (const check of checks) {
      const reasons = (check.reason_codes?.length ? check.reason_codes : [check.code]).map((code) => {
        const labels = {
          complete_nav_missing: "账户净值不完整",
          minimum_lot_unknown: "最小交易单位未知",
          execution_price_unknown: "情景执行价格未知",
          fee_unknown: "费用假设未知",
          valuation_missing: "账户估值缺失",
          trading_rule_missing: "可信交易规则缺失",
          insufficient_cash: "可用现金低于情景所需金额",
          position_limit_exceeded: "单标的仓位比例检查",
          mandate_limit_exceeded: "情景与账户约束不符",
          "mandate:max_single_weight": "单标的仓位比例检查",
          cash_nonnegative: "检查账户情景后的可用现金是否为负",
          minimum_increment_unavailable: "缺少可信最小交易单位，无法构造交易情景",
          soft_limits: "软性限制尚未集成",
        };
        if (code.startsWith("mandate_constraint_unknown:")) return "当前账户约定未设置此项财务限制";
        if (code.startsWith("mandate_constraint_violation:")) return "情景超过账户约定的财务限制";
        if (code.startsWith("sector_coverage_missing:")) return "持仓行业分类覆盖不足";
        if (code.startsWith("valuation_price_missing:")) return "持仓估值价格缺失";
        return labels[code] || String(code).replaceAll("_", " ");
      });
      const state = { pass: "通过", blocked: "阻断", unknown: "未知", not_configured: "未配置" }[check.state] || check.state;
      const scope = check.scope === "baseline" ? "基于当前账户" : "基于试算情景";
      const violation = check.state === "blocked" && check.scope === "scenario" && reasons.some((reason) => reason === "单标的仓位比例检查" || reason.startsWith("情景超过"));
      list.appendChild(el("li", {}, `${reasons.join("、")}（${scope}）${violation ? "：超过约束" : `：${state}`}`));
    }
    box.appendChild(list);
  }
  const limitReasonLabels = {
    mandate_changed: "账户约定已变化",
    valuation_changed: "估值版本已变化",
    portfolio_snapshot_changed: "账户快照已变化",
    listing_sources_changed: "标的来源已变化",
    purposes_changed: "账户用途已变化",
    candidate_sources_changed: "候选来源已变化",
    source_policy_changed: "来源策略已变化",
    trade_assumptions_changed: "情景假设已变化",
  };
  const limitReasons = (limits?.reasons || []).map((code) => limitReasonLabels[code] || String(code).replaceAll("_", " "));
  if (limits) box.appendChild(el("p", { class: "faint" }, `当前证据：${limits.current_status || "unknown"}${limitReasons.length ? `；${limitReasons.join("、")}` : ""}${limits.refresh_required ? "；需要刷新" : ""}`));
  box.appendChild(el("p", { class: "faint" }, "生成时使用当时冻结的账户快照、约束和估值版本。"));
  box.appendChild(el("p", {}, "此结果只说明冻结输入下的适配状态；ready 只表示满足本次已检查条件，不构成买卖建议（工程功能，不代表生产启用）。"));
  const details = el("details");
  details.appendChild(el("summary", {}, "查看完整版本化记录"));
  if (basis) details.appendChild(el("p", { class: "faint" }, `冻结版本标识：${basis}`));
  details.appendChild(el("pre", { class: "raw-json" }, JSON.stringify(payload, null, 2)));
  box.appendChild(details);
  region.replaceChildren(box);
}

async function loadSavedFit(region, accountId) {
  const saved = readOperation(sessionStorage, savedKey(accountId));
  if (!saved?.fit_ref) return false;
  const refId = identity(saved.fit_ref);
  if (!refId) return false;
  try {
    const view = await get(`/portfolio/fit-assessments/${encodeURIComponent(refId)}?account_id=${encodeURIComponent(accountId)}&revision=1`);
    renderFitView(region, view, saved.basis, Boolean(saved.stale_scenario));
    return true;
  } catch (error) {
    say(region, `最近一次适配记录暂时无法读取：${error.message}`, "notice notice-error");
    return true;
  }
}

export function bootAccountFit() {
  const root = document.querySelector("#account-fit-root");
  const region = document.querySelector("#account-fit-content");
  if (!root || !region || root.dataset.fitMounted === "true") return;
  root.dataset.fitMounted = "true";
  let epoch = 0;
  const timers = new Map();
  const resumes = new Map();
  const inFlight = new Set();
  const retryDelays = new Map();
  let disposed = false;
  const tickEpoch = () => ++epoch;
  const stop = () => { disposed = true; tickEpoch(); for (const handle of timers.values()) clearTimeout(handle); timers.clear(); };
  const observer = new MutationObserver(() => { if (!root.isConnected) { stop(); observer.disconnect(); } });
  observer.observe(document.documentElement, { childList: true, subtree: true });
  const onVisibility = () => {
    if (document.hidden) { for (const handle of timers.values()) clearTimeout(handle); timers.clear(); }
    else for (const [key, resume] of resumes) if (!timers.has(key) && !inFlight.has(key)) resume();
  };
  document.addEventListener("visibilitychange", onVisibility);

  const listingId = root.dataset.listingId;
  if (!listingId) {
    say(region, "该标的尚未登记在受控证券身份名单中，账户适配检查不可用。", "notice notice-warn");
    return;
  }

  const load = async () => {
    const token = tickEpoch();
    say(region, "正在读取账户快照和适配输入…", "notice notice-loading");
    try {
      const accountsValue = await get("/portfolio/accounts");
      if (token !== epoch || disposed || !root.isConnected) return;
      const accounts = accountRows(accountsValue);
      if (!accounts.length) { say(region, "尚无账户。请先在持仓池登记账户。", "notice"); return; }
      const initialAccountId = chosenAccountId(accounts[0]);
      const contextValue = await get(`/portfolio/fit-context?account_id=${encodeURIComponent(initialAccountId)}&listing_id=${encodeURIComponent(listingId)}`);
      if (token !== epoch || disposed || !root.isConnected) return;
      const listing = contextValue.listing;
      if (!listing?.listing_ref || listing.listing_ref.kind !== "listing") {
        say(region, "受控证券身份信息暂不可用。", "notice notice-warn"); return;
      }
      mountForm({ root, region, accounts, listing, token, currentEpoch: () => epoch, bumpEpoch: tickEpoch, loadSavedFit, schedulePoll, restoreTerminalFit });
    } catch (error) {
      if (token === epoch && !disposed) say(region, `账户适配上下文加载失败：${error.message}`, "notice notice-error");
    }
  };

  function schedulePoll(runId, accountId, sourceRecord, _token, resultPane, delay = WAIT_MS) {
    if (disposed || document.hidden || !root.isConnected) return;
    const operationId = sourceRecord?.operation_id || sourceRecord?.body?.operation_id || "";
    const key = `${accountId}:${runId}:${operationId}`;
    if (inFlight.has(key)) return;
    if (timers.has(key)) clearTimeout(timers.get(key));
    const resume = () => schedulePoll(runId, accountId, sourceRecord, epoch, resultPane);
    resumes.set(key, resume);
    const handle = setTimeout(async () => {
      if (timers.get(key) !== handle) return;
      timers.delete(key);
      if (disposed || document.hidden || !root.isConnected) return;
      if (resultPane.parentElement?.querySelector('[name="account_id"]')?.value !== accountId) { resumes.delete(key); return; }
      inFlight.add(key);
      try {
        const run = await get(`/investment-assessment-runs/${encodeURIComponent(runId)}`);
        if (disposed || document.hidden || !root.isConnected) return;
        if (resultPane.parentElement?.querySelector('[name="account_id"]')?.value !== accountId) { resumes.delete(key); return; }
        const stored = readOperation(sessionStorage, fitKey(accountId));
        if (stored?.operation_id !== operationId || String(stored?.accepted?.run_ref?.id || runId) !== String(runId)) { resumes.delete(key); retryDelays.delete(key); return; }
        retryDelays.delete(key);
        if (isTerminal(run.status)) {
          const fitRef = fitRefFromRun(run);
          const terminal = { ...stored, phase: "terminal", terminal_status: run.status, terminal_error: run.error || null, fit_ref: fitRef };
          try {
            sessionStorage.setItem(`${savedKey(accountId)}:terminal`, JSON.stringify(terminal));
            if (fitRef) saveOperation(sessionStorage, fitKey(accountId), terminal);
          } catch { /* the durable accepted record can re-read the run after reload */ }
          if (fitRef) {
            const record = { fit_ref: fitRef, basis: terminal.context?.basis || "已保存版本", operation_id: terminal.operation_id, calculation_basis_key: null, purpose_binding_key: null, stale_scenario: Boolean(terminal.context?.form_dirty) };
            try {
              const view = await get(`/portfolio/fit-assessments/${encodeURIComponent(fitRef.id)}?account_id=${encodeURIComponent(accountId)}&revision=${fitRef.revision || 1}`);
              const current = readOperation(sessionStorage, fitKey(accountId));
              if (disposed || !root.isConnected || resultPane.parentElement?.querySelector('[name="account_id"]')?.value !== accountId || current?.operation_id !== operationId || current?.phase !== "terminal") { resumes.delete(key); return; }
              record.basis = current.context?.basis || record.basis;
              record.stale_scenario = Boolean(current.context?.form_dirty);
              record.calculation_basis_key = view?.payload?.calculation_basis_key || null;
              record.purpose_binding_key = view?.payload?.purpose_binding_key || null;
              sessionStorage.setItem(savedKey(accountId), JSON.stringify(record));
              sessionStorage.removeItem(fitKey(accountId));
              const submit = resultPane.parentElement?.querySelector('.fit-form button[type="submit"]');
              if (submit) submit.disabled = false;
              renderFitView(resultPane, view, record.basis, record.stale_scenario);
            } catch (error) {
              const current = readOperation(sessionStorage, fitKey(accountId));
              const stillCurrent = !disposed && root.isConnected && resultPane.parentElement?.querySelector('[name="account_id"]')?.value === accountId && current?.operation_id === operationId && current?.phase === "terminal";
              if (stillCurrent) {
                const retry = el("button", { type: "button", class: "btn btn-sm" }, "重试读取已完成记录");
                retry.addEventListener("click", () => restoreTerminalFit(resultPane, accountId, current));
                resultPane.replaceChildren(el("p", { class: "notice notice-error" }, `评估已结束，结果读取失败；原操作和结果引用仍已保存：${error.message}`), retry);
              } else resumes.delete(key);
            }
          } else {
            sessionStorage.removeItem(fitKey(accountId));
            const submit = resultPane.parentElement?.querySelector('.fit-form button[type="submit"]');
            if (submit) submit.disabled = false;
            say(resultPane, `评估已结束（${run.status}），未生成单项记录。检查当前输入后，可用“生成账户适配检查”按钮显式创建新操作。`, "notice notice-warn");
          }
          resumes.delete(key);
          return;
        }
        say(resultPane, `评估任务状态：${run.status || "running"}。页面保留同一任务，稍后继续检查。`, "notice notice-loading");
        inFlight.delete(key);
        schedulePoll(runId, accountId, stored, epoch, resultPane);
      } catch (error) {
        const stored = readOperation(sessionStorage, fitKey(accountId));
        const sameOperation = stored?.operation_id === operationId && String(stored?.accepted?.run_ref?.id || runId) === String(runId);
        const selectedAccount = resultPane.parentElement?.querySelector('[name="account_id"]')?.value === accountId;
        if (!sameOperation) { resumes.delete(key); retryDelays.delete(key); }
        else if (error.retryable || error.status === 0 || error.status >= 500) {
          if (selectedAccount) say(resultPane, `任务状态暂时无法读取；稍后会继续读取同一任务。${error.message}`, "notice notice-error");
          const previousDelay = retryDelays.get(key) || WAIT_MS;
          retryDelays.set(key, Math.min(previousDelay * 2, 30000));
          inFlight.delete(key);
          schedulePoll(runId, accountId, sourceRecord, epoch, resultPane, previousDelay);
        } else {
          resumes.delete(key);
          retryDelays.delete(key);
          if (selectedAccount) say(resultPane, `任务状态读取被拒绝（${error.status || "错误"}）；请检查登录状态并刷新页面以继续读取同一任务。${error.message}`, "notice notice-error");
        }
      } finally { inFlight.delete(key); }
    }, delay);
    timers.set(key, handle);
  }

  async function restoreTerminalFit(resultPane, accountId, terminalRecord) {
    const fitRef = terminalRecord.fit_ref;
    if (!fitRef?.id) return;
    try {
      const view = await get(`/portfolio/fit-assessments/${encodeURIComponent(fitRef.id)}?account_id=${encodeURIComponent(accountId)}&revision=${fitRef.revision || 1}`);
      const currentPending = readOperation(sessionStorage, fitKey(accountId));
      if (disposed || !root.isConnected || resultPane.parentElement?.querySelector('[name="account_id"]')?.value !== accountId || currentPending?.operation_id !== terminalRecord.operation_id || currentPending?.phase !== "terminal") return;
      const record = {
        fit_ref: fitRef, basis: currentPending.context?.basis || "已保存版本",
        operation_id: terminalRecord.operation_id, calculation_basis_key: null,
        purpose_binding_key: null, stale_scenario: Boolean(currentPending.context?.form_dirty),
      };
      record.calculation_basis_key = view?.payload?.calculation_basis_key || null;
      record.purpose_binding_key = view?.payload?.purpose_binding_key || null;
      sessionStorage.setItem(savedKey(accountId), JSON.stringify(record));
      sessionStorage.removeItem(fitKey(accountId));
      const submit = resultPane.parentElement?.querySelector('.fit-form button[type="submit"]');
      const current = resultPane.parentElement?.querySelector('[name="account_id"]')?.value === accountId;
      if (submit && current) submit.disabled = false;
      if (current) renderFitView(resultPane, view, record.basis, record.stale_scenario);
    } catch (error) {
      const currentPending = readOperation(sessionStorage, fitKey(accountId));
      if (!disposed && root.isConnected && currentPending?.operation_id === terminalRecord.operation_id && currentPending?.phase === "terminal" && resultPane.parentElement?.querySelector('[name="account_id"]')?.value === accountId) {
        const retry = el("button", { type: "button", class: "btn btn-sm" }, "重试读取已完成记录");
        retry.addEventListener("click", () => restoreTerminalFit(resultPane, accountId, currentPending));
        resultPane.replaceChildren(el("p", { class: "notice notice-error" }, `评估已结束，精确结果读取失败；原操作和结果引用仍已保存：${error.message}`), retry);
      }
    }
  }

  load();
}

function mountForm({ root, region, accounts, listing, currentEpoch, bumpEpoch, loadSavedFit, schedulePoll, restoreTerminalFit }) {
  const form = el("form", { class: "fit-form" });
  const accountSelect = el("select", { name: "account_id", required: true, "aria-label": "账户" });
  for (const account of accounts) {
    const id = chosenAccountId(account);
    accountSelect.appendChild(el("option", { value: id }, account.name || `账户 ${id.slice(0, 8)}`));
  }
  const accountField = el("label", { class: "fit-field" }); accountField.append(el("span", {}, "账户"), accountSelect); form.appendChild(accountField);
  const controls = {};
  const purposeSelect = el("select", { name: "purpose" });
  controls.purpose = purposeSelect;
  const purposeField = el("label", { class: "fit-field" }); purposeField.append(el("span", {}, "用途"), purposeSelect); form.appendChild(purposeField);
  for (const [label, name, type] of [
    ["操作情景", "action", "select"],
    ["情景执行价格", "execution_price", "number"], ["固定费用", "fixed_fee", "number"],
    ["费率（小数）", "fee_rate", "number"],
  ]) {
    const control = type === "select" ? el("select", { name }) : el("input", { name, type: "number", min: "0", step: "any", inputmode: "decimal" });
    controls[name] = control;
    if (name === "action") for (const [value, text] of [["open", "新标的情景"], ["add", "已有仓位情景"], ["hold", "仅评估当前事实"]]) control.appendChild(el("option", { value }, text));
    const field = el("label", { class: "fit-field" }); field.append(el("span", {}, label), control); form.appendChild(field);
  }
  const unknownNote = el("p", { class: "fit-unknown-note" }, "执行价格或费用留空时会明确记录为 unknown；空白费用不会当作零。估值缺失时仍可使用明确的未知输入。");
  form.appendChild(unknownNote);
  const gaps = el("p", { class: "fit-context-note" }); form.appendChild(gaps);
  const submit = el("button", { type: "submit", class: "btn btn-primary" }, "生成账户适配检查"); form.appendChild(submit);
  const result = el("div", { class: "fit-operation-result", "aria-live": "polite" });
  region.replaceChildren(form, result);

  let context = null;
  let activeToken = currentEpoch();
  let currentContext = null;
  let activeOperation = null;
  let contextLoadId = 0;
  const setUncertainNotice = (record, accountId) => {
    const message = el("p", { class: "notice notice-warn" }, "上次提交未收到确认。可以用相同操作 ID 和完整输入重新提交以查询原收据。");
    const retry = el("button", { type: "button", class: "btn btn-sm" }, "用相同操作 ID 重试确认");
    retry.addEventListener("click", () => submitRecord(record, accountId, true));
    result.replaceChildren(message, retry);
  };
  const setPreparedNotice = (record, accountId) => {
    submit.disabled = true;
    const message = el("p", { class: "notice notice-warn" }, "完整请求已保存，但发送状态未能保存；此操作尚未发送。可继续发送原请求，或安全解除待发送状态。");
    const retry = el("button", { type: "button", class: "btn btn-sm" }, "继续发送已准备操作");
    const release = el("button", { type: "button", class: "btn btn-sm" }, "解除未发送操作");
    retry.addEventListener("click", () => submitRecord(record, accountId));
    release.addEventListener("click", () => {
      sessionStorage.removeItem(fitKey(accountId));
      if (accountSelect.value === accountId) { submit.disabled = !currentContext?.snapshot || !currentContext?.mandate_ref; result.replaceChildren(); }
    });
    result.replaceChildren(message, retry, release);
  };
  const submitRecord = async (record, accountId, isRetry = false) => {
    const token = currentEpoch();
    submit.disabled = true;
    say(result, isRetry ? "正在用原操作 ID 查询提交结果…" : "已保存操作 ID，正在提交…", "notice notice-loading");
    record.phase = "sending";
    try {
      saveOperation(sessionStorage, fitKey(accountId), record);
    } catch {
      record.phase = "prepared";
      if (token === currentEpoch()) setPreparedNotice(record, accountId);
      return;
    }
    try {
      const receipt = await post("/portfolio/fit-assessments", record.body, { idempotency: false });
      const durable = readOperation(sessionStorage, fitKey(accountId));
      if (durable?.operation_id === record.operation_id) {
        const accepted = { ...durable, phase: "accepted", accepted: receipt };
        saveOperation(sessionStorage, fitKey(accountId), accepted);
        activeOperation = accepted;
        if (accountSelect.value === accountId) say(result, "请求已接受，正在跟踪任务…", "notice notice-loading");
        schedulePoll(receipt.run_ref.id, accountId, accepted, token, result);
      } else if (accountSelect.value === accountId) {
        say(result, "旧操作已收到回执，但当前账户已有其他操作；不会覆盖当前操作。", "notice notice-warn");
      }
    } catch (error) {
      const durable = readOperation(sessionStorage, fitKey(accountId));
      if (durable?.operation_id !== record.operation_id) return;
      if ([401, 403].includes(error.status)) {
        const uncertain = { ...durable, phase: "uncertain" };
        try { saveOperation(sessionStorage, fitKey(accountId), uncertain); activeOperation = uncertain; } catch { /* keep original durable record */ }
        if (token === currentEpoch()) setUncertainNotice(uncertain, accountId);
      } else if ([409, 422].includes(error.status)) {
        if (activeOperation?.operation_id === record.operation_id) activeOperation = null;
        sessionStorage.removeItem(fitKey(accountId));
        if (token === currentEpoch()) {
          submit.disabled = !currentContext?.snapshot || !currentContext?.mandate_ref;
          say(result, `服务端拒绝了原输入（${error.status}）：${error.message}。已解除待确认锁；请先检查输入。`, "notice notice-error");
        }
      } else {
        const uncertain = { ...durable, phase: "uncertain" };
        try { saveOperation(sessionStorage, fitKey(accountId), uncertain); activeOperation = uncertain; } catch { /* keep original durable record */ }
        if (token === currentEpoch()) setUncertainNotice(uncertain, accountId);
      }
    }
  };
  const contextLoad = async () => {
    const accountId = accountSelect.value;
    const requestId = ++contextLoadId;
    const requestToken = bumpEpoch();
    activeToken = requestToken;
    context = null;
    currentContext = null;
    submit.disabled = true;
    result.replaceChildren(el("p", { class: "notice notice-loading" }, "正在读取此账户的精确版本…"));
    try {
      const response = await get(`/portfolio/fit-context?account_id=${encodeURIComponent(accountId)}&listing_id=${encodeURIComponent(listing.listing_ref.id)}`);
      if (requestId !== contextLoadId || accountSelect.value !== accountId || !root.isConnected) return;
      context = response;
      currentContext = response;
      purposeSelect.replaceChildren();
      const purposes = response.purpose_version?.purposes || [];
      if (!purposes.length) purposeSelect.appendChild(el("option", { value: "" }, response.purpose_version ? "已确认无用途" : "未记录用途"));
      else {
        purposeSelect.appendChild(el("option", { value: "" }, "选择账户用途"));
        for (const purpose of purposes) purposeSelect.appendChild(el("option", { value: purpose.id }, purpose.name || purpose.label || purpose.id));
      }
      const action = actionFor(response, listing.listing_ref);
      controls.action.value = action;
      const actionIsKnown = actionKnown(response, listing.listing_ref);
      controls.action.disabled = !actionIsKnown;
      const missing = [...(response.gaps || [])];
      const enabled = response.enabled || {};
      const notes = [];
      if (!response.snapshot) { notes.push("尚无已确认账户快照，不能提交"); submit.disabled = true; }
      else submit.disabled = false;
      if (!actionIsKnown) { notes.push("该标的持仓状态未确认，不能选择适配情景"); submit.disabled = true; }
      if (missing.includes("valuation_missing")) notes.push("当前估值缺失；可以继续，缺失估值将按 unknown 记录");
      if (missing.includes("account_constraints_not_configured")) notes.push("当前约定尚未绑定此账户；财务限制将按 unknown 记录");
      if (!response.mandate_ref) { notes.push("尚无已保存的约定，不能提交"); submit.disabled = true; }
      if (enabled.account_fit === false) { notes.push("账户适配功能当前关闭"); submit.disabled = true; }
      if (enabled.portfolio_ledger === false) { notes.push("账户台账功能当前关闭"); submit.disabled = true; }
      const listingSource = response.listing_source;
      if (listingSource) notes.push(`标的参考来源：${listingSource.price?.provider || listingSource.listing_source_ref?.kind || "已登记证券身份"}${listingSource.price?.price == null ? "；价格未知" : `；缓存价 ${listingSource.price.price}`}`);
      gaps.textContent = notes.join("。") || "账户快照、约定与标的信息已读取。";
      const basis = fitBasis(response);
      const saved = readOperation(sessionStorage, savedKey(accountId));
      if (saved?.fit_ref) {
        const stale = saved.basis !== basis;
        const history = el("details", { class: "fit-saved-result" });
        history.appendChild(el("summary", {}, stale ? "查看上次已完成记录（基于旧输入）" : "查看上次已完成记录"));
        const savedPanel = el("div");
        history.appendChild(savedPanel);
        result.replaceChildren(history);
        loadSavedFit(savedPanel, accountId);
      }
      const pending = readOperation(sessionStorage, fitKey(accountId));
      if (pending?.phase === "accepted" && pending.accepted?.run_ref?.id) {
        activeOperation = pending;
        result.prepend(el("p", { class: "notice notice-loading" }, "已接受任务，恢复跟踪同一个 run；不会重新发送请求。"));
        schedulePoll(pending.accepted.run_ref.id, accountId, pending, requestToken, result);
      } else if (pending?.phase === "terminal" && pending.fit_ref?.id) {
        activeOperation = pending;
        restoreTerminalFit(result, accountId, pending);
      } else if (pending?.phase === "prepared") {
        setPreparedNotice(pending, accountId);
      } else if (pending?.phase === "sending" || pending?.phase === "uncertain") {
        activeOperation = pending;
        submit.disabled = true;
        setUncertainNotice(pending, accountId);
      }
    } catch (error) {
      if (requestId === contextLoadId && accountSelect.value === accountId && root.isConnected) { submit.disabled = true; gaps.textContent = `账户上下文读取失败：${error.message}`; }
    }
  };
  accountSelect.addEventListener("change", contextLoad);
  controls.action.addEventListener("change", () => {
    const hold = controls.action.value === "hold";
    for (const key of ["execution_price", "fixed_fee", "fee_rate"]) controls[key].disabled = hold;
  });
  form.addEventListener("input", () => {
    activeToken = bumpEpoch();
    const operationAccountId = activeOperation?.body?.account_id || activeOperation?.context?.account_id;
    if (activeOperation && operationAccountId === accountSelect.value && ["prepared", "sending", "uncertain", "accepted", "terminal"].includes(activeOperation.phase)) {
      const durable = readOperation(sessionStorage, fitKey(operationAccountId));
      if (durable?.operation_id === activeOperation.operation_id && ["prepared", "sending", "uncertain", "accepted", "terminal"].includes(durable.phase)) {
        durable.context = { ...durable.context, form_dirty: true };
        try { saveOperation(sessionStorage, fitKey(operationAccountId), durable); activeOperation = durable; } catch { /* accepted body remains recoverable */ }
      }
    }
    const liveResult = result.querySelector(".fit-result");
    if (liveResult && liveResult.dataset.staleScenario !== "true") {
      liveResult.dataset.staleScenario = "true";
      liveResult.prepend(el("p", { class: "notice notice-warn fit-stale-scenario" }, "以下状态来自上次已提交情景；当前表单已编辑。"));
      const heading = liveResult.querySelector(".fit-financial-status");
      if (heading) heading.textContent = `上次情景：${heading.textContent}`;
    }
    const saved = readOperation(sessionStorage, savedKey(accountSelect.value));
    if (saved?.fit_ref) gaps.textContent = "当前表单已变化；最近一次记录仍保留生成时的输入版本。";
    const pending = readOperation(sessionStorage, fitKey(accountSelect.value));
    if (pending?.phase === "accepted" && pending.accepted?.run_ref?.id) {
      schedulePoll(pending.accepted.run_ref.id, accountSelect.value, pending, activeToken, result);
    }
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!context?.snapshot || !context?.mandate_ref || !listing.listing_ref || submit.disabled) return;
    const accountId = accountSelect.value;
    const existing = readOperation(sessionStorage, fitKey(accountId));
    if (existing) { gaps.textContent = "存在尚未完成确认的操作；页面不会自动创建新操作。"; return; }
    const action = controls.action.value;
    const values = {
      execution_price: amount(controls.execution_price.value),
      fixed_fee: amount(controls.fixed_fee.value),
      fee_rate: amount(controls.fee_rate.value),
    };
    const item = { listing_ref: listing.listing_ref, action };
    if (action !== "hold") item.trade_assumptions = { ...values, assumption_source: "user_scenario", unknown_fields: unknowns(values) };
    const body = {
      operation_id: crypto.randomUUID(), account_id: accountId,
      snapshot_ref: context.snapshot.reference, mandate_ref: context.mandate_ref,
      valuation_binding: context.valuation_binding,
      purpose_binding: purposeBinding(context, purposeSelect.value || null), items: [item],
    };
    const basis = fitBasis(context);
    const record = operationRecord(body.operation_id, body, { basis, account_id: accountId }, "prepared");
    activeOperation = record;
    try { saveOperation(sessionStorage, fitKey(accountId), record); }
    catch { activeOperation = null; gaps.textContent = "浏览器无法保存稳定操作 ID 和完整请求，本次未发送。请启用会话存储后重试。"; return; }
    const submittedEpoch = currentEpoch();
    try {
      await submitRecord(record, accountId);
    } catch (error) {
      if (submittedEpoch === currentEpoch()) say(result, `本地操作状态无法更新：${error.message}`, "notice notice-error");
    }
  });
  contextLoad();
}
