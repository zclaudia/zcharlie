"""Business-date clock helper tests."""

from datetime import date

from freezegun import freeze_time

from investment_agent.clock import shanghai_today, today_in


@freeze_time("2026-10-08T20:00:00Z")
def test_shanghai_today_rolls_over_before_utc_midnight() -> None:
    assert shanghai_today() == date(2026, 10, 9)
    assert today_in("UTC") == date(2026, 10, 8)
    assert today_in("America/New_York") == date(2026, 10, 8)


@freeze_time("2026-10-08T15:59:59Z")
def test_shanghai_today_before_local_midnight() -> None:
    assert shanghai_today() == date(2026, 10, 8)
