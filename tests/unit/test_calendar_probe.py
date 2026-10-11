"""Scheduled calendar probe: dual-source check, candidate file, notifications."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from investment_agent.config import (
    AppYamlConfig,
    SchedulerConfig,
    Settings,
    SnapshotScheduleConfig,
)
from investment_agent.notifiers.file_notifier import FileNotifier
from investment_agent.providers.akshare_calendar import fetch_trading_days
from investment_agent.scheduler import runner
from investment_agent.scheduler.calendar_refresh import (
    build_year,
    days_of_year,
    merge_years,
    parse_calendar_text,
    render_calendar,
)
from investment_agent.scheduler.trading_calendar import TradingCalendar, load_trading_calendar
from investment_agent.settings.loader import runtime_calendar_path
from investment_agent.snapshots.calendar_probe import candidate_path, collect_calendar_probe
from investment_agent.snapshots.jobs import CalendarProbePayload

CALENDAR_PATH = Path("config/calendars/a_share.yaml")
WEEKDAYS_2027 = [day for day in days_of_year(2027) if day.weekday() < 5]
HOLIDAY_2027 = date(2027, 1, 1)  # a Friday; the fixture below closes it
TRADING_2027 = {day for day in WEEKDAYS_2027 if day != HOLIDAY_2027}


class FakeTradeDates:
    """Day-by-day answers for the probe (baostock stand-in)."""

    def __init__(self, answers: dict[date, bool] | None = None) -> None:
        self.answers = (
            answers
            if answers is not None
            else {day: day in TRADING_2027 for day in days_of_year(2027)}
        )
        self.calls: list[tuple[date, date]] = []

    async def fetch_trade_dates(self, start: date, end: date) -> dict[date, bool]:
        self.calls.append((start, end))
        return {day: flag for day, flag in self.answers.items() if start <= day <= end}


class FakeSina:
    def __init__(self, days: set[date]) -> None:
        self.days = days

    def tool_trade_date_hist_sina(self) -> dict[str, list[date]]:
        return {"trade_date": sorted(self.days)}


def _calendar() -> TradingCalendar:
    """The committed calendar (2026 only) - the probe then looks at 2027."""

    return load_trading_calendar(CALENDAR_PATH)


def _notifications(tmp_path: Path) -> list[Path]:
    return sorted((tmp_path / "notifications").glob("**/*.md"))


async def _probe(
    calendar: TradingCalendar | None,
    *,
    today: date,
    tmp_path: Path,
    year: int = 2027,
    answers: dict[date, bool] | None = None,
    sina_days: set[date] | None = None,
) -> tuple[Any, FakeTradeDates]:
    client = FakeTradeDates(answers)
    sina = set(TRADING_2027) if sina_days is None else sina_days
    outcome = await collect_calendar_probe(
        CalendarProbePayload(as_of=today, year=year),
        calendar=calendar,
        reference_client=client,
        sina_backend=FakeSina(sina),
        fetch_trading_days=fetch_trading_days,
        data_dir=tmp_path,
        notifier=FileNotifier(tmp_path / "notifications"),
        run_id=42,
        today=today,
    )
    return outcome, client


async def test_probe_skips_outside_the_expiry_window(tmp_path: Path) -> None:
    outcome, client = await _probe(_calendar(), today=date(2026, 6, 1), tmp_path=tmp_path)

    assert outcome.status == "success"
    assert outcome.metadata["status"] == "not_due"
    assert client.calls == []
    assert _notifications(tmp_path) == []


async def test_probe_skips_a_year_the_calendar_already_covers(tmp_path: Path) -> None:
    reviewed_2027 = build_year(2027, TRADING_2027).model_copy(
        update={"verified_by": ["baostock", "akshare"], "verified_on": date(2026, 12, 18)}
    )
    reviewed = merge_years(_calendar(), [reviewed_2027])

    outcome, client = await _probe(reviewed, today=date(2027, 11, 15), tmp_path=tmp_path)

    assert outcome.metadata["status"] == "up_to_date"
    assert client.calls == []


async def test_probe_reports_an_unpublished_year_silently(tmp_path: Path) -> None:
    outcome, _ = await _probe(
        _calendar(), today=date(2026, 12, 1), answers={}, tmp_path=tmp_path
    )

    assert outcome.status == "success"
    assert outcome.metadata["status"] == "not_published"
    assert _notifications(tmp_path) == []
    assert not candidate_path(tmp_path, 2027).exists()


async def test_probe_writes_a_candidate_and_notifies_once(tmp_path: Path) -> None:
    today = date(2026, 12, 1)
    outcome, _ = await _probe(_calendar(), today=today, tmp_path=tmp_path)

    assert outcome.status == "success"
    assert outcome.metadata["status"] == "candidate"
    candidate = candidate_path(tmp_path, 2027)
    assert candidate.is_file() and outcome.metadata["candidate"] == str(candidate)

    reviewed = parse_calendar_text(candidate.read_text(encoding="utf-8"))
    assert [item.year for item in reviewed.years] == [2026, 2027]
    item = reviewed.years[-1]
    assert item.closed_dates == {HOLIDAY_2027}
    assert (item.verified_by, item.verified_on) == (["baostock", "akshare"], today)

    files = _notifications(tmp_path)
    assert len(files) == 1
    body = files[0].read_text(encoding="utf-8")
    assert "交易日历候选 2027" in body and "2027-01-01" in body
    assert "数据健康" in body and "DATA_DIR/calendars/a_share.yaml" in body

    # A second probe with identical answers rewrites nothing and stays quiet.
    repeat, _ = await _probe(_calendar(), today=today, tmp_path=tmp_path)
    assert repeat.metadata["changed"] is False
    assert len(_notifications(tmp_path)) == 1


async def test_probe_aborts_on_a_source_disagreement(tmp_path: Path) -> None:
    missing = sorted(TRADING_2027)[100]
    outcome, _ = await _probe(
        _calendar(),
        today=date(2026, 12, 1),
        sina_days=set(TRADING_2027) - {missing},
        tmp_path=tmp_path,
    )

    assert outcome.status == "partial"
    assert outcome.metadata == {
        "status": "conflict",
        "year": 2027,
        "disagreements": [missing.isoformat()],
    }
    assert not candidate_path(tmp_path, 2027).exists()
    files = _notifications(tmp_path)
    assert len(files) == 1 and "分歧" in files[0].read_text(encoding="utf-8")


async def test_probe_reports_an_unreadable_calendar(tmp_path: Path) -> None:
    outcome, client = await _probe(None, today=date(2026, 12, 1), tmp_path=tmp_path)

    assert outcome.status == "partial"
    assert outcome.metadata["status"] == "calendar_unavailable"
    assert client.calls == []


async def test_probe_job_enqueues_only_inside_the_expiry_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enqueued: list[tuple[Any, str]] = []

    async def fake_enqueue(session: Any, payload: Any, *, triggered_by: str, **_: Any) -> Any:
        enqueued.append((payload, triggered_by))
        return SimpleNamespace(id=7, idempotency_key=f"key:{payload.year}"), True

    @asynccontextmanager
    async def fake_scope() -> Any:
        yield None

    monkeypatch.setattr(runner, "enqueue_snapshot_job", fake_enqueue)
    monkeypatch.setattr(runner, "session_scope", fake_scope)

    outside = await runner.run_calendar_probe_job(
        calendar_path=str(CALENDAR_PATH), timezone="Asia/Shanghai", today=date(2026, 6, 1)
    )
    assert outside == 0 and enqueued == []

    inside = await runner.run_calendar_probe_job(
        calendar_path=str(CALENDAR_PATH), timezone="Asia/Shanghai", today=date(2026, 11, 16)
    )
    assert inside == 1
    payload, triggered_by = enqueued[0]
    assert (payload.year, payload.as_of) == (2027, date(2026, 11, 16))
    assert triggered_by == "scheduler"

    unavailable = await runner.run_calendar_probe_job(
        calendar_path="config/calendars/missing.yaml",
        timezone="Asia/Shanghai",
        today=date(2026, 11, 16),
    )
    assert unavailable is None and len(enqueued) == 1


async def test_probe_job_reads_the_data_dir_calendar(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Per-fire resolution: a data-dir copy that already has 2027 silences the probe."""

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    reviewed = merge_years(
        _calendar(),
        [
            build_year(2027, TRADING_2027).model_copy(
                update={"verified_by": ["baostock", "akshare"], "verified_on": date(2026, 12, 18)}
            )
        ],
    )
    target = runtime_calendar_path(CALENDAR_PATH, data_dir=tmp_path)
    target.parent.mkdir(parents=True)
    target.write_text(render_calendar(reviewed), encoding="utf-8")

    enqueued: list[Any] = []

    async def fake_enqueue(session: Any, payload: Any, *, triggered_by: str, **_: Any) -> Any:
        enqueued.append(payload)
        return SimpleNamespace(id=3, idempotency_key=f"key:{payload.year}"), True

    @asynccontextmanager
    async def fake_scope() -> Any:
        yield None

    monkeypatch.setattr(runner, "enqueue_snapshot_job", fake_enqueue)
    monkeypatch.setattr(runner, "session_scope", fake_scope)

    created = await runner.run_calendar_probe_job(
        calendar_path=str(CALENDAR_PATH), timezone="Asia/Shanghai", today=date(2026, 11, 16)
    )

    assert created == 0 and enqueued == []  # the baseline alone would have enqueued 2027


