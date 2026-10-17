/* Read-only per-listing projection for P2 supplemental applications. */

const labels = {
  applicable: "适用", not_applicable: "不适用", unknown: "未知", triggered: "已触发",
  not_triggered: "未触发", verified: "已核对", pending: "待核对", conflicted: "存在冲突",
  unresolved: "未决", inconclusive: "无法判断", clear: "无新问题", supported: "有支持材料",
  mitigated: "有缓解材料", resolved: "已解除", mechanical: "机械评估", reviewed: "语义已评审",
  stale: "需要刷新", valid: "当前有效", available: "可读取", missing: "缺少应用",
  info: "提示", warning: "需要注意", critical: "重大事项",
  business_demand: "业务与需求", competition_durability: "竞争优势",
  financial_cash: "财务与现金流", balance_sheet_allocation: "资产负债与资本配置",
  price_expectations: "价格与预期", downside_counter_case: "下行情景与反证",
  portfolio_alternatives: "组合与替代方案",
};
const label = (value) => labels[value] || value || "未知";
const refText = (ref) => ref ? String(ref.kind) + ":" + String(ref.id) + (ref.revision == null ? "" : "@" + ref.revision) : "未提供";

// Mirror of investor/enums.py REASON_LABELS; codes may carry ":<identity>"
// suffixes, so display matches the head before the first colon.
const reasonLabels = {
  rule_binding_missing: "无研究规则绑定（结论早于规则体系）",
  rule_coverage_unpublished: "规则覆盖尚未随结论发布",
  rule_coverage_binding_missing: "覆盖清单缺少原始规则绑定",
  rule_coverage_pending: "存在新增重要规则未覆盖此结论",
  rule_coverage_scan_incomplete: "规则扫描超出有界上限，覆盖不完整",
  supplemental_rules_disabled_with_rule_binding: "规则链任务在开关关闭期间被拒",
  rule_expired: "规则已过期",
  rule_review_due: "规则复核期限已到",
  research_check_due: "规则研究要求已到检查时点",
  event_publication_boundary: "规则事件在结论之后发布",
  event_effective_started: "规则事件在结论之后生效",
  event_effective_ended: "规则事件有效期已结束",
  unbound_event: "出现结论未覆盖的新事件",
  critical_pending: "存在重要未决规则事项",
  coverage_pending: "规则覆盖待复核",
  review_due: "规则复核到期",
};
const reasonLabel = (code) => {
  if (code == null || code === "") return "";
  const text = String(code);
  return reasonLabels[text.split(":")[0]] || reasonLabels[text] || text;
};
const coverageStageLabels = { prepared: "准备中（未发布）", published: "已发布" };
const coverageEntryLabels = {
  application: "已有逐规则评估", not_applicable: "不适用（有依据）",
  unknown: "未知", unbound_new: "绑定后新增规则",
};
const COVERAGE_ENTRY_PREVIEW = 50;

async function get(path) {
  const response = await fetch(path, {credentials: "same-origin", headers: {"X-Requested-With": "fetch"}});
  let data = null;
  try { data = await response.json(); } catch { /* no response body */ }
  if (!response.ok) throw new Error(typeof data?.detail === "string" ? data.detail : "读取失败（HTTP " + response.status + "）。");
  return data;
}

function node(tag, value, className) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (value != null) element.textContent = String(value);
  return element;
}

function refList(title, refs) {
  const details = document.createElement("details");
  details.append(node("summary", title + "（" + refs.length + "）"));
  if (!refs.length) details.append(node("p", "无精确引用。", "supplemental-muted"));
  else {
    const list = document.createElement("ul");
    for (const ref of refs) list.append(node("li", refText(ref)));
    details.append(list);
  }
  return details;
}

function textList(title, values, format = (value) => String(value)) {
  const details = document.createElement("details");
  details.append(node("summary", title + "（" + values.length + "）"));
  const list = document.createElement("ul");
  for (const value of values) list.append(node("li", format(value)));
  if (!values.length) list.append(node("li", "无"));
  details.append(list);
  return details;
}

