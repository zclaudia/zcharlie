"""End-to-end forward validation on synthetic PIT data (offline, no network).

Each test writes the minimum snapshot rows the tool reads, then runs the real
``load_dataset`` path: PIT membership, ``load_snapshot_view``, canonical
bundle, ``score_pool``, screening, tradable T+1 entry and the report.

The point is to pin semantics that a real-data run cannot isolate:

* a known monotone signal is recovered as positive IC and rising buckets;
* a no-signal cross-section reports ~0 IC and is not reported as monotone;
* data published after ``as_of`` cannot enter the score;
* a suspended entry is deferred or excluded, never filled;
* costs reduce every net return, and the report says so.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select

from investment_agent.clock import SHANGHAI_TZ
from investment_agent.domain.a_share import A_SHARE_MARKETS
from investment_agent.research.forward_validation.contracts import (
    CostModel,
    ValidationConfig,
)
from investment_agent.research.forward_validation.dataset import load_dataset
from investment_agent.research.forward_validation.evaluation import Benchmark
from investment_agent.storage.models.pit_snapshot import (
    FinancialSnapshot,
    MarketSnapshot,
)
from investment_agent.storage.models.reference import UniverseMembership
from investment_agent.storage.repositories.listing import ListingRepository

AS_OF = date(2024, 6, 28)
TICKERS = [f"6000{index:02d}.SH" for index in range(1, 31)]
# Captured on the signal day (18:00 Asia/Shanghai), so live rows are visible at AS_OF.
CAPTURED = datetime(2024, 6, 28, 10, 0, tzinfo=UTC)


async def _listing(session, ticker: str):
    return await ListingRepository(session).ensure_a_share_listing(
        ticker, company_name=f"公司{ticker[:6]}"
    )


def _trading_days(start: date, count: int) -> list[date]:
    days: list[date] = []
    current = start
    while len(days) < count:
        if current.weekday() < 5:
            days.append(current)
        current += timedelta(days=1)
    return days


async def _seed_member(
    session,
    ticker: str,
    *,
    days: list[date],
    signal_price: float,
    forward_daily: float,
    roe: float,
    revenue_growth: float,
    suspended_days: set[date] | None = None,
) -> None:
    """One constituent with a flat pre-signal price and a chosen forward drift."""

    listing = await _listing(session, ticker)
    session.add(
        UniverseMembership(
            universe_code="CSI300",
            listing_id=listing.id,
            valid_from=date(2024, 1, 2),
            valid_to=None,
            source="test",
        )
    )
    signal_index = days.index(AS_OF)
    suspended_days = suspended_days or set()
    for index, day in enumerate(days):
        if index <= signal_index:
            price = signal_price
        else:
            price = signal_price * (1.0 + forward_daily) ** (index - signal_index)
        suspended = day in suspended_days
        raw_close = price if not suspended else signal_price
        session.add(
            MarketSnapshot(
                listing_id=listing.id,
                trade_date=day,
                open=Decimal(str(round(raw_close, 4))),
                high=Decimal(str(round(raw_close * 1.01, 4))),
                low=Decimal(str(round(raw_close * 0.99, 4))),
                close=Decimal(str(round(raw_close, 4))),
                volume=Decimal("0" if suspended else "1000000"),
                adjust="none",
                provider="baostock",
                captured_at=CAPTURED,
                is_backfill=False,
                version=1,
                pe_ttm=Decimal("12"),
                pb_mrq=Decimal("1.5"),
                ps_ttm=Decimal("2"),
                turnover=Decimal("0.01"),
                trade_status=0 if suspended else 1,
                is_st=False,
            )
        )
        session.add(
            MarketSnapshot(
                listing_id=listing.id,
                trade_date=day,
                open=Decimal(str(round(raw_close, 4))),
                high=Decimal(str(round(raw_close * 1.01, 4))),
                low=Decimal(str(round(raw_close * 0.99, 4))),
                close=Decimal(str(round(raw_close, 4))),
                volume=Decimal("0" if suspended else "1000000"),
                adjust="backward",
                provider="baostock",
                captured_at=CAPTURED,
                is_backfill=False,
                version=1,
            )
        )
    # Five annual reports (the factor layer needs up to four for stability).
    for year in range(2019, 2024):
        session.add(
            FinancialSnapshot(
                listing_id=listing.id,
                report_period=date(year, 12, 31),
                period_kind="annual",
                available_from=date(year + 1, 4, 30),
                availability="actual",
                fields={
                    "roe": roe,
                    "net_profit": 1_000_000_000.0 * (1 + revenue_growth) ** (year - 2019),
                    "revenue": 5_000_000_000.0 * (1 + revenue_growth) ** (year - 2019),
                    "net_margin": 0.2,
                    "gross_margin": 0.4,
                    "debt_ratio": 0.3,
                    "operating_cash_flow": 800_000_000.0,
                    "capital_expenditure": 200_000_000.0,
                    "total_shares": 1_000_000_000.0,
                },
                provider="baostock",
                captured_at=CAPTURED,
                is_backfill=False,
                version=1,
            )
        )
    await session.flush()


def config(**overrides: Any) -> ValidationConfig:
    values: dict[str, Any] = {
        "as_of_dates": (AS_OF,),
        "horizons": (1, 3),
        "min_scored": 20,
        "min_group_size": 3,
        "min_coverage": 0.5,
        "min_template_hits": 3,
        "template_source": "files",
    }
    values.update(overrides)
    return ValidationConfig(**values)


def _observation(report: dict[str, Any]) -> dict[str, Any]:
    section = report["cross_sections"][0]
    return section["horizons"]["1m"]


async def test_known_monotone_signal_is_recovered(db_session):
    days = _trading_days(date(2023, 1, 2), 680)
    # Score inputs rise with i; forward drift rises with the same i.
    for index, ticker in enumerate(TICKERS):
        await _seed_member(
            db_session,
            ticker,
            days=days,
            signal_price=10.0 + index,
            forward_daily=0.0004 * index,
            roe=0.05 + 0.01 * index,
            revenue_growth=0.02 + 0.002 * index,
        )
    data = await load_dataset(db_session, config())
    report = data.report
    horizon = _observation(report)
    assert report["status"] in {"partial", "ok"}, (
        report["insufficient_reasons"], report["failures"][:2]
    )
    assert horizon["evaluated"] >= 20
    assert horizon["ic_spearman"] is not None and horizon["ic_spearman"] > 0.5
    assert horizon["monotonicity"]["status"] == "ok"
    assert horizon["monotonicity"]["strict"] is True
    assert horizon["top_minus_bottom"] is not None and horizon["top_minus_bottom"] > 0


async def test_no_signal_cross_section_is_not_reported_as_monotone(db_session):
    days = _trading_days(date(2023, 1, 2), 680)
    # Alternating forward drift independent of the score inputs.
    for index, ticker in enumerate(TICKERS):
        await _seed_member(
            db_session,
            ticker,
            days=days,
            signal_price=10.0,
            forward_daily=0.0 if index % 2 else 0.001,
            roe=0.10,
            revenue_growth=0.05,
        )
    data = await load_dataset(db_session, config())
    horizon = _observation(data.report)
    assert horizon["monotonicity"]["strict"] is False
    assert abs(horizon["ic_spearman"] or 0.0) < 0.5


async def test_future_data_is_invisible_to_the_score(db_session):
    days = _trading_days(date(2023, 1, 2), 680)
    for ticker in TICKERS:
        await _seed_member(
            db_session,
            ticker,
            days=days,
            signal_price=10.0,
            forward_daily=0.0002,
            roe=0.10,
            revenue_growth=0.05,
        )
    await db_session.flush()
    listing = await _listing(db_session, TICKERS[0])
    # A later annual report captured later must not appear at AS_OF.
    db_session.add(
        FinancialSnapshot(
            listing_id=listing.id,
            report_period=date(2024, 12, 31),
            period_kind="annual",
            available_from=date(2025, 4, 30),
            availability="actual",
            fields={"roe": 0.99, "net_profit": 9e9, "revenue": 9e9},
            provider="baostock",
            captured_at=datetime(2025, 5, 1, tzinfo=UTC),
            is_backfill=False,
            version=1,
        )
    )
    await db_session.flush()
    data = await load_dataset(db_session, config())
    first = next(item for item in data.observations if item.ticker == TICKERS[0])
    assert first.max_input_date is not None and first.max_input_date <= AS_OF
    assert data.report["lookahead_check"]["passed"] is True
    assert data.report["lookahead_check"]["violations"] == []


async def test_lookahead_guard_rejects_a_future_input():
    from investment_agent.research.forward_validation.contracts import ScoredObservation

    item = ScoredObservation(
        ticker="600001.SH",
        listing_id=1,
        as_of=AS_OF,
        signal_session=AS_OF,
        industry_group="other",
        investment_score=60.0,
        factor_coverage=0.9,
        needs_agent_review=False,
        score_suppression_reason=None,
        selected_model="general",
        quality_signals=(),
        score_status="scored",
        score_failure=None,
        outcomes={},
        max_input_date=AS_OF + timedelta(days=1),
    )
    from investment_agent.research.forward_validation.evaluation import build_report

    benchmark = Benchmark(
        name="equal_weight",
        by_horizon={1: {AS_OF: 0.0}, 3: {AS_OF: 0.0}},
        construction="test",
        note="",
    )
    report = build_report(
        config=config(),
        observations=[item],
        benchmark=benchmark,
        sessions=[AS_OF],
        market_data={},
        scoring={},
        templates={"template_refs": []},
        diagnostics={},
        failures=[],
        code_version={},
    )
    assert report["lookahead_check"]["passed"] is False
    assert report["lookahead_check"]["violations"] == [f"{AS_OF.isoformat()}:600001.SH"]


async def test_suspended_entry_is_deferred_or_excluded_never_filled(db_session):
    days = _trading_days(date(2023, 1, 2), 680)
    signal_index = days.index(AS_OF)
    blocked = set(days[signal_index + 1 : signal_index + 4])
    for ticker in TICKERS:
        suspend = blocked if ticker == TICKERS[0] else set()
        await _seed_member(
            db_session,
            ticker,
            days=days,
            signal_price=10.0,
            forward_daily=0.0002,
            roe=0.10,
            revenue_growth=0.05,
            suspended_days=suspend,
        )
    data = await load_dataset(db_session, config(max_entry_deferral=1))
    first = next(item for item in data.observations if item.ticker == TICKERS[0])
    outcome = first.outcome(1)
    assert outcome is not None
    assert outcome.status == "entry_untradable"
    assert outcome.entry_date is None and outcome.net_return is None

    # With enough deferral the same stock enters on the first reopened session.
    data = await load_dataset(db_session, config(max_entry_deferral=5))
    first = next(item for item in data.observations if item.ticker == TICKERS[0])
    outcome = first.outcome(1)
    assert outcome is not None and outcome.status == "ok"
    assert outcome.entry_date == days[signal_index + 4]


async def test_costs_reduce_returns_and_are_reported(db_session):
    days = _trading_days(date(2023, 1, 2), 680)
    for ticker in TICKERS:
        await _seed_member(
            db_session,
            ticker,
            days=days,
            signal_price=10.0,
            forward_daily=0.001,
            roe=0.10,
            revenue_growth=0.05,
        )
    free = await load_dataset(db_session, config(cost=CostModel(0.0, 0.0, 0.0)))
    costly = await load_dataset(
        db_session, config(cost=CostModel(commission_bps=3.0, stamp_duty_bps=5.0, slippage_bps=10.0))
    )
    free_horizon = _observation(free.report)
    costly_horizon = _observation(costly.report)
    free_returns = [bucket["mean_net_return"] for bucket in free_horizon["buckets"]]
    costly_returns = [bucket["mean_net_return"] for bucket in costly_horizon["buckets"]]
    assert free_returns and costly_returns
    assert sum(costly_returns) < sum(free_returns)
    assert costly_horizon["mean_net_excess"] == pytest.approx(
        free_horizon["mean_net_excess"], abs=1e-9
    )  # the same cost model applies to the benchmark, so excess is cost-neutral
    assert costly.report["cost_model"]["round_trip_bps"] == pytest.approx(31.0)
    assert costly.report["guard"]["calibration_status_changed"] is False
    # Template calibration status is copied, never advanced.
    assert all(
        value == "uncalibrated"
        for value in costly.report["templates"]["calibration_status"].values()
    )


async def test_insufficient_sample_is_explicit_and_never_ok(db_session):
    days = _trading_days(date(2023, 1, 2), 680)
    for index, ticker in enumerate(TICKERS[:5]):
        await _seed_member(
            db_session,
            ticker,
            days=days,
            signal_price=10.0,
            forward_daily=0.0004 * index,
            roe=0.10,
            revenue_growth=0.05,
        )
    data = await load_dataset(db_session, config(min_scored=20))
    report = data.report
    assert report["status"] == "insufficient"
    assert report["insufficient_reasons"]
    assert report["guard"]["calibration_status_changed"] is False


async def test_pit_membership_includes_later_removed_names(db_session):
    days = _trading_days(date(2023, 1, 2), 680)
    for ticker in TICKERS:
        await _seed_member(
            db_session,
            ticker,
            days=days,
            signal_price=10.0,
            forward_daily=0.0002,
            roe=0.10,
            revenue_growth=0.05,
        )
    listing = (await db_session.execute(select(UniverseMembership).limit(1))).scalar_one()
    listing.valid_to = date(2024, 3, 1)  # removed before AS_OF
    await db_session.flush()
    removed_ticker = TICKERS[0]
    data = await load_dataset(db_session, config())
    assert removed_ticker not in {item.ticker for item in data.observations}
    # And a name removed *after* AS_OF is still part of the historical pool.
    listing.valid_to = date(2024, 12, 1)
    await db_session.flush()
    data = await load_dataset(db_session, config())
    assert removed_ticker in {item.ticker for item in data.observations}


async def test_scoring_failure_is_counted_not_hidden(db_session):
    days = _trading_days(date(2023, 1, 2), 680)
    for ticker in TICKERS:
        await _seed_member(
            db_session,
            ticker,
            days=days,
            signal_price=10.0,
            forward_daily=0.0002,
            roe=0.10,
            revenue_growth=0.05,
        )
    # A member with no bars at all cannot be scored.
    await _listing(db_session, "600099.SH")
    session = db_session
    listing = await _listing(session, "600099.SH")
    session.add(
        UniverseMembership(
            universe_code="CSI300",
            listing_id=listing.id,
            valid_from=date(2024, 1, 2),
            valid_to=None,
            source="test",
        )
    )
    await session.flush()
    data = await load_dataset(session, config())
    failed = [item for item in data.observations if item.score_status == "failed"]
    assert [item.ticker for item in failed] == ["600099.SH"]
    assert data.report["sample"]["total_failed"] == 1
    assert data.report["status"] == "partial"


def test_config_hash_is_stable_and_sensitive():
    first = config()
    second = config()
    assert first.config_hash == second.config_hash
    assert config(horizons=(3,)).config_hash != first.config_hash
    with pytest.raises(ValueError):
        config(as_of_dates=())
    with pytest.raises(ValueError):
        config(horizons=(2,))


def test_trading_day_helper_stays_weekday_only():
    days = _trading_days(date(2023, 1, 2), 10)
    assert all(day.weekday() < 5 for day in days)


# Keep imports used by the annotations above intentional in this test module.
_ = (A_SHARE_MARKETS, SHANGHAI_TZ, time)
