import { randomUUID } from "./uuid.js";
import { get, post } from "./api.js";
import { el } from "./dom.js";
import { operationRecord, readOperation, saveOperation } from "./account_fit.js";

const SLOT = "zc-investment-assessment-pending-ranking-v1";
const SAVED = "zc-investment-assessment-last-ranking-v1";
const WAIT_MS = 1200;
const TERMINAL = new Set(["success", "partial", "failed", "cancelled", "completed"]);

function accountList(value) { return Array.isArray(value) ? value : Array.isArray(value?.items) ? value.items : []; }
function accountId(account) { return String(account?.id ?? account?.account_id ?? ""); }
function readSaved(key) { return readOperation(sessionStorage, key); }
function writeSaved(key, value) { saveOperation(sessionStorage, key, value); }
function pendingKey(id) { return `${SLOT}:${id}`; }
function savedKey(id) { return `${SAVED}:${id}`; }
function say(node, text, type = "notice") { node.replaceChildren(el("p", { class: type }, text)); }
function unlockSubmit(resultPane, id) {
  const form = resultPane.parentElement?.querySelector(".ranking-form");
  if (form?.querySelector('[name="account_id"]')?.value === id) form.querySelector('button[type="submit"]').disabled = false;
}
function amount(value) { const raw = value.trim(); return raw === "" ? null : raw; }
function unknowns(values) { return Object.entries(values).filter(([, value]) => value === null).map(([name]) => name); }
function statusText(entry) { return `${entry.financial_status || "unknown"} · ${entry.ranking_bucket || "incomplete"}`; }
function reasonText(code) {
  const known = {
    minimum_lot_unknown: "最小交易单位未知",
    execution_price_unknown: "情景执行价格未知",
    fee_unknown: "费用假设未知",
    valuation_missing: "账户估值缺失",
    trading_rule_missing: "可信交易规则缺失",
    insufficient_cash: "可用现金不足",
    position_limit_exceeded: "超过单标的限制",
    cash_nonnegative: "检查情景后可用现金是否为负",
    complete_nav_missing: "账户净值不完整",
    mandate_changed: "账户约定已变化",
    valuation_changed: "估值版本已变化",
    portfolio_snapshot_changed: "账户快照已变化",
    listing_sources_changed: "标的来源已变化",
    purposes_changed: "账户用途已变化",
    candidate_sources_changed: "候选来源已变化",
    source_policy_changed: "来源策略已变化",
    trade_assumptions_changed: "情景假设已变化",
  };
  if (String(code).startsWith("mandate_constraint_unknown:")) return "账户约定未设置此项财务限制";
  if (String(code).startsWith("mandate_constraint_violation:")) return "超过账户约定的财务限制";
  if (String(code).startsWith("sector_coverage_missing:")) return "持仓行业分类覆盖不足";
  if (String(code).startsWith("valuation_price_missing:")) return "持仓估值价格缺失";
  return known[code] || String(code).replaceAll("_", " ");
}

function buildPurposeBinding(context, state, purposeId, reason) {
  if (state === "selected") return { state, purpose_ref: context.purpose_version.reference, purpose_id: purposeId, confirmed_at: context.purpose_version.confirmed_at };
  if (state === "confirmed_empty") return { state, purpose_ref: context.purpose_version.reference, confirmed_at: context.purpose_version.confirmed_at };
  if (state === "unknown") return { state, reason };
  return { state };
}