function facet(title, value) {
  const details = document.createElement("details");
  details.append(node("summary", title + " · " + label(value?.status)));
  if (value?.summary) details.append(node("p", value.summary));
  for (const unknown of value?.unknowns || []) details.append(node("p", unknown, "unknown-copy"));
  for (const claim of value?.claims || []) {
    details.append(node("p", (claim.kind || "claim") + " · " + (claim.verification || "pending") + " · " + (claim.text || claim.summary || "未提供说明")));
  }
  details.append(refList("精确证据引用", value?.evidence_refs || []));
  return details;
}

function appReference(app) {
  return app ? {kind: "rule_application", id: app.id, revision: app.revision} : null;
}

function evidenceReferences(app) {
  return (app?.frozen_pair?.subject?.evidence || [])
    .filter((item) => item?.id != null && Number.isInteger(item.revision))
    .map((item) => ({kind: "evidence", id: item.id, revision: item.revision}));
}

function renderApplicationCard(app, wrapped, {includeImpact = true} = {}) {
  const rule = app.frozen_pair?.rule || {};
  const card = node("article", null, "supplemental-item");
  card.append(node("h3", (rule.name || "补充规则") + " · " + (rule.revision || app.rule_ref?.revision || "未知") + " 版"));
  const meta = node("div", null, "supplemental-item-meta");
  for (const value of [
    "匹配：" + label(app.applicability), "条件：" + label(app.condition_result),
    "证据：" + label(app.evidence_status), "事项：" + label(app.assessment_status),
    "判断：" + label(app.semantic_status), "时效：" + label(wrapped.current_limits?.current_status),
  ]) meta.append(node("span", value, "badge"));
  card.append(meta);
  card.append(node("p", "评估时点 " + (app.as_of || "未知") + " · 评估时间 " + (app.evaluated_at || "未知") + " · " + (app.application_mode || "未知模式"), "supplemental-muted"));
  if (wrapped.current_limits?.reasons?.length) card.append(node("p", "时效说明：" + wrapped.current_limits.reasons.join("；"), "supplemental-muted"));
  if (wrapped.current_limits?.coverage_pending) card.append(node("p", "来源或扫描覆盖待补充。", "unknown-copy"));
  card.append(facet("事件事实", app.result?.event_fact));
  card.append(facet("公司暴露", app.result?.company_exposure));
  if (includeImpact) card.append(facet("影响推断", app.result?.impact_assessment));
  card.append(refList("规则来源", rule.source_refs || []));
  card.append(refList("冻结时的证据", evidenceReferences(app)));
  card.append(refList("当前承接的未决事项", app.open_issue_refs || []));
  if (app.result?.diagnostics?.length) {
    card.append(refList("评估诊断", app.result.diagnostics.map((value, index) => ({kind: "diagnostic", id: String(index + 1) + ": " + value}))));
  }
  const exact = node("button", "读取精确应用版本", "btn btn-sm");
  exact.type = "button";
  exact.addEventListener("click", async () => {
    exact.disabled = true;
    try {
      const value = await get("/supplemental-rule-applications/" + encodeURIComponent(app.id) + "?revision=" + (app.revision || 1));
      exact.insertAdjacentText("afterend", " · 已读回 " + refText(appReference(app)) + "，输入 " + (value.payload?.input_hash || app.input_hash));
    } catch (error) { exact.insertAdjacentText("afterend", " · " + error.message); }
    finally { exact.disabled = false; }
  });
  card.append(exact);
  return card;
}

