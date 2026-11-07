"""Closed bank-topic DTOs. Persistence, proof review and DSL evaluation are separate."""

from datetime import date, datetime
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BeforeValidator, Field, StrictBool, model_validator

from investment_agent.investor.common import (
    Contract,
    DecimalValue,
    ObjectRef,
    OwnerId,
    Revision,
    Text,
)

TEMPLATE_KEY: Literal["bank_nii_sustainability_v1"] = "bank_nii_sustainability_v1"
SCHEMA_VERSION = 1
SchemaVersion = Annotated[int, Field(strict=True, ge=1, le=1)]
Hash = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
StableId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,79}$")]
ReasonCode = Annotated[str, Field(pattern=r"^[a-z0-9_.:-]{1,160}$")]
ListingId = Annotated[int, Field(strict=True, ge=1)]
FactMetric = Literal["nii", "nim", "nii_volume_effect", "nii_rate_effect", "nii_change_disclosed"]
ConditionMetric = Literal[
    "nii_yoy", "nim_delta_bp", "nii_volume_effect", "nii_rate_effect", "nii_scale_covers_rate",
]
Coverage = Literal["complete", "partial", "unavailable"]
CheckOutcome = Literal[
    "met", "not_met", "insufficient_evidence", "not_comparable", "source_invalidated",
]
RunState = Literal["queued", "running", "completed", "partial", "failed", "cancelled"]
ConditionState = Literal["draft", "armed", "paused", "cancelled", "completed", "expired"]


class TopicRef(Contract):
    """Domain-only reference until the four kinds are integrated into ObjectRef."""

    kind: Literal["topic_case", "topic_revision", "topic_condition", "topic_check"]
    id: UUID
    revision: Revision
    field_path: Annotated[str, Field(pattern=r"^facts\.[a-z][a-z0-9_]{0,79}$")] | None = None

    @model_validator(mode="after")
    def immutable_identity(self) -> Self:
        if self.kind in {"topic_revision", "topic_check"} and self.revision != 1:
            raise ValueError("immutable topic artifacts use an independent UUID and revision=1")
        if self.field_path is not None and self.kind != "topic_revision":
            raise ValueError("only a topic revision can address a stable fact field")
        return self


class CaseRef(TopicRef):
    kind: Literal["topic_case"] = "topic_case"


class TopicRevisionRef(TopicRef):
    kind: Literal["topic_revision"] = "topic_revision"
    field_path: None = None


class ConditionRef(TopicRef):
    kind: Literal["topic_condition"] = "topic_condition"


class CheckRef(TopicRef):
    kind: Literal["topic_check"] = "topic_check"


def _exact_reference(value: object) -> object:
    # Keep these restrictions when integration later adds the kinds to ObjectRef.
    raw = value.model_dump() if isinstance(value, ObjectRef) else value
    if isinstance(raw, dict) and raw.get("kind") in {
        "topic_case", "topic_revision", "topic_condition", "topic_check",
    }:
        return TopicRef.model_validate(raw)
    return value


ExactRef = Annotated[ObjectRef | TopicRef, BeforeValidator(_exact_reference)]


class AnnualPeriod(Contract):
    start: date
    end: date
    basis: Literal["period_to_date/annual"] = "period_to_date/annual"

    @model_validator(mode="after")
    def calendar_annual(self) -> Self:
        if self.start != date(self.end.year, 1, 1) or self.end != date(self.end.year, 12, 31):
            raise ValueError("this template requires a complete calendar annual period")
        return self


class TopicScope(Contract):
    reporting_scope: Literal["consolidated"] = "consolidated"
    accounting_standard: Literal["CAS"] = "CAS"
    currency: Literal["CNY"] = "CNY"


class ListingSubject(Contract):
    kind: Literal["listing"] = "listing"
    listing_id: ListingId


class CreateTopicRequest(TopicScope):
    schema_version: SchemaVersion = 1
    idempotency_key: UUID
    template_key: Literal["bank_nii_sustainability_v1"] = TEMPLATE_KEY
    ticker: Literal["600036.SH"] = "600036.SH"
    period: AnnualPeriod
    baseline_period: AnnualPeriod

    @model_validator(mode="after")
    def fixed_first_case(self) -> Self:
        if self.period.end.year != 2025 or self.baseline_period.end.year != 2024:
            raise ValueError("the initial topic is CMB FY2025 versus its FY2024 comparison")
        return self


