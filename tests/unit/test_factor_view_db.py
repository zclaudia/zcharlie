"""Database-backed snapshot view: point-in-time reads through the repositories."""

from __future__ import annotations

from datetime import date

import pytest

from investment_agent.factors.catalog import DEFAULT_REGISTRY
from investment_agent.factors.view import OBSERVATION, TARGET_POOL, load_snapshot_view
from tests.support.factor_view import AS_OF, LATER, ON_TIME, TICKER, _seed  # noqa: F401


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
    # only a CSRC row is stored: the group comes from the fallback table
    assert values["roe"].flags == ("industry_fallback", "report_overdue")
    assert values["roe"].inputs_ref["industry"]["matched_by"] == "code"
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
