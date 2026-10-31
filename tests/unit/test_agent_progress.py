"""Card 08: PlanView method coverage projection (§9).

The v1 projection stays exactly what it was: ``method=None``, empty
``method_step_ids``, unchanged step states and ``extra_completed``. The v2
projection (any frozen receipt, §9) reads the first committed save for plan
bindings, the latest committed save for step results, and derives every
package step's coverage state - including a blocking step outside the plan,
which stays visible as ``uncovered`` while the task runs. Such a plan can
only reach storage under a render-only receipt (an enforcing first plan must
cover every blocking step), which is the historical M1 in-flight shape.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import update

from investment_agent.agent.method_receipt import VALIDATOR_RENDER_ONLY, build_receipt
from investment_agent.agent.progress import task_progress
from investment_agent.agent.research import (
    MethodPlanBinding,
    MethodPlannedProgress,
    MethodStepResult,
    ResearchProgress,
)
from investment_agent.agent.tasks import ActionResult, TaskService
from investment_agent.investor.common import ObjectRef
from investment_agent.investor.enums import StopReason, TaskStatus
from investment_agent.investor.method_packages import (
    HypothesesRequirement,
    MethodPackageDefinition,
    MethodStep,
    PlanRequirement,
)
from investment_agent.investor.methods import MethodBinding, MethodConfig, MethodService
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.investor import TaskMethodBinding
from tests.support.conversations import (
    base,
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


def _coverage_package() -> MethodPackageDefinition:
    """Five package steps that cover every §9 display state between them."""
    return MethodPackageDefinition(
        package_key="planview-coverage",
        applicable_kinds=("company_research",),
        steps=(
            MethodStep(id="plan_step", title="Falsifiable plan", instruction_kind="falsifiable_plan",
                blocking=True,
                required_outputs=(
                    PlanRequirement(type="plan", require_completion_criteria=True, require_stop_conditions=True),
                    HypothesesRequirement(type="hypotheses", min_count=1))),
            MethodStep(id="gap_step", title="Gap path", instruction_kind="counter_analysis",
                blocking=True, allow_missing=True),
            MethodStep(id="missing_step", title="Never settled", instruction_kind="dimension_assessment",
                blocking=True),
            MethodStep(id="counter_step", title="Counter evidence", instruction_kind="counter_analysis",
                blocking=False),
            MethodStep(id="orphan_step", title="Never planned", instruction_kind="snapshot_capture",
                blocking=True),
        ),
    )


def _binding(saved: dict[str, Any]) -> MethodBinding:
    return MethodBinding(
        schema_version=2,
        method_ref=ObjectRef(kind="method", id=uuid4(), revision=1),
        config=MethodConfig.model_validate(saved["discipline"]),
        package_ref=ObjectRef(kind="package", id=UUID(saved["artifact_id"]), revision=saved["version"]),
        package_hash=saved["content_hash"],
    )


async def _save_package(definition: MethodPackageDefinition) -> dict[str, Any]:
    async with session_scope() as session:
        return await MethodService(session).save_package(definition, source="planview-tests")


async def _v2_task(subject: ObjectRef, package: MethodPackageDefinition) -> tuple[Any, Any, dict[str, Any]]:
    """A claimed company research task carrying an injected v2 (render-only) receipt.

    Admission froze the v1 baseline payload; the Core update rewrites it to the
    render-only receipt shape without touching the append-only ORM listener
    (the same historical M1 in-flight rewrite as test_method_validation.py).
    """
    saved = await _save_package(package)
    submitted = await submit(await new_conversation(),
        message(text="深入研究", context_refs=(subject,), research_kind="company_research"))
    trusted = await claim(submitted.task)
    receipt = build_receipt(_binding(saved), package, validator_version=VALIDATOR_RENDER_ONLY)
    async with session_scope() as session:
        await session.execute(update(TaskMethodBinding)
            .where(TaskMethodBinding.task_id == str(submitted.task.id))
            .values(payload={"schema_version": 2, "receipt": receipt.model_dump(mode="json")}))
    return submitted.task, trusted, saved


async def _commit_progress(trusted: Any, progress: ResearchProgress) -> None:
    """One fabricated committed save_research_progress through the real settle
    boundary: the progress artifact lands in task artifact append order."""
    async with session_scope() as session:
        action = await TaskService(session).prepare_action(trusted, intent_key=f"tool:{uuid4()}",
            parameters={"tool": "save_research_progress", "business": progress.model_dump(mode="json")},
            effect="database")
        refs = (ObjectRef(kind="research_progress", id=UUID(action.operation_id), revision=1),)

        async def write(owned_session: Any, operation: UUID) -> ActionResult:
            del owned_session, operation
            return ActionResult({"progress": progress.model_dump(mode="json"), "text": "研究进展已保存。", "refs": []},
                artifact_refs=refs)

        await TaskService(session).settle_action(trusted, UUID(action.operation_id), write)


def _planned_progress(dimensions: tuple, **updates: Any) -> MethodPlannedProgress:
    plain = full_progress(dimensions).model_copy(
        update={"evidence_steps": EVIDENCE_STEPS, "completed_steps": (EVIDENCE_STEPS[0],)}).model_dump(mode="json")
    return MethodPlannedProgress.model_validate({**plain, **updates})


def _first_plan_bindings() -> tuple[MethodPlanBinding, ...]:
    """Four of the five package steps mapped; blocking orphan_step stays out."""
    return (MethodPlanBinding(evidence_step_index=0, method_step_id="plan_step"),
            MethodPlanBinding(evidence_step_index=0, method_step_id="gap_step"),
            MethodPlanBinding(evidence_step_index=1, method_step_id="missing_step"),
            MethodPlanBinding(evidence_step_index=1, method_step_id="counter_step"))


# --------------------------------------------------------------------------- #
# v1 projection: exactly the previous view
# --------------------------------------------------------------------------- #


async def test_v1_plan_view_is_unchanged(conversation_db) -> None:
    candidate, _, subject = await fixture()
    submitted = await submit(await new_conversation(),
        message(text="深入研究", context_refs=(subject,), research_kind="company_research"))
    trusted = await claim(submitted.task)
    await _commit_progress(trusted, full_progress(candidate.thesis.assessed_dimensions).model_copy(
        update={"evidence_steps": ("Check reported cash and opposing explanations", "Assess the opposing explanation")}))
    async with session_scope() as session:
        view = await task_progress(session, submitted.task.id, base())
    assert view is not None and view.plan is not None
    plan = view.plan
    assert plan.method is None
    assert all(step.method_step_ids == () for step in plan.steps)
    # Running task: the first uncompleted plan item is "next"; the completed
    # text outside the plan stays in extra_completed.
    assert [step.state for step in plan.steps] == ["next", "pending"]
    assert plan.extra_completed == ("Clarified scope and alternatives",)
    assert plan.question == "Explain the bounded question"
    assert plan.completion_criteria == ("Explain scope and material gaps",)
    assert plan.stop_conditions == ("Stop when critical source unavailable",)
    assert plan.saved_at is not None and plan.saved_at_unknown_reason is None


# --------------------------------------------------------------------------- #
# v2 projection: coverage from first plan plus latest results
# --------------------------------------------------------------------------- #


async def test_v2_method_coverage_projection(conversation_db) -> None:
    candidate, _, subject = await fixture()
    dimensions = candidate.thesis.assessed_dimensions
    task, trusted, saved = await _v2_task(subject, _coverage_package())
    await _commit_progress(trusted, _planned_progress(dimensions, plan_bindings=_first_plan_bindings()))

    # While the task runs, mapped steps are planned and the blocking step
    # outside the plan shows uncovered.
    async with session_scope() as session:
        running = await task_progress(session, task.id, base())
    assert running is not None and running.plan is not None and running.plan.method is not None
    method = running.plan.method
    assert method.package_key == "planview-coverage"
    assert method.version == saved["version"]
    assert method.content_hash == saved["content_hash"]
    assert method.discipline == MethodConfig.model_validate(saved["discipline"])
    assert [(step.method_step_id, step.status) for step in method.steps] == [
        ("plan_step", "planned"),
        ("gap_step", "planned"),
        ("missing_step", "planned"),
        ("counter_step", "planned"),
        ("orphan_step", "uncovered"),
    ]
    assert [step.method_step_ids for step in running.plan.steps] == [
        ("plan_step", "gap_step"), ("missing_step", "counter_step")]
    assert [step.state for step in running.plan.steps] == ["done", "next"]

    # A latest save with partial step results, then the task fails: the
    # satisfied/gap results decide their own status, the blocking step left
    # without a result is missing (the orphan included), the non-blocking
    # uncovered step stays planned.
    await _commit_progress(trusted, _planned_progress(dimensions, plan_bindings=_first_plan_bindings(),
        step_results=(
            MethodStepResult(method_step_id="plan_step", evidence_step_indices=(0,), status="satisfied"),
            MethodStepResult(method_step_id="gap_step", evidence_step_indices=(0,), status="completed_with_gap",
                gap_reason="The counter-evidence pass stayed incomplete within the admitted scope"),
        )))
    async with session_scope() as session:
        failed = await TaskService(session).checkpoint(trusted, checkpoint={},
            terminal="failed", stop_reason=StopReason.FAILED)
    assert failed.status == TaskStatus.FAILED
    async with session_scope() as session:
        settled = await task_progress(session, task.id, base())
    assert settled is not None and settled.plan is not None and settled.plan.method is not None
    assert [(step.method_step_id, step.status) for step in settled.plan.method.steps] == [
        ("plan_step", "satisfied"),
        ("gap_step", "completed_with_gap"),
        ("missing_step", "missing"),
        ("counter_step", "planned"),
        ("orphan_step", "missing"),
    ]
    # A failed task never assigns "next".
    assert [step.state for step in settled.plan.steps] == ["done", "pending"]
    assert [step.method_step_ids for step in settled.plan.steps] == [
        ("plan_step", "gap_step"), ("missing_step", "counter_step")]