class Gap(Contract):
    code: ReasonCode
    field_path: Text


class CreateTopicReceipt(Contract):
    schema_version: SchemaVersion = 1
    case_ref: CaseRef
    head_ref: TopicRevisionRef
    state: Literal["draft", "ready", "archived"]
    coverage: Coverage
    gaps: tuple[Gap, ...]
    control_revision: Revision


class TopicCase(Contract):
    schema_version: SchemaVersion = 1
    id: UUID
    owner_id: OwnerId
    listing_id: ListingId
    template_key: Literal["bank_nii_sustainability_v1"] = TEMPLATE_KEY
    base_period: AnnualPeriod
    head_revision: Revision
    control_revision: Revision
    state: Literal["draft", "ready", "archived"]
    created_at: datetime


class SourceTiming(Contract):
    published_at: datetime | None
    published_date: date | None
    published_precision: Literal["timestamp", "date", "unknown"]
    acquired_at: datetime | None
    checked_at: datetime | None
    review_actor: Text | None

    @model_validator(mode="after")
    def honest_precision(self) -> Self:
        if self.published_precision == "timestamp":
            if self.published_at is None or self.published_date is not None:
                raise ValueError("timestamp precision requires only the known public instant")
        elif self.published_precision == "date":
            if self.published_date is None or self.published_at is not None:
                raise ValueError("date precision must not invent a public instant")
        elif self.published_date is not None or self.published_at is not None:
            raise ValueError("unknown publication cannot carry a guessed date")
        if (self.checked_at is None) != (self.review_actor is None):
            raise ValueError("a source check needs both actor and timestamp")
        return self


class FactProof(Contract):
    original_ref: ObjectRef
    pdf_sha256: Hash
    pdf_page: Annotated[int, Field(strict=True, ge=1)]
    printed_page: Text
    table_title: Text
    row_path: Text
    column_path: Text
    excerpt: Text
    excerpt_hash: Hash
    rule_version: Text

    @model_validator(mode="after")
    def original_kind(self) -> Self:
        if self.original_ref.kind != "disclosure_original":
            raise ValueError("a bank proof must bind a stored original")
        return self


class TopicFactRevision(TopicScope):
    schema_version: SchemaVersion = 1
    id: UUID
    owner_id: OwnerId
    case_id: UUID
    stable_fact_id: StableId
    revision: Revision
    metric: FactMetric
    value: DecimalValue | None
    unit: Literal["CNY", "fraction"]
    raw_value: Text | None
    raw_unit: Literal["CNY_million", "percent"] | None
    period: AnnualPeriod
    comparison_period: AnnualPeriod | None
    source_refs: Annotated[tuple[ObjectRef, ...], Field(max_length=100)]
    proof: FactProof | None
    verification: Literal["candidate", "pending_review", "verified", "scope_conflict", "source_unavailable"]
    unknown_reason: ReasonCode | None
    timing: SourceTiming

    @model_validator(mode="after")
    def quantity_semantics(self) -> Self:
        if (self.value is None) != (self.unknown_reason is not None):
            raise ValueError("null facts need an explicit reason; zero is a known value")
        expected_unit = "fraction" if self.metric == "nim" else "CNY"
        expected_raw = "percent" if self.metric == "nim" else "CNY_million"
        if self.unit != expected_unit or self.raw_unit not in {None, expected_raw}:
            raise ValueError("fact metric and units disagree")
        if self.verification == "verified" and (
            self.proof is None or self.timing.checked_at is None or self.value is None
            or self.raw_value is None or self.raw_unit is None or self.timing.acquired_at is None
        ):
            raise ValueError("verified facts require a proof, checked time and known value")
        return self


class TopicFactRowRef(Contract):
    """Internal row identity, not an ObjectRef or a new externally resolvable kind."""

    id: UUID
    revision: Revision


class ExistingFactSource(Contract):
    binding_kind: Literal["existing_fact"]
    source_ref: ObjectRef

    @model_validator(mode="after")
    def existing_fact_namespace(self) -> Self:
        ref = self.source_ref
        if ref.kind == "peer_annual_snapshot":
            import re

            if ref.revision != 1 or ref.field_path is None or not re.fullmatch(r"fields\.[a-z][a-z0-9_]{0,79}", ref.field_path):
                raise ValueError("peer facts need immutable snapshot revision=1 and an exact fields.<field>")
        elif ref.kind == "evidence":
            if ref.field_path not in {None, "value"}:
                raise ValueError("numeric evidence cannot bind a text/explanation field")
        else:
            raise ValueError("existing facts only bind peer annual fields or numeric evidence")
        return self


