"""Tradable entry/exit and cost-aware returns (pure).

Rules implemented here are deliberately conservative and explicit:

* **Entry is T+1**: the first market session after the signal session. A
  suspended stock, or one that opens at its limit-up price, is deferred one
  session at a time up to ``max_entry_deferral`` and then reported
  ``entry_untradable``. The entry price is that session's **open**.
* **Exit** is the close of the session ``HORIZON_SESSIONS[months]`` after the
  entry session, taken from the market session list (not the stock's bars), so
  a suspension does not shorten the window. A suspended exit session (or one
  closing limit-down) defers the sale forward, up to ``max_exit_deferral``.
* **Returns use the stored backward-adjusted series.** The limit check uses
  the **raw** series because exchange limits are computed on raw prices.
* **Costs** are applied as ``buy_factor`` / ``sell_factor``; the net return is
  the only return used for bucket, IC and hit-rate statistics.

Nothing here reads a clock or a database.
"""

from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from investment_agent.research.forward_validation.contracts import (
    HORIZON_SESSIONS,
    CostModel,
    HorizonOutcome,
    PriceSeries,
    Tradability,
)

#: Tolerance on a limit comparison (one half tick).
LIMIT_TOLERANCE = 0.0051


def price_limit_ratio(ticker: str, *, is_st: bool | None) -> float:
    """Daily price-limit ratio for an A-share ticker.

    Main board 10 %; ChiNext (300/301) and STAR (688/689) 20 %; Beijing 30 %.
    ST is 5 % on the main board but keeps 20 %/30 % on ChiNext/STAR/BSE.
    """

    symbol, _, market = ticker.partition(".")
    symbol = symbol.strip()
    if market.upper() == "BJ":
        return 0.30
    growth_board = symbol.startswith(("300", "301", "688", "689"))
    if is_st and not growth_board:
        return 0.05
    return 0.20 if growth_board else 0.10


def _tick(value: float) -> float:
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _previous_close(close: dict[date, float], day: date, sessions: list[date] | None) -> float | None:
    """Raw close that sets ``day``'s price limit.

    The exchange computes the limit from the stock's own previous close. On a
    normal day that is the previous market session; after a long suspension the
    stock has no bar on that session, so the reference is the stock's **last
    actual raw close before ``day``** (the resumption reference price). Looking
    only at the previous market session would return ``None`` on a resumption
    day and silently disable the limit check (``limit_unknown``), which would
    let a resumption-at-limit order "fill".
    """

    if sessions is not None:
        index = _session_index(sessions, day)
        if index is not None and index > 0:
            previous = close.get(sessions[index - 1])
            if previous is not None and previous > 0:
                return previous
    earlier = [key for key in close if key < day and close[key] is not None and close[key] > 0]
    return close[max(earlier)] if earlier else None


def _session_index(sessions: list[date], day: date) -> int | None:
    """Index of ``day`` in the ascending session list (``None`` if absent)."""

    low, high = 0, len(sessions) - 1
    while low <= high:
        middle = (low + high) // 2
        if sessions[middle] == day:
            return middle
        if sessions[middle] < day:
            low = middle + 1
        else:
            high = middle - 1
    return None


def session_on_or_after(sessions: list[date], day: date) -> int | None:
    """Index of the first session ``>= day``."""

    for index, value in enumerate(sessions):
        if value >= day:
            return index
    return None


def session_on_or_before(sessions: list[date], day: date) -> int | None:
    """Index of the last session ``<= day``."""

    found: int | None = None
    for index, value in enumerate(sessions):
        if value <= day:
            found = index
        else:
            break
    return found


def _bar_status(series: PriceSeries, day: date) -> str:
    """``missing_bar`` / ``suspended`` / ``trading`` for one session.

    A zero-volume bar is never tradable, whatever ``trade_status`` claims: the
    docs promise "volume 0 = untradable", and trusting ``status == 1`` there
    would turn a data inconsistency into a fill.
    """

    if day not in series.raw_close and day not in series.adjusted_close:
        return "missing_bar"
    status = series.trade_status.get(day)
    volume = series.volume.get(day)
    if status == 0 or (volume is not None and volume == 0):
        return "suspended"
    return "trading"


