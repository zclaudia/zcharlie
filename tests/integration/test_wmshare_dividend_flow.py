"""Recorded public responses through provider, storage and the existing factor."""

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from investment_agent.dividends.service import dividend_context, persist_wmshare_capture
from investment_agent.providers.wmshare_dividends import WmshareDividendClient
from investment_agent.storage.models.pit_snapshot import DividendEventSnapshot
from investment_agent.storage.repositories.dividend import DividendRepository
from investment_agent.storage.repositories.listing import ListingRepository
from investment_agent.storage.repositories.market_snapshot import MarketSnapshotRepository

FIXTURE = Path(__file__).parents[1] / "fixtures/wmshare/dividends-real.json"
DAY = date(2026, 11, 13)
CAPTURED = datetime(2026, 11, 13, 3, tzinfo=UTC)


async def test_recorded_cash_and_platform_values_remain_distinct(db_session):
    recorded = json.loads(FIXTURE.read_text())["stocks"]["600036.SH"]
    listing = await ListingRepository(db_session).ensure_a_share_listing(
        "600036.SH", company_name="招商银行"
    )
    price = Decimal(str(recorded["last"]["result"]["close"]))
    await MarketSnapshotRepository(db_session).insert(
        listing_id=listing.id,
        trade_date=date(2026, 11, 12),
        open=price,
        high=price,
        low=price,
        close=price,
        volume=Decimal(1),
        provider="baostock",
        captured_at=CAPTURED,
    )
    repository = DividendRepository(db_session)
    await repository.upsert_event(
        listing_id=listing.id,
        ex_date=date(2026, 7, 10),
        available_from=date(2026, 7, 4),
        operate_year=2026,
        provider="baostock",
        captured_at=CAPTURED,
        payload={
            "cash_per_share_pre_tax": Decimal("1"),
            "stock_per_share": Decimal(0),
            "reserve_per_share": Decimal(0),
        },
        details={"dividCashStock": "10派10元"},
    )
    for year in (2025, 2026):
        await repository.record_coverage(
            listing_id=listing.id,
            provider="baostock",
            year=year,
            checked_at=CAPTURED,
            event_count=1,
        )
    sdk = SimpleNamespace(
        last_ec_cost=None,
        dividend=SimpleNamespace(
            history=lambda *a, **k: recorded["history_recent"]["result"],
            snapshot=lambda **k: recorded["snapshot"]["result"],
        ),
    )
    capture = await WmshareDividendClient(sdk).fetch("600036.SH", DAY)
    capture.captured_at = CAPTURED
    assert capture.history_error is None
    await persist_wmshare_capture(db_session, listing.id, capture, raw_ref="fixture")
    first = await dividend_context(db_session, "600036.SH", DAY)
    assert first["cash"]["provider"] == "wmshare"
    assert first["cash"]["cash_per_share"] == pytest.approx(2.016)
    assert first["cash"]["value"] == pytest.approx(0.04729064)
    assert first["platform"]["yield_ttm"] == pytest.approx(0.0235280319)
    # The snapshot date is yesterday; do not claim a same-date comparison today.
    assert first["difference"] is None
    count = await db_session.scalar(select(func.count()).select_from(DividendEventSnapshot))
    await persist_wmshare_capture(db_session, listing.id, capture, raw_ref="same-fixture")
    assert await db_session.scalar(select(func.count()).select_from(DividendEventSnapshot)) == count
    assert (await dividend_context(db_session, "600036.SH", DAY))["cash"] == first["cash"]
    # Captures made on the 13th cannot become knowledge on the 12th.
    earlier = await dividend_context(db_session, "600036.SH", date(2026, 11, 12))
    assert earlier["platform"] is None
    assert earlier["cash"]["value"] is None


async def test_recorded_undated_distribution_stays_pending(db_session):
    recorded = json.loads(FIXTURE.read_text())["stocks"]["601988.SH"]
    listing = await ListingRepository(db_session).ensure_a_share_listing(
        "601988.SH", company_name="中国银行"
    )
    sdk = SimpleNamespace(
        last_ec_cost=None,
        dividend=SimpleNamespace(
            history=lambda *a, **k: recorded["history_recent"]["result"],
            snapshot=lambda **k: recorded["snapshot"]["result"],
        ),
    )
    capture = await WmshareDividendClient(sdk).fetch("601988.SH", DAY)
    capture.captured_at = CAPTURED
    await persist_wmshare_capture(db_session, listing.id, capture, raw_ref="fixture")
    result = await dividend_context(db_session, "601988.SH", DAY)
    pending = [r for r in result["pending"] if r.get("dps") == 0.119]
    assert len(pending) == 1 and pending[0]["ex_div"] is None and pending[0]["pay"] is None
    wm_events = await DividendRepository(db_session).events_as_of(
        listing.id, date(2025, 11, 13), DAY, provider="wmshare"
    )
    assert sum(float(row.cash_per_share_pre_tax) for row in wm_events) == pytest.approx(0.2263)
