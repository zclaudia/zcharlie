// 行业与同行参照（WP10）：个股页独立只读区域。
// 只消费 WP9 的两个只读端点（GET /stocks/{ticker}/industry-peer-context 与
// .../companies）和 run detail 里保存的研究绑定；同源 cookie 认证与 api.js
// 的错误分类沿用现有模式。展示纪律（设计 §页面与接口接入）：
// * 独立自加载——不依赖 loadStockDetail 已取得评分；本区域失败也不影响评分区。
// * 一切统计数值来自服务端，浏览器不重算中位数（这里的换算只是十进制字符串
//   的展示格式化，用 BigInt 精确缩放，不经 float）。
// * 面向用户表达：状态有中文标签映射；不展示来源字段名、内部 hash、内部枚举
//   原文；网络错误与"字段缺失"分开显示。
// * 两个区域明确区分：当前参照（最新已提交批次）与"该次研究的参照"（快照绑定，
//   null 显示未保存，绝不用最新结果回填）。

import { ApiError, get } from "./api.js";
import { el } from "./dom.js";
import { formatDateTime } from "./format.js";

// ---- 面向用户的标签（内部枚举一律不原样上屏） -----------------------------

const CONTEXT_STATUS_LABELS = Object.freeze({
  ready: "参照可用",
  partial: "部分可用",
  pending: "参照数据准备中",
  not_supported: "尚未覆盖",
  missing: "暂无可用参照",
  conflict: "存在未核实冲突",
  stale: "结果待复核",
  failed: "采集失败",
});

const METRIC_STATUS_LABELS = Object.freeze({
  comparable: "可比",
  target_not_applicable: "目标不适用",
  insufficient_peers: "样本不足",
  missing: "缺失",
  conflict: "冲突",
});

const VERIFICATION_LABELS = Object.freeze({
  verified: "已核对",
  candidate: "候选待核",
});

const EXCLUSION_LABELS = Object.freeze({
  not_in_cohort: "不在名单内",
  duplicate_company: "公司重复",
  period_mismatch: "报告期不一致",
  missing_field: "输入字段缺失",
  field_conflict: "输入存在未核实冲突",
  source_field_mismatch: "来源口径不一致",
  non_positive_denominator: "分母非正（该指标不适用）",
  non_positive_equity: "归母净资产非正（ROE 不适用）",
  applicability_input_missing: "适用性判断字段缺失",
});

const METRIC_LABELS = Object.freeze({
  roe: "净资产收益率（ROE）",
  contract_revenue: "合同负债 / 营业收入",
  cfo_parent_profit: "经营现金流 / 归母净利润",
});

// 年度快照的规范字段名 → 中文名（不是来源的原始字段名；来源字段名永不上屏）。
const FIELD_LABELS = Object.freeze({
  roe_weighted_annual: "年度加权 ROE",
  contract_liabilities: "合同负债",
  operating_revenue: "营业收入",
  net_profit_parent: "归母净利润",
  operating_cash_flow: "经营现金流",
  equity_parent: "归母净资产",
  revenue_total_check: "营业总收入（仅核对项）",
});

// ---- 十进制字符串的展示格式化（不经 float，不重算统计） -------------------
// 导出仅供 node 驱动的契约测试使用；页面自身只用 renderMetrics 里的路径。

