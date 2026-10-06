"""Versioned A-share trading calendar tests."""

from datetime import date
from pathlib import Path

import pytest

from investment_agent.scheduler.trading_calendar import (
    TradingCalendar,
    UnknownTradingCalendarYearError,
    load_trading_calendar,
)


def test_official_2026_calendar_handles_holiday_weekday_and_weekend():
    calendar = load_trading_calendar(Path("config/calendars/a_share.yaml"))

    assert calendar.calendar_id == "CN_A_SHARE"
    assert calendar.timezone == "Asia/Shanghai"
    assert calendar.is_trading_day(date(2026, 10, 5)) is False
    assert calendar.is_trading_day(date(2026, 10, 8)) is True
    assert calendar.is_trading_day(date(2026, 10, 10)) is False


def test_calendar_fails_closed_for_unreviewed_year():
    calendar = load_trading_calendar(Path("config/calendars/a_share.yaml"))

    with pytest.raises(UnknownTradingCalendarYearError, match="2027"):
        calendar.is_trading_day(date(2027, 1, 4))


def test_calendar_rejects_dates_in_wrong_year():
    with pytest.raises(ValueError, match="do not belong"):
        TradingCalendar.model_validate(
            {
                "calendar_id": "TEST",
                "timezone": "Asia/Shanghai",
                "source": {
                    "title": "fixture",
                    "url": "https://example.test/calendar",
                    "published_on": "2025-01-01",
                },
                "years": [
                    {
                        "year": 2026,
                        "closed_dates": ["2027-01-01"],
                    }
                ],
            }
        )


def _calendar(years: list[int]) -> TradingCalendar:
    return TradingCalendar.model_validate(
        {
            "calendar_id": "TEST",
            "timezone": "Asia/Shanghai",
            "source": {"title": "t", "url": "https://example.test", "published_on": "2025-12-01"},
            "years": [{"year": year} for year in years],
        }
    )


def test_calendar_coverage_reports_end_and_days_remaining():
    from investment_agent.scheduler.trading_calendar import calendar_coverage

    official = load_trading_calendar(Path("config/calendars/a_share.yaml"))
    far = calendar_coverage(official, date(2026, 6, 1))
    near = calendar_coverage(official, date(2026, 11, 2))
    last_day = calendar_coverage(official, date(2026, 12, 31))

    assert far.coverage_end == date(2026, 12, 31)
    assert far.days_remaining == 213
    assert far.expiring_soon is False
    assert near.days_remaining == 59
    assert near.expiring_soon is True
    assert "2026-12-31" in near.message()
    assert last_day.days_remaining == 0
    assert last_day.expired is False


def test_calendar_coverage_after_expiry_and_contiguous_years():
    from investment_agent.scheduler.trading_calendar import calendar_coverage

    expired = calendar_coverage(_calendar([2026]), date(2027, 1, 5))
    assert expired.expired is True
    assert expired.expiring_soon is True
    assert expired.coverage_end is None
    assert expired.days_remaining == -5
    assert "2027" in expired.message()

    contiguous = calendar_coverage(_calendar([2026, 2027, 2029]), date(2026, 12, 1))
    assert contiguous.coverage_end == date(2027, 12, 31)
    assert contiguous.expiring_soon is False
