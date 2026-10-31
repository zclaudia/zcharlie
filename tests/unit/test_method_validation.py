"""Card 07: package-level method validation and committed_sequence (§6.2).

Covers the §11 M2 acceptance pairs on real admitted v2 tasks: an unrelated
calculation cannot stand in for the required method, a self-reported satisfied
result without actions/refs is refused, dependency order and the gap path have
both directions, first plans are validated as plans, changed plans are
refused, verified-only counter evidence follows the enforce_steps path, and
v1 tasks plus render-only receipts never trigger package validation at all.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, update

from investment_agent.agent.execution import ExecutionDeniedError
from investment_agent.agent.method_receipt import VALIDATOR_RENDER_ONLY, build_receipt
from investment_agent.agent.research import (
    MethodPlanBinding,
    MethodPlannedProgress,
    MethodStepResult,
    validate_progress,
)
from investment_agent.agent.tasks import ActionResult, TaskService
from investment_agent.evidence.contracts import CalculationRecord
from investment_agent.investor.common import ObjectRef, Quantity
from investment_agent.investor.method_packages import (
    CalculationRequirement,
    CounterRefsRequirement,
    HypothesesRequirement,
    MethodPackageDefinition,
    MethodStep,
    PlanRequirement,
)
from investment_agent.investor.methods import (
    MethodBinding,
    MethodConfig,
    MethodService,
    task_method_receipt,
)
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.agent import ToolAction
from investment_agent.storage.models.evidence import SavedCalculation, SavedEvidence
from investment_agent.storage.models.investor import MethodDefaultHead, TaskMethodBinding
from tests.support.conversations import (
    claim,
    message,
    new_conversation,
    submit,
)
from tests.support.conversations import (
    conversation_db as conversation_db,
)
from tests.support.publication import fixture
from tests.support.research import full_progress

EVIDENCE_STEPS = ("Plan the falsifiable research", "Attempt the reverse DCF valuation")


def _flow_package() -> MethodPackageDefinition:
    """One package with blocking coverage, a dependency and a gap path."""
    return MethodPackageDefinition(
        package_key="validation-flow",
        applicable_kinds=("company_research",),
        steps=(
            MethodStep(
                id="plan_step",
                title="Falsifiable plan",
                instruction_kind="falsifiable_plan",
                blocking=True,
                required_outputs=(
                    PlanRequirement(type="plan", require_completion_criteria=True, require_stop_conditions=True),
                    HypothesesRequirement(type="hypotheses", min_count=1),
                ),
            ),
            MethodStep(
                id="counter_step",
                title="Counter evidence",
                instruction_kind="counter_analysis",
                blocking=False,
                required_outputs=(CounterRefsRequirement(type="counter_refs", min_count=1, verified_only=True),),
            ),
            MethodStep(
                id="dcf_step",
                title="Reverse DCF",
                instruction_kind="calculation",
                blocking=True,
                depends_on=("plan_step",),
                allow_missing=True,
                required_outputs=(CalculationRequirement(type="calculation", method="reverse_dcf_fcfe"),),
            ),
        ),
    )


def _flow_binding(saved: dict[str, Any]) -> MethodBinding:
    return MethodBinding(
        schema_version=2,
        method_ref=ObjectRef(kind="method", id=uuid4(), revision=1),
        config=MethodConfig.model_validate(saved["discipline"]),
        package_ref=ObjectRef(kind="package", id=UUID(saved["artifact_id"]), revision=saved["version"]),
        package_hash=saved["content_hash"],
    )


async def _save_package(definition: MethodPackageDefinition) -> dict[str, Any]:
    async with session_scope() as session:
        return await MethodService(session).save_package(definition, source="validation-tests")


async def _admit_v2_task(subject: ObjectRef, package: MethodPackageDefinition | None = None) -> tuple[Any, Any]:
    """A real enforcing v2 admission through the default head, then its claim."""
    saved = await _save_package(package or _flow_package())
    async with session_scope() as session:
        session.add(MethodDefaultHead(
            owner_id="local_owner", channel="company-research", revision=1,
            payload=_flow_binding(saved).model_dump(mode="json")))
    receipt = await submit(await new_conversation(), message(text="深入研究", context_refs=(subject,), research_kind="company_research"))
    async with session_scope() as session:
        frozen = await task_method_receipt(session, receipt.task.id)
    assert frozen is not None and frozen.validator_version == "1"
    return receipt, await claim(receipt.task)


def _bindings() -> tuple[MethodPlanBinding, ...]:
    """First-plan mapping: both blocking steps plus the counter step (many-to-one)."""
    return (MethodPlanBinding(evidence_step_index=0, method_step_id="plan_step"),
            MethodPlanBinding(evidence_step_index=0, method_step_id="counter_step"),
            MethodPlanBinding(evidence_step_index=1, method_step_id="dcf_step"))


def _progress(dimensions: tuple, **updates: Any) -> MethodPlannedProgress:
    """A complete v2 progress over the flow package's two evidence steps."""
    plain = full_progress(dimensions, answered=False,
        stopped_because="The method plan still needs evidence before any conclusion.").model_copy(
        update={"evidence_steps": EVIDENCE_STEPS}).model_dump(mode="json")
    return MethodPlannedProgress.model_validate({**plain, "plan_bindings": _bindings(), **updates})


