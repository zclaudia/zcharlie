"""Valuation percentile factors (P1-B2).

``pe_percentile_5y`` / ``pb_percentile_5y`` place the latest baostock daily
``pe_ttm`` / ``pb_mrq`` (latest session on or before ``as_of``) within its own
distribution over the trailing five years.

Definition (point in time - only sessions with ``trade_date <= as_of``):

* window: sessions in ``(as_of - 5 years, as_of]``;
* distribution: the window's sessions with a positive value. Loss-making
  days (``pe_ttm <= 0``; ``pb_mrq <= 0`` for negative book value) and
  missing values are excluded, consistent with the v1.3 negative-PE rule;
* percentile = (number of distribution values <= current) / (distribution
  size), inclusive, so it lies in ``(0, 1]`` and the cheapest session of the
  window scores ``1/n``; rounded to 6 dp;
* current value <= 0 -> no value, flag ``negative_earnings`` (PE) /
  ``negative_book_value`` (PB);
* fewer than :data:`MIN_VALID_SESSIONS` valid sessions (about one year) ->
  ``missing`` with ``short_history``;
* valid history starting more than :data:`COVERAGE_TOLERANCE_DAYS` after the
  window start -> ``approx`` with ``short_history``; ``inputs_ref`` records
  ``years_covered``;
* ``report_overdue`` is carried when the listing has an open overdue report.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import Any

from investment_agent.factors.library import FLAG_NO_PRICE, latest_bar
from investment_agent.factors.registry import (
    FLAG_NEGATIVE_BOOK_VALUE,
    FLAG_NEGATIVE_EARNINGS,
    FLAG_SHORT_HISTORY,
    Factor,
    FactorInput,
    Partial,
)
from investment_agent.factors.snapshot_model import SnapshotView

PERCENTILE_YEARS = 5
MIN_VALID_SESSIONS = 240
COVERAGE_TOLERANCE_DAYS = 31


def years_before(day: date, years: int) -> date:
    """Same calendar day ``years`` earlier (29 February -> 28 February)."""

    try:
        return day.replace(year=day.year - years)
    except ValueError:
        return day.replace(year=day.year - years, day=28)


def inclusive_percentile(values: list[float], current: float) -> float:
    """Fraction of ``values`` that are ``<= current``."""

    if not values:
        raise ValueError("percentile of an empty distribution")
    return round(sum(1 for value in values if value <= current) / len(values), 6)


def _percentile(column: str, non_positive_flag: str) -> Callable[[SnapshotView], Partial]:
    def body(view: SnapshotView) -> Partial:
        bar, flags = latest_bar(view)
        if bar is None:
            return Partial(None, flags=(FLAG_NO_PRICE,))
        window_start = years_before(view.as_of, PERCENTILE_YEARS)
        window = [day for day in view.raw_bars if window_start < day.trade_date <= view.as_of]
        valid = [
            (day.trade_date, value)
            for day in window
            if (value := getattr(day, column)) is not None and value > 0
        ]
        current = getattr(bar, column)
        ref: dict[str, Any] = {
            "column": column,
            "current": current,
            "current_date": bar.trade_date.isoformat(),
            "window": [window_start.isoformat(), view.as_of.isoformat()],
            "sessions": len(window),
            "valid_sessions": len(valid),
            "excluded_non_positive": sum(
                1 for day in window if (v := getattr(day, column)) is not None and v <= 0
            ),
        }
        if valid:
            first = valid[0][0]
            ref["first_valid"] = first.isoformat()
            ref["years_covered"] = round((view.as_of - first).days / 365.25, 2)
        if current is None:
            return Partial(None, flags=flags, inputs_ref=ref)
        if current <= 0:
            return Partial(None, flags=(non_positive_flag, *flags), inputs_ref=ref)
        if len(valid) < MIN_VALID_SESSIONS:
            return Partial(None, flags=(FLAG_SHORT_HISTORY, *flags), inputs_ref=ref)
        value = inclusive_percentile([v for _, v in valid], current)
        short = (valid[0][0] - window_start).days > COVERAGE_TOLERANCE_DAYS
        result_flags = ((FLAG_SHORT_HISTORY,) if short else ()) + flags
        return Partial(
            value,
            quality="approx" if result_flags else None,
            flags=result_flags,
            inputs_ref=ref,
        )

    return body


_SOURCE = "market_snapshot(adjust=none, provider=baostock)"

VALUATION_FACTORS: tuple[Factor, ...] = (
    Factor(
        "pe_percentile_5y",
        "1",
        "fraction (0,1]",
        "Inclusive percentile of the latest daily PE TTM within its trailing 5-year "
        "distribution of positive values; missing when PE <= 0 or under ~1y of history.",
        (FactorInput("baostock", "pe_ttm", f"{_SOURCE}.pe_ttm"),),
        _percentile("pe_ttm", FLAG_NEGATIVE_EARNINGS),
        carries_overdue_flag=True,
    ),
    Factor(
        "pb_percentile_5y",
        "1",
        "fraction (0,1]",
        "Inclusive percentile of the latest daily PB MRQ within its trailing 5-year "
        "distribution of positive values; missing when PB <= 0 or under ~1y of history.",
        (FactorInput("baostock", "pb_mrq", f"{_SOURCE}.pb_mrq"),),
        _percentile("pb_mrq", FLAG_NEGATIVE_BOOK_VALUE),
        carries_overdue_flag=True,
    ),
)
