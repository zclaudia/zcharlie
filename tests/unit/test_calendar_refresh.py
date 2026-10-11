"""Trading-calendar refresh: build, cross-check, diff and render the YAML.

Covers the pure helpers behind ``tools/refresh_trading_calendar.py`` and the
canonical form of the committed ``config/calendars/a_share.yaml``.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from investment_agent.scheduler.calendar_refresh import (
    HEADER,
    build_year,
    days_of_year,
    diff_years,
    merge_years,
    parse_calendar_text,
    plan_refresh,
    refresh_years,
    render_calendar,
    sina_reaches_year_end,
    trading_day_disagreements,
    year_fully_answered,
)
from investment_agent.scheduler.trading_calendar import (
    TradingCalendar,
    TradingCalendarYear,
    load_trading_calendar,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
CALENDAR_PATH = REPO_ROOT / "config" / "calendars" / "a_share.yaml"

WEEKDAYS_2026 = {day for day in days_of_year(2026) if day.weekday() < 5}


def _year(
    year: int,
    *,
    closed: set[date] | None = None,
    opened: set[date] | None = None,
    verified_by: list[str] | None = None,
    verified_on: date | None = None,
) -> TradingCalendarYear:
    return TradingCalendarYear(
        year=year,
        closed_dates=closed or set(),
        open_dates=opened or set(),
        verified_by=verified_by or [],
        verified_on=verified_on,
    )


def _calendar(*years: TradingCalendarYear) -> TradingCalendar:
    return TradingCalendar.model_validate(
        {
            "calendar_id": "TEST",
            "timezone": "Asia/Shanghai",
            "source": {
                "title": "fixture",
                "url": "https://example.test/calendar",
                "published_on": "2025-12-01",
            },
            "years": [item.model_dump() for item in years],
        }
    )


def test_year_provenance_defaults_to_hand_transcribed() -> None:
    plain = TradingCalendarYear(year=2026, closed_dates={date(2026, 1, 1)})
    assert plain.verified_by == []
    assert plain.verified_on is None


def test_build_year_maps_weekday_closures_and_weekend_sessions() -> None:
    trading = {day for day in WEEKDAYS_2026 if day != date(2026, 1, 1)}
    trading.add(date(2026, 1, 3))  # a Saturday session (synthetic: exercises open_dates)

    built = build_year(2026, trading)

    assert built.closed_dates == {date(2026, 1, 1)}
    assert built.open_dates == {date(2026, 1, 3)}
    assert (built.verified_by, built.verified_on) == ([], None)


def test_build_year_rejects_days_outside_the_year() -> None:
    with pytest.raises(ValueError, match="outside 2026"):
        build_year(2026, {date(2027, 1, 4)})


def test_year_fully_answered_requires_every_day() -> None:
    answers = {day: day.weekday() < 5 for day in days_of_year(2026)}
    assert year_fully_answered(2026, answers)

    del answers[date(2026, 7, 1)]
    assert not year_fully_answered(2026, answers)
    assert not year_fully_answered(2027, {})  # nothing published yet


def test_sina_reaches_year_end_needs_the_closing_week() -> None:
    assert sina_reaches_year_end(2026, {date(2026, 12, 24)})
    assert sina_reaches_year_end(2026, {date(2026, 6, 1), date(2026, 12, 20)})
    assert not sina_reaches_year_end(2026, {date(2026, 12, 19)})
    assert not sina_reaches_year_end(2026, set())
    assert not sina_reaches_year_end(2026, {date(2025, 12, 31)})


def test_trading_day_disagreements_is_a_sorted_symmetric_difference() -> None:
    left = {date(2026, 1, 5), date(2026, 1, 6)}
    right = {date(2026, 1, 6), date(2026, 5, 4)}
    assert trading_day_disagreements(left, right) == [date(2026, 1, 5), date(2026, 5, 4)]
    assert trading_day_disagreements(left, left) == []


def test_merge_years_replaces_adds_and_orders_without_touching_the_input() -> None:
    current = _calendar(_year(2026, closed={date(2026, 1, 1)}))
    merged = merge_years(
        current,
        [
            _year(
                2027,
                closed={date(2027, 2, 8)},
                verified_by=["baostock", "akshare"],
                verified_on=date(2026, 12, 18),
            ),
            _year(
                2026,
                closed={date(2026, 1, 1), date(2026, 5, 4)},
                verified_by=["baostock", "akshare"],
                verified_on=date(2026, 10, 11),
            ),
        ],
    )

    assert [item.year for item in merged.years] == [2026, 2027]
    assert merged.years[0].closed_dates == {date(2026, 1, 1), date(2026, 5, 4)}
    assert merged.years[1].verified_by == ["baostock", "akshare"]
    assert merged.calendar_id == "TEST" and merged.source.title == "fixture"
    assert current.years[0].closed_dates == {date(2026, 1, 1)}  # input untouched


def test_diff_years_reports_dates_new_years_and_provenance() -> None:
    current = _calendar(_year(2026, closed={date(2026, 1, 1)}))
    proposed = _calendar(
        _year(
            2026,
            closed={date(2026, 5, 4)},
            verified_by=["baostock", "akshare"],
            verified_on=date(2026, 10, 11),
        ),
        _year(2027, closed={date(2027, 2, 8)}),
    )

    lines = diff_years(current, proposed)

    assert "year 2026: closed dates removed: 2026-01-01" in lines
    assert "year 2026: closed dates added: 2026-05-04" in lines
    assert "year 2026: provenance hand-transcribed -> baostock+akshare on 2026-10-11" in lines
    assert "year 2027: new (1 closed weekdays, 0 weekend sessions)" in lines
    assert diff_years(current, current) == []


def test_diff_years_truncates_long_date_lists() -> None:
    closed = {day for day in sorted(WEEKDAYS_2026)[:10]}
    lines = diff_years(_calendar(_year(2026)), _calendar(_year(2026, closed=closed)))

    assert any(line.endswith("(+2 more)") for line in lines)


def test_render_calendar_round_trips_and_stays_canonical() -> None:
    calendar = _calendar(
        _year(
            2026,
            closed={date(2026, 5, 4), date(2026, 1, 1)},
            verified_by=["baostock", "akshare"],
            verified_on=date(2026, 10, 11),
        )
    )

    text = render_calendar(calendar)

    assert text.startswith(HEADER)
    assert parse_calendar_text(text) == calendar
    assert "!!set" not in text
    closed_block = text.split("closed_dates:\n", 1)[1]
    assert closed_block.index("2026-01-01") < closed_block.index("2026-05-04")

    plain = render_calendar(_calendar(_year(2027, closed={date(2027, 2, 8)})))
    assert "verified_by" not in plain and "verified_on" not in plain


def test_refresh_years_covers_reviewed_years_plus_the_next_one() -> None:
    assert refresh_years(_calendar(_year(2026), _year(2027))) == [2026, 2027, 2028]
    assert refresh_years(_calendar(_year(2027, closed={date(2027, 2, 8)}))) == [2027, 2028]
    assert refresh_years(_calendar(), today=date(2026, 10, 11)) == [2026]


def test_plan_refresh_proposes_only_fully_answered_agreeing_years() -> None:
    trading_2026 = WEEKDAYS_2026 - {date(2026, 1, 1), date(2026, 10, 1)}
    answers = {day: day in trading_2026 for day in days_of_year(2026)}
    # 2027: baostock answers only the first half of the year.
    answers.update({day: day.weekday() < 5 for day in days_of_year(2027) if day.month <= 6})
    # 2028: baostock answers the whole year; Sina stops in June.
    trading_2028 = {day for day in days_of_year(2028) if day.weekday() < 5}
    answers.update({day: day in trading_2028 for day in days_of_year(2028)})
    # 2029: both answer the whole year but disagree about one day.
    trading_2029 = {day for day in days_of_year(2029) if day.weekday() < 5}
    answers.update({day: day in trading_2029 for day in days_of_year(2029)})
    sina = (
        set(trading_2026)
        | {day for day in trading_2028 if day.month <= 6}
        | (trading_2029 - {date(2029, 3, 1)})
    )

    plan = plan_refresh(
        _calendar(),
        years=[2026, 2027, 2028, 2029, 2030],
        baostock_answers=answers,
        sina_days=sina,
        verified_by=("baostock", "akshare"),
        verified_on=date(2026, 10, 11),
    )

    assert [item.year for item in plan.proposed.years] == [2026]
    proposed = plan.proposed.years[0]
    assert proposed.closed_dates == {date(2026, 1, 1), date(2026, 10, 1)}
    assert proposed.verified_by == ["baostock", "akshare"]
    assert proposed.verified_on == date(2026, 10, 11)

    reasons = dict(plan.skipped)
    assert "stops at 2027-06-30" in reasons[2027]
    assert "sina reaches 2028-06-30" in reasons[2028]
    assert "no answer" in reasons[2030]
    assert [year for year, _ in plan.conflicts] == [2029]
    assert plan.conflicts[0][1] == (date(2029, 3, 1),)
    assert not plan.ok
    assert "year 2026: new" in "\n".join(plan.changes)


def test_plan_refresh_is_quiet_when_the_calendar_is_current() -> None:
    trading_2026 = WEEKDAYS_2026 - {date(2026, 1, 1)}
    answers = {day: day in trading_2026 for day in days_of_year(2026)}
    current = _calendar(
        _year(
            2026,
            closed={date(2026, 1, 1)},
            verified_by=["baostock", "akshare"],
            verified_on=date(2026, 10, 11),
        )
    )

    plan = plan_refresh(
        current,
        years=[2026],
        baostock_answers=answers,
        sina_days=trading_2026,
        verified_by=("baostock", "akshare"),
        verified_on=date(2026, 10, 11),
    )

    assert plan.ok and plan.changes == ()


def test_committed_calendar_is_the_canonical_rendering() -> None:
    committed = CALENDAR_PATH.read_text(encoding="utf-8")
    assert render_calendar(load_trading_calendar(CALENDAR_PATH)) == committed


def test_committed_calendar_records_how_each_year_was_verified() -> None:
    calendar = load_trading_calendar(CALENDAR_PATH)
    assert calendar.years  # never empty
    for item in calendar.years:
        assert item.verified_by, f"year {item.year} carries no verification provenance"
        assert item.verified_on is not None
