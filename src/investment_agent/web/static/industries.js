import { ApiError, get } from "./api.js";
import { el } from "./dom.js";
import { formatDateTime } from "./format.js";

const labels = {
  baijiu_annual_v1: "白酒年度指标",
  bank_annual_v1: "银行年度指标",
};
const packMetrics = {
  baijiu_annual_v1: ["roe", "contract_revenue", "cfo_parent_profit"],
  bank_annual_v1: ["roe", "net_interest_margin", "npl_ratio", "provision_coverage"],
};
const metricNames = {
  roe: "年度加权 ROE", contract_revenue: "合同负债 / 营业收入",
  cfo_parent_profit: "经营现金流 / 归母净利润", net_interest_margin: "净息差",
  npl_ratio: "不良贷款率", provision_coverage: "拨备覆盖率",
};
const stateNames = { verified: "已核对原件", candidate: "候选待核", missing: "缺失", not_applicable: "不适用", conflict: "存在冲突" };
const exclusionNames = {
  missing_field: "字段缺失", field_conflict: "来源存在冲突", source_field_mismatch: "来源口径不一致",
  non_positive_denominator: "分母非正，指标不适用", non_positive_equity: "归母净资产非正，ROE 不适用",
  applicability_input_missing: "适用性判断字段缺失", period_mismatch: "报告期不一致",
  unverified_input: "输入尚未核验", not_frozen: "批次未冻结该公司指标",
};
function reasonText(reason, metricKey = "") {
  const raw = String(reason || "");
  if (raw.includes("applicability_input_missing") && metricKey === "roe") {
    return "归母净资产口径待核，暂不生成 ROE 比较";
  }
  if (exclusionNames[raw]) return exclusionNames[raw];
  if (raw.includes("no usable source fact for this metric")) return "本批次没有可用的该项指标数据";
  const insufficient = /^only (\d+) valid peers; at least (\d+) are required/.exec(raw);
  if (insufficient) return `有效同行 ${insufficient[1]} 家，至少 ${insufficient[2]} 家后才显示中位数`;
  if (raw.startsWith("target input unusable")) return "目标公司的指标输入缺失或待核；同行中位数仍按已保存批次展示";
  if (raw === "batch未冻结该公司指标") return "该公司没有已保存的此项指标结果";
  return raw && !/[A-Za-z]{3,}|_[a-z]+/.test(raw) ? raw : "数据缺失或暂不可比";
}
const $ = (id) => document.getElementById(id);
const industrySelect = $("industry-select");
const periodSelect = $("industry-period");
const detail = $("industry-detail");