class TopicFactSource(Contract):
    binding_kind: Literal["topic_fact"]
    fact_ref: TopicFactRowRef


class FactBinding(Contract):
    stable_fact_id: StableId
    metric: FactMetric
    source: Annotated[ExistingFactSource | TopicFactSource, Field(discriminator="binding_kind")]


class SourcedQuantity(Contract):
    value: DecimalValue | None
    unit: Literal["CNY", "fraction", "bp"]
    unknown_reason: ReasonCode | None
    source_fact_ids: Annotated[tuple[StableId, ...], Field(max_length=20)]

    @model_validator(mode="after")
    def known_has_frozen_inputs(self) -> Self:
        if (self.value is None) != (self.unknown_reason is not None):
            raise ValueError("null quantities need a reason; zero cannot stand for missing")
        if self.value is not None and not self.source_fact_ids:
            raise ValueError("known decomposition quantities name their frozen parent fact bindings")
        if len(set(self.source_fact_ids)) != len(self.source_fact_ids):
            raise ValueError("decomposition source IDs are unique")
        return self


class AmountQuantity(SourcedQuantity):
    unit: Literal["CNY"] = "CNY"


class FractionQuantity(SourcedQuantity):
    unit: Literal["fraction"] = "fraction"


class BasisPointQuantity(SourcedQuantity):
    unit: Literal["bp"] = "bp"


class Decomposition(Contract):
    method: Literal["disclosed_volume_rate"] = "disclosed_volume_rate"
    coverage: Coverage
    nii_current: AmountQuantity
    nii_baseline: AmountQuantity
    nim_current: FractionQuantity
    nim_baseline: FractionQuantity
    volume_effect: AmountQuantity
    rate_effect: AmountQuantity
    disclosed_change: AmountQuantity
    delta_nii: AmountQuantity
    nii_yoy: FractionQuantity
    delta_nim_bp: BasisPointQuantity
    residual: AmountQuantity
    tolerance: AmountQuantity
    rounding_consistent: StrictBool | None

    @model_validator(mode="after")
    def complete_requires_disclosed_inputs(self) -> Self:
        inputs = (self.nii_current, self.nii_baseline, self.nim_current, self.nim_baseline,
                  self.volume_effect, self.rate_effect, self.disclosed_change)
        if self.coverage == "complete" and any(item.value is None for item in inputs):
            raise ValueError("missing disclosed inputs cannot be a complete decomposition")
        no_effects = self.volume_effect.value is None and self.rate_effect.value is None
        if no_effects != (self.coverage == "unavailable"):
            raise ValueError("without a disclosed volume/rate table decomposition is unavailable")
        if self.rounding_consistent is not None and (
            self.residual.value is None or self.tolerance.value is None
            or any(item.value is None for item in inputs)
        ):
            raise ValueError("rounding status requires complete disclosed inputs and residual bounds")
        return self


class TopicExplanation(Contract):
    stable_id: StableId
    revision: Revision
    level: Literal["L1", "L2", "L3", "L4"]
    statement: Text
    support_refs: Annotated[tuple[ExactRef, ...], Field(max_length=100)]
    counter_refs: Annotated[tuple[ExactRef, ...], Field(max_length=100)]
    missing_evidence: tuple[Text, ...]
    review_state: Literal["unreviewed", "pending_review", "reviewed", "rejected"]
    reviewer: Text | None
    reviewed_at: datetime | None

    @model_validator(mode="after")
    def review_identity(self) -> Self:
        if (self.reviewer is None) != (self.reviewed_at is None):
            raise ValueError("reviewer and time must be present together")
        if self.review_state in {"reviewed", "rejected"} and self.reviewer is None:
            raise ValueError("a reviewed explanation needs its review identity")
        if not self.counter_refs and not self.missing_evidence:
            raise ValueError("missing counter-evidence must remain an explicit gap")
        return self


