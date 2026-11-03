"""Read-only task progress projection; never fences, claims or charges.

The GET routes own one real read transaction and call into this module inside
it. Saved plans and usage stay readable after the Agent is switched off, the
mandate is replaced or the authorization expires: only current runtime settings
and the saved grant are read, and missing data degrades to explicit nulls with
stable reasons instead of failing the projection.
"""

import logging
import unicodedata
from datetime import UTC, datetime, timedelta
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import ColumnElement, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.agent.budget import BudgetService
from investment_agent.agent.contracts import (
    CoverageStatus,
    MethodCoverage,
    MethodCoverageStep,
    PlanStep,
    PlanView,
    Task,
    TaskLimits,
    TaskProgress,
    Usage,
    rebuild_method_coverage,
)
from investment_agent.agent.execution import ExecutionGrant
from investment_agent.agent.method_receipt import TaskMethodReceipt
from investment_agent.agent.research import (
    MethodPlanBinding,
    MethodPlannedProgress,
    MethodStepResult,
    ResearchProgress,
)
from investment_agent.agent.tasks import COMPLETED_TASK_STATUSES, TERMINAL, TaskService
from investment_agent.config import Settings
from investment_agent.investor.common import ObjectRef
from investment_agent.investor.enums import TaskStatus
from investment_agent.investor.foundations import InvestorError
from investment_agent.investor.method_packages import MethodPackage, MethodStep
from investment_agent.investor.methods import MethodService, task_method_receipt
from investment_agent.settings.loader import load_settings
from investment_agent.storage.models.agent import (
    BudgetEntry,
    ExecutionGrantRow,
    PublicTaskEvent,
    ToolAction,
)

_log = logging.getLogger(__name__)

EVENT_HISTORY_PRUNED = "event_history_pruned"

# The coverage contract resolves MethodConfig lazily (import-cycle note on
# rebuild_method_coverage); this module already sits above investor.methods.
rebuild_method_coverage()


def normalize_step(text: str) -> str:
    return unicodedata.normalize("NFC", text).strip()


