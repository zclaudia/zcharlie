"""Persist wmshare captures and expose the existing cash-yield calculation."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.factors.catalog import DEFAULT_REGISTRY
from investment_agent.factors.view_db import load_snapshot_view
from investment_agent.providers.wmshare_dividends import WmshareCapture, _date
from investment_agent.storage.models.pit_snapshot import WmshareDividendSnapshot
from investment_agent.storage.repositories.dividend import DividendRepository
from investment_agent.storage.repositories.listing import ListingRepository
from investment_agent.storage.repositories.wmshare_dividend_snapshot import latest_as_of


async def persist_wmshare_capture(
    session: AsyncSession, listing_id: int, capture: WmshareCapture, raw_ref: str | None = None
) -> dict[str, Any]:
    """Save successful parts; caller controls the transaction."""
    repository = DividendRepository(session)
    inserted = 0
    if capture.covered_years and not capture.history_error:
        existing = await repository.events_as_of(
            listing_id, date(min(capture.covered_years), 1, 1), capture.as_of, provider="wmshare"
        )
        returned = {event.ex_date for event in capture.events}
        if any(row.ex_date not in returned for row in existing):
            capture.history_error = (
                "Previously recorded dividend ex-date missing; reconciliation required"
            )
            capture.covered_years = []
    # Never overlay a previously complete event set with a partial response.
    for event in capture.events if capture.covered_years and not capture.history_error else ():
        row = await repository.upsert_event(
            listing_id=listing_id,
            ex_date=event.ex_date,
            available_from=event.announce_date or event.ex_date,
            operate_year=event.ex_date.year,
            provider="wmshare",
            captured_at=capture.captured_at,
            payload={
                "announce_date": event.announce_date,
                "pay_date": event.pay_date,
                "cash_per_share_pre_tax": event.cash_per_share,
            },
            details=event.details,
            raw_ref=raw_ref,
        )
        inserted += row is not None
    for year in capture.covered_years if not capture.history_error else ():
        await repository.record_coverage(
            listing_id=listing_id,
            provider="wmshare",
            year=year,
            checked_at=capture.captured_at,
            event_count=sum(event.ex_date.year == year for event in capture.events),
        )

    def metric(value: object) -> Decimal | None:
        try:
            result = Decimal(str(value))
            return result if result.is_finite() and not isinstance(value, bool) else None
        except InvalidOperation:
            return None

    history_available = (
        bool(capture.covered_years) and not capture.history_error
    ) or capture.pending_only
    if capture.snapshot is not None or history_available:
        payload = capture.snapshot or {}
        session.add(
            WmshareDividendSnapshot(
                listing_id=listing_id,
                captured_at=capture.captured_at,
                snapshot_available=capture.snapshot is not None,
                history_available=history_available,
                quote_date=_date(payload.get("quote_date")),
                yield_ttm=metric(payload.get("yield_ttm")),
                payout_ttm=metric(payload.get("payout_ttm")),
                payload=payload,
                pending=capture.pending if history_available else [],
                raw_ref=raw_ref,
            )
        )
    await session.flush()
    return {
        "events_inserted": inserted,
        "covered_years": capture.covered_years,
        "snapshot_saved": capture.snapshot is not None,
        "history_error": capture.history_error,
        "snapshot_error": capture.snapshot_error,
        "terminal": capture.terminal,
        "ec": capture.ec,
    }


async def dividend_context(session: AsyncSession, ticker: str, as_of: date) -> dict[str, Any]:
    """Return the point-in-time stock-page context using the existing factor."""
    listing = await ListingRepository(session).get_a_share_listing(ticker)
    if listing is None:
        raise LookupError(f"no listing for {ticker}")
    view = await load_snapshot_view(session, ticker, as_of)
    factor = DEFAULT_REGISTRY.get("dividend_yield_ttm").compute(view)
    bar = view.latest_raw_bar()
    history = view.dividends
    provider = history.provider if history else None
    rows = (
        await DividendRepository(session).events_as_of(
            listing.id, date(1900, 1, 1), as_of, provider=provider
        )
        if provider
        else []
    )
    snapshot = await latest_as_of(session, listing.id, as_of)
    history_capture = await latest_as_of(session, listing.id, as_of, history=True)
    platform = (
        {
            "yield_ttm": float(snapshot.yield_ttm) if snapshot.yield_ttm is not None else None,
            "payout_ttm": float(snapshot.payout_ttm) if snapshot.payout_ttm is not None else None,
            "quote_date": snapshot.quote_date.isoformat() if snapshot.quote_date else None,
            "captured_at": snapshot.captured_at.isoformat(),
        }
        if snapshot
        else None
    )
    diff = None
    if (
        snapshot is not None
        and snapshot.yield_ttm is not None
        and factor.value is not None
        and bar
        and snapshot.quote_date == bar.trade_date == as_of
    ):
        value = float(snapshot.yield_ttm) - factor.value
        if abs(value) > 0.003:
            diff = {"value": value, "points": value * 100, "label": "口径差异"}
    return {
        "ticker": ticker,
        "as_of": as_of.isoformat(),
        "cash": {
            "value": factor.value,
            "cash_per_share": factor.inputs_ref.get("cash_per_share_ttm"),
            "window_start": (as_of - timedelta(days=365)).isoformat(),
            "window_end": as_of.isoformat(),
            "price": bar.close if bar else None,
            "price_date": bar.trade_date.isoformat() if bar else None,
            "provider": provider,
            "quality": factor.quality,
            "flags": list(factor.flags),
        },
        "events": [
            {
                "ex_date": row.ex_date.isoformat(),
                "dps": float(row.cash_per_share_pre_tax)
                if row.cash_per_share_pre_tax is not None
                else None,
                "pay_date": row.pay_date.isoformat() if row.pay_date else None,
                "provider": row.provider,
            }
            for row in rows
        ],
        "pending": history_capture.pending if history_capture else [],
        "platform": platform,
        "difference": diff,
    }
