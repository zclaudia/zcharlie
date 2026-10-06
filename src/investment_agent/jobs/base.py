"""Job handler contract for the queue worker.

The worker owns claiming, lease fencing, heartbeats and the retry-or-fail
decision; a :class:`JobHandler` owns everything specific to one ``kind`` of
``pipeline_run``: validating its payload, executing it, and any side effects
around the run lifecycle (watchlist bookkeeping, notifications).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.repositories.pipeline_run import RunLease

JobOutcome = Literal["completed", "partial"]


@dataclass(frozen=True)
class ClaimedJob:
    """Snapshot of a freshly claimed run, detached from its DB session."""

    run_id: int
    kind: str
    lease: RunLease
    listing_id: int | None = None
    universe_code: str | None = None
    request_json: dict[str, object] = field(default_factory=dict)

    @property
    def attempt(self) -> int:
        return self.lease.attempt


class JobContext(Protocol):
    """What a handler may use from the worker executing it."""

    worker_id: str

    async def run_with_heartbeat[T](self, lease: RunLease, work: Callable[[], Awaitable[T]]) -> T:
        """Run ``work`` while keeping the lease's heartbeat fresh.

        Raises :class:`LeaseLostError` (after cancelling ``work``) when the
        lease is lost mid-run.
        """
        ...


class JobHandler(Protocol):
    kind: str

    async def on_claimed(self, session: AsyncSession, job: ClaimedJob) -> None: ...

    async def execute(self, job: ClaimedJob, ctx: JobContext) -> JobOutcome:
        """Run the job under the worker's lease scope.

        Return ``"completed"`` or ``"partial"`` (the handler persisted the
        terminal status itself). Raise to let the worker retry (transient
        errors) or fail the run.
        """
        ...

    async def on_retry_scheduled(
        self, session: AsyncSession, job: ClaimedJob, error: str
    ) -> None: ...

    async def on_recovered(
        self,
        session: AsyncSession,
        run: PipelineRun,
        *,
        requeued: bool,
        now: datetime,
    ) -> None: ...

    async def finalize(self, job: ClaimedJob) -> None:
        """Best-effort post-run side effects; must not raise."""
        ...


class BaseJobHandler:
    """No-op lifecycle hooks; subclasses implement ``execute``."""

    kind: str = ""

    async def on_claimed(self, session: AsyncSession, job: ClaimedJob) -> None:
        return None

    async def on_retry_scheduled(
        self, session: AsyncSession, job: ClaimedJob, error: str
    ) -> None:
        return None

    async def on_recovered(
        self,
        session: AsyncSession,
        run: PipelineRun,
        *,
        requeued: bool,
        now: datetime,
    ) -> None:
        return None

    async def finalize(self, job: ClaimedJob) -> None:
        return None
