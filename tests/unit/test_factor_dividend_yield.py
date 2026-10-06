"""Trailing dividend yield (F2 / Q2): factor quality cases on pure views and the
point-in-time database view."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from investment_agent.factors.catalog import DEFAULT_REGISTRY
from investment_agent.factors.library import (
    FLAG_DIVIDENDS_NOT_COLLECTED,
    FLAG_DIVIDENDS_STALE,
    FLAG_NO_PRICE,
)
from investment_agent.factors.registry import FactorValue
from investment_agent.factors.view import (
    OBSERVATION,
    Bar,
    Dividend,
    DividendHistory,
    SnapshotView,
    load_snapshot_view,
)
from investment_agent.storage.database import session_scope
from investment_agent.storage.repositories.dividend import DividendRepository
from investment_agent.storage.repositories.listing import ListingRepository
from investment_agent.storage.repositories.market_snapshot import MarketSnapshotRepository

AS_OF = date(2026, 9, 30)


def _compute(view: SnapshotView) -> FactorValue:
    return DEFAULT_REGISTRY.get("dividend_yield_ttm").compute(view)


# --------------------------------------------------------------------------- #
# Factor (pure view)
# --------------------------------------------------------------------------- #


def _view(
    *,
    dividends: DividendHistory | None,
    close: float | None = 10.0,
    as_of: date = AS_OF,
    **kwargs,
) -> SnapshotView:
    bars = [Bar(trade_date=as_of, close=close, ref="bar")] if close is not None else []
    return SnapshotView.build(
        ticker="600900.SH",
        as_of=as_of,
        industry_code="D44",
        industry_name="电力、热力生产和供应业",
        raw_bars=bars,
        adjusted_bars=bars,
        dividends=dividends,
        **kwargs,
    )


COVERED = {2025: date(2026, 9, 27), 2026: date(2026, 9, 27)}


def test_yield_sums_cash_dividends_in_the_trailing_window():
    history = DividendHistory(
        events=(
            Dividend(date(2025, 9, 30), 9.0, ref="a"),  # exactly 365 days before: excluded
            Dividend(date(2025, 10, 1), 0.2, ref="b"),
            Dividend(date(2026, 7, 17), 0.3, ref="c"),
            Dividend(date(2026, 10, 9), 5.0, ref="future"),  # ex-date after as_of
        ),
        covered_years=COVERED,
    )
    value = _compute(_view(dividends=history))
    assert value.quality == "ok" and FLAG_DIVIDENDS_STALE not in value.flags
    assert value.value == pytest.approx(0.05)
    assert [item["ref"] for item in value.inputs_ref["dividends"]] == ["b", "c"]
    assert value.inputs_ref["market"]["trade_date"] == "2026-09-30"


def test_yield_is_zero_without_dividends_but_missing_when_not_collected():
    assert _compute(_view(dividends=DividendHistory(covered_years=COVERED))).value == 0.0
    partial = _compute(_view(dividends=DividendHistory(covered_years={2026: date(2026, 9, 27)})))
    assert partial.quality == "missing" and FLAG_DIVIDENDS_NOT_COLLECTED in partial.flags
    assert partial.inputs_ref["uncovered_years"] == [2025]
    absent = _compute(_view(dividends=None))
    assert absent.quality == "missing" and FLAG_DIVIDENDS_NOT_COLLECTED in absent.flags


def test_yield_missing_without_price():
    value = _compute(_view(dividends=DividendHistory(covered_years=COVERED), close=None))
    assert value.quality == "missing" and value.flags[0] == FLAG_NO_PRICE


def test_events_not_public_by_as_of_are_invisible():
    history = DividendHistory(
        events=(Dividend(date(2026, 9, 1), 1.0, available_from=date(2026, 10, 2)),),
        covered_years=COVERED,
    )
    assert _compute(_view(dividends=history)).value == 0.0


def test_later_bonus_issue_restates_earlier_cash():
    history = DividendHistory(
        events=(
            Dividend(date(2026, 3, 1), 2.52),
            Dividend(date(2026, 4, 26), 1.8, share_multiplier=1.8),
        ),
        covered_years=COVERED,
    )
    value = _compute(_view(dividends=history))
    assert value.value == pytest.approx((2.52 / 1.8 + 1.8) / 10.0)


def test_stale_collection_is_approx():
    history = DividendHistory(covered_years={2025: date(2026, 8, 1), 2026: date(2026, 8, 1)})
    value = _compute(_view(dividends=history))
    assert value.quality == "approx" and FLAG_DIVIDENDS_STALE in value.flags


def test_observation_policy_is_unsupported():
    value = _compute(_view(dividends=DividendHistory(covered_years=COVERED), policy=OBSERVATION))
    assert value.quality == "missing" and "unsupported_provider" in value.flags


# --------------------------------------------------------------------------- #
# Database view (point in time)
# --------------------------------------------------------------------------- #


async def test_load_snapshot_view_reads_dividends_point_in_time(db_session):
    del db_session
    async with session_scope() as session:
        listing = await ListingRepository(session).ensure_a_share_listing("600900.SH")
        market = MarketSnapshotRepository(session)
        for day in (date(2026, 9, 18), AS_OF):
            await market.insert(
                listing_id=listing.id, trade_date=day, open=Decimal(25), high=Decimal(25),
                low=Decimal(25), close=Decimal(25), volume=Decimal(1), provider="baostock",
                captured_at=datetime.combine(day, datetime.min.time(), UTC) + timedelta(hours=9),
            )
        repository = DividendRepository(session)
        for ex_date, cash, captured, backfill in (
            (date(2026, 2, 12), "0.21", datetime(2026, 10, 7, tzinfo=UTC), True),
            (date(2026, 7, 17), "0.79", datetime(2026, 9, 1, tzinfo=UTC), False),
            # live capture after as_of: unknown on as_of
            (date(2026, 9, 29), "1.00", datetime(2026, 10, 3, tzinfo=UTC), False),
        ):
            await repository.upsert_event(
                listing_id=listing.id, ex_date=ex_date, available_from=ex_date - timedelta(days=60),
                operate_year=ex_date.year, provider="baostock", captured_at=captured,
                payload={"cash_per_share_pre_tax": Decimal(cash)}, details={},
                is_backfill=backfill,
            )
        await repository.record_coverage(
            listing_id=listing.id, provider="baostock", year=2025,
            checked_at=datetime(2026, 10, 7, tzinfo=UTC), event_count=0, is_backfill=True,
        )
        await repository.record_coverage(
            listing_id=listing.id, provider="baostock", year=2026,
            checked_at=datetime(2026, 9, 26, tzinfo=UTC), event_count=2,
        )
        view = await load_snapshot_view(session, "600900.SH", AS_OF)
        before_coverage = await load_snapshot_view(session, "600900.SH", date(2026, 9, 20))

    assert view.dividends is not None
    assert [event.ex_date for event in view.dividends.events] == [
        date(2026, 2, 12), date(2026, 7, 17)
    ]
    value = _compute(view)
    assert value.quality == "ok" and value.value == pytest.approx(1.0 / 25)
    # The 2026 coverage was first checked after 2026-09-20: not collected yet then.
    early = _compute(before_coverage)
    assert early.quality == "missing" and FLAG_DIVIDENDS_NOT_COLLECTED in early.flags
