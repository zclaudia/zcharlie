// 纯格式化函数（无 DOM）：候选理由、规则条件、状态标签。
// 只把 API 返回的确定性数值换成可读文字，不计算、不打分、不给操作判断。
// 措辞只描述研究优先级；区间名保留标识符并配中性中文标签。

export const FACTOR_LABELS = Object.freeze({
  roe: "ROE",
  roe_stability: "ROE 稳定性",
  gross_margin: "毛利率",
  net_margin: "净利率",
  debt_ratio: "资产负债率",
  net_profit_growth_3y: "净利润 3 年复合增速",
  revenue_growth_3y: "营收 3 年复合增速",
  free_cash_flow: "自由现金流",
  eps_ttm: "EPS(TTM)",
  pe_ttm: "PE(TTM)",
  pb_mrq: "PB(MRQ)",
  ps_ttm: "PS(TTM)",
  pe_fy: "PE(FY)",
  pb_fy: "PB(FY)",
  ps_fy: "PS(FY)",
  market_cap: "总市值",
  return_1y: "近 1 年涨跌幅",
  volatility_1y: "近 1 年波动率",
  max_drawdown_1y: "近 1 年最大回撤",
  dividend_yield_ttm: "股息率(TTM)",
  turnover_20d: "20 日换手率",
  is_st: "ST 标记",
  trade_status: "交易状态",
  report_overdue: "财报逾期",
  pe_percentile_5y: "PE 5 年分位",
  pb_percentile_5y: "PB 5 年分位",
  position_weight: "持仓权重",
  gain_vs_cost: "相对成本涨跌",
  holding_days: "持有天数",
});

// 区间是固定枚举（funnel-design 5.4）；标签只描述"需要关注/复核"，不是操作指令。
export const ZONE_LABELS = Object.freeze({
  buy_zone: "可研究区间",
  drop: "移出候选",
  add: "持仓区间 add",
  trim: "持仓区间 trim",
  exit_review: "持仓区间 复核",
});

export const CANDIDATE_STATUS_LABELS = Object.freeze({
  open: "待处理",
  promoted: "已晋级",
  dismissed: "已忽略",
  expired: "已过期",
});

export const DRAFT_STATE_LABELS = Object.freeze({
  none: "待起草",
  pending: "起草中",
  ready: "草稿就绪",
  failed: "起草失败",
});

export const RULE_STATUS_LABELS = Object.freeze({
  draft: "草稿",
  active: "生效",
  retired: "已停用",
});

export const CALIBRATION_LABELS = Object.freeze({
  uncalibrated: "未校准",
  backtested: "已回测",
  validated: "已验证",
});

export const VERIFICATION_STATUS_LABELS = Object.freeze({
  pending: "排队中",
  deferred: "已延后",
  running: "核对中",
  done: "已完成",
  failed: "核对失败",
  superseded: "已合并",
});

export const VERIFICATION_TRIGGER_LABELS = Object.freeze({
  promote: "晋级核对",
  conflict: "免费源冲突",
  missing: "免费源缺失",
  exit_review: "复核",
  calibration: "抽样校准",
});

export const DECISION_KIND_LABELS = Object.freeze({
  nominate: "提名",
  promote: "晋级",
  dismiss: "忽略",
  approve_rule: "批准规则",
  reject_rule: "驳回草稿",
  edit_rule: "修改参数",
});

export const OUTCOME_LABELS = Object.freeze({
  met: "满足",
  not_met: "未满足",
  missing: "缺失",
  not_applicable: "不适用",
});

export const QUALITY_LABELS = Object.freeze({
  ok: "正常",
  approx: "近似",
  missing: "缺失",
  not_applicable: "不适用",
});

export const FLAG_LABELS = Object.freeze({
  approx: "近似",
  short_history: "历史不足",
  provider_fallback: "备用来源",
  industry_fallback: "行业回退",
  factor_absent: "因子缺失",
});

export const INDUSTRY_GROUP_LABELS = Object.freeze({
  banking: "银行",
  insurance: "保险",
  broker: "券商",
  utility: "公用事业",
  cyclical: "周期",
  growth_manufacturing: "成长制造",
  venture: "互联网与软件",
  consumer: "消费",
  healthcare: "医药医疗",
  real_estate: "地产",
  other: "其他",
});

const OP_SYMBOLS = Object.freeze({
  "<": "<",
  "<=": "≤",
  ">": ">",
  ">=": "≥",
  "==": "=",
  "!=": "≠",
});

export function label(map, key) {
  if (key === null || key === undefined || key === "") return "—";
  return Object.prototype.hasOwnProperty.call(map, key) ? map[key] : String(key);
}

export function factorLabel(name) {
  return label(FACTOR_LABELS, name);
}

function trimZeros(text) {
  return text.includes(".") ? text.replace(/\.?0+$/, "") : text;
}

function fixed(value, digits, trim) {
  const text = value.toFixed(digits);
  return trim ? trimZeros(text) : text;
}