def _final(dimensions: tuple, **updates: Any) -> MethodPlannedProgress:
    return _progress(dimensions, **updates)


async def _commit_action(session: Any, trusted: Any, *, tool: str, business: dict[str, Any],
                         result: dict[str, Any], progress_artifact: dict[str, Any] | None = None) -> UUID:
    """One fabricated committed action through the real settle boundary.

    The v2 commit path numbers the action (committed_sequence) as a side
    effect; a progress_artifact registers the research_progress baseline in
    task artifact append order, exactly like the real save_progress tool.
    """
    action = await TaskService(session).prepare_action(trusted, intent_key=f"tool:{uuid4()}",
        parameters={"tool": tool, "business": business}, effect="database")
    refs = (ObjectRef(kind="research_progress", id=UUID(action.operation_id), revision=1),) if progress_artifact else ()

    async def write(owned_session: Any, operation: UUID) -> ActionResult:
        del owned_session, operation
        return ActionResult(result, artifact_refs=refs)

    await TaskService(session).settle_action(trusted, UUID(action.operation_id), write)
    return UUID(action.operation_id)


async def _commit_plan(session: Any, trusted: Any, planned: MethodPlannedProgress) -> UUID:
    return await _commit_action(session, trusted, tool="save_research_progress",
        business=planned.model_dump(mode="json"),
        result={"progress": planned.model_dump(mode="json"), "text": "研究进展已保存。", "refs": []},
        progress_artifact=True)


async def _commit_calculation(session: Any, trusted: Any, ref: ObjectRef, tool: str) -> UUID:
    return await _commit_action(session, trusted, tool=tool, business={"ticker": "600000.SH"},
        result={"status": "ok", "calculation_ref": ref.model_dump(mode="json")})


async def _saved_calculation(method: str, evidence: ObjectRef) -> ObjectRef:
    # One day back: the record must be visible at the task's observation cutoff.
    as_of = datetime.now(UTC) - timedelta(days=1)
    record = CalculationRecord(id=uuid4(), schema_version=1, revision=1, actor="fixture", origin_ref=None,
        created_at=as_of, method=method, method_version="fixture",
        fact_refs=(evidence,), assumption_refs=(), input_hash="fixture", as_of=as_of,
        applicable=True, status="ok", results={"percentile": Quantity(value="0.25", unit="fraction")})
    async with session_scope() as session:
        session.add(SavedCalculation(id=str(record.id), revision=1, owner_id="local_owner",
            payload=record.model_dump(mode="json")))
    return ObjectRef(kind="calculation", id=record.id, revision=1)


# --------------------------------------------------------------------------- #
# committed_sequence assignment (v2 only; v1 rows stay NULL)
# --------------------------------------------------------------------------- #


async def test_v2_commits_are_sequenced_and_v1_stay_null(conversation_db) -> None:
    candidate, _, subject = await fixture()
    # The v1 task must exist before any default head: admission resolves v1.
    v1 = await submit(await new_conversation(), message(text="深入研究", context_refs=(subject,), research_kind="company_research"))
    v1_trusted = await claim(v1.task)
    async with session_scope() as session:
        await _commit_action(session, v1_trusted, tool="get_snapshot", business={"as_of": "x"}, result={"status": "ok"})
        rows = (await session.scalars(select(ToolAction).where(ToolAction.task_id == str(v1.task.id)))).all()
        assert len(rows) == 1 and rows[0].committed_sequence is None
    receipt, trusted = await _admit_v2_task(subject)
    async with session_scope() as session:
        await _commit_action(session, trusted, tool="get_snapshot", business={"as_of": "1"}, result={"status": "ok"})
        await _commit_action(session, trusted, tool="get_snapshot", business={"as_of": "2"}, result={"status": "ok"})
        rows = (await session.scalars(select(ToolAction).where(ToolAction.task_id == str(receipt.task.id)))).all()
        assert sorted(action.committed_sequence for action in rows) == [1, 2]


