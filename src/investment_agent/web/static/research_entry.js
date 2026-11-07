import { get, post } from "./api.js";
import { el } from "./dom.js";
import { randomUUID } from "./uuid.js";

const STORAGE = "research-entry-v1";
export const RESEARCH_TEMPLATES = Object.freeze({
  company_research: "这家公司的主要商业驱动是什么？请列明可核对证据、反证与缺口。",
  cash_flow: "这家公司的利润与经营现金流为何背离？请核对报告期、口径、反证与缺口。",
  counter_evidence: "哪些已核验事实可能推翻这家公司的主要投资假设？请区分反证、风险与证据缺失。",
});
const terminal = new Set(["succeeded", "partial", "failed", "cancelled"]);
const stopLabels = {answered:"已回答", evidence_blocked:"证据不足", no_progress:"没有取得新的有效进展", budget_exhausted:"原任务额度已用尽", cancelled:"已取消", failed:"执行失败"};

export function renderResearchEntryResult(root, task, artifacts = []) {
  root.hidden = false;
  root.replaceChildren();
  const published = task.artifact_refs?.some(ref => ref.kind === "recommendation");
  const heading = published ? "有正式观点" : task.stop_reason === "evidence_blocked" ? "证据不足" : terminal.has(task.status) ? "未形成正式观点" : "研究尚未完成";
  root.append(el("h3", {}, heading), el("p", {}, `原任务状态：${task.status}；停止原因：${stopLabels[task.stop_reason] || task.stop_reason || "尚未停止"}`));
  if (task.waiting?.reason) root.append(el("p", {}, task.waiting.reason));
  const progress = artifacts.at(-1)?.progress;
  if (progress) {
    for (const [title, values] of [["理由", progress.inferences], ["缺口与下一步", progress.gaps]]) {
      if (values?.length) {
        root.append(el("h4", {}, title));
        const list = el("ul"); values.forEach(text => list.append(el("li", {}, text))); root.append(list);
      }
    }
    root.append(el("p", {}, `已记录反证引用：${progress.counter_refs?.length || 0}；无引用不表示不存在反证。`));
    if (progress.stopped_because) root.append(el("p", {}, progress.stopped_because));
  }
  root.append(el("p", {}, "任务完成、材料充足与正式观点发布分别由原研究流程判断；请在原会话查看完整理由、来源及后续步骤。"));
}

