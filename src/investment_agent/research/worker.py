"""Persistent database worker for queued single-stock research runs."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import structlog
from pydantic import BaseModel, Field

from investment_agent.config import Settings
from investment_agent.notifiers.file_notifier import FileNotifier
from investment_agent.research.change_notification import render_change_notification
from investment_agent.research.ifind_chain import (
    ResearchChainResult,
    ResearchPartialError,
    run_ifind_research_chain,
)
from investment_agent.storage.database import session_scope
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository
from investment_agent.storage.repositories.research_change import ResearchChangeRepository
from investment_agent.storage.repositories.research_snapshot import (
    ResearchSnapshotRepository,
)
from investment_agent.storage.repositories.watchlist import WatchlistRepository

log = structlog.get_logger(__name__)

ResearchRunner = Callable[..., Awaitable[ResearchChainResult]]


class ResearchJobPayload(BaseModel):
    """Credential-free input persisted for a queued research run."""

    ticker: str = Field(pattern=r"^\d{6}\.(SH|SZ|BJ)$")
    industry: str = Field(min_length=1, max_length=128)
    report_year: int = Field(ge=1990, le=2100)
    as_of: date


class ResearchWorker:
    """Claim and execute one research job at a time."""

    def __init__(
        self,
        *,
        settings: Settings,
        worker_id: str,
        runner: ResearchRunner = run_ifind_research_chain,
        retry_base_delay_seconds: float = 5.0,
    ) -> None:
        self.settings = settings
        self.worker_id = worker_id
        self.runner = runner
        self.retry_base_delay_seconds = retry_base_delay_seconds

    async def recover_stale(self, *, stale_after_seconds: float) -> tuple[list[int], list[int]]:
        now = datetime.now(UTC)
        async with session_scope() as session:
            repository = PipelineRunRepository(session)
            requeued, failed = await repository.recover_stale(
                kind="ifind_research",
                stale_before=now - timedelta(seconds=stale_after_seconds),
                now=now,
            )
            watchlist_repository = WatchlistRepository(session)
            for run_id in requeued:
                run = await repository.get(run_id)
                if run is not None and run.listing_id is not None:
                    await watchlist_repository.mark_refresh_queued(
                        run.listing_id,
                        run_id=run.id,
                    )
            for run_id in failed:
                run = await repository.get(run_id)
                if run is not None and run.listing_id is not None:
                    await watchlist_repository.mark_refresh_finished(
                        run.listing_id,
                        run_id=run.id,
                        status="failed",
                        refreshed_at=run.finished_at or now,
                        error=run.error_summary,
                    )
            return requeued, failed

    async def run_once(self) -> int | None:
        async with session_scope() as session:
            run = await PipelineRunRepository(session).claim_next(
                worker_id=self.worker_id,
                kind="ifind_research",
            )
            if run is None:
                return None
            run_id = run.id
            listing_id = run.listing_id
            request_json = run.request_json
            if listing_id is not None:
                await WatchlistRepository(session).mark_refresh_queued(
                    listing_id,
                    run_id=run_id,
                    status="running",
                )

        try:
            payload = ResearchJobPayload.model_validate(request_json)
            await self.runner(
                settings=self.settings,
                proxy_url=self.settings.ifind_proxy_url,
                proxy_api_key=self.settings.ifind_proxy_api_key,
                ticker=payload.ticker,
                industry=payload.industry,
                report_year=payload.report_year,
                as_of=payload.as_of,
                output_root=self.settings.data_dir / "research-runs",
                triggered_by="worker",
                claimed_run_id=run_id,
            )
            log.info("research.worker.completed", run_id=run_id, worker_id=self.worker_id)
        except ResearchPartialError as exc:
            log.warning(
                "research.worker.partial",
                run_id=run_id,
                worker_id=self.worker_id,
                reason=exc.reason,
            )
        except Exception as exc:
            retry_scheduled = False
            async with session_scope() as session:
                repository = PipelineRunRepository(session)
                current = await repository.get(run_id)
                if current is not None:
                    if _is_transient_error(exc):
                        retry_at = datetime.now(UTC) + timedelta(
                            seconds=self.retry_base_delay_seconds
                            * (2 ** max(current.attempt_count - 1, 0))
                        )
                        retry_scheduled = await repository.schedule_retry(
                            run_id,
                            error_summary=f"retry scheduled: {type(exc).__name__}: {exc}",
                            available_at=retry_at,
                        )
                        if retry_scheduled and listing_id is not None:
                            await WatchlistRepository(session).mark_refresh_retrying(
                                listing_id,
                                run_id=run_id,
                                error=f"retry scheduled: {type(exc).__name__}: {exc}",
                            )
                    if not retry_scheduled and current.status == "running":
                        await repository.mark_failed(run_id, str(exc))
            if retry_scheduled:
                log.warning(
                    "research.worker.retry_scheduled",
                    run_id=run_id,
                    worker_id=self.worker_id,
                    error=str(exc),
                )
            else:
                log.exception(
                    "research.worker.failed",
                    run_id=run_id,
                    worker_id=self.worker_id,
                )
        finally:
            if listing_id is not None:
                await _sync_watchlist_refresh(run_id=run_id, listing_id=listing_id)
                try:
                    await _notify_meaningful_change(
                        settings=self.settings,
                        run_id=run_id,
                    )
                except Exception:
                    log.exception(
                        "research.worker.notification_failed",
                        run_id=run_id,
                        worker_id=self.worker_id,
                    )
        return run_id


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


def _is_transient_error(exc: Exception) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429 or exc.response.status_code >= 500
    return isinstance(
        exc,
        (
            httpx.TimeoutException,
            httpx.TransportError,
            ConnectionError,
            TimeoutError,
            OSError,
        ),
    )


async def _notify_meaningful_change(*, settings: Settings, run_id: int) -> str | None:
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

    content = render_change_notification(
        ticker=ticker,
        company_name=company_name,
        current_run_id=run_id,
        previous_run_id=previous_id,
        diff=diff,
    )
    result = await FileNotifier(settings.data_dir / "notifications").send(
        content,
        channel_metadata={
            "kind": "research_change",
            "run_id": run_id,
            "date": notification_date,
        },
    )
    if not result.success or result.target is None:
        raise RuntimeError(result.error or "research change notification failed")
    async with session_scope() as session:
        await ResearchChangeRepository(session).mark_notified(
            run_id,
            path=result.target,
        )
    return result.target


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
) -> None:
    """Recover abandoned work, then poll until cancelled or one iteration completes."""

    worker = ResearchWorker(settings=settings, worker_id=worker_id)
    requeued, failed = await worker.recover_stale(
        stale_after_seconds=stale_after_seconds
    )
    log.info(
        "research.worker.started",
        worker_id=worker_id,
        requeued_run_ids=requeued,
        failed_run_ids=failed,
    )
    while True:
        run_id = await worker.run_once()
        if once:
            return
        if run_id is None:
            await asyncio.sleep(poll_interval_seconds)
