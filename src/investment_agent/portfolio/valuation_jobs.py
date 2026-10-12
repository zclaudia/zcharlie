"""Deterministic daily valuation jobs (watch-holding-plan 2.10 / 6.2 WP6).

Reads never compute a valuation: the API returns a stored immutable result or
"更新中". This module owns the deterministic queue job that *does* compute:

* :func:`enqueue_valuation` - one run per ``(account, ledger revision, date,
  price knowledge watermark)``. The payload's knowledge cutoff is the
  deterministic day-end (never the wall clock), so a repeated enqueue with the
  same key carries the same payload and reuses the queued run instead of
  raising an idempotency mismatch; the watermark in the key is what lets a
  newly ingested close price mint a *new* identity for the same day, so the
  post-close fan-out recomputes instead of silently reusing the morning run;
* :func:`enqueue_due_valuations` - the close-of-day / cross-midnight fan-out
  over every account, idempotent per account and isolated per account (one
  broken account never aborts the rest);
* :class:`PortfolioValuationHandler` - no LLM, no model budget; outcomes are
  ``completed`` (stored or reused) and ``partial`` (facts advanced during the
  computation: the audit row stays, the pointer does not move, the read keeps
  reporting "估值更新中").
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import structlog
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.clock import shanghai_today
from investment_agent.config import Settings
from investment_agent.investor.foundations import InvestorError
from investment_agent.jobs.base import BaseJobHandler, ClaimedJob, JobContext, JobOutcome
from investment_agent.portfolio.service import PortfolioService
from investment_agent.portfolio.valuation import ValuationService, day_knowledge_cutoff
from investment_agent.scheduler.trading_calendar import (
    UnknownTradingCalendarYearError,
    load_trading_calendar,
)
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.pit_snapshot import MarketSnapshot
from investment_agent.storage.models.portfolio import PortfolioAccount
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository

log = structlog.get_logger(__name__)

VALUATION_JOB = "portfolio_valuation"

NO_PRICE_KNOWLEDGE = "none"
"""Watermark of an account whose confirmed snapshot holds nothing to price."""


class ValuationJobPayload(BaseModel):
    """Frozen inputs of one daily valuation run (stable across retries)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    account_id: UUID
    valuation_date: date
    knowledge_cutoff: datetime


def valuation_job_key(
    account_id: UUID,
    *,
    revision: int,
    valuation_date: date,
    knowledge_watermark: str | None = None,
) -> str:
    """One identity per (account, ledger version, date, price knowledge).

    The watermark keeps identities distinct when only prices moved: a newly
    ingested close must revalue the same ledger revision on the same day.
    """

    key = f"{VALUATION_JOB}:{account_id}:{revision}:{valuation_date.isoformat()}"
    if knowledge_watermark is not None:
        key = f"{key}:{knowledge_watermark}"
    return key


async def price_knowledge_watermark(session: AsyncSession, listing_ids: Sequence[int]) -> str:
    """The newest ``captured_at`` among a holding set's stored snapshots.

    Part of the valuation identity: newly ingested relevant prices advance the
    watermark, so the next enqueue for the same (account, revision, date)
    creates a new run instead of reusing the stale one (WP6).
    """

    if not listing_ids:
        return NO_PRICE_KNOWLEDGE
    captured_at = await session.scalar(
        select(func.max(MarketSnapshot.captured_at)).where(
            MarketSnapshot.listing_id.in_(int(listing_id) for listing_id in listing_ids)
        )
    )
    if captured_at is None:
        return NO_PRICE_KNOWLEDGE
    if captured_at.tzinfo is None:  # SQLite returns naive datetimes
        captured_at = captured_at.replace(tzinfo=UTC)
    return captured_at.astimezone(UTC).isoformat()