# --------------------------------------------------------------------------- #
# First committed plan (final=False save path)
# --------------------------------------------------------------------------- #


async def test_first_plan_invalid_bindings_rejected(conversation_db) -> None:
    candidate, _, subject = await fixture()
    dimensions = candidate.thesis.assessed_dimensions
    receipt, _ = await _admit_v2_task(subject)
    task_id = receipt.task.id
    cases = (
        _progress(dimensions, plan_bindings=(MethodPlanBinding(evidence_step_index=5, method_step_id="plan_step"),)),
        _progress(dimensions, plan_bindings=(MethodPlanBinding(evidence_step_index=0, method_step_id="no_such_step"),)),
        _progress(dimensions, plan_bindings=(MethodPlanBinding(evidence_step_index=0, method_step_id="plan_step"),
                                             MethodPlanBinding(evidence_step_index=0, method_step_id="plan_step"))),
    )
    async with session_scope() as session:
        for planned in cases:
            with pytest.raises(ExecutionDeniedError, match="method_plan_binding_invalid"):
                await validate_progress(session, task_id, planned)


async def test_first_plan_blocking_coverage_and_premature_results(conversation_db) -> None:
    candidate, _, subject = await fixture()
    dimensions = candidate.thesis.assessed_dimensions
    receipt, _ = await _admit_v2_task(subject)
    task_id = receipt.task.id
    uncovered = _progress(dimensions, plan_bindings=(MethodPlanBinding(evidence_step_index=0, method_step_id="plan_step"),))
    premature = _progress(dimensions, step_results=(
        MethodStepResult(method_step_id="plan_step", evidence_step_indices=(0,), status="satisfied", hypothesis_indices=(0,)),))
    valid = _progress(dimensions)
    async with session_scope() as session:
        with pytest.raises(ExecutionDeniedError, match="method_step_required"):
            await validate_progress(session, task_id, uncovered)
        with pytest.raises(ExecutionDeniedError, match="method_step_result_premature"):
            await validate_progress(session, task_id, premature)
        text, refs = await validate_progress(session, task_id, valid)
        assert text and refs == ()


# --------------------------------------------------------------------------- #
# Final settlement: unrelated calculation, self-reported satisfaction
# --------------------------------------------------------------------------- #


async def test_unrelated_calculation_cannot_satisfy_reverse_dcf(conversation_db) -> None:
    candidate, evidence, subject = await fixture()
    dimensions = candidate.thesis.assessed_dimensions
    receipt, trusted = await _admit_v2_task(subject)
    percentile = await _saved_calculation("historical_valuation_percentile", evidence)
    async with session_scope() as session:
        await _commit_plan(session, trusted, _progress(dimensions))
        percentile_operation = await _commit_calculation(session, trusted, percentile, "historical_valuation_percentile")
    final = _final(dimensions, fact_refs=(percentile,), step_results=(
        MethodStepResult(method_step_id="plan_step", evidence_step_indices=(0,), status="satisfied", hypothesis_indices=(0,)),
        MethodStepResult(method_step_id="dcf_step", evidence_step_indices=(1,), status="satisfied",
            action_ids=(percentile_operation,), output_refs=(percentile,))))
    async with session_scope() as session:
        with pytest.raises(ExecutionDeniedError, match="method_step_output_required"):
            await validate_progress(session, receipt.task.id, final, final=True)


async def test_self_reported_satisfied_without_actions_or_refs_rejected(conversation_db) -> None:
    candidate, _, subject = await fixture()
    dimensions = candidate.thesis.assessed_dimensions
    receipt, trusted = await _admit_v2_task(subject)
    async with session_scope() as session:
        await _commit_plan(session, trusted, _progress(dimensions))
    final = _final(dimensions, step_results=(
        MethodStepResult(method_step_id="plan_step", evidence_step_indices=(0,), status="satisfied", hypothesis_indices=(0,)),
        MethodStepResult(method_step_id="dcf_step", evidence_step_indices=(1,), status="satisfied")))
    async with session_scope() as session:
        with pytest.raises(ExecutionDeniedError, match="method_step_output_required"):
            await validate_progress(session, receipt.task.id, final, final=True)


