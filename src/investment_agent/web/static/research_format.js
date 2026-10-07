// 单股研究页的可读化：评分分项、证据点路径、LLM 解释文本。
// 只把确定性结果中的标识符 / 枚举 / 原始小数换成中文与格式化数值，不复算分数。
// 与候选页分开：候选页不展示综合分（见 format.js 开头的约定）。

import {
  FIELD_LABELS,
  FRESHNESS_LABELS,
  INDUSTRY_GROUP_LABELS,
  METRIC_UNITS,
  QUALITY_FLAG_LABELS,
  TEMPLATE_LABELS,
  ZONE_LABELS,
  fieldLabel,
  formatFactorValue,
  formatParamValue,
  label,
  paramLabel,
  parseParamName,
  templateLabel,
} from "./format.js";

function trimZeros(text) {
  return text.includes(".") ? text.replace(/\.?0+$/, "") : text;
}

// 评分分项（factor_scores）中文名。
export const SCORE_FACTOR_LABELS = Object.freeze({
  valuation_level: "估值水平",
  shareholder_return: "股东回报",
  roe_quality: "ROE 质量",
  roe_level: "ROE 水平",
  roe_stability: "ROE 稳定性",
  profit_growth: "利润成长",
  revenue_growth: "营收增长",
  drawdown_control: "回撤控制",
  capital_structure_review: "资本结构复核",
  financial_leverage_review: "财务杠杆复核",
  balance_sheet_quality: "资产负债表质量",
  cash_flow_quality: "现金流质量",
  cash_reserve: "现金储备",
  debt_level: "负债水平",
  dividend_yield: "股息率",
  growth_durability: "成长持续性",
  growth_quality: "成长质量",
  profitability_quality: "盈利质量",
  competitive_advantage: "竞争优势",
  cost_advantage: "成本优势",
  cycle_position: "周期位置",
  resource_reserve: "资源储备",
  industry_space: "行业空间",
  tam_potential: "市场空间",
  technical_barrier: "技术壁垒",
  commercialization_path: "商业化路径",
});

export const RISK_LABELS = Object.freeze({
  low: "低",
  medium_low: "中低",
  medium: "中",
  medium_high: "中高",
  high: "高",
});

export const VALUATION_LABELS = Object.freeze({
  cheap: "偏低",
  fair: "合理",
  expensive: "偏高",
  unknown: "未知",
});

export const STRATEGY_LABELS = Object.freeze({
  dividend_blue_chip: "红利蓝筹",
  quality_growth: "质量成长",
  steady_quality_growth: "稳健质量成长",
  cycle_reversal: "周期反转",
  high_growth_venture: "高成长风险投资",
  generic: "通用",
  banking: "银行模型",
  insurance: "保险模型",
  public_utility: "公用事业模型",
  manufacturing_growth: "制造成长模型",
});

export function scoreFactorLabel(name) {
  return label(SCORE_FACTOR_LABELS, name);
}


// 点路径 -> 中文标签："key_metrics.roe" -> "ROE"，"factor_scores.valuation_level" -> "估值水平（分项）"。
const PATH_LABELS = Object.freeze({
  research_confidence_label: "研究置信度等级",
  primary_strategy_label: "主策略",
  selected_model_label: "选用模型",
  strategy_type: "策略类型",
  source_mode: "数据模式",
  strategy_label: "策略",
  primary_strategy: "主策略",
  secondary_strategy: "次策略",
  secondary_strategy_label: "次策略",
  classification_confidence: "分类置信度",
  strategy_confidence: "策略置信度",
  selected_model: "选用模型",
  model_fit_score: "模型适配分",
  model_fit_label: "模型适配度",
  applicable_models: "适用模型",
  industry: "行业",
  risk_level: "风险等级",
  valuation_level: "估值水平",
  investment_score: "投资分",
  business_score: "业务分",
  research_confidence_score: "研究置信度",
  data_quality_score: "数据质量分",
  factor_coverage: "因子覆盖率",
  missing_fields: "缺失字段",
  missing_factors: "缺失因子",
  quality_flags: "质量标记",
  warnings: "提示",
  suggestion: "工作流标签",
  suggestion_label: "工作流标签",
  provider_conflicts: "来源冲突",
  provider_unavailable_fields: "来源不可用字段",
  needs_agent_review: "需人工复核",
  freshness: "数据新鲜度",
  field_sources: "字段来源",
  company_profile: "公司画像",
  business_model_tags: "业务标签",
  non_applicable_fields: "不适用字段",
  classification_fit_scores: "分类适配分",
  evidence_summary: "证据摘要",
  is_stale: "是否过期",
  report_period_end: "报告期",
  age_hours: "距今小时",
  age_trading_days: "距今交易日",
  verification: "核对状态",
  quality: "质量",
});