def test_build_scheduler_registers_the_calendar_probe_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("API_KEY", "x")
    monkeypatch.setenv("LLM_TIER3_API_KEY", "x")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    def job_ids(config: SnapshotScheduleConfig) -> set[str]:
        built = runner.build_scheduler(
            settings=settings, app_config=AppYamlConfig(scheduler=SchedulerConfig(), snapshot=config)
        )
        return {job.id for job in built.get_jobs()}

    assert "calendar_probe" in job_ids(SnapshotScheduleConfig(calendar_probe_cron="0 8 * * 1"))
    assert "calendar_probe" not in job_ids(SnapshotScheduleConfig(calendar_probe_cron=None))


def test_calendar_probe_is_a_validated_schedule_setting() -> None:
    from investment_agent.settings.registry import SPECS_BY_KEY, SnapshotSchedule

    spec = SPECS_BY_KEY["schedule.snapshot.calendar_probe_cron"]
    assert spec.yaml_path == ("snapshot", "calendar_probe_cron")
    with pytest.raises(ValueError):
        SnapshotSchedule(calendar_probe_cron="0 8 * * 9")


def test_candidates_lists_parseable_files_by_year(tmp_path: Path) -> None:
    from investment_agent.snapshots.calendar_probe import candidates

    assert candidates(tmp_path) == []
    good = merge_years(
        _calendar(), [build_year(2027, TRADING_2027).model_copy(update={"verified_on": date(2026, 12, 18)})]
    )
    path = candidate_path(tmp_path, 2027)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_calendar(good), encoding="utf-8")
    (path.parent / "a_share-2028.yaml").write_text("not: a calendar\n", encoding="utf-8")

    found = candidates(tmp_path)

    assert [item.year for item in found] == [2027]
    assert found[0].closed_dates == (HOLIDAY_2027,)
    assert found[0].trading_days == len(TRADING_2027)
    assert found[0].verified_on == date(2026, 12, 18)