// "0.3253" 按 scale=2（×100）、保留 2 位 -> "32.53"；四舍五入用 BigInt 精确完成。
export function formatScaledDecimal(text, scale, digits) {
  const match = /^([+-]?)(\d+)(?:\.(\d*))?$/.exec(String(text ?? "").trim());
  if (!match) return null;
  const sign = match[1] === "-" ? "-" : "";
  const intPart = match[2];
  const fracPart = match[3] ?? "";
  // value = (intPart+fracPart) * 10^(scale - len(fracPart))
  const absExp = fracPart.length - scale;
  const digitsAll = (intPart + fracPart).replace(/^0+(?=\d)/, "");
  let scaled = BigInt(digitsAll || "0");
  let intOut;
  let fracOut;
  if (absExp <= 0) {
    scaled *= 10n ** BigInt(-absExp);
    intOut = scaled.toString();
    fracOut = "";
  } else {
    const text2 = scaled.toString().padStart(absExp + 1, "0");
    intOut = text2.slice(0, text2.length - absExp);
    fracOut = text2.slice(text2.length - absExp);
  }
  if (fracOut.length > digits) {
    const roundDigit = fracOut[digits];
    const keep = fracOut.slice(0, digits);
    if (roundDigit >= "5") {
      const merged = (BigInt(intOut + keep) + 1n).toString().padStart(intOut.length + digits, "0");
      intOut = merged.slice(0, merged.length - digits);
      fracOut = merged.slice(merged.length - digits);
    } else {
      fracOut = keep;
    }
  } else {
    fracOut = fracOut.padEnd(digits, "0");
  }
  intOut = intOut.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  return sign + intOut + (digits > 0 ? `.${fracOut}` : "");
}

// 指标数值展示：ROE 与合同负债收入比按 percent（fraction×100，2 位）、
// 现金流比 4 位小数、位置按 percent（已是百分数值，2 位）。
export function formatMetricNumber(text, metricKey, role) {
  if (role === "position") {
    const value = formatScaledDecimal(text, 0, 2);
    return value === null ? null : `${value}%`;
  }
  if (metricKey === "cfo_parent_profit") {
    return formatScaledDecimal(text, 0, 4);
  }
  const value = formatScaledDecimal(text, 2, 2);
  return value === null ? null : `${value}%`;
}

// 金额：千分位 + 币种（数量结构里的 currency；展示不换算单位）。
function formatAmountQuantity(quantity) {
  const match = /^(\d+)(?:\.(\d+))?$/.exec(String(quantity.value ?? "").trim());
  if (!match) return String(quantity.value);
  const intOut = match[1].replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  const currency = quantity.currency || quantity.unit || "";
  return `${intOut}${match[2] ? `.${match[2]}` : ""}${currency ? ` ${currency}` : ""}`;
}

// Quantity（{value, unit, currency, unknown_reason}）→ 展示节点。
// 字段缺失（value 为 null）与网络错误是两条不同路径：这里只处理前者。
function quantityText(quantity, metricKey, role) {
  if (!quantity || quantity.value === null || quantity.value === undefined) {
    return quantity && quantity.unknown_reason ? `缺失（${quantity.unknown_reason}）` : "缺失";
  }
  const formatted = formatMetricNumber(quantity.value, metricKey, role);
  return formatted === null ? String(quantity.value) : formatted;
}

function label(map, key) {
  if (key === null || key === undefined || key === "") return "—";
  return Object.prototype.hasOwnProperty.call(map, key) ? map[key] : null;
}

// ---- 通用小件 ---------------------------------------------------------------

function loadingNotice() {
  return el("p", { class: "notice notice-loading", "data-state": "loading" }, "正在加载…");
}

// 网络错误一行提示 + 重试（与业务上的"字段缺失"分开：只有这条走 ApiError 路径）。
// 401 与现有页面一致：回到登录页，不在本区域里造一个死错误状态。
function errorNotice(error, retry) {
  if (error instanceof ApiError && error.status === 401) {
    globalThis.location.href = "/app/login";
    return el("p", { class: "notice" }, "登录已过期，正在跳转登录页…");
  }
  const message =
    error instanceof ApiError && error.message ? error.message : "参照数据加载失败，请重试。";
  const box = el("p", { class: "notice notice-error" }, message);
  if (retry) box.appendChild(el("button", { type: "button" }, "重试")).addEventListener("click", retry);
  return box;
}

function noticeList(title, items, cssClass) {
  const wrap = el("section", { class: `peer-messages ${cssClass || ""}` });
  wrap.appendChild(el("h4", {}, title));
  const list = el("ul");
  for (const item of items) list.appendChild(el("li", {}, item));
  wrap.appendChild(list);
  return wrap;
}