def stored_time(value: datetime) -> datetime:
    """SQLite DateTime drops the offset: stored event times are UTC wall time."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


async def _event_occurred_at(session: AsyncSession, task_id: UUID, *, event_type: str,
                             matches: ColumnElement[bool] | None = None) -> datetime | None:
    query = select(PublicTaskEvent.occurred_at).where(
        PublicTaskEvent.task_id == str(task_id),
        PublicTaskEvent.payload["type"].as_string() == event_type,
    )
    if matches is not None:
        query = query.where(matches)
    # The latest matching event wins (explicit order, never table order): the
    # terminal state and each artifact save are the last event of their kind.
    occurred = await session.scalar(query.order_by(PublicTaskEvent.event_id.desc()))
    return stored_time(occurred) if occurred is not None else None


async def _saved_at(session: AsyncSession, task_id: UUID, ref: ObjectRef) -> tuple[datetime | None, str | None]:
    occurred = await _event_occurred_at(session, task_id, event_type="artifact_saved",
        matches=PublicTaskEvent.payload["payload"]["ref"]["id"].as_string() == str(ref.id))
    if occurred is None:
        return None, EVENT_HISTORY_PRUNED
    return stored_time(occurred), None


async def committed_progress[T: ResearchProgress](
    session: AsyncSession, task: Task, model: type[T]
) -> list[tuple[ObjectRef, T]]:
    """Committed research progress artifacts in task artifact append order.

    The one traversal shared by the read-only projection and the §6.2
    first-plan search: rows that no longer parse are skipped, never guessed,
    and SQL order is never consulted.
    """
    found: list[tuple[ObjectRef, T]] = []
    for ref in task.artifact_refs:
        if ref.kind != "research_progress":
            continue
        action = await session.get(ToolAction, str(ref.id))
        if (action is None or action.task_id != str(task.id) or action.state != "committed"
                or not isinstance(action.result, dict)):
            continue
        try:
            found.append((ref, model.model_validate(action.result["progress"])))
        except (KeyError, ValidationError):
            continue
    return found


def _step_id_map(bindings: tuple[MethodPlanBinding, ...]) -> dict[int, tuple[str, ...]]:
    """Evidence-step index -> the method steps mapped to it, in binding order."""
    mapped: dict[int, list[str]] = {}
    for binding in bindings:
        mapped.setdefault(binding.evidence_step_index, []).append(binding.method_step_id)
    return {index: tuple(step_ids) for index, step_ids in mapped.items()}


def _coverage_status(step: MethodStep, covered: set[str], settled: dict[str, CoverageStatus],
                     *, settled_task: bool) -> CoverageStatus:
    """§9 display state of one package step.

    A result decides its own status; a blocking step left without a result in
    a settled task (succeeded/partial/failed) is missing; everything else is
    planned when the first plan covered it and uncovered otherwise, so a
    blocking step outside the plan stays visible.
    """
    if step.id in settled:
        return settled[step.id]
    if settled_task and step.blocking:
        return "missing"
    return "planned" if step.id in covered else "uncovered"


async def _method_coverage(
    session: AsyncSession,
    grant: ExecutionGrant,
    receipt: TaskMethodReceipt,
    bindings: tuple[MethodPlanBinding, ...],
    results: tuple[MethodStepResult, ...],
    *,
    settled_task: bool,
) -> MethodCoverage | None:
    """§9 method block from the frozen package behind the receipt binding.

    Display-only projection: a package artifact that no longer loads degrades
    to ``None`` instead of failing the whole progress view.
    """
    assert receipt.binding.package_ref is not None
    try:
        package = MethodPackage.model_validate(
            await MethodService(session, grant.owner).package_by_ref(receipt.binding.package_ref))
    except InvestorError:
        _log.warning("method_coverage_unavailable package_ref=%s", receipt.binding.package_ref.id)
        return None
    covered = {binding.method_step_id for binding in bindings}
    settled: dict[str, CoverageStatus] = {result.method_step_id: result.status for result in results}
    steps = tuple(MethodCoverageStep(method_step_id=step.id, blocking=step.blocking,
        status=_coverage_status(step, covered, settled, settled_task=settled_task))
        for step in package.steps)
    return MethodCoverage(package_key=package.package_key, version=package.version,
        content_hash=package.content_hash, discipline=package.discipline, steps=steps)


async def _plan_view(session: AsyncSession, task: Task, grant: ExecutionGrant) -> PlanView | None:
    """Latest committed research_progress by artifact append order, never SQL order.

    v2 tasks (a frozen admission receipt) additionally project §9 method
    coverage: plan bindings come from the first committed save, step results
    from the latest one. v1 tasks keep the exact previous projection with
    ``method=None`` and empty ``method_step_ids``.
    """
    receipt = await task_method_receipt(session, task.id)
    method: MethodCoverage | None = None
    mapped: dict[int, tuple[str, ...]] = {}
    latest: tuple[ObjectRef, ResearchProgress] | None
    if receipt is None:
        saves = await committed_progress(session, task, ResearchProgress)
        latest = saves[-1] if saves else None
    else:
        method_saves = await committed_progress(session, task, MethodPlannedProgress)
        latest = method_saves[-1] if method_saves else None
        if method_saves:
            first_bindings = method_saves[0][1].plan_bindings
            mapped = _step_id_map(first_bindings)
            method = await _method_coverage(session, grant, receipt, first_bindings,
                method_saves[-1][1].step_results,
                settled_task=task.status.value in COMPLETED_TASK_STATUSES)
    if latest is None:
        return None
    ref, progress = latest
    completed = {normalize_step(step) for step in progress.completed_steps}
    planned = {normalize_step(step) for step in progress.evidence_steps}
    if not planned:
        return None
    steps: list[PlanStep] = []
    next_assigned = False
    for index, step in enumerate(progress.evidence_steps):
        method_step_ids = mapped.get(index, ())
        if normalize_step(step) in completed:
            steps.append(PlanStep(text=step, state="done", method_step_ids=method_step_ids))
            continue
        # "next" marks the next uncompleted plan item while the task runs
        # uncancelled; it never claims a tool is executing this step now.
        if not next_assigned and task.status == TaskStatus.RUNNING and not task.cancel_requested:
            next_assigned = True
            steps.append(PlanStep(text=step, state="next", method_step_ids=method_step_ids))
        else:
            steps.append(PlanStep(text=step, state="pending", method_step_ids=method_step_ids))
    saved_at, reason = await _saved_at(session, task.id, ref)
    return PlanView(ref=ref, question=progress.question, saved_at=saved_at,
        saved_at_unknown_reason=reason, steps=tuple(steps),
        extra_completed=tuple(step for step in progress.completed_steps
            if normalize_step(step) not in planned),
        completion_criteria=progress.completion_criteria, stop_conditions=progress.stop_conditions,
        method=method)


async def _elapsed(session: AsyncSession, task: Task,
                   now: datetime) -> tuple[int | None, str | None]:
    """Queuing and waiting count; terminal duration freezes at the final state."""
    if task.status.value not in TERMINAL:
        return max(0, int((now - task.created_at).total_seconds())), None
    occurred = await _event_occurred_at(session, task.id, event_type="task_state",
        matches=PublicTaskEvent.payload["payload"]["status"].as_string().in_(sorted(TERMINAL)))
    if occurred is None:
        return None, EVENT_HISTORY_PRUNED
    return max(0, int((stored_time(occurred) - task.created_at).total_seconds())), None


async def task_progress(session: AsyncSession, task_id: UUID, base: Settings) -> TaskProgress | None:
    """None means the task has no saved execution grant (404 or progress=null)."""
    task = await TaskService(session).get_task(task_id)
    saved = await session.get(ExecutionGrantRow, str(task_id))
    if saved is None:
        return None
    grant = ExecutionGrant.model_validate(saved.payload)
    settings = await load_settings(session, base=base)
    now = datetime.now(UTC)
    day = BudgetService.day()
    rows = (await session.scalars(select(BudgetEntry).where(
        BudgetEntry.owner_id == "local_owner", BudgetEntry.state != "released",
        or_(BudgetEntry.task_id == str(task_id), BudgetEntry.day == day)))).all()
    headroom = BudgetService.headroom(grant.research_kind,
        [row for row in rows if row.task_id == str(task_id)],
        [row for row in rows if row.day == day], settings)
    elapsed, elapsed_reason = await _elapsed(session, task, now)
    limits = TaskLimits(model_requests=settings.investor_agent_task_requests,
        tool_calls=settings.investor_agent_task_tool_calls, tokens=settings.investor_agent_task_tokens,
        seconds=settings.investor_agent_task_seconds,
        finish_only=False if task.status.value in TERMINAL else bool(headroom["finish_only"]))
    usage: Usage = task.usage
    return TaskProgress(task_id=task.id, task_revision=task.revision, research_kind=grant.research_kind,
        as_of=now, plan=await _plan_view(session, task, grant), limits=limits, usage=usage,
        elapsed_seconds=elapsed, elapsed_unknown_reason=elapsed_reason,
        deadline=task.created_at + timedelta(seconds=settings.investor_agent_task_seconds))
