"""Accumulation week counting and trading-calendar day helpers (no DB).

The P1 exit metric ("快照连续积累 ≥ 8 周且完整率达标") is computed here from
:class:`LiveSnapshot` values the queries layer already read: pure week counting,
testable without a session. Only ``snapshots.health.queries`` supplies the
inputs.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta

from investment_agent.scheduler.trading_calendar import (
    TradingCalendar,
    is_trading_day_or_weekday,
)
from investment_agent.snapshots.health.models import (
    _WEEKS_SHOWN,
    TARGET_WEEKS,
    Accumulation,
    WeekStat,
    WeekStatus,
)

# --------------------------------------------------------------------------- #
# Trading-calendar helpers
# --------------------------------------------------------------------------- #


def is_trading_day(calendar: TradingCalendar | None, day: date) -> bool:
    """Kept for this module's callers; the rule has one home in
    :func:`~investment_agent.scheduler.trading_calendar.is_trading_day_or_weekday`."""

    return is_trading_day_or_weekday(calendar, day)


def previous_trading_day(calendar: TradingCalendar | None, day: date, *, inclusive: bool) -> date:
    current = day if inclusive else day - timedelta(days=1)
    for _ in range(60):
        if is_trading_day(calendar, current):
            return current
        current -= timedelta(days=1)
    return current


def trading_days_between(
    calendar: TradingCalendar | None, after: date, until: date
) -> int:
    """Trading days in ``(after, until]``."""

    count = 0
    current = after + timedelta(days=1)
    while current <= until:
        if is_trading_day(calendar, current):
            count += 1
        current += timedelta(days=1)
    return count


def _week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


def _week_label(start: date) -> str:
    year, week, _ = start.isocalendar()
    return f"{year}-W{week:02d}"


# --------------------------------------------------------------------------- #
# Accumulation
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class LiveSnapshot:
    trade_date: date
    ratio: float


def compute_accumulation(
    snapshots: Iterable[LiveSnapshot],
    *,
    universe_code: str,
    today: date,
    threshold: float,
    calendar: TradingCalendar | None,
    target_weeks: int = TARGET_WEEKS,
) -> Accumulation:
    """Week counting for the P1 exit metric (see the module docstring)."""

    best: dict[date, float] = {}
    for item in snapshots:
        if item.trade_date > today:
            continue
        best[item.trade_date] = max(best.get(item.trade_date, 0.0), item.ratio)
    if not best:
        return Accumulation(
            universe_code=universe_code,
            target_weeks=target_weeks,
            threshold=threshold,
            streak_weeks=0,
            weeks_with_snapshots=0,
            weeks_meeting_threshold=0,
            first_live_trade_date=None,
            latest_live_trade_date=None,
            reached=False,
            weeks=[],
        )
    by_week: dict[date, list[float]] = {}
    for day, ratio in best.items():
        by_week.setdefault(_week_start(day), []).append(ratio)
    first = min(best)
    current_week = _week_start(today)
    weeks: list[WeekStat] = []
    streak = 0
    meeting = 0
    start = _week_start(first)
    while start <= current_week:
        ratios = by_week.get(start, [])
        trading = sum(
            1 for offset in range(7) if is_trading_day(calendar, start + timedelta(days=offset))
        )
        min_ratio = round(min(ratios), 4) if ratios else None
        status: WeekStatus
        if ratios and min_ratio is not None and min_ratio >= threshold:
            status = "qualified"
            streak += 1
            meeting += 1
        elif start == current_week:
            status = "in_progress"
        elif not ratios and trading == 0:
            status = "no_trading"
        else:
            status = "below_threshold" if ratios else "missing"
            streak = 0
        weeks.append(
            WeekStat(
                week=_week_label(start),
                start=start,
                trading_days=trading,
                collected_days=len(ratios),
                min_ratio=min_ratio,
                status=status,
            )
        )
        start += timedelta(days=7)
    return Accumulation(
        universe_code=universe_code,
        target_weeks=target_weeks,
        threshold=threshold,
        streak_weeks=streak,
        weeks_with_snapshots=len(by_week),
        weeks_meeting_threshold=meeting,
        first_live_trade_date=first,
        latest_live_trade_date=max(best),
        reached=streak >= target_weeks,
        weeks=list(reversed(weeks))[:_WEEKS_SHOWN],
    )