// ---- 区域装配 ----------------------------------------------------------------

class PeerSection {
  constructor(section, ticker) {
    this.section = section;
    this.ticker = ticker;
    this.currentBody = section.querySelector("#peer-context-current");
    this.boundBody = section.querySelector("#peer-context-bound");
    this.evidencePanel = section.querySelector("#peer-context-evidence");
    this.companiesCache = new Map();
  }

  start() {
    this.section.hidden = false;
    this.currentBody.replaceChildren(loadingNotice());
    this.boundBody.replaceChildren(loadingNotice());
    // not_supported 时整体只留一行"尚未覆盖"：并行在途的研究绑定读取不再渲染。
    this.suppressed = false;
    this.loadCurrent();
    this.loadResearchBinding();
  }

  contextUrl(batchRunId) {
    const path = `/stocks/${encodeURIComponent(this.ticker)}/industry-peer-context`;
    return batchRunId ? `${path}?batch_run_id=${encodeURIComponent(batchRunId)}` : path;
  }

  loadCurrent() {
    this.currentBody.replaceChildren(loadingNotice());
    return get(this.contextUrl())
      .then((context) => {
        if (context && context.status === "not_supported") {
          this.suppressed = true;
          this.renderNotSupported(context);
          return context;
        }
        this.currentBody.replaceChildren();
        this.currentBody.appendChild(this.renderContextHead("当前参照（最新已提交批次）"));
        this.currentBody.appendChild(this.renderContext(context, { showDetail: true }));
        return context;
      })
      .catch((error) => {
        this.currentBody.replaceChildren(errorNotice(error, () => this.loadCurrent()));
      });
  }

  // 该次研究的参照：页面展示的最近一次研究快照保存的绑定。
  // 绑定为 null 显示"该次研究未保存同行参照"，绝不回填最新结果。
  loadResearchBinding() {
    this.boundBody.replaceChildren(loadingNotice());
    const historyUrl = `/research/stocks/${encodeURIComponent(this.ticker)}/history?limit=1`;
    return get(historyUrl)
      .then(async (snapshots) => {
        if (this.suppressed) return null;
        if (!Array.isArray(snapshots) || !snapshots.length) {
          this.boundBody.replaceChildren(
            el("p", { class: "muted" }, "尚无研究快照；该区域显示最近一次研究保存的同行参照绑定。")
          );
          return null;
        }
        const runId = snapshots[0].run_id;
        const detail = await get(`/research/runs/${encodeURIComponent(runId)}`);
        if (this.suppressed) return null;
        const binding = detail && detail.industry_peer_context;
        if (!binding || !binding.batch_run_id) {
          this.boundBody.replaceChildren(
            el(
              "p",
              { class: "notice" },
              `该次研究（运行 #${runId}）未保存同行参照；当时的结果保持原样，不用最新结果回填。`
            )
          );
          return null;
        }
        const context = await get(this.contextUrl(binding.batch_run_id));
        if (this.suppressed) return null;
        this.boundBody.replaceChildren();
        this.boundBody.appendChild(
          this.renderContextHead(`该次研究（运行 #${runId}）的参照`)
        );
        this.boundBody.appendChild(this.renderContext(context, { showDetail: true }));
        return context;
      })
      .catch((error) => {
        if (this.suppressed) return null;
        this.boundBody.replaceChildren(errorNotice(error, () => this.loadResearchBinding()));
        return null;
      });
  }

  // 名单外或功能未开启：整个区域只给一行"尚未覆盖"（服务端同状态不区分两者），
  // 不展开任何子区域；差距原文收进弱化文本，如实保留。
  renderNotSupported(context) {
    this.currentBody.replaceChildren();
    this.boundBody.replaceChildren();
    const head = this.renderContextHead("当前参照（最新已提交批次）");
    head.appendChild(el("span", { class: "badge" }, label(CONTEXT_STATUS_LABELS, context.status)));
    const body = el("div");
    body.appendChild(
      el(
        "p",
        { class: "notice" },
        "行业与同行参照尚未覆盖该股票：功能未开启，或该股票不在参照名单内。既有个股研究不受影响。"
      )
    );
    for (const gap of context.gaps || []) body.appendChild(el("p", { class: "faint" }, gap));
    this.currentBody.appendChild(head);
    this.currentBody.appendChild(body);
    this.section.dataset.state = "ready";
  }

