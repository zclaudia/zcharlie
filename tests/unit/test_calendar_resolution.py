"""Calendar resolution: the DATA_DIR copy takes precedence over the baseline."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from investment_agent.config import Settings
from investment_agent.scheduler.calendar_refresh import render_calendar
from investment_agent.scheduler.trading_calendar import load_trading_calendar
from investment_agent.settings.loader import (
    calendar_path,
    configured_calendar_path,
    resolved_calendar_path,
    runtime_calendar_path,
    runtime_data_dir,
)
from investment_agent.snapshots.handlers import default_calendar

REPO_CALENDAR = Path("config/calendars/a_share.yaml")


def test_runtime_path_keeps_the_baseline_file_name(tmp_path: Path) -> None:
    target = runtime_calendar_path(REPO_CALENDAR, data_dir=tmp_path)

    assert target == tmp_path / "calendars" / "a_share.yaml"
    assert not target.exists()  # computed, not created


def test_resolution_prefers_the_data_dir_copy(tmp_path: Path) -> None:
    assert resolved_calendar_path(REPO_CALENDAR, data_dir=tmp_path) == REPO_CALENDAR

    target = runtime_calendar_path(REPO_CALENDAR, data_dir=tmp_path)
    target.parent.mkdir(parents=True)
    target.write_text(render_calendar(load_trading_calendar(REPO_CALENDAR)), encoding="utf-8")

    assert resolved_calendar_path(REPO_CALENDAR, data_dir=tmp_path) == target


def test_a_broken_runtime_copy_is_not_silently_ignored(tmp_path: Path) -> None:
    """Fail closed: a corrupt copy must surface, never fall back to the baseline."""

    target = runtime_calendar_path(REPO_CALENDAR, data_dir=tmp_path)
    target.parent.mkdir(parents=True)
    target.write_text("calendar_id: broken\n", encoding="utf-8")

    resolved = resolved_calendar_path(REPO_CALENDAR, data_dir=tmp_path)

    assert resolved == target
    with pytest.raises(ValidationError):
        load_trading_calendar(resolved)


def test_runtime_data_dir_follows_the_environment(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "env-data"))
    assert runtime_data_dir() == tmp_path / "env-data"

    monkeypatch.delenv("DATA_DIR")
    assert runtime_data_dir() == Path(str(Settings.model_fields["data_dir"].default))


def test_calendar_path_resolves_through_the_environment(monkeypatch, tmp_path: Path) -> None:
    baseline = Path(configured_calendar_path(Path("config/app.yaml")))
    assert baseline == REPO_CALENDAR
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    assert Path(calendar_path(Path("config/app.yaml"))) == REPO_CALENDAR  # no copy yet

    target = runtime_calendar_path(baseline, data_dir=tmp_path)
    target.parent.mkdir(parents=True)
    target.write_text(render_calendar(load_trading_calendar(baseline)), encoding="utf-8")

    assert Path(calendar_path(Path("config/app.yaml"))) == target


def test_worker_default_calendar_follows_the_data_dir(monkeypatch, tmp_path: Path) -> None:
    """The worker runtime (probe, snapshot gates) reads the effective calendar."""

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    assert [item.year for item in default_calendar().years] == [2026]

    baseline = load_trading_calendar(REPO_CALENDAR)
    trimmed = baseline.model_copy(update={"years": []})
    target = runtime_calendar_path(REPO_CALENDAR, data_dir=tmp_path)
    target.parent.mkdir(parents=True)
    target.write_text(render_calendar(trimmed), encoding="utf-8")

    assert default_calendar().years == []
