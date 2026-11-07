"""Closed research-quality DTOs; none of these objects grants execution authority."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import Field, StrictBool, field_validator, model_validator

from investment_agent.domain.llm_catalog import Capability, ModelRef
from investment_agent.investor.common import (
    BusinessError,
    Contract,
    DecimalValue,
    ObjectRef,
    Revision,
    Text,
)
from investment_agent.research_quality.refs import QualityArtifactRef, parse_public_ref

Hash = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
PositiveInt = Annotated[int, Field(strict=True, ge=1)]
NonnegativeInt = Annotated[int, Field(strict=True, ge=0)]
RepeatCount = Annotated[int, Field(strict=True, ge=1, le=2)]
type Arm = Literal["baseline", "candidate"]
type ExperimentState = Literal[
    "draft",
    "sealed",
    "running",
    "blocked",
    "cancelling",
    "cancelled",
    "awaiting_review",
    "reported",
]
type TrialState = Literal[
    "planned", "queued", "running", "completed", "failed", "cancelled", "blocked"
]
type MaterialMode = Literal["observed_materials", "reconstructed_materials"]
type UsageState = Literal["not_sent", "reserved", "sent", "settled", "unknown"]
type MutationOperation = Literal[
    "create_case", "create_experiment", "seal", "start", "cancel", "assess", "report"
]

EXPERIMENT_TRANSITIONS: dict[str, frozenset[str]] = {
    "draft": frozenset({"sealed", "cancelling"}),
    "sealed": frozenset({"running", "cancelling"}),
    "running": frozenset({"blocked", "awaiting_review", "cancelling"}),
    "blocked": frozenset({"running", "awaiting_review", "cancelling"}),
    "cancelling": frozenset({"cancelled"}),
    "cancelled": frozenset(),
    "awaiting_review": frozenset({"reported", "cancelling"}),
    "reported": frozenset(),
}
MATERIAL_KINDS = frozenset(
    {
        "evidence",
        "disclosure_original",
        "research_dataset",
        "financial_snapshot",
        "market_snapshot",
        "calculation",
        "earnings_forecast",
        "disclosure_schedule",
        "business_segment",
    }
)
MUTATION_NOT_OBSERVED_NOTICE = (
    "404仅表示当前未见可访问的已提交收据，不证明无在途请求；保留原mutation_id和请求体。"
)


def exact(ref: ObjectRef, kind: str, *, immutable: bool = False) -> None:
    if ref.kind != kind or ref.field_path is not None or immutable and ref.revision != 1:
        raise ValueError(f"An exact whole {kind} reference is required")


class VersionOne(Contract):
    schema_version: Literal[1] = 1

    @field_validator("schema_version", mode="before")
    @classmethod
    def strict_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("Schema version must be a strict integer")
        return value


class QualityLimits(Contract):
    max_requests: PositiveInt
    max_tokens: PositiveInt


class QualityCaseInput(VersionOne):
    mutation_id: UUID
    listing_ref: ObjectRef
    question: Text
    cutoff: datetime
    material_refs: Annotated[tuple[ObjectRef, ...], Field(max_length=200)]
    material_mode: MaterialMode = "observed_materials"

    @model_validator(mode="after")
    def scope_and_sources(self) -> Self:
        exact(self.listing_ref, "listing", immutable=True)
        if not self.question.strip() or any(
            ref.kind not in MATERIAL_KINDS for ref in self.material_refs
        ):
            raise ValueError("A question and exact financial material references are required")
        if len(set(self.material_refs)) != len(self.material_refs):
            raise ValueError("Duplicate material reference")
        return self


class QualityCaseSaved(VersionOne):
    case_ref: QualityArtifactRef
    listing_ref: ObjectRef
    question: Text
    cutoff: datetime
    material_mode: MaterialMode
    material_refs: tuple[ObjectRef, ...]
    gaps: tuple[Text, ...] = ()

    @model_validator(mode="after")
    def kinds(self) -> Self:
        parse_public_ref(self.case_ref, expected_kind="quality_case")
        exact(self.listing_ref, "listing", immutable=True)
        if any(ref.kind not in MATERIAL_KINDS for ref in self.material_refs):
            raise ValueError("Only financial material references belong in a case")
        return self


class CreateExperiment(VersionOne):
    mutation_id: UUID
    case_ref: QualityArtifactRef
    proposal_id: UUID
    expected_proposal_revision: Revision
    repeat_count: RepeatCount = 1
    limits: QualityLimits
    rubric_version: Literal["research-quality.v1"] = "research-quality.v1"

    @model_validator(mode="after")
    def case_kind(self) -> Self:
        parse_public_ref(self.case_ref, expected_kind="quality_case")
        return self


class ExperimentMutation(VersionOne):
    mutation_id: UUID
    expected_revision: Revision


class SealExperiment(ExperimentMutation):
    pass


class StartExperiment(ExperimentMutation):
    spec_hash: Hash


class CancelExperiment(ExperimentMutation):
    pass


class TrialPlan(Contract):
    arms: tuple[Literal["baseline"], Literal["candidate"]] = ("baseline", "candidate")
    repeat_count: RepeatCount
    planned_trials: PositiveInt

    @model_validator(mode="after")
    def planned_denominator(self) -> Self:
        if self.planned_trials != 2 * self.repeat_count:
            raise ValueError("Two arms times the frozen repeat count determines planned trials")
        return self


class Preflight(Contract):
    status: Literal["pending", "ready", "blocked"]
    errors: tuple[BusinessError, ...] = ()

    @model_validator(mode="after")
    def ready_errors(self) -> Self:
        if self.status == "ready" and self.errors:
            raise ValueError("Ready preflight cannot retain blocking errors")
        return self


class ExperimentLinks(Contract):
    self: Annotated[str, Field(pattern=r"^/investor/methods/quality-experiments/[0-9a-f-]{36}$")]


class ExperimentCreated(VersionOne):
    experiment_id: UUID
    revision: Revision
    state: Literal["draft"] = "draft"
    execution_started: StrictBool = False
    preflight: Preflight
    model_policy: Literal["same_authorized_investor_model"] = "same_authorized_investor_model"
    limits: QualityLimits
    trial_plan: TrialPlan
    links: ExperimentLinks

    @model_validator(mode="after")
    def draft_has_no_execution(self) -> Self:
        if (
            self.execution_started
            or self.links.self != f"/investor/methods/quality-experiments/{self.experiment_id}"
        ):
            raise ValueError("Draft cannot execute and must link its own exact experiment")
        return self


class SafeModelSnapshot(Contract):
    """The credential-free shape returned by review_panel.service.model_snapshot."""

    ref: Text
    provider_id: Text
    provider_kind: Literal["openai", "deepseek"]
    model_id: Text
    model: Text
    profile: Literal["openai", "deepseek"]
    output_mode: Literal["prompted", "tool", "native"]
    thinking: Literal["auto", "enabled", "disabled"]
    capabilities: tuple[Capability, ...]
    selection: ModelRef
    invocation_hash: Hash

    @model_validator(mode="after")
    def selection_identity(self) -> Self:
        if self.ref != f"{self.provider_id}/{self.model_id}" or (
            self.selection.provider_id,
            self.selection.model_id,
        ) != (self.provider_id, self.model_id):
            raise ValueError("Model snapshot selection identity disagrees")
        return self


class ArmMethod(Contract):
    arm: Arm
    method_ref: ObjectRef
    binding_hash: Hash
    package_ref: ObjectRef | None = None
    package_hash: Hash | None = None

    @model_validator(mode="after")
    def exact_method(self) -> Self:
        exact(self.method_ref, "method", immutable=True)
        if (self.package_ref is None) != (self.package_hash is None):
            raise ValueError("Package reference and hash must be paired")
        if self.package_ref is not None:
            exact(self.package_ref, "package")
        return self


class ExperimentSealed(VersionOne):
    experiment_id: UUID
    revision: Revision
    state: Literal["sealed"] = "sealed"
    execution_started: StrictBool = False
    spec_ref: QualityArtifactRef
    spec_hash: Hash
    envelope_ref: QualityArtifactRef
    envelope_hash: Hash
    model: SafeModelSnapshot
    methods: tuple[ArmMethod, ArmMethod]
    limits: QualityLimits
    trial_plan: TrialPlan

    @model_validator(mode="after")
    def frozen_refs(self) -> Self:
        parse_public_ref(self.spec_ref, expected_kind="quality_spec")
        parse_public_ref(self.envelope_ref, expected_kind="quality_envelope")
        if self.execution_started or tuple(item.arm for item in self.methods) != (
            "baseline",
            "candidate",
        ):
            raise ValueError("Seal freezes two ordered arms and does not start execution")
        return self


class TrialStarted(Contract):
    trial_id: UUID
    arm: Arm
    repeat_index: RepeatCount
    task_ref: ObjectRef
    state: Literal["queued", "running", "blocked"] = "queued"

    @model_validator(mode="after")
    def task_kind(self) -> Self:
        exact(self.task_ref, "task", immutable=True)
        return self


class ExperimentStarted(VersionOne):
    experiment_id: UUID
    revision: Revision
    state: Literal["running"] = "running"
    spec_hash: Hash
    trials: Annotated[tuple[TrialStarted, ...], Field(min_length=2, max_length=4)]

    @model_validator(mode="after")
    def complete_admission(self) -> Self:
        indices = {trial.repeat_index for trial in self.trials}
        actual = {(trial.arm, trial.repeat_index) for trial in self.trials}
        expected = {(arm, index) for arm in ("baseline", "candidate") for index in indices}
        if (
            indices != set(range(1, len(indices) + 1))
            or actual != expected
            or len(actual) != len(self.trials)
            or len({trial.task_ref for trial in self.trials}) != len(self.trials)
        ):
            raise ValueError("Start response must contain one unique task for each paired trial")
        return self


class ExperimentCancelled(VersionOne):
    experiment_id: UUID
    revision: Revision
    state: Literal["cancelling", "cancelled"]
    task_refs: tuple[ObjectRef, ...] = ()

    @model_validator(mode="after")
    def tasks(self) -> Self:
        for ref in self.task_refs:
            exact(ref, "task", immutable=True)
        return self


class RubricScore(Contract):
    score: Literal[0, 1, 2] | None
    reason: Text
    not_applicable_reason: Text | None = None

    @field_validator("score", mode="before")
    @classmethod
    def strict_score(cls, value: object) -> object:
        if value is not None and type(value) is not int:
            raise ValueError("Score must be strict integer 0/1/2 or justified N/A")
        return value

    @model_validator(mode="after")
    def justified_na(self) -> Self:
        if (self.score is None) != bool(
            self.not_applicable_reason and self.not_applicable_reason.strip()
        ):
            raise ValueError("N/A requires its own reason; numeric scores cannot claim N/A")
        return self


class QualityScores(Contract):
    fact_relationship: RubricScore
    research_depth: RubricScore
    counter_evidence: RubricScore
    independent_revision: RubricScore
    clarity_next_steps: RubricScore


class AssessmentInput(VersionOne):
    mutation_id: UUID
    expected_assessment_revision: NonnegativeInt
    output_hash: Hash
    rubric_version: Literal["research-quality.v1"] = "research-quality.v1"
    scores: QualityScores
    critical_fact_error: StrictBool = False
    scope_violation: StrictBool = False
    blinding_compromised: StrictBool = False


class AssessmentSaved(VersionOne):
    experiment_id: UUID
    assessment_id: UUID
    assessment_revision: Revision
    output_hash: Hash
    blinded: StrictBool
    blinding_compromised: StrictBool


class AssessmentVersion(Contract):
    assessment_id: UUID
    revision: Revision
    payload_hash: Hash


class ReportInput(ExperimentMutation):
    assessment_versions: Annotated[tuple[AssessmentVersion, ...], Field(max_length=100)]


class Denominators(Contract):
    planned: NonnegativeInt
    admitted: NonnegativeInt
    sent: NonnegativeInt
    completed: NonnegativeInt
    usable_output: NonnegativeInt
    paired: NonnegativeInt
    scored: NonnegativeInt

    @model_validator(mode="after")
    def count_bounds(self) -> Self:
        if not (
            self.usable_output <= self.completed <= self.admitted <= self.planned
            and self.sent <= self.admitted
            and self.scored <= self.usable_output
            and 2 * self.paired <= self.usable_output
        ):
            raise ValueError(
                "Failure denominators cannot drop planned trials or manufacture paired/scored outputs"
            )
        return self


class UsageProjection(Contract):
    state: UsageState
    requests: NonnegativeInt
    tokens: NonnegativeInt | None
    cost: Annotated[DecimalValue, Field(ge=0)] | None
    cost_status: Literal["known", "unknown"]

    @model_validator(mode="after")
    def unknown_is_not_zero(self) -> Self:
        if (self.cost_status == "unknown") != (self.cost is None):
            raise ValueError("Unknown actual cost remains null")
        if self.state == "unknown" and (self.tokens is not None or self.cost is not None):
            raise ValueError("Unknown actual usage cannot invent a token count or actual cost")
        return self


class ReportSaved(VersionOne):
    experiment_id: UUID
    revision: Revision
    state: Literal["reported"] = "reported"
    report_ref: QualityArtifactRef
    report_hash: Hash
    completeness: Literal["complete", "partial"]
    denominators: Denominators

    @model_validator(mode="after")
    def report_kind(self) -> Self:
        parse_public_ref(self.report_ref, expected_kind="quality_report")
        return self


type MutationResponse = (
    QualityCaseSaved
    | ExperimentCreated
    | ExperimentSealed
    | ExperimentStarted
    | ExperimentCancelled
    | AssessmentSaved
    | ReportSaved
)
_MUTATION_RESPONSES: dict[str, type[Contract]] = {
    "create_case": QualityCaseSaved,
    "create_experiment": ExperimentCreated,
    "seal": ExperimentSealed,
    "start": ExperimentStarted,
    "cancel": ExperimentCancelled,
    "assess": AssessmentSaved,
    "report": ReportSaved,
}


class MutationReceipt(VersionOne):
    mutation_id: UUID
    operation: MutationOperation
    experiment_id: UUID | None
    result_refs: tuple[QualityArtifactRef, ...]
    response_status: Annotated[int, Field(strict=True, ge=200, le=202)]
    response: MutationResponse
    committed_at: datetime

    @model_validator(mode="before")
    @classmethod
    def closed_response(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        operation = data.get("operation")
        if not isinstance(operation, str) or operation not in _MUTATION_RESPONSES:
            raise ValueError("Unknown mutation operation")
        data["response"] = _MUTATION_RESPONSES[operation].model_validate(data.get("response"))
        return data

    @model_validator(mode="after")
    def original_response_identity(self) -> Self:
        if type(self.response) is not _MUTATION_RESPONSES[self.operation]:
            raise ValueError("Operation/response mismatch")
        if self.operation == "create_case":
            if self.experiment_id is not None:
                raise ValueError("Case creation does not create an experiment")
        elif (
            self.experiment_id is None
            or getattr(self.response, "experiment_id", None) != self.experiment_id
        ):
            raise ValueError("Receipt must retain the original experiment identity")
        expected_refs = tuple(
            getattr(self.response, name)
            for name in ("case_ref", "spec_ref", "envelope_ref", "report_ref")
            if hasattr(self.response, name)
        )
        if len(set(self.result_refs)) != len(self.result_refs) or set(self.result_refs) != set(
            expected_refs
        ):
            raise ValueError(
                "Receipt result_refs must retain exactly the original response artifact references"
            )
        return self


class _BlindAssignment(Contract):
    trial_id: UUID
    label: Literal["A", "B"]
    arm: Arm


class _BlindMap(Contract):
    """Internal only; no public DTO, URL whitelist or __all__ export includes it."""

    experiment_id: UUID
    assignments: Annotated[tuple[_BlindAssignment, ...], Field(min_length=2, max_length=4)]
    mapping_hash: Hash