function renderRankingPage(region, response, tokenIsCurrent) {
  const view = el("div", { class: "ranking-view" });
  const snapshot = response.snapshot || {};
  view.appendChild(el("p", {}, `生成时证据：${snapshot.current_status || "unknown"}；覆盖 ${snapshot.coverage || "unknown"}；结果数 ${snapshot.entry_count ?? response.entries?.length ?? 0}。`));
  const limits = el("p", { class: "faint ranking-current-limits" });
  view.appendChild(limits);
  const filter = el("select", { "aria-label": "财务状态筛选" });
  for (const [value, label] of [["", "全部状态"], ["pass", "pass"], ["warning", "warning"], ["blocked", "blocked"], ["unknown", "unknown"]]) filter.appendChild(el("option", { value }, label));
  const list = el("ol", { class: "ranking-entries" });
  const pager = el("div", { class: "ranking-pager" });
  const next = el("button", { type: "button", class: "btn btn-sm" }, "下一页");
  const status = el("p", { class: "faint", "aria-live": "polite" });
  pager.append(status, next);
  view.append(filter, list, pager);
  let cursor = "";
  let epoch = 0;
  const load = async (reset = false) => {
    if (reset) cursor = "";
    const token = ++epoch;
    list.replaceChildren(el("li", { class: "notice notice-loading" }, "正在读取排序条目…"));
    next.disabled = true;
    const params = new URLSearchParams({ account_id: snapshot.account_id, revision: "1", limit: "50" });
    if (cursor) params.set("cursor", cursor);
    if (filter.value) params.set("financial_status", filter.value);
    try {
      const page = await get(`/candidates/rankings/${encodeURIComponent(snapshot.id)}?${params}`);
      if (token !== epoch || !tokenIsCurrent()) return;
      const currentLimits = page.current_limits || {};
      limits.textContent = `当前证据：${currentLimits.current_status || "unknown"}${currentLimits.refresh_required ? "；需要刷新" : ""}${currentLimits.reasons?.length ? `；${currentLimits.reasons.map(reasonText).join("、")}` : ""}`;
      cursor = page.next_cursor || "";
      list.replaceChildren();
      for (const entry of page.entries || []) {
        const row = el("li", { class: `ranking-entry ranking-${entry.ranking_bucket || "incomplete"}` });
        const ticker = entry.listing_ticker || entry.listing_ref?.id || "未知代码";
        const name = entry.listing_name || "名称未知";
        const title = `${ticker} · ${name}`;
        row.append(el("strong", {}, `${entry.ordinal + 1}. ${title}`), el("span", { class: "ranking-financial-status" }, `财务状态：${entry.financial_status || "unknown"}`), el("span", {}, `整体状态：${entry.ranking_bucket || "incomplete"}`));
        if (entry.resolved_action) row.appendChild(el("span", { class: "faint" }, `情景：${entry.resolved_action}`));
        for (const check of entry.checks || []) {
          const reasons = (check.reason_codes?.length ? check.reason_codes : [check.code]).filter(Boolean).map(reasonText).join("、");
          row.appendChild(el("p", { class: "faint" }, `检查：${reasons || "未说明"} · ${check.state || "unknown"}`));
        }
        if (entry.execution_error) row.appendChild(el("p", { class: "notice notice-warn" }, entry.execution_error));
        const sourceDetails = el("details");
        sourceDetails.appendChild(el("summary", {}, "查看来源与检查项"));
        sourceDetails.addEventListener("toggle", async () => {
          if (!sourceDetails.open || sourceDetails.dataset.loaded) return;
          sourceDetails.dataset.loaded = "true";
          try {
            const sources = await get(`/candidates/rankings/${encodeURIComponent(snapshot.id)}/entries/${encodeURIComponent(entry.listing_ref.id)}?account_id=${encodeURIComponent(snapshot.account_id)}&revision=1`);
            if (token !== epoch || !tokenIsCurrent()) return;
            for (const check of sources.entry?.checks || []) {
              const reasons = (check.reason_codes?.length ? check.reason_codes : [check.code]).filter(Boolean).map(reasonText).join("、");
              sourceDetails.appendChild(el("p", {}, `检查：${reasons || "未说明"} · ${check.state || "unknown"}`));
            }
            for (const source of sources.sources || []) {
              const label = [source.source_kind, source.template_id, source.template_version, source.provider, source.status, source.note].filter(Boolean).join(" · ");
              sourceDetails.appendChild(el("p", { class: "faint" }, `来源：${label || source.source_ref?.kind || "已冻结来源"}`));
            }
            const full = el("details");
            full.appendChild(el("summary", {}, "查看完整来源记录"));
            full.appendChild(el("pre", { class: "raw-json" }, JSON.stringify(sources, null, 2)));
            sourceDetails.appendChild(full);
          } catch (error) {
            sourceDetails.appendChild(el("p", { class: "notice notice-error" }, `来源读取失败：${error.message}`));
          }
        });
        row.appendChild(sourceDetails);
        list.appendChild(row);
      }
      if (!list.children.length) list.appendChild(el("li", { class: "notice" }, "本页没有匹配条目。"));
      status.textContent = cursor ? "还有后续条目。" : "已到当前筛选结果末尾。";
      next.disabled = !cursor;
    } catch (error) {
      if (token !== epoch || !tokenIsCurrent()) return;
      list.replaceChildren(el("li", { class: "notice notice-error" }, `排序条目加载失败：${error.message}`));
      next.disabled = true;
    }
  };
  filter.addEventListener("change", () => { epoch++; load(true); });
  next.addEventListener("click", () => { epoch++; load(false); });
  load(true);
  return view;
}