async def test_required_calculation_with_generating_action_settles(conversation_db) -> None:
    """The positive M2 example: the exact method plus its generating action passes."""
    candidate, evidence, subject = await fixture()
    dimensions = candidate.thesis.assessed_dimensions
    receipt, trusted = await _admit_v2_task(subject)
    dcf = await _saved_calculation("reverse_dcf_fcfe", evidence)
    async with session_scope() as session:
        plan_operation = await _commit_plan(session, trusted, _progress(dimensions))
        calc_operation = await _commit_calculation(session, trusted, dcf, "reverse_dcf_fcfe")
    final = _final(dimensions, fact_refs=(dcf,), step_results=(
        MethodStepResult(method_step_id="plan_step", evidence_step_indices=(0,), status="satisfied",
            hypothesis_indices=(0,), action_ids=(plan_operation,)),
        MethodStepResult(method_step_id="dcf_step", evidence_step_indices=(1,), status="satisfied",
            action_ids=(calc_operation,), output_refs=(dcf,))))
    async with session_scope() as session:
        _, refs = await validate_progress(session, receipt.task.id, final, final=True)
        assert dcf in refs


# --------------------------------------------------------------------------- #
# Dependency order (§6.2 item 5)
# --------------------------------------------------------------------------- #


def _ordered_final(dimensions: tuple, dcf: ObjectRef, plan_operation: UUID, calc_operation: UUID) -> MethodPlannedProgress:
    return _final(dimensions, fact_refs=(dcf,), step_results=(
        MethodStepResult(method_step_id="plan_step", evidence_step_indices=(0,), status="satisfied",
            hypothesis_indices=(0,), action_ids=(plan_operation,)),
        MethodStepResult(method_step_id="dcf_step", evidence_step_indices=(1,), status="satisfied",
            action_ids=(calc_operation,), output_refs=(dcf,))))


async def test_dependency_order_violation_and_not_replayable(conversation_db) -> None:
    candidate, evidence, subject = await fixture()
    dimensions = candidate.thesis.assessed_dimensions
    receipt, trusted = await _admit_v2_task(subject)
    dcf = await _saved_calculation("reverse_dcf_fcfe", evidence)
    async with session_scope() as session:
        # The calculation commits BEFORE the plan: prerequisite max >= follower min.
        calc_operation = await _commit_calculation(session, trusted, dcf, "reverse_dcf_fcfe")
        plan_operation = await _commit_plan(session, trusted, _progress(dimensions))
    final = _ordered_final(dimensions, dcf, plan_operation, calc_operation)
    async with session_scope() as session:
        with pytest.raises(ExecutionDeniedError, match="method_step_order_violation"):
            await validate_progress(session, receipt.task.id, final, final=True)
        # A committed action without committed_sequence is not_replayable: the
        # order requirement counts as failed, never as passed.
        await session.execute(update(ToolAction)
            .where(ToolAction.operation_id == str(calc_operation)).values(committed_sequence=None))
        with pytest.raises(ExecutionDeniedError, match="method_step_order_violation"):
            await validate_progress(session, receipt.task.id, final, final=True)


async def test_dependency_order_satisfied_in_commit_order(conversation_db) -> None:
    candidate, evidence, subject = await fixture()
    dimensions = candidate.thesis.assessed_dimensions
    receipt, trusted = await _admit_v2_task(subject)
    dcf = await _saved_calculation("reverse_dcf_fcfe", evidence)
    async with session_scope() as session:
        plan_operation = await _commit_plan(session, trusted, _progress(dimensions))
        calc_operation = await _commit_calculation(session, trusted, dcf, "reverse_dcf_fcfe")
    final = _ordered_final(dimensions, dcf, plan_operation, calc_operation)
    async with session_scope() as session:
        await validate_progress(session, receipt.task.id, final, final=True)


# --------------------------------------------------------------------------- #
# Gap close paths (§6.2 item 6)
# --------------------------------------------------------------------------- #


