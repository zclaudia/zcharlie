"""Read existing local A-share data for the wmshare experiment.

The target universe is CSI300; this probe does not load application settings.
"""

from __future__ import annotations

import asyncio
from datetime import date, timedelta
from pathlib import Path

from sqlalchemy import event, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from investment_agent.domain.a_share import format_listing_ticker
from investment_agent.factors.catalog import DEFAULT_REGISTRY
from investment_agent.factors.snapshot_model import DIVIDEND_HISTORY_DAYS
from investment_agent.factors.view_db import load_snapshot_view
from investment_agent.storage.models.reference import Listing
from investment_agent.storage.repositories.dividend import DividendRepository
from investment_agent.storage.repositories.listing import ListingRepository
from investment_agent.storage.repositories.universe import UniverseRepository
from investment_agent.storage.repositories.watchlist import WatchlistRepository


def _empty_stock(note: str) -> dict:
    return {
        "price": None,
        "price_date": None,
        "dividend_yield_ttm": None,
        "annual_roe": None,
        "report_period": None,
        "dividend_events": None,
        "dividend_coverage": None,
        "note": note,
    }


async def _load(database: Path, as_of: date, tickers: list[str]) -> dict:
    # URI mode prevents SQLite from creating a missing database. query_only adds
    # a connection-level guard against accidental writes to an existing one.
    url = f"sqlite+aiosqlite:///{database.absolute().as_uri()}?mode=ro&uri=true"  # noqa: ASYNC240
    engine = create_async_engine(url)

    @event.listens_for(engine.sync_engine, "connect")
    def _read_only(connection, _record):
        cursor = connection.cursor()
        cursor.execute("PRAGMA query_only=ON")
        cursor.close()

    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            members = await UniverseRepository(session).members_as_of("CSI300", as_of)
            watchlist = await WatchlistRepository(session).list_enabled()
            ids = [item.listing_id for item in watchlist]
            listings = (await session.execute(select(Listing).where(Listing.id.in_(ids)))).scalars()
            watch_tickers = sorted(
                format_listing_ticker(ticker=x.ticker, market_code=x.market_code) for x in listings
            )
            stocks = {}
            for ticker in dict.fromkeys(t.upper() for t in tickers):
                try:
                    view = await load_snapshot_view(session, ticker, as_of)
                except LookupError as exc:
                    stocks[ticker] = _empty_stock(str(exc))
                    continue
                bar = view.latest_raw_bar()
                annual = view.latest_annual()
                dividend = DEFAULT_REGISTRY.get("dividend_yield_ttm").compute(view)
                roe = DEFAULT_REGISTRY.get("roe").compute(view, enforce_applicability=False)
                listing = await ListingRepository(session).get_a_share_listing(ticker)
                rows = await DividendRepository(session).events_as_of(
                    listing.id,
                    as_of - timedelta(days=DIVIDEND_HISTORY_DAYS),
                    as_of,
                    provider=view.provider,
                )
                stocks[ticker] = {
                    "price": bar.close if bar else None,
                    "price_date": bar.trade_date.isoformat() if bar else None,
                    "dividend_yield_ttm": dividend.value,
                    "annual_roe": roe.value,
                    "report_period": annual.report_period.isoformat() if annual else None,
                    "dividend_events": [
                        {
                            "ex_date": row.ex_date.isoformat(),
                            "cash_per_share": float(row.cash_per_share_pre_tax)
                            if row.cash_per_share_pre_tax is not None
                            else None,
                            "announce_date": row.announce_date.isoformat()
                            if row.announce_date
                            else None,
                            "available_from": row.available_from.isoformat()
                            if row.available_from
                            else None,
                        }
                        for row in rows
                    ],
                    "dividend_coverage": view.dividends.describe_coverage()
                    if view.dividends
                    else None,
                    "industry": view.industry_name,
                    "note": ", ".join(dividend.flags) if dividend.flags else "",
                }
            return {
                "status": "ok" if members else "partial",
                "note": (
                    "Universe: CSI300; watchlist: currently enabled entries."
                    if members
                    else "CSI300 membership unavailable for this date; watchlist: currently enabled entries."
                ),
                "universe_tickers": [member.ticker for member in members] if members else None,
                "watchlist_tickers": watch_tickers,
                "stocks": stocks,
            }
    finally:
        await engine.dispose()


def load_local_context(database: Path | None, as_of: date, tickers: list[str]) -> dict:
    """Load local data without creating or changing the supplied database."""
    if database is None:
        reason = "No local database supplied."
    else:
        try:
            return asyncio.run(_load(database, as_of, tickers))
        except (OSError, SQLAlchemyError) as exc:
            reason = f"Local database unavailable: {exc}"
    return {
        "status": "unavailable",
        "note": reason,
        "universe_tickers": None,
        "watchlist_tickers": None,
        "stocks": {ticker.upper(): _empty_stock(reason) for ticker in tickers},
    }