/** Standalone guide; optional transports keep component tests entirely offline. */
export function bootResearchEntry(root = document.querySelector("#research-entry"), options = {}) {
  if (!root || root.dataset.booted) return;
  root.dataset.booted = "true";
  const read = options.get || get, write = options.post || post;
  const storage = options.storage || sessionStorage;
  const $ = name => root.querySelector(`[data-entry-${name}]`);
  let selected = null, checked = null, generation = 0, searchGeneration = 0, busy = false, pending = null, accepted = null;
  const notice = text => { $("notice").textContent = text; };
  const payload = () => ({listing_ref:selected?.ref, template_key:$("template").value, question:$("question").value.trim()});
  const signature = () => JSON.stringify(payload());
  function persist() {
    try { storage.setItem(STORAGE, JSON.stringify({selected, pending, accepted, template:$("template").value, question:$("question").value})); return true; }
    catch { notice("浏览器无法保存原消息标识；请恢复会话存储后再发送。"); return false; }
  }
  function sync() {
    $("submit").disabled = busy || !!pending || !$("consent").checked || !checked || checked.signature !== signature() || checked.response.model.availability !== "available";
    $("retry").hidden = !pending; $("retry").disabled = busy;
    $("preflight").disabled = busy || !!pending;
    root.querySelectorAll("input,textarea,select,[data-entry-search-form] button,[data-entry-matches] button").forEach(node => { node.disabled = busy || !!pending; });
  }
  function invalidate() {
    generation++; checked = null; $("consent").checked = false; $("coverage").hidden = true; persist(); sync();
  }
  function showError(error) { notice(error.detail?.message || error.message || "请求失败，请保留输入后重试。"); }
  function choose(hit) {
    selected = hit;
    $("identity").textContent = `${hit.name} · ${hit.ticker}`;
    $("matches").replaceChildren(); invalidate();
  }
  async function search(event) {
    event.preventDefault(); if (busy || pending) return;
    const q = $("search").value.trim(), version = ++searchGeneration;
    if (!q) return;
    try {
      const hits = await read(`/agent/listings?q=${encodeURIComponent(q)}`);
      if (version !== searchGeneration) return;
      $("matches").replaceChildren();
      for (const hit of hits) {
        const button = el("button", {type:"button"}, `${hit.name} · ${hit.ticker}`);
        button.addEventListener("click", () => { if (!busy && !pending) choose(hit); }); $("matches").append(button);
      }
      notice(hits.length ? "请选择一个已保存证券。" : "未找到已保存证券；请先在原观察池中准备证券身份。");
      sync();
    } catch (error) { if (version === searchGeneration) showError(error); }
  }
  function showCoverage(response) {
    const box = $("coverage"); box.hidden = false; box.replaceChildren();
    box.append(el("h3", {}, response.coverage === "missing" ? "暂无可读材料" : "已有部分材料"), el("p", {}, response.notice || "即时预检不保证覆盖完整或后续研究成功。"),
      el("p", {}, `预检时间：${response.preflight_at}；可计算性：${response.calculability === "inputs_visible" ? "模板输入可读，仍需核对口径" : "缺少模板计算输入"}`));
    const materials = el("ul");
    for (const material of response.materials || []) {
      const value = material.value;
      materials.append(el("li", {}, `${material.field} · ${material.report_period || "报告期待核对"} · ${material.source} · ${material.source_ref.kind} #${material.source_ref.id} r${material.source_ref.revision} · ${material.locator} · ${material.verification} · ${material.status}${value ? ` · ${value.value ?? "未知"} ${value.unit}` : ""}`));
    }
    box.append(materials, el("p", {}, "candidate 是候选材料，不能当作已核验事实；此预检不会采集或补充材料。"));
    const gaps = el("ul"); (response.gaps || []).forEach(gap => gaps.append(el("li", {}, gap.message || `${gap.field}：${gap.code}`))); box.append(gaps);
    box.append(el("p", {}, `当前方法：${response.method.method_ref.id} · 绑定版本 ${response.method.binding_schema_version}；模型：${response.model.model || response.model.reason || response.model.availability}`));
    if (response.budget) box.append(el("p", {}, `原任务上限：${response.budget.requests} 次请求、${response.budget.tokens} tokens、${response.budget.seconds} 秒；已有日额度等准入限制仍生效。`));
  }
  async function preflight() {
    if (!selected || !$("question").value.trim()) { notice("请先选择证券并填写问题。"); return; }
    busy = true; checked = null; $("consent").checked = false; const version = ++generation, input = payload(), key = signature(); sync();
    try {
      // Resolve identity through the existing listing API; never accept a typed ID.
      await read(`/agent/listings/${selected.ref.id}`);
      const response = await write("/investor/research-entry/preflight", input);
      if (version !== generation || key !== signature()) return;
      checked = {signature:key, response}; showCoverage(response); notice("可以只看缺口并停在这里；运行需要下方明确选择并点击开始。");
    } catch (error) { if (version === generation) showError(error); }
    finally { busy = false; sync(); }
  }
  async function refreshResult() {
    if (!accepted) return;
    try {
      const [task, artifacts] = await Promise.all([read(`/agent/tasks/${accepted.task_id}`), read(`/agent/tasks/${accepted.task_id}/research`)]);
      renderResearchEntryResult($("result"), task, artifacts); resultActions();
    } catch (error) { showError(error); }
  }
  function resultActions() {
    $("result").append(el("a", {href:`/app/agent?conversation=${encodeURIComponent(accepted.conversation_id)}`}, "在原会话查看完整研究"));
    const button = el("button", {type:"button"}, "刷新原任务结果"); button.addEventListener("click", refreshResult); $("result").append(button);
  }
  async function send(retry = false) {
    if (busy || (retry ? !pending : $("submit").disabled)) return;
    busy = true; sync();
    try {
      if (!pending) {
        pending = {conversation_id:null, body:{client_message_id:randomUUID(), text:$("question").value.trim(), context_refs:[selected.ref], research_kind:"company_research"}};
        if (!persist()) { pending = null; return; }
      }
      if (!pending.conversation_id) {
        // No research message has been sent yet. Retrying an unacknowledged
        // conversation creation can leave an empty conversation, never a paid task.
        const conversation = await write("/agent/conversations", {title:`${selected.name} · ${pending.body.text}`.slice(0,120), context_refs:pending.body.context_refs});
        pending.conversation_id = conversation.id;
        if (!persist()) return;
      }
      const receipt = await write(`/agent/conversations/${pending.conversation_id}/messages`, structuredClone(pending.body));
      accepted = {conversation_id:pending.conversation_id, task_id:receipt.task.id}; pending = null; checked = null; $("consent").checked = false; persist();
      renderResearchEntryResult($("result"), receipt.task); resultActions(); notice("原消息已受理，请在原会话继续查看研究过程。"); options.onAccepted?.(accepted, receipt);
    } catch (error) {
      // A retry cannot prove whether an earlier unacknowledged message committed.
      // In particular, 404 is not evidence of non-receipt. Keep its original key.
      if (!retry && pending?.conversation_id && ([401,403,422].includes(error.status) || error.detail?.code === "execution_denied")) { pending = null; persist(); }
      if (pending) notice("发送结果尚未确认，已保留原会话和消息标识。请确认原消息，勿创建另一条研究。"); else showError(error);
    } finally { busy = false; sync(); }
  }
  $("search-form").addEventListener("submit", search);
  $("template").addEventListener("change", () => { $("question").value = RESEARCH_TEMPLATES[$("template").value]; invalidate(); });
  $("question").addEventListener("input", invalidate); $("consent").addEventListener("change", sync);
  $("preflight").addEventListener("click", preflight); $("submit").addEventListener("click", () => send()); $("retry").addEventListener("click", () => send(true));
  try {
    const saved = JSON.parse(storage.getItem(STORAGE) || "{}");
    if (saved.template in RESEARCH_TEMPLATES) $("template").value = saved.template;
    if (typeof saved.question === "string") $("question").value = saved.question;
    if (saved.selected?.ref?.kind === "listing") { selected = saved.selected; $("identity").textContent = `${selected.name} · ${selected.ticker}`; }
    pending = saved.pending || null; accepted = saved.accepted || null;
    if (pending) notice("上次发送结果尚待确认，请确认原消息。");
    if (accepted) { resultActions(); $("result").hidden = false; }
  } catch { notice("无法恢复研究入口，请重新准备。"); }
  sync();
  return {refreshResult};
}
