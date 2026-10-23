"""A0/A1a investor contracts. Parsing is not a publication or permission check."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from investment_agent.investor.common import (
    Contract,
    DecimalValue,
    ObjectRef,
    OwnerId,
    Quantity,
    Record,
    Text,
)
from investment_agent.investor.enums import (
    AssumptionSource,
    BusinessView,
    CaseStatus,
    CompanyAction,
    DecisionKind,
    DimensionStatus,
    NumericRole,
    PriceView,
    RecommendationStatus,
    Sufficiency,
)


class TextNode(Contract):
    type: Literal["text"]
    text: Text


class EntityNode(Contract):
    type: Literal["entity_ref"]
    ref: ObjectRef = Field(description="Exact authorized listing identity only. Case, task, message and method references cannot be rendered as security identities.", json_schema_extra={"allOf": [{"properties": {"kind": {"const": "listing"}, "id": {"type": "integer", "minimum": 1}, "field_path": {"type": "null"}}}]})

    @field_validator("ref")
    @classmethod
    def listing_identity(cls, value: ObjectRef) -> ObjectRef:
        if value.kind != "listing" or type(value.id) is not int or value.field_path is not None:
            raise ValueError("entity_ref requires an exact listing reference; omit internal case/method/task/message metadata from research content")
        return value


class NumericNode(Contract):
    type: Literal["numeric_claim"]
    ref: ObjectRef = Field(description='historical_fact requires saved evidence with field_path="value". calculation_output requires a saved calculation with field_path="results.<exact result key>" (plural results). A bare record ref with field_path=null is not a numeric value reference.')
    role: NumericRole
    display_format: Literal["decimal", "percent", "date", "code"]
    # Value, subject, unit and precision are resolved by the publishing service.


ContentNode = Annotated[TextNode | EntityNode | NumericNode, Field(discriminator="type")]


class Claim(Contract):
    id: UUID
    kind: Literal["fact", "assumption", "inference"]
    text: tuple[ContentNode, ...]
    materiality: Literal["critical", "context"]
    support_refs: tuple[ObjectRef, ...]
    opposition_refs: tuple[ObjectRef, ...]
    assumption_refs: tuple[ObjectRef, ...]
    verification: Literal["pending", "verified", "conflicted"]
    impact_if_wrong: Text


class Hypothesis(Record):
    hypothesis_id: UUID
    question: Text
    source: AssumptionSource
    scope: Text
    support_condition: Text
    falsification_condition: Text
    check_by: datetime
    status: Literal["untested", "supported", "weakened", "falsified"]
    last_check_ref: ObjectRef | None


Dimension = Literal[
    "business_demand",
    "competition_durability",
    "financial_cash",
    "balance_sheet_allocation",
    "price_expectations",
    "downside_counter_case",
    "portfolio_alternatives",
]
DIMENSIONS: tuple[Dimension, ...] = (
    "business_demand",
    "competition_durability",
    "financial_cash",
    "balance_sheet_allocation",
    "price_expectations",
    "downside_counter_case",
    "portfolio_alternatives",
)


class DimensionAssessment(Contract):
    dimension: Dimension
    status: DimensionStatus
    applicable: bool
    reason: Text | None = None
    refs: tuple[ObjectRef, ...] = ()

    @model_validator(mode="after")
    def applicability_reason(self) -> "DimensionAssessment":
        if not self.applicable and (not self.reason or self.status == DimensionStatus.EVIDENCED):
            raise ValueError("not applicable requires reason and is not evidenced")
        return self


class ResearchAuthorization(Contract):
    background_enabled: bool = False
    exploration_scope_refs: tuple[ObjectRef, ...] = ()
    daily_budget: Quantity
    exploration_daily_budget: Quantity | None = None
    review_daily_budget: Quantity | None = None
    per_task_budget: Quantity
    max_leads_per_batch: Annotated[int, Field(ge=0)]
    max_new_tasks_per_batch: Annotated[int, Field(ge=0)]
    source_refs: tuple[ObjectRef, ...]


class PortfolioConstraints(Contract):
    """User-confirmed mandate limits for one explicit CNY account.

    These are not scenario targets. A saved authenticated mandate revision is
    the authority; free text is never parsed into these limits.
    """
    account_id: UUID
    currency: Literal["CNY"] = "CNY"
    max_single_weight: Annotated[DecimalValue, Field(ge=0, le=1, max_digits=40, decimal_places=18)] | None = None
    max_sector_weight: Annotated[DecimalValue, Field(ge=0, le=1, max_digits=40, decimal_places=18)] | None = None
    minimum_cash: Annotated[DecimalValue, Field(ge=0, max_digits=40, decimal_places=18)] | None = None
    minimum_cash_weight: Annotated[DecimalValue, Field(ge=0, le=1, max_digits=40, decimal_places=18)] | None = None


class Mandate(Record):
    goals: tuple[str, ...]
    scope_refs: tuple[ObjectRef, ...]
    hard_constraints: tuple[str, ...]
    portfolio_constraints: PortfolioConstraints | None = None
    unknowns: tuple[str, ...]
    authorization: ResearchAuthorization


class Case(Record):
    user_id: OwnerId
    listing_ref: ObjectRef
    status: CaseStatus
    episode: Annotated[int, Field(ge=1)]
    pinned: bool
    background_paused: bool = False
    user_archived: bool = False
    muted: bool = False
    current_thesis_ref: ObjectRef | None
    current_recommendation_ref: ObjectRef | None


class ResearchPlan(Record):
    episode_ref: ObjectRef
    question: Text
    completion_criteria: tuple[Text, ...]
    hypothesis_refs: tuple[ObjectRef, ...]
    evidence_steps: tuple[Text, ...]
    stop_conditions: tuple[Text, ...]
    budget_ref: ObjectRef
    progress: tuple[str, ...] = ()


class ThesisRevision(Record):
    case_ref: ObjectRef
    previous_ref: ObjectRef | None
    change_reason: Text
    question_scope: Text
    assessed_dimensions: tuple[DimensionAssessment, ...]
    claims: tuple[Claim, ...]
    assumptions: tuple[Hypothesis, ...]
    counter_case: tuple[ContentNode, ...]
    evidence_set_hash: Text
    method_ref: ObjectRef
    as_of: datetime


class Dependency(Contract):
    ref: ObjectRef
    materiality: Literal["critical", "contextual"]
    fields: tuple[Text, ...]


class ConditionalAction(Contract):
    action: CompanyAction
    condition_refs: tuple[ObjectRef, ...]
    rationale_refs: tuple[ObjectRef, ...]


class CompanyRecommendation(Record):
    scope: Literal["company_view"]
    case_ids: Annotated[tuple[UUID, ...], Field(min_length=1, max_length=1)]
    thesis_revision_refs: Annotated[tuple[ObjectRef, ...], Field(min_length=1)]
    as_of: datetime
    question_scope: Text
    assessed_dimensions: tuple[DimensionAssessment, ...]
    sufficiency: Sufficiency
    claims: tuple[Claim, ...]
    counter_case: tuple[ContentNode, ...]
    assumptions: tuple[Hypothesis, ...]
    gaps: tuple[str, ...]
    conditions: tuple[ObjectRef, ...]
    valid_until: datetime
    next_review_at: datetime
    monitor_refs: tuple[ObjectRef, ...]
    dependency_refs: tuple[Dependency, ...]
    # Objective company research does not require a personal investment profile.
    # Existing explicitly bound opinions retain their revision checks.
    mandate_revision_id: ObjectRef | None = None
    method_revision_id: ObjectRef
    provenance: tuple[ObjectRef, ...]
    business_view: BusinessView
    price_view: PriceView
    conditional_actions: tuple[ConditionalAction, ...]
    portfolio_snapshot_id: None = None
    position_proposal: None = None

    @model_validator(mode="after")
    def complete_dimensions_and_time(self) -> "CompanyRecommendation":
        names = [entry.dimension for entry in self.assessed_dimensions]
        if len(names) != len(DIMENSIONS) or set(names) != set(DIMENSIONS):
            raise ValueError("company view requires each of the seven dimensions exactly once")
        if self.valid_until <= self.as_of or self.next_review_at > self.valid_until:
            raise ValueError("review and validity dates are inconsistent")
        return self


class RecommendationView(Contract):
    recommendation: CompanyRecommendation
    status: RecommendationStatus
    effective_valid_until: datetime
    effective_next_review_at: datetime
    limitation_reasons: tuple[str, ...]
    replacement_ref: ObjectRef | None


class PublicationCheck(Contract):
    rule_version: Text
    passed: tuple[str, ...]
    failures: tuple[Text, ...]
    field_errors: dict[str, str]
    semantic_review_ref: ObjectRef | None


class UserDecision(Record):
    portfolio_plan_ref: ObjectRef | None = None
    option_id: UUID | None = None
    action_id: UUID | None = None
    recommendation_ref: ObjectRef | None
    case_ref: ObjectRef | None
    decision: DecisionKind
    user_text: Text
    reason: str | None
    decided_at: datetime

    @model_validator(mode="after")
    def target_required(self) -> "UserDecision":
        if self.portfolio_plan_ref is not None:
            if self.portfolio_plan_ref.kind != "portfolio_plan" or self.portfolio_plan_ref.field_path or self.option_id is None or self.recommendation_ref is not None or self.case_ref is not None:
                raise ValueError("portfolio decision needs exact plan/option and no company target")
        elif self.option_id is not None or self.action_id is not None:
            raise ValueError("option/action requires portfolio plan")
        elif self.recommendation_ref is None and self.case_ref is None:
            raise ValueError("decision needs a recommendation, case or portfolio option")
        return self


class WatchlistChange(Record):
    """Result of an authenticated watchlist add (not a versioned pool object)."""

    listing_ref: ObjectRef
    watchlist_id: int
    ticker: str
    already_on_watchlist: bool
    draft_state: str


class Memory(Record):
    kind: Literal["explicit_preference", "unverified_observation"]
    text: Text
    scope_refs: tuple[ObjectRef, ...]
    source_refs: Annotated[tuple[ObjectRef, ...], Field(min_length=1)]
    status: Literal["active", "revoked", "expired"]
    valid_until: datetime | None
    revoked_at: datetime | None
    supersedes_ref: ObjectRef | None

    @model_validator(mode="after")
    def observation_expires(self) -> "Memory":
        if self.kind == "unverified_observation" and self.valid_until is None:
            raise ValueError("observations require expiry")
        if (self.status == "revoked") != (self.revoked_at is not None):
            raise ValueError("revoked memory requires a revocation timestamp")
        return self


class Episode(Record):
    case_ref: ObjectRef
    number: Annotated[int, Field(ge=1)]
    question: Text
    plan_refs: tuple[ObjectRef, ...]
    artifact_refs: tuple[ObjectRef, ...]


class MonitorCondition(Contract):
    field_ref: ObjectRef
    operator: Literal["lt", "le", "eq", "ge", "gt", "changed"]
    threshold_ref: ObjectRef | None
    period_basis: Text


class MonitorSpec(Record):
    case_ref: ObjectRef
    hypothesis_ref: ObjectRef | None
    conditions: tuple[MonitorCondition, ...]
    next_due_at: datetime
    required: bool
    active: bool
    trigger: Literal["field", "calendar"] = "field"


class ReviewEvent(Record):
    trigger_refs: tuple[ObjectRef, ...]
    recommendation_ref: ObjectRef
    evidence_diff_refs: tuple[ObjectRef, ...]
    result: Literal["maintain", "revise", "withdraw", "inconclusive"]
    reasoning_revision: bool = False
    new_recommendation_ref: ObjectRef | None
    rationale: Text
    due_at: datetime
    started_at: datetime
    completed_at: datetime


class RecommendationEvent(Record):
    recommendation_ref: ObjectRef
    type: Literal["published", "review_required", "expired", "superseded", "withdrawn", "extended"]
    review_ref: ObjectRef | None
    checked_at: datetime
    checked_dependency_refs: tuple[ObjectRef, ...]
    next_review_at: datetime | None
    effective_valid_until: datetime | None
    replacement_ref: ObjectRef | None