  renderContextHead(title) {
    const head = el("div", { class: "peer-area-head" });
    head.appendChild(el("h3", {}, title));
    return head;
  }

  // 一个参照读结果（当前或历史）的完整渲染。
  renderContext(context, { showDetail }) {
    const wrap = el("div", { class: "peer-context" });
    const status = label(CONTEXT_STATUS_LABELS, context.status) || context.status;
    const badge = el("span", { class: `badge peer-status peer-status-${context.status}`, title: status }, status);
    wrap.appendChild(badge);

    if (context.old_reference) {
      wrap.appendChild(
        el(
          "p",
          { class: "notice notice-warn" },
          `当前报告期（${context.current_report_period || "未知"}）尚无该批次的参照数据；以下为上一报告期 ${context.report_period || ""} 的旧参照，不冒充当前年度。`
        )
      );
    }

    const facts = el("dl", { class: "kv-list peer-facts" });
    const appendFact = (term, value) => {
      if (value === null || value === undefined) return;
      facts.appendChild(el("dt", {}, term));
      facts.appendChild(el("dd", {}, value));
    };
    appendFact("目标公司", context.target ? `${context.target.name || ""}（${context.target.ticker}）` : null);
    appendFact("报告期", context.report_period);
    appendFact("数据时点", formatDateTime(context.data_as_of));
    if (context.inputs_acquired_earliest && context.inputs_acquired_latest) {
      const earliest = formatDateTime(context.inputs_acquired_earliest);
      const latest = formatDateTime(context.inputs_acquired_latest);
      appendFact(
        "输入取得时间",
        earliest === latest ? earliest : `最早 ${earliest} · 最晚 ${latest}`
      );
    }
    appendFact("最后核查", formatDateTime(context.last_checked_at));
    appendFact("批次提交", formatDateTime(context.submitted_at));
    if (facts.children.length) wrap.appendChild(facts);

    if (context.status === "pending") wrap.appendChild(this.renderPending(context));
    wrap.appendChild(this.renderMetrics(context, showDetail));
    for (const [title, items, css] of [
      ["待复核原因", context.stale_reasons, "peer-stale"],
      ["提示", context.warnings, "peer-warn"],
      ["缺口", context.gaps, "peer-gap"],
    ]) {
      if (Array.isArray(items) && items.length) wrap.appendChild(noticeList(title, items, css));
    }
    this.section.dataset.state = "ready";
    return wrap;
  }

  renderPending(context) {
    const box = el("p", { class: "notice" }, "参照数据正在准备中：名单采集任务排队或运行中，完成后本页才会出现结果。");
    const hint = context.job_hint;
    if (hint && hint.run_reference) {
      box.appendChild(el("span", { class: "faint" }, ` 运行线索：#${hint.run_reference}`));
    } else if (hint && hint.has_pending_job === false) {
      box.appendChild(el("span", { class: "faint" }, "（当前没有排队中的采集任务）"));
    }
    return box;
  }

  renderMetrics(context, showDetail) {
    const metrics = Array.isArray(context.metrics) ? context.metrics : [];
    const wrap = el("div", { class: "peer-metrics" });
    if (!metrics.length) {
      wrap.appendChild(
        el(
          "p",
          { class: "muted" },
          "本批次没有为该公司生成指标结果；缺口与原因见下方说明。"
        )
      );
      return wrap;
    }
    const list = el("ul", { class: "peer-metric-list" });
    for (const metric of metrics) list.appendChild(this.renderMetric(metric));
    wrap.appendChild(list);
    if (showDetail && context.batch_run_id) {
      wrap.appendChild(this.renderCompanies(context.batch_run_id, metrics.map((m) => m.metric_key)));
    }
    return wrap;
  }

