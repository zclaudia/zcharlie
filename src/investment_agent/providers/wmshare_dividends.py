"""Narrow wmshare dividend adapter; does not configure or create the SDK client."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any


def _date(value: object) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def _amount(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() and result >= 0 else None
    except InvalidOperation:
        return None


@dataclass(frozen=True)
class WmshareEvent:
    ex_date: date
    cash_per_share: Decimal
    announce_date: date | None
    pay_date: date | None
    details: dict[str, Any]


@dataclass
class WmshareCapture:
    ticker: str
    as_of: date
    captured_at: datetime
    events: list[WmshareEvent] = field(default_factory=list)
    pending: list[dict[str, Any]] = field(default_factory=list)
    covered_years: list[int] = field(default_factory=list)
    history: Any = None
    snapshot: dict[str, Any] | None = None
    history_error: str | None = None
    snapshot_error: str | None = None
    pending_only: bool = False
    terminal: bool = False
    ec: dict[str, int | None] = field(default_factory=dict)


def parse_history(capture: WmshareCapture, rows: Any) -> None:
    """Retain ambiguous rows, and never certify incomplete/empty results."""
    capture.history = rows
    if not isinstance(rows, list):
        capture.history_error = "Dividend history is not a list"
        return
    incomplete = not rows or len(rows) >= 500
    by_date: dict[date, WmshareEvent] = {}
    ambiguous: set[date] = set()
    for raw in rows:
        if not isinstance(raw, dict):
            incomplete = True
            continue
        ex = _date(raw.get("ex_div"))
        amount = _amount(raw.get("dps"))
        if amount is None:
            if ex is None or ex.year >= capture.as_of.year - 1:
                incomplete = True
            capture.pending.append(raw)
            continue
        if ex is None or ex > capture.as_of:
            if raw.get("ex_div") and ex is None:
                incomplete = True
            if amount > 0:
                capture.pending.append(raw)
            continue
        if amount == 0:
            continue
        announce = _date(raw.get("announce"))
        if announce and announce > capture.as_of:
            if ex.year >= capture.as_of.year - 1:
                incomplete = True
            capture.pending.append(raw)
            continue
        event = WmshareEvent(ex, amount, announce, _date(raw.get("pay")), raw)
        previous = by_date.get(ex)
        if previous and (previous.cash_per_share != amount or previous.details != raw):
            if ex.year >= capture.as_of.year - 1:
                incomplete = True
            ambiguous.add(ex)
        else:
            by_date[ex] = event
    capture.events = [by_date[day] for day in sorted(by_date) if day not in ambiguous]
    if not capture.events:
        # A well-formed response with only undated/future plans is useful for
        # display, but does not establish a complete cash-dividend history.
        capture.pending_only = bool(capture.pending) and not incomplete
        incomplete = True
    if not incomplete:
        capture.covered_years = [capture.as_of.year - 1, capture.as_of.year]
    else:
        capture.history_error = "Dividend history incomplete or unconfirmed"


class WmshareDividendClient:
    def __init__(self, client: Any):
        self.client = client

    async def fetch(self, ticker: str, as_of: date) -> WmshareCapture:
        capture = WmshareCapture(ticker=ticker, as_of=as_of, captured_at=datetime.now(UTC))
        for part in ("history", "snapshot"):
            try:
                self.client.last_ec_cost = None
                if part == "history":
                    value = await asyncio.to_thread(self.client.dividend.history, ticker, limit=500)
                    parse_history(capture, value)
                else:
                    value = await asyncio.to_thread(self.client.dividend.snapshot, sym=ticker)
                    if isinstance(value, dict):
                        capture.snapshot = value
                    else:
                        capture.snapshot_error = "Dividend snapshot is not a map"
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                for key in ("client_id", "client_secret"):
                    secret = getattr(self.client, key, None)
                    if secret:
                        error = error.replace(str(secret), "[redacted]")
                setattr(capture, f"{part}_error", error)
                capture.terminal = type(exc).__name__ in ("QuotaError", "AuthError")
            capture.ec[part] = getattr(self.client, "last_ec_cost", None)
            if capture.terminal:
                break
        capture.captured_at = datetime.now(UTC)
        return capture
