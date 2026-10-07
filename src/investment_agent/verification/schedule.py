"""Verification schedule (P1-A13): dispatcher and weekly calibration sampling.

* **Dispatcher** (``verification.dispatch_cron``, default every 30 minutes):
  when requests are due (``pending``, or ``deferred`` with ``not_before <=
  now``) and no ``ifind_verify`` run is queued or running, enqueue one run
  keyed by the 30-minute UTC slot (``ifind_verify:<slot>``) - idempotent per
  slot, so a misfire catch-up or restart never doubles it. Deferred requests
  are therefore retried by the first slot after their ``not_before``.
* **Calibration** (``verification.calibration_cron``, default Sunday 21:00):
  queue ``calibration`` requests (all key fields, latest annual period) for
  ``calibration_count`` (default 10) enabled A-share watchlist stocks, sampled
  with a generator seeded by the ISO week - the same week always yields the
  same sample, a new week a new one. Open requests deduplicate a re-fire.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime

import structlog
from sqlalchemy import func, select

from investment_agent.snapshots.collect import SnapshotPreconditionError
from investment_agent.snapshots.jobs import FinancialSnapshotPayload
from investment_agent.snapshots.scope import scope_members
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.verification.budget import utc_now
from investment_agent.verification.job import IFIND_VERIFY, dispatch_slot, enqueue_verify_run
from investment_agent.verification.queue import (
    count_due,
    enqueue_verification,
    release_stale_running,
)

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class DispatchOutcome:
    due: int
    run_id: int | None = None
    created: bool = False
    reason: str = ""


async def dispatch_verification(
    *, now: datetime | None = None, triggered_by: str = "scheduler"
) -> DispatchOutcome:
    """Enqueue one ``ifind_verify`` run for the current slot when requests are due."""

    stamp = now or utc_now()
    async with session_scope() as session:
        await release_stale_running(session, now=stamp)
        due = await count_due(session, now=stamp)
        if due == 0:
            return DispatchOutcome(0, reason="nothing due")
        active = (
            await session.execute(
                select(func.min(PipelineRun.id)).where(
                    PipelineRun.kind == IFIND_VERIFY,
                    PipelineRun.status.in_(("queued", "running")),
                )
            )
        ).scalar_one_or_none()
        if active is not None:
            return DispatchOutcome(due, run_id=active, reason="a run is already queued or running")
        run, created = await enqueue_verify_run(
            session, slot=dispatch_slot(stamp), triggered_by=triggered_by
        )
        outcome = DispatchOutcome(due, run_id=run.id, created=created)
    log.info("verification.dispatch", due=due, run_id=outcome.run_id, created=outcome.created)
    return outcome


def calibration_week(day: date) -> str:
    year, week, _ = day.isocalendar()
    return f"{year}-W{week:02d}"


def calibration_sample(tickers: Sequence[str], *, week: str, count: int) -> list[str]:
    """Deterministic sample of ``count`` tickers for an ISO ``week`` (sorted output)."""

    pool = sorted(set(tickers))
    if count <= 0 or not pool:
        return []
    chosen = random.Random(f"calibration:{week}").sample(pool, min(count, len(pool)))
    return sorted(chosen)


async def enqueue_calibration(
    *, today: date, count: int, now: datetime | None = None
) -> list[str]:
    """Queue this week's calibration sample; returns the sampled tickers."""

    week = calibration_week(today)
    # The watchlist scope of the snapshot collectors: enabled A-share items.
    scope = FinancialSnapshotPayload(
        provider="ifind", scope="watchlist", report_period=date(today.year - 1, 12, 31), as_of=today
    )
    async with session_scope() as session:
        try:
            members = await scope_members(session, scope, today)
        except SnapshotPreconditionError:
            log.info("verification.calibration.skipped", week=week, reason="empty watchlist")
            return []
        sample = calibration_sample(list(members), week=week, count=count)
        created = 0
        for ticker in sample:
            created += await enqueue_verification(
                listing_id=members[ticker],
                ticker=ticker,
                trigger="calibration",
                detail={"week": week},
                session=session,
                now=now,
            )
    log.info("verification.calibration.queued", week=week, sample=sample, created=created)
    return sample
