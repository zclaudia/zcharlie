"""Database-backed snapshot view: point-in-time reads through the repositories."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from investment_agent.factors.library import DEFAULT_REGISTRY
from investment_agent.factors.view import OBSERVATION, TARGET_POOL, load_snapshot_view
from investment_agent.providers.baostock_reference import IndustryEntry, IndustrySnapshot
from investment_agent.storage.repositories.financial_snapshot import FinancialSnapshotRepository
from investment_agent.storage.repositories.industry import IndustryRepository
from investment_agent.storage.repositories.listing import ListingRepository
from investment_agent.storage.repositories.market_snapshot import MarketSnapshotRepository
from investment_agent.storage.repositories.report_overdue import ReportOverdueRepository

TICKER = "600519.SH"
AS_OF = date(2026, 5, 6)
ON_TIME = datetime(2026, 5, 6, 9, 0, tzinfo=UTC)  # 17:00 Shanghai on AS_OF
LATER = datetime(2026, 5, 8, 9, 0, tzinfo=UTC)


async def _seed(session) -> int:
    listing = await ListingRepository(session).ensure_a_share_listing(
        TICKER, company_name="贵州茅台"
    )
    entry = IndustryEntry(
        ticker=TICKER,
        company_name="贵州茅台",
        industry_code="C15",
        industry_name="酒、饮料和精制茶制造业",
        effective_date=date(2026, 1, 5),
        raw_industry="C15酒、饮料和精制茶制造业",
        classification_label="证监会行业分类",
    )
    await IndustryRepository(session).apply_industry_snapshot(
        IndustrySnapshot(
            as_of=date(2026, 1, 5),
            effective_date=date(2026, 1, 5),
            entries=(entry,),
            captured_at=datetime(2026, 1, 5, 2, tzinfo=UTC),
            raw_rows=(),
        )
    )
    market = MarketSnapshotRepository(session)
    for offset in range(5):
        day = AS_OF - timedelta(days=4 - offset)
        for adjust, close in (("none", 1500 + offset), ("backward", 9000 + offset)):
            await market.insert(
                listing_id=listing.id,
                trade_date=day,
                open=Decimal(close),
                high=Decimal(close),
                low=Decimal(close),
                close=Decimal(close),
                volume=Decimal(1),
                provider="baostock",
                captured_at=datetime.combine(day, datetime.min.time(), UTC) + timedelta(hours=10),
                adjust=adjust,
                pe_ttm=Decimal("20.5"),
                pb_mrq=Decimal("7"),
                ps_ttm=Decimal("9"),
                turnover=Decimal("0.003"),
                trade_status=1,
                is_st=False,
            )
    # A bar after as_of, a correction captured after as_of, and an iFinD row.
    await market.insert(
        listing_id=listing.id,
        trade_date=AS_OF + timedelta(days=1),
        open=Decimal(1),
        high=Decimal(1),
        low=Decimal(1),
        close=Decimal(1),
        volume=Decimal(1),
        provider="baostock",
        captured_at=LATER,
    )
    await market.insert(
        listing_id=listing.id,
        trade_date=AS_OF,
        open=Decimal(1),
        high=Decimal(1),
        low=Decimal(1),
        close=Decimal(1),
        volume=Decimal(1),
        provider="baostock",
        captured_at=LATER,
        pe_ttm=Decimal("-3"),
    )
    await market.insert(
        listing_id=listing.id,
        trade_date=AS_OF,
        open=Decimal(7),
        high=Decimal(7),
        low=Decimal(7),
        close=Decimal(1400),
        volume=Decimal(1),
        provider="ifind",
        captured_at=ON_TIME,
        total_shares=Decimal(1_000_000_000),
    )

    financial = FinancialSnapshotRepository(session)
    base = {
        "roe": 0.3,
        "net_profit": 100.0,
        "revenue": 200.0,
        "total_shares": 1_250_000_000.0,
        "debt_ratio": 0.2,
        "missing_fields": [],
        "source_values": {},
    }
    for year, pub in (
        (2022, date(2023, 3, 31)),
        (2023, date(2024, 4, 3)),
        (2024, date(2025, 4, 3)),
        (2025, date(2026, 4, 17)),
    ):
        await financial.insert(
            listing_id=listing.id,
            report_period=date(year, 12, 31),
            period_kind="annual",
            fields={**base, "roe": 0.3 + (year - 2022) / 100, "net_profit": 100.0 + year - 2022},
            provider="baostock",
            captured_at=datetime(2026, 4, 30, tzinfo=UTC),
            availability="actual",
            available_from=pub,
            is_backfill=year < 2025,
        )
    # Q1 published after as_of: invisible.
    await financial.insert(
        listing_id=listing.id,
        report_period=date(2026, 3, 31),
        period_kind="q1",
        fields={**base, "eps_ttm": 70.0},
        provider="baostock",
        captured_at=datetime(2026, 5, 8, tzinfo=UTC),
        availability="actual",
        available_from=date(2026, 5, 7),
    )
    # A re-capture of FY2025 after as_of: the earlier version stays in force.
    await financial.insert(
        listing_id=listing.id,
        report_period=date(2025, 12, 31),
        period_kind="annual",
        fields={**base, "roe": 0.99},
        provider="baostock",
        captured_at=LATER,
        availability="actual",
        available_from=date(2026, 4, 17),
    )
    # iFinD FY2025: net_profit means attributable to parent there.
    await financial.insert(
        listing_id=listing.id,
        report_period=date(2025, 12, 31),
        period_kind="annual",
        fields={
            "roe": 0.35,
            "net_profit": 90.0,
            "revenue": 210.0,
            "source_values": {"ths_eps_basic_stock": 65.0},
        },
        provider="ifind",
        captured_at=ON_TIME,
        availability="approx",
        available_from=date(2026, 5, 6),
    )
    await ReportOverdueRepository(session).record(
        listing_id=listing.id,
        report_period=date(2026, 3, 31),
        deadline=date(2026, 4, 30),
        detected_on=date(2026, 5, 1),
        provider="baostock",
    )
    await session.flush()
    return listing.id


async def test_target_pool_view_reads_baostock_point_in_time(db_session):
    await _seed(db_session)
    view = await load_snapshot_view(db_session, TICKER, AS_OF)

    assert view.policy == TARGET_POOL
    assert view.industry_group == "consumer"
    assert [bar.trade_date for bar in view.raw_bars][-1] == AS_OF
    assert view.latest_raw_bar().close == 1504.0  # not the later correction / iFinD row
    assert view.latest_raw_bar().pe_ttm == 20.5
    assert view.adjusted_bars[-1].close == 9004.0
    assert [p.report_period.year for p in view.periods] == [2022, 2023, 2024, 2025]
    fy2025 = view.latest_annual()
    assert fy2025.fields["roe"] == pytest.approx(0.33)
    assert fy2025.fields["net_profit_total"] == 103.0
    assert "net_profit" not in fy2025.fields and "revenue" not in fy2025.fields
    assert fy2025.ref.startswith("financial_snapshot:")
    assert view.overdue_periods == (date(2026, 3, 31),)

    values = DEFAULT_REGISTRY.compute_all(view)
    assert values["roe"].value == pytest.approx(0.33)
    assert values["roe"].flags == ("report_overdue",)
    assert values["roe_stability"].quality == "ok"
    assert values["market_cap"].value == 1504.0 * 1_250_000_000.0
    assert values["report_overdue"].value == 1.0


async def test_view_before_pubdate_and_capture_sees_less(db_session):
    await _seed(db_session)
    view = await load_snapshot_view(db_session, TICKER, date(2026, 4, 16))
    assert view.latest_annual().report_period == date(2024, 12, 31)
    assert view.raw_bars == ()  # first bar is captured on 2026-05-02
    assert view.overdue_periods == ()


async def test_observation_view_reads_ifind_with_parent_semantics(db_session):
    await _seed(db_session)
    view = await load_snapshot_view(db_session, TICKER, AS_OF, policy=OBSERVATION)
    assert [bar.close for bar in view.raw_bars] == [1400.0]
    period = view.latest_annual()
    assert period.fields["net_profit_parent"] == 90.0
    assert period.fields["revenue_total"] == 210.0
    assert period.fields["eps_basic_fy"] == 65.0
    pe = DEFAULT_REGISTRY.get("pe_fy").compute(view)
    assert pe.value == round(1400.0 / 65.0, 4)
    market_cap = DEFAULT_REGISTRY.get("market_cap").compute(view)
    assert market_cap.value == 1400.0 * 1_000_000_000
    assert market_cap.inputs_ref["total_shares"]["source"] == "market_snapshot.total_shares"


async def test_unknown_listing_raises(db_session):
    with pytest.raises(LookupError):
        await load_snapshot_view(db_session, "000001.SZ", AS_OF)
