"""AG03 fixtures verify audit boundaries, never count as real-data coverage."""
import sqlite3
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.exc import OperationalError

from investment_agent.factors.valuation import VALUATION_FACTORS, years_before
from investment_agent.factors.view import Bar, SnapshotView
from investment_agent.investor.coverage import (
    amount_audit,
    audit_database,
    numeric,
    percentile_audit,
)
from investment_agent.storage.models.pit_snapshot import FinancialSnapshot

AS_OF = date(2026, 10, 10)


def period(year: int, *, provider: str = "akshare", version: int = 1,
           cfo: object = 0, capex: object = -1) -> FinancialSnapshot:
    return FinancialSnapshot(id=year+version, listing_id=1, report_period=date(year, 12, 31),
                             period_kind="annual", provider=provider, version=version,
                             availability="actual", available_from=date(year+1, 4, 1),
                             captured_at=datetime(2026, 10, 7, tzinfo=UTC), is_backfill=False,
                             fields={"part": "cash_flow", "operating_cash_flow": cfo,
                                     "capital_expenditure": capex}, raw_ref="fixture-only")


@pytest.mark.parametrize("value,expected", [(0, True), (-1, True), (None, False),
                                           (True, False), (float("nan"), False),
                                           (float("inf"), False), ("100", False)])
def test_amount_presence_is_not_positive_or_zero_fill(value, expected):
    assert numeric(value) is expected


def test_missing_and_latest_version_are_not_added_or_zero_filled():
    result = amount_audit([period(2023), period(2024), period(2025),
                          period(2024, version=2, capex=None)], (2023, 2024, 2025))
    assert result["available"]
    assert not result["both_amounts_three_years"]
    assert result["annual_coverage"]["capital_expenditure"] == [2023, 2025]
    assert result["primary_reason"] == "capital_expenditure:missing_annual_2024"
    assert len(result["evidence"]) == 3
    assert result["fcfe"]["verified_available"] is None


def test_ratio_snapshots_are_not_amount_coverage():
    row = period(2025, provider="baostock")
    row.fields = {"cfo_to_np": 2, "net_profit": 100, "free_cash_flow": 180}
    result = amount_audit([row], (2023, 2024, 2025))
    assert result["available"] is False
    assert result["evidence"] == []
    assert result["primary_reason"] == "no_raw_cashflow_amounts"


@pytest.mark.parametrize("count,offset,quality", [(239, 31, "missing"), (240, 31, "ok"),
                                                (240, 32, "approx")])
def test_reused_factor_threshold_and_five_year_start(count, offset, quality):
    start = years_before(AS_OF, 5)
    bars = [Bar(trade_date=start+timedelta(days=offset+i), close=10, pe_ttm=5)
            for i in range(count-1)]
    bars += [Bar(trade_date=AS_OF, close=10, pe_ttm=5),
             Bar(trade_date=start, close=10, pe_ttm=5),
             Bar(trade_date=AS_OF+timedelta(days=1), close=10, pe_ttm=5)]
    result = percentile_audit(asdict(VALUATION_FACTORS[0].compute(
        SnapshotView.build(ticker="000001.SZ", as_of=AS_OF, raw_bars=bars))))
    assert result["quality"] == quality
    assert result["inputs_ref"]["valid_sessions"] == count
    assert result["available"] is (count >= 240)


def test_negative_excluded_and_stale_both_recorded():
    bars = [Bar(trade_date=AS_OF-timedelta(days=500-i), close=10, pe_ttm=5)
            for i in range(300)]
    bars[-1] = Bar(trade_date=bars[-1].trade_date, close=10, pe_ttm=-1)
    result = percentile_audit(asdict(VALUATION_FACTORS[0].compute(
        SnapshotView.build(ticker="000001.SZ", as_of=AS_OF, raw_bars=bars))))
    assert result["available"] is False
    assert result["primary_reason"] == "negative_earnings"
    assert "stale_price" in result["reasons"]
    assert result["inputs_ref"]["excluded_non_positive"] == 1


async def test_missing_database_is_not_created(tmp_path: Path):
    path = tmp_path / "absent.db"
    with pytest.raises(FileNotFoundError):
        await audit_database(path, universe="CSI300", as_of=AS_OF)
    assert not path.exists()


async def test_empty_database_has_explicit_error_and_no_migration(tmp_path: Path):
    path = tmp_path / "empty.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE sentinel(value TEXT)")
    before = path.read_bytes()
    with pytest.raises(OperationalError, match="no such table: universe_membership"):
        await audit_database(path, universe="CSI300", as_of=AS_OF)
    assert path.read_bytes() == before


async def test_session_pit_visibility_and_explicit_pool(db_session):
    from investment_agent.investor.coverage import audit_session
    from investment_agent.storage.models.reference import Universe, UniverseMembership
    from investment_agent.storage.repositories.listing import ListingRepository

    listing = await ListingRepository(db_session).ensure_a_share_listing("000001.SZ")
    db_session.add(Universe(code="AUDIT", name="fixture"))
    await db_session.flush()
    db_session.add(UniverseMembership(universe_code="AUDIT", listing_id=listing.id,
                                     valid_from=date(2020, 1, 1), source="fixture"))
    visible = period(2023)
    future_public = period(2024)
    future_public.available_from = date(2027, 1, 1)
    future_capture = period(2025)
    future_capture.captured_at = datetime(2027, 1, 1, tzinfo=UTC)
    backfilled = period(2025, provider="ifind", version=2)
    backfilled.captured_at = datetime(2027, 1, 1, tzinfo=UTC)
    backfilled.is_backfill = True
    unknown = period(2022)
    unknown.availability = "unknown"
    for row in (visible, future_public, future_capture, backfilled, unknown):
        row.listing_id = listing.id
        db_session.add(row)
    await db_session.flush()
    result = await audit_session(db_session, universe="AUDIT", as_of=AS_OF)
    assert result["summary"]["cashflow"]["denominator"] == 1
    assert {row["id"] for row in result["rows"][0]["cashflow"]["evidence"]} == {
        visible.id, backfilled.id,
    }
    assert result["rows"][0]["market_cap"]["available"] is False
    assert result["rows"][0]["market_cap"]["primary_reason"] != "available"
    with pytest.raises(ValueError, match="empty or missing universe"):
        await audit_session(db_session, universe="MISSING", as_of=AS_OF)