function renderIssueGroup(group, appByRef) {
  const card = node("article", null, "supplemental-issue-group");
  const dimension = group.dimension ? " · " + label(group.dimension) : "";
  card.append(node("h3", "归并事项" + dimension + (group.issue_key ? " · " + group.issue_key : "")));
  card.append(node("p", "下列依据分别展示；不把不同规则或事件推断合成单一公司结论。", "supplemental-muted"));
  if (group.review_required) card.append(node("p", "需要人工复核；此汇总不是正式结论。", "unknown-copy"));
  card.append(refList("全部规则应用（完整分组范围）", group.application_refs || []));
  card.append(refList("全部规则版本", group.rule_refs || []));
  card.append(refList("全部事件版本", group.event_refs || []));
  const bases = node("div", null, "supplemental-issue-bases");
  for (const basis of group.bases || []) {
    const basisCard = node("article", null, "supplemental-issue-basis");
    basisCard.append(node("h4", "单条影响依据 · " + refText(basis.application_ref)));
    basisCard.append(node("p", "规则：" + refText(basis.rule_ref) + " · 事件：" + refText(basis.event_ref)));
    basisCard.append(node("p", "严重度：" + label(basis.severity) + " · 重要性：" + label(basis.materiality)));
    basisCard.append(node("p", "条件：" + label(basis.condition_result) + " · 证据：" + label(basis.evidence_status) + " · 事项：" + label(basis.assessment_status)));
    basisCard.append(facet("该依据的影响推断", basis.impact_assessment));
    basisCard.append(textList("有效措施", basis.effects || [], (effect) => effect.effect + " · " + effect.reason));
    basisCard.append(textList("解除要求", basis.release_requirements || []));
    if (basis.diagnostics?.length) basisCard.append(textList("依据诊断", basis.diagnostics));
    bases.append(basisCard);
  }
  card.append(bases);
  const audit = document.createElement("details");
  audit.append(node("summary", "本页逐规则审计详情"));
  for (const ref of group.application_refs || []) {
    const wrapped = appByRef.get(refText(ref));
    if (wrapped?.payload) audit.append(renderApplicationCard(wrapped.payload, wrapped, {includeImpact: false}));
  }
  card.append(audit);
  return card;
}

export function renderCurrentApplications(target, view, page = {}) {
  target.replaceChildren();
  const applications = view.applications || [];
  const pageIndex = page.pageIndex || 0;
  target.append(node(
    "p",
    "已读取范围：" + (view.listing_ref?.kind === "listing" ? "证券 " + view.listing_ref.id : "未声明证券范围")
      + "；评估视角：" + (view.context || "未知") + "；第 " + (pageIndex + 1) + " 页，本页 " + applications.length
      + " 条。仅显示本页，完整集合需逐页读取；本页不代表全部范围。",
    "supplemental-muted",
  ));
  if (page.onPrevious || page.onNext || view.cursor) {
    const nav = node("div", null, "supplemental-page-controls");
    if (page.onPrevious) {
      const previous = node("button", "上一页", "btn btn-sm");
      previous.type = "button"; previous.addEventListener("click", page.onPrevious); nav.append(previous);
    }
    if (view.cursor && page.onNext) {
      const next = node("button", "下一页", "btn btn-sm");
      next.type = "button"; next.addEventListener("click", page.onNext); nav.append(next);
    }
    target.append(nav);
  }
  if (view.mixed) target.append(node("p", "当前集合来自不同评估时点或来源；状态为混合快照，请逐条核对。", "supplemental-unbound"));
  if (!view.coverage_complete) target.append(node("p", "覆盖尚未完整。扫描缺口、未绑定事件或待更新成员仍可能存在；这不代表没有风险。", "supplemental-unbound"));
  for (const event of view.unbound_events || []) {
    target.append(node("p", "有待关联的事件线索：" + refText({kind: "research_event", id: event.id, revision: event.revision}), "supplemental-unbound"));
  }
  for (const diagnostic of view.diagnostics || []) target.append(node("p", diagnostic, "supplemental-muted"));
  const apps = applications;
  const appByRef = new Map();
  for (const wrapped of apps) {
    if (wrapped.payload) appByRef.set(refText(appReference(wrapped.payload)), wrapped);
  }
  const issueGroups = view.issue_groups || [];
  const grouped = new Set();
  for (const group of issueGroups) {
    if (!(group.application_refs || []).some((ref) => appByRef.has(refText(ref)))) continue;
    target.append(renderIssueGroup(group, appByRef));
    for (const ref of group.application_refs || []) grouped.add(refText(ref));
  }
  if (!apps.length && !(view.unbound_events || []).length) {
    target.append(node("p", view.coverage_complete ? "当前范围没有可展示的逐规则应用。空集合不表示无风险。" : "目前没有可展示的应用；覆盖状态仍待核实。", "supplemental-muted"));
  }
  for (const wrapped of apps) {
    const app = wrapped.payload;
    const ref = app ? refText(appReference(app)) : null;
    if (ref && grouped.has(ref)) continue;
    if (!app) {
      const card = node("article", null, "supplemental-item");
      card.append(node("h3", "成员 " + label(wrapped.status)));
      target.append(card); continue;
    }
    target.append(renderApplicationCard(app, wrapped));
  }
  for (const event of view.unbound_events || []) {
    const card = node("article", null, "supplemental-unbound");
    card.append(node("h3", "待关联事件线索：" + (event.title || event.event_key)));
    card.append(node("p", "状态：" + (event.verification === "user_report" ? "用户报告，尚未核验" : label(event.verification)) + " · " + (event.policy_stage || "阶段未知")));
    card.append(node("p", "精确版本 " + refText({kind: "research_event", id: event.id, revision: event.revision})));
    if (event.conflict) card.append(node("p", "来源存在冲突，保留为待核事项。", "unknown-copy"));
    target.append(card);
  }
}

