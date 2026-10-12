"""Read-only task progress projection; never fences, claims or charges.

The GET routes own one real read transaction and call into this module inside
it. Saved plans and usage stay readable after the Agent is switched off, the
mandate is replaced or the authorization expires: only current runtime settings
and the saved grant are read, and missing data degrades to explicit nulls with
stable reasons instead of failing the projection.
"""

import unicodedata
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import ColumnElement, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.agent.budget import BudgetService
from investment_agent.agent.contracts import (
    PlanStep,
    PlanView,
    Task,
    TaskLimits,
    TaskProgress,
    Usage,
)
from investment_agent.agent.execution import ExecutionGrant
from investment_agent.agent.research import ResearchProgress
from investment_agent.agent.tasks import TERMINAL, TaskService
from investment_agent.config import Settings
from investment_agent.investor.common import ObjectRef
from investment_agent.investor.contracts import Mandate
from investment_agent.investor.enums import TaskStatus
from investment_agent.settings.loader import load_settings
from investment_agent.storage.models.agent import (
    BudgetEntry,
    ExecutionGrantRow,
    PublicTaskEvent,
    ToolAction,
)
from investment_agent.storage.models.investor import MandateHead, MandateRevision

EVENT_HISTORY_PRUNED = "event_history_pruned"


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


async def _plan_view(session: AsyncSession, task: Task) -> PlanView | None:
    """Latest committed research_progress by artifact append order, never SQL order."""
    for ref in reversed(task.artifact_refs):
        if ref.kind != "research_progress":
            continue
        action = await session.get(ToolAction, str(ref.id))
        if (action is None or action.task_id != str(task.id) or action.state != "committed"
                or not isinstance(action.result, dict)):
            continue
        try:
            progress = ResearchProgress.model_validate(action.result["progress"])
        except (KeyError, ValidationError):
            continue
        completed = {normalize_step(step) for step in progress.completed_steps}
        planned = {normalize_step(step) for step in progress.evidence_steps}
        if not planned:
            return None
        steps: list[PlanStep] = []
        next_assigned = False
        for step in progress.evidence_steps:
            if normalize_step(step) in completed:
                steps.append(PlanStep(text=step, state="done"))
                continue
            # "next" marks the next uncompleted plan item while the task runs
            # uncancelled; it never claims a tool is executing this step now.
            if not next_assigned and task.status == TaskStatus.RUNNING and not task.cancel_requested:
                next_assigned = True
                steps.append(PlanStep(text=step, state="next"))
            else:
                steps.append(PlanStep(text=step, state="pending"))
        saved_at, reason = await _saved_at(session, task.id, ref)
        return PlanView(ref=ref, question=progress.question, saved_at=saved_at,
            saved_at_unknown_reason=reason, steps=tuple(steps),
            extra_completed=tuple(step for step in progress.completed_steps
                if normalize_step(step) not in planned),
            completion_criteria=progress.completion_criteria, stop_conditions=progress.stop_conditions)
    return None


async def _task_cost_limit(session: AsyncSession,
                           grant: ExecutionGrant) -> tuple[Decimal | None, str | None]:
    """Per-task USD cap for display; cost_limits' rules without its raises."""
    cap = grant.task_cost_usd
    if grant.mandate_id is None:
        return (None, "unbounded") if cap is None else (cap, None)
    head = await session.get(MandateHead, grant.owner)
    if head is None or head.mandate_id != str(grant.mandate_id):
        return None, "mandate_unavailable"
    revision = await session.get(MandateRevision, (head.mandate_id, head.revision))
    if revision is None:
        return None, "mandate_unavailable"
    quantity = Mandate.model_validate(revision.payload).authorization.per_task_budget
    currency = quantity.currency
    if quantity.unit in {"USD", "CNY", "HKD", "EUR"}:
        if currency is not None and currency != quantity.unit:
            return None, "authorization_budget_currency_invalid"
        currency = quantity.unit
    elif quantity.unit != "currency":
        return None, "authorization_budget_unit_invalid"
    if quantity.value is not None and currency != "USD":
        return None, "verified_exchange_rate_unavailable"
    if quantity.value is not None and quantity.value < 0:
        return None, "authorization_budget_invalid"
    if quantity.value is not None:
        cap = min(cap, quantity.value) if cap is not None else quantity.value
    return (cap, None) if cap is not None else (None, "unbounded")


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
    cost_usd, cost_reason = await _task_cost_limit(session, grant)
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
        seconds=settings.investor_agent_task_seconds, cost_usd=cost_usd, cost_limit_reason=cost_reason,
        finish_only=False if task.status.value in TERMINAL else bool(headroom["finish_only"]))
    usage: Usage = task.usage
    return TaskProgress(task_id=task.id, task_revision=task.revision, research_kind=grant.research_kind,
        as_of=now, plan=await _plan_view(session, task), limits=limits, usage=usage,
        elapsed_seconds=elapsed, elapsed_unknown_reason=elapsed_reason,
        deadline=task.created_at + timedelta(seconds=settings.investor_agent_task_seconds))