def entry_tradability(
    series: PriceSeries,
    sessions: list[date],
    day: date,
    *,
    side: str = "buy",
) -> Tradability:
    """Whether ``day`` can be an entry session and why not.

    ``buy`` blocks on suspension and limit-up open; ``sell`` blocks on
    suspension and limit-down open. The limit reference is the stock's last
    actual raw close before ``day`` (see :func:`_previous_close`), so a
    resumption day after a long suspension is still limit-checked. Only a stock
    with no prior close at all (a first listing) gets ``limit_unknown``: the
    limit is unknowable and the bar stays tradable.
    """

    status = _bar_status(series, day)
    if status == "missing_bar":
        return Tradability(False, "missing_bar")
    if status == "suspended":
        return Tradability(False, "suspended")
    raw_open = series.raw_open.get(day)
    raw_close = series.raw_close.get(day)
    if raw_open is None or raw_close is None:
        return Tradability(False, "missing_bar")
    previous = _previous_close(series.raw_close, day, sessions)
    if previous is None or previous <= 0:
        return Tradability(True, None, ("limit_unknown",))
    ratio = price_limit_ratio(series.ticker, is_st=series.is_st.get(day))
    # Limit prices are rounded to 0.01; a bar within half a tick of the
    # computed limit counts as "at the limit".
    upper = _tick(previous * (1 + ratio)) - LIMIT_TOLERANCE
    lower = _tick(previous * (1 - ratio)) + LIMIT_TOLERANCE
    if side == "buy" and raw_open >= upper:
        return Tradability(False, "limit_up")
    if side == "sell" and raw_open <= lower:
        return Tradability(False, "limit_down")
    return Tradability(True, None)


def exit_tradability(series: PriceSeries, sessions: list[date], day: date) -> Tradability:
    """Exit uses the close: block on suspension or a close at limit-down.

    A close *at* the limit-down price means a seller could not reliably sell
    at that close, so the sale defers. Limit-up closes are fine for a sell.
    """

    status = _bar_status(series, day)
    if status == "missing_bar":
        return Tradability(False, "missing_bar")
    if status == "suspended":
        return Tradability(False, "suspended")
    raw_close = series.raw_close.get(day)
    if raw_close is None:
        return Tradability(False, "missing_bar")
    previous = _previous_close(series.raw_close, day, sessions)
    if previous is None or previous <= 0:
        return Tradability(True, None, ("limit_unknown",))
    ratio = price_limit_ratio(series.ticker, is_st=series.is_st.get(day))
    lower = _tick(previous * (1 - ratio)) + LIMIT_TOLERANCE
    if raw_close <= lower:
        return Tradability(False, "limit_down")
    return Tradability(True, None)


def resolve_entry(
    series: PriceSeries,
    sessions: list[date],
    signal_session: date,
    *,
    max_deferral: int,
) -> tuple[date | None, int, Tradability]:
    """First tradable buy session strictly after ``signal_session``.

    Returns ``(entry_date, deferral_sessions, last_tradability)``.
    """

    start = session_on_or_after(sessions, signal_session)
    if start is None:
        return None, 0, Tradability(False, "no_signal_session")
    index = start + 1
    deferral = 0
    last = Tradability(False, "no_next_session")
    while index < len(sessions):
        candidate = sessions[index]
        result = entry_tradability(series, sessions, candidate)
        if result.tradable:
            return candidate, deferral, result
        last = result
        if deferral >= max_deferral:
            break
        deferral += 1
        index += 1
    return None, deferral, last