def _write_candidate(tmp_path: Path, calendar: TradingCalendar) -> Any:
    from investment_agent.snapshots.calendar_probe import candidates

    path = candidate_path(tmp_path, max(item.year for item in calendar.years))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_calendar(calendar), encoding="utf-8")
    return candidates(tmp_path)[-1]


def test_apply_candidate_adds_the_year_and_removes_the_candidate(tmp_path: Path) -> None:
    from investment_agent.snapshots.calendar_probe import apply_candidate

    current_path = tmp_path / "a_share.yaml"
    current_path.write_text(render_calendar(_calendar()), encoding="utf-8")
    proposed = merge_years(
        _calendar(),
        [
            build_year(2027, TRADING_2027).model_copy(
                update={"verified_by": ["baostock", "akshare"], "verified_on": date(2026, 12, 18)}
            )
        ],
    )
    candidate = _write_candidate(tmp_path, proposed)

    written, added = apply_candidate(current_path, candidate)

    assert added == (2027,)
    assert [item.year for item in written.years] == [2026, 2027]
    assert load_trading_calendar(current_path).years[-1].closed_dates == {HOLIDAY_2027}
    assert not candidate.path.exists()


@pytest.mark.parametrize("scenario", ["missing_year", "changed_year", "adds_nothing"])
def test_apply_candidate_only_ever_adds_years(tmp_path: Path, scenario: str) -> None:
    from investment_agent.snapshots.calendar_probe import CandidateConflictError, apply_candidate

    current = _calendar()
    current_path = tmp_path / "a_share.yaml"
    current_path.write_text(render_calendar(current), encoding="utf-8")
    reviewed_2027 = build_year(2027, TRADING_2027)
    if scenario == "missing_year":
        proposed = current.model_copy(update={"years": [reviewed_2027]})  # 2026 dropped
    elif scenario == "changed_year":
        changed_2026 = current.years[0].model_copy(
            update={"closed_dates": current.years[0].closed_dates | {date(2026, 7, 1)}}
        )
        proposed = current.model_copy(update={"years": [changed_2026, reviewed_2027]})
    else:
        proposed = current
    candidate = _write_candidate(tmp_path, proposed)

    with pytest.raises(CandidateConflictError, match="2026|adds no year"):
        apply_candidate(current_path, candidate)

    assert load_trading_calendar(current_path) == current
    assert candidate.path.exists()
