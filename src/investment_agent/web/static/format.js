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
  if (kind === "days") return `${fixed(value, 0, false)} 天`;
  return trimZeros(value.toFixed(4));
}

export function formatThreshold(op, threshold, unit) {
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

// 时间一律按北京时间显示。后端时间戳以 UTC 存储：带 Z / 偏移的按其换算，
// 不带时区的（SQLite 取回的 naive 值）按 UTC 解释；纯日期（交易日、数据日）原样返回，不做换算。
export const DISPLAY_TIMEZONE = "Asia/Shanghai";
const DATE_ONLY = /^\d{4}-\d{2}-\d{2}$/;
const HAS_ZONE = /(Z|[+-]\d{2}(:?\d{2})?)$/i;
const BEIJING_FORMAT = new Intl.DateTimeFormat("en-CA", {
  timeZone: DISPLAY_TIMEZONE,
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  hourCycle: "h23",
});

// "2026-10-09T16:09:43Z" -> {date: "2026-10-10", time: "00:09"}；无法解析返回 null。
function beijingParts(value) {
  if (typeof value !== "string" || !value || DATE_ONLY.test(value)) return null;
  const text = value.trim().replace(" ", "T");
  const parsed = new Date(HAS_ZONE.test(text) ? text : `${text}Z`);
  if (Number.isNaN(parsed.getTime())) return null;
  const parts = Object.fromEntries(BEIJING_FORMAT.formatToParts(parsed).map((part) => [part.type, part.value]));
  return { date: `${parts.year}-${parts.month}-${parts.day}`, time: `${parts.hour}:${parts.minute}` };
}

export function formatDate(value) {
  if (typeof value !== "string" || !value) return "—";
  const parts = beijingParts(value);
  return parts ? parts.date : value.slice(0, 10);
}

export function formatDateTime(value) {
  if (typeof value !== "string" || !value) return "—";
  const parts = beijingParts(value);
  return parts ? `${parts.date} ${parts.time}` : value.slice(0, 16).replace("T", " ");
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

// ---- 可读化（P0）：把标识符、枚举、原始小数换成人读文字 ----------------

// 字段（数据层）中文名；未在 FACTOR_LABELS 中的补充。
export const FIELD_LABELS = Object.freeze({
  ...FACTOR_LABELS,
  pe: "PE",
  pb: "PB",
  dividend_yield: "股息率",
  close: "收盘价",
  turnover: "换手率",
  bvps_fy: "每股净资产(FY)",
  eps_basic_fy: "基本 EPS(FY)",
  revenue_ps_fy: "每股营收(FY)",
  capital_expenditure: "资本开支",
  cash_dividend_per_share: "每股现金分红",
  net_profit_parent: "归母净利润",
  net_profit_total: "净利润",
  operating_cash_flow: "经营现金流",
  revenue_main: "主营收入",
  revenue_total: "营业总收入",
  total_equity: "所有者权益",
  total_shares: "总股本",
});

// 关键指标与因子的单位，复用 formatFactorValue。
export const METRIC_UNITS = Object.freeze({
  market_cap: "CNY",
  pe: "times",
  pb: "times",
  pe_ttm: "times",
  pb_mrq: "times",
  ps_ttm: "times",
  dividend_yield: "fraction",
  dividend_yield_ttm: "fraction",
  roe: "fraction",
  roe_stability: "ratio",
  debt_ratio: "fraction",
  gross_margin: "fraction",
  net_margin: "fraction",
  revenue_growth_3y: "fraction",
  net_profit_growth_3y: "fraction",
  max_drawdown_1y: "fraction",
  volatility_1y: "fraction",
  return_1y: "fraction",
  pe_percentile_5y: "fraction",
  pb_percentile_5y: "fraction",
  position_weight: "fraction",
  gain_vs_cost: "fraction",
  holding_days: "days",
  free_cash_flow: "CNY",
  operating_cash_flow: "CNY",
  eps_ttm: "CNY/share",
});

// 规则模板 id -> 中文名（与 config/rule_templates/*.yaml 的 name 保持一致）。
export const TEMPLATE_LABELS = Object.freeze({
  cycle_reversal: "周期反转",
  dividend_blue_chip: "红利蓝筹",
  high_growth_venture: "高成长风险投资",
  quality_growth: "质量成长",
  steady_quality_growth: "稳健质量成长",
});

// 运行 / 刷新 / 新鲜度等状态值（statusBadge 统一使用）。
export const STATUS_LABELS = Object.freeze({
  queued: "排队中",
  running: "运行中",
  success: "成功",
  partial: "部分成功",
  failed: "失败",
  needs_review: "待复核",
  ok: "正常",
  stale: "已过期",
  unknown: "未知",
  created: "已排队",
  reused: "复用运行",
  skipped: "已跳过",
  cancelled: "已取消",
  error: "错误",
  warning: "警告",
});

export const RUN_STAGE_LABELS = Object.freeze({
  queued: "排队",
  claimed: "已领取",
  created: "已创建",
  fetching: "获取数据",
  scoring: "评分",
  explaining: "生成解释",
  persisting: "写入结果",
  completed: "已完成",
  partial: "部分完成",
  failed: "失败",
});

export const QUALITY_FLAG_LABELS = Object.freeze({
  derived_field: "派生字段",
  factor_missing: "因子缺失",
  missing_field: "字段缺失",
  model_not_applicable: "模型不适用",
  short_price_history: "价格历史不足",
  stale_data: "数据过期",
  main_business_revenue: "主营收入口径",
  ...FLAG_LABELS,
});

// 数据源标识 -> 显示名：全站唯一的基础表（含全部 provider 键）。
// health.js 只在本页需要更具体的渠道说明时覆盖个别条目（如 akshare），不另建新表。
export const PROVIDER_LABELS = Object.freeze({
  baostock: "baostock",
  akshare: "AkShare",
  eastmoney: "东方财富",
  tencent: "腾讯行情",
  ifind: "iFinD",
});

export const SOURCE_VERIFICATION_LABELS = Object.freeze({
  unverified: "未核对",
  verified: "已核对",
  conflict: "有冲突",
});

export const PERIOD_KIND_LABELS = Object.freeze({
  annual: "年报",
  q1: "一季报",
  half: "半年报",
  h1: "半年报",
  q3: "三季报",
});

export const FRESHNESS_LABELS = Object.freeze({
  stock: "公司信息",
  market_data: "行情",
  financial_data: "财报",
});

export function fieldLabel(name) {
  return label(FIELD_LABELS, name);
}

// "cycle_reversal@2" -> "周期反转 v2"；未知模板原样返回。
export function templateLabel(ref) {
  if (typeof ref !== "string" || !ref) return "—";
  const [id, version] = ref.split("@");
  const name = label(TEMPLATE_LABELS, id);
  return version ? `${name} v${version}` : name;
}

// 规则参数名 -> 中文："buy_pb_pct_max" -> "可研究区间 · PB 5 年分位上限"。
// 命名约定：[区间前缀_]因子缩写_(min|max)；无前缀为筛选条件参数。
const PARAM_ZONE_PREFIXES = Object.freeze({
  buy: "buy_zone",
  drop: "drop",
  add: "add",
  trim: "trim",
  exit: "exit_review",
});

const PARAM_FACTOR_ALIASES = Object.freeze({
  pb_pct: "pb_percentile_5y",
  pe_pct: "pe_percentile_5y",
  weight: "position_weight",
  gain: "gain_vs_cost",
  drawdown: "max_drawdown_1y",
  revenue_growth: "revenue_growth_3y",
  profit_growth: "net_profit_growth_3y",
  volatility: "volatility_1y",
  dividend_yield: "dividend_yield_ttm",
  holding_days: "holding_days",
});

export function parseParamName(name) {
  if (typeof name !== "string") return null;
  const match = /^(?:(buy|drop|add|trim|exit)_)?([a-z0-9_]+?)_(min|max)$/.exec(name);
  if (!match) return null;
  const [, prefix, body, bound] = match;
  const factor = PARAM_FACTOR_ALIASES[body] || body;
  return { zone: prefix ? PARAM_ZONE_PREFIXES[prefix] : "screen", factor, bound };
}

export function paramLabel(name) {
  const parsed = parseParamName(name);
  if (!parsed) return String(name ?? "—");
  const zone = parsed.zone === "screen" ? "筛选" : label(ZONE_LABELS, parsed.zone);
  const factor = factorLabel(parsed.factor);
  const gap = /[A-Za-z)]$/.test(factor) ? " " : "";
  return `${zone} · ${factor}${gap}${parsed.bound === "min" ? "下限" : "上限"}`;
}

// 参数取值按其因子的单位显示；units 来自 API 的 factor_units，缺省用 METRIC_UNITS。
export function formatParamValue(name, value, units) {
  if (value === null || value === undefined || value === "") return "—";
  const parsed = parseParamName(name);
  const unit = parsed ? (units && units[parsed.factor]) || METRIC_UNITS[parsed.factor] : undefined;
  return unit ? formatFactorValue(value, unit, { precise: false }) : String(value);
}

// 原始错误串 -> 一行人话 + 原文（展开查看）。
export function summarizeError(raw) {
  if (raw === null || raw === undefined || raw === "") return { summary: "—", raw: "" };
  const text = String(raw);
  let prefix = "";
  let body = text;
  const stage = /^(explanation|scoring|fetching|persisting) failed:\s*/i.exec(body);
  if (stage) {
    prefix = { explanation: "解释生成失败", scoring: "评分失败", fetching: "数据获取失败", persisting: "写入失败" }[
      stage[1].toLowerCase()
    ];
    body = body.slice(stage[0].length);
  }
  let summary;
  const http = /Error code:\s*(\d{3})/.exec(body);
  const model = /model ([\w.\-]+)/.exec(body);
  if (http && /model_not_found|No available channel/.test(body)) {
    summary = `LLM 服务不可用（HTTP ${http[1]}${model ? ` · 模型 ${model[1]} 无可用渠道` : ""}）`;
  } else if (http) {
    summary = `LLM 服务返回错误（HTTP ${http[1]}）`;
  } else if (/IntegrityError/.test(body)) {
    const constraint = /(NOT NULL|UNIQUE|FOREIGN KEY) constraint failed:\s*([\w.]+)/.exec(body);
    summary = constraint
      ? `数据库写入失败（${constraint[2]} ${constraint[1] === "NOT NULL" ? "不能为空" : constraint[1] === "UNIQUE" ? "重复" : "外键不匹配"}）`
      : "数据库写入失败";
  } else if (/validation errors? for (\w+)/.test(body)) {
    const match = /(\d+) validation errors? for (\w+)/.exec(body);
    summary = match ? `LLM 输出不符合格式（${match[2]} 有 ${match[1]} 处校验错误）` : "LLM 输出不符合格式";
  } else if (/timed? ?out|Timeout/i.test(body)) {
    summary = "请求超时";
  } else if (/Quota|quota|budget/.test(body)) {
    summary = "数据源配额用尽";
  } else {
    summary = body.length > 80 ? `${body.slice(0, 80)}…` : body;
  }
  if (prefix) summary = `${prefix}：${summary}`;
  return { summary, raw: summary === text ? "" : text };
}

// 采集任务的英文明细（"applied 300, changed 300"）-> 中文；未知词原样保留。
const DETAIL_WORDS = Object.freeze({
  applied: "应用",
  changed: "变更",
  current: "当前",
  removed: "移除",
  added: "新增",
  changes: "变动",
  skipped: "跳过",
  failed: "失败",
  missing: "缺失",
  updated: "更新",
});

export function humanizeDetail(text) {
  if (typeof text !== "string" || !text) return text ?? "—";
  return text
    .replace(/\b([a-z]+) (\d+)/g, (whole, word, count) =>
      Object.prototype.hasOwnProperty.call(DETAIL_WORDS, word) ? `${DETAIL_WORDS[word]} ${count}` : whole
    )
    .replace(/, /g, "，");
}

export const INDUSTRY_SYSTEM_LABELS = Object.freeze({
  csrc: "证监会行业",
  sw2021: "申万 2021",
});