class TopicRevision(TopicScope):
    schema_version: SchemaVersion = 1
    id: UUID
    case_id: UUID
    revision: Revision
    question: Text
    period: AnnualPeriod
    baseline_period: AnnualPeriod
    as_of: datetime
    fact_bindings: Annotated[tuple[FactBinding, ...], Field(max_length=100)]
    decomposition: Decomposition | None
    explanations: Annotated[tuple[TopicExplanation, ...], Field(max_length=100)]
    condition_refs: Annotated[tuple[ConditionRef, ...], Field(max_length=100)]
    content_hash: Hash
    supersedes_ref: TopicRevisionRef | None

    @model_validator(mode="after")
    def stable_bindings_are_unique(self) -> Self:
        ids = [binding.stable_fact_id for binding in self.fact_bindings]
        if len(set(ids)) != len(ids):
            raise ValueError("each revision binds a stable fact ID only once")
        if self.decomposition is not None:
            for field in type(self.decomposition).model_fields:
                quantity = getattr(self.decomposition, field)
                if isinstance(quantity, SourcedQuantity) and not set(quantity.source_fact_ids) <= set(ids):
                    raise ValueError("decomposition sources must exist in this frozen parent revision")
        return self


class NumericPredicate(Contract):
    metric: Literal["nii_yoy", "nim_delta_bp", "nii_volume_effect", "nii_rate_effect"]
    operator: Literal["gt", "gte", "lt", "lte"]
    threshold: DecimalValue
    unit: Literal["fraction", "bp", "CNY"]

    @model_validator(mode="after")
    def metric_unit(self) -> Self:
        expected = {"nii_yoy": "fraction", "nim_delta_bp": "bp"}.get(self.metric, "CNY")
        if self.unit != expected:
            raise ValueError("condition metric and threshold unit disagree")
        return self


class BooleanPredicate(Contract):
    metric: Literal["nii_scale_covers_rate"]
    operator: Literal["eq"]
    threshold: StrictBool
    unit: Literal["boolean"]


PredicateLeaf = NumericPredicate | BooleanPredicate


class ConditionPredicate(Contract):
    kind: Literal["all", "any"]
    conditions: Annotated[tuple[PredicateLeaf, ...], Field(min_length=1, max_length=5)]


class CreateConditionRequest(Contract):
    schema_version: SchemaVersion = 1
    idempotency_key: UUID
    expected_revision: Revision
    topic_revision_ref: TopicRevisionRef
    activate: StrictBool
    dsl_version: Literal["bank-condition/1"] = "bank-condition/1"
    predicate: ConditionPredicate
    target_selector: Literal["next_comparable_annual"] = "next_comparable_annual"
    review_due_at: datetime
    expires_at: datetime
    timezone: Literal["Asia/Shanghai"] = "Asia/Shanghai"

    @model_validator(mode="after")
    def ordered_window(self) -> Self:
        if self.review_due_at >= self.expires_at:
            raise ValueError("expiry must follow the planned review due time")
        if self.topic_revision_ref.field_path is not None:
            raise ValueError("conditions bind a complete topic revision")
        return self


class ConditionRevision(Contract):
    schema_version: SchemaVersion = 1
    condition_ref: ConditionRef
    owner_id: OwnerId
    case_id: UUID
    topic_revision_ref: TopicRevisionRef
    dsl_version: Literal["bank-condition/1"]
    predicate: ConditionPredicate
    dependency_manifest: Annotated[tuple[FactBinding, ...], Field(max_length=100)]
    target_selector: Literal["next_comparable_annual"]
    review_due_at: datetime
    expires_at: datetime
    timezone: Literal["Asia/Shanghai"]
    activation_basis: Literal["explicit_user_enable"] | None
    content_hash: Hash

    @model_validator(mode="after")
    def ordered_window(self) -> Self:
        if self.review_due_at >= self.expires_at:
            raise ValueError("expiry must follow due time")
        return self


class ConditionHead(Contract):
    condition_id: UUID
    head_revision: Revision
    control_revision: Revision
    state: ConditionState
    next_due_at: datetime | None
    active_run_ref: ObjectRef | None
    last_check_ref: CheckRef | None

    @model_validator(mode="after")
    def run_reference(self) -> Self:
        if self.active_run_ref is not None and self.active_run_ref.kind != "pipeline_run":
            raise ValueError("an active run is a pipeline_run, not a check outcome")
        return self


class UnknownReason(Contract):
    metric: ConditionMetric
    code: ReasonCode


class CheckValues(Contract):
    nii_yoy: DecimalValue | None = None
    nim_delta_bp: DecimalValue | None = None
    nii_volume_effect: DecimalValue | None = None
    nii_rate_effect: DecimalValue | None = None
    nii_scale_covers_rate: StrictBool | None = None


