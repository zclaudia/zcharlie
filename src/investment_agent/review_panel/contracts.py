"""Multi-perspective review contracts (docs/product/multi-model-review-design.md §5).

``SeatOpinion`` is what one seat's model returns. Everything the server adds
(seat identity, lens version, model snapshot, evidence the seat actually read,
validation annotations, usage) lives in ``SeatAnnotations`` and the storage row,
never in the model-written object.

Review opinions are not formal recommendations: they never pass publication
checks, never enter the actionable-opinion view and never link to user
decisions or portfolio plans (§5.3).
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

DIMENSION_TAXONOMY_VERSION = "v1"
MAX_SEATS_PER_ROUND = 6
REQUIRED_SEAT_CAPABILITIES: tuple[str, ...] = ("tools", "json_object")


class CompanyStance(StrEnum):
    POSITIVE = "positive"
    NEUTRAL = "neutral"
    NEGATIVE = "negative"
    INSUFFICIENT = "insufficient"


COMPANY_STANCE_LABELS: dict[CompanyStance, str] = {
    CompanyStance.POSITIVE: "偏正面",
    CompanyStance.NEUTRAL: "中性",
    CompanyStance.NEGATIVE: "偏负面",
    CompanyStance.INSUFFICIENT: "证据不足",
}


class Confidence(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


CONFIDENCE_LABELS: dict[Confidence, str] = {
    Confidence.LOW: "低",
    Confidence.MEDIUM: "中",
    Confidence.HIGH: "高",
}


class TradeView(StrEnum):
    WATCH_BUY = "watch_buy"
    HOLD_WATCH = "hold_watch"
    CONSIDER_REDUCE = "consider_reduce"
    AVOID = "avoid"
    NO_VIEW = "no_view"


TRADE_VIEW_LABELS: dict[TradeView, str] = {
    TradeView.WATCH_BUY: "关注买入",
    TradeView.HOLD_WATCH: "持有观察",
    TradeView.CONSIDER_REDUCE: "考虑减持",
    TradeView.AVOID: "回避",
    TradeView.NO_VIEW: "无看法",
}


class Dimension(StrEnum):
    """Fixed taxonomy for deterministic comparison (§5.2), version ``v1``."""

    REVENUE_GROWTH = "revenue_growth"
    MARGIN = "margin"
    CASH_FLOW_QUALITY = "cash_flow_quality"
    BALANCE_SHEET = "balance_sheet"
    VALUATION = "valuation"
    INDUSTRY_STRUCTURE = "industry_structure"
    CYCLE_POSITION = "cycle_position"
    GOVERNANCE = "governance"
    POLICY_REGULATION = "policy_regulation"
    DATA_QUALITY = "data_quality"
    OTHER = "other"


DIMENSION_LABELS: dict[Dimension, str] = {
    Dimension.REVENUE_GROWTH: "营收增长",
    Dimension.MARGIN: "利润率",
    Dimension.CASH_FLOW_QUALITY: "现金流质量",
    Dimension.BALANCE_SHEET: "资产负债",
    Dimension.VALUATION: "估值水平",
    Dimension.INDUSTRY_STRUCTURE: "行业格局",
    Dimension.CYCLE_POSITION: "周期位置",
    Dimension.GOVERNANCE: "治理与股东",
    Dimension.POLICY_REGULATION: "政策监管",
    Dimension.DATA_QUALITY: "数据质量",
    Dimension.OTHER: "其他",
}


class Direction(StrEnum):
    POSITIVE = "positive"
    NEGATIVE = "negative"


class _ModelOutput(BaseModel):
    # Model-written objects tolerate unknown keys so a chatty model does not
    # fail the whole seat; the server never reads anything outside the schema.
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


class Assumption(_ModelOutput):
    dimension: Dimension
    direction: Direction
    text: str = Field(min_length=1, max_length=600)
    evidence_refs: list[str] = Field(default_factory=list, max_length=12)


class Point(_ModelOutput):
    text: str = Field(min_length=1, max_length=600)
    evidence_refs: list[str] = Field(default_factory=list, max_length=12)


class Falsifier(_ModelOutput):
    dimension: Dimension
    text: str = Field(min_length=1, max_length=400)


class TradeCondition(_ModelOutput):
    """A conditional trade view. A threshold needs a basis or ``is_assumption``."""

    text: str = Field(min_length=1, max_length=400)
    metric: str | None = Field(default=None, max_length=80)
    threshold: float | None = None
    basis_ref: str | None = None
    is_assumption: bool = False


class SeatOpinion(_ModelOutput):
    """One seat's structured review (§5.1). Written by the model."""

    summary: str = Field(min_length=1, max_length=1200)
    company_stance: CompanyStance
    confidence: Confidence
    trade_view: TradeView
    trade_conditions: list[TradeCondition] = Field(default_factory=list, max_length=6)
    assumptions: list[Assumption] = Field(default_factory=list, max_length=16)
    bull_points: list[Point] = Field(default_factory=list, max_length=10)
    bear_points: list[Point] = Field(default_factory=list, max_length=10)
    falsifiers: list[Falsifier] = Field(default_factory=list, max_length=10)
    open_questions: list[str] = Field(default_factory=list, max_length=10)


class _ServerRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RefAnnotation(_ServerRecord):
    """A model-cited evidence ref that the seat did not actually read this round."""

    path: str
    """JSON path inside the opinion, e.g. ``bull_points[2].evidence_refs[0]``."""
    ref: str
    reason: str


class NumberAnnotation(_ServerRecord):
    """Result of the numeric scan in annotate mode (§5.3)."""

    path: str
    value: str
    status: str
    """``sourced`` (traced to a read evidence ref) or ``unverified``."""
    source_ref: str | None = None


class SeatAnnotations(_ServerRecord):
    invalid_refs: list[RefAnnotation] = Field(default_factory=list)
    numbers: list[NumberAnnotation] = Field(default_factory=list)
    missing_required_dimensions: list[Dimension] = Field(default_factory=list)


class RoundTrigger(StrEnum):
    MANUAL = "manual"
    CHANGE = "change"
    SCHEDULED = "scheduled"


class RoundStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    """Every seat reached a terminal state and the comparison is saved."""
    FAILED = "failed"
    """No seat produced an opinion."""


class SeatStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    """Usage limit hit; an opinion may still be present."""
    FAILED = "failed"


TERMINAL_SEAT_STATUSES = frozenset({SeatStatus.SUCCEEDED, SeatStatus.PARTIAL, SeatStatus.FAILED})


__all__ = [
    "COMPANY_STANCE_LABELS",
    "CONFIDENCE_LABELS",
    "DIMENSION_LABELS",
    "DIMENSION_TAXONOMY_VERSION",
    "MAX_SEATS_PER_ROUND",
    "REQUIRED_SEAT_CAPABILITIES",
    "TERMINAL_SEAT_STATUSES",
    "TRADE_VIEW_LABELS",
    "Assumption",
    "CompanyStance",
    "Confidence",
    "Dimension",
    "Direction",
    "Falsifier",
    "NumberAnnotation",
    "Point",
    "RefAnnotation",
    "RoundStatus",
    "RoundTrigger",
    "SeatAnnotations",
    "SeatOpinion",
    "SeatStatus",
    "TradeCondition",
    "TradeView",
]
