"""Agent execution seam for the existing heartbeat/retry worker.

No default handler is registered until AG08 supplies an authorized executor.
"""

from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from contextvars import ContextVar
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

import structlog
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.agent.tasks import TaskBoundaryError, TaskClaim, TaskService
from investment_agent.investor.enums import StopReason, TaskStatus
from investment_agent.jobs.base import BaseJobHandler, ClaimedJob, JobContext, JobOutcome
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.agent import TaskAttempt
from investment_agent.storage.models.analysis import PipelineRun

if TYPE_CHECKING:
    from investment_agent.config import Settings

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]
Executor = Callable[[TaskClaim, JobContext], Awaitable[JobOutcome]]

# The claim travels with the job's own asyncio context (on_claimed and execute
# run in the same task), never in handler instance state shared across jobs.
_CLAIM: ContextVar[TaskClaim | None] = ContextVar("agent_job_claim", default=None)


class AgentJobHandler(BaseJobHandler):
    def __init__(
        self, kind: str, executor: Executor, *, sessions: SessionFactory = session_scope
    ) -> None:
        if kind not in {"agent_turn", "agent_research", "agent_review", "agent_discovery"}:
            raise ValueError("unsupported agent kind")
        self.kind = kind
        self.executor = executor
        self.sessions = sessions

    async def on_claimed(self, session: AsyncSession, job: ClaimedJob) -> None:
        _CLAIM.set(await TaskService(session).claim(
            UUID(str(job.request_json["task_id"])), job.lease
        ))

    async def execute(self, job: ClaimedJob, ctx: JobContext) -> JobOutcome:
        claim = _CLAIM.get()
        _CLAIM.set(None)
        if claim is None:
            return "completed"

        async def execute() -> JobOutcome:
            async with self.sessions() as session:
                task = await TaskService(session).get_task(claim.task_id)
                if task.cancel_requested and task.status == TaskStatus.RUNNING:
                    await TaskService(session).ack_cancel(claim)
                    return "completed"
                if task.status == TaskStatus.CANCELLED:
                    from investment_agent.storage.repositories.pipeline_run import (
                        PipelineRunRepository,
                    )

                    await PipelineRunRepository(session).mark_success(job.run_id, lease=job.lease)
                    return "completed"
            try:
                return await self.executor(claim, ctx)
            except TaskBoundaryError:
                async with self.sessions() as session:
                    task = await TaskService(session).get_task(claim.task_id)
                    if task.cancel_requested and task.status == TaskStatus.RUNNING:
                        await TaskService(session).ack_cancel(claim)
                        return "completed"
                raise

        return await ctx.run_with_heartbeat(job.lease, execute)

    async def on_retry_scheduled(self, session: AsyncSession, job: ClaimedJob, error: str) -> None:
        await session.execute(
            update(TaskAttempt)
            .where(
                TaskAttempt.run_id == job.run_id,
                TaskAttempt.lease_attempt == job.attempt,
                TaskAttempt.status == "running",
            )
            .values(status="retry_scheduled")
        )

    async def on_recovered(
        self, session: AsyncSession, run: PipelineRun, *, requeued: bool, now: datetime
    ) -> None:
        await session.execute(
            update(TaskAttempt)
            .where(
                TaskAttempt.run_id == run.id,
                TaskAttempt.lease_attempt == run.attempt_count,
                TaskAttempt.status == "running",
            )
            .values(status="abandoned" if requeued else "failed")
        )
        if not requeued:
            await self._failed(session, run)

    async def _failed(self, session: AsyncSession, run: PipelineRun) -> None:
        service = TaskService(session)
        task_id = UUID(str(run.request_json["task_id"]))
        row = await service._row(task_id)
        if row.status in {"queued", "running"} and row.run_id == run.id:
            task = await service.get_task(task_id)
            await service._save(
                row,
                task.model_copy(
                    update={
                        "status": TaskStatus.CANCELLED
                        if task.cancel_requested
                        else TaskStatus.FAILED,
                        "stop_reason": StopReason.CANCELLED
                        if task.cancel_requested
                        else StopReason.FAILED,
                    }
                ),
            )
            await service._state_event(row, None)
            await session.execute(
                update(TaskAttempt)
                .where(
                    TaskAttempt.run_id == run.id,
                    TaskAttempt.lease_attempt == run.attempt_count,
                    TaskAttempt.status == "running",
                )
                .values(status="cancelled" if task.cancel_requested else "failed")
            )

    async def finalize(self, job: ClaimedJob) -> None:
        try:
            async with self.sessions() as session:
                run = await session.get(PipelineRun, job.run_id)
                if run is not None and run.status == "failed":
                    await self._failed(session, run)
        except Exception:
            structlog.get_logger(__name__).exception(
                "agent.task_finalize_failed", run_id=job.run_id
            )


def agent_job_handlers(settings: "Settings") -> list[AgentJobHandler]:
    """Default worker wiring; admission/each execution boundary still fail closed.

    Registered even while disabled so existing admitted jobs can stop cleanly
    after a configuration change. Injected worker handler sets stay untouched.
    """
    from investment_agent.agent.executor import AgentExecutor
    from investment_agent.agent.research import ResearchPolicy, registrations

    executor = AgentExecutor(settings, registrations=registrations(), specification=ResearchPolicy(settings).specification())
    from investment_agent.investor.discovery import DiscoveryPolicy
    discovery = AgentExecutor(settings, specification=DiscoveryPolicy(settings).specification())
    return [*[AgentJobHandler(kind, executor) for kind in ("agent_turn", "agent_research", "agent_review")], AgentJobHandler("agent_discovery", discovery)]