const PATH_SECTIONS = Object.freeze({
  key_metrics: (rest) => fieldLabel(rest[0]),
  factor_scores: (rest) => `${scoreFactorLabel(rest[0])}（分项）`,
  classification_fit_scores: (rest) => `分类适配 · ${label(STRATEGY_LABELS, rest[0])}`,
  freshness: (rest) =>
    `新鲜度 · ${label(FRESHNESS_LABELS, rest[0])}${rest[1] ? ` · ${label(PATH_LABELS, rest[1])}` : ""}`,
  field_sources: (rest) => {
    const field = rest.length >= 2 ? rest[1] : rest[0];
    const tail = rest.length >= 3 ? ` · ${label(PATH_LABELS, rest[2])}` : "";
    return `${fieldLabel(field)} 来源${tail}`;
  },
  company_profile: (rest) => label(PATH_LABELS, rest[0]),
});

export function pathLabel(path) {
  if (typeof path !== "string" || !path) return "—";
  const [head, ...rest] = path.split(".");
  if (rest.length && PATH_SECTIONS[head]) return PATH_SECTIONS[head](rest);
  if (!rest.length) {
    if (Object.prototype.hasOwnProperty.call(PATH_LABELS, head)) return PATH_LABELS[head];
    if (Object.prototype.hasOwnProperty.call(FIELD_LABELS, head)) return FIELD_LABELS[head];
  }
  return path;
}

// 证据值按路径格式化（key_metrics 用单位，分数保留 1 位，枚举换中文）。
export function formatPathValue(path, value) {
  if (value === null || value === undefined) return "无值";
  if (Array.isArray(value)) {
    if (!value.length) return "无";
    if (value.length > 3) return `${value.length} 项`;
    return value.map((item) => (typeof item === "string" ? humanizeText(pathLabel(item)) : JSON.stringify(item))).join("、");
  }
  if (typeof value === "boolean") return value ? "是" : "否";
  const parts = String(path || "").split(".");
  const leaf = parts[parts.length - 1];
  if (typeof value === "number") {
    if (parts[0] === "key_metrics" && METRIC_UNITS[leaf]) return formatFactorValue(value, METRIC_UNITS[leaf]);
    if (/confidence|coverage|data_quality_score/.test(leaf) && value <= 1) return `${(value * 100).toFixed(0)}%`;
    return trimZeros(value.toFixed(1));
  }
  if (typeof value === "string") {
    if (leaf === "risk_level") return label(RISK_LABELS, value);
    if (leaf === "valuation_level") return label(VALUATION_LABELS, value);
    if (/strategy$|selected_model/.test(leaf)) return label(STRATEGY_LABELS, value);
    if (leaf === "suggestion" || leaf === "verification" || leaf === "quality") return humanizeText(value);
    return value;
  }
  if (typeof value === "object") {
    const keys = Object.keys(value);
    return keys.length ? `${keys.length} 项` : "无";
  }
  return String(value);
}