  renderMetric(metric) {
    const item = el("li", { class: "peer-metric", "data-metric-key": metric.metric_key });
    const head = el("div", { class: "peer-metric-head" });
    head.appendChild(el("span", { class: "peer-metric-name" }, label(METRIC_LABELS, metric.metric_key) || metric.metric_key));
    const statusText = label(METRIC_STATUS_LABELS, metric.status) || metric.status;
    head.appendChild(el("span", { class: `badge peer-metric-status-${metric.status}`, title: statusText }, statusText));
    item.appendChild(head);

    // 不足 5 家有效同行（或整体缺失/冲突）不展示统计：只显示覆盖数与原因。
    const showStatistics = metric.status === "comparable" || metric.status === "target_not_applicable";
    const facts = el("dl", { class: "kv-list peer-metric-facts" });
    const appendFact = (term, value) => {
      facts.appendChild(el("dt", {}, term));
      const dd = el("dd");
      if (value instanceof Node) dd.appendChild(value);
      else dd.textContent = value;
      facts.appendChild(dd);
    };
    if (showStatistics) {
      appendFact("本公司", quantityText(metric.target_value, metric.metric_key, "value"));
      appendFact("同行中位数", quantityText(metric.peer_median, metric.metric_key, "value"));
      if (metric.status === "comparable") {
        appendFact("位置（升序）", quantityText(metric.ascending_position, metric.metric_key, "position"));
      } else {
        appendFact("位置（升序）", "不适用于本公司（同行中位数仍有效）");
      }
    }
    appendFact(
      "有效 / 候选同行",
      `${metric.valid_peer_count ?? 0} / ${metric.candidate_peer_count ?? 0}`
    );
    appendFact("核对覆盖", this.verificationText(metric));
    item.appendChild(facts);

    if (metric.reason) item.appendChild(el("p", { class: "faint peer-metric-reason" }, metric.reason));
    for (const note of metric.quality_notes || []) {
      item.appendChild(el("p", { class: "faint peer-metric-reason" }, note));
    }
    const excluded = Array.isArray(metric.excluded_companies) ? metric.excluded_companies : [];
    if (excluded.length) item.appendChild(this.renderExcluded(excluded));
    if (metric.calculation_ref) item.appendChild(this.renderRecordRef(metric.calculation_ref));
    return item;
  }

  verificationText(metric) {
    const counts = metric.verification_counts && typeof metric.verification_counts === "object" ? metric.verification_counts : {};
    const parts = [];
    for (const [key, value] of Object.entries(counts)) {
      const name = label(VERIFICATION_LABELS, key) || key;
      parts.push(`${name} ${value}`);
    }
    if (!parts.length) {
      return metric.cites_no_evidence ? "0 项（本批输入无可引用证据）" : "0 项";
    }
    return parts.join(" · ");
  }

  renderExcluded(excluded) {
    const box = el("details", { class: "peer-excluded" });
    box.appendChild(el("summary", {}, `排除公司（${excluded.length}）`));
    const list = el("ul");
    for (const entry of excluded) {
      const name = entry.name ? `${entry.name}（${entry.ticker}）` : entry.ticker;
      const text = `${name}：${label(EXCLUSION_LABELS, entry.reason) || "不纳入统计"}`;
      const li = el("li", {}, text);
      if (entry.is_target) li.appendChild(el("span", { class: "badge warn" }, "本公司"));
      if (entry.detail) li.appendChild(el("span", { class: "faint" }, ` — ${entry.detail}`));
      list.appendChild(li);
    }
    box.appendChild(list);
    return box;
  }

  // 同行明细：按批次映射返回的固定版本细表（数值全部来自服务端）。
  renderCompanies(batchRunId, metricKeys) {
    const box = el("details", { class: "peer-companies" });
    const body = el("div", { class: "peer-companies-body" });
    body.appendChild(loadingNotice());
    box.addEventListener("toggle", () => {
      if (box.open && !body.dataset.loaded) {
        this.loadCompanies(batchRunId, metricKeys).then((node) => {
          if (!node) return;
          body.dataset.loaded = "1";
          body.replaceChildren(node);
        });
      }
    });
    box.appendChild(el("summary", {}, "同行明细"));
    box.appendChild(body);
    return box;
  }