export function bootCandidateRanking() {
  const root = document.querySelector("#candidate-ranking-root");
  const region = document.querySelector("#candidate-ranking-content");
  if (!root || !region || root.dataset.rankingMounted === "true") return;
  root.dataset.rankingMounted = "true";
  let epoch = 0;
  const timers = new Map();
  const resumes = new Map();
  const inFlight = new Set();
  const retryDelays = new Map();
  let disposed = false;
  const nextEpoch = () => ++epoch;
  const observer = new MutationObserver(() => { if (!root.isConnected) { disposed = true; nextEpoch(); for (const handle of timers.values()) clearTimeout(handle); timers.clear(); observer.disconnect(); } });
  observer.observe(document.documentElement, { childList: true, subtree: true });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) { for (const handle of timers.values()) clearTimeout(handle); timers.clear(); }
    else for (const [key, resume] of resumes) if (!timers.has(key) && !inFlight.has(key)) resume();
  });

  const load = async () => {
    const token = nextEpoch();
    try {
      const accounts = accountList(await get("/portfolio/accounts"));
      if (token !== epoch || disposed) return;
      if (!accounts.length) { say(region, "尚无账户。请先在持仓池登记账户。", "notice"); return; }
      const account = accounts[0];
      const context = await get(`/portfolio/fit-context?account_id=${encodeURIComponent(accountId(account))}`);
      if (token !== epoch || disposed) return;
      mount({ root, region, accounts, initialContext: context, token, currentEpoch: () => epoch, bumpEpoch: nextEpoch, poll });
    } catch (error) { if (token === epoch) say(region, `排序上下文加载失败：${error.message}`, "notice notice-error"); }
  };

