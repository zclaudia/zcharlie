"""Deterministic daily valuation jobs (watch-holding-plan 2.10 / 6.2 WP6).

Reads never compute a valuation: the API returns a stored immutable result or
"更新中". This module owns the deterministic queue job that *does* compute:

* :func:`enqueue_valuation` - one run per ``(account, ledger revision, date)``
  with the knowledge cutoff frozen in the payload, so a worker retry reuses
  the exact input identity (:meth:`ValuationService.ensure` reuses the stored
  row) and never rewrites a historical result;
* :func:`enqueue_due_valuations` - the close-of-day / cross-midnight fan-out
  over every account, idempotent per account;
* :class:`PortfolioValuationHandler` - no LLM, no model budget; outcomes are
  ``completed`` (stored or reused) and ``partial`` (facts advanced during the
  computation: the audit row stays, the pointer does not move, the read keeps
  reporting "估值更新中").
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import structlog
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.clock import shanghai_today
from investment_agent.config import Settings
from investment_agent.investor.foundations import InvestorError
from investment_agent.jobs.base import BaseJobHandler, ClaimedJob, JobContext, JobOutcome
from investment_agent.portfolio.service import PortfolioService
from investment_agent.portfolio.valuation import ValuationService, default_knowledge_cutoff
from investment_agent.scheduler.trading_calendar import (
    UnknownTradingCalendarYearError,
    load_trading_calendar,
)
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.portfolio import PortfolioAccount
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository

log = structlog.get_logger(__name__)

VALUATION_JOB = "portfolio_valuation"


class ValuationJobPayload(BaseModel):
    """Frozen inputs of one daily valuation run (stable across retries)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    account_id: UUID
    valuation_date: date
    knowledge_cutoff: datetime


def valuation_job_key(account_id: UUID, *, revision: int, valuation_date: date) -> str:
    """One identity per (account, ledger version, date) - the exact input basis."""

    return f"{VALUATION_JOB}:{account_id}:{revision}:{valuation_date.isoformat()}"


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
) -> tuple[Any, bool]:
    """Queue one deterministic valuation run for the account's current version."""

    snapshot = await PortfolioService(session).snapshot(account_id)
    cutoff = knowledge_cutoff or datetime.now(UTC)
    payload = ValuationJobPayload(
        account_id=account_id,
        valuation_date=valuation_date,
        knowledge_cutoff=cutoff,
    )
    key = valuation_job_key(
        account_id, revision=revision if revision is not None else snapshot.revision, valuation_date=valuation_date
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
    now: datetime | None = None,
) -> list[int]:
    """Enqueue today's valuation for every account with a confirmed snapshot.

    Idempotent per (account, revision, date); returns the newly created run ids.
    """

    day = valuation_date or shanghai_today()
    at = now or datetime.now(UTC)
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
                knowledge_cutoff=min(default_knowledge_cutoff(day), at),
                triggered_by=triggered_by,
            )
        except InvestorError:  # an account without a confirmed snapshot cannot valuate
            log.info("portfolio.valuation.enqueue_skipped", account_id=account_id)
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
        log.exception("zone_detection.after_valuation_failed", run_id=str(payload.account_id))


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
    "PRICE_INGESTION_JOBS",
    "VALUATION_JOB",
    "PortfolioValuationHandler",
    "ValuationJobPayload",
    "calendar_predicate",
    "enqueue_due_valuations",
    "enqueue_valuation",
    "required_valuation_date",
    "valuation_job_key",
]
