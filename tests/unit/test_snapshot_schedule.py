"""Snapshot collection schedule: trigger decisions (pure) and job registration."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from investment_agent.config import (
    AppYamlConfig,
    SchedulerConfig,
    Settings,
    SnapshotScheduleConfig,
    load_app_yaml,
)
from investment_agent.scheduler import runner
from investment_agent.scheduler.snapshot_jobs import (
    financial_periods_due,
    is_financial_deadline_trigger,
    is_first_trading_day_of_month,
    snapshot_payloads,
    statutory_deadline,
)
from investment_agent.scheduler.trading_calendar import load_trading_calendar
from investment_agent.snapshots.jobs import (
    FinancialSnapshotPayload,
    IndustrySyncPayload,
    MarketSnapshotPayload,
    UniverseSyncPayload,
)
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.analysis import PipelineRun

ROOT = Path(__file__).resolve().parents[2]
CALENDAR_PATH = str(ROOT / "config" / "calendars" / "a_share.yaml")
CALENDAR = load_trading_calendar(Path(CALENDAR_PATH))


def test_statutory_deadlines():
    assert statutory_deadline(date(2026, 3, 31)) == date(2026, 4, 30)
    assert statutory_deadline(date(2026, 6, 30)) == date(2026, 8, 31)
    assert statutory_deadline(date(2026, 9, 30)) == date(2026, 10, 31)
    assert statutory_deadline(date(2025, 12, 31)) == date(2026, 4, 30)


@pytest.mark.parametrize(
    ("today", "expected"),
    [
        # Q3 season open; H1 deadline passed.
        (date(2026, 10, 6), [date(2026, 6, 30), date(2026, 9, 30)]),
        # After 04-30 both the annual report and Q1 are due.
        (date(2026, 5, 6), [date(2025, 12, 31), date(2026, 3, 31)]),
        # Annual + Q1 season in progress; Q3 is the latest passed.
        (date(2026, 4, 15), [date(2025, 9, 30), date(2025, 12, 31), date(2026, 3, 31)]),
        (date(2026, 9, 1), [date(2026, 6, 30)]),
        (date(2026, 11, 7), [date(2026, 9, 30)]),
        (date(2027, 1, 9), [date(2026, 9, 30), date(2026, 12, 31)]),
    ],
)
def test_financial_periods_due(today, expected):
    assert financial_periods_due(today) == expected


def test_first_trading_day_of_month_respects_holidays():
    # 2026-10-01..07 are holidays (and the weekend): first session is 10-08.
    assert not is_first_trading_day_of_month(CALENDAR, date(2026, 10, 1))
    assert is_first_trading_day_of_month(CALENDAR, date(2026, 10, 8))
    assert not is_first_trading_day_of_month(CALENDAR, date(2026, 10, 9))
    assert is_first_trading_day_of_month(CALENDAR, date(2026, 9, 1))


def test_financial_deadline_trigger_days():
    # 2026-05-01..05 closed -> first session after 04-30 is 05-06.
    assert is_financial_deadline_trigger(CALENDAR, date(2026, 5, 6))
    assert not is_financial_deadline_trigger(CALENDAR, date(2026, 5, 7))
    assert not is_financial_deadline_trigger(CALENDAR, date(2026, 4, 30))
    assert is_financial_deadline_trigger(CALENDAR, date(2026, 9, 1))
    # 10-31 is a Saturday -> Monday 11-02.
    assert is_financial_deadline_trigger(CALENDAR, date(2026, 11, 2))
    assert not is_financial_deadline_trigger(CALENDAR, date(2026, 11, 3))


def test_payload_decisions():
    universes = ["CSI300", "SSE50"]
    monday = date(2026, 9, 28)
    assert snapshot_payloads(
        "universe_sync", today=monday, calendar=None, universes=universes
    ) == [
        UniverseSyncPayload(universe_code="CSI300", as_of=monday),
        UniverseSyncPayload(universe_code="SSE50", as_of=monday),
    ]
    # Market snapshot only on trading days.
    assert snapshot_payloads(
        "market_snapshot", today=date(2026, 10, 6), calendar=CALENDAR, universes=universes
    ) == []
    assert snapshot_payloads(
        "market_snapshot", today=date(2026, 9, 30), calendar=CALENDAR, universes=["CSI300"]
    ) == [MarketSnapshotPayload(universe_code="CSI300", trade_date=date(2026, 9, 30))]
    # Industry sync once a month, for the union of universes.
    assert snapshot_payloads(
        "industry_sync", today=date(2026, 10, 8), calendar=CALENDAR, universes=universes
    ) == [IndustrySyncPayload(as_of=date(2026, 10, 8), universe_codes=("CSI300", "SSE50"))]
    monthly = snapshot_payloads(
        "industry_sync", today=date(2026, 10, 8), calendar=CALENDAR, universes=universes
    )[0]
    assert isinstance(monthly, IndustrySyncPayload)
    assert monthly.systems == ("csrc", "sw2021")  # CSRC and Shenwan 2021 (P1-A9)
    assert snapshot_payloads(
        "industry_sync", today=date(2026, 10, 9), calendar=CALENDAR, universes=universes
    ) == []
    saturday = date(2026, 10, 10)
    assert snapshot_payloads(
        "financial_weekly", today=saturday, calendar=None, universes=["CSI300"]
    ) == [
        FinancialSnapshotPayload(
            universe_code="CSI300", report_period=date(2026, 6, 30), as_of=saturday
        ),
        FinancialSnapshotPayload(
            universe_code="CSI300", report_period=date(2026, 9, 30), as_of=saturday
        ),
        # Eastmoney period summaries after baostock (P1-A11); 10-10 is outside
        # the cash-flow windows, so no per-stock statements.
        FinancialSnapshotPayload(
            provider="akshare",
            part="summary",
            universe_code="CSI300",
            report_period=date(2026, 6, 30),
            as_of=saturday,
        ),
        FinancialSnapshotPayload(
            provider="akshare",
            part="summary",
            universe_code="CSI300",
            report_period=date(2026, 9, 30),
            as_of=saturday,
        ),
    ]
    assert snapshot_payloads(
        "financial_deadline", today=date(2026, 10, 9), calendar=CALENDAR, universes=["CSI300"]
    ) == []
    deadline = snapshot_payloads(
        "financial_deadline", today=date(2026, 11, 2), calendar=CALENDAR, universes=["CSI300"]
    )
    assert [(p.provider, getattr(p, "part", None)) for p in deadline] == [
        ("baostock", None),
        ("akshare", "summary"),
    ]
    with pytest.raises(ValueError, match="calendar"):
        snapshot_payloads("market_snapshot", today=monday, calendar=None, universes=["CSI300"])


def test_snapshot_config_validates_universes():
    with pytest.raises(ValidationError):
        SnapshotScheduleConfig(universes=["SPX"])
    shipped = load_app_yaml(ROOT / "config" / "app.yaml")
    assert shipped.snapshot is not None
    assert shipped.snapshot.universes == ["CSI300"]
    assert shipped.snapshot.market_snapshot_cron == "30 18 * * 1-5"
    assert SnapshotScheduleConfig().market_snapshot_cron == "30 18 * * 1-5"


def _settings(monkeypatch) -> Settings:
    monkeypatch.setenv("API_KEY", "x")
    monkeypatch.setenv("LLM_TIER3_API_KEY", "x")
    return Settings(_env_file=None)  # type: ignore[call-arg]


def test_build_scheduler_registers_snapshot_jobs(monkeypatch):
    cfg = AppYamlConfig(
        scheduler=SchedulerConfig(a_share_calendar_path=CALENDAR_PATH),
        snapshot=SnapshotScheduleConfig(financial_deadline_cron=None),
    )
    sched = runner.build_scheduler(settings=_settings(monkeypatch), app_config=cfg)
    jobs = {job.id: job for job in sched.get_jobs()}
    assert set(jobs) == {
        "snapshot_universe_sync",
        "snapshot_industry_sync",
        "snapshot_market_snapshot",
        "snapshot_financial_weekly",
    }
    market = jobs["snapshot_market_snapshot"]
    assert market.misfire_grace_time == runner.MISFIRE_GRACE_SECONDS
    assert market.coalesce is True
    assert str(market.trigger.timezone) == "Asia/Shanghai"
    assert market.args == ("market_snapshot",)


def test_build_scheduler_without_snapshot_section_has_no_snapshot_jobs(monkeypatch):
    cfg = AppYamlConfig(scheduler=SchedulerConfig())
    sched = runner.build_scheduler(settings=_settings(monkeypatch), app_config=cfg)
    assert sched.get_jobs() == []


async def _runs() -> list[PipelineRun]:
    async with session_scope() as session:
        return list((await session.execute(select(PipelineRun).order_by(PipelineRun.id))).scalars())


async def test_scheduled_market_job_enqueues_once_per_date(db_session):
    del db_session
    cfg = SnapshotScheduleConfig(universes=["CSI300", "SSE50"])
    created = await runner.run_snapshot_schedule_job(
        "market_snapshot", calendar_path=CALENDAR_PATH, snapshot_cfg=cfg, today=date(2026, 9, 30)
    )
    again = await runner.run_snapshot_schedule_job(
        "market_snapshot", calendar_path=CALENDAR_PATH, snapshot_cfg=cfg, today=date(2026, 9, 30)
    )
    holiday = await runner.run_snapshot_schedule_job(
        "market_snapshot", calendar_path=CALENDAR_PATH, snapshot_cfg=cfg, today=date(2026, 10, 1)
    )
    assert (created, again, holiday) == (2, 0, 0)
    runs = await _runs()
    assert [run.idempotency_key for run in runs] == [
        "market_snapshot:baostock:CSI300:2026-09-30",
        "market_snapshot:baostock:SSE50:2026-09-30",
    ]
    assert all(
        run.request_json["provider"] == "baostock" and run.request_json["scope"] == "universe"
        for run in runs
    )
    assert all(run.triggered_by == "scheduler" and run.status == "queued" for run in runs)


async def test_unknown_calendar_year_skips_gated_jobs_but_not_universe_sync(db_session):
    del db_session
    cfg = SnapshotScheduleConfig()
    gated = await runner.run_snapshot_schedule_job(
        "market_snapshot", calendar_path=CALENDAR_PATH, snapshot_cfg=cfg, today=date(2030, 1, 7)
    )
    assert gated is None
    universe = await runner.run_snapshot_schedule_job(
        "universe_sync", calendar_path=CALENDAR_PATH, snapshot_cfg=cfg, today=date(2030, 1, 7)
    )
    assert universe == 1


async def test_startup_catches_up_snapshot_jobs_that_fired_today(db_session):
    del db_session
    cfg = AppYamlConfig(
        scheduler=SchedulerConfig(a_share_calendar_path=CALENDAR_PATH),
        snapshot=SnapshotScheduleConfig(),
    )
    # Wednesday 2026-09-30 19:30 Asia/Shanghai: market (18:30), the gated
    # 08:00 industry and 19:00 deadline jobs already fired today;
    # Monday/Saturday jobs did not.
    caught = await runner._startup_snapshot_check(cfg, datetime(2026, 9, 30, 11, 30, tzinfo=UTC))
    assert set(caught) == {"industry_sync", "market_snapshot", "financial_deadline"}
    runs = await _runs()
    assert [run.kind for run in runs] == ["market_snapshot"]

    early = await runner._startup_snapshot_check(cfg, datetime(2026, 9, 29, 23, 0, tzinfo=UTC))
    assert early == []  # 07:00 Shanghai: nothing has fired yet


def test_cash_flow_statements_only_in_disclosure_windows():
    from investment_agent.scheduler.snapshot_jobs import in_cash_flow_window

    assert in_cash_flow_window(date(2026, 4, 15)) and in_cash_flow_window(date(2026, 5, 15))
    assert not in_cash_flow_window(date(2026, 4, 14)) and not in_cash_flow_window(date(2026, 5, 16))
    assert in_cash_flow_window(date(2026, 8, 22)) and in_cash_flow_window(date(2026, 10, 17))
    assert not in_cash_flow_window(date(2026, 12, 5))
    saturday = date(2026, 10, 17)
    payloads = snapshot_payloads(
        "financial_weekly", today=saturday, calendar=None, universes=["CSI300"]
    )
    (cash_flow,) = [p for p in payloads if getattr(p, "part", None) == "cash_flow"]
    assert isinstance(cash_flow, FinancialSnapshotPayload)
    assert cash_flow.provider == "akshare" and cash_flow.scope == "universe"
    assert cash_flow.periods == (date(2026, 9, 30), date(2026, 6, 30))
    assert payloads[-1] is cash_flow  # after baostock and the summaries
    deadline = snapshot_payloads(
        "financial_deadline", today=date(2026, 11, 2), calendar=CALENDAR, universes=["CSI300"]
    )
    assert all(getattr(p, "part", None) != "cash_flow" for p in deadline)