  async loadCompanies(batchRunId, metricKeys) {
    try {
      const cacheKey = `${this.ticker}:${batchRunId}`;
      let promise = this.companiesCache.get(cacheKey);
      if (!promise) {
        const path = `/stocks/${encodeURIComponent(this.ticker)}/industry-peer-context/companies`;
        promise = get(`${path}?batch_run_id=${encodeURIComponent(batchRunId)}`);
        this.companiesCache.set(cacheKey, promise);
      }
      const detail = await promise;
      return this.renderCompaniesTable(detail, metricKeys);
    } catch (error) {
      return errorNotice(error, null);
    }
  }

  renderCompaniesTable(detail, metricKeys) {
    const wrap = el("div");
    const keys = metricKeys.length ? metricKeys : [...new Set(detail.companies.flatMap((c) => c.metrics.map((m) => m.metric_key)))];
    const node = el("table", { class: "stack-table peer-companies-table" });
    const headRow = el("tr");
    headRow.appendChild(el("th", { scope: "col" }, "公司"));
    for (const key of keys) {
      headRow.appendChild(el("th", { scope: "col" }, label(METRIC_LABELS, key) || key));
    }
    node.appendChild(el("thead")).appendChild(headRow);
    const tbody = el("tbody");
    for (const company of detail.companies || []) {
      const tr = el("tr");
      if (company.metrics.some((m) => m.is_target)) tr.classList.add("peer-target-row");
      const nameCell = el("td", { "data-label": "公司" });
      nameCell.appendChild(el("span", {}, company.name));
      nameCell.appendChild(el("span", { class: "faint" }, ` ${company.ticker}`));
      tr.appendChild(nameCell);
      for (const key of keys) {
        const metric = (company.metrics || []).find((m) => m.metric_key === key);
        const cell = el("td", { "data-label": label(METRIC_LABELS, key) || key });
        if (!metric) {
          cell.appendChild(el("span", { class: "faint" }, "—"));
        } else if (metric.included && metric.value !== null && metric.value !== undefined) {
          cell.appendChild(el("span", {}, formatMetricNumber(metric.value, key, "value") || metric.value));
        } else {
          const text = label(EXCLUSION_LABELS, metric.exclusion_reason) || "未纳入";
          cell.appendChild(el("span", { class: "faint", title: metric.exclusion_detail || text }, text));
        }
        tr.appendChild(cell);
      }
      tbody.appendChild(tr);
    }
    node.appendChild(tbody);
    wrap.appendChild(node);

    const evidence = Array.isArray(detail.field_evidence) ? detail.field_evidence : [];
    if (evidence.length) {
      const names = new Map((detail.companies || []).map((c) => [c.company_id, c.name]));
      const box = el("div", { class: "peer-evidence-links" });
      box.appendChild(el("h4", {}, "字段证据引用"));
      for (const link of evidence) {
        const company = names.get(link.company_id);
        const text = `查看证据 · ${company || "公司"} · ${label(FIELD_LABELS, link.field) || "字段"}`;
        box.appendChild(this.evidenceButton(link.ref, text));
      }
      wrap.appendChild(box);
    }
    return wrap;
  }

  // 计算记录引用按钮：接现有 /investor/calculations/{id} 详情入口。
  renderRecordRef(ref) {
    return this.evidenceButton(ref, "查看计算记录");
  }

  evidenceButton(ref, text) {
    const button = el("button", { type: "button", class: "chip peer-evidence-chip" }, text);
    button.addEventListener("click", () => this.showEvidence(ref));
    return button;
  }