function poll(runId, account, record, _token, resultPane, delay = WAIT_MS) {
    if (disposed || document.hidden || !root.isConnected) return;
    const id = accountId(account);
    const operationId = record?.operation_id || record?.body?.operation_id || "";
    const key = `${id}:${runId}:${operationId}`;
    if (inFlight.has(key)) return;
    if (timers.has(key)) clearTimeout(timers.get(key));
    const resume = () => poll(runId, account, record, epoch, resultPane);
    resumes.set(key, resume);
    const handle = setTimeout(async () => {
      if (timers.get(key) !== handle) return;
      timers.delete(key);
      if (disposed || document.hidden || !root.isConnected) return;
      if (resultPane.parentElement?.querySelector('[name="account_id"]')?.value !== id) { resumes.delete(key); return; }
      inFlight.add(key);
      try {
        const run = await get(`/investment-assessment-runs/${encodeURIComponent(runId)}`);
        if (disposed || document.hidden || !root.isConnected) return;
        if (resultPane.parentElement?.querySelector('[name="account_id"]')?.value !== id) { resumes.delete(key); return; }
        const stored = readSaved(pendingKey(id));
        if (stored?.operation_id !== operationId || String(stored?.accepted?.run_ref?.id || runId) !== String(runId)) { resumes.delete(key); retryDelays.delete(key); return; }
        retryDelays.delete(key);
        if (TERMINAL.has(run.status)) {
          const rankingRef = run.ranking_ref;
          const terminal = { ...stored, phase: "terminal", terminal_status: run.status, terminal_error: run.error || null, ranking_ref: rankingRef || null };
          try {
            writeSaved(`${savedKey(id)}:terminal`, terminal);
            if (rankingRef) writeSaved(pendingKey(id), terminal);
          } catch { /* the durable accepted record can re-read the run after reload */ }
          if (!rankingRef) {
            try { sessionStorage.removeItem(pendingKey(id)); } catch { /* terminal run has no snapshot to recover */ }
            unlockSubmit(resultPane, id);
            say(resultPane, `排序任务已结束（${run.status}），尚未生成排序快照。检查当前输入后，可用“生成账户候选排序”按钮显式创建新操作。`, "notice notice-warn");
            resumes.delete(key);
            return;
          }
          await finalizeRanking(resultPane, account, terminal);
          resumes.delete(key);
          return;
        }
        say(resultPane, `排序任务状态：${run.status || "running"}。正在跟踪同一任务。`, "notice notice-loading");
        inFlight.delete(key);
        poll(runId, account, stored, epoch, resultPane);
      } catch (error) {
        const stored = readSaved(pendingKey(id));
        const sameOperation = stored?.operation_id === operationId && String(stored?.accepted?.run_ref?.id || runId) === String(runId);
        const selectedAccount = resultPane.parentElement?.querySelector('[name="account_id"]')?.value === id;
        if (!sameOperation) { resumes.delete(key); retryDelays.delete(key); }
        else if (error.retryable || error.status === 0 || error.status >= 500) {
          if (selectedAccount) say(resultPane, `任务状态读取失败；稍后会继续读取同一任务：${error.message}`, "notice notice-error");
          const previousDelay = retryDelays.get(key) || WAIT_MS;
          retryDelays.set(key, Math.min(previousDelay * 2, 30000));
          inFlight.delete(key);
          poll(runId, account, record, epoch, resultPane, previousDelay);
        } else {
          resumes.delete(key);
          retryDelays.delete(key);
          if (selectedAccount) say(resultPane, `任务状态读取被拒绝（${error.status || "错误"}）；请检查登录状态并刷新页面以继续读取同一任务。${error.message}`, "notice notice-error");
        }
      }
      finally { inFlight.delete(key); }
    }, delay);
    timers.set(key, handle);
  }

  async function finalizeRanking(resultPane, account, terminal) {
    const id = accountId(account);
    try {
      const pending = readSaved(pendingKey(id));
      if (pending?.operation_id !== terminal.operation_id || pending?.phase !== "terminal") return;
      const saved = { ranking_ref: terminal.ranking_ref, basis: pending.context.basis, account_id: id, operation_id: terminal.operation_id, stale_scenario: Boolean(pending.context.form_dirty) };
      writeSaved(savedKey(id), saved);
      sessionStorage.removeItem(pendingKey(id));
      unlockSubmit(resultPane, id);
      if (resultPane.parentElement?.querySelector('[name="account_id"]')?.value === id) await renderSaved(resultPane, saved, () => !disposed && root.isConnected && resultPane.parentElement?.querySelector('[name="account_id"]')?.value === id && readSaved(savedKey(id))?.operation_id === terminal.operation_id && !readSaved(pendingKey(id)), true);
    } catch (error) {
      if (resultPane.parentElement?.querySelector('[name="account_id"]')?.value === id) say(resultPane, `排序已完成，结果索引保存失败；重新打开页面会继续读取同一任务：${error.message}`, "notice notice-error");
    }
  }
  load();
}

async function renderSaved(region, saved, tokenIsCurrent, expand = false) {
  const details = el("details", { class: "ranking-saved", open: expand });
  details.appendChild(el("summary", {}, "查看最近一次已完成排序"));
  if (saved.stale_scenario) details.appendChild(el("p", { class: "notice notice-warn ranking-stale-scenario" }, "以下排序来自上次已提交情景；当前表单已编辑。"));
  const panel = el("div");
  details.appendChild(panel);
  region.prepend(details);
  await loadSnapshot(panel, saved, tokenIsCurrent);
}