async def test_gap_close_requires_reason_mention_and_attempt_action(conversation_db) -> None:
    candidate, evidence, subject = await fixture()
    dimensions = candidate.thesis.assessed_dimensions
    receipt, trusted = await _admit_v2_task(subject)
    async with session_scope() as session:
        plan_operation = await _commit_plan(session, trusted, _progress(dimensions))
    percentile = await _saved_calculation("historical_valuation_percentile", evidence)
    async with session_scope() as session:
        attempt = await _commit_calculation(session, trusted, percentile, "historical_valuation_percentile")
    reason = "Reverse DCF inputs stayed unavailable within the admitted scope"

    def gap_final(result_updates: dict[str, Any], progress_updates: dict[str, Any]) -> MethodPlannedProgress:
        return _final(dimensions, gaps=progress_updates.get("gaps", ()), limitations=progress_updates.get("limitations", ()),
            step_results=(
                MethodStepResult(method_step_id="plan_step", evidence_step_indices=(0,), status="satisfied",
                    hypothesis_indices=(0,), action_ids=(plan_operation,)),
                MethodStepResult(method_step_id="dcf_step", evidence_step_indices=(1,), status="completed_with_gap",
                    action_ids=(attempt,), **result_updates)))

    async with session_scope() as session:
        # allow_missing step, real attempt, reason and a matching gaps entry: passes.
        await validate_progress(session, receipt.task.id, gap_final({"gap_reason": reason}, {"gaps": (reason,)}), final=True)
        # No matching gaps/limitations explanation: refused.
        with pytest.raises(ExecutionDeniedError, match="method_step_gap_invalid"):
            await validate_progress(session, receipt.task.id, gap_final({"gap_reason": reason}, {}), final=True)
        # Missing gap_reason: refused.
        with pytest.raises(ExecutionDeniedError, match="method_step_gap_invalid"):
            await validate_progress(session, receipt.task.id, gap_final({}, {"gaps": (reason,)}), final=True)
        # No retained attempt action for an action-backed step: refused.
        without_attempt = _final(dimensions, gaps=(reason,), step_results=(
            MethodStepResult(method_step_id="plan_step", evidence_step_indices=(0,), status="satisfied",
                hypothesis_indices=(0,), action_ids=(plan_operation,)),
            MethodStepResult(method_step_id="dcf_step", evidence_step_indices=(1,), status="completed_with_gap",
                gap_reason=reason)))
        with pytest.raises(ExecutionDeniedError, match="method_step_gap_invalid"):
            await validate_progress(session, receipt.task.id, without_attempt, final=True)


async def test_gap_close_refused_without_allow_missing(conversation_db) -> None:
    candidate, evidence, subject = await fixture()
    dimensions = candidate.thesis.assessed_dimensions
    package = _flow_package()
    strict = package.model_copy(update={"steps": tuple(
        step.model_copy(update={"allow_missing": False}) if step.id == "dcf_step" else step for step in package.steps)})
    receipt, trusted = await _admit_v2_task(subject, strict)
    percentile = await _saved_calculation("historical_valuation_percentile", evidence)
    async with session_scope() as session:
        plan_operation = await _commit_plan(session, trusted, _progress(dimensions))
        attempt = await _commit_calculation(session, trusted, percentile, "historical_valuation_percentile")
    reason = "Reverse DCF inputs stayed unavailable within the admitted scope"
    final = _final(dimensions, gaps=(reason,), step_results=(
        MethodStepResult(method_step_id="plan_step", evidence_step_indices=(0,), status="satisfied",
            hypothesis_indices=(0,), action_ids=(plan_operation,)),
        MethodStepResult(method_step_id="dcf_step", evidence_step_indices=(1,), status="completed_with_gap",
            action_ids=(attempt,), gap_reason=reason)))
    async with session_scope() as session:
        with pytest.raises(ExecutionDeniedError, match="method_step_gap_invalid"):
            await validate_progress(session, receipt.task.id, final, final=True)


# --------------------------------------------------------------------------- #
# Plan keeping and verified-only counter evidence
# --------------------------------------------------------------------------- #


async def test_changed_final_plan_rejected(conversation_db) -> None:
    candidate, _, subject = await fixture()
    dimensions = candidate.thesis.assessed_dimensions
    receipt, trusted = await _admit_v2_task(subject)
    async with session_scope() as session:
        await _commit_plan(session, trusted, _progress(dimensions))
    changed = _final(dimensions, evidence_steps=EVIDENCE_STEPS[:1] + ("A rewritten second step",))
    async with session_scope() as session:
        with pytest.raises(ExecutionDeniedError, match="method_plan_changed"):
            await validate_progress(session, receipt.task.id, changed, final=True)


