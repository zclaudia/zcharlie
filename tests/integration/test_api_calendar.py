"""``/calendar`` - status, manual check and candidate apply (health page card)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import desc, select

from investment_agent.scheduler.calendar_refresh import (
    build_year,
    days_of_year,
    merge_years,
    parse_calendar_text,
    render_calendar,
)
from investment_agent.scheduler.trading_calendar import load_trading_calendar
from investment_agent.snapshots.calendar_probe import candidate_path
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.analysis import PipelineRun

REPO_CALENDAR = Path("config/calendars/a_share.yaml")
KEY = {"X-API-Key": "secret"}
WEEKDAYS_2027 = [day for day in days_of_year(2027) if day.weekday() < 5]
TRADING_2027 = {day for day in WEEKDAYS_2027 if day != date(2027, 1, 1)}


def _app_yaml(tmp_path: Path, calendar: Path) -> Path:
    path = tmp_path / "app.yaml"
    path.write_text(
        "scheduler:\n"
        f"  a_share_calendar_path: {calendar}\n"
        "  watchlist_refresh_cron: null\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def calendar_env(tmp_path, monkeypatch) -> Path:
    """A scratch copy of the committed calendar plus an app.yaml pointing at it."""

    calendar = tmp_path / "a_share.yaml"
    calendar.write_bytes(REPO_CALENDAR.read_bytes())
    import investment_agent.api.routes.dashboard as dashboard

    monkeypatch.setattr(dashboard, "_APP_CONFIG_PATH", _app_yaml(tmp_path, calendar))
    return calendar


def _write_candidate(tmp_path: Path, *, calendar: Path | None = None) -> Path:
    current = load_trading_calendar(calendar or REPO_CALENDAR)
    proposed = merge_years(
        current,
        [
            build_year(2027, TRADING_2027).model_copy(
                update={"verified_by": ["baostock", "akshare"], "verified_on": date(2026, 12, 18)}
            )
        ],
    )
    path = candidate_path(tmp_path, 2027)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_calendar(proposed), encoding="utf-8")
    return path


def test_calendar_routes_require_auth(api_client) -> None:
    assert api_client.get("/calendar/status").status_code == 401
    assert api_client.post("/calendar/check").status_code == 401
    assert api_client.post("/calendar/apply").status_code == 401


def test_status_reports_coverage_and_no_candidate(api_client, calendar_env, tmp_path) -> None:
    body = api_client.get("/calendar/status", headers=KEY).json()

    assert body["calendar_id"] == "CN_A_SHARE" and body["timezone"] == "Asia/Shanghai"
    assert [item["year"] for item in body["years"]] == [2026]
    assert body["coverage"]["end"] == "2026-12-31"
    assert body["coverage"]["expired"] is False
    assert body["candidate"] is None
    assert body["writable"] is True
    assert body["last_probe"] is None
    assert body["path"] == str(calendar_env) and body["baseline"] == str(calendar_env)
    assert body["source"] == "baseline"
    assert body["target"] == str(tmp_path / "calendars" / "a_share.yaml")


def test_status_lists_the_pending_candidate(api_client, calendar_env, tmp_path) -> None:
    _write_candidate(tmp_path, calendar=calendar_env)

    body = api_client.get("/calendar/status", headers=KEY).json()

    candidate = body["candidate"]
    assert candidate["year"] == 2027
    assert candidate["closed_dates"] == ["2027-01-01"]
    assert candidate["trading_days"] == len(TRADING_2027)
    assert candidate["verified_by"] == ["baostock", "akshare"]
    assert candidate["verified_on"] == "2026-12-18"


def test_apply_writes_the_data_dir_copy_and_removes_the_candidate(
    api_client, calendar_env, tmp_path
) -> None:
    repo_before = REPO_CALENDAR.read_bytes()
    baseline_before = calendar_env.read_bytes()
    candidate = _write_candidate(tmp_path, calendar=calendar_env)
    target = tmp_path / "calendars" / "a_share.yaml"

    response = api_client.post("/calendar/apply", headers=KEY)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["added_years"] == [2027]
    assert body["removed_candidate"] == str(candidate)
    assert body["path"] == str(target)
    assert not candidate.exists()
    written = load_trading_calendar(target)
    assert [item.year for item in written.years] == [2026, 2027]
    item = written.years[-1]
    assert item.closed_dates == {date(2027, 1, 1)}
    assert item.verified_on == date(2026, 12, 18)
    assert calendar_env.read_bytes() == baseline_before, "the baseline must stay untouched"
    assert REPO_CALENDAR.read_bytes() == repo_before, "the repository calendar must stay untouched"

    # The data-dir copy takes precedence for every reader from now on.
    status = api_client.get("/calendar/status", headers=KEY).json()
    assert status["source"] == "data_dir" and status["path"] == str(target)
    assert [item["year"] for item in status["years"]] == [2026, 2027]

    # A second apply adds the next year on top of the runtime copy.
    two_years = load_trading_calendar(target)
    candidate_2028 = candidate_path(tmp_path, 2028)
    candidate_2028.write_text(
        render_calendar(
            merge_years(two_years, [build_year(2028, {d for d in days_of_year(2028) if d.weekday() < 5})])
        ),
        encoding="utf-8",
    )
    second = api_client.post("/calendar/apply", headers=KEY)
    assert second.status_code == 200, second.text
    assert second.json()["added_years"] == [2028]
    assert [item.year for item in load_trading_calendar(target).years] == [2026, 2027, 2028]


def test_apply_without_a_candidate_is_404(api_client, calendar_env) -> None:
    response = api_client.post("/calendar/apply", headers=KEY)

    assert response.status_code == 404
    assert "候选" in response.json()["detail"]


def test_apply_refuses_to_rewrite_a_reviewed_year(api_client, calendar_env, tmp_path) -> None:
    """A candidate is only allowed to add years, never to change one."""

    current = load_trading_calendar(calendar_env)
    tampered = current.model_copy(
        update={
            "years": [
                current.years[0].model_copy(
                    update={"closed_dates": current.years[0].closed_dates | {date(2026, 7, 1)}}
                ),
                build_year(2027, TRADING_2027),
            ]
        }
    )
    path = candidate_path(tmp_path, 2027)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_calendar(tampered), encoding="utf-8")

    response = api_client.post("/calendar/apply", headers=KEY)

    assert response.status_code == 409
    assert "2026" in response.json()["detail"]
    assert path.exists() and load_trading_calendar(calendar_env).years[0] == current.years[0]


def test_apply_reports_an_unwritable_data_dir(api_client, calendar_env, tmp_path) -> None:
    """A read-only data dir (the only thing the write needs) is a 409, not a crash."""

    _write_candidate(tmp_path, calendar=calendar_env)
    tmp_path.chmod(0o555)
    try:
        response = api_client.post("/calendar/apply", headers=KEY)
    finally:
        tmp_path.chmod(0o755)

    assert response.status_code == 409
    assert "DATA_DIR" in response.json()["detail"]
    assert not (tmp_path / "calendars").exists()


async def test_check_enqueues_a_forced_probe(api_client, calendar_env) -> None:
    response = api_client.post("/calendar/check", headers=KEY)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["year"] == 2027 and body["created"] is True
    async with session_scope() as session:
        run = await session.scalar(
            select(PipelineRun)
            .where(PipelineRun.kind == "calendar_probe")
            .order_by(desc(PipelineRun.id))
            .limit(1)
        )
        assert run is not None and run.id == body["run_id"]
        assert run.request_json == {"as_of": run.request_json["as_of"], "year": 2027, "force": True}
        assert run.triggered_by == "api"
    status_body = api_client.get("/calendar/status", headers=KEY).json()
    assert status_body["last_probe"]["run_id"] == body["run_id"]
    assert status_body["last_probe"]["status"] == "queued"


def test_status_marks_an_unreadable_calendar(api_client, tmp_path, monkeypatch) -> None:
    import investment_agent.api.routes.dashboard as dashboard

    monkeypatch.setattr(
        dashboard, "_APP_CONFIG_PATH", _app_yaml(tmp_path, tmp_path / "missing.yaml")
    )

    assert api_client.get("/calendar/status", headers=KEY).status_code == 503


def test_render_round_trip_of_the_scratch_candidate(tmp_path, calendar_env) -> None:
    """Guard for the fixture itself: the candidate parses back as rendered."""

    path = _write_candidate(tmp_path, calendar=calendar_env)
    parsed = parse_calendar_text(path.read_text(encoding="utf-8"))
    assert parsed.years[-1].closed_dates == {date(2027, 1, 1)}