async function loadSnapshot(panel, saved, tokenIsCurrent) {
  try {
    const id = saved.ranking_ref.id;
    const params = new URLSearchParams({ account_id: saved.account_id, revision: String(saved.ranking_ref.revision || 1), limit: "50" });
    const response = await get(`/candidates/rankings/${encodeURIComponent(id)}?${params}`);
    if (!tokenIsCurrent()) return;
    panel.appendChild(el("p", { class: "faint" }, "生成时使用当时冻结的账户快照、约束和估值版本。"));
    const basisDetails = el("details");
    basisDetails.append(el("summary", {}, "查看冻结版本标识"), el("p", { class: "faint" }, saved.basis));
    panel.appendChild(basisDetails);
    panel.appendChild(renderRankingPage(panel, response, tokenIsCurrent));
  } catch (error) { if (tokenIsCurrent()) say(panel, `已完成排序记录读取失败：${error.message}`, "notice notice-error"); }
}

function mount({ root, region, accounts, initialContext, currentEpoch, bumpEpoch, poll }) {
  const form = el("form", { class: "ranking-form" });
  const account = el("select", { name: "account_id", required: true, "aria-label": "账户" });
  for (const row of accounts) account.appendChild(el("option", { value: accountId(row) }, row.name || `账户 ${accountId(row).slice(0, 8)}`));
  const accountLabel = el("label", { class: "fit-field" }); accountLabel.append(el("span", {}, "账户"), account); form.appendChild(accountLabel);
  const purposeState = el("select", { name: "purpose_state", required: true, "aria-label": "用途状态" });
  for (const [value, label] of [["selected", "已选择用途"], ["confirmed_empty", "已确认无用途"], ["none_recorded", "尚无记录"], ["unselected", "未选择"], ["unknown", "用途未知"]]) purposeState.appendChild(el("option", { value }, label));
  purposeState.value = initialContext.purpose_binding?.state || "none_recorded";
  const purposeSelect = el("select", { name: "purpose_id", "aria-label": "账户用途" });
  const purposeReason = el("input", { type: "text", name: "purpose_reason", maxlength: "200", placeholder: "说明用途未知原因" });
  const purposeField = el("label", { class: "fit-field" }); purposeField.append(el("span", {}, "用途"), purposeState, purposeSelect, purposeReason); form.appendChild(purposeField);
  const priceConfirmation = el("input", { type: "checkbox", name: "price_confirmation" });
  const confirmLabel = el("label", { class: "ranking-confirm" });
  confirmLabel.append(priceConfirmation, el("span", {}, "我确认将账户估值价格用作本次排序的情景价格"));
  form.appendChild(confirmLabel);
  const fixedFee = el("input", { type: "number", min: "0", step: "any", name: "fixed_fee", inputmode: "decimal" });
  const feeRate = el("input", { type: "number", min: "0", step: "any", name: "fee_rate", inputmode: "decimal" });
  for (const [text, control] of [["固定费用（留空表示 unknown）", fixedFee], ["费率（小数，留空表示 unknown）", feeRate]]) {
    const label = el("label", { class: "fit-field" }); label.append(el("span", {}, text), control); form.appendChild(label);
  }
  const contextNote = el("p", { class: "fit-context-note" });
  form.appendChild(contextNote);
  form.appendChild(el("p", { class: "fit-unknown-note" }, "费用空值会明确记录为 unknown。排序来源只使用本页提供的模板；本轮不会自动登记新来源。"));
  const submit = el("button", { type: "submit", class: "btn btn-primary" }, "生成账户候选排序"); form.appendChild(submit);
  const results = el("div", { class: "ranking-results", "aria-live": "polite" });
  region.replaceChildren(form, results);
  let context = initialContext;
  let localEpoch = currentEpoch();
  let activeOperation = null;
  let contextLoadId = 0;
  const updatePurposeOptions = () => {
    purposeSelect.replaceChildren();
    const purposes = context?.purpose_version?.purposes || [];
    for (const purpose of purposes) purposeSelect.appendChild(el("option", { value: purpose.id }, purpose.name || purpose.label || purpose.id));
    purposeSelect.disabled = purposeState.value !== "selected" || purposes.length === 0;
    purposeSelect.hidden = purposeSelect.disabled;
    purposeReason.hidden = purposeState.value !== "unknown";
  };
  updatePurposeOptions();
  purposeState.addEventListener("change", updatePurposeOptions);

  const refresh = async () => {
    const accountIdAtRequest = account.value;
    const requestId = ++contextLoadId;
    const token = bumpEpoch(); localEpoch = token;
    submit.disabled = true; contextNote.textContent = "正在读取此账户的精确版本…";
    try {
      context = null;
      const response = await get(`/portfolio/fit-context?account_id=${encodeURIComponent(accountIdAtRequest)}`);
      if (requestId !== contextLoadId || account.value !== accountIdAtRequest || !root.isConnected) return;
      context = response;
      purposeState.value = context.purpose_binding?.state || "none_recorded";
      updatePurposeOptions();
      const gaps = context.gaps || [];
      const reasons = [];
      const templates = context.source_templates || [];
      if (!context.snapshot) reasons.push("尚无已确认账户快照");
      if (!context.mandate_ref) reasons.push("尚无已保存的约定");
      if (!templates.length) reasons.push("当前没有可用来源模板");
      if (context.enabled?.candidate_ranking === false) reasons.push("候选排序功能当前关闭");
      if (context.enabled?.portfolio_ledger === false) reasons.push("账户台账功能当前关闭");
      if (gaps.includes("valuation_missing")) reasons.push("估值缺失；确认后价格情景将为 unknown");
      if (gaps.includes("account_constraints_not_configured")) reasons.push("当前约定尚未绑定此账户；财务限制将按 unknown 记录");
      contextNote.textContent = reasons.length ? `${reasons.join("；")}。` : `已载入 ${templates.length} 个来源模板。`;
      submit.disabled = !context.snapshot || !context.mandate_ref || !templates.length || context.enabled?.candidate_ranking === false || context.enabled?.portfolio_ledger === false;
      const pending = readSaved(pendingKey(account.value));
      if (pending?.phase === "accepted" && pending.accepted?.run_ref?.id) {
        activeOperation = pending;
        results.replaceChildren(el("p", { class: "notice notice-loading" }, "已接受任务，恢复跟踪同一 run；不会重新发送请求。"));
        poll(pending.accepted.run_ref.id, accounts.find((row) => accountId(row) === account.value), pending, token, results);
      } else if (pending?.phase === "terminal" && pending.ranking_ref?.id) {
        activeOperation = pending;
        await recoverTerminal(pending, account.value);
      } else if (pending?.phase === "prepared") {
        showPrepared(pending, account.value);
      } else if (pending?.phase === "sending" || pending?.phase === "uncertain") {
        activeOperation = pending;
        submit.disabled = true;
        showUncertain(pending, account.value);
      }
      const saved = readSaved(savedKey(account.value));
      if (saved?.ranking_ref) {
        const detail = el("details", { class: "ranking-saved" });
        detail.appendChild(el("summary", {}, "查看最近一次已完成排序（默认关闭）"));
        const oldBasis = el("p", { class: "faint" }, "生成时使用当时冻结的账户快照、约束和估值版本。");
        if (saved.basis !== basisOf(context) || saved.stale_scenario) oldBasis.textContent += "；当前输入已变化，此记录仍基于旧输入。";
        if (saved.stale_scenario) detail.appendChild(el("p", { class: "notice notice-warn ranking-stale-scenario" }, "以下排序来自上次已提交情景；当前表单已编辑。"));
        detail.appendChild(oldBasis);
        const basisDetails = el("details");
        basisDetails.append(el("summary", {}, "查看冻结版本标识"), el("p", { class: "faint" }, saved.basis));
        detail.appendChild(basisDetails);
        results.appendChild(detail);
        const panel = el("div"); detail.appendChild(panel);
        const savedAccountId = account.value;
        const loadOld = async () => { if (detail.dataset.loaded) return; detail.dataset.loaded = "true"; await loadSnapshot(panel, saved, () => account.value === savedAccountId); };
        detail.addEventListener("toggle", () => { if (detail.open) loadOld(); });
      }
    } catch (error) { if (requestId === contextLoadId && account.value === accountIdAtRequest && root.isConnected) contextNote.textContent = `账户上下文读取失败：${error.message}`; }
  };
  const showUncertain = (record, id) => {
    const retry = el("button", { type: "button", class: "btn btn-sm" }, "用相同操作 ID 重试确认");
    retry.addEventListener("click", () => sendRecord(record, id, true));
    results.replaceChildren(el("p", { class: "notice notice-warn" }, "上次提交未收到确认。可用相同操作 ID 和完整输入重新提交以查询原收据。"), retry);
  };
  const showPrepared = (record, id) => {
    submit.disabled = true;
    const message = el("p", { class: "notice notice-warn" }, "完整请求已保存，但发送状态未能保存；此操作尚未发送。可继续发送原请求，或安全解除待发送状态。");
    const retry = el("button", { type: "button", class: "btn btn-sm" }, "继续发送已准备操作");
    const release = el("button", { type: "button", class: "btn btn-sm" }, "解除未发送操作");
    retry.addEventListener("click", () => sendRecord(record, id, true));
    release.addEventListener("click", () => {
      sessionStorage.removeItem(pendingKey(id));
      if (account.value === id) { submit.disabled = !context?.snapshot || !context?.mandate_ref || !context?.source_templates?.length; results.replaceChildren(); }
    });
    results.replaceChildren(message, retry, release);
  };
  const recoverTerminal = async (record, id) => {
    const saved = { ranking_ref: record.ranking_ref, basis: record.context.basis, account_id: id, operation_id: record.operation_id, stale_scenario: Boolean(record.context.form_dirty) };
    try {
      writeSaved(savedKey(id), saved);
      sessionStorage.removeItem(pendingKey(id));
      if (account.value === id) await renderSaved(results, saved, () => !disposed && root.isConnected && account.value === id && readSaved(savedKey(id))?.operation_id === terminal.operation_id && !readSaved(pendingKey(id)), true);
    } catch (error) { if (account.value === id) say(results, `排序已完成，结果索引保存失败；重新打开页面会继续读取同一任务：${error.message}`, "notice notice-error"); }
  };
  const sendRecord = async (record, id, retry = false) => {
    const requestEpoch = currentEpoch();
    submit.disabled = true;
    results.replaceChildren(el("p", { class: "notice notice-loading" }, retry ? "正在用原操作 ID 查询提交结果…" : "已保存操作 ID，正在提交…"));
    record.phase = "sending";
    try {
      writeSaved(pendingKey(id), record);
    } catch {
      record.phase = "prepared";
      if (requestEpoch === currentEpoch()) showPrepared(record, id);
      return;
    }
    try {
      const receipt = await post("/candidates/rankings", record.body, { idempotency: false });
      const durable = readSaved(pendingKey(id));
      if (durable?.operation_id === record.operation_id) {
        const accepted = { ...durable, phase: "accepted", accepted: receipt };
        writeSaved(pendingKey(id), accepted);
        activeOperation = accepted;
        poll(receipt.run_ref.id, accounts.find((row) => accountId(row) === id), accepted, requestEpoch, results);
      } else if (account.value === id) {
        say(results, "旧操作已收到回执，但当前账户已有其他操作；不会覆盖当前操作。", "notice notice-warn");
      }
    } catch (error) {
      const durable = readSaved(pendingKey(id));
      if (durable?.operation_id !== record.operation_id) return;
      if ([409, 422].includes(error.status)) {
        record.phase = "rejected";
        if (activeOperation?.operation_id === record.operation_id) activeOperation = null;
        sessionStorage.removeItem(pendingKey(id));
        if (requestEpoch === currentEpoch()) {
          submit.disabled = !context?.snapshot || !context?.mandate_ref || !context?.source_templates?.length;
          say(results, `服务端拒绝了原输入（${error.status}）：${error.message}。待确认锁已解除，请检查表单。`, "notice notice-error");
        }
      } else {
        record.phase = "uncertain";
        const uncertain = { ...durable, phase: "uncertain" };
        try { writeSaved(pendingKey(id), uncertain); activeOperation = uncertain; } catch { /* original durable record remains */ }
        if (requestEpoch === currentEpoch()) showUncertain(uncertain, id);
      }
    }
  };
  account.addEventListener("change", () => { localEpoch = bumpEpoch(); context = null; submit.disabled = true; contextNote.textContent = "正在切换账户并读取精确版本…"; results.replaceChildren(); refresh(); });
  form.addEventListener("input", () => {
    localEpoch = bumpEpoch();
    const operationAccountId = activeOperation?.body?.account_id || activeOperation?.context?.account_id;
    if (activeOperation && operationAccountId === account.value && ["prepared", "sending", "uncertain", "accepted", "terminal"].includes(activeOperation.phase)) {
      const durable = readSaved(pendingKey(operationAccountId));
      if (durable?.operation_id === activeOperation.operation_id && ["prepared", "sending", "uncertain", "accepted", "terminal"].includes(durable.phase)) {
        durable.context = { ...durable.context, form_dirty: true };
        try { writeSaved(pendingKey(operationAccountId), durable); activeOperation = durable; } catch { /* accepted body remains recoverable */ }
      }
    }
    if (readSaved(savedKey(account.value))?.ranking_ref) {
      contextNote.textContent = "当前排序表单已变化；最近一次已完成结果仍基于生成时的输入。";
      const savedDetail = results.querySelector(".ranking-saved");
      if (savedDetail && !savedDetail.querySelector(".ranking-stale-scenario")) savedDetail.prepend(el("p", { class: "notice notice-warn ranking-stale-scenario" }, "以下排序来自上次已提交情景；当前表单已编辑。"));
    }
    const pending = readSaved(pendingKey(account.value));
    if (pending?.phase === "accepted" && pending.accepted?.run_ref?.id) {
      activeOperation = pending;
      poll(pending.accepted.run_ref.id, accounts.find((row) => accountId(row) === account.value), pending, localEpoch, results);
    }
  });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!context?.snapshot || !context?.mandate_ref || !context.source_templates?.length || submit.disabled) return;
    if (!priceConfirmation.checked) { contextNote.textContent = "请先确认是否将账户估值价格用作情景价格。"; return; }
    const id = account.value;
    if (readSaved(pendingKey(id))) { contextNote.textContent = "存在尚未确认的操作；页面不会自动创建新操作。"; return; }
    const values = { fixed_fee: amount(fixedFee.value), fee_rate: amount(feeRate.value) };
    const body = {
      operation_id: randomUUID(), account_id: id,
      snapshot_ref: context.snapshot.reference, mandate_ref: context.mandate_ref,
      valuation_binding: context.valuation_binding,
      purpose_binding: buildPurposeBinding(context, purposeState.value, purposeSelect.value, purposeReason.value.trim()),
      view: "account", action_policy: "consider_buy",
      source_policy: { template_ids: context.source_templates, include_manual_open: true, include_watchlist_exclusions: true, as_of_policy: "server_latest_success_per_template" },
      trade_assumptions: { kind: "batch", ...values, assumption_source: "user_scenario", unknown_fields: unknowns(values), price_policy: "valuation_price_as_scenario", confirmed: true },
    };
    const basis = basisOf(context);
    const record = operationRecord(body.operation_id, body, { basis, account_id: id }, "prepared");
    activeOperation = record;
    try { writeSaved(pendingKey(id), record); }
    catch { activeOperation = null; contextNote.textContent = "浏览器无法保存操作 ID 和完整请求，本次未发送。"; return; }
    await sendRecord(record, id);
  });
  refresh();
}

function basisOf(context) {
  const exact = (value) => value?.id ? `${value.id}@${value.revision || 1}` : "missing";
  return `${exact(context.snapshot?.reference)} / ${exact(context.mandate_ref)} / ${context.valuation_binding?.state || "unknown"}:${exact(context.valuation_binding?.valuation_ref)} / ${context.purpose_binding?.state || "unknown"}`;
}
