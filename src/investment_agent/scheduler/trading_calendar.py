"""Versioned, fail-closed trading calendar for scheduled A-share research."""

from datetime import date
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, model_validator


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
