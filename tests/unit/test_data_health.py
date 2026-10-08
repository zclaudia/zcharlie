"""P1-E2 data health aggregate: accumulation, collections, freshness, backfill,
verification, overdue and warnings."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from investment_agent.snapshots.health import (
    HealthWarning,
    LiveSnapshot,
    build_data_health,
    compute_accumulation,
    live_market_snapshots,
)
from investment_agent.storage.models.verification import IfindCallLedger, VerificationRequest
from tests.unit._health_support import (
    CALENDAR,
    UNIVERSE,
    add_bars,
    add_dividend_coverage,
    add_financial,
    add_industry,
    add_market_run,
    add_overdue,
    add_run,
    completeness,
    health_settings,
    seed_members,
    ticker,
)

NOW = datetime(2026, 10, 15, 4, 0, tzinfo=UTC)  # Thu 12:00 Asia/Shanghai
TODAY = date(2026, 10, 15)


def _accumulate(days: dict[date, float], today: date, threshold: float = 0.95):
    return compute_accumulation(
        [LiveSnapshot(day, ratio) for day, ratio in days.items()],
        universe_code=UNIVERSE,
        today=today,
        threshold=threshold,
        calendar=CALENDAR,
    )


def _codes(health) -> set[str]:
    return {warning.code for warning in health.warnings}


# ---- accumulation ----------------------------------------------------------


def test_accumulation_counts_consecutive_iso_weeks() -> None:
    result = _accumulate(
        {date(2026, 9, 30): 1.0, date(2026, 10, 9): 1.0, date(2026, 10, 12): 0.99},
        today=date(2026, 10, 21),
    )
    assert result.streak_weeks == 3
    assert result.weeks_with_snapshots == 3
    assert result.weeks_meeting_threshold == 3
    assert result.first_live_trade_date == date(2026, 9, 30)
    assert result.latest_live_trade_date == date(2026, 10, 12)
    assert not result.reached
    assert [(week.week, week.status) for week in result.weeks] == [
        ("2026-W43", "in_progress"),
        ("2026-W42", "qualified"),
        ("2026-W41", "qualified"),
        ("2026-W40", "qualified"),
    ]
    # National Day week: Oct 8 and 9 trade, Oct 5-7 closed.
    assert result.weeks[2].trading_days == 2


def test_accumulation_gap_resets_the_streak() -> None:
    result = _accumulate(
        {date(2026, 9, 30): 1.0, date(2026, 10, 12): 1.0}, today=date(2026, 10, 21)
    )
    assert result.streak_weeks == 1
    assert result.weeks_with_snapshots == 2
    assert [week.status for week in result.weeks] == [
        "in_progress",
        "qualified",
        "missing",
        "qualified",
    ]


def test_accumulation_skips_weeks_without_trading_days() -> None:
    # 2026-02-16..20 is the Spring Festival closure: no trading day that week.
    result = _accumulate({date(2026, 2, 13): 1.0, date(2026, 2, 24): 1.0}, today=date(2026, 2, 25))
    statuses = {week.week: week.status for week in result.weeks}
    assert statuses == {"2026-W09": "qualified", "2026-W08": "no_trading", "2026-W07": "qualified"}
    assert result.streak_weeks == 2


def test_accumulation_threshold_uses_lowest_day_and_best_run_per_day() -> None:
    snapshots = [
        LiveSnapshot(date(2026, 9, 30), 1.0),
        LiveSnapshot(date(2026, 10, 8), 1.0),
        LiveSnapshot(date(2026, 10, 9), 0.9),
        LiveSnapshot(date(2026, 10, 12), 0.8),
        LiveSnapshot(date(2026, 10, 12), 0.97),  # a re-run fixed the day
    ]
    result = compute_accumulation(
        snapshots, universe_code=UNIVERSE, today=date(2026, 10, 14), threshold=0.95, calendar=CALENDAR
    )
    weeks = {week.week: week for week in result.weeks}
    assert weeks["2026-W41"].status == "below_threshold"
    assert weeks["2026-W41"].min_ratio == 0.9
    assert weeks["2026-W41"].collected_days == 2
    assert weeks["2026-W42"].status == "qualified"  # the current week counts once it qualifies
    assert result.streak_weeks == 1
    assert result.weeks_meeting_threshold == 2


def test_accumulation_reaches_the_target_after_eight_weeks() -> None:
    days = {date(2026, 8, 3) + timedelta(weeks=index): 1.0 for index in range(8)}
    result = _accumulate(days, today=date(2026, 9, 22))
    assert result.streak_weeks == 8 and result.reached


def test_accumulation_without_live_snapshots() -> None:
    result = _accumulate({}, today=TODAY)
    assert result.streak_weeks == 0 and result.weeks == [] and result.first_live_trade_date is None


async def test_live_snapshots_exclude_backfill_watchlist_failed_and_empty_runs(db_session) -> None:
    await seed_members(db_session, 1)
    await add_market_run(db_session, date(2026, 10, 9))
    await add_market_run(db_session, date(2026, 10, 12), status="partial", ratio_collected=(300, 280))
    await add_market_run(db_session, date(2026, 10, 13), status="failed", ratio_collected=(300, 0))
    await add_market_run(db_session, date(2026, 10, 14), status="partial", ratio_collected=(300, 0))
    # history backfill never counts as a live snapshot
    await add_run(
        db_session,
        "snapshot_backfill",
        {"universe_code": UNIVERSE, "as_of": "2026-10-07", "part": "prices", "symbols": [ticker(0)], "chunk": 0},
        metadata={"completeness": completeness(1, 1)},
    )
    await add_run(
        db_session,
        "market_snapshot",
        {"provider": "ifind", "scope": "watchlist", "trade_date": "2026-10-13"},
        metadata={"completeness": completeness(5, 5)},
        universe=None,
    )
    found = sorted((item.trade_date, item.ratio) for item in await live_market_snapshots(db_session, UNIVERSE))
    assert found == [(date(2026, 10, 9), 1.0), (date(2026, 10, 12), 0.9333)]


# ---- full aggregate --------------------------------------------------------


async def test_empty_database_is_quiet(db_session) -> None:
    health = await build_data_health(db_session, health_settings(), now=NOW, today=TODAY)
    assert {w.level for w in health.warnings} <= {"info"}
    assert _codes(health) == {"market_never_collected", "universe_never_synced", "directory_missing"}
    assert health.collections == []
    assert health.accumulation.streak_weeks == 0
    assert health.backfill.as_of is None and health.backfill.parts == []
    assert health.verification.budget.remaining == 150
    assert health.overdue.open == 0


async def test_collections_group_runs_with_trend_missing_and_quota_stop(db_session) -> None:
    await seed_members(db_session, 3)
    await add_market_run(db_session, date(2026, 10, 12))
    await add_market_run(db_session, date(2026, 10, 13), ratio_collected=(300, 299))
    missing = {
        "600010.SH": ("no_data", "suspended, no bar"),
        "600011.SH": ("quota", "not collected: iFinD quota exhausted"),
        "600012.SH": ("failed", "ProviderError: boom"),
    }
    await add_market_run(
        db_session,
        date(2026, 10, 14),
        status="partial",
        ratio_collected=(300, 270),
        missing=missing,
        stop_reason="quota",
        finished_at=NOW - timedelta(hours=2),
    )
    queued = await add_run(
        db_session,
        "market_snapshot",
        {"provider": "baostock", "scope": "universe", "universe_code": UNIVERSE, "trade_date": "2026-10-15"},
        status="queued",
    )
    await add_run(
        db_session,
        "financial_snapshot",
        {
            "provider": "akshare",
            "scope": "universe",
            "universe_code": UNIVERSE,
            "report_period": "2026-06-30",
            "as_of": "2026-10-10",
            "part": "summary",
        },
        metadata={"completeness": completeness(300, 300)},
    )
    await add_run(
        db_session,
        "financial_snapshot",
        {
            "provider": "akshare",
            "scope": "universe",
            "universe_code": UNIVERSE,
            "report_period": "2020-12-31",
            "as_of": "2026-10-06",
            "part": "summary",
            "backfill": True,
        },
        metadata={"completeness": completeness(319, 319)},
    )

    health = await build_data_health(db_session, health_settings(), now=NOW, today=TODAY)
    by_key = {item.key: item for item in health.collections}
    assert set(by_key) == {
        "market_snapshot:baostock:CSI300",
        "financial_snapshot:akshare:summary:CSI300",
    }, "history backfill runs are not live collections"
    market = by_key["market_snapshot:baostock:CSI300"]
    assert market.in_flight == 1 and queued
    assert [point.ratio for point in market.trend] == [1.0, 0.9967, 0.9]
    assert [point.subject for point in market.trend] == ["2026-10-12", "2026-10-13", "2026-10-14"]
    last = market.last_run
    assert last is not None and last.status == "partial" and last.stop_reason == "quota"
    assert last.missing_by_kind == {"failed": 1, "quota": 1, "no_data": 1}
    assert [item.ticker for item in last.missing] == ["600012.SH", "600011.SH", "600010.SH"]
    assert last.missing_total == 3
    assert health.backfill.akshare_financials == {"success": 1}

    warnings = {w.code: w for w in health.warnings}
    assert warnings["completeness_low"].level == "warn"
    assert "90.0%" in warnings["completeness_low"].message
    assert warnings["quota_stop"].level == "warn"
    assert warnings["quota_stop"].section == "collections"


async def test_old_quota_stop_and_failed_latest_run(db_session) -> None:
    await seed_members(db_session, 1)
    await add_market_run(
        db_session,
        date(2026, 10, 12),
        status="partial",
        ratio_collected=(300, 299),
        stop_reason="quota",
        missing={"600001.SH": ("quota", "quota")},
        finished_at=NOW - timedelta(days=2),
    )
    await add_run(
        db_session,
        "dividend_snapshot",
        {"provider": "baostock", "scope": "universe", "universe_code": UNIVERSE, "as_of": "2026-10-10"},
        status="failed",
        error_summary="ProviderError: baostock login failed",
        metadata={"completeness": completeness(300, 0)},
    )
    health = await build_data_health(db_session, health_settings(), now=NOW, today=TODAY)
    codes = _codes(health)
    assert "quota_stop" not in codes, "stops older than 24 h are history"
    failed = next(w for w in health.warnings if w.code == "collection_failed")
    assert failed.level == "error" and "baostock login failed" in failed.message
    assert health.warnings[0].level == "error", "errors sort first"


async def test_freshness_lag_coverage_and_periods(db_session) -> None:
    ids = await seed_members(db_session, 3)
    await add_bars(db_session, ids[:2], [date(2026, 10, 9), date(2026, 10, 12)], adjusts=("none", "backward"))
    await add_bars(db_session, ids[2:], [date(2026, 10, 9)])
    await add_bars(db_session, ids, [date(2026, 10, 14)], provider="ifind")  # watchlist rows ignored
    await add_financial(db_session, ids, date(2026, 3, 31))
    await add_financial(db_session, ids[:1], date(2026, 6, 30))
    await add_financial(db_session, ids, date(2026, 9, 30), provider="akshare")
    await add_dividend_coverage(db_session, ids[:2], 2026)
    await add_dividend_coverage(db_session, ids, 2025)

    health = await build_data_health(db_session, health_settings(), now=NOW, today=TODAY)
    market = health.freshness.market
    assert market.members == 3
    assert market.latest_trade_date == date(2026, 10, 12)
    assert market.members_at_latest == 2
    assert market.last_trading_day == TODAY
    assert market.expected_trade_date == date(2026, 10, 14)
    assert market.lag_trading_days == 2
    assert [(p.report_period, p.listings, p.coverage) for p in health.freshness.financial] == [
        (date(2026, 6, 30), 1, 0.3333),
        (date(2026, 3, 31), 3, 1.0),
    ]
    dividend = health.freshness.dividend
    assert (dividend.year, dividend.covered, dividend.members) == (2026, 2, 3)
    warnings = {w.code: w for w in health.warnings}
    assert warnings["market_stale"].level == "warn"
    assert "2026-10-12" in warnings["market_stale"].message
    assert warnings["market_coverage_low"].level == "warn"

    later = await build_data_health(
        db_session, health_settings(), now=NOW + timedelta(days=1), today=date(2026, 10, 16)
    )
    assert later.freshness.market.lag_trading_days == 3
    assert next(w for w in later.warnings if w.code == "market_stale").level == "error"


async def test_fresh_market_has_no_stale_warning(db_session) -> None:
    ids = await seed_members(db_session, 2)
    await add_bars(db_session, ids, [date(2026, 10, 14)])
    health = await build_data_health(db_session, health_settings(), now=NOW, today=TODAY)
    assert health.freshness.market.lag_trading_days == 0
    assert not {"market_stale", "market_coverage_low"} & _codes(health)


async def test_universe_sync_history_and_industry_coverage(db_session) -> None:
    ids = await seed_members(db_session, 3)
    # a member that left and its replacement
    left = await seed_members(db_session, 1, first=50, valid_from=date(2025, 6, 16))
    from sqlalchemy import update

    from investment_agent.storage.models.reference import UniverseMembership

    await db_session.execute(
        update(UniverseMembership)
        .where(UniverseMembership.listing_id == left[0])
        .values(valid_to=date(2026, 6, 15))
    )
    await add_run(
        db_session,
        "universe_sync",
        {"universe_code": UNIVERSE, "as_of": "2026-10-12"},
        metadata={
            "effective_date": "2026-06-15",
            "members": 3,
            "added": [ticker(2)],
            "removed": [ticker(50)],
            "changed": True,
        },
        finished_at=NOW - timedelta(days=20),
    )
    await add_industry(db_session, ids[0], "sw2021")
    await add_industry(db_session, ids[0], "csrc")
    await add_industry(db_session, ids[1], "csrc")
    await add_run(
        db_session,
        "industry_sync",
        {"as_of": "2026-10-01", "universe_codes": [UNIVERSE], "systems": ["csrc", "sw2021"]},
        status="partial",
        metadata={
            "applied_entries": 300,
            "changed": 2,
            "failed_systems": ["sw2021"],
            "errors": {"sw2021": "ProviderError: swsresearch.com unavailable"},
        },
        universe=None,
        finished_at=NOW - timedelta(days=50),
    )

    health = await build_data_health(db_session, health_settings(), now=NOW, today=TODAY)
    universe = health.universes[0]
    assert universe.members == 3
    assert universe.last_sync is not None
    assert (universe.last_sync.added, universe.last_sync.removed) == ([ticker(2)], [ticker(50)])
    assert universe.last_sync.effective_date == "2026-06-15"
    changes = {item.date: (item.added, item.removed) for item in universe.changes}
    assert changes[date(2026, 6, 15)] == (0, 1)
    assert changes[date(2026, 1, 5)] == (3, 0)
    industry = health.industry
    assert (industry.members, industry.sw2021, industry.csrc_fallback, industry.unclassified) == (3, 1, 1, 1)
    systems = {item.system: item for item in industry.systems}
    assert systems["csrc"].status == "success" and systems["csrc"].detail == "applied 300, changed 2"
    assert systems["sw2021"].status == "failed" and "swsresearch" in (systems["sw2021"].detail or "")
    codes = _codes(health)
    assert "universe_sync_stale" in codes
    assert "industry_sync_stale" in codes


async def test_backfill_progress_per_part(db_session) -> None:
    await seed_members(db_session, 1)
    base = {"universe_code": UNIVERSE, "as_of": "2026-10-07", "price_years": 5, "annual_years": 6, "quarters": 4}
    await add_run(
        db_session,
        "snapshot_backfill",
        {**base, "part": "membership"},
        metadata={"symbols": 4, "current": 3, "removed": 1, "change_dates": ["2026-06-15"]},
        key="snapshot_backfill:CSI300:2026-10-07:membership:p5a6q4",
    )
    chunk = [ticker(index) for index in range(4)]
    await add_run(
        db_session,
        "snapshot_backfill",
        {**base, "part": "prices", "symbols": chunk, "chunk": 0},
        status="partial",
        metadata={
            "collected_symbols": chunk[:3],
            "completeness": completeness(4, 3, {chunk[3]: ("failed", "timeout")}),
        },
        key="snapshot_backfill:CSI300:2026-10-07:prices:p5a6q4:c000",
    )
    health = await build_data_health(db_session, health_settings(), now=NOW, today=TODAY)
    parts = {part.part: part for part in health.backfill.parts}
    assert health.backfill.as_of == date(2026, 10, 7)
    assert (parts["membership"].done, parts["membership"].total) == (4, 4)
    assert "removed 1" in parts["membership"].detail
    prices = parts["prices"]
    assert (prices.done, prices.total, prices.failed) == (3, 4, 1)
    assert prices.failed_symbols == [chunk[3]]
    assert prices.statuses == {"partial": 1}
    assert parts["financials"].jobs == 0


async def test_budget_queue_and_conflicts(db_session) -> None:
    ids = await seed_members(db_session, 2)
    for minutes in (200, 100, 10):
        db_session.add(
            IfindCallLedger(
                at=NOW - timedelta(minutes=minutes), endpoint="/stock/history", ticker_count=3,
                outcome="ok", source="verification",
            )
        )
    db_session.add(  # outside the 5 h window
        IfindCallLedger(at=NOW - timedelta(hours=6), endpoint="/x", ticker_count=1, outcome="ok", source="cli")
    )

    def request(index: int, status: str, trigger: str, **extra) -> VerificationRequest:
        return VerificationRequest(
            listing_id=ids[index % 2],
            ticker=ticker(index % 2),
            trigger=trigger,
            priority=1,
            status=status,
            dedup_key=f"{index}:{trigger}:{status}",
            requested_at=NOW - timedelta(days=1),
            **extra,
        )

    db_session.add_all(
        [
            request(0, "pending", "promote"),
            request(1, "deferred", "conflict"),
            request(2, "running", "conflict"),
            request(3, "failed", "missing"),
            request(
                4,
                "done",
                "conflict",
                result={"report_period": "2025-12-31", "conflicts": ["revenue_total"], "verified": ["roe"]},
                finished_at=NOW - timedelta(hours=3),
            ),
            request(5, "done", "calibration", result={"conflicts": [], "verified": ["roe"]}, finished_at=NOW),
        ]
    )
    await add_run(
        db_session,
        "financial_snapshot",
        {"provider": "akshare", "scope": "universe", "universe_code": UNIVERSE,
         "report_period": "2020-12-31", "as_of": "2026-10-06", "part": "summary", "backfill": True},
        metadata={
            "completeness": completeness(2, 2),
            "provider_conflict": {"count": 1, "items": {ticker(1): {"debt_ratio": {"akshare": 0.5}}}},
        },
        finished_at=NOW - timedelta(days=1),
    )
    await db_session.flush()

    health = await build_data_health(
        db_session, health_settings(budget_cap=3), now=NOW, today=TODAY
    )
    budget = health.verification.budget
    assert (budget.cap, budget.used, budget.remaining) == (3, 3, 0)
    assert budget.next_free_at == NOW - timedelta(minutes=200) + timedelta(hours=5)
    assert health.verification.by_status == {
        "deferred": 1, "done": 2, "failed": 1, "pending": 1, "running": 1,
    }
    assert health.verification.open_by_trigger == {"conflict": 2, "promote": 1}
    conflicts = health.verification.conflicts
    assert [(c.source, c.ticker, c.fields, c.backfill) for c in conflicts] == [
        ("verification", ticker(0), ["revenue_total"], False),
        ("akshare_crosscheck", ticker(1), ["debt_ratio"], True),
    ]
    assert health.verification.conflicts_total == 2
    warnings = {w.code: w for w in health.warnings}
    assert warnings["budget_exhausted"].level == "warn"
    assert ticker(0) in warnings["provider_conflict"].message
    assert ticker(1) not in warnings["provider_conflict"].message, "backfill conflicts never warn"


async def test_overdue_lists_open_reports_only(db_session) -> None:
    ids = await seed_members(db_session, 2)
    await add_overdue(db_session, ids[0], date(2026, 6, 30))
    await add_overdue(db_session, ids[1], date(2026, 6, 30), resolved=date(2026, 9, 3))
    health = await build_data_health(db_session, health_settings(), now=NOW, today=TODAY)
    assert health.overdue.open == 1
    assert [(item.ticker, item.report_period) for item in health.overdue.items] == [
        (ticker(0), date(2026, 6, 30))
    ]
    assert "report_overdue" in _codes(health)


async def test_accumulation_reset_warning_and_calendar_passthrough(db_session) -> None:
    await seed_members(db_session, 1)
    await add_market_run(db_session, date(2026, 9, 30))
    await add_market_run(db_session, date(2026, 10, 9), ratio_collected=(300, 200))
    calendar = HealthWarning(code="calendar", level="warn", message="日历即将到期", section="freshness")
    health = await build_data_health(
        db_session, health_settings(calendar_warnings=(calendar,)), now=NOW, today=TODAY
    )
    assert health.accumulation.streak_weeks == 0
    assert health.accumulation.weeks_with_snapshots == 2
    codes = _codes(health)
    assert {"accumulation_reset", "calendar"} <= codes


@pytest.mark.parametrize("threshold", [0.5, 0.99])
async def test_threshold_comes_from_settings(db_session, threshold: float) -> None:
    await seed_members(db_session, 1)
    await add_market_run(db_session, date(2026, 10, 14), ratio_collected=(100, 97))
    health = await build_data_health(
        db_session, health_settings(threshold=threshold), now=NOW, today=TODAY
    )
    assert health.threshold == threshold
    assert (health.accumulation.streak_weeks == 1) is (threshold == 0.5)
    assert ("completeness_low" in _codes(health)) is (threshold == 0.99)


async def test_dashboard_summary_survives_a_failing_health_check(db_session, monkeypatch, tmp_path) -> None:
    from investment_agent.api.routes import dashboard, data_health
    from tests.unit._snapshot_support import settings_for

    async def boom(*args, **kwargs):
        raise RuntimeError("broken aggregate")

    monkeypatch.setattr(data_health, "compute_data_health", boom)
    warnings, counts = await dashboard.data_health_summary(db_session, settings_for(tmp_path))
    assert [w.code for w in warnings] == ["data_health_unavailable"]
    assert warnings[0].level == "error" and warnings[0].link == "/app/health"
    assert counts is None


async def test_dashboard_summary_keeps_only_warn_and_error(db_session, tmp_path) -> None:
    from investment_agent.api.routes import dashboard
    from tests.unit._snapshot_support import settings_for

    warnings, counts = await dashboard.data_health_summary(db_session, settings_for(tmp_path))
    assert warnings == []  # empty database: info only
    assert counts is not None and counts.infos >= 1 and counts.errors == counts.warnings == 0
