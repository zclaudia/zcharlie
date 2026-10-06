"""Crontab weekday numbers must keep their standard meaning (0/7 = Sunday).

APScheduler 3.x numbers weekdays from Monday = 0, so an untranslated
"30 15 * * 1-5" fired Tuesday-Saturday and the Monday refresh never ran.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from investment_agent.scheduler.runner import _crontab_dow, _parse_cron

TZ = ZoneInfo("Asia/Shanghai")


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("1-5", "mon,tue,wed,thu,fri"),
        ("1", "mon"),
        ("6", "sat"),
        ("0", "sun"),
        ("7", "sun"),
        ("0,6", "sun,sat"),
        ("1-7", "mon,tue,wed,thu,fri,sat,sun"),
        ("1-5/2", "mon,wed,fri"),
        ("*", "*"),
        ("sun", "sun"),
        ("mon-fri", "mon-fri"),
    ],
)
def test_crontab_weekday_translation(field: str, expected: str) -> None:
    assert _crontab_dow(field) == expected


@pytest.mark.parametrize("field", ["8", "5-1", "x-3", "3/2"])
def test_invalid_crontab_weekdays_are_rejected(field: str) -> None:
    with pytest.raises(ValueError):
        _crontab_dow(field)


def _fire_days(cron: str, count: int = 7) -> list[str]:
    trigger = _parse_cron(cron, timezone="Asia/Shanghai")
    now = datetime(2026, 10, 9, 23, 0, tzinfo=TZ)  # Friday night
    days = []
    for _ in range(count):
        fire = trigger.get_next_fire_time(None, now)
        assert fire is not None
        days.append(fire.strftime("%a"))
        now = fire.replace(second=1)
    return days


def test_weekday_refresh_fires_monday_to_friday() -> None:
    assert _fire_days("30 15 * * 1-5") == ["Mon", "Tue", "Wed", "Thu", "Fri", "Mon", "Tue"]


def test_monday_and_saturday_jobs_keep_their_days() -> None:
    assert set(_fire_days("0 8 * * 1", 3)) == {"Mon"}
    assert set(_fire_days("0 9 * * 6", 3)) == {"Sat"}
    assert set(_fire_days("0 20 * * 0", 3)) == {"Sun"}
