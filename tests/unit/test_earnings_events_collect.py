"""``earnings_event`` job (mrd-intake C1 / C2): payload and key, season / weekday gates,
scope union, the three parts (storage, dedup, PIT, unchanged captures), the
overdue reconciliation, breaker / circuit ``partial`` paths, the worker handler
and the CLI command."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from typer.testing import CliRunner

from investment_agent.providers.akshare_eastmoney import (
    EastmoneyTransientError,
    RawTable,
)
from investment_agent.providers.circuit import ANY_ENDPOINT, CircuitBreaker
from investment_agent.providers.errors import ProviderUnavailableError
from investment_agent.research.worker import ResearchWorker
from investment_agent.scheduler.snapshot_jobs import (
    EARNINGS_WINDOWS,
    earnings_report_period,
    in_earnings_window,
    is_first_trading_day_of_week,
    snapshot_payloads,
)
from investment_agent.scheduler.trading_calendar import load_trading_calendar
from investment_agent.snapshots.collect import ArtifactWriter, SnapshotPreconditionError
from investment_agent.snapshots.earnings_events import (
    MAX_SEGMENT_PERIODS,
    collect_earnings_events,
    earnings_scope,
    reconcile_overdue,
    upcoming_trading_days,
)
from investment_agent.snapshots.eastmoney import MAX_CONSECUTIVE_FAILURES
from investment_agent.snapshots.jobs import (
    EARNINGS_EVENT,
    EarningsEventPayload,
    enqueue_snapshot_job,
    idempotency_key,
    payload_kind,
)
from investment_agent.storage.database import init_engine, session_scope
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.base import Base
from investment_agent.storage.models.candidates import CandidateRecord
from investment_agent.storage.models.earnings_events import (
    BusinessSegmentRecord,
    DisclosureScheduleRecord,
    EarningsForecastRecord,
)
from investment_agent.storage.models.user_domain import HoldingItem
from investment_agent.storage.repositories.earnings_events import EarningsEventRepository
from investment_agent.storage.repositories.listing import ListingRepository
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository
from investment_agent.storage.repositories.report_overdue import ReportOverdueRepository
from tests.unit._eastmoney_fake import (
    EVENTS_CAPTURED_AT,
    FakeEastmoneyBackend,
    fake_client,
    schedule_table,
)
from tests.unit._snapshot_support import (
    handlers_for,
    seed_universe,
    seed_watchlist,
    settings_for,
)

ROOT = Path(__file__).resolve().parents[2]
CALENDAR = load_trading_calendar(ROOT / "config" / "calendars" / "a_share.yaml")

H1_2026 = date(2026, 6, 30)
Q3_2026 = date(2026, 9, 30)
AS_OF = date(2026, 10, 9)  # Friday; next trading days 10-12 / 10-13 / 10-14
POOL = ["600030.SH", "600519.SH", "601318.SH", "000001.SZ"]


# --------------------------------------------------------------------------- #
# Fixtures and helpers
# --------------------------------------------------------------------------- #


def _client(backend: FakeEastmoneyBackend | None = None, **kwargs):
    kwargs.setdefault("clock", EVENTS_CAPTURED_AT)
    return fake_client(backend, **kwargs)


def _payload(part: str, **kwargs) -> EarningsEventPayload:
    kwargs.setdefault("universe_code", "CSI300")
    kwargs.setdefault("report_period", H1_2026 if part == "forecast" else Q3_2026)
    kwargs.setdefault("as_of", AS_OF)
    kwargs.setdefault("provider", "akshare")
    return EarningsEventPayload(part=part, **kwargs)  # type: ignore[arg-type]


async def _collect(payload, tmp_path, client=None, *, circuit=None, run_id: int = 1):
    return await collect_earnings_events(
        payload,
        client=client or _client(),
        artifacts=ArtifactWriter(tmp_path, run_id=run_id, attempt=1),
        lease=None,
        now=lambda: EVENTS_CAPTURED_AT,
        circuit=circuit,
        calendar=CALENDAR,
        run_id=run_id,
    )


async def _seed_pools() -> dict[str, int]:
    """Target pool POOL; watchlist 002107 + 000002; candidate 000856; holding 300633."""

    ids = await seed_universe(POOL)
    ids.update(await seed_watchlist(["002107.SZ", "000002.SZ"]))
    async with session_scope() as session:
        listings = ListingRepository(session)
        candidate = await listings.ensure_a_share_listing("000856.SZ")
        holding = await listings.ensure_a_share_listing("300633.SZ")
        ids["000856.SZ"], ids["300633.SZ"] = candidate.id, holding.id
        session.add(
            CandidateRecord(
                listing_id=candidate.id, ticker="000856.SZ", status="open",
                opened_at=EVENTS_CAPTURED_AT, expires_at=EVENTS_CAPTURED_AT + timedelta(days=14),
                sources=[],
            )
        )
        session.add(
            HoldingItem(
                listing_id=holding.id, cost_basis=Decimal(10), shares=Decimal(100),
                opened_at=date(2026, 9, 1),
            )
        )
    return ids


async def _rows(model):
    async with session_scope() as session:
        return list((await session.execute(select(model).order_by(model.id))).scalars().all())


# --------------------------------------------------------------------------- #
# Payload, key, schedule
# --------------------------------------------------------------------------- #


def test_payload_key_and_validation():
    payload = _payload("forecast")
    assert payload_kind(payload) == EARNINGS_EVENT and payload.provider == "akshare"
    assert idempotency_key(payload) == (
        "earnings_event:akshare:CSI300:2026-06-30:2026-10-09:forecast"
    )
    assert idempotency_key(_payload("segments")) == (
        "earnings_event:akshare:CSI300:2026-09-30:2026-10-09:segments"
    )
    assert idempotency_key(
        EarningsEventPayload(part="schedule", scope="watchlist", report_period=Q3_2026, as_of=AS_OF)
    ) == "earnings_event:akshare:watchlist:2026-09-30:2026-10-09:schedule"
    assert EarningsEventPayload.model_validate(payload.model_dump(mode="json")) == payload
    with pytest.raises(ValidationError, match="akshare"):
        _payload("forecast", provider="baostock")
    with pytest.raises(ValidationError, match="quarter end"):
        _payload("forecast", report_period=date(2026, 7, 1))
    with pytest.raises(ValidationError):
        _payload("dividends")


def test_schedule_gate_season_trading_day_and_weekly_segments():
    assert EARNINGS_WINDOWS[0] == ((1, 1), (1, 31)) and len(EARNINGS_WINDOWS) == 4
    assert in_earnings_window(date(2026, 10, 31)) and not in_earnings_window(date(2026, 11, 1))
    assert earnings_report_period(date(2026, 10, 9)) == Q3_2026
    assert earnings_report_period(date(2026, 1, 15)) == date(2025, 12, 31)
    # Friday 2026-10-09: forecast + schedule of 2026Q3; Monday 10-12 adds the segments.
    friday = snapshot_payloads("earnings_event", today=AS_OF, calendar=CALENDAR, universes=["CSI300"])
    assert [p.part for p in friday] == ["forecast", "schedule"]  # type: ignore[union-attr]
    assert all(p.report_period == Q3_2026 and p.as_of == AS_OF for p in friday)  # type: ignore[union-attr]
    monday = snapshot_payloads(
        "earnings_event", today=date(2026, 10, 12), calendar=CALENDAR, universes=["CSI300", "SSE50"]
    )
    assert [(p.universe_code, p.part) for p in monday] == [  # type: ignore[union-attr]
        ("CSI300", "forecast"), ("CSI300", "schedule"), ("CSI300", "segments"),
        ("SSE50", "forecast"), ("SSE50", "schedule"), ("SSE50", "segments"),
    ]
    # Thursday 2026-10-08 is the first trading day of its week (10-05 .. 10-07 are
    # National Day holidays), so it also enqueues the segments; Friday does not.
    assert is_first_trading_day_of_week(CALENDAR, date(2026, 10, 8))
    assert not is_first_trading_day_of_week(CALENDAR, date(2026, 10, 9))
    assert not is_first_trading_day_of_week(CALENDAR, date(2026, 10, 10))  # Saturday
    thursday = snapshot_payloads(
        "earnings_event", today=date(2026, 10, 8), calendar=CALENDAR, universes=["CSI300"]
    )
    assert [p.part for p in thursday] == ["forecast", "schedule", "segments"]  # type: ignore[union-attr]
    holiday = snapshot_payloads(
        "earnings_event", today=date(2026, 10, 1), calendar=CALENDAR, universes=["CSI300"]
    )
    assert holiday == []
    off_season = snapshot_payloads(
        "earnings_event", today=date(2026, 9, 30), calendar=CALENDAR, universes=["CSI300"]
    )
    assert off_season == []
    with pytest.raises(ValueError, match="needs the trading calendar"):
        snapshot_payloads("earnings_event", today=AS_OF, calendar=None, universes=["CSI300"])


def test_upcoming_trading_days_skip_weekends_and_holidays():
    assert upcoming_trading_days(AS_OF, CALENDAR) == [
        date(2026, 10, 12), date(2026, 10, 13), date(2026, 10, 14),
    ]
    assert upcoming_trading_days(date(2026, 9, 30), CALENDAR) == [
        date(2026, 10, 8), date(2026, 10, 9), date(2026, 10, 12),
    ]
    # No calendar (or an unreviewed year): weekdays.
    assert upcoming_trading_days(date(2031, 1, 3), None) == [
        date(2031, 1, 6), date(2031, 1, 7), date(2031, 1, 8),
    ]
    assert upcoming_trading_days(date(2031, 1, 3), CALENDAR)[0] == date(2031, 1, 6)


# --------------------------------------------------------------------------- #
# Scope
# --------------------------------------------------------------------------- #


async def test_scope_unions_target_pool_and_user_pools(db_session):
    del db_session
    ids = await _seed_pools()
    members, pools = await earnings_scope(_payload("forecast"))
    assert list(members) == [
        "000001.SZ", "000002.SZ", "000856.SZ", "002107.SZ", "300633.SZ", "600030.SH",
        "600519.SH", "601318.SH",
    ]
    assert members["600519.SH"] == ids["600519.SH"]
    assert pools.pools_of("002107.SZ") == ["watchlist"] and pools.pools_of("600519.SH") == []
    assert pools.pools_of("000856.SZ") == ["candidate"] and pools.pools_of("300633.SZ") == ["holding"]
    segments, _ = await earnings_scope(_payload("segments"))
    assert list(segments) == ["000002.SZ", "000856.SZ", "002107.SZ", "300633.SZ"]
    only_user, _ = await earnings_scope(_payload("schedule", scope="watchlist", universe_code=None))
    assert list(only_user) == ["000002.SZ", "000856.SZ", "002107.SZ", "300633.SZ"]


async def test_empty_target_pool_is_a_precondition_error(db_session, tmp_path):
    del db_session
    with pytest.raises(SnapshotPreconditionError, match="universe_sync"):
        await _collect(_payload("forecast"), tmp_path)
    # Segments never need the target pool: empty user pools mean nothing to do.
    report = await _collect(_payload("segments"), tmp_path)
    assert report.status == "success" and report.metadata["members"] == 0


# --------------------------------------------------------------------------- #
# Forecasts
# --------------------------------------------------------------------------- #


async def test_forecasts_are_stored_once_and_new_pool_forecasts_are_listed(db_session, tmp_path):
    del db_session
    ids = await _seed_pools()
    client = _client()
    report = await _collect(_payload("forecast"), tmp_path, client)

    assert report.status == "success" and report.provider == "akshare"
    assert client.calls == 1
    rows = await _rows(EarningsForecastRecord)
    by_ticker = {
        next(t for t, i in ids.items() if i == row.listing_id): row for row in rows
    }
    # Only scope members with a forecast: 600519 / 601318 / 000001 never forecast 2026H1,
    # 688208 (in the answer) is outside the scope.
    assert set(by_ticker) == {"600030.SH", "000002.SZ", "002107.SZ"}
    citic = by_ticker["600030.SH"]
    assert (citic.report_period, citic.notice_date, citic.forecast_type) == (
        H1_2026, date(2026, 7, 11), "预增",
    )
    assert citic.indicator == "归属于上市公司股东的净利润"
    assert citic.profit_low == Decimal("23343000000") == citic.profit_high
    assert citic.yoy_low == Decimal("0.7015") and citic.provider == "akshare"
    assert citic.captured_at.replace(tzinfo=UTC) == EVENTS_CAPTURED_AT
    assert citic.raw_ref == "snapshot-runs/1/attempt-1/earnings-forecast-2026-06-30.json"
    assert (tmp_path / citic.raw_ref).exists()
    vanke = by_ticker["000002.SZ"]
    assert vanke.forecast_type == "增亏" and vanke.profit_low == Decimal("-15000000000")
    assert "亏损" in vanke.content

    metadata = report.metadata
    assert metadata["completeness"]["ratio"] == 1.0 if "completeness" in metadata else True
    assert metadata["stored"] == 3 and metadata["existing"] == 0 and metadata["in_scope"] == 3
    assert metadata["stored_by_type"] == {"增亏": 1, "预增": 2}
    # Only the user's pools are notified: 600030 is a target-pool member only.
    listed = {item["ticker"]: item for item in metadata["new_forecasts"]}
    assert set(listed) == {"000002.SZ", "002107.SZ"} and metadata["new_forecast_count"] == 2
    assert listed["002107.SZ"]["pools"] == ["watchlist"]
    assert listed["002107.SZ"]["forecast_type"] == "预增"
    assert listed["002107.SZ"]["profit_low"] == 65_000_000
    assert listed["002107.SZ"]["raw_ref"] == citic.raw_ref

    # The same notices again: nothing inserted, nothing to notify.
    again = await _collect(_payload("forecast"), tmp_path, client, run_id=2)
    assert again.status == "success"
    assert again.metadata["stored"] == 0 and again.metadata["existing"] == 3
    assert again.metadata["new_forecasts"] == [] and again.metadata["new_forecast_count"] == 0
    assert len(await _rows(EarningsForecastRecord)) == 3


async def test_forecast_lookahead_and_undisclosed_period(db_session, tmp_path):
    del db_session
    await _seed_pools()
    # Captured before the 2026-08-19 notice of 600187 (in the market answer): that row
    # is deferred, everything dated up to the capture date is stored, the run succeeds.
    early = _client(clock=datetime(2026, 8, 18, 12, tzinfo=UTC))
    report = await _collect(_payload("forecast"), tmp_path, early)
    assert report.status == "success"
    assert report.metadata["deferred_count"] > 0
    assert all(item["notice_date"] > "2026-08-18" for item in report.metadata["deferred"])
    stored = await _rows(EarningsForecastRecord)
    assert all(row.notice_date <= date(2026, 8, 18) for row in stored)
    # A period nobody has forecast yet: success with nothing stored.
    report = await _collect(_payload("forecast", report_period=Q3_2026), tmp_path)
    assert report.status == "success"
    assert report.metadata["market_listings"] == 0 and report.metadata["stored"] == 0


async def test_forecast_circuit_open_is_partial_without_a_call(db_session, tmp_path):
    del db_session
    await _seed_pools()
    circuit = CircuitBreaker(lambda: EVENTS_CAPTURED_AT)
    circuit.open_now("akshare", EastmoneyTransientError("down", provider="akshare"))
    client = _client(circuit=circuit)
    report = await _collect(_payload("forecast"), tmp_path, client, circuit=circuit)
    assert report.status == "partial" and report.stop_reason == "provider_unavailable"
    assert client.calls == 0 and report.completeness.stopped == sorted(report.completeness.expected)


# --------------------------------------------------------------------------- #
# Disclosure schedule
# --------------------------------------------------------------------------- #


async def test_schedule_appends_changes_only_and_lists_upcoming_pool_disclosures(
    db_session, tmp_path
):
    del db_session
    ids = await _seed_pools()
    client = _client()
    report = await _collect(_payload("schedule"), tmp_path, client)
    assert report.status == "success"
    rows = await _rows(DisclosureScheduleRecord)
    by_ticker = {next(t for t, i in ids.items() if i == row.listing_id): row for row in rows}
    assert set(by_ticker) == set(POOL) | {"002107.SZ", "000002.SZ", "000856.SZ", "300633.SZ"}
    moved = by_ticker["300633.SZ"]
    assert (moved.first_date, moved.scheduled_date, moved.actual_date) == (
        date(2026, 10, 22), date(2026, 10, 29), None,
    )
    assert moved.captured_at.replace(tzinfo=UTC) == EVENTS_CAPTURED_AT
    assert moved.raw_ref == "snapshot-runs/1/attempt-1/earnings-schedule-2026-09-30.json"
    metadata = report.metadata
    assert metadata["appended"] == 8 and metadata["unchanged"] == 0
    assert metadata["upcoming_window"] == ["2026-10-12", "2026-10-13", "2026-10-14"]
    # 002107 (watchlist) appointed Saturday 10-10 and 000856 (candidate) 10-13 are
    # upcoming; 000002 (watchlist, 10-30) and 600030 (target pool only, 10-27) are not.
    upcoming = {item["ticker"]: item for item in metadata["upcoming_disclosures"]}
    assert set(upcoming) == {"002107.SZ", "000856.SZ"}
    assert upcoming["002107.SZ"]["scheduled_date"] == "2026-10-10"
    assert upcoming["002107.SZ"]["trading_days_ahead"] == 0
    assert upcoming["000856.SZ"]["trading_days_ahead"] == 2
    assert upcoming["000856.SZ"]["pools"] == ["candidate"]
    assert upcoming["000856.SZ"]["raw_ref"] == moved.raw_ref

    # Unchanged answer the next day: no new rows (append on change only).
    again = await _collect(_payload("schedule", as_of=date(2026, 10, 12)), tmp_path, client, run_id=2)
    assert again.metadata["appended"] == 0 and again.metadata["unchanged"] == 8
    assert len(await _rows(DisclosureScheduleRecord)) == 8

    # 002107 publishes on 10-12: a new row with the actual date is appended.
    table = schedule_table()
    published = RawTable(
        table.columns,
        tuple(
            {**row, "实际披露时间": "2026-10-12T00:00:00"} if row["股票代码"] == "002107" else row
            for row in table.rows
        ),
    )
    later = _client(
        FakeEastmoneyBackend(overrides={("schedule", "20260930"): published}),
        clock=datetime(2026, 10, 12, 11, tzinfo=UTC),
    )
    third = await _collect(_payload("schedule", as_of=date(2026, 10, 12)), tmp_path, later, run_id=3)
    assert third.metadata["appended"] == 1 and third.metadata["published"] == 1
    rows = await _rows(DisclosureScheduleRecord)
    assert len(rows) == 9 and rows[-1].actual_date == date(2026, 10, 12)
    assert rows[-1].listing_id == ids["002107.SZ"]
    assert "002107.SZ" not in {item["ticker"] for item in third.metadata["upcoming_disclosures"]}
    async with session_scope() as session:
        latest = await EarningsEventRepository(session).latest_schedules([ids["002107.SZ"]], Q3_2026)
    assert latest[ids["002107.SZ"]].actual_date == date(2026, 10, 12)


async def test_schedule_reconciles_open_overdue_rows_without_resolving(db_session, tmp_path):
    del db_session
    ids = await _seed_pools()
    async with session_scope() as session:
        await ReportOverdueRepository(session).record(
            listing_id=ids["002107.SZ"], report_period=Q3_2026, deadline=date(2026, 10, 31),
            detected_on=date(2026, 11, 2), provider="baostock",
        )
    table = schedule_table()
    published = RawTable(
        table.columns,
        tuple(
            {**row, "实际披露时间": "2026-11-03T00:00:00"}
            if row["股票代码"] in {"002107", "600519"}
            else row
            for row in table.rows
        ),
    )
    client = _client(
        FakeEastmoneyBackend(overrides={("schedule", "20260930"): published}),
        clock=datetime(2026, 11, 3, 11, tzinfo=UTC),
    )
    report = await _collect(_payload("schedule", as_of=date(2026, 11, 3)), tmp_path, client)
    reconcile = report.metadata["report_overdue_reconcile"]
    assert reconcile["deadline"] == "2026-10-31" and reconcile["checked"] is True
    assert reconcile["published_per_eastmoney"] == [
        {"ticker": "002107.SZ", "actual_date": "2026-11-03"}
    ]
    # 600519 published late per Eastmoney but was never recorded overdue (baostock may
    # already have it): listed, never recorded here.
    assert reconcile["late_not_recorded"] == [{"ticker": "600519.SH", "actual_date": "2026-11-03"}]
    async with session_scope() as session:
        rows = await ReportOverdueRepository(session).for_period(Q3_2026)
    assert len(rows) == 1 and rows[0].resolved_on is None
    empty = await reconcile_overdue(period=Q3_2026, listing_by_ticker={}, actual_dates={})
    assert empty["checked"] is False


# --------------------------------------------------------------------------- #
# Business segments
# --------------------------------------------------------------------------- #


async def test_segments_store_the_user_pools_and_skip_unchanged_captures(db_session, tmp_path):
    del db_session
    await seed_universe(POOL)
    ids = await seed_watchlist(["600519.SH", "000002.SZ"])
    client = _client()
    report = await _collect(_payload("segments"), tmp_path, client)
    assert report.status == "success" and client.calls == 2
    rows = await _rows(BusinessSegmentRecord)
    assert {row.listing_id for row in rows} == {ids["600519.SH"]}
    assert len({row.report_period for row in rows}) == 4 <= MAX_SEGMENT_PERIODS
    latest = [row for row in rows if row.report_period == H1_2026]
    assert [(row.kind, row.name) for row in latest][:3] == [
        ("product", "茅台酒"), ("product", "其他系列酒"), ("product", "其他(补充)"),
    ]
    moutai = latest[0]
    assert moutai.revenue == Decimal("77724437925.48") and moutai.share == Decimal("0.856909")
    assert moutai.margin == Decimal("0.922793") and moutai.profit == Decimal("71723553696.78")
    assert moutai.provider == "akshare" and moutai.raw_ref == (
        "snapshot-runs/1/attempt-1/earnings-segments-600519.SH.json"
    )
    assert (tmp_path / moutai.raw_ref).exists()
    # 000002 has no fixture answer: Eastmoney "no data" counts as no_data, not a failure.
    assert report.completeness.missing["000002.SZ"][0] == "no_data"
    metadata = report.metadata
    assert metadata["stored_periods"] == 4 and metadata["unchanged_periods"] == 0
    assert metadata["latest_periods"] == {"600519.SH": "2026-06-30"}

    again = await _collect(_payload("segments"), tmp_path, client, run_id=2)
    assert again.metadata["stored_periods"] == 0 and again.metadata["unchanged_periods"] == 4
    assert len(await _rows(BusinessSegmentRecord)) == len(rows)

    async with session_scope() as session:
        chosen = await EarningsEventRepository(session).latest_segments(ids["600519.SH"])
    assert chosen is not None and chosen.report_period == H1_2026 and chosen.kind == "product"
    assert [row.name for row in chosen.rows] == ["茅台酒", "其他系列酒", "其他(补充)"]
    async with session_scope() as session:
        older = await EarningsEventRepository(session).latest_segments(
            ids["600519.SH"], as_of=date(2026, 3, 1)
        )
    assert older is not None and older.report_period == date(2025, 12, 31)


async def test_segments_breaker_stops_after_consecutive_failures_and_opens_the_circuit(
    db_session, tmp_path
):
    del db_session
    ids = await seed_watchlist(["000002.SZ", "002107.SZ", "600030.SH", "600519.SH", "601318.SH"])
    errors = [EastmoneyTransientError("boom", provider="akshare")] * MAX_CONSECUTIVE_FAILURES
    circuit = CircuitBreaker(lambda: EVENTS_CAPTURED_AT)
    client = _client(FakeEastmoneyBackend(errors=list(errors)), circuit=circuit, max_attempts=1)
    report = await _collect(_payload("segments"), tmp_path, client, circuit=circuit)
    assert report.status == "partial" and report.stop_reason == "provider_unavailable"
    assert report.completeness.failed == ["000002.SZ", "002107.SZ", "600030.SH"]
    assert report.completeness.stopped == ["600519.SH", "601318.SH"]
    assert circuit.status("akshare", ANY_ENDPOINT).is_open(EVENTS_CAPTURED_AT)
    assert ids and await _rows(BusinessSegmentRecord) == []

    # The next run hits the open circuit before any call.
    client2 = _client(circuit=circuit)
    second = await _collect(_payload("segments"), tmp_path, client2, circuit=circuit, run_id=2)
    assert second.status == "partial" and client2.calls == 0
    assert second.completeness.stopped == sorted(ids)


async def test_segments_isolated_failure_keeps_the_rest(db_session, tmp_path):
    del db_session
    await seed_watchlist(["000002.SZ", "600519.SH"])
    client = _client(
        FakeEastmoneyBackend(errors=[EastmoneyTransientError("once", provider="akshare")]),
        max_attempts=1,
    )
    report = await _collect(_payload("segments"), tmp_path, client)
    assert report.status == "partial" and report.stop_reason is None
    assert report.completeness.failed == ["000002.SZ"]
    assert report.completeness.collected == {"600519.SH"}


# --------------------------------------------------------------------------- #
# Worker handler and CLI
# --------------------------------------------------------------------------- #


async def test_worker_handler_runs_the_three_parts(db_session, tmp_path):
    del db_session
    await _seed_pools()
    settings = settings_for(tmp_path)
    async with session_scope() as session:
        ids = []
        for part in ("forecast", "schedule", "segments"):
            run, _ = await enqueue_snapshot_job(session, _payload(part), triggered_by="test")
            ids.append(run.id)
    worker = ResearchWorker(
        settings=settings,
        worker_id="w",
        handlers=handlers_for(settings, None, eastmoney=_client(), clock=lambda: EVENTS_CAPTURED_AT),
    )
    for run_id in ids:
        assert await worker.run_once() == run_id
    async with session_scope() as session:
        runs = [await PipelineRunRepository(session).get(run_id) for run_id in ids]
    assert [run.status for run in runs] == ["success", "success", "success"]
    assert [run.metadata_json["part"] for run in runs] == ["forecast", "schedule", "segments"]
    assert runs[0].metadata_json["new_forecast_count"] == 2
    assert runs[1].metadata_json["upcoming_count"] == 2
    assert runs[2].metadata_json["members"] == 4
    assert len(await _rows(EarningsForecastRecord)) == 3


async def test_failed_whole_market_call_fails_the_run_for_retry(db_session, tmp_path):
    del db_session
    await _seed_pools()
    client = _client(
        FakeEastmoneyBackend(errors=[EastmoneyTransientError("down", provider="akshare")] * 2),
    )
    report = await _collect(_payload("schedule"), tmp_path, client)
    assert report.status == "failed" and report.all_failures_transient
    assert await _rows(DisclosureScheduleRecord) == []
    circuit = CircuitBreaker(lambda: EVENTS_CAPTURED_AT)
    unavailable = ProviderUnavailableError(
        "open", provider="akshare", open_until=EVENTS_CAPTURED_AT, fail_count=3
    )
    report = await _collect(
        _payload("schedule"), tmp_path,
        _client(FakeEastmoneyBackend(errors=[unavailable])), circuit=circuit,
    )
    assert report.status == "partial" and report.stop_reason == "provider_unavailable"


@pytest.fixture
def cli_db(tmp_path, monkeypatch):
    db_path = tmp_path / "cli.db"
    url = f"sqlite+aiosqlite:///{db_path}"
    monkeypatch.setenv("API_KEY", "secret")
    monkeypatch.setenv("LLM_TIER3_API_KEY", "x")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", url)

    async def create() -> None:
        engine = init_engine(url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await engine.dispose()

    asyncio.run(create())
    return url


def _keys(url: str) -> list[str]:
    async def load() -> list[str]:
        engine = init_engine(url)
        async with session_scope() as session:
            keys = list(
                (
                    await session.execute(
                        select(PipelineRun.idempotency_key).order_by(PipelineRun.id)
                    )
                ).scalars()
            )
        await engine.dispose()
        return [str(key) for key in keys]

    return asyncio.run(load())


def test_cli_collect_earnings_events(cli_db):
    from investment_agent.cli.main import app

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["snapshot", "collect-earnings-events", "--part", "forecast", "--period", "20250930",
         "--as-of", "2026-10-09"],
    )
    assert result.exit_code == 0, result.output
    result = runner.invoke(
        app, ["snapshot", "collect-earnings-events", "--part", "segments", "--as-of", "2026-10-09"]
    )
    assert result.exit_code == 0, result.output
    result = runner.invoke(
        app,
        ["snapshot", "collect-earnings-events", "--part", "schedule", "--scope", "watchlist",
         "--period", "2026-09-30", "--as-of", "2026-10-09"],
    )
    assert result.exit_code == 0, result.output
    result = runner.invoke(
        app, ["snapshot", "collect-earnings-events", "--part", "dividends", "--as-of", "2026-10-09"]
    )
    assert result.exit_code != 0 and "--part" in result.output
    result = runner.invoke(
        app, ["snapshot", "collect-earnings-events", "--part", "forecast", "--period", "2026-07-01"]
    )
    assert result.exit_code != 0 and "quarter end" in result.output
    assert _keys(cli_db) == [
        "earnings_event:akshare:CSI300:2025-09-30:2026-10-09:forecast",
        "earnings_event:akshare:CSI300:2026-09-30:2026-10-09:segments",
        "earnings_event:akshare:watchlist:2026-09-30:2026-10-09:schedule",
    ]


def test_fake_backend_records_calls_for_the_three_parts():
    backend = FakeEastmoneyBackend()
    assert backend.forecast_table("20260630") is not None and backend.forecast_table("20260930") is None
    assert backend.schedule_table("20260930") is not None and backend.schedule_table("20260630") is None
    assert backend.segment_table("SH600519") is not None and backend.segment_table("SZ000002") is None
    assert backend.calls == [
        "forecast:20260630", "forecast:20260930", "schedule:20260930", "schedule:20260630",
        "segments:SH600519", "segments:SZ000002",
    ]
