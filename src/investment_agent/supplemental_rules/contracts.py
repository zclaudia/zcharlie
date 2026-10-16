"""Strict, persistence-neutral P2 supplemental-rule contracts.

These models describe frozen facts and user-authored definitions. They do not
authorize a mutation, imply a source is trusted, or perform evaluation.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import Field, model_validator

from investment_agent.investor.common import (
    Contract,
    DecimalValue,
    ObjectRef,
    OwnerId,
    Revision,
    Text,
    utc_timestamp,
)
from investment_agent.investor.contracts import Dimension

Context = Literal["company_research", "candidate", "account_fit", "holding_review"]
RuleType = Literal["metric_condition", "research_requirement", "event_impact"]
Effect = Literal["annotate", "require_evidence", "require_review", "limit_conclusion"]
Severity = Literal["info", "warning", "critical"]
PolicyStage = Literal[
    "proposed",
    "enacted",
    "effective",
    "modified",
    "repealed",
    "unknown",
    "not_applicable",
]
Verification = Literal["candidate", "verified", "user_report"]
MatchStatus = Literal["applicable", "not_applicable", "unknown"]
ApplicationMode = Literal["live", "historical_replay"]
MAX_LISTINGS_PER_BATCH = 100
MAX_PAGE_SIZE = 100
MAX_PAIRS_PER_MANIFEST = 2000
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_SELECTOR_DEPTH = 2
MAX_SELECTOR_MEMBERS = 2000


class ListingSelector(Contract):
    kind: Literal["listing"]
    listing_ref: ObjectRef

    @model_validator(mode="after")
    def listing_ref_kind(self) -> ListingSelector:
        if self.listing_ref.kind != "listing":
            raise ValueError("listing selector requires a listing reference")
        return self


class CompanySelector(Contract):
    kind: Literal["company"]
    company_id: Annotated[int, Field(strict=True, ge=1)]


class IndustryCategoryTerm(Contract):
    kind: Literal["industry"]
    system: Text
    scheme_version: Text
    code: Text
    source_ref: ObjectRef


class MembershipCategoryTerm(Contract):
    kind: Literal["product", "region"]
    members: Annotated[tuple[Text, ...], Field(min_length=1, max_length=2000)]
    complete: bool
    source_ref: ObjectRef


CategoryTerm = Annotated[
    IndustryCategoryTerm | MembershipCategoryTerm,
    Field(discriminator="kind"),
]


class CategoryExpression(Contract):
    operator: Literal["all", "any"]
    terms: Annotated[
        tuple[CategoryTerm | CategoryExpression, ...], Field(min_length=1, max_length=32)
    ]

    @model_validator(mode="after")
    def bounded_depth(self) -> CategoryExpression:
        def depth(node: CategoryExpression, current: int) -> int:
            return max(
                [
                    current,
                    *(
                        depth(child, current + 1)
                        for child in node.terms
                        if isinstance(child, CategoryExpression)
                    ),
                ]
            )

        if depth(self, 1) > 2:
            raise ValueError("category selector supports at most two expression layers")
        return self


class CategorySelector(Contract):
    kind: Literal["category"]
    expression: CategoryExpression


Selector = Annotated[
    ListingSelector | CompanySelector | CategorySelector,
    Field(discriminator="kind"),
]


class MetricLeaf(Contract):
    kind: Literal["factor"] = "factor"
    factor_id: Text
    operator: Literal["<", "<=", ">", ">=", "==", "!=", "between", "in"]
    value: DecimalValue | tuple[DecimalValue, ...]
    unit: Text | None = None
    params: dict[str, DecimalValue | str | bool | None] = Field(default_factory=dict, max_length=32)

    @model_validator(mode="after")
    def validate_operator_value_shape(self) -> MetricLeaf:
        if self.operator == "in":
            if not isinstance(self.value, tuple) or not 1 <= len(self.value) <= 64:
                raise ValueError("in requires between one and 64 values")
        elif self.operator == "between":
            if not isinstance(self.value, tuple) or len(self.value) != 2:
                raise ValueError("between requires exactly two bounds")
            if self.value[0] > self.value[1]:
                raise ValueError("between lower bound must not exceed upper bound")
        elif isinstance(self.value, tuple):
            raise ValueError("this operator requires one scalar value")
        return self


class MetricCondition(Contract):
    kind: Literal["all", "any"]
    conditions: Annotated[
        tuple[MetricLeaf | MetricCondition, ...], Field(min_length=1, max_length=32)
    ]

    @model_validator(mode="after")
    def bounded_tree(self) -> MetricCondition:
        def visit(node: MetricCondition | MetricLeaf, depth: int) -> tuple[int, int]:
            if isinstance(node, MetricLeaf):
                return 1, depth
            counts = [visit(item, depth + 1) for item in node.conditions]
            return sum(count for count, _ in counts), max([depth, *(d for _, d in counts)])

        leaves, depth = visit(self, 1)
        if leaves > 64 or depth > 4:
            raise ValueError("metric condition exceeds 64 leaves or depth 4")
        return self


class MetricConditionDefinition(Contract):
    kind: Literal["metric_condition"]
    condition: MetricCondition
    unknown_policy: Literal["preserve_unknown"] = "preserve_unknown"


class ResearchRequirementDefinition(Contract):
    kind: Literal["research_requirement"]
    question: Text
    required_evidence: Annotated[tuple[Text, ...], Field(min_length=1, max_length=32)]
    completion_criteria: Text
    falsification_criteria: Text
    check_by: datetime
    incomplete_effects: Annotated[tuple[Effect, ...], Field(min_length=1, max_length=4)]

    @model_validator(mode="after")
    def aware_check_by(self) -> ResearchRequirementDefinition:
        object.__setattr__(self, "check_by", utc_timestamp(self.check_by))
        return self


class EventImpactDefinition(Contract):
    kind: Literal["event_impact"]
    event_ref: ObjectRef
    affected_scope: Annotated[tuple[Text, ...], Field(min_length=1, max_length=32)]
    exposure_evidence: Annotated[tuple[Text, ...], Field(min_length=1, max_length=32)]
    impact_path: Text
    release_requirements: Annotated[tuple[Text, ...], Field(min_length=1, max_length=32)]

    @model_validator(mode="after")
    def event_ref_kind(self) -> EventImpactDefinition:
        if self.event_ref.kind != "research_event":
            raise ValueError("event impact requires a research_event reference")
        return self


Definition = Annotated[
    MetricConditionDefinition | ResearchRequirementDefinition | EventImpactDefinition,
    Field(discriminator="kind"),
]


class RuleDefinition(Contract):
    """One immutable content revision; lifecycle state belongs to the head."""

    id: UUID
    revision: Revision
    schema_version: Literal[1] = 1
    name: Text
    rationale: Text
    source_refs: tuple[ObjectRef, ...] = Field(max_length=64)
    selector: Selector
    contexts: Annotated[frozenset[Context], Field(min_length=1, max_length=4)]
    rule_type: RuleType
    definition: Definition
    dimensions: Annotated[frozenset[Dimension], Field(min_length=1, max_length=7)]
    effects: Annotated[frozenset[Effect], Field(min_length=1, max_length=4)]
    severity: Severity
    materiality: Text
    event_ref: ObjectRef | None = None
    issue_key: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    valid_from: datetime
    valid_until: datetime | None = None
    review_due_at: datetime
    release_requirements: Annotated[tuple[Text, ...], Field(min_length=1, max_length=32)]
    content_hash: str | None = None
    semantic_hash: str | None = None

    @model_validator(mode="after")
    def validate_definition_and_times(self) -> RuleDefinition:
        if self.definition.kind != self.rule_type:
            raise ValueError("definition kind must match rule_type")
        if self.rule_type == "event_impact":
            assert isinstance(self.definition, EventImpactDefinition)
            if self.event_ref != self.definition.event_ref:
                raise ValueError("event_ref must match event-impact definition")
        if self.event_ref is not None and self.event_ref.kind != "research_event":
            raise ValueError("event_ref must reference a research_event")
        for name in ("valid_from", "valid_until", "review_due_at"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, utc_timestamp(value))
        if self.valid_until is not None and self.valid_until <= self.valid_from:
            raise ValueError("valid_until must be later than valid_from")
        if self.review_due_at < self.valid_from or (
            self.valid_until is not None and self.review_due_at > self.valid_until
        ):
            raise ValueError("review_due_at must fall within the validity interval")
        if self.rule_type != "event_impact" and self.event_ref is not None:
            raise ValueError("event_ref is only valid for an event_impact rule")
        return self

    @property
    def reference(self) -> ObjectRef:
        return ObjectRef(kind="supplemental_rule", id=self.id, revision=self.revision)


class ResearchEvent(Contract):
    """Immutable business-event revision, distinct from application/audit rows."""

    id: UUID
    revision: Revision
    owner_id: OwnerId
    event_key: Text
    title: Text
    event_type: Text
    jurisdiction: Text
    affected_products: tuple[Text, ...] = Field(max_length=128)
    affected_subjects: tuple[Text, ...] = Field(max_length=128)
    policy_stage: PolicyStage
    published_at: datetime | None = None
    effective_from: datetime | None = None
    effective_to: datetime | None = None
    acquired_at: datetime
    available_from: datetime
    source_refs: tuple[ObjectRef, ...] = Field(max_length=64)
    verification: Verification
    conflict: bool
    previous_ref: ObjectRef | None = None
    content_hash: str | None = None

    @model_validator(mode="after")
    def validate_event_times_and_refs(self) -> ResearchEvent:
        for name in (
            "published_at",
            "effective_from",
            "effective_to",
            "acquired_at",
            "available_from",
        ):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, utc_timestamp(value))
        if (
            self.effective_to is not None
            and self.effective_from is not None
            and self.effective_to < self.effective_from
        ):
            raise ValueError("effective_to must not precede effective_from")
        if self.revision == 1 and self.previous_ref is not None:
            raise ValueError("first event revision cannot have previous_ref")
        if self.revision > 1:
            if (
                self.previous_ref is None
                or self.previous_ref.kind != "research_event"
                or self.previous_ref.id != self.id
            ):
                raise ValueError("event revision must reference its exact prior event identity")
            if self.previous_ref.revision != self.revision - 1:
                raise ValueError("previous_ref must reference the preceding revision")
        if self.available_from < self.acquired_at:
            raise ValueError("available_from cannot precede server acquisition")
        return self

    @property
    def reference(self) -> ObjectRef:
        return ObjectRef(kind="research_event", id=self.id, revision=self.revision)


class FrozenSubject(Contract):
    listing_ref: ObjectRef
    company_id: int
    ticker: Text
    name: str | None = None
    as_of: datetime
    input_status: Literal["ready", "partial", "unknown"]
    selector_inputs: dict[str, Any]
    factor_values: dict[str, Any]
    evidence: tuple[dict[str, Any], ...]
    source_manifest: tuple[dict[str, Any], ...]
    diagnostics: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_subject(self) -> FrozenSubject:
        if self.listing_ref.kind != "listing":
            raise ValueError("frozen subject requires a listing reference")
        if self.company_id < 1:
            raise ValueError("company_id must be positive")
        object.__setattr__(self, "as_of", utc_timestamp(self.as_of))
        return self


class FrozenPair(Contract):
    ordinal: Annotated[int, Field(strict=True, ge=0)]
    listing_ref: ObjectRef
    context: Context
    rule_ref: ObjectRef
    rule: RuleDefinition
    subject: FrozenSubject
    selected_event: ResearchEvent | None = None
    prior_live_application_ref: ObjectRef | None = None
    prior_open_issue_refs: tuple[ObjectRef, ...] = Field(default=(), max_length=256)
    prior_issue_keys: tuple[str, ...] = Field(default=(), max_length=256)
    prior_input_hash: str | None = None
    input_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    rule_state: Literal["active", "suspended", "retired", "expired", "review_due"] = "active"
    match_status: MatchStatus
    matching_manifest_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    diagnostics: tuple[str, ...] = ()

    @model_validator(mode="after")
    def pair_identity(self) -> FrozenPair:
        if self.listing_ref.kind != "listing" or self.listing_ref != self.subject.listing_ref:
            raise ValueError("pair and subject listing references must match")
        if (
            self.rule_ref.kind != "supplemental_rule"
            or self.rule_ref.id != self.rule.id
            or self.rule_ref.revision != self.rule.revision
        ):
            raise ValueError("pair rule_ref must identify the frozen rule revision")
        if self.context not in self.rule.contexts and not self.prior_open_issue_refs:
            raise ValueError("pair context must be enabled by the frozen rule")
        if (
            self.prior_live_application_ref is not None
            and self.prior_live_application_ref.kind != "rule_application"
        ):
            raise ValueError("prior application reference must be rule_application")
        if any(ref.kind != "rule_application" for ref in self.prior_open_issue_refs):
            raise ValueError("prior open issue refs must identify rule applications")
        return self


class FrozenManifest(Contract):
    owner_id: OwnerId
    # Internal admissions may legitimately freeze an empty listing scope. Public
    # EvaluationRequest remains non-empty; the manifest records that decision.
    listing_refs: Annotated[
        tuple[ObjectRef, ...], Field(min_length=0, max_length=MAX_LISTINGS_PER_BATCH)
    ]
    contexts: Annotated[frozenset[Context], Field(min_length=1, max_length=4)]
    as_of: datetime
    evaluated_at: datetime
    rules_generation: Annotated[int, Field(strict=True, ge=0)]
    events_generation: Annotated[int, Field(strict=True, ge=0)]
    matcher_version: Text
    evaluator_version: Text
    pairs: tuple[FrozenPair, ...] = Field(max_length=MAX_PAIRS_PER_MANIFEST)
    unbound_events: tuple[ResearchEvent, ...] = Field(max_length=MAX_PAIRS_PER_MANIFEST)
    scan_diagnostics: tuple[str, ...] = ()
    scope_semantic_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    input_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    manifest_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]

    @model_validator(mode="after")
    def validate_manifest(self) -> FrozenManifest:
        object.__setattr__(self, "as_of", utc_timestamp(self.as_of))
        object.__setattr__(self, "evaluated_at", utc_timestamp(self.evaluated_at))
        if tuple(pair.ordinal for pair in self.pairs) != tuple(range(len(self.pairs))):
            raise ValueError("manifest pairs must be complete and ordered from ordinal zero")
        if any(ref.kind != "listing" for ref in self.listing_refs):
            raise ValueError("manifest listing_refs must all identify listings")
        if len(set(self.listing_refs)) != len(self.listing_refs):
            raise ValueError("manifest listing_refs must be unique")
        listing_refs = set(self.listing_refs)
        if not self.listing_refs and self.pairs:
            raise ValueError("a zero-target manifest cannot contain pairs")
        if any(pair.listing_ref not in listing_refs for pair in self.pairs):
            raise ValueError("every pair must belong to a manifest listing")
        if any(pair.context not in self.contexts for pair in self.pairs):
            raise ValueError("every pair context must belong to the manifest context scope")
        from investment_agent.supplemental_rules.canonical import canonical_json

        if len(canonical_json(self).encode("utf-8")) > MAX_MANIFEST_BYTES:
            raise ValueError("manifest exceeds 4 MiB")
        return self


class EvidenceFacet(Contract):
    status: Literal["verified", "pending", "conflicted", "not_applicable", "unknown"]
    summary: str | None = None
    evidence_refs: tuple[ObjectRef, ...] = Field(default=(), max_length=128)
    unknowns: tuple[str, ...] = ()
    claims: tuple[dict[str, Any], ...] = ()


class EffectiveEffect(Contract):
    effect: Effect
    basis_refs: tuple[ObjectRef, ...] = Field(default=(), max_length=128)
    reason: Text


class EvaluationResult(Contract):
    applicability: MatchStatus
    condition_result: Literal["triggered", "not_triggered", "unknown", "not_applicable"]
    evidence_status: Literal["verified", "pending", "conflicted", "not_applicable"]
    assessment_status: Literal[
        "clear", "unresolved", "supported", "mitigated", "resolved", "inconclusive"
    ]
    semantic_status: Literal["mechanical", "pending", "reviewed"]
    effective_effects: tuple[EffectiveEffect, ...] = ()
    diagnostics: tuple[str, ...] = ()
    open_issue_refs: tuple[ObjectRef, ...] = ()
    closure_refs: tuple[ObjectRef, ...] = ()
    event_fact: EvidenceFacet
    company_exposure: EvidenceFacet
    impact_assessment: EvidenceFacet
    semantic_review_ref: ObjectRef | None = None

    @model_validator(mode="after")
    def no_false_resolution(self) -> EvaluationResult:
        if self.applicability == "not_applicable" and (
            self.condition_result != "not_applicable" or self.evidence_status != "not_applicable"
        ):
            raise ValueError("not-applicable results require matching condition/evidence status")
        if self.applicability == "unknown" and self.condition_result != "unknown":
            raise ValueError("unknown applicability cannot produce a known condition result")
        if self.semantic_status != "reviewed" and self.semantic_review_ref is not None:
            raise ValueError("unreviewed evaluation cannot cite a semantic review")
        if self.semantic_status == "reviewed" and self.semantic_review_ref is None:
            raise ValueError("reviewed evaluation requires an exact semantic review reference")
        if self.semantic_review_ref is not None and self.semantic_review_ref.kind != "review":
            raise ValueError("semantic_review_ref must identify a review")
        if self.condition_result == "unknown" and self.assessment_status in {"clear", "resolved"}:
            raise ValueError("unknown result cannot be clear or resolved")
        if self.semantic_status != "reviewed" and self.assessment_status in {
            "supported", "mitigated", "resolved",
        }:
            raise ValueError("mechanical or pending evaluations cannot claim semantic conclusions")
        issue_refs = (*self.open_issue_refs, *self.closure_refs)
        if any(ref.kind != "rule_application" or ref.field_path is not None for ref in issue_refs):
            raise ValueError("issue references must identify exact rule applications")
        if set(self.open_issue_refs).intersection(self.closure_refs):
            raise ValueError("an issue cannot be both open and closed in one result")
        if self.assessment_status == "clear" and self.open_issue_refs:
            raise ValueError("clear evaluation cannot carry open issue references")
        if self.assessment_status == "resolved" and (
            self.semantic_status != "reviewed"
            or self.semantic_review_ref is None
            or not self.closure_refs
            or self.open_issue_refs
        ):
            raise ValueError("resolved evaluation requires review, closed issues, and no open issues")
        return self


class RuleApplication(Contract):
    id: UUID
    revision: Literal[1] = 1
    operation_id: UUID
    owner_id: OwnerId
    listing_ref: ObjectRef
    company_id: int
    context: Context
    rule_ref: ObjectRef
    rules_generation: Annotated[int, Field(strict=True, ge=0)]
    events_generation: Annotated[int, Field(strict=True, ge=0)]
    matching_manifest_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    event_ref: ObjectRef | None = None
    as_of: datetime
    evaluated_at: datetime
    valid_until: datetime | None = None
    review_due_at: datetime
    input_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    matcher_version: Text
    evaluator_version: Text
    dependencies: tuple[ObjectRef, ...] = Field(max_length=128)
    series_key: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    application_sequence: Annotated[int, Field(strict=True, ge=1)]
    application_mode: ApplicationMode
    origin_ref: ObjectRef | None = None
    created_at: datetime
    actor: Text
    previous_application_ref: ObjectRef | None = None
    frozen_pair: FrozenPair
    source_manifest_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    evaluation_purpose: Literal["coverage", "issue_review"]
    applicability: MatchStatus
    condition_result: Literal["triggered", "not_triggered", "unknown", "not_applicable"]
    evidence_status: Literal["verified", "pending", "conflicted", "not_applicable"]
    assessment_status: Literal[
        "clear", "unresolved", "supported", "mitigated", "resolved", "inconclusive"
    ]
    semantic_status: Literal["mechanical", "pending", "reviewed"]
    semantic_review_ref: ObjectRef | None = None
    effective_effects: tuple[dict[str, Any], ...] = ()
    open_issue_refs: tuple[ObjectRef, ...] = ()
    closure_refs: tuple[ObjectRef, ...] = ()
    result: EvaluationResult
    payload: dict[str, Any]

    @model_validator(mode="after")
    def application_identity(self) -> RuleApplication:
        if self.listing_ref.kind != "listing" or self.rule_ref.kind != "supplemental_rule":
            raise ValueError("application requires listing and supplemental_rule references")
        if self.event_ref is not None and self.event_ref.kind != "research_event":
            raise ValueError("application event_ref must reference research_event")
        if (
            self.previous_application_ref is not None
            and self.previous_application_ref.kind != "rule_application"
        ):
            raise ValueError("previous_application_ref must reference rule_application")
        if (
            self.previous_application_ref is not None
            and self.previous_application_ref != self.frozen_pair.prior_live_application_ref
        ):
            raise ValueError("previous application must use the exact frozen predecessor reference")
        for name in ("as_of", "evaluated_at", "valid_until", "review_due_at", "created_at"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, utc_timestamp(value))
        if self.semantic_review_ref is not None and self.semantic_review_ref.kind != "review":
            raise ValueError("semantic_review_ref must identify a review")
        if self.semantic_status != "reviewed" and self.semantic_review_ref is not None:
            raise ValueError("only reviewed semantic results can reference a review")
        if self.applicability == "not_applicable" and (
            self.condition_result != "not_applicable" or self.evidence_status != "not_applicable"
        ):
            raise ValueError(
                "not-applicable applications require matching condition/evidence statuses"
            )
        if self.applicability == "unknown" and self.condition_result != "unknown":
            raise ValueError("unknown match cannot produce a known condition result")
        if self.semantic_status != "reviewed" and self.semantic_review_ref is not None:
            raise ValueError("only reviewed semantic results can reference a review")
        if (
            self.application_mode == "historical_replay"
            and self.evaluation_purpose == "issue_review"
        ):
            raise ValueError("historical replay cannot resolve a live issue")
        if (
            self.rule_ref != self.frozen_pair.rule_ref
            or self.listing_ref != self.frozen_pair.listing_ref
            or self.context != self.frozen_pair.context
        ):
            raise ValueError("application identity must match its frozen pair")
        if self.company_id != self.frozen_pair.subject.company_id:
            raise ValueError("application company identity must match its frozen subject")
        if self.as_of != self.frozen_pair.subject.as_of:
            raise ValueError("application as_of must match its frozen subject")
        if self.matching_manifest_hash != self.frozen_pair.matching_manifest_hash:
            raise ValueError("application matching manifest hash must match its frozen pair")
        from investment_agent.supplemental_rules.canonical import canonical_hash

        expected_input_hash = canonical_hash(
            {
                "pair": self.frozen_pair,
                "manifest_hash": self.source_manifest_hash,
                "mode": self.application_mode,
                "purpose": self.evaluation_purpose,
            }
        )
        if self.input_hash != expected_input_hash:
            raise ValueError(
                "application input_hash must bind its frozen pair, manifest, mode, and purpose"
            )
        from investment_agent.supplemental_rules.canonical import stable_series_key

        expected_series = stable_series_key(
            str(self.owner_id),
            str(self.listing_ref.id),
            str(self.rule_ref.id),
            self.context,
        )
        if self.series_key != expected_series:
            raise ValueError(
                "application series_key must bind its owner/listing/rule/context identity"
            )
        if self.previous_application_ref != self.frozen_pair.prior_live_application_ref:
            raise ValueError("application predecessor must match the frozen live predecessor")
        if self.event_ref != (
            self.frozen_pair.selected_event.reference if self.frozen_pair.selected_event else None
        ):
            raise ValueError(
                "application event reference must match the exact frozen event revision"
            )
        if (
            self.valid_until != self.frozen_pair.rule.valid_until
            or self.review_due_at != self.frozen_pair.rule.review_due_at
        ):
            raise ValueError("application validity boundaries must match the frozen rule")
        result_fields = (
            "applicability",
            "condition_result",
            "evidence_status",
            "assessment_status",
            "semantic_status",
            "effective_effects",
            "open_issue_refs",
            "closure_refs",
            "semantic_review_ref",
        )
        for field_name in result_fields:
            left, right = getattr(self, field_name), getattr(self.result, field_name)
            if field_name == "effective_effects":
                left = tuple(EffectiveEffect.model_validate(item) for item in left)
            if left != right:
                raise ValueError(f"application {field_name} must match its typed result")
        if self.applicability != self.frozen_pair.match_status:
            raise ValueError("application applicability must match its frozen selector result")
        for ref in (*self.dependencies, *self.open_issue_refs, *self.closure_refs):
            if ref.field_path is not None or ref.revision is None:
                raise ValueError(
                    "application dependency and issue references must be exact revisions"
                )
        for lineage_ref in (
            self.origin_ref,
            self.previous_application_ref,
            self.semantic_review_ref,
        ):
            if lineage_ref is not None and (
                lineage_ref.field_path is not None or lineage_ref.revision is None
            ):
                raise ValueError("application lineage references must identify exact revisions")
        if self.result.semantic_status == "reviewed":
            raise ValueError("P2 mechanical applications cannot claim a completed semantic review")
        if self.assessment_status in {"supported", "mitigated", "resolved"}:
            raise ValueError("P2 applications cannot claim semantic conclusions")
        if self.closure_refs:
            raise ValueError("P2 applications cannot close issues without a bound review workflow")
        return self

    @property
    def reference(self) -> ObjectRef:
        return ObjectRef(kind="rule_application", id=self.id, revision=self.revision)


class EvaluationRequest(Contract):
    """User intent only; the server enumerates and freezes all rule pairs."""

    idempotency_key: UUID
    listing_refs: Annotated[
        tuple[ObjectRef, ...], Field(min_length=1, max_length=MAX_LISTINGS_PER_BATCH)
    ]
    contexts: Annotated[frozenset[Context], Field(min_length=1, max_length=4)]
    mode: ApplicationMode
    evaluation_purpose: Literal["coverage", "issue_review"] = "coverage"
    exact_rule_refs: tuple[ObjectRef, ...] = Field(default=(), max_length=100)
    prior_application_refs: tuple[ObjectRef, ...] = Field(default=(), max_length=2000)
    replay_run_ref: ObjectRef | None = None
    replay_manifest_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None = None
    input_as_of: datetime | None = None

    @model_validator(mode="after")
    def request_shape(self) -> EvaluationRequest:
        if any(
            ref.kind != "listing" or ref.field_path is not None or ref.revision != 1
            for ref in self.listing_refs
        ):
            raise ValueError("evaluation listing_refs must identify exact current listings")
        if any(
            ref.kind != "supplemental_rule" or ref.field_path is not None
            for ref in self.exact_rule_refs
        ):
            raise ValueError("exact_rule_refs must identify exact supplemental rule revisions")
        if any(
            ref.kind != "rule_application" or ref.field_path is not None
            for ref in self.prior_application_refs
        ):
            raise ValueError("prior_application_refs must identify exact rule applications")
        if self.mode == "historical_replay":
            if (
                self.replay_run_ref is None
                or self.replay_run_ref.kind != "pipeline_run"
                or self.replay_run_ref.field_path is not None
                or self.replay_run_ref.revision != 1
                or self.input_as_of is None
                or self.replay_manifest_hash is None
            ):
                raise ValueError(
                    "historical replay requires its exact source run, manifest hash, and input_as_of"
                )
            if self.evaluation_purpose == "issue_review":
                raise ValueError("historical replay cannot be used for live issue review")
            object.__setattr__(self, "input_as_of", utc_timestamp(self.input_as_of))
        elif (
            self.replay_run_ref is not None
            or self.replay_manifest_hash is not None
            or self.input_as_of is not None
        ):
            raise ValueError("live evaluation cannot contain replay bindings")
        if self.evaluation_purpose == "issue_review" and not self.prior_application_refs:
            raise ValueError("issue_review requires exact prior application refs")
        return self


class MutationReceipt(Contract):
    """Common typed discriminator for P2 idempotency receipts."""

    owner_id: OwnerId
    operation_id: UUID
    result_kind: Literal["rule_mutation", "event_mutation", "evaluation_admission"]
    command: Text
    target_ref: ObjectRef | None = None
    accepted_at: datetime
    result_ref: ObjectRef | None = None
    run_ref: ObjectRef | None = None
    source_manifest_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None = None

    @model_validator(mode="after")
    def validate_receipt(self) -> MutationReceipt:
        object.__setattr__(self, "accepted_at", utc_timestamp(self.accepted_at))
        if self.result_ref is not None:
            expected = {
                "rule_mutation": "supplemental_rule",
                "event_mutation": "research_event",
            }.get(self.result_kind)
            if expected is not None and self.result_ref.kind != expected:
                raise ValueError("receipt result_ref kind does not match result_kind")
        if self.run_ref is not None and self.run_ref.kind != "pipeline_run":
            raise ValueError("receipt run_ref must identify a pipeline run")
        if (self.run_ref is None) != (self.source_manifest_hash is None):
            raise ValueError(
                "receipt source_manifest_hash is required exactly when a run_ref is present"
            )
        return self


class EvaluationAdmission(Contract):
    operation_id: UUID
    run_ref: ObjectRef
    manifest_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    accepted_at: datetime

    @model_validator(mode="after")
    def admission_ref(self) -> EvaluationAdmission:
        if self.run_ref.kind != "pipeline_run":
            raise ValueError("evaluation admission requires a pipeline run reference")
        object.__setattr__(self, "accepted_at", utc_timestamp(self.accepted_at))
        return self


MetricCondition.model_rebuild()
