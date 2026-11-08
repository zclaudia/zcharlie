"""Market session windows and benchmark construction (pure).

Session windows are *not* derived from any single stock's bars: they are the
sorted union of stored market sessions. A suspended stock therefore does not
shorten the horizon for everybody else. The report records this construction
and whether it was complete for the requested window.

The equal-weight benchmark reuses the same tradable, cost-aware outcome as
every member: an observation enters at T+1 on its own adjusted open, exits
after the horizon's session count, and pays the same round-trip cost. The
benchmark mean is subtracted from each member's net return, so a suspension of
one stock does not silently distort the comparison.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import date
from typing import Any

from investment_agent.research.forward_validation import prices, stats
from investment_agent.research.forward_validation.contracts import (
    HORIZON_SESSIONS,
    CostModel,
    PriceSeries,
)
from investment_agent.research.forward_validation.evaluation import Benchmark


def market_sessions(dates: Iterable[date]) -> list[date]:
    return sorted(set(dates))


def coverage_window(
    sessions: Sequence[date],
    *,
    starts: Sequence[date],
    horizon_months: Sequence[int],
) -> dict[str, Any]:
    """Whether the stored calendar reaches the exit of every requested window."""

    if not sessions:
        return {"complete": False, "reason": "no stored market sessions"}
    gaps: list[dict[str, Any]] = []
    complete = True
    session_list = list(sessions)
    for start in starts:
        for months in horizon_months:
            entry_index = prices.session_on_or_after(session_list, start)
            if entry_index is None:
                complete = False
                gaps.append({"as_of": start.isoformat(), "horizon": months, "reason": "no_entry"})
                continue
            target = entry_index + 1 + HORIZON_SESSIONS[months]
            if target >= len(sessions):
                complete = False
                gaps.append(
                    {"as_of": start.isoformat(), "horizon": months, "reason": "beyond_calendar"}
                )
    first = sessions[0]
    last = sessions[-1]
    # A stored calendar that jumps more than a long holiday is a coverage gap,
    # not a market closure: 20 calendar days covers Spring Festival + weekends.
    large_gaps = [
        {
            "after": sessions[index].isoformat(),
            "before": sessions[index + 1].isoformat(),
            "calendar_days": (sessions[index + 1] - sessions[index]).days,
        }
        for index in range(len(sessions) - 1)
        if (sessions[index + 1] - sessions[index]).days > 20
    ]
    return {
        "complete": complete,
        "first_session": first.isoformat(),
        "last_session": last.isoformat(),
        "session_count": len(sessions),
        "starts": [start.isoformat() for start in starts],
        "horizons": list(horizon_months),
        "gaps": gaps,
        "large_gaps": large_gaps,
        "note": (
            "sessions are the sorted union of stored market bars; a stock's own "
            "suspension does not shorten its horizon"
        ),
    }


def equal_weight_benchmark(
    *,
    sessions: Sequence[date],
    as_of_dates: Sequence[date],
    series: Mapping[str, PriceSeries],
    members_by_as_of: Mapping[date, Sequence[str]],
    cost: CostModel,
    horizons: Sequence[int],
    max_entry_deferral: int,
    max_exit_deferral: int,
) -> tuple[Benchmark, dict[str, Any]]:
    """Mean net return of **that date's** constituent series over each window.

    Using the constituent set of each ``as_of`` (not a union across dates)
    keeps the benchmark comparable to the cross-section it is subtracted from.
    """

    session_list = list(sessions)
    by_horizon: dict[int, dict[date, float | None]] = {months: {} for months in horizons}
    detail: dict[str, Any] = {"windows": {}}
    for as_of in as_of_dates:
        detail["windows"][as_of.isoformat()] = {}
        tickers = sorted(set(members_by_as_of.get(as_of, ())))
        for months in horizons:
            returns: list[float] = []
            entry_dates: list[date] = []
            exit_dates: list[date] = []
            for ticker in tickers:
                if ticker not in series:
                    continue
                outcome = prices.compute_outcome(
                    series[ticker],
                    session_list,
                    as_of,
                    months,
                    cost,
                    max_entry_deferral=max_entry_deferral,
                    max_exit_deferral=max_exit_deferral,
                )
                if outcome.status == "ok" and outcome.net_return is not None:
                    returns.append(outcome.net_return)
                    if outcome.entry_date is not None:
                        entry_dates.append(outcome.entry_date)
                    if outcome.exit_date is not None:
                        exit_dates.append(outcome.exit_date)
            by_horizon[months][as_of] = stats.safe_mean(returns)
            detail["windows"][as_of.isoformat()][str(months)] = {
                "constituents": len(tickers),
                "priced_members": len(returns),
                "entry_date": max(entry_dates).isoformat() if entry_dates else None,
                "exit_date": max(exit_dates).isoformat() if exit_dates else None,
            }
    benchmark = Benchmark(
        name="equal_weight",
        by_horizon=by_horizon,
        construction=(
            "mean net return of that as_of's constituents with a tradable T+1 "
            "entry, same adjusted series, session count and cost model as the members"
        ),
        note=(
            "computed from stored bars; not a published index. Members of the "
            "cross-section that could not be priced are excluded from the mean"
        ),
    )
    return benchmark, detail


def empty_benchmark(name: str, reason: str) -> Benchmark:
    """An explicit, unavailable benchmark instead of a silent zero."""

    return Benchmark(
        name=name,
        by_horizon={},
        construction="unavailable",
        note=reason,
    )


__all__ = [
    "coverage_window",
    "empty_benchmark",
    "equal_weight_benchmark",
    "market_sessions",
]
