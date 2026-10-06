"""Pure metric functions shared by every provider path (single definition).

These are the only implementations of the derived metrics used both by the
iFinD canonicalization (:mod:`investment_agent.research.ifind_chain`) and by
the factor library; neither side keeps a copy. Behaviour is frozen by the
golden corpus (decision D3): changing a formula here changes observation
scores and needs a scoring version bump.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterable, Sequence
from datetime import date, timedelta
from typing import Any

# Trailing window for the "1y" market statistics. The provider request asks for
# ~400 calendar days so the window survives holidays; statistics are computed
# only over sessions within 365 calendar days of the anchor date.
ONE_YEAR_WINDOW_DAYS = 365
# Fewer sessions than this inside the window means the "1y" statistics cover
# materially less than a year (new listing, long suspension) and are flagged.
MIN_ONE_YEAR_SESSIONS = 200


def parse_number(value: Any) -> float | None:
    """Provider text/number -> finite float; blanks, ``--``, ``None``, ``nan`` -> ``None``.

    Thousands separators are removed (``"1,234.5"`` -> ``1234.5``).
    """

    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text or text in {"--", "None", "null", "nan"}:
        return None
    try:
        result = float(text)
    except ValueError:
        return None
    return result if math.isfinite(result) else None


def percent_to_fraction(value: Any) -> float | None:
    """Provider percent (``34.46``) -> fraction (``0.3446``) via :func:`parse_number`."""

    number = parse_number(value)
    return None if number is None else number / 100.0


def annualized_growth(latest: float | None, base: float | None, years: int) -> float | None:
    """Compound annual growth ``(latest / base) ** (1 / years) - 1``, rounded to 6 dp.

    Undefined (``None``) for a negative latest value, a non-positive base or a
    non-positive span: a CAGR across a sign change is meaningless.
    """

    if latest is None or base is None or latest < 0 or base <= 0 or years <= 0:
        return None
    return round(float((latest / base) ** (1 / years) - 1), 6)


def roe_stability(roe_values: Sequence[float | None]) -> float | None:
    """``1 - pstdev/mean`` over the full expected ROE history, clamped to [0, 1].

    Every expected year must be present and the mean must be positive;
    otherwise stability is undefined (a partial history or a loss-making
    average would make the ratio meaningless) and the field stays missing.
    """

    if not roe_values or any(value is None for value in roe_values):
        return None
    values = [float(value) for value in roe_values if value is not None]
    roe_mean = statistics.mean(values)
    if roe_mean <= 0:
        return None
    return round(max(0.0, min(1.0, 1 - statistics.pstdev(values) / roe_mean)), 6)


def positive_multiple(price: float | None, per_share: float | None) -> float | None:
    """``price / per_share`` only when both exist and the denominator is positive."""

    if price is None or per_share is None or per_share <= 0:
        return None
    return price / per_share


def trim_to_window[T](
    dated: Iterable[tuple[date, T]],
    anchor: date | None,
    *,
    days: int = ONE_YEAR_WINDOW_DAYS,
) -> list[T]:
    """Keep only items dated in ``(anchor - days, anchor]``, in input order.

    ``anchor`` defaults to the latest date present.
    """

    items = list(dated)
    if not items:
        return []
    end = anchor or max(item_date for item_date, _ in items)
    start = end - timedelta(days=days)
    return [item for item_date, item in items if start < item_date <= end]


def return_stats(
    series: Iterable[tuple[Any, float | None]],
) -> tuple[float | None, float | None, float | None]:
    """Total return, annualized volatility and max drawdown of a close series.

    ``series`` holds ``(sort_key, close)``; non-positive or missing closes are
    ignored and the rest is ordered by ``sort_key``. Volatility is the
    population stdev of daily simple returns × sqrt(252); drawdown is the
    largest positive peak-to-trough fraction. All rounded to 6 dp; ``None``
    with fewer than two prices.
    """

    points = [(key, close) for key, close in series if close is not None and close > 0]
    points.sort(key=lambda item: item[0])
    prices = [value for _, value in points]
    if len(prices) < 2:
        return None, None, None

    total_return = prices[-1] / prices[0] - 1
    daily_returns = [
        current / previous - 1 for previous, current in zip(prices, prices[1:], strict=False)
    ]
    volatility = statistics.pstdev(daily_returns) * math.sqrt(252)

    peak = prices[0]
    max_drawdown = 0.0
    for price in prices:
        peak = max(peak, price)
        max_drawdown = max(max_drawdown, (peak - price) / peak)
    return round(total_return, 6), round(volatility, 6), round(max_drawdown, 6)