def calendar_predicate(calendar_path: str) -> Any:
    """``date -> True | False | None``: reviewed days, or an unreviewed year.

    ``None`` means the year is not in the trading calendar: staleness cannot be
    settled then, and the valuation reports a limitation instead of treating
    weekdays as reviewed data (plan 2.10 / WP10).
    """

    calendar = load_trading_calendar(Path(calendar_path))

    def predicate(value: date) -> bool | None:
        try:
            return calendar.is_trading_day(value)
        except UnknownTradingCalendarYearError:
            return None

    return predicate


async def enqueue_valuation(
    session: AsyncSession,
    *,
    account_id: UUID,
    valuation_date: date,
    knowledge_cutoff: datetime | None = None,
    triggered_by: str,
    revision: int | None = None,
    knowledge_watermark: str | None = None,
) -> tuple[Any, bool]:
    """Queue one deterministic valuation run for the account's current version.

    Without an explicit cutoff the payload carries the valuation date's
    deterministic day-end boundary, so the same key always carries the same
    payload. Without an explicit watermark the account's current price
    knowledge is measured here, so a newly ingested close price revalues the
    same (account, revision, date) under a fresh identity (WP6).
    """

    snapshot = await PortfolioService(session).snapshot(account_id)
    cutoff = knowledge_cutoff or day_knowledge_cutoff(valuation_date)
    if knowledge_watermark is None:
        knowledge_watermark = await price_knowledge_watermark(
            session,
            # Listing refs carry integer ids by contract (identifier_namespace).
            [int(holding.listing_ref.id) for holding in snapshot.holdings],
        )
    payload = ValuationJobPayload(
        account_id=account_id,
        valuation_date=valuation_date,
        knowledge_cutoff=cutoff,
    )
    key = valuation_job_key(
        account_id,
        revision=revision if revision is not None else snapshot.revision,
        valuation_date=valuation_date,
        knowledge_watermark=knowledge_watermark,
    )
    return await PipelineRunRepository(session).enqueue(
        kind=VALUATION_JOB,
        idempotency_key=key,
        triggered_by=triggered_by,
        listing_id=None,
        request_json=payload.model_dump(mode="json"),
    )


async def enqueue_due_valuations(
    session: AsyncSession,
    *,
    triggered_by: str,
    valuation_date: date | None = None,
) -> list[int]:
    """Enqueue the day's valuation for every account with a confirmed snapshot.

    Idempotent per (account, revision, date, price knowledge); each account is
    enqueued in its own error boundary, so one broken account cannot abort the
    fan-out. Returns the newly created run ids.
    """

    day = valuation_date or shanghai_today()
    account_ids = list(
        (await session.scalars(select(PortfolioAccount.id).order_by(PortfolioAccount.id))).all()
    )
    created: list[int] = []
    for account_id in account_ids:
        try:
            run, was_created = await enqueue_valuation(
                session,
                account_id=UUID(account_id),
                valuation_date=day,
                triggered_by=triggered_by,
            )
        except InvestorError:  # an account without a confirmed snapshot cannot valuate
            log.info("portfolio.valuation.enqueue_skipped", account_id=account_id)
            continue
        except Exception:  # one account's failure must not abort the fan-out
            log.exception("portfolio.valuation.enqueue_failed", account_id=account_id)
            continue
        if was_created:
            created.append(run.id)
    return created


def required_valuation_date(
    today: date,
    is_trading_day: Callable[[date], bool | None] | None,
    *,
    lookback_days: int = 15,
) -> date | None:
    """The trading date the current valuation should carry.

    ``None`` when the calendar cannot decide (unreviewed year): the read then
    simply returns the stored result instead of claiming freshness.
    """

    if is_trading_day is None:
        return None
    for offset in range(lookback_days):
        day = today - timedelta(days=offset)
        status = is_trading_day(day)
        if status is None:
            return None
        if status:
            return day
    return None


PRICE_INGESTION_JOBS = frozenset({"close_quote", "close_quote_retry", "market_snapshot"})
"""Snapshot jobs after which the day's portfolio valuations are queued (WP6)."""


