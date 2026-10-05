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
