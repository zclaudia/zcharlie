"""Pure typed command bodies shared by HTTP and transaction services."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, model_validator

from investment_agent.investor.common import Contract, ObjectRef, Text, utc_timestamp
from investment_agent.investor.contracts import Dimension
from investment_agent.supplemental_rules.contracts import (
    Context,
    Definition,
    Effect,
    EvaluationAdmission,
    EvaluationRequest,
    EventImpactDefinition,
    MutationReceipt,
    ResearchEvent,
    RuleDefinition,
    Selector,
    Severity,
)


class RuleContent(Contract):
    """User-editable rule fields; identity and hashes are server-owned."""

    schema_version: Literal[1] = 1
    name: Text
    rationale: Text
    source_refs: tuple[ObjectRef, ...] = Field(max_length=64)
    selector: Selector
    contexts: frozenset[Context] = Field(min_length=1, max_length=4)
    rule_type: Literal["metric_condition", "research_requirement", "event_impact"]
    definition: Definition
    dimensions: frozenset[Dimension] = Field(min_length=1, max_length=7)
    effects: frozenset[Effect] = Field(min_length=1, max_length=4)
    severity: Severity
    materiality: Text
    event_ref: ObjectRef | None = None
    issue_key: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    valid_from: datetime
    valid_until: datetime | None = None
    review_due_at: datetime
    release_requirements: tuple[Text, ...] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def normalized_definition(self) -> RuleContent:
        for name in ("valid_from", "valid_until", "review_due_at"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, utc_timestamp(value))
        if self.definition.kind != self.rule_type:
            raise ValueError("definition kind must match rule_type")
        if any(ref.field_path is not None for ref in self.source_refs):
            raise ValueError("source_refs do not support field paths")
        if self.selector.kind == "listing" and (
            self.selector.listing_ref.field_path is not None
            or self.selector.listing_ref.revision != 1
        ):
            raise ValueError("listing selectors require the exact current listing reference")
        if self.selector.kind == "category":
            pending = list(self.selector.expression.terms)
            while pending:
                term = pending.pop()
                if hasattr(term, "terms"):
                    pending.extend(term.terms)
                elif term.source_ref.field_path is not None or term.source_ref.kind != "evidence":
                    raise ValueError("category source_refs require exact saved evidence references")
        if self.rule_type == "event_impact":
            if (
                not isinstance(self.definition, EventImpactDefinition)
                or self.event_ref != self.definition.event_ref
            ):
                raise ValueError("event_ref must match the event-impact definition")
        elif self.event_ref is not None:
            raise ValueError("event_ref is only valid for an event_impact rule")
        if self.event_ref is not None and (
            self.event_ref.kind != "research_event" or self.event_ref.field_path is not None
        ):
            raise ValueError("event_ref must be an exact research_event reference")
        if self.valid_until is not None and self.valid_until <= self.valid_from:
            raise ValueError("valid_until must be later than valid_from")
        if self.review_due_at < self.valid_from or (
            self.valid_until is not None and self.review_due_at > self.valid_until
        ):
            raise ValueError("review_due_at must fall within the validity interval")
        return self

    def as_revision(self, rule_id: UUID, revision: int) -> RuleDefinition:
        return RuleDefinition(id=rule_id, revision=revision, **self.model_dump(mode="python"))


class RuleCreateRequest(Contract):
    idempotency_key: UUID
    expected_head_revision: Literal[0] = 0
    definition: RuleContent


class RuleRevisionRequest(Contract):
    idempotency_key: UUID
    expected_head_revision: Annotated[int, Field(strict=True, ge=1)]
    definition: RuleContent


class RuleValidationRequest(Contract):
    idempotency_key: UUID
    expected_head_revision: Annotated[int, Field(strict=True, ge=1)]
    rule_ref: ObjectRef
    content_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]

    @model_validator(mode="after")
    def exact_rule(self) -> RuleValidationRequest:
        if self.rule_ref.kind != "supplemental_rule" or self.rule_ref.field_path is not None:
            raise ValueError("rule_ref must be an exact supplemental_rule reference")
        return self


class RuleDecisionRequest(Contract):
    idempotency_key: UUID
    expected_head_revision: Annotated[int, Field(strict=True, ge=1)]
    rule_ref: ObjectRef
    content_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    action: Literal["approve", "suspend", "retire", "reject"]
    validation_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None = None
    reason: Annotated[str, Field(min_length=1, max_length=1000)] | None = None

    @model_validator(mode="after")
    def valid_action_fields(self) -> RuleDecisionRequest:
        if self.rule_ref.kind != "supplemental_rule" or self.rule_ref.field_path is not None:
            raise ValueError("rule_ref must be an exact supplemental_rule reference")
        if self.action == "approve" and self.validation_hash is None:
            raise ValueError("approve requires the exact validation_hash")
        if self.action != "approve" and self.validation_hash is not None:
            raise ValueError("validation_hash is only valid for approve")
        if self.action in {"suspend", "retire", "reject"} and self.reason is None:
            raise ValueError(f"{self.action} requires a reason")
        return self


class ResearchEventInput(Contract):
    """Public event fields; verification and acquisition timestamps are server-owned."""

    event_key: Text
    title: Text
    event_type: Text
    jurisdiction: Text
    affected_products: tuple[Text, ...] = Field(max_length=128)
    affected_subjects: tuple[Text, ...] = Field(max_length=128)
    policy_stage: Literal[
        "proposed", "enacted", "effective", "modified", "repealed", "unknown", "not_applicable"
    ]
    published_at: datetime | None = None
    effective_from: datetime | None = None
    effective_to: datetime | None = None
    source_refs: tuple[ObjectRef, ...] = Field(max_length=64)

    @model_validator(mode="after")
    def event_times(self) -> ResearchEventInput:
        for name in ("published_at", "effective_from", "effective_to"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, utc_timestamp(value))
        if (
            self.effective_to is not None
            and self.effective_from is not None
            and self.effective_to < self.effective_from
        ):
            raise ValueError("effective_to must not precede effective_from")
        if any(ref.field_path is not None for ref in self.source_refs):
            raise ValueError("event source_refs do not support field paths")
        return self

    def as_user_report(
        self,
        *,
        event_id: UUID,
        revision: int,
        owner_id: Literal["local_owner"],
        acquired_at: datetime,
        available_from: datetime,
        previous_ref: ObjectRef | None = None,
    ) -> ResearchEvent:
        return ResearchEvent(
            id=event_id,
            revision=revision,
            owner_id=owner_id,
            **self.model_dump(mode="python"),
            verification="user_report",
            conflict=False,
            acquired_at=acquired_at,
            available_from=available_from,
            previous_ref=previous_ref,
        )


class ResearchEventCreateRequest(Contract):
    idempotency_key: UUID
    expected_event_revision: Literal[0] = 0
    event: ResearchEventInput


class ResearchEventRevisionRequest(Contract):
    idempotency_key: UUID
    expected_event_revision: Annotated[int, Field(strict=True, ge=1)]
    previous_ref: ObjectRef
    event: ResearchEventInput

    @model_validator(mode="after")
    def exact_previous(self) -> ResearchEventRevisionRequest:
        if (
            self.previous_ref.kind != "research_event"
            or self.previous_ref.field_path is not None
            or self.previous_ref.revision != self.expected_event_revision
        ):
            raise ValueError("previous_ref must identify the exact current research_event revision")
        return self


class ValidationResponse(Contract):
    receipt: MutationReceipt
    rule_ref: ObjectRef
    content_hash: str
    validation_hash: str
    outcome: Literal["pass", "fail"]
    diagnostics: tuple[str, ...]
    head_revision: int
    event_sequence: int


class RuleMutationResponse(Contract):
    receipt: MutationReceipt
    rule_ref: ObjectRef
    head_revision: int
    state: str
    run_ref: ObjectRef | None = None
    replayed: bool = False


class EventMutationResponse(Contract):
    receipt: MutationReceipt
    event_ref: ObjectRef
    run_ref: ObjectRef | None = None
    replayed: bool = False


class EvaluationAdmissionResponse(Contract):
    receipt: MutationReceipt
    admission: EvaluationAdmission


__all__ = [
    "EvaluationAdmissionResponse",
    "EvaluationRequest",
    "EventMutationResponse",
    "ResearchEventCreateRequest",
    "ResearchEventInput",
    "ResearchEventRevisionRequest",
    "RuleContent",
    "RuleCreateRequest",
    "RuleDecisionRequest",
    "RuleMutationResponse",
    "RuleRevisionRequest",
    "RuleValidationRequest",
    "ValidationResponse",
]
