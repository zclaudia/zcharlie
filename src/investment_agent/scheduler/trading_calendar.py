"""Versioned, fail-closed trading calendar for scheduled A-share research."""

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, model_validator

# Warn this many days before the reviewed calendar runs out. Exchanges usually
# publish next year's holiday schedule in December, so 60 days leaves time to
# review and add it before scheduled refreshes silently stop.
CALENDAR_EXPIRY_WARNING_DAYS = 60


class UnknownTradingCalendarYearError(ValueError):
    """Raised when scheduling would rely on an unreviewed calendar year."""


class TradingCalendarSource(BaseModel):
    title: str
    url: str
    published_on: date


class TradingCalendarYear(BaseModel):
    year: int = Field(ge=2000, le=2200)
    closed_dates: set[date] = Field(default_factory=set)
    open_dates: set[date] = Field(default_factory=set)

    @model_validator(mode="after")
    def validate_dates_belong_to_year(self) -> "TradingCalendarYear":
        mismatched = {
            value
            for value in self.closed_dates | self.open_dates
            if value.year != self.year
        }
        if mismatched:
            raise ValueError(
                f"calendar dates do not belong to {self.year}: {sorted(mismatched)}"
            )
        overlap = self.closed_dates & self.open_dates
        if overlap:
            raise ValueError(f"calendar dates cannot be both open and closed: {overlap}")
        return self


class TradingCalendar(BaseModel):
    calendar_id: str
    timezone: str
    source: TradingCalendarSource
    years: list[TradingCalendarYear]

    @model_validator(mode="after")
    def validate_unique_years(self) -> "TradingCalendar":
        values = [item.year for item in self.years]
        if len(values) != len(set(values)):
            raise ValueError("calendar years must be unique")
        return self

    def coverage_end(self, today: date) -> date | None:
        """Last date of the contiguous reviewed years starting at ``today``.

        Returns None when ``today``'s own year is not reviewed (already expired).
        """
        covered = {item.year for item in self.years}
        if today.year not in covered:
            return None
        year = today.year
        while year + 1 in covered:
            year += 1
        return date(year, 12, 31)

    def is_trading_day(self, value: date) -> bool:
        year = next((item for item in self.years if item.year == value.year), None)
        if year is None:
            raise UnknownTradingCalendarYearError(
                f"{self.calendar_id} has no reviewed calendar for {value.year}"
            )
        if value in year.open_dates:
            return True
        if value.weekday() >= 5:
            return False
        return value not in year.closed_dates


def load_trading_calendar(path: Path) -> TradingCalendar:
    with path.open(encoding="utf-8") as handle:
        return TradingCalendar.model_validate(yaml.safe_load(handle))


@dataclass(frozen=True)
class CalendarCoverage:
    """How long the reviewed trading calendar keeps scheduled refreshes alive."""

    calendar_id: str
    today: date
    coverage_end: date | None
    days_remaining: int
    warning_days: int = CALENDAR_EXPIRY_WARNING_DAYS

    @property
    def expired(self) -> bool:
        return self.coverage_end is None

    @property
    def expiring_soon(self) -> bool:
        return self.expired or self.days_remaining <= self.warning_days

    def message(self) -> str:
        if self.coverage_end is None:
            return (
                f"交易日历 {self.calendar_id} 未覆盖 {self.today.year} 年，"
                "自选股定时刷新已停止；请在官方公布休市安排后补充日历。"
            )
        return (
            f"交易日历 {self.calendar_id} 仅覆盖至 {self.coverage_end.isoformat()}"
            f"（剩余 {self.days_remaining} 天），请在官方公布休市安排后补充下一年度。"
        )


def calendar_coverage(
    calendar: TradingCalendar,
    today: date,
    *,
    warning_days: int = CALENDAR_EXPIRY_WARNING_DAYS,
) -> CalendarCoverage:
    """Report the calendar's coverage end and days remaining as of ``today``."""

    end = calendar.coverage_end(today)
    if end is not None:
        days_remaining = (end - today).days
    else:
        # Expired: negative days since the last reviewed year ended (or -1).
        past_years = [item.year for item in calendar.years if item.year < today.year]
        days_remaining = (
            (date(max(past_years), 12, 31) - today).days if past_years else -1
        )
    return CalendarCoverage(
        calendar_id=calendar.calendar_id,
        today=today,
        coverage_end=end,
        days_remaining=days_remaining,
        warning_days=warning_days,
    )