  // 引用面板：同源读取 /investor/evidence|calculations/{id}，展示允许的
  // 元数据（值、期间、核对状态、定位、限制）；不展示 hash，不嵌 PDF。
  async showEvidence(ref) {
    if (!this.evidencePanel) return;
    this.evidencePanel.hidden = false;
    this.evidencePanel.replaceChildren(loadingNotice());
    this.evidencePanel.scrollIntoView({ behavior: "smooth", block: "nearest" });
    const isEvidence = ref.kind === "evidence";
    const base = isEvidence ? "/investor/evidence" : "/investor/calculations";
    try {
      const result = await get(`${base}/${encodeURIComponent(ref.id)}?revision=${encodeURIComponent(ref.revision ?? 1)}`);
      const record = result && result.data ? result.data : result;
      this.evidencePanel.replaceChildren(this.renderEvidencePanel(ref, result, record, isEvidence));
    } catch (error) {
      this.evidencePanel.replaceChildren(
        el("h4", {}, "引用详情"),
        errorNotice(error, () => this.showEvidence(ref))
      );
    }
  }

  renderEvidencePanel(ref, result, record, isEvidence) {
    const wrap = el("div", { class: "peer-evidence-panel" });
    const head = el("div", { class: "card-head" });
    head.appendChild(el("h4", {}, isEvidence ? "证据引用详情" : "计算记录详情"));
    head.appendChild(el("button", { type: "button", class: "link-button" }, "关闭")).addEventListener("click", () => {
      this.evidencePanel.hidden = true;
      this.evidencePanel.replaceChildren();
    });
    wrap.appendChild(head);
    if (result && typeof result === "object" && "status" in result && result.status && result.status !== "ok") {
      wrap.appendChild(el("p", { class: "notice notice-warn" }, `当前读取状态：${result.status === "conflict" ? "存在冲突或更正" : result.status === "stale" ? "待复核" : "受限"}`));
    }
    const facts = el("dl", { class: "kv-list" });
    const appendFact = (term, value) => {
      if (value === null || value === undefined || value === "") return;
      facts.appendChild(el("dt", {}, term));
      facts.appendChild(el("dd", {}, value));
    };
    if (isEvidence) {
      appendFact("核验状态", label(VERIFICATION_LABELS, record.verification) || record.verification);
      appendFact("定位", record.locator);
      const value = record.value;
      if (value && value.value !== null && value.value !== undefined) {
        appendFact(value.unit === "CNY" || value.currency ? "金额" : "保存值", formatAmountQuantity(value));
      }
      if (record.text) appendFact("保存材料", record.text);
      if (record.period) appendFact("期间", `${record.period.start || ""} 至 ${record.period.end || ""}（${record.period.basis || ""}）`);
      appendFact("披露时间", formatDateTime(record.published_at));
      appendFact("取得时间", formatDateTime(record.acquired_at));
      appendFact("可用时间", formatDateTime(record.available_from));
    } else {
      appendFact("计算状态", record.status === "ok" ? "计算完成" : record.status);
      appendFact("方法适用", record.applicable ? "适用" : "不适用");
      if (Array.isArray(record.quality)) for (const note of record.quality) appendFact("质量限制", note);
      if (Array.isArray(record.errors)) for (const note of record.errors) appendFact("错误", note);
    }
    if (result && Array.isArray(result.warnings)) {
      for (const warning of result.warnings) appendFact("当前核查限制", warning.startsWith("verification=") ? label(VERIFICATION_LABELS, warning.slice(13)) || warning : warning);
    }
    wrap.appendChild(facts);
    return wrap;
  }
}

// ---- 挂载 --------------------------------------------------------------------

export function bootIndustryPeers() {
  const section = document.querySelector("#peer-context-section");
  if (!section || section.dataset.peerMounted) return null;
  section.dataset.peerMounted = "1";
  const ticker = (section.getAttribute("data-ticker") || "").trim();
  if (!ticker) return null;
  const mounted = new PeerSection(section, ticker);
  mounted.start();
  return mounted;
}

// document 缺席（node 契约测试的裸 import）时安静退出，不做任何挂载。
if (typeof document !== "undefined") {
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bootIndustryPeers);
  } else {
    bootIndustryPeers();
  }
}
