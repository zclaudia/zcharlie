"""Directory-search read model shared by the HTTP API, the MCP transport and
the agent's ``search_stock_directory`` tool.

Sits below every transport: this module imports storage, settings and domain
modules only, never ``api``/``agent``/``mcp_server`` — the agent tool must not
reach back into the API package (import-graph baseline).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import structlog
from pydantic import BaseModel
from sqlalchemy import or_, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.clock import shanghai_today
from investment_agent.config import Settings, SnapshotScheduleConfig
from investment_agent.domain.a_share import format_listing_ticker
from investment_agent.factors.industry_groups import usable_sw_code
from investment_agent.providers.shenwan_directory import (
    SW2021_SYSTEM,
    default_shenwan_directory,
)
from investment_agent.settings.loader import load_runtime
from investment_agent.storage.models.earnings_events import DisclosureScheduleRecord
from investment_agent.storage.models.pit_snapshot import (
    FinancialSnapshot,
    IndustryClassification,
)
from investment_agent.storage.models.reference import Listing
from investment_agent.storage.models.user_domain import WatchlistItem
from investment_agent.storage.repositories.pit import end_of_business_day_utc
from investment_agent.storage.repositories.stock_directory import StockDirectoryRepository
from investment_agent.storage.repositories.universe import UniverseRepository

log = structlog.get_logger(__name__)

_APP_CONFIG_PATH = Path("config/app.yaml")


class StockSearchItem(BaseModel):
    """One directory hit; the client fills the stock's form fields."""

    ticker: str
    name: str
    industry: str | None = None
    """Current Shenwan level-1 industry, with CSRC as fallback."""
    report_year: int | None = None
    """Latest completed fiscal year confirmed by an actual annual disclosure."""
    disclosed_on: date | None = None
    status: str
    """``listed`` / ``suspended`` / ``delisted``."""
    in_universe: bool
    """Member of the current snapshot target pool today."""
    on_watchlist: bool


async def snapshot_universes(session: AsyncSession, settings: Settings) -> tuple[str, ...]:
    """The snapshot target pool from the runtime schedule (settings store)."""

    try:
        snapshot: SnapshotScheduleConfig = (
            await load_runtime(session, settings, _APP_CONFIG_PATH)
        ).schedule.snapshot
    except Exception:
        log.warning("data_health.schedule_unavailable")
        snapshot = SnapshotScheduleConfig()
    return tuple(dict.fromkeys(snapshot.universes))


async def search_directory(
    session: AsyncSession, settings: Settings | None, q: str, *, limit: int
) -> list[StockSearchItem]:
    """Directory hits for ``q`` with the target-pool / watchlist flags
    (shared by the API, the MCP ``search_stocks`` tool and the agent).

    ``settings=None`` (agent tool) skips the runtime target-pool lookup and
    reports every item as outside the universe.
    """

    query = q.strip()
    if not query:
        return []
    rows = await StockDirectoryRepository(session).search(query, limit=limit)
    if not rows:
        return []
    tickers = {row.ticker for row in rows}
    today = shanghai_today()
    industries = await _stock_industries(session, tickers, today)
    annuals = await _annual_disclosures(session, tickers, today)
    universes = await snapshot_universes(session, settings) if settings is not None else ()
    members = await UniverseRepository(session).members_as_of(universes[0], today) if universes else ()
    in_universe = {member.ticker for member in members} & tickers
    on_watchlist = await _watchlist_tickers(session) & tickers
    return [
        StockSearchItem(
            ticker=row.ticker,
            name=row.name,
            industry=industries.get(row.ticker),
            report_year=annuals[row.ticker][0] if row.ticker in annuals and annuals[row.ticker][0] == today.year - 1 else None,
            disclosed_on=annuals[row.ticker][1] if row.ticker in annuals and annuals[row.ticker][0] == today.year - 1 else None,
            status=row.status,
            in_universe=row.ticker in in_universe,
            on_watchlist=row.ticker in on_watchlist,
        )
        for row in rows
    ]


async def _annual_disclosures(
    session: AsyncSession, tickers: set[str], today: date
) -> dict[str, tuple[int, date]]:
    """Actual annual disclosures from financial captures or published schedules."""
    cutoff = end_of_business_day_utc(today)
    snapshots = select(
        FinancialSnapshot.listing_id.label("listing_id"),
        FinancialSnapshot.report_period.label("period"),
        FinancialSnapshot.available_from.label("disclosed_on"),
    ).where(
        FinancialSnapshot.period_kind == "annual",
        FinancialSnapshot.availability == "actual",
        FinancialSnapshot.available_from <= today,
        FinancialSnapshot.captured_at < cutoff,
    )
    schedules = select(
        DisclosureScheduleRecord.listing_id,
        DisclosureScheduleRecord.report_period,
        DisclosureScheduleRecord.actual_date,
    ).where(
        DisclosureScheduleRecord.actual_date <= today,
        DisclosureScheduleRecord.captured_at < cutoff,
    )
    disclosures = union_all(snapshots, schedules).subquery()
    stmt = select(
        Listing.ticker, Listing.market_code, disclosures.c.period, disclosures.c.disclosed_on
    ).join(disclosures, disclosures.c.listing_id == Listing.id).where(
        Listing.ticker.in_({ticker.split(".")[0] for ticker in tickers}),
    )
    annuals: dict[str, tuple[int, date]] = {}
    for symbol, market, period, disclosed_on in (await session.execute(stmt)).all():
        ticker = format_listing_ticker(ticker=symbol, market_code=market)
        if ticker not in tickers or (period.month, period.day) != (12, 31) or period.year >= today.year:
            continue
        known = annuals.get(ticker)
        if not known or period.year > known[0] or (period.year == known[0] and disclosed_on < known[1]):
            annuals[ticker] = (period.year, disclosed_on)
    return annuals


async def _stock_industries(
    session: AsyncSession, tickers: set[str], today: date
) -> dict[str, str]:
    """Batch local industries using the same preference as candidate promotion."""
    stmt = (
        select(Listing.ticker, Listing.market_code, IndustryClassification)
        .join(IndustryClassification, IndustryClassification.listing_id == Listing.id)
        .where(
            Listing.ticker.in_({ticker.split(".")[0] for ticker in tickers}),
            IndustryClassification.system.in_((SW2021_SYSTEM, "csrc")),
            IndustryClassification.valid_from <= today,
            or_(IndustryClassification.valid_to.is_(None), IndustryClassification.valid_to > today),
        )
        .order_by(IndustryClassification.valid_from.desc())
    )
    industries: dict[str, str] = {}
    shenwan: dict[str, str] = {}
    directory = default_shenwan_directory()
    for symbol, market, classification in (await session.execute(stmt)).all():
        ticker = format_listing_ticker(ticker=symbol, market_code=market)
        if ticker not in tickers:
            continue
        if classification.system == SW2021_SYSTEM:
            code = usable_sw_code(
                classification.code, classification.valid_from, classification.valid_to,
                directory, classification.name,
            )
            chain = directory.chain(code) if code else ()
            if chain and chain[0].level == 1:
                shenwan.setdefault(ticker, chain[0].name)
        elif classification.name:
            industries.setdefault(ticker, classification.name)
    return {**industries, **shenwan}


async def _watchlist_tickers(session: AsyncSession) -> set[str]:
    stmt = select(Listing.ticker, Listing.market_code).join(
        WatchlistItem, WatchlistItem.listing_id == Listing.id
    )
    return {
        format_listing_ticker(ticker=ticker, market_code=market_code)
        for ticker, market_code in (await session.execute(stmt)).all()
    }