function metricText(value, unit) {
  if (value === null || value === undefined || value === "") return "缺失";
  const formatted = formatScaledDecimal(value, unit === "fraction" ? 2 : 0, unit === "fraction" ? 2 : 4);
  if (formatted === null) return String(value);
  return unit === "fraction" ? `${formatted}%` : formatted;
}
function formatScaledDecimal(text, scale, digits) {
  const match = /^([+-]?)(\d+)(?:\.(\d*))?$/.exec(String(text ?? "").trim());
  if (!match) return null;
  const sign = match[1] === "-" ? "-" : "";
  const frac = match[3] ?? "";
  const shift = frac.length - scale;
  const raw = BigInt((match[2] + frac).replace(/^0+(?=\d)/, "") || "0");
  let whole;
  let tail;
  if (shift <= 0) {
    whole = (raw * 10n ** BigInt(-shift)).toString();
    tail = "";
  } else {
    const padded = raw.toString().padStart(shift + 1, "0");
    whole = padded.slice(0, -shift);
    tail = padded.slice(-shift);
  }
  if (tail.length > digits) {
    const keep = tail.slice(0, digits);
    if (tail[digits] >= "5") {
      const rounded = (BigInt(whole + keep) + 1n).toString().padStart(whole.length + digits, "0");
      whole = rounded.slice(0, -digits);
      tail = rounded.slice(-digits);
    } else tail = keep;
  } else tail = tail.padEnd(digits, "0");
  whole = whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  return sign + whole + (digits ? `.${tail}` : "");
}
function sourceLink(ref) {
  if (!ref || ref.kind !== "evidence" || !ref.id) return null;
  const a = el("a", { href: `/investor/evidence/${encodeURIComponent(ref.id)}?revision=${encodeURIComponent(ref.revision || 1)}`, target: "_blank", rel: "noreferrer" }, "查看字段材料");
  return a;
}
function metricCard(metric) {
  const card = el("article", { class: "card industry-metric" });
  card.append(el("h3", {}, metric.label || metricNames[metric.metric_key] || "年度指标"));
  const dl = el("dl");
  const rows = [
    ["候选公司", `${metric.candidate_count} 家`],
    ["有效公司", `${metric.valid_count} 家`],
    ["全名单中位数", metric.median === null ? `有效样本不足 ${metric.median_minimum} 家` : metricText(metric.median, metric.unit)],
    ["个股排除目标后中位数", metric.target_excluded_median === null ? `有效同行不足 ${metric.median_minimum} 家` : metricText(metric.target_excluded_median, metric.unit)],
    ["目标公司值", metric.target_value === null ? "缺失或不适用" : metricText(metric.target_value, metric.unit)],
  ];
  for (const [name, value] of rows) dl.append(el("dt", {}, name), el("dd", {}, value));
  card.append(dl);
  const checks = metric.verified_coverage || {};
  const count = Number(checks.verified || 0);
  const candidate = Number(checks.candidate || 0);
  card.append(el("p", { class: "industry-meta" }, `字段核验覆盖：${count} 项已核对，${candidate} 项候选待核。`));
  if (metric.gap_reason) card.append(el("p", { class: "notice" }, reasonText(metric.gap_reason, metric.metric_key)));
  return card;
}
function renderDetail(data) {
  detail.replaceChildren();
  if (data.enabled === false) {
    detail.append(el("p", { class: "notice" }, data.gaps?.[0] || "行业参照功能尚未开启。"));
    return;
  }
  const head = el("section", { class: "card" });
  head.append(el("h2", {}, data.metric_pack === "bank_annual_v1" ? "银行研究样本" : "白酒研究样本"));
  head.append(el("p", { class: "industry-meta" }, `名单观察日：${data.observed_on} · 名单状态：${stateNames[data.verification] || "候选待核"} · 样本：${data.candidate_count} 家`));
  head.append(el("p", { class: "industry-notice" }, "本页年度结果使用当前所示名单；名单观察日期不等于历史生效日期，也不能据此推断历史可投资性。"));
  if (data.report_period) {
    const target = data.comparison_target;
    head.append(el("p", { class: "industry-meta" }, `报告期：${data.report_period} · 批次状态：${data.batch_status === "success" ? "完整执行" : "部分完成"} · 提交时间：${formatDateTime(data.submitted_at)}`));
    if (target) head.append(el("p", { class: "industry-meta" }, `“目标公司值”和“排除目标后的同行中位数”对应 ${target.name || "目标公司"}（${target.ticker}）；该目标由本批次名单映射确定。`));
  }
  if (!data.batch_run_id) head.append(el("p", { class: "notice" }, "该名单目前没有此年度的已提交结果。"));
  detail.append(head);
  if (data.metrics?.length) {
    const grid = el("div", { class: "industry-metrics" });
    for (const metric of data.metrics) grid.append(metricCard(metric));
    detail.append(grid);
    const table = el("section", { class: "card industry-table-wrap" });
    table.append(el("h2", {}, "公司明细"));
    const tbl = el("table", { class: "industry-table" });
    const keys = packMetrics[data.metric_pack] || [];
    const thead = el("thead"); const tr = el("tr");
    for (const title of ["公司", ...keys.map((key) => metricNames[key]), "字段材料引用"]) tr.append(el("th", {}, title));
    thead.append(tr); tbl.append(thead);
    const tbody = el("tbody");
    for (const company of data.companies || []) {
      if (company.metric_key !== keys[0]) continue;
      const row = el("tr");
      const companyCell = el("td");
      const companyTitle = `${company.name || "未命名"}（${company.ticker || "—"}）${company.is_target ? " · 目标公司" : ""}`;
      companyCell.append(company.ticker
        ? el("a", { href: `/app/stocks/${encodeURIComponent(company.ticker)}`, title: "进入公司研究" }, companyTitle)
        : el("span", {}, companyTitle));
      row.append(companyCell);
      for (const key of keys) {
        const item = (data.companies || []).find((x) => x.company_id === company.company_id && x.metric_key === key);
        const reason = reasonText(item?.exclusion_reason, key);
        const cell = el("td", { class: "num" }, item?.included ? metricText(item.value, item.unit) : reason);
        row.append(cell);
      }
      const evidence = el("td");
      for (const key of keys) {
        const item = (data.companies || []).find((x) => x.company_id === company.company_id && x.metric_key === key);
        for (const ref of item?.evidence_refs || []) { const link = sourceLink(ref); if (link) evidence.append(link); }
      }
      if (!evidence.childNodes.length) evidence.textContent = "无字段材料引用";
      row.append(evidence); tbody.append(row);
    }
    tbl.append(tbody); table.append(tbl); detail.append(table);
  }
  for (const gap of [...(data.gaps || []), ...(data.warnings || [])]) detail.append(el("p", { class: "notice" }, gap));
}
let detailRequestId = 0;
async function loadDetail() {
  const requestId = ++detailRequestId;
  const key = industrySelect.value;
  if (!key) return;
  detail.replaceChildren(el("p", { class: "notice notice-loading" }, "正在读取已提交年度结果…"));
  try {
    const period = periodSelect.value;
    const suffix = period ? `?report_period=${encodeURIComponent(period)}` : "";
    const result = await get(`/industries/${encodeURIComponent(key)}${suffix}`);
    if (requestId !== detailRequestId) return;
    renderDetail(result);
  } catch (error) {
    if (requestId !== detailRequestId) return;
    detail.replaceChildren(el("p", { class: "notice notice-error" }, error instanceof ApiError ? error.message : "行业结果读取失败。"));
  }
}
async function boot() {
  try {
    const result = await get("/industries");
    if (!result.enabled) {
      $("industries-list").replaceChildren(el("p", { class: "notice" }, result.gaps?.[0] || "行业参照功能尚未开启。"));
      return;
    }
    const industries = result.industries || [];
    if (!industries.length) {
      $("industries-list").replaceChildren(el("p", { class: "notice" }, "尚无已保存的行业名单。"));
      industrySelect.replaceChildren(el("option", { value: "" }, "暂无名单"));
      return;
    }
    $("industries-list").replaceChildren(el("p", { class: "notice" }, `找到 ${industries.length} 个已保存研究样本。`));
    industrySelect.replaceChildren(...industries.map((item) => {
      const name = item.metric_pack === "bank_annual_v1" ? "银行研究样本" : "白酒研究样本";
      return el("option", { value: item.cohort_key }, `${name} · ${item.candidate_count} 家`);
    }));
    industrySelect.addEventListener("change", () => selectIndustry(industries));
    periodSelect.addEventListener("change", loadDetail);
    selectIndustry(industries);
  } catch (error) {
    $("industries-list").replaceChildren(el("p", { class: "notice notice-error" }, error instanceof ApiError ? error.message : "行业名单读取失败。"));
  }
}
async function selectIndustry(industries) {
  const item = industries.find((row) => row.cohort_key === industrySelect.value);
  const periods = item?.periods || [];
  periodSelect.disabled = !periods.length;
  periodSelect.replaceChildren(el("option", { value: "" }, periods.length ? "选择年度" : "暂无已提交年度"), ...periods.map((period) => el("option", { value: period }, period)));
  if (periods.length) periodSelect.value = periods[0];
  await loadDetail();
}
boot();
