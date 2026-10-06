"""Queue job handlers for the snapshot collectors.

``market_snapshot`` / ``financial_snapshot`` are routed by the payload's
``provider`` (decision D6): ``baostock`` -> :mod:`investment_agent.snapshots.breadth`
(target pool), ``ifind`` -> the iFinD batch collectors (watchlist only).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

import structlog

from investment_agent.config import Settings
from investment_agent.jobs.base import BaseJobHandler, ClaimedJob, JobContext, JobOutcome
from investment_agent.notifiers.file_notifier import FileNotifier
from investment_agent.providers.baostock_market import BaostockMarketClient
from investment_agent.providers.baostock_reference import BaostockReferenceClient
from investment_agent.research.ifind_chain import IfindProxyClient
from investment_agent.snapshots.breadth import (
    BaostockCollectorClient,
    collect_baostock_financial,
    collect_baostock_market,
)
from investment_agent.snapshots.collect import (
    ArtifactWriter,
    CollectionReport,
    ProxyBatchCaller,
    ProxyClient,
)
from investment_agent.snapshots.financial import collect_financial_snapshot
from investment_agent.snapshots.jobs import (
    FINANCIAL_SNAPSHOT,
    INDUSTRY_SYNC,
    MARKET_SNAPSHOT,
    SNAPSHOT_KINDS,
    UNIVERSE_SYNC,
    FinancialSnapshotPayload,
    IndustrySyncPayload,
    MarketSnapshotPayload,
    UniverseSyncPayload,
)
from investment_agent.snapshots.market import collect_market_snapshot
from investment_agent.snapshots.notifications import notify_snapshot_run
from investment_agent.snapshots.reference import (
    ReferenceClient,
    sync_industry,
    sync_universe,
)
from investment_agent.snapshots.throttle import AsyncRateLimiter
from investment_agent.storage.database import session_scope
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository, RunLease

log = structlog.get_logger(__name__)

ProxyFactory = Callable[[], AbstractAsyncContextManager[ProxyClient]]
ReferenceFactory = Callable[[], ReferenceClient]
BaostockFactory = Callable[[], BaostockCollectorClient]


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class SnapshotRuntime:
    """Dependencies shared by every snapshot handler of one worker."""

    settings: Settings
    limiter: AsyncRateLimiter
    proxy_factory: ProxyFactory
    reference_factory: ReferenceFactory
    clock: Callable[[], datetime] = _utc_now
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    batch_backoff_seconds: float = 2.0
    notifier: FileNotifier | None = field(default=None)
    baostock_factory: BaostockFactory = BaostockMarketClient

    def file_notifier(self) -> FileNotifier:
        return self.notifier or FileNotifier(self.settings.data_dir / "notifications")


def default_runtime(settings: Settings) -> SnapshotRuntime:
    def proxy_factory() -> AbstractAsyncContextManager[ProxyClient]:
        return IfindProxyClient(
            base_url=settings.ifind_proxy_url, api_key=settings.ifind_proxy_api_key
        )

    return SnapshotRuntime(
        settings=settings,
        limiter=AsyncRateLimiter(settings.ifind_collect_rate_per_minute),
        proxy_factory=proxy_factory,
        reference_factory=BaostockReferenceClient,
    )


@dataclass(frozen=True)
class _Result:
    status: Literal["success", "partial"]
    metadata: dict[str, object]
    summary: str = ""


class SnapshotJobHandler(BaseJobHandler):
    """Executes one snapshot kind and persists its completeness metadata."""

    def __init__(self, kind: str, runtime: SnapshotRuntime) -> None:
        if kind not in SNAPSHOT_KINDS:
            raise ValueError(f"not a snapshot job kind: {kind!r}")
        self.kind = kind
        self.runtime = runtime

    async def execute(self, job: ClaimedJob, ctx: JobContext) -> JobOutcome:
        artifacts = ArtifactWriter(
            self.runtime.settings.data_dir, run_id=job.run_id, attempt=job.attempt
        )

        async def work() -> _Result:
            return await self._collect(job, artifacts)

        result = await ctx.run_with_heartbeat(job.lease, work)
        async with session_scope() as session:
            repository = PipelineRunRepository(session)
            if result.status == "success":
                await repository.mark_success(job.run_id, result.metadata, lease=job.lease)
                return "completed"
            await repository.mark_partial(
                job.run_id, result.summary, result.metadata, lease=job.lease
            )
        log.warning(
            "snapshot.job.partial",
            run_id=job.run_id,
            kind=self.kind,
            summary=result.summary,
        )
        return "partial"

    async def _collect(self, job: ClaimedJob, artifacts: ArtifactWriter) -> _Result:
        request = job.request_json
        lease = job.lease
        if self.kind == UNIVERSE_SYNC:
            metadata = await sync_universe(
                UniverseSyncPayload.model_validate(request),
                client=self.runtime.reference_factory(),
                artifacts=artifacts,
                lease=lease,
            )
            return _Result("success", metadata)
        if self.kind == INDUSTRY_SYNC:
            metadata = await sync_industry(
                IndustrySyncPayload.model_validate(request),
                client=self.runtime.reference_factory(),
                artifacts=artifacts,
                lease=lease,
            )
            return _Result("success", metadata)
        if self.kind == MARKET_SNAPSHOT:
            market_payload = MarketSnapshotPayload.model_validate(request)
            if market_payload.provider == "baostock":
                report = await collect_baostock_market(
                    market_payload,
                    client=self.runtime.baostock_factory(),
                    artifacts=artifacts,
                    lease=lease,
                    now=self.runtime.clock,
                )
            else:
                async with self.runtime.proxy_factory() as client:
                    report = await collect_market_snapshot(
                        market_payload,
                        caller=self._caller(client),
                        artifacts=artifacts,
                        lease=lease,
                        now=self.runtime.clock,
                    )
            return await self._settle(report, job.run_id, lease)
        assert self.kind == FINANCIAL_SNAPSHOT
        financial_payload = FinancialSnapshotPayload.model_validate(request)
        if financial_payload.provider == "baostock":
            report = await collect_baostock_financial(
                financial_payload,
                client=self.runtime.baostock_factory(),
                artifacts=artifacts,
                lease=lease,
                now=self.runtime.clock,
            )
        else:
            async with self.runtime.proxy_factory() as client:
                report = await collect_financial_snapshot(
                    financial_payload,
                    caller=self._caller(client),
                    artifacts=artifacts,
                    lease=lease,
                    now=self.runtime.clock,
                )
        return await self._settle(report, job.run_id, lease)

    def _caller(self, client: ProxyClient) -> ProxyBatchCaller:
        return ProxyBatchCaller(
            client,
            self.runtime.limiter,
            retries=self.runtime.settings.ifind_collect_batch_retries,
            backoff_seconds=self.runtime.batch_backoff_seconds,
            sleep=self.runtime.sleep,
        )

    async def _settle(
        self, report: CollectionReport, run_id: int, lease: RunLease
    ) -> _Result:
        metadata = report.to_metadata()
        status = report.status
        if status == "failed":
            # Keep the evidence on the run before the worker retries / fails it.
            async with session_scope() as session:
                await PipelineRunRepository(session).record_metadata(
                    run_id, metadata, lease=lease
                )
            raise report.failure()
        return _Result(status, metadata, report.summary())

    async def finalize(self, job: ClaimedJob) -> None:
        try:
            await notify_snapshot_run(
                job.run_id,
                notifier=self.runtime.file_notifier(),
                threshold=self.runtime.settings.snapshot_completeness_threshold,
            )
        except Exception:
            log.exception("snapshot.job.notification_failed", run_id=job.run_id, kind=self.kind)


def snapshot_job_handlers(
    settings: Settings, runtime: SnapshotRuntime | None = None
) -> list[SnapshotJobHandler]:
    """One handler per snapshot kind, all sharing one proxy rate limiter.

    baostock calls need no token bucket: the process-wide baostock session
    lock already serializes them.
    """

    shared = runtime or default_runtime(settings)
    return [SnapshotJobHandler(kind, shared) for kind in SNAPSHOT_KINDS]