class PortfolioValuationHandler(BaseJobHandler):
    """The deterministic valuation run (never calls a model)."""

    kind = VALUATION_JOB

    def __init__(self, *, settings: Settings, calendar_path: str | None = None) -> None:
        self.settings = settings
        self.calendar_path = calendar_path

    async def execute(self, job: ClaimedJob, ctx: JobContext) -> JobOutcome:
        payload = ValuationJobPayload.model_validate(job.request_json)
        predicate = None
        if self.calendar_path:
            try:
                predicate = calendar_predicate(self.calendar_path)
            except Exception:
                log.exception("portfolio.valuation.calendar_unavailable", path=self.calendar_path)
        async with session_scope() as session:
            max_staleness = await _max_staleness_days(session, self.settings)

        async def work() -> Any:
            async with session_scope() as session:
                return await ValuationService(session).ensure(
                    payload.account_id,
                    valuation_date=payload.valuation_date,
                    now=payload.knowledge_cutoff,
                    is_trading_day=predicate,
                    max_staleness_days=max_staleness,
                )

        value = await ctx.run_with_heartbeat(job.lease, work)
        async with session_scope() as session:
            runs = PipelineRunRepository(session)
            metadata: dict[str, object] = {
                "account_id": str(payload.account_id),
                "valuation_date": payload.valuation_date.isoformat(),
                "knowledge_cutoff": payload.knowledge_cutoff.isoformat(),
            }
            if value is None:
                metadata["valuation_status"] = "ledger_advanced"
                await runs.mark_partial(
                    job.run_id, "ledger_advanced_during_valuation", metadata, lease=job.lease
                )
                log.info("portfolio.valuation.ledger_advanced", run_id=job.run_id)
                return "partial"
            metadata.update(
                {
                    "valuation_status": "stored",
                    "valuation_id": str(value.id),
                    "ledger_revision": value.ledger_revision,
                    "total_assets": str(value.total_assets) if value.total_assets is not None else None,
                }
            )
            await runs.mark_success(job.run_id, metadata, lease=job.lease)
        log.info(
            "portfolio.valuation.finished",
            run_id=job.run_id,
            valuation_id=str(value.id),
            ledger_revision=value.ledger_revision,
        )
        await _enqueue_zone_detection_after_valuation(payload)
        return "completed"


async def _enqueue_zone_detection_after_valuation(payload: ValuationJobPayload) -> None:
    """A stored valuation is a new holding input: re-run the holding zones (WP8).

    The trigger token carries the account, so one account's completion cannot
    hide another account's new valuation behind the same run identity (each run
    still evaluates every account's current valuation).
    """

    from investment_agent.rules.zone_detection import HOLDING_SCOPE
    from investment_agent.rules.zone_jobs import enqueue_zone_detection

    try:
        async with session_scope() as session:
            await enqueue_zone_detection(
                session,
                as_of=payload.valuation_date,
                scope=HOLDING_SCOPE,
                triggered_by="portfolio_valuation",
                reason=f"valuation:{payload.account_id}",
            )
    except Exception:
        # The scheduler's cron and the ingestion hook remain as safety nets.
        log.exception("zone_detection.after_valuation_failed", account_id=str(payload.account_id))


async def _max_staleness_days(session: AsyncSession, settings: Settings) -> int:
    """The runtime-configured price-age threshold (WP10); module default offline."""

    from investment_agent.settings.loader import load_settings

    try:
        selected = await load_settings(session, base=settings)
        return int(selected.portfolio_valuation_max_staleness_days)
    except Exception:
        log.exception("portfolio.valuation.settings_unavailable")
        return int(settings.portfolio_valuation_max_staleness_days)


__all__ = [
    "NO_PRICE_KNOWLEDGE",
    "PRICE_INGESTION_JOBS",
    "VALUATION_JOB",
    "PortfolioValuationHandler",
    "ValuationJobPayload",
    "calendar_predicate",
    "enqueue_due_valuations",
    "enqueue_valuation",
    "price_knowledge_watermark",
    "required_valuation_date",
    "valuation_job_key",
]