function renderRuleCoverage(target, view) {
  target.replaceChildren();
  target.append(node("h3", "研究规则绑定与覆盖（工程展示）"));
  target.append(node(
    "p",
    "绑定、覆盖与限制只描述本次已检查的规则范围；不构成买卖建议，也不代表生产启用。",
    "supplemental-muted",
  ));
  if (view.enabled === false) {
    target.append(node("p", "补充规则链功能当前关闭；以下为既有只读数据，不代表生产启用。", "unknown-copy"));
  }
  const binding = view.binding;
  if (view.state === "bound" && binding) {
    const meta = node("div", null, "supplemental-item-meta");
    for (const value of [
      "绑定：bound",
      "规则世代 R" + binding.rules_generation + " · 事件世代 E" + binding.events_generation,
      "绑定规则 " + binding.rule_set.length + " 条",
      "受理时点 " + (binding.admission_as_of || "未知"),
    ]) meta.append(node("span", value, "badge"));
    target.append(meta);
    if (!binding.rule_set.length) {
      target.append(node("p", "绑定时无生效规则：这是合法的空规则集绑定，不是遗留未绑定。", "supplemental-muted"));
    }
    const hashes = node("details");
    hashes.append(node("summary", "绑定溯源哈希"));
    hashes.append(node("p", "绑定引用 " + refText({kind: "research_rule_binding", id: binding.id, revision: binding.revision}), "supplemental-muted"));
    hashes.append(node("p", "规则集合哈希 " + binding.rule_set_hash, "supplemental-muted"));
    if (binding.content_hash) hashes.append(node("p", "绑定内容哈希 " + binding.content_hash, "supplemental-muted"));
    target.append(hashes);
  } else {
    for (const code of view.reasons || []) {
      if (code.split(":")[0] === "rule_binding_missing") {
        target.append(node("p", "legacy 标记：未覆盖新增规则。该主体没有研究规则绑定（结论早于规则体系）；不回填、不追认。", "unknown-copy"));
      } else if (code.split(":")[0] === "rule_coverage_unpublished") {
        target.append(node("p", "已有任务级规则绑定，但规则覆盖尚未随结论发布；当前结论未覆盖新增规则。", "unknown-copy"));
      } else {
        target.append(node("p", "绑定状态未知：" + reasonLabel(code), "unknown-copy"));
      }
    }
  }
  const manifest = view.manifest;
  if (manifest) {
    const stage = node("p", null, "supplemental-muted");
    stage.append(node("span", "覆盖清单 · " + (coverageStageLabels[manifest.stage] || manifest.stage) + " · 生效截点 " + manifest.effective_cutoff, "badge"));
    target.append(stage);
    const entries = manifest.rule_entries || [];
    const list = document.createElement("ul");
    for (const entry of entries.slice(0, COVERAGE_ENTRY_PREVIEW)) {
      const line = "逐规则：" + refText(entry.rule_ref) + " · " + (coverageEntryLabels[entry.status] || entry.status);
      const item = node("li", line);
      if (entry.unknown_reason) item.append(node("span", " · 原因：" + reasonLabel(entry.unknown_reason), "unknown-copy"));
      list.append(item);
    }
    target.append(list);
    if (entries.length > COVERAGE_ENTRY_PREVIEW) {
      target.append(node("p", "其余 " + (entries.length - COVERAGE_ENTRY_PREVIEW) + " 条逐规则状态请经精确覆盖清单接口分页读取。", "supplemental-muted"));
    }
    const limits = view.current_limits;
    if (limits) {
      if (limits.reasons?.length) {
        target.append(node("p", "当前限制（REVIEW_REQUIRED 依据）：" + limits.reasons.map(reasonLabel).join("；"), "unknown-copy"));
      } else {
        target.append(node("p", "当前限制检查：未发现新增未覆盖事项；这不构成买卖建议。", "supplemental-muted"));
      }
    }
  } else if (view.state === "bound") {
    target.append(node("p", "已绑定但暂无可展示的已发布覆盖清单。", "supplemental-muted"));
  }
}

