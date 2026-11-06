"""Automatic review triggers: change trigger and weekly batch (design §8)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select

from investment_agent.config import Settings
from investment_agent.domain.a_share import format_listing_ticker, parse_a_share_ticker
from investment_agent.review_panel import triggering
from investment_agent.review_panel.contracts import RoundStatus, RoundTrigger
from investment_agent.review_panel.service import RoundRejected
from investment_agent.review_panel.triggering import (
    run_scheduled_review_batch,
    trading_days_between,
    trigger_change_review,
)
from investment_agent.scheduler.trading_calendar import load_trading_calendar
from investment_agent.storage.models.review_panel import ReviewRoundRecord
from investment_agent.storage.repositories.listing import ListingRepository
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository
from investment_agent.storage.repositories.research_snapshot import (
    ResearchSnapshotRepository,
)
from investment_agent.storage.repositories.review_panel import ReviewPanelRepository
from investment_agent.storage.repositories.watchlist import WatchlistRepository
from tests.support.llm_catalog import catalog_values, slot_values

CALENDAR = load_trading_calendar(Path("config/calendars/a_share.yaml"))


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "api_key": "fixture",
        "llm_catalog": json.dumps(catalog_values()),
        "model_slots": json.dumps(slot_values()),
        "review_panel_enabled": True,
        "review_panel_auto_enabled": True,
        "review_panel_schedule_enabled": True,
        "review_panel_auto_cooldown_trading_days": 3,
        "review_panels": _panels(),
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _panels() -> str:
    return json.dumps(
        {
            "default_panel": "default",
            "panels": {
                "default": {
                    "name": "默认评审组",
                    "seats": [
                        {"seat_id": "value", "lens_id": "value", "model_ref": "primary/reason"},
                        {"seat_id": "bear", "lens_id": "bear", "model_ref": "primary/fast"},
                    ],
                }
            },
        }
    )


def _score(*, review: bool, investment: float = 70.0) -> dict[str, object]:
    return {
        "investment_score": investment,
        "research_confidence_score": 80.0,
        "strategy_label": "quality",
        "needs_agent_review": review,
        "scoring_version": "quality.v1",
        "source_mode": "ifind",
    }


async def _listing(db_session, ticker: str = "600036.SH"):
    return await ListingRepository(db_session).ensure_a_share_listing(
        ticker, company_name="招商银行"
    )


async def _watch(db_session, listing) -> None:
    await WatchlistRepository(db_session).add(listing.id)


async def _snapshot(
    db_session,
    listing,
    *,
    score: dict[str, object],
    as_of: date = date(2026, 11, 6),
):
    run = await PipelineRunRepository(db_session).create(
        kind="ifind_research",
        listing_id=listing.id,
        status="success",
        stage="completed",
    )
    ticker = format_listing_ticker(
        ticker=listing.ticker, market_code=listing.market_code
    )
    parsed = parse_a_share_ticker(ticker)
    assert parsed is not None
    snapshot = await ResearchSnapshotRepository(db_session).create(
        pipeline_run_id=run.id,
        symbol=listing.ticker,
        market=parsed[1].suffix,
        company_name="招商银行",
        industry="银行",
        as_of=as_of,
        report_year=2025,
        canonical_schema_version="v1",
        scoring_version="quality.v1",
        score_schema_version="v1",
        score_json=score,
        explanation_json=None,
        artifact_manifest={},
    )
    return run, snapshot


async def _changed_run(db_session, listing):
    """A baseline snapshot, then a materially changed one; returns the latter run."""

    await _snapshot(db_session, listing, score=_score(review=False, investment=70.0))
    run, _snapshot_row = await _snapshot(
        db_session, listing, score=_score(review=False, investment=78.0)
    )
    return run


async def _completed_round(
    db_session,
    listing_id: int,
    *,
    trigger: RoundTrigger,
    created_at: datetime,
    trigger_ref: str | None = None,
) -> ReviewRoundRecord:
    row = await ReviewPanelRepository(db_session).create_round(
        listing_id=listing_id,
        trigger=trigger,
        seats=[],
        panel_id="default",
        panel_snapshot={},
        as_of=date(2026, 1, 1),
        evidence_cutoff=datetime(2026, 1, 1, tzinfo=UTC),
        base_pack_hash="0" * 64,
        reserved_tokens=0,
        reserved_requests=0,
        trigger_ref=trigger_ref,
        status=RoundStatus.COMPLETED,
    )
    row.created_at = created_at
    row.completed_at = created_at
    await db_session.flush()
    return row


async def _round_count(db_session, listing_id: int | None = None) -> int:
    stmt = select(func.count()).select_from(ReviewRoundRecord)
    if listing_id is not None:
        stmt = stmt.where(ReviewRoundRecord.listing_id == listing_id)
    return int(await db_session.scalar(stmt) or 0)


# --------------------------------------------------------------------------- #
# Change trigger
# --------------------------------------------------------------------------- #


async def test_change_trigger_creates_one_round_for_the_run(db_session) -> None:
    listing = await _listing(db_session)
    await _watch(db_session, listing)
    run = await _changed_run(db_session, listing)

    row = await trigger_change_review(
        db_session,
        _settings(),
        listing_id=listing.id,
        run_id=run.id,
        calendar=CALENDAR,
        today=date(2026, 11, 6),
    )

    assert row is not None
    assert row.trigger == RoundTrigger.CHANGE.value
    assert row.trigger_ref == f"research_run:{run.id}"
    assert await _round_count(db_session, listing.id) == 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"review_panel_enabled": False},
        {"review_panel_auto_enabled": False},
    ],
)
async def test_change_trigger_respects_both_flags(db_session, overrides) -> None:
    listing = await _listing(db_session)
    await _watch(db_session, listing)
    run = await _changed_run(db_session, listing)

    row = await trigger_change_review(
        db_session,
        _settings(**overrides),
        listing_id=listing.id,
        run_id=run.id,
        calendar=CALENDAR,
        today=date(2026, 11, 6),
    )

    assert row is None
    assert await _round_count(db_session) == 0


async def test_change_trigger_skips_without_material_change(db_session) -> None:
    listing = await _listing(db_session)
    await _watch(db_session, listing)
    run, _ = await _snapshot(db_session, listing, score=_score(review=False))

    assert await trigger_change_review(
        db_session,
        _settings(),
        listing_id=listing.id,
        run_id=run.id,
        calendar=CALENDAR,
        today=date(2026, 11, 6),
    ) is None
    assert await _round_count(db_session) == 0


async def test_change_trigger_fires_on_needs_agent_review(db_session) -> None:
    listing = await _listing(db_session)
    await _watch(db_session, listing)
    run, _ = await _snapshot(db_session, listing, score=_score(review=True))

    row = await trigger_change_review(
        db_session,
        _settings(),
        listing_id=listing.id,
        run_id=run.id,
        calendar=CALENDAR,
        today=date(2026, 11, 6),
    )
    assert row is not None


async def test_change_trigger_cooldown_counts_trading_days(db_session) -> None:
    listing = await _listing(db_session)
    await _watch(db_session, listing)
    run = await _changed_run(db_session, listing)
    # Monday 2026-11-02, an automatic start.
    await _completed_round(
        db_session,
        listing.id,
        trigger=RoundTrigger.CHANGE,
        created_at=datetime(2026, 11, 2, 1, tzinfo=UTC),
        trigger_ref="prior-change",
    )

    # Wednesday: only Tuesday and Wednesday elapsed -> younger than 3.
    assert await trigger_change_review(
        db_session,
        _settings(),
        listing_id=listing.id,
        run_id=run.id,
        calendar=CALENDAR,
        today=date(2026, 11, 4),
    ) is None
    # Thursday: Tuesday, Wednesday, Thursday elapsed -> allowed.
    row = await trigger_change_review(
        db_session,
        _settings(),
        listing_id=listing.id,
        run_id=run.id,
        calendar=CALENDAR,
        today=date(2026, 11, 5),
    )
    assert row is not None


async def test_zero_cooldown_disables_the_wait(db_session) -> None:
    listing = await _listing(db_session)
    await _watch(db_session, listing)
    run = await _changed_run(db_session, listing)
    await _completed_round(
        db_session,
        listing.id,
        trigger=RoundTrigger.SCHEDULED,
        created_at=datetime(2026, 11, 5, 23, tzinfo=UTC),
        trigger_ref="prior-scheduled",
    )

    row = await trigger_change_review(
        db_session,
        _settings(review_panel_auto_cooldown_trading_days=0),
        listing_id=listing.id,
        run_id=run.id,
        calendar=CALENDAR,
        today=date(2026, 11, 6),
    )
    assert row is not None


async def test_change_trigger_dedupes_by_trigger_ref(db_session) -> None:
    listing = await _listing(db_session)
    await _watch(db_session, listing)
    run = await _changed_run(db_session, listing)
    settings = _settings(review_panel_auto_cooldown_trading_days=0)

    first = await trigger_change_review(
        db_session, settings, listing_id=listing.id, run_id=run.id,
        calendar=CALENDAR, today=date(2026, 11, 6),
    )
    assert first is not None
    await ReviewPanelRepository(db_session).set_round_status(
        first.id, RoundStatus.COMPLETED
    )
    second = await trigger_change_review(
        db_session, settings, listing_id=listing.id, run_id=run.id,
        calendar=CALENDAR, today=date(2026, 11, 6),
    )

    assert second is None
    assert await _round_count(db_session, listing.id) == 1


async def test_round_rejection_is_swallowed(db_session) -> None:
    listing = await _listing(db_session)
    await _watch(db_session, listing)
    run = await _changed_run(db_session, listing)

    # No panel configured: create_round raises panel_not_configured.
    assert await trigger_change_review(
        db_session,
        _settings(review_panels=""),
        listing_id=listing.id,
        run_id=run.id,
        calendar=CALENDAR,
        today=date(2026, 11, 6),
    ) is None
    assert await _round_count(db_session) == 0


async def test_non_success_run_is_ignored(db_session) -> None:
    listing = await _listing(db_session)
    await _watch(db_session, listing)
    run = await _changed_run(db_session, listing)
    run.status = "failed"
    await db_session.flush()

    assert await trigger_change_review(
        db_session,
        _settings(),
        listing_id=listing.id,
        run_id=run.id,
        calendar=CALENDAR,
        today=date(2026, 11, 6),
    ) is None


def test_trading_days_between_uses_the_calendar() -> None:
    # National Day holiday 2026-10-01..07 is closed; 09-30 and 10-08 are open.
    assert trading_days_between(CALENDAR, date(2026, 9, 30), date(2026, 10, 8)) == 1
    assert trading_days_between(CALENDAR, date(2026, 11, 2), date(2026, 11, 5)) == 3
    assert trading_days_between(CALENDAR, date(2026, 11, 5), date(2026, 11, 5)) == 0


# --------------------------------------------------------------------------- #
# Weekly batch
# --------------------------------------------------------------------------- #


async def _batch_listing(db_session, ticker: str, *, material: bool):
    listing = await _listing(db_session, ticker)
    await _watch(db_session, listing)
    if material:
        await _changed_run(db_session, listing)
    else:
        await _snapshot(db_session, listing, score=_score(review=False))
    return listing


async def test_batch_orders_unreviewed_material_then_oldest_review(
    db_session, monkeypatch
) -> None:
    fresh = await _batch_listing(db_session, "600036.SH", material=True)
    old = await _batch_listing(db_session, "600519.SH", material=False)
    recent = await _batch_listing(db_session, "601318.SH", material=False)
    await _completed_round(
        db_session, old.id, trigger=RoundTrigger.MANUAL,
        created_at=datetime(2026, 1, 5, tzinfo=UTC),
    )
    await _completed_round(
        db_session, recent.id, trigger=RoundTrigger.MANUAL,
        created_at=datetime(2026, 10, 5, tzinfo=UTC),
    )

    calls: list[int] = []

    async def fake_create(session, settings, *, listing_id, trigger, trigger_ref=None, **kwargs):
        calls.append(listing_id)
        return ReviewRoundRecord(id=len(calls))

    monkeypatch.setattr(triggering, "create_round", fake_create)
    outcome = await run_scheduled_review_batch(
        db_session,
        _settings(review_panel_batch_max_rounds=10),
        batch_date=date(2026, 11, 7),
        calendar=CALENDAR,
        today=date(2026, 11, 7),
    )

    assert outcome.created == 3
    assert calls == [fresh.id, old.id, recent.id]


async def test_batch_cap_bounds_one_run(db_session, monkeypatch) -> None:
    first = await _batch_listing(db_session, "600036.SH", material=True)
    second = await _batch_listing(db_session, "600519.SH", material=True)
    await _batch_listing(db_session, "601318.SH", material=True)
    calls: list[int] = []

    async def fake_create(session, settings, *, listing_id, trigger, trigger_ref=None, **kwargs):
        calls.append(listing_id)
        return ReviewRoundRecord(id=len(calls))

    monkeypatch.setattr(triggering, "create_round", fake_create)
    outcome = await run_scheduled_review_batch(
        db_session,
        _settings(review_panel_batch_max_rounds=2),
        batch_date=date(2026, 11, 7),
        calendar=CALENDAR,
        today=date(2026, 11, 7),
    )

    assert calls == [first.id, second.id]
    assert outcome.created == 2
    assert outcome.stopped_reason == "batch_cap"


async def test_batch_stops_on_daily_quota_exhaustion(db_session, monkeypatch) -> None:
    listings = [
        await _batch_listing(db_session, ticker, material=True)
        for ticker in ("600036.SH", "600519.SH", "601318.SH")
    ]
    calls: list[int] = []

    async def fake_create(session, settings, *, listing_id, trigger, trigger_ref=None, **kwargs):
        calls.append(listing_id)
        if len(calls) == 2:
            raise RoundRejected("daily_rounds_exhausted", "quota")
        return ReviewRoundRecord(id=len(calls))

    monkeypatch.setattr(triggering, "create_round", fake_create)
    outcome = await run_scheduled_review_batch(
        db_session,
        _settings(review_panel_batch_max_rounds=10),
        batch_date=date(2026, 11, 7),
        calendar=CALENDAR,
        today=date(2026, 11, 7),
    )

    assert calls == [listings[0].id, listings[1].id]
    assert outcome.created == 1
    assert outcome.stopped_reason == "daily_rounds_exhausted"


async def test_batch_skips_active_round_and_cooldown(db_session, monkeypatch) -> None:
    active = await _batch_listing(db_session, "600036.SH", material=True)
    cooling = await _batch_listing(db_session, "600519.SH", material=True)
    available = await _batch_listing(db_session, "601318.SH", material=True)
    await ReviewPanelRepository(db_session).create_round(
        listing_id=active.id,
        trigger=RoundTrigger.MANUAL,
        seats=[],
        panel_id="default",
        panel_snapshot={},
        as_of=date(2026, 11, 6),
        evidence_cutoff=datetime(2026, 11, 6, tzinfo=UTC),
        base_pack_hash="0" * 64,
        reserved_tokens=0,
        reserved_requests=0,
        status=RoundStatus.QUEUED,
    )
    await _completed_round(
        db_session, cooling.id, trigger=RoundTrigger.CHANGE,
        created_at=datetime(2026, 11, 5, 1, tzinfo=UTC),
    )

    calls: list[int] = []

    async def fake_create(session, settings, *, listing_id, trigger, trigger_ref=None, **kwargs):
        calls.append(listing_id)
        return ReviewRoundRecord(id=len(calls))

    monkeypatch.setattr(triggering, "create_round", fake_create)
    outcome = await run_scheduled_review_batch(
        db_session,
        _settings(review_panel_batch_max_rounds=10),
        batch_date=date(2026, 11, 7),
        calendar=CALENDAR,
        today=date(2026, 11, 7),
    )

    assert calls == [available.id]
    assert outcome.created == 1
    assert outcome.skipped == 2


async def test_batch_is_idempotent_for_the_same_date(db_session) -> None:
    first = await _batch_listing(db_session, "600036.SH", material=True)
    second = await _batch_listing(db_session, "600519.SH", material=True)
    settings = _settings(
        review_panel_batch_max_rounds=10,
        review_panel_auto_cooldown_trading_days=0,
    )

    one = await run_scheduled_review_batch(
        db_session, settings, batch_date=date(2026, 11, 7),
        calendar=CALENDAR, today=date(2026, 11, 7),
    )
    # Finish the rounds so the second run reaches the trigger_ref dedupe check
    # instead of stopping at the active-round guard.
    for row in (
        await db_session.execute(
            select(ReviewRoundRecord).where(
                ReviewRoundRecord.listing_id.in_([first.id, second.id])
            )
        )
    ).scalars():
        await ReviewPanelRepository(db_session).set_round_status(
            row.id, RoundStatus.COMPLETED
        )
    two = await run_scheduled_review_batch(
        db_session, settings, batch_date=date(2026, 11, 7),
        calendar=CALENDAR, today=date(2026, 11, 7),
    )

    assert one.created == 2
    assert two.created == 0
    assert await _round_count(db_session) == 2


@pytest.mark.parametrize(
    "overrides",
    [
        {"review_panel_enabled": False},
        {"review_panel_schedule_enabled": False},
        {"review_panel_batch_max_rounds": 0},
    ],
)
async def test_batch_flags_and_zero_cap_do_nothing(db_session, overrides) -> None:
    await _batch_listing(db_session, "600036.SH", material=True)

    outcome = await run_scheduled_review_batch(
        db_session,
        _settings(**overrides),
        batch_date=date(2026, 11, 7),
        calendar=CALENDAR,
        today=date(2026, 11, 7),
    )
    assert outcome.created == 0
    assert await _round_count(db_session) == 0