def resolve_exit(
    series: PriceSeries,
    sessions: list[date],
    entry_session: date,
    months: int,
    *,
    max_deferral: int,
) -> tuple[date | None, int, Tradability, str | None]:
    """Close session for a horizon, deferring past suspension/limit-down.

    Returns ``(exit_date, deferral_sessions, last_tradability, pending_reason)``
    where ``pending_reason`` is ``pending_horizon`` when the stored market
    calendar ends before the horizon matures.
    """

    entry_index = _session_index(sessions, entry_session)
    if entry_index is None:
        return None, 0, Tradability(False, "no_entry_session"), "no_entry_session"
    target = entry_index + HORIZON_SESSIONS[months]
    if target >= len(sessions):
        return None, 0, Tradability(False, "pending_horizon"), "pending_horizon"
    deferral = 0
    index = target
    last = Tradability(False, "pending_horizon")
    while index < len(sessions):
        candidate = sessions[index]
        result = exit_tradability(series, sessions, candidate)
        if result.tradable:
            return candidate, deferral, result, None
        last = result
        if deferral >= max_deferral:
            break
        deferral += 1
        index += 1
    if index >= len(sessions) and last.reason in {"missing_bar", "suspended"}:
        return None, deferral, last, "pending_horizon"
    return None, deferral, last, f"exit_{last.reason}"


def net_return(gross: float, cost: CostModel) -> float:
    """Gross price return -> net of the explicit round-trip cost assumption.

    The buyer pays ``buy_factor`` on the way in and receives ``sell_factor`` on
    the way out, so costs always *reduce* the return.
    """

    return (1.0 + gross) * cost.sell_factor / cost.buy_factor - 1.0


def compute_outcome(
    series: PriceSeries,
    sessions: list[date],
    signal_session: date,
    months: int,
    cost: CostModel,
    *,
    max_entry_deferral: int,
    max_exit_deferral: int,
) -> HorizonOutcome:
    """One horizon's tradable, cost-aware outcome for one listing."""

    entry, entry_deferral, entry_state = resolve_entry(
        series, sessions, signal_session, max_deferral=max_entry_deferral
    )
    if entry is None:
        return HorizonOutcome(
            months=months,
            status="entry_untradable",
            reason=entry_state.reason or "entry_untradable",
            entry_deferral_sessions=entry_deferral,
            flags=entry_state.flags,
        )
    exit_day, exit_deferral, exit_state, pending = resolve_exit(
        series, sessions, entry, months, max_deferral=max_exit_deferral
    )
    flags = (*entry_state.flags, *exit_state.flags)
    if exit_day is None:
        # Deferral exhausted for any blocking reason (suspended / missing bar /
        # limit-down) is an untradable exit, not a new status. The concrete
        # reason stays on the outcome so the report can still distinguish it.
        status = "pending_horizon" if pending == "pending_horizon" else "exit_untradable"
        return HorizonOutcome(
            months=months,
            status=status,
            reason=exit_state.reason,
            entry_date=entry,
            entry_deferral_sessions=entry_deferral,
            exit_deferral_sessions=exit_deferral,
            flags=flags,
        )
    entry_price = series.adjusted_open.get(entry) or series.raw_open.get(entry)
    exit_price = series.adjusted_close.get(exit_day) or series.raw_close.get(exit_day)
    signal_price = series.adjusted_close.get(signal_session) or series.raw_close.get(
        signal_session
    )
    if (
        entry_price is None
        or exit_price is None
        or entry_price <= 0
        or exit_price <= 0
    ):
        return HorizonOutcome(
            months=months,
            status="no_price",
            reason="non_positive_price",
            entry_date=entry,
            exit_date=exit_day,
            entry_deferral_sessions=entry_deferral,
            exit_deferral_sessions=exit_deferral,
            flags=flags,
        )
    gross = exit_price / entry_price - 1.0
    signal = None
    if signal_price is not None and signal_price > 0:
        signal = exit_price / signal_price - 1.0
    return HorizonOutcome(
        months=months,
        status="ok",
        entry_date=entry,
        exit_date=exit_day,
        entry_deferral_sessions=entry_deferral,
        exit_deferral_sessions=exit_deferral,
        gross_return=gross,
        net_return=net_return(gross, cost),
        signal_return=signal,
        flags=flags,
    )


def adjusted_source(series: PriceSeries) -> str:
    """``backward`` when the stored adjusted series drove the return."""

    return series.adjusted_source


__all__ = [
    "adjusted_source",
    "compute_outcome",
    "entry_tradability",
    "exit_tradability",
    "net_return",
    "price_limit_ratio",
    "resolve_entry",
    "resolve_exit",
    "session_on_or_after",
    "session_on_or_before",
]