async function loadRuleCoverage(target, listingId) {
  try {
    const view = await get("/supplemental-rule-coverages/current?listing=" + encodeURIComponent(listingId));
    renderRuleCoverage(target, view);
  } catch (error) {
    target.replaceChildren(node("p", "研究规则绑定与覆盖读取失败：" + error.message, "unknown-copy"));
  }
}

export async function bootSupplementalItems(root = document.querySelector("#supplemental-items-root")) {
  if (!root) return;
  const target = root.querySelector("#supplemental-items-content");
  const coverageTarget = root.querySelector("#supplemental-coverage-content");
  const listingId = Number(root.dataset.listingId);
  const context = root.dataset.context || "company_research";
  if (!Number.isInteger(listingId) || listingId < 1) {
    if (coverageTarget) coverageTarget.replaceChildren(node("p", "未能读取已登记的证券标识。", "unknown-copy"));
    target.replaceChildren(node("p", "未能读取已登记的证券标识。", "unknown-copy"));
    return;
  }
  if (coverageTarget) loadRuleCoverage(coverageTarget, listingId);
  const cursors = [null];
  let pageIndex = 0;
  const limit = 100;
  async function loadPage(nextIndex = pageIndex) {
    const query = new URLSearchParams({listing_id: String(listingId), context, limit: String(limit)});
    if (cursors[nextIndex]) query.set("cursor", cursors[nextIndex]);
    const view = await get("/supplemental-rule-applications/current?" + query);
    pageIndex = nextIndex;
    if (view.cursor) cursors[nextIndex + 1] = view.cursor;
    renderCurrentApplications(target, view, {
      pageIndex,
      onPrevious: pageIndex > 0 ? () => loadPage(pageIndex - 1).catch(showError) : null,
      onNext: view.cursor ? () => loadPage(pageIndex + 1).catch(showError) : null,
    });
  }
  function showError(error) { target.append(node("p", error.message, "unknown-copy")); }
  try { await loadPage(); }
  catch (error) { target.replaceChildren(node("p", error.message, "unknown-copy")); }
}

export {get as supplementalItemsGet, reasonLabel as supplementalReasonLabel, renderRuleCoverage};