async def test_counter_refs_verified_only_path(conversation_db) -> None:
    candidate, evidence, subject = await fixture()
    dimensions = candidate.thesis.assessed_dimensions
    receipt, trusted = await _admit_v2_task(subject)
    async with session_scope() as session:
        plan_operation = await _commit_plan(session, trusted, _progress(dimensions))
    dcf = await _saved_calculation("reverse_dcf_fcfe", evidence)
    async with session_scope() as session:
        calc_operation = await _commit_calculation(session, trusted, dcf, "reverse_dcf_fcfe")

    def final_with_counter() -> MethodPlannedProgress:
        return _final(dimensions, fact_refs=(dcf,), counter_refs=(evidence,), step_results=(
            MethodStepResult(method_step_id="plan_step", evidence_step_indices=(0,), status="satisfied",
                hypothesis_indices=(0,), action_ids=(plan_operation,)),
            MethodStepResult(method_step_id="counter_step", evidence_step_indices=(0,), status="satisfied",
                output_refs=(evidence,)),
            MethodStepResult(method_step_id="dcf_step", evidence_step_indices=(1,), status="satisfied",
                action_ids=(calc_operation,), output_refs=(dcf,))))

    async with session_scope() as session:
        # The fixture evidence is verified without unresolved conflicts: passes.
        await validate_progress(session, receipt.task.id, final_with_counter(), final=True)
        row = await session.get(SavedEvidence, (str(evidence.id), evidence.revision))
        await session.execute(update(SavedEvidence).where(SavedEvidence.id == str(evidence.id))
            .values(payload={**row.payload, "verification": "candidate"}))
        with pytest.raises(ExecutionDeniedError, match="method_step_output_required"):
            await validate_progress(session, receipt.task.id, final_with_counter(), final=True)


# --------------------------------------------------------------------------- #
# v1 tasks and render-only receipts never trigger package validation
# --------------------------------------------------------------------------- #


async def test_v1_task_and_render_only_receipt_skip_package_validation(conversation_db) -> None:
    candidate, _, subject = await fixture()
    dimensions = candidate.thesis.assessed_dimensions
    # v1 task: no default head, so no receipt exists; a v2-shaped settlement
    # without any baseline would fail under enforcing but must not be judged.
    v1 = await submit(await new_conversation(), message(text="深入研究", context_refs=(subject,), research_kind="company_research"))
    async with session_scope() as session:
        assert await task_method_receipt(session, v1.task.id) is None
        await validate_progress(session, v1.task.id, _final(dimensions, step_results=(
            MethodStepResult(method_step_id="plan_step", evidence_step_indices=(0,), status="satisfied", hypothesis_indices=(0,)),
            MethodStepResult(method_step_id="dcf_step", evidence_step_indices=(1,), status="satisfied"))), final=True)
    # Historical in-flight M1 receipt: render-only, so even a final with a
    # committed baseline and no step results is not package-judged.
    saved = await _save_package(_flow_package())
    async with session_scope() as session:
        session.add(MethodDefaultHead(
            owner_id="local_owner", channel="company-research", revision=1,
            payload=_flow_binding(saved).model_dump(mode="json")))
    render_only = await submit(await new_conversation(), message(text="深入研究", context_refs=(subject,), research_kind="company_research"))
    render_trusted = await claim(render_only.task)
    async with session_scope() as session:
        frozen = await task_method_receipt(session, render_only.task.id)
        assert frozen is not None and frozen.validator_version == "1"
        binding = _flow_binding(saved)
        receipt = build_receipt(binding, _flow_package(), validator_version=VALIDATOR_RENDER_ONLY)
        await session.execute(update(TaskMethodBinding).where(TaskMethodBinding.task_id == str(render_only.task.id))
            .values(payload={"schema_version": 2, "receipt": receipt.model_dump(mode="json")}))
        await _commit_plan(session, render_trusted, _progress(dimensions))
    async with session_scope() as session:
        assert await task_method_receipt(session, render_only.task.id) is not None
        await validate_progress(session, render_only.task.id, _final(dimensions), final=True)


# --------------------------------------------------------------------------- #
# Repairable classification
# --------------------------------------------------------------------------- #


def test_repairable_whitelist_matches_card_07_codes() -> None:
    from investment_agent.agent.execution import ResearchValidationError

    for code in ("method_plan_binding_invalid", "method_step_required", "method_step_output_required",
                 "method_step_order_violation", "method_plan_changed", "method_step_gap_invalid",
                 "method_step_result_premature"):
        assert ResearchValidationError(code, "progress.x", "m").repairable
    for code in ("method_action_invalid", "method_capability_unavailable"):
        assert not ResearchValidationError(code, "progress.x", "m").repairable
