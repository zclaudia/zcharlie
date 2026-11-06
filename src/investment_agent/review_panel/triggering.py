"""Automatic review triggers (design §8, §10.2).

Two triggers live here, both calling :func:`create_round` and never replacing
its validation:

* the change trigger, invoked after a successful watchlist research run whose
  change summary says the snapshot changed materially;
* the weekly scheduled batch, invoked by the scheduler on Saturday morning.

Both honour the per-listing automatic cooldown measured in *trading days* with
the reviewed A-share calendar (``config/calendars``). A rejection from
``create_round`` is logged and swallowed: a review is an add-on and must never
fail the run that produced the snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.clock import SHANGHAI_TZ, shanghai_today
from investment_agent.config import Settings
from investment_agent.domain.a_share import format_listing_ticker, parse_a_share_ticker
from investment_agent.review_panel.contracts import RoundTrigger
from investment_agent.review_panel.service import RoundRejected, create_round
from investment_agent.scheduler.trading_calendar import (
    TradingCalendar,
    is_trading_day_or_weekday,
    load_trading_calendar,
)
from investment_agent.storage.models.reference import Listing
from investment_agent.storage.models.review_panel import ReviewRoundRecord
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository
from investment_agent.storage.repositories.research_change import ResearchChangeRepository
from investment_agent.storage.repositories.research_snapshot import (
    ResearchSnapshotRepository,
)
from investment_agent.storage.repositories.review_panel import ReviewPanelRepository
from investment_agent.storage.repositories.watchlist import WatchlistRepository

log = structlog.get_logger(__name__)

#: Rejections that mean the day's shared quota is gone; a batch stops there.
QUOTA_EXHAUSTED_CODES = frozenset(
    {"daily_rounds_exhausted", "daily_tokens_exhausted"}
)
SUCCESS_RUN_STATUS = "success"


@dataclass(frozen=True)
class BatchOutcome:
    """What one scheduled batch run did (bounded and idempotent per date)."""

    batch_date: date
    created: int
    skipped: int
    stopped_reason: str | None = None


def load_effective_calendar() -> TradingCalendar | None:
    """The calendar in effect (data-dir copy first), or ``None`` when unreadable.

    Cooldown then falls back to the weekday rule, exactly like the other
    planning helpers; an unreadable calendar must not block automatic review.
    """

    try:
        from investment_agent.settings.loader import calendar_path

        return load_trading_calendar(Path(calendar_path()))
    except Exception:
        log.warning("review.trigger.calendar_unavailable", exc_info=True)
        return None


def _aware_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _local_date(value: datetime) -> date:
    return _aware_utc(value).astimezone(SHANGHAI_TZ).date()


def trading_days_between(
    calendar: TradingCalendar | None, start: date, end: date
) -> int:
    """Trading days strictly after ``start`` and no later than ``end``.

    ``is_trading_day_or_weekday`` keeps an unreviewed calendar year (or a
    missing calendar) from failing closed; the count is only a cooldown.
    """

    if end <= start:
        return 0
    cursor = start + timedelta(days=1)
    count = 0
    while cursor <= end:
        if is_trading_day_or_weekday(calendar, cursor):
            count += 1
        cursor += timedelta(days=1)
    return count


async def in_auto_cooldown(
    session: AsyncSession,
    settings: Settings,
    *,
    listing_id: int,
    calendar: TradingCalendar | None,
    today: date,
) -> bool:
    """Whether an automatic round started too recently for this listing.

    ``review_panel_auto_cooldown_trading_days = 0`` disables the cooldown.
    Both change-triggered and scheduled rounds count as automatic starts.
    """

    days = settings.review_panel_auto_cooldown_trading_days
    if days <= 0:
        return False
    last = await ReviewPanelRepository(session).last_auto_round_at(listing_id)
    if last is None:
        return False
    elapsed = trading_days_between(calendar, _local_date(last), today)
    return elapsed < days


async def _listing_symbol(session: AsyncSession, listing_id: int) -> tuple[str, str] | None:
    listing = await session.get(Listing, listing_id)
    if listing is None:
        return None
    ticker = format_listing_ticker(ticker=listing.ticker, market_code=listing.market_code)
    parsed = parse_a_share_ticker(ticker)
    if parsed is None:
        return None
    return listing.ticker, parsed[1].suffix


async def _latest_snapshot_material_state(
    session: AsyncSession, listing_id: int
) -> tuple[bool, datetime | None, datetime | None]:
    """``(has_material_change, latest_snapshot_created_at, last_completed_review_at)``.

    A material change is the existing deterministic signal
    (``meaningful_change_count > 0``) or ``needs_agent_review``. It is
    "unreviewed" when its snapshot is newer than the listing's latest completed
    round (or when the listing has never been reviewed).
    """

    symbol_market = await _listing_symbol(session, listing_id)
    if symbol_market is None:
        return False, None, None
    snapshot = await ResearchSnapshotRepository(session).latest(
        symbol=symbol_market[0], market=symbol_market[1]
    )
    last_round = await ReviewPanelRepository(session).latest_round(
        listing_id, completed_only=True
    )
    last_reviewed_at = (
        _aware_utc(last_round.completed_at or last_round.created_at)
        if last_round is not None
        else None
    )
    if snapshot is None:
        return False, None, last_reviewed_at
    change_set = await ResearchChangeRepository(session).by_snapshot(snapshot.id)
    needs_review = snapshot.score_json.get("needs_agent_review") is True
    material = needs_review or (
        change_set is not None and change_set.meaningful_change_count > 0
    )
    if not material:
        return False, _aware_utc(snapshot.created_at), last_reviewed_at
    snapshot_at = _aware_utc(snapshot.created_at)
    unreviewed = last_reviewed_at is None or snapshot_at > last_reviewed_at
    return unreviewed, snapshot_at, last_reviewed_at


async def trigger_change_review(
    session: AsyncSession,
    settings: Settings,
    *,
    listing_id: int,
    run_id: int,
    trigger_ref: str | None = None,
    calendar: TradingCalendar | None = None,
    today: date | None = None,
) -> ReviewRoundRecord | None:
    """Create one change-triggered round after a successful research run.

    Returns the round row, or ``None`` when a flag is off, the run produced no
    material change, the listing is cooling down, or ``create_round`` rejects
    the round. Never raises for a review decision.
    """

    if not (settings.review_panel_enabled and settings.review_panel_auto_enabled):
        return None
    run = await PipelineRunRepository(session).get(run_id)
    if (
        run is None
        or run.status != SUCCESS_RUN_STATUS
        or run.listing_id != listing_id
    ):
        return None
    snapshot = await ResearchSnapshotRepository(session).by_run(run_id)
    if snapshot is None:
        log.debug(
            "review.trigger.no_snapshot", listing_id=listing_id, run_id=run_id
        )
        return None
    change_set = await ResearchChangeRepository(session).by_run(run_id)
    needs_review = snapshot.score_json.get("needs_agent_review") is True
    material = needs_review or (
        change_set is not None and change_set.meaningful_change_count > 0
    )
    if not material:
        log.debug(
            "review.trigger.no_material_change",
            listing_id=listing_id,
            run_id=run_id,
        )
        return None
    if await in_auto_cooldown(
        session,
        settings,
        listing_id=listing_id,
        calendar=calendar,
        today=today or shanghai_today(),
    ):
        log.info(
            "review.trigger.cooldown",
            listing_id=listing_id,
            run_id=run_id,
            cooldown_trading_days=settings.review_panel_auto_cooldown_trading_days,
        )
        return None
    reference = trigger_ref or f"research_run:{run_id}"
    try:
        round_row = await create_round(
            session,
            settings,
            listing_id=listing_id,
            trigger=RoundTrigger.CHANGE,
            trigger_ref=reference,
        )
    except RoundRejected as exc:
        log.info(
            "review.trigger.rejected",
            listing_id=listing_id,
            run_id=run_id,
            code=exc.code,
        )
        return None
    log.info(
        "review.trigger.created",
        listing_id=listing_id,
        run_id=run_id,
        round_id=round_row.id,
        trigger_ref=reference,
    )
    return round_row


async def run_scheduled_review_batch(
    session: AsyncSession,
    settings: Settings,
    *,
    batch_date: date,
    calendar: TradingCalendar | None = None,
    today: date | None = None,
) -> BatchOutcome:
    """One bounded weekly batch (design §8, §10.2).

    Ordering: listings with an unreviewed material change first, then the
    longest since their last completed round (never reviewed first). Listings
    with an active round or inside the automatic cooldown are skipped. The
    batch stops when the configured per-batch cap is reached or when a daily
    quota rejection says no further round can be created today. ``trigger_ref``
    is stable per (batch date, listing), so re-running the same date creates no
    duplicates.
    """

    if not (settings.review_panel_enabled and settings.review_panel_schedule_enabled):
        return BatchOutcome(batch_date=batch_date, created=0, skipped=0)
    max_rounds = settings.review_panel_batch_max_rounds
    if max_rounds <= 0:
        return BatchOutcome(
            batch_date=batch_date, created=0, skipped=0, stopped_reason="batch_disabled"
        )
    local_today = today or batch_date
    items = await WatchlistRepository(session).list_enabled()
    candidates: list[tuple[tuple[bool, datetime], int]] = []
    for item in items:
        unreviewed, _snapshot_at, last_reviewed_at = (
            await _latest_snapshot_material_state(session, item.listing_id)
        )
        candidates.append(
            (
                (not unreviewed, last_reviewed_at or datetime.min.replace(tzinfo=UTC)),
                item.listing_id,
            )
        )
    candidates.sort(key=lambda entry: entry[0])

    repository = ReviewPanelRepository(session)
    created = 0
    skipped = 0
    stopped_reason: str | None = None
    for _priority, listing_id in candidates:
        if created >= max_rounds:
            stopped_reason = "batch_cap"
            break
        if await repository.active_round_exists(listing_id):
            skipped += 1
            continue
        if await in_auto_cooldown(
            session,
            settings,
            listing_id=listing_id,
            calendar=calendar,
            today=local_today,
        ):
            skipped += 1
            continue
        reference = f"review_batch:{batch_date.isoformat()}:{listing_id}"
        try:
            await create_round(
                session,
                settings,
                listing_id=listing_id,
                trigger=RoundTrigger.SCHEDULED,
                trigger_ref=reference,
            )
        except RoundRejected as exc:
            if exc.code in QUOTA_EXHAUSTED_CODES:
                stopped_reason = exc.code
                break
            skipped += 1
            log.info(
                "review.batch.rejected",
                batch_date=batch_date.isoformat(),
                listing_id=listing_id,
                code=exc.code,
            )
            continue
        created += 1
        log.info(
            "review.batch.created",
            batch_date=batch_date.isoformat(),
            listing_id=listing_id,
            trigger_ref=reference,
        )
    log.info(
        "review.batch.finished",
        batch_date=batch_date.isoformat(),
        created=created,
        skipped=skipped,
        stopped_reason=stopped_reason,
    )
    return BatchOutcome(
        batch_date=batch_date,
        created=created,
        skipped=skipped,
        stopped_reason=stopped_reason,
    )


__all__ = [
    "BatchOutcome",
    "QUOTA_EXHAUSTED_CODES",
    "in_auto_cooldown",
    "load_effective_calendar",
    "run_scheduled_review_batch",
    "trading_days_between",
    "trigger_change_review",
]