// 单位来自因子注册表（API 的 unit 字段）。
// precise=true 用于实际值（固定 1 位小数），否则用于阈值（去掉多余的 0）。
export function formatFactorValue(value, unit, { precise = true } = {}) {
  if (value === null || value === undefined) return "—";
  if (typeof value === "boolean") return value ? "是" : "否";
  if (typeof value !== "number" || !Number.isFinite(value)) return String(value);
  const kind = typeof unit === "string" ? unit : "";
  if (kind.startsWith("bool")) return value ? "是" : "否";
  if (kind.startsWith("fraction")) {
    return `${fixed(value * 100, precise ? 1 : 2, !precise)}%`;
  }
  if (kind.startsWith("ratio")) return fixed(value, 2, !precise);
  if (kind === "times") return `${fixed(value, precise ? 1 : 2, !precise)} 倍`;
  if (kind === "CNY") {
    if (Math.abs(value) >= 1e8) return `${fixed(value / 1e8, precise ? 1 : 2, !precise)} 亿元`;
    if (Math.abs(value) >= 1e4) return `${fixed(value / 1e4, precise ? 1 : 2, !precise)} 万元`;
    return `${fixed(value, 0, false)} 元`;
  }
  if (kind === "CNY/share") return `${fixed(value, 2, !precise)} 元`;
  return trimZeros(value.toFixed(4));
}

function formatThreshold(op, threshold, unit) {
  if (op === "between" && Array.isArray(threshold) && threshold.length === 2) {
    const [low, high] = threshold;
    return `介于 ${formatFactorValue(low, unit, { precise: false })}–${formatFactorValue(high, unit, { precise: false })}`;
  }
  if (op === "in" && Array.isArray(threshold)) {
    return `属于 {${threshold.map((item) => formatFactorValue(item, unit, { precise: false })).join(", ")}}`;
  }
  const symbol = label(OP_SYMBOLS, op);
  return `${symbol} ${formatFactorValue(threshold, unit, { precise: false })}`;
}

// 一条筛选理由 -> "ROE 12.0% ≥ 8%"；缺失 / 不适用时显式说明，不静默隐藏。
export function formatReason(reason) {
  if (!reason || typeof reason !== "object") return "—";
  if (!reason.factor) return reason.detail ? String(reason.detail) : "—";
  const name = factorLabel(reason.factor);
  const threshold = formatThreshold(reason.op, reason.threshold, reason.unit);
  if (reason.outcome === "not_applicable") return `${name} 不适用（条件跳过：${threshold}）`;
  if (reason.actual === null || reason.actual === undefined) {
    return `${name} 缺失（条件 ${threshold}）`;
  }
  const actual = formatFactorValue(reason.actual, reason.unit);
  if (reason.op === "between" || reason.op === "in") return `${name} ${actual}（${threshold}）`;
  return `${name} ${actual} ${threshold}`;
}

export function paramName(value) {
  if (typeof value !== "string") return null;
  const match = /^\$([a-z][a-z0-9_]*)$/.exec(value);
  return match ? match[1] : null;
}

function resolveValue(value, params) {
  if (Array.isArray(value)) return value.map((item) => resolveValue(item, params));
  const name = paramName(value);
  if (name === null) return value;
  return params && Object.prototype.hasOwnProperty.call(params, name) ? params[name] : value;
}

function paramsOf(value) {
  const list = Array.isArray(value) ? value : [value];
  return list.map(paramName).filter((name) => name !== null);
}

// 规则条件 {factor, op, value} -> "PE 5 年分位 ≤ 25%"，参数引用解析为生效值。
export function formatCondition(condition, params, units) {
  if (!condition || typeof condition !== "object") return { text: "—", params: [] };
  const unit = units ? units[condition.factor] : undefined;
  const resolved = resolveValue(condition.value, params);
  return {
    text: `${factorLabel(condition.factor)} ${formatThreshold(condition.op, resolved, unit)}`,
    params: paramsOf(condition.value),
  };
}

export function formatDate(value) {
  if (typeof value !== "string" || !value) return "—";
  return value.slice(0, 10);
}

export function formatDateTime(value) {
  if (typeof value !== "string" || !value) return "—";
  return value.slice(0, 16).replace("T", " ");
}

// 草稿理由：起草 Agent 存 JSON（逐参数理由 / 整体思路 / 复核点）；
// 人工或旧草稿可能是纯文本，原样展示。
export function parseRationale(raw) {
  if (typeof raw !== "string" || !raw.trim()) return { text: "", params: {}, overall: "", watch: [] };
  try {
    const data = JSON.parse(raw);
    if (data && typeof data === "object" && !Array.isArray(data)) {
      return {
        text: "",
        params: data.params && typeof data.params === "object" ? data.params : {},
        overall: typeof data.overall_rationale === "string" ? data.overall_rationale : "",
        watch: Array.isArray(data.watch_points) ? data.watch_points.map(String) : [],
      };
    }
  } catch {
    // 纯文本理由
  }
  return { text: raw, params: {}, overall: "", watch: [] };
}

// 422 的 detail：规则校验 {message, issues:[{code,path,message}]}、
// FastAPI 校验 [{loc,msg}] 或字符串 -> 逐条文字。
export function describeIssues(detail) {
  if (!detail) return [];
  if (typeof detail === "string") return [detail];
  if (Array.isArray(detail)) {
    return detail.map((item) =>
      item && typeof item === "object"
        ? `${Array.isArray(item.loc) ? item.loc.join(".") : ""} ${item.msg || ""}`.trim()
        : String(item)
    );
  }
  if (typeof detail === "object") {
    if (Array.isArray(detail.issues) && detail.issues.length) {
      return detail.issues.map((issue) => `${issue.path || ""}：${issue.message || issue.code || ""}`);
    }
    if (detail.message) return [String(detail.message)];
  }
  return [JSON.stringify(detail)];
}

export function inRange(value, range) {
  return (
    typeof value === "number" &&
    Number.isFinite(value) &&
    range &&
    value >= range.min &&
    value <= range.max
  );
}
