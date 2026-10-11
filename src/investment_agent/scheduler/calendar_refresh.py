"""Trading-calendar maintenance: build, cross-check, diff and render the YAML.

``config/calendars/a_share.yaml`` stays a reviewed artifact - the scheduler
gates on it and stops (fail-closed) for an unreviewed year - but it is no
longer transcribed by hand: this module turns two independent provider answers
(baostock ``query_trade_dates`` and Sina's calendar via AkShare) into a
candidate calendar with per-year provenance. A year is proposed only when both
sources answer every day of it and agree on every trading day, so the annual
refresh (docs/product/operations.md, "年度 / 不定期维护") is a diff review
instead of a data-entry task.

Nothing here writes files or calls a provider: the pure functions take the
answers as arguments, and ``tools/refresh_trading_calendar.py`` is the CLI
that fetches and commits them.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import yaml

from investment_agent.scheduler.trading_calendar import TradingCalendar, TradingCalendarYear

HEADER = """\
# A-share trading calendar. Regenerate with tools/refresh_trading_calendar.py
# (baostock and AkShare/Sina must agree day by day); do not edit years by hand.
# closed_dates lists weekday closures only; Saturdays and Sundays are closed by
# rule. open_dates lists weekend trading days (none in recent years).
"""

SINA_YEAR_END_DAY = (12, 20)
"""A complete Sina year always reaches its closing week.