// LLM 文本里残留的点路径 / 标识符 / 原始小数（旧数据或模型未遵守提示）：
// "key_metrics.roe 为 0.120198" -> "ROE 为 12.0%"；只替换已知标识符，其余原样。
const IDENT_LABELS = Object.freeze({
  ...INDUSTRY_GROUP_LABELS,
  calibration_status: "校准状态",
  preference_notes: "用户偏好备注",
  negative_earnings: "亏损",
  screen_result: "筛选结果",
  ...STRATEGY_LABELS,
  ...SCORE_FACTOR_LABELS,
  ...FIELD_LABELS,
  ...PATH_LABELS,
  ...ZONE_LABELS,
  ...QUALITY_FLAG_LABELS,
  cheap: "偏低",
  expensive: "偏高",
  fair: "合理",
  monitor: "持续跟踪",
  needs_review: "待复核",
  medium: "中",
  medium_high: "中高",
  medium_low: "中低",
  unverified: "未核对",
  verified: "已核对",
  true: "是",
  false: "否",
  severity: "级别",
  info: "提示",
  review: "需复核",
  financial_data: "财报",
  market_data: "行情",
  verification: "核对状态",
  warnings: "提示",
  suggestion: "工作流标签",
  source_mode: "数据模式",
  snapshot: "快照",
  direct: "直取",
  derived: "派生",
});

const FRACTION_LABEL_PATTERN = (() => {
  const names = Object.entries(METRIC_UNITS)
    .filter(([key, unit]) => unit === "fraction" && FIELD_LABELS[key])
    .map(([key]) => FIELD_LABELS[key])
    .sort((a, b) => b.length - a.length)
    .map((name) => name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  return new RegExp(`(${names.join("|")})(\\s*(?:为|是|=|：)?\\s*)(-?\\d+\\.\\d+)(?![\\d%])`, "g");
})();

export function humanizeText(text) {
  if (typeof text !== "string" || !text) return text || "";
  let out = text.replace(
    /\b((?:[a-z][a-z0-9_]*\.)*[a-z][a-z0-9_]*)(\s*(?:为|=|：|:)\s*)(-?\d+(?:\.\d+)?)/g,
    (whole, path, sep, number) => {
      const leaf = path.split(".").pop();
      const metricLike = path.startsWith("key_metrics.") || !path.includes(".");
      const value = Number(number);
      let shown;
      const param = !path.includes(".") && !METRIC_UNITS[leaf] ? parseParamName(path) : null;
      if (param && FIELD_LABELS[param.factor]) {
        return `${paramLabel(path)}${sep.trim() === "=" ? " " : sep}${formatParamValue(path, value)}`;
      }
      if (metricLike && METRIC_UNITS[leaf]) shown = formatFactorValue(value, METRIC_UNITS[leaf]);
      else if (path.includes(".")) shown = formatPathValue(path, value);
      else return whole;
      const name = path.includes(".") ? pathLabel(path) : fieldLabel(path);
      return `${name}${sep.trim() === "=" ? " " : sep}${shown}`;
    }
  );
  // "PB 5 年分位为 0.114" -> "PB 5 年分位为 11.4%"：只处理比例类因子的中文名 + 带小数点的数。
  out = out.replace(FRACTION_LABEL_PATTERN, (whole, name, sep, number) => {
    const value = Number(number);
    return Math.abs(value) <= 5 ? `${name}${sep}${formatFactorValue(value, "fraction")}` : whole;
  });
  out = out.replace(/([\u4e00-\u9fa5]{2,})@(\d+)/g, "$1 v$2");
  out = out.replace(/\b([a-z][a-z0-9_]*)@(\d+)\b/g, (whole, id, version) =>
    Object.prototype.hasOwnProperty.call(TEMPLATE_LABELS, id) ? templateLabel(`${id}@${version}`) : whole
  );
  out = out.replace(/\b[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+\b/g, (path) => {
    const labelled = pathLabel(path);
    return labelled === path ? path : labelled;
  });
  out = out.replace(/\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b|\b(?:roe|pe|pb|cheap|expensive|fair|monitor|industry|medium|banking|insurance|unverified|verified|true|false|severity|info|review|warnings|suggestion|verification|snapshot|direct|derived)\b/g, (word) => {
    if (Object.prototype.hasOwnProperty.call(IDENT_LABELS, word)) return IDENT_LABELS[word];
    const param = parseParamName(word);
    return param && FIELD_LABELS[param.factor] ? paramLabel(word) : word;
  });
  return out;
}

