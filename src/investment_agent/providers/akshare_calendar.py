"""Sina's A-share trading calendar via AkShare (``tool_trade_date_hist_sina``).

The second, independent answer to "which days trade" used by
``tools/refresh_trading_calendar.py``: one request returns every trading day
since 1990-12-19 through the end of the current year, and it extends into the
next year once the exchanges publish their holiday schedule (around December).

Maintenance-only: scheduled jobs gate on the committed
``config/calendars/a_share.yaml`` and never call a provider. ``akshare`` is
imported lazily so offline tests never touch it.
"""

from __future__ import annotations

from collections.abc import Collection
from datetime import date, datetime
from typing import Any, Protocol, cast

import structlog

from investment_agent.providers.errors import ProviderDataError, ProviderTransientError

log = structlog.get_logger(__name__)

PROVIDER = "sina"
COLUMN = "trade_date"


class CalendarBackend(Protocol):
    def tool_trade_date_hist_sina(self) -> Any: ...


def load_default_backend() -> CalendarBackend:
    """Import the real ``akshare`` module lazily (it pulls in pandas)."""

    import akshare

    return cast(CalendarBackend, akshare)


def _as_day(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except ValueError as exc:
            raise ProviderDataError(
                f"sina calendar returned an unparsable date {value!r}", provider=PROVIDER
            ) from exc
    to_date = getattr(value, "date", None)  # pandas.Timestamp
    if callable(to_date):
        converted = to_date()
        if isinstance(converted, date):
            return converted
    raise ProviderDataError(
        f"sina calendar returned an unsupported date value {value!r}", provider=PROVIDER
    )


def fetch_trading_days(*, backend: CalendarBackend | None = None) -> Collection[date]:
    """Every trading day Sina lists (all years), from one request."""

    client = backend if backend is not None else load_default_backend()
    try:
        frame = client.tool_trade_date_hist_sina()
    except Exception as exc:  # requests / parsing failures from AkShare
        raise ProviderTransientError(
            f"sina calendar fetch failed: {exc}", provider=PROVIDER
        ) from exc
    try:
        column = frame[COLUMN]
    except Exception as exc:
        raise ProviderDataError(
            f"sina calendar answer has no {COLUMN!r} column", provider=PROVIDER
        ) from exc
    days = frozenset(_as_day(value) for value in column)
    if not days:
        raise ProviderDataError("sina calendar answer is empty", provider=PROVIDER)
    log.info("sina_calendar_fetched", days=len(days), latest=max(days).isoformat())
    return days
