"""Every domain enum has one canonical Chinese UI label."""

from enum import StrEnum


class LabelledEnum(StrEnum):
    @property
    def label(self) -> str:
        return LABELS[type(self).__name__][self.value]


class BusinessView(LabelledEnum):
    FAVORABLE = "favorable"
    MIXED = "mixed"
    UNFAVORABLE = "unfavorable"
    UNDETERMINED = "undetermined"


class PriceView(LabelledEnum):
    ATTRACTIVE = "attractive"
    BALANCED = "balanced"
    DEMANDING = "demanding"
    UNDETERMINED = "undetermined"


class Sufficiency(LabelledEnum):
    INSUFFICIENT = "insufficient"
    BOUNDED = "bounded"
    SUFFICIENT = "sufficient_for_scope"


class CompanyAction(LabelledEnum):
    BUY = "buy"
    AVOID = "avoid"
    SELL = "sell"
    WAIT = "wait"


class RecommendationStatus(LabelledEnum):
    PUBLISHED = "published"
    REVIEW_REQUIRED = "review_required"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"
    WITHDRAWN = "withdrawn"


class CaseStatus(LabelledEnum):
    LEAD = "lead"
    RESEARCHING = "researching"
    TRACKING = "tracking"
    PARKED = "parked"
    ARCHIVED = "archived"


class TaskStatus(LabelledEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_DATA = "waiting_data"
    WAITING_USER = "waiting_user"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StopReason(LabelledEnum):
    ANSWERED = "answered"
    EVIDENCE_BLOCKED = "evidence_blocked"
    NO_PROGRESS = "no_progress"
    BUDGET_EXHAUSTED = "budget_exhausted"
    CANCELLED = "cancelled"
    FAILED = "failed"


class DecisionKind(LabelledEnum):
    ACCEPT = "accept"
    REJECT = "reject"
    DEFER = "defer"
    INDEPENDENT = "independent"


class DimensionStatus(LabelledEnum):
    EVIDENCED = "evidenced"
    QUALITATIVE = "qualitative"
    MISSING = "missing"


class CalculationStatus(LabelledEnum):
    OK = "ok"
    NOT_APPLICABLE = "not_applicable"
    MISSING_INPUTS = "missing_inputs"
    MISSING_ASSUMPTIONS = "missing_assumptions"
    STALE_INPUTS = "stale_inputs"
    OUTSIDE_SEARCH_RANGE = "outside_search_range"
    CALCULATION_ERROR = "calculation_error"


class NumericRole(LabelledEnum):
    FACT = "historical_fact"
    OUTPUT = "calculation_output"
    SCENARIO = "scenario_assumption"
    THRESHOLD = "threshold"
    DATE = "date"
    CODE = "code"


class AssumptionSource(LabelledEnum):
    METHOD_DEFAULT = "method_default"
    USER_SCENARIO = "user_scenario"
    AGENT_SCENARIO = "agent_scenario"


LABELS: dict[str, dict[str, str]] = {
    "BusinessView": dict(
        favorable="偏正面", mixed="好坏参半", unfavorable="偏负面", undetermined="暂不判断"
    ),
    "PriceView": dict(
        attractive="有吸引力", balanced="大致合理", demanding="要求较高", undetermined="暂不判断"
    ),
    "Sufficiency": dict(insufficient="不足", bounded="有限", sufficient_for_scope="足以回答本问题"),
    "CompanyAction": dict(buy="可考虑买入", avoid="回避", sell="可考虑卖出", wait="等待"),
    "RecommendationStatus": dict(
        published="有效",
        review_required="待复核",
        expired="已过期",
        superseded="已被替代",
        withdrawn="已撤回",
    ),
    "CaseStatus": dict(
        lead="线索", researching="研究中", tracking="持续跟踪", parked="搁置", archived="归档"
    ),
    "TaskStatus": dict(
        queued="排队中",
        running="进行中",
        waiting_data="等待数据",
        waiting_user="等你处理",
        succeeded="已完成",
        partial="部分完成",
        failed="失败",
        cancelled="已取消",
    ),
    "StopReason": dict(
        answered="已回答",
        evidence_blocked="证据受阻",
        no_progress="无进展",
        budget_exhausted="预算用完",
        cancelled="已取消",
        failed="失败",
    ),
    "DecisionKind": dict(accept="接受", reject="不采纳", defer="暂缓", independent="我自行决定"),
    "DimensionStatus": dict(evidenced="已取证", qualitative="仅定性", missing="尚缺材料"),
    "CalculationStatus": dict(
        ok="计算成功",
        not_applicable="不适用",
        missing_inputs="缺少输入",
        missing_assumptions="缺少假设",
        stale_inputs="输入过期",
        outside_search_range="超出求解区间",
        calculation_error="计算失败",
    ),
    "NumericRole": dict(
        historical_fact="事实",
        calculation_output="计算",
        scenario_assumption="假设",
        threshold="条件",
        date="日期",
        code="代码",
    ),
    "AssumptionSource": dict(
        method_default="方法默认", user_scenario="你的情景", agent_scenario="Agent 情景"
    ),
}
