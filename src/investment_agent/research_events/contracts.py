"""Closed routing contracts; persistent resolvers separately authorize publishing.

No domain module imports are required, including for optional future sources.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, ClassVar, Literal, Self
from uuid import UUID

from pydantic import Field, StrictBool, TypeAdapter, field_validator, model_validator

from investment_agent.investor.common import Contract, ObjectRef, OwnerId, Revision

PositiveInt = Annotated[int, Field(strict=True, ge=1)]
NonnegativeInt = Annotated[int, Field(strict=True, ge=0)]
RoutingText = Annotated[str, Field(min_length=1, max_length=2000)]
Code = Annotated[str, Field(min_length=1, max_length=160, pattern=r"^[a-z0-9_.:-]+$")]
Hash = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
type SetArray[T] = Annotated[tuple[T, ...], Field(max_length=100, json_schema_extra={"canonical_set": True})]


class ListingSubject(Contract):
    kind: Literal["listing"]
    listing_id: PositiveInt


class AccountSubject(Contract):
    kind: Literal["account"]
    account_id: UUID


type Subject = Annotated[ListingSubject | AccountSubject, Field(discriminator="kind")]


class IntegerSourceRef(Contract):
    kind: Literal["review_round", "review_comparison", "research_change_set"]
    id: PositiveInt
    revision: Revision

    @field_validator("revision")
    @classmethod
    def immutable_revision(cls, value: int) -> int:
        if value != 1:
            raise ValueError("immutable legacy event sources use revision 1")
        return value


class UUIDSourceRef(Contract):
    kind: Literal["review_cycle", "case", "recommendation_event", "evidence",
                  "topic_check", "decision_observation", "calculation"]
    id: UUID
    revision: Revision

    @model_validator(mode="after")
    def immutable_revision(self) -> Self:
        if self.kind not in {"review_cycle", "case", "decision_observation"} and self.revision != 1:
            raise ValueError("immutable event source uses revision 1")
        return self


type EventSourceRef = Annotated[IntegerSourceRef | UUIDSourceRef, Field(discriminator="kind")]
SOURCE_REF_ADAPTER: TypeAdapter[EventSourceRef] = TypeAdapter(EventSourceRef)


class PlannedObjectRef(Contract):
    """Future domain addresses, deliberately independent of formal ObjectRef."""

    kind: Literal["topic_case", "topic_revision", "topic_condition", "topic_check",
                  "portfolio_stress", "decision_observation"]
    id: UUID
    revision: Revision
    field_path: RoutingText | None = None

    @model_validator(mode="after")
    def immutable_revision(self) -> Self:
        if self.kind in {"topic_revision", "topic_check"} and self.revision != 1:
            raise ValueError("immutable domain row uses reference revision 1")
        return self


class LegacyUUIDRef(Contract):
    kind: Literal["review_cycle", "recommendation_event"]
    id: UUID
    revision: Revision


type ObjectReference = ObjectRef | PlannedObjectRef
type ExactRef = ObjectRef | PlannedObjectRef | IntegerSourceRef | LegacyUUIDRef


class Period(Contract):
    start: date
    end: date
    basis: RoutingText

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.start > self.end:
            raise ValueError("period starts after its end")
        return self


class PostprocessEligibility(Contract):
    watchlist_sync: StrictBool
    review_admission: StrictBool
    accepted_at: datetime
    listing_id: PositiveInt
    settings_revision: NonnegativeInt | None
    settings_snapshot_hash: Hash


class RoutingPayload(Contract):
    # Each named reference field has an explicit per-payload kind requirement.
    _ref_kinds: ClassVar[dict[str, str]] = {}
    canonical_set_fields: ClassVar[frozenset[str]] = frozenset({
        "invalidated_refs", "exact_source_refs", "notify_reasons", "quality",
        "changed_reasons", "reason_codes",
    })

    @model_validator(mode="after")
    def reference_kinds(self) -> Self:
        for name, kind in self._ref_kinds.items():
            value = getattr(self, name)
            if value is not None and value.kind != kind:
                raise ValueError(f"{name} requires kind {kind}")
        return self


class HistoricalPayload(RoutingPayload):
    backfill: StrictBool = False
    manifest_hash: Hash | None = None

    @model_validator(mode="after")
    def manifest_required(self) -> Self:
        if self.backfill != (self.manifest_hash is not None):
            raise ValueError("backfill requires its manifest hash; live events have none")
        return self


class ResearchChangePayload(HistoricalPayload):
    subject: ListingSubject
    run_ref: ObjectRef
    current_snapshot_id: PositiveInt
    previous_snapshot_id: PositiveInt | None
    baseline: StrictBool
    has_meaningful_changes: StrictBool
    source_changed: StrictBool
    postprocess_eligibility: PostprocessEligibility | None
    _ref_kinds = {"run_ref": "pipeline_run"}

    @model_validator(mode="after")
    def eligibility_scope(self) -> Self:
        if self.postprocess_eligibility and self.postprocess_eligibility.listing_id != self.subject.listing_id:
            raise ValueError("postprocessing eligibility belongs to another listing")
        return self


class ReviewPanelPayload(HistoricalPayload):
    subject: ListingSubject
    comparison_ref: IntegerSourceRef
    algorithm_version: RoutingText
    notify_reasons: Annotated[SetArray[Literal["high_divergence", "stance_flip"]], Field(max_length=2)]
    _ref_kinds = {"comparison_ref": "review_comparison"}


class ReviewCyclePayload(HistoricalPayload):
    subject: ListingSubject
    case_ref: ObjectRef
    case_episode: PositiveInt
    phase: Literal["merged", "dispatched", "dirty", "settled", "control", "migration_snapshot"]
    state: Literal["pending", "blocked", "running", "completed", "stopped"]
    task_ref: ObjectRef | None
    reason_codes: SetArray[Code]
    exact_source_refs: SetArray[ExactRef]
    _ref_kinds = {"case_ref": "case", "task_ref": "task"}


class RecommendationPayload(HistoricalPayload):
    subject: ListingSubject
    recommendation_ref: ObjectRef
    case_ref: ObjectRef
    validity: Literal["published", "review_required", "expired", "superseded", "withdrawn", "extended"]
    replacement_ref: ObjectRef | None
    review_ref: ObjectRef | None
    exact_source_refs: SetArray[ExactRef]
    _ref_kinds = {"recommendation_ref": "recommendation", "case_ref": "case",
                  "replacement_ref": "recommendation", "review_ref": "review"}


class CaseControlPayload(HistoricalPayload):
    subject: ListingSubject
    case_ref: ObjectRef
    case_episode: PositiveInt
    action: Literal["archive", "reopen", "pause", "resume", "mute", "unmute"]
    _ref_kinds = {"case_ref": "case"}


class SourceInvalidatedPayload(RoutingPayload):
    subject: ListingSubject
    reason: Literal["source_corrected"]
    invalidated_refs: Annotated[SetArray[ObjectRef], Field(min_length=1)]
    replacement_refs: Annotated[tuple[ObjectRef, ...], Field(min_length=1, max_length=1)]

    @model_validator(mode="after")
    def evidence_only(self) -> Self:
        for value in (*self.invalidated_refs, *self.replacement_refs):
            if value.kind != "evidence" or value.field_path is not None or value.revision != 1:
                raise ValueError("numeric corrections require whole evidence rows at revision 1")
        if self.replacement_refs[0] in self.invalidated_refs:
            raise ValueError("a replacement cannot invalidate itself")
        return self


class TopicPayload(RoutingPayload):
    subject: ListingSubject
    topic_ref: PlannedObjectRef
    condition_ref: PlannedObjectRef
    check_ref: PlannedObjectRef
    prior_check_ref: PlannedObjectRef | None
    target_period: Period
    baseline_period: Period
    scope: RoutingText
    scope_changed: StrictBool
    outcome: Literal["met", "not_met", "insufficient_evidence", "not_comparable", "source_invalidated"]
    changed_reasons: Annotated[SetArray[Code], Field(min_length=1)]
    exact_source_refs: SetArray[ExactRef]
    _ref_kinds = {"topic_ref": "topic_revision", "condition_ref": "topic_condition",
                  "check_ref": "topic_check", "prior_check_ref": "topic_check"}

    @model_validator(mode="after")
    def distinct_previous(self) -> Self:
        if self.check_ref == self.prior_check_ref:
            raise ValueError("prior check must be a different row")
        return self


class AccountPayload(RoutingPayload):
    subject: AccountSubject
    version_refs: Annotated[tuple[ExactRef, ...], Field(min_length=1, max_length=100)]
    quality: SetArray[Code]
    safe_summary: Annotated[str, Field(min_length=1, max_length=300)]
    detail_url: Annotated[str, Field(pattern=r"^/(?:app|portfolio|investor)/[^?#]*$", max_length=500)]

    @field_validator("safe_summary", "detail_url")
    @classmethod
    def safe_text(cls, value: str) -> str:
        if any(char in value for char in ("<", ">", "\x00", "\n", "\r")):
            raise ValueError("routing display values must be plain text")
        return value

    @field_validator("detail_url")
    @classmethod
    def local_path(cls, value: str) -> str:
        if "\\" in value or "%" in value or any(part in {".", ".."} for part in value.split("/")):
            raise ValueError("detail_url must be a literal product-local path")
        return value


class StressPayload(AccountPayload):
    study_ref: PlannedObjectRef
    calculation_ref: ObjectRef
    _ref_kinds = {"study_ref": "portfolio_stress", "calculation_ref": "calculation"}


class DecisionDuePayload(AccountPayload):
    observation_ref: PlannedObjectRef
    decision_ref: ObjectRef
    registered_at: datetime
    due_at: datetime
    not_before: datetime
    _ref_kinds = {"observation_ref": "decision_observation", "decision_ref": "decision"}

    @model_validator(mode="after")
    def ordered_times(self) -> Self:
        if not self.registered_at < self.due_at <= self.not_before:
            raise ValueError("observation times must be registered < due <= not_before")
        return self


class DecisionCheckPayload(AccountPayload):
    observation_ref: PlannedObjectRef
    decision_ref: ObjectRef
    calculation_ref: ObjectRef
    previous_check_ref: ObjectRef | None
    registered_at: datetime
    due_at: datetime
    outcome: Literal["met", "not_met", "unknown", "mixed"]
    _ref_kinds = {"observation_ref": "decision_observation", "decision_ref": "decision",
                  "calculation_ref": "calculation", "previous_check_ref": "calculation"}

    @model_validator(mode="after")
    def previous_and_window(self) -> Self:
        if self.registered_at >= self.due_at or self.previous_check_ref == self.calculation_ref:
            raise ValueError("invalid check window or self predecessor")
        return self


class DecisionSupersededPayload(DecisionCheckPayload):
    superseded_ref: ObjectRef
    _ref_kinds = {**DecisionCheckPayload._ref_kinds, "superseded_ref": "calculation"}

    @model_validator(mode="after")
    def distinct_superseded(self) -> Self:
        if self.superseded_ref == self.calculation_ref:
            raise ValueError("a check cannot supersede itself")
        return self


type EventType = Literal[
    "research_change.recorded", "review_panel.finalized", "review_cycle.changed",
    "recommendation.validity_changed", "case.control_changed", "source.invalidated",
    "topic.observation_changed", "portfolio_stress.completed", "portfolio_stress.partial",
    "decision_observation.due", "decision_fact_check.completed", "decision_fact_check.partial",
    "decision_fact_check.superseded",
]
type Payload = (ResearchChangePayload | ReviewPanelPayload | ReviewCyclePayload |
                RecommendationPayload | CaseControlPayload | SourceInvalidatedPayload |
                TopicPayload | StressPayload | DecisionDuePayload | DecisionCheckPayload |
                DecisionSupersededPayload)

# Shape support is NOT a source registration or a publication allowlist.
EVENT_CONTRACTS: dict[str, tuple[str, type[RoutingPayload], str | None]] = {
    "research_change.recorded": ("research_change_set", ResearchChangePayload, None),
    "review_panel.finalized": ("review_round", ReviewPanelPayload, None),
    "review_cycle.changed": ("review_cycle", ReviewCyclePayload, None),
    "recommendation.validity_changed": ("recommendation_event", RecommendationPayload, None),
    "case.control_changed": ("case", CaseControlPayload, "case_ref"),
    "source.invalidated": ("evidence", SourceInvalidatedPayload, None),
    "topic.observation_changed": ("topic_check", TopicPayload, "check_ref"),
    "portfolio_stress.completed": ("calculation", StressPayload, "calculation_ref"),
    "portfolio_stress.partial": ("calculation", StressPayload, "calculation_ref"),
    "decision_observation.due": ("decision_observation", DecisionDuePayload, "observation_ref"),
    "decision_fact_check.completed": ("calculation", DecisionCheckPayload, "calculation_ref"),
    "decision_fact_check.partial": ("calculation", DecisionCheckPayload, "calculation_ref"),
    "decision_fact_check.superseded": ("calculation", DecisionSupersededPayload, "calculation_ref"),
}


class EventEnvelope(Contract):
    event_id: UUID
    event_type: EventType
    owner_id: OwnerId
    source_ref: EventSourceRef
    source_revision: Revision
    occurred_at: datetime
    recorded_at: datetime
    schema_version: Literal[1]
    payload_hash: Hash
    payload: Payload

    @field_validator("schema_version", mode="before")
    @classmethod
    def strict_schema_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("schema version must be a strict integer")
        return value

    @model_validator(mode="before")
    @classmethod
    def select_payload(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        event_type = data.get("event_type")
        if not isinstance(event_type, str) or event_type not in EVENT_CONTRACTS:
            raise ValueError("unknown event type")
        payload_type = EVENT_CONTRACTS[event_type][1]
        payload = data.get("payload")
        if isinstance(payload, RoutingPayload) and type(payload) is not payload_type:
            raise ValueError("payload class does not match event type")
        data["payload"] = payload_type.model_validate(payload)
        return data

    @model_validator(mode="after")
    def consistent(self) -> Self:
        from investment_agent.research_events import canonical

        kind, payload_type, mirrored = EVENT_CONTRACTS[self.event_type]
        if type(self.payload) is not payload_type or self.source_ref.kind != kind:
            raise ValueError("event type/source/payload mismatch")
        if self.source_revision != self.source_ref.revision:
            raise ValueError("source revisions disagree")
        if mirrored:
            value = getattr(self.payload, mirrored)
            if (value.kind, value.id, value.revision) != (self.source_ref.kind, self.source_ref.id, self.source_revision):
                raise ValueError("payload result reference disagrees with source")
            if value.field_path is not None:
                raise ValueError("mirrored source must be a whole row")
        if isinstance(self.payload, SourceInvalidatedPayload) and self.payload.replacement_refs[0].id != self.source_ref.id:
            raise ValueError("replacement must equal the event source")
        if isinstance(self.payload, DecisionDuePayload) and self.occurred_at != self.payload.due_at:
            raise ValueError("due event occurred_at must equal due_at")
        if isinstance(self.payload, StressPayload) and (self.event_type.endswith(".partial")) != bool(self.payload.quality):
            raise ValueError("stress completion quality disagrees with event type")
        if self.payload_hash != canonical.payload_hash(self.payload):
            raise ValueError("payload hash mismatch")
        if self.event_id != canonical.event_id(self.owner_id, self.event_type, self.source_ref, self.source_revision):
            raise ValueError("event ID mismatch")
        return self


class StoredEvent(EventEnvelope):
    sequence: PositiveInt


class InboxRebuildState(Contract):
    candidate_generation: UUID
    domain_watermark: NonnegativeInt
    journal_watermark: NonnegativeInt
    last_replayed_sequence: NonnegativeInt
    state: Literal["building", "catching_up", "ready", "blocked"]

    @model_validator(mode="after")
    def watermark_bound(self) -> Self:
        if self.last_replayed_sequence > self.journal_watermark:
            raise ValueError("replay cannot exceed journal watermark")
        return self


type ReceiptState = Literal["applied", "ignored", "retry", "blocked_predecessor", "quarantined"]
