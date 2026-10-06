"""Persistent database worker for queued jobs.

The worker claims runs of every registered job kind (``ifind_research`` plus
the snapshot collectors), keeps each claim's heartbeat fresh, fences all
lifecycle updates with the claim's lease, and settles failures through the
shared retry policy. Kind-specific behaviour lives in
:class:`~investment_agent.jobs.base.JobHandler` implementations.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable, Collection, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import structlog
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.config import Settings
from investment_agent.jobs.base import (
    BaseJobHandler,
    ClaimedJob,
    JobContext,
    JobHandler,
    JobOutcome,
)
from investment_agent.notifiers.file_notifier import FileNotifier
from investment_agent.research.change_notification import render_change_notification
from investment_agent.research.ifind_chain import (
    ResearchChainResult,
    ResearchPartialError,
    run_ifind_research_chain,
)
from investment_agent.research.retry_policy import is_transient_error
from investment_agent.snapshots.handlers import snapshot_job_handlers
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.repositories.pipeline_run import (
    LeaseLostError,
    PipelineRunRepository,
    RunLease,
    lease_scope,
)
from investment_agent.storage.repositories.research_change import ResearchChangeRepository
from investment_agent.storage.repositories.research_snapshot import (
    ResearchSnapshotRepository,
)
from investment_agent.storage.repositories.watchlist import WatchlistRepository

log = structlog.get_logger(__name__)

ResearchRunner = Callable[..., Awaitable[ResearchChainResult]]

IFIND_RESEARCH_KIND = "ifind_research"
MAX_LOOP_BACKOFF_SECONDS = 60.0


class ResearchJobPayload(BaseModel):
    """Credential-free input persisted for a queued research run."""

    ticker: str = Field(pattern=r"^\d{6}\.(SH|SZ|BJ)$")
    industry: str = Field(min_length=1, max_length=128)
    report_year: int = Field(ge=1990, le=2100)
    as_of: date


class IfindResearchHandler(BaseJobHandler):
    """``ifind_research``: one single-stock research chain run.

    The chain persists the run's terminal status itself (it is passed the
    claimed run id); this handler keeps the watchlist row in step with the
    run and writes the meaningful-change notification afterwards.
    """

    kind = IFIND_RESEARCH_KIND

    def __init__(self, *, settings: Settings, runner: ResearchRunner) -> None:
        self.settings = settings
        self.runner = runner

    async def on_claimed(self, session: AsyncSession, job: ClaimedJob) -> None:
        if job.listing_id is not None:
            await WatchlistRepository(session).mark_refresh_queued(
                job.listing_id,
                run_id=job.run_id,
                status="running",
            )

    async def execute(self, job: ClaimedJob, ctx: JobContext) -> JobOutcome:
        payload = ResearchJobPayload.model_validate(job.request_json)

        async def invoke_runner() -> ResearchChainResult:
            return await self.runner(
                settings=self.settings,
                proxy_url=self.settings.ifind_proxy_url,
                proxy_api_key=self.settings.ifind_proxy_api_key,
                ticker=payload.ticker,
                industry=payload.industry,
                report_year=payload.report_year,
                as_of=payload.as_of,
                output_root=self.settings.data_dir / "research-runs",
                triggered_by="worker",
                claimed_run_id=job.run_id,
            )

        try:
            await ctx.run_with_heartbeat(job.lease, invoke_runner)
        except ResearchPartialError as exc:
            log.warning(
                "research.worker.partial",
                run_id=job.run_id,
                worker_id=ctx.worker_id,
                reason=exc.reason,
            )
            return "partial"
        return "completed"

    async def on_retry_scheduled(
        self, session: AsyncSession, job: ClaimedJob, error: str
    ) -> None:
        if job.listing_id is not None:
            await WatchlistRepository(session).mark_refresh_retrying(
                job.listing_id,
                run_id=job.run_id,
                error=error,
            )

    async def on_recovered(
        self,
        session: AsyncSession,
        run: PipelineRun,
        *,
        requeued: bool,
        now: datetime,
    ) -> None:
        if run.listing_id is None:
            return
        watchlist_repository = WatchlistRepository(session)
        if requeued:
            await watchlist_repository.mark_refresh_queued(run.listing_id, run_id=run.id)
        else:
            await watchlist_repository.mark_refresh_finished(
                run.listing_id,
                run_id=run.id,
                status="failed",
                refreshed_at=run.finished_at or now,
                error=run.error_summary,
            )

    async def finalize(self, job: ClaimedJob) -> None:
        if job.listing_id is None:
            return
        try:
            await _sync_watchlist_refresh(run_id=job.run_id, listing_id=job.listing_id)
        except Exception:
            log.exception(
                "research.worker.watchlist_sync_failed",
                run_id=job.run_id,
                worker_id=job.lease.worker_id,
            )
        try:
            await _notify_meaningful_change(settings=self.settings, run_id=job.run_id)
        except Exception:
            log.exception(
                "research.worker.notification_failed",
                run_id=job.run_id,
                worker_id=job.lease.worker_id,
            )


def default_job_handlers(
    settings: Settings, *, runner: ResearchRunner = run_ifind_research_chain
) -> list[JobHandler]:
    """Every job kind a worker serves unless restricted with ``kinds``."""

    return [
        IfindResearchHandler(settings=settings, runner=runner),
        *snapshot_job_handlers(settings),
    ]


class ResearchWorker:
    """Claim and execute one queued job at a time, dispatching by ``kind``."""

    def __init__(
        self,
        *,
        settings: Settings,
        worker_id: str,
        runner: ResearchRunner = run_ifind_research_chain,
        retry_base_delay_seconds: float = 5.0,
        heartbeat_interval_seconds: float = 30.0,
        handlers: Sequence[JobHandler] | None = None,
        kinds: Collection[str] | None = None,
    ) -> None:
        self.settings = settings
        self.worker_id = worker_id
        self.runner = runner
        self.retry_base_delay_seconds = retry_base_delay_seconds
        self.heartbeat_interval_seconds = heartbeat_interval_seconds
        registry = {
            handler.kind: handler
            for handler in (
                handlers
                if handlers is not None
                else default_job_handlers(settings, runner=runner)
            )
        }
        if kinds is not None:
            unknown = sorted(set(kinds) - registry.keys())
            if unknown:
                raise ValueError(f"no handler registered for job kinds: {unknown}")
            registry = {kind: handler for kind, handler in registry.items() if kind in kinds}
        if not registry:
            raise ValueError("a worker needs at least one job handler")
        self.handlers: dict[str, JobHandler] = registry

    @property
    def kinds(self) -> list[str]:
        return list(self.handlers)

    async def recover_stale(self, *, stale_after_seconds: float) -> tuple[list[int], list[int]]:
        now = datetime.now(UTC)
        async with session_scope() as session:
            repository = PipelineRunRepository(session)
            requeued, failed = await repository.recover_stale(
                kinds=self.kinds,
                stale_before=now - timedelta(seconds=stale_after_seconds),
                now=now,
            )
            outcomes = [(run_id, True) for run_id in requeued] + [
                (run_id, False) for run_id in failed
            ]
            for run_id, was_requeued in outcomes:
                run = await repository.get(run_id)
                if run is None:
                    continue
                handler = self.handlers.get(run.kind)
                if handler is not None:
                    await handler.on_recovered(session, run, requeued=was_requeued, now=now)
            return requeued, failed

    async def run_once(self, *, run_id: int | None = None) -> int | None:
        """Claim and run one job; ``run_id`` claims that specific queued run."""

        async with session_scope() as session:
            run = await PipelineRunRepository(session).claim_next(
                worker_id=self.worker_id,
                kinds=self.kinds,
                run_id=run_id,
            )
            if run is None:
                return None
            job = ClaimedJob(
                run_id=run.id,
                kind=run.kind,
                lease=RunLease(
                    run_id=run.id,
                    worker_id=self.worker_id,
                    attempt=run.attempt_count,
                ),
                listing_id=run.listing_id,
                universe_code=run.universe_code,
                request_json=dict(run.request_json or {}),
            )
            handler = self.handlers[job.kind]
            await handler.on_claimed(session, job)

        try:
            # Every lifecycle update made for this run is fenced by the lease,
            # so a worker that lost its claim cannot clobber a newer attempt.
            with lease_scope(job.lease):
                outcome = await handler.execute(job, self)
            if outcome == "completed":
                log.info(
                    "research.worker.completed",
                    run_id=job.run_id,
                    worker_id=self.worker_id,
                    kind=job.kind,
                )
        except LeaseLostError as exc:
            log.warning(
                "research.worker.lease_lost",
                run_id=job.run_id,
                worker_id=self.worker_id,
                attempt=job.lease.attempt,
                detail=exc.detail,
            )
        except Exception as exc:
            await self._settle_failure(exc, job=job, handler=handler)
        finally:
            await handler.finalize(job)
        return job.run_id

    async def run_with_heartbeat[T](
        self, lease: RunLease, work: Callable[[], Awaitable[T]]
    ) -> T:
        """Run ``work`` while a background task keeps the claim's heartbeat fresh.

        If the heartbeat discovers the lease was lost (stale recovery handed the
        run to another worker), ``work`` is cancelled and LeaseLostError is
        raised so this worker stops without touching the run.
        """

        work_task: asyncio.Task[T] = asyncio.create_task(_as_coroutine(work))
        heartbeat_task = asyncio.create_task(self._heartbeat_loop(lease, work_task))
        try:
            return await work_task
        except asyncio.CancelledError:
            lost = heartbeat_task.done() and not heartbeat_task.cancelled()
            lost_error = heartbeat_task.result() if lost else None
            if lost_error is not None and work_task.cancelled():
                current = asyncio.current_task()
                if current is None or not current.cancelling():
                    raise lost_error from None
            raise
        finally:
            heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat_task

    async def _heartbeat_loop(
        self, lease: RunLease, work_task: asyncio.Task[Any]
    ) -> LeaseLostError | None:
        while not work_task.done():
            await asyncio.sleep(self.heartbeat_interval_seconds)
            if work_task.done():
                return None
            try:
                async with session_scope() as session:
                    await PipelineRunRepository(session).heartbeat(lease.run_id, lease=lease)
            except LeaseLostError as exc:
                if work_task.done():
                    return None
                log.warning(
                    "research.worker.heartbeat_lease_lost",
                    run_id=lease.run_id,
                    worker_id=lease.worker_id,
                    attempt=lease.attempt,
                    detail=exc.detail,
                )
                work_task.cancel()
                return exc
            except Exception:
                # A transient DB error must not kill the run; the next beat
                # retries well before the stale threshold.
                log.exception(
                    "research.worker.heartbeat_failed",
                    run_id=lease.run_id,
                    worker_id=lease.worker_id,
                )
        return None

    async def _settle_failure(
        self,
        exc: Exception,
        *,
        job: ClaimedJob,
        handler: JobHandler,
    ) -> None:
        """Decide retry-or-terminal for a failed attempt in one transaction."""

        lease = job.lease
        run_id = lease.run_id
        retry_scheduled = False
        try:
            async with session_scope() as session:
                repository = PipelineRunRepository(session)
                if is_transient_error(exc):
                    error = f"retry scheduled: {type(exc).__name__}: {exc}"
                    retry_at = datetime.now(UTC) + timedelta(
                        seconds=self.retry_base_delay_seconds
                        * (2 ** max(lease.attempt - 1, 0))
                    )
                    retry_scheduled = await repository.schedule_retry(
                        run_id,
                        error_summary=error,
                        available_at=retry_at,
                        lease=lease,
                    )
                    if retry_scheduled:
                        await handler.on_retry_scheduled(session, job, error)
                if not retry_scheduled:
                    await repository.mark_failed(run_id, str(exc), lease=lease)
        except LeaseLostError as lost:
            log.warning(
                "research.worker.lease_lost",
                run_id=run_id,
                worker_id=self.worker_id,
                attempt=lease.attempt,
                detail=lost.detail,
                error=str(exc),
            )
            return
        if retry_scheduled:
            log.warning(
                "research.worker.retry_scheduled",
                run_id=run_id,
                worker_id=self.worker_id,
                error=str(exc),
            )
        else:
            log.error(
                "research.worker.failed",
                run_id=run_id,
                worker_id=self.worker_id,
                exc_info=exc,
            )


async def _as_coroutine[T](work: Callable[[], Awaitable[T]]) -> T:
    return await work()


async def _sync_watchlist_refresh(*, run_id: int, listing_id: int) -> None:
    async with session_scope() as session:
        run = await PipelineRunRepository(session).get(run_id)
        if run is None or run.status not in {"success", "partial", "failed"}:
            return
        await WatchlistRepository(session).mark_refresh_finished(
            listing_id,
            run_id=run_id,
            status=run.status,
            refreshed_at=run.finished_at or datetime.now(UTC),
            error=run.error_summary,
        )


async def _notify_meaningful_change(*, settings: Settings, run_id: int) -> str | None:
    """Write the change notification for one run at most once.

    The target path is deterministic per run, the write is atomic, and an
    already-written file is reused, so a crash between writing the file and
    recording it never produces a second notification.
    """

    async with session_scope() as session:
        change_set = await ResearchChangeRepository(session).by_run(run_id)
        snapshot = await ResearchSnapshotRepository(session).by_run(run_id)
        if (
            change_set is None
            or snapshot is None
            or not change_set.has_meaningful_changes
        ):
            return None
        if change_set.notification_path is not None:
            existing = _existing_notification(change_set.notification_path)
            if existing is not None:
                return str(existing)
        diff = change_set.diff_json
        previous_run_id = diff.get("previous_run_id")
        previous_id = previous_run_id if isinstance(previous_run_id, int) else None
        ticker = f"{snapshot.symbol}.{snapshot.market}"
        company_name = snapshot.company_name
        notification_date = snapshot.as_of.isoformat()

    notifier = FileNotifier(settings.data_dir / "notifications")
    channel_metadata: dict[str, object] = {
        "kind": "research_change",
        "run_id": run_id,
        "date": notification_date,
    }
    content = render_change_notification(
        ticker=ticker,
        company_name=company_name,
        current_run_id=run_id,
        previous_run_id=previous_id,
        diff=diff,
    )
    target = notifier.target_path(content.format, channel_metadata)
    if not target.is_file():
        result = await notifier.send(content, channel_metadata=channel_metadata)
        if not result.success or result.target is None:
            raise RuntimeError(result.error or "research change notification failed")
        target = Path(result.target)
    async with session_scope() as session:
        await ResearchChangeRepository(session).mark_notified(
            run_id,
            path=str(target),
        )
    return str(target)


def _existing_notification(raw_path: str) -> Path | None:
    path = Path(raw_path)
    return path if path.is_file() else None


async def run_worker(
    *,
    settings: Settings,
    worker_id: str,
    poll_interval_seconds: float = 1.0,
    stale_after_seconds: float = 300.0,
    once: bool = False,
    recovery_interval_seconds: float | None = None,
    heartbeat_interval_seconds: float | None = None,
    worker: ResearchWorker | None = None,
    kinds: Collection[str] | None = None,
) -> None:
    """Poll for work until cancelled (or one iteration with ``once``).

    Stale claims are recovered at startup and then every
    ``recovery_interval_seconds`` (default ``stale_after / 2``), so runs
    orphaned by a crashed peer are picked up without restarting a worker.
    Any error in an iteration is logged and retried with exponential backoff;
    a database hiccup never terminates the worker loop.
    """

    if worker is None:
        worker = ResearchWorker(
            settings=settings,
            worker_id=worker_id,
            heartbeat_interval_seconds=(
                heartbeat_interval_seconds
                if heartbeat_interval_seconds is not None
                else max(stale_after_seconds / 4, 1.0)
            ),
            kinds=kinds,
        )
    recovery_interval = (
        recovery_interval_seconds
        if recovery_interval_seconds is not None
        else max(stale_after_seconds / 2, poll_interval_seconds)
    )
    last_recovery: float | None = None
    consecutive_errors = 0
    log.info(
        "research.worker.started",
        worker_id=worker_id,
        kinds=getattr(worker, "kinds", None),
    )
    while True:
        try:
            now = time.monotonic()
            if last_recovery is None or now - last_recovery >= recovery_interval:
                requeued, failed = await worker.recover_stale(
                    stale_after_seconds=stale_after_seconds
                )
                last_recovery = now
                if requeued or failed:
                    log.info(
                        "research.worker.recovered_stale",
                        worker_id=worker_id,
                        requeued_run_ids=requeued,
                        failed_run_ids=failed,
                    )
            run_id = await worker.run_once()
            consecutive_errors = 0
        except Exception:
            if once:
                raise
            consecutive_errors += 1
            backoff = min(
                poll_interval_seconds * (2 ** (consecutive_errors - 1)),
                MAX_LOOP_BACKOFF_SECONDS,
            )
            log.exception(
                "research.worker.loop_error",
                worker_id=worker_id,
                consecutive_errors=consecutive_errors,
                backoff_seconds=backoff,
            )
            await asyncio.sleep(backoff)
            continue
        if once:
            return
        if run_id is None:
            await asyncio.sleep(poll_interval_seconds)