Sina lists trading days only, so an unreached month means the year is not
published yet (the exchanges publish the holiday schedule around December);
one week of margin absorbs a year-end holiday run.
"""

MAX_DATES_REPORTED = 8

VERIFIED_BY: tuple[str, ...] = ("baostock", "akshare")
"""Provenance recorded for a year both providers agreed on (akshare = Sina's table)."""


def days_of_year(year: int) -> list[date]:
    day = date(year, 1, 1)
    days: list[date] = []
    while day.year == year:
        days.append(day)
        day += timedelta(days=1)
    return days


def build_year(year: int, trading_days: Collection[date]) -> TradingCalendarYear:
    """One calendar year from its trading days: weekday closures + weekend sessions."""

    trading = set(trading_days)
    outside = sorted(day for day in trading if day.year != year)
    if outside:
        raise ValueError(f"trading days outside {year}: {[day.isoformat() for day in outside[:3]]}")
    return TradingCalendarYear(
        year=year,
        closed_dates={d for d in days_of_year(year) if d.weekday() < 5 and d not in trading},
        open_dates={d for d in days_of_year(year) if d.weekday() >= 5 and d in trading},
    )


def year_fully_answered(year: int, answers: Mapping[date, bool]) -> bool:
    """Whether a day-by-day answer (baostock) covers every day of ``year``."""

    return all(day in answers for day in days_of_year(year))


def sina_reaches_year_end(year: int, days: Collection[date]) -> bool:
    """Whether Sina's calendar reaches the closing week of ``year``."""

    in_year = [day for day in days if day.year == year]
    return bool(in_year) and max(in_year) >= date(year, *SINA_YEAR_END_DAY)


def trading_day_disagreements(left: Collection[date], right: Collection[date]) -> list[date]:
    """Days where two trading-day answers disagree (symmetric difference, sorted)."""

    return sorted(set(left) ^ set(right))


def merge_years(
    calendar: TradingCalendar, replacements: Sequence[TradingCalendarYear]
) -> TradingCalendar:
    """``calendar`` with ``replacements`` placed by year, years ordered ascending."""

    merged = {item.year: item for item in calendar.years}
    for item in replacements:
        merged[item.year] = item
    return calendar.model_copy(update={"years": [merged[year] for year in sorted(merged)]})


def diff_years(current: TradingCalendar, proposed: TradingCalendar) -> list[str]:
    """Reviewable change lines between two calendars (dates and provenance)."""

    lines: list[str] = []
    before = {item.year: item for item in current.years}
    after = {item.year: item for item in proposed.years}
    for year in sorted(after.keys() - before.keys()):
        item = after[year]
        lines.append(
            f"year {year}: new ({len(item.closed_dates)} closed weekdays, "
            f"{len(item.open_dates)} weekend sessions)"
        )
    for year in sorted(before.keys() - after.keys()):
        lines.append(f"year {year}: removed")
    for year in sorted(before.keys() & after.keys()):
        old, new = before[year], after[year]
        sections = (
            ("closed", old.closed_dates - new.closed_dates, new.closed_dates - old.closed_dates),
            ("open", old.open_dates - new.open_dates, new.open_dates - old.open_dates),
        )
        for label, removed, added in sections:
            if removed:
                lines.append(f"year {year}: {label} dates removed: {_dates(removed)}")
            if added:
                lines.append(f"year {year}: {label} dates added: {_dates(added)}")
        if (old.verified_by, old.verified_on) != (new.verified_by, new.verified_on):
            lines.append(f"year {year}: provenance {_provenance(old)} -> {_provenance(new)}")
    return lines


def _dates(values: Collection[date]) -> str:
    shown = ", ".join(day.isoformat() for day in sorted(values)[:MAX_DATES_REPORTED])
    extra = len(values) - MAX_DATES_REPORTED
    return f"{shown} (+{extra} more)" if extra > 0 else shown


def _provenance(item: TradingCalendarYear) -> str:
    if not item.verified_by:
        return "hand-transcribed"
    return f"{'+'.join(item.verified_by)} on {item.verified_on}"


def render_calendar(calendar: TradingCalendar) -> str:
    """The canonical file text: header comment plus deterministic YAML."""

    body = yaml.safe_dump(
        calendar_document(calendar),
        sort_keys=False,
        allow_unicode=True,
        indent=2,
        default_flow_style=False,
        width=88,
    )
    return HEADER + body


def calendar_document(calendar: TradingCalendar) -> dict[str, Any]:
    years: list[dict[str, Any]] = []
    for item in sorted(calendar.years, key=lambda entry: entry.year):
        entry: dict[str, Any] = {
            "year": item.year,
            "closed_dates": sorted(item.closed_dates),
            "open_dates": sorted(item.open_dates),
        }
        if item.verified_by:
            entry["verified_by"] = list(item.verified_by)
        if item.verified_on is not None:
            entry["verified_on"] = item.verified_on
        years.append(entry)
    return {
        "calendar_id": calendar.calendar_id,
        "timezone": calendar.timezone,
        "source": {
            "title": calendar.source.title,
            "url": calendar.source.url,
            "published_on": calendar.source.published_on,
        },
        "years": years,
    }


def parse_calendar_text(text: str) -> TradingCalendar:
    """Parse rendered or committed calendar text (the render check on write)."""

    return TradingCalendar.model_validate(yaml.safe_load(text))


def refresh_years(calendar: TradingCalendar, *, today: date | None = None) -> list[int]:
    """Years to check by default: the reviewed ones plus the next one."""

    covered = sorted(item.year for item in calendar.years)
    if not covered:
        return [(today or date.today()).year]
    return sorted({*covered, covered[-1] + 1})


@dataclass(frozen=True)
class RefreshPlan:
    """What one refresh run proposes, without touching the file."""

    current: TradingCalendar
    proposed: TradingCalendar
    changes: tuple[str, ...]
    """Reviewable diff lines; empty = nothing to write."""
    skipped: tuple[tuple[int, str], ...]
    """Years left alone as ``(year, reason)`` (e.g. schedule not published yet)."""
    conflicts: tuple[tuple[int, tuple[date, ...]], ...]
    """Years where the two sources disagree as ``(year, first disagreeing days)``."""

    @property
    def ok(self) -> bool:
        return not self.conflicts


def plan_refresh(
    current: TradingCalendar,
    *,
    years: Sequence[int],
    baostock_answers: Mapping[date, bool],
    sina_days: Collection[date],
    verified_by: Sequence[str],
    verified_on: date,
) -> RefreshPlan:
    """Cross-check both sources and build the candidate calendar.

    A year is proposed only when baostock answers every day of it, Sina reaches
    its closing week, and the two agree on every trading day. Otherwise the
    year is skipped (not published yet / partial answer) or reported as a
    conflict (disagreeing sources) - never written.
    """

    replacements: list[TradingCalendarYear] = []
    skipped: list[tuple[int, str]] = []
    conflicts: list[tuple[int, tuple[date, ...]]] = []
    for year in sorted(set(years)):
        in_year = {day: flag for day, flag in baostock_answers.items() if day.year == year}
        if not in_year:
            skipped.append((year, "baostock has no answer yet (schedule not published)"))
            continue
        if not year_fully_answered(year, in_year):
            skipped.append((year, f"baostock answer stops at {max(in_year).isoformat()}"))
            continue
        sina_year = {day for day in sina_days if day.year == year}
        if not sina_reaches_year_end(year, sina_year):
            reached = max(sina_year).isoformat() if sina_year else "nothing"
            skipped.append((year, f"sina reaches {reached} (schedule not published)"))
            continue
        baostock_trading = {day for day, flag in in_year.items() if flag}
        disagreements = trading_day_disagreements(baostock_trading, sina_year)
        if disagreements:
            conflicts.append((year, tuple(disagreements[:10])))
            continue
        proposed = build_year(year, baostock_trading)
        replacements.append(
            proposed.model_copy(
                update={"verified_by": list(verified_by), "verified_on": verified_on}
            )
        )
    proposed_calendar = merge_years(current, replacements)
    return RefreshPlan(
        current=current,
        proposed=proposed_calendar,
        changes=tuple(diff_years(current, proposed_calendar)),
        skipped=tuple(skipped),
        conflicts=tuple(conflicts),
    )