class LeafResult(Contract):
    metric: ConditionMetric
    status: Literal["true", "false", "unknown", "not_applicable"]
    value: DecimalValue | StrictBool | None
    unknown_reason: ReasonCode | None

    @model_validator(mode="after")
    def unknown_leaf(self) -> Self:
        unknown = self.status in {"unknown", "not_applicable"}
        if unknown != (self.value is None) or unknown != (self.unknown_reason is not None):
            raise ValueError("unknown/not_applicable leaves carry null with a reason")
        if self.status == "not_applicable" and (
            self.metric != "nii_scale_covers_rate" or self.unknown_reason != "no_rate_drag"
        ):
            raise ValueError("not_applicable is limited to the no-rate-drag leaf")
        if self.value is not None and isinstance(self.value, bool) != (self.metric == "nii_scale_covers_rate"):
            raise ValueError("leaf value type must match the metric")
        return self


class SemanticInputs(Contract):
    """No scan clock here: the semantic hash includes the due phase instead."""

    schema_version: SchemaVersion = 1
    subject: ListingSubject
    topic_revision_ref: TopicRevisionRef
    condition_ref: ConditionRef
    evaluator_version: Text
    target_period: AnnualPeriod
    baseline_period: AnnualPeriod
    scope: TopicScope
    exact_source_refs: Annotated[tuple[ExactRef, ...], Field(max_length=100)]
    values: CheckValues
    availability: Literal["available", "awaiting_source", "insufficient_evidence", "source_conflict", "source_invalidated"]
    due_phase: Literal["pre_due", "due", "expired"]


class ExecutionManifest(Contract):
    schema_version: SchemaVersion = 1
    semantic_inputs: SemanticInputs
    as_of: datetime


class CheckResult(Contract):
    schema_version: SchemaVersion = 1
    check_ref: CheckRef
    condition_ref: ConditionRef
    outcome: CheckOutcome
    coverage: Coverage
    values: CheckValues
    unknown_reasons: tuple[UnknownReason, ...]
    as_of: datetime
    execution_manifest_hash: Hash
    semantic_input_hash: Hash


class CheckRunView(Contract):
    """Domain display of a pipeline job, never a financial predicate outcome."""

    run_ref: ObjectRef
    state: RunState
    check_ref: CheckRef | None
    reason: ReasonCode | None
    retryable: StrictBool

    @model_validator(mode="after")
    def cancelled_has_no_result(self) -> Self:
        if self.run_ref.kind != "pipeline_run":
            raise ValueError("a run view requires the existing pipeline_run identity")
        if self.state == "cancelled" and (
            self.reason != "cancelled" or self.retryable or self.check_ref is not None
        ):
            raise ValueError("cancellation is non-retryable and does not fabricate a check")
        return self


class CheckRevision(Contract):
    schema_version: SchemaVersion = 1
    owner_id: OwnerId
    subject: ListingSubject
    result: CheckResult
    execution_manifest: ExecutionManifest
    prior_check_ref: CheckRef | None
    supersedes_ref: CheckRef | None
    trigger: Literal["manual", "due", "new_local_source", "correction", "late_correction"]
    created_at: datetime

    @model_validator(mode="after")
    def frozen_identities(self) -> Self:
        inputs = self.execution_manifest.semantic_inputs
        if self.subject != inputs.subject or self.result.condition_ref != inputs.condition_ref:
            raise ValueError("check subject and condition must equal frozen inputs")
        if self.result.as_of != self.execution_manifest.as_of:
            raise ValueError("the recorded check must retain its execution time")
        return self


class TopicObservationPayload(Contract):
    """Payload shape only; no producer registration or source authority is granted."""

    subject: ListingSubject
    topic_ref: TopicRevisionRef
    condition_ref: ConditionRef
    check_ref: CheckRef
    prior_check_ref: CheckRef | None
    target_period: AnnualPeriod
    baseline_period: AnnualPeriod
    scope: Literal["consolidated"]
    scope_changed: StrictBool
    outcome: CheckOutcome
    changed_reasons: Annotated[tuple[ReasonCode, ...], Field(min_length=1, max_length=100)]
    exact_source_refs: Annotated[tuple[ExactRef, ...], Field(max_length=100)]

    @model_validator(mode="after")
    def whole_revision(self) -> Self:
        if self.topic_ref.field_path is not None:
            raise ValueError("observation payload binds the complete topic revision")
        return self
