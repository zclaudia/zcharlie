"""Point-in-time price lookup shared by the ledger projection and daily valuation.

One query shape serves both readers: the latest ``adjust == "none"`` close of a
listing that a knowledge cutoff could know (``captured_at <= cutoff``), never
after the cutoff's Shanghai business date and strictly positive. The ledger
projection asks for a whole page of listings at once, the daily valuation for
one listing at a time; both must resolve the same row for the same instant.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Protocol

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.storage.models.pit_snapshot import MarketSnapshot


@dataclass(frozen=True)
class PricePoint:
    """The one PIT close a knowledge cutoff may use for a listing."""

    id: int
    version: int
    trade_date: date
    close: Decimal


class PriceLookup(Protocol):
    """An injected page of PIT prices: the projection needs no database.

    ``listing_ids`` are the listings to resolve; the mapping holds only the
    listings a usable close exists for. ``trade_date`` is the business-date
    bound and ``knowledge_cutoff`` the instant the data must not postdate.
    """

    async def __call__(
        self,
        listing_ids: Sequence[Any],
        *,
        trade_date: date,
        knowledge_cutoff: datetime,
    ) -> Mapping[Any, PricePoint]: ...


def _pit_filters(trade_date: date, knowledge_cutoff: datetime) -> list[Any]:
    """The filters every PIT price read shares; order is irrelevant to the result."""

    return [
        MarketSnapshot.adjust == "none",
        MarketSnapshot.trade_date <= trade_date,
        MarketSnapshot.captured_at <= knowledge_cutoff,
        MarketSnapshot.close > 0,
    ]


def _order_by() -> tuple[Any, ...]:
    return (
        MarketSnapshot.trade_date.desc(),
        MarketSnapshot.version.desc(),
        MarketSnapshot.id.desc(),
    )


async def latest_price_as_of(
    session: AsyncSession,
    listing_id: Any,
    *,
    trade_date: date,
    knowledge_cutoff: datetime,
) -> PricePoint | None:
    """The latest usable close of one listing at the cutoff, or ``None``."""

    row: MarketSnapshot | None = await session.scalar(
        select(MarketSnapshot)
        .where(*_pit_filters(trade_date, knowledge_cutoff), MarketSnapshot.listing_id == listing_id)
        .order_by(*_order_by())
        .limit(1)
    )
    if row is None:
        return None
    return PricePoint(id=row.id, version=row.version, trade_date=row.trade_date, close=row.close)


async def latest_prices_as_of(
    session: AsyncSession,
    listing_ids: Sequence[Any],
    *,
    trade_date: date,
    knowledge_cutoff: datetime,
) -> dict[Any, PricePoint]:
    """The same row as :func:`latest_price_as_of` for every listing, in one query.

    A window function ranks the candidate rows per listing exactly as the
    single-listing ``order_by(...).limit(1)`` chain would, so the resolved
    winner cannot differ between the batch and the per-listing read.
    """

    ids = list(listing_ids)
    if not ids:
        return {}
    ranked: Any = (
        select(
            MarketSnapshot.listing_id.label("listing_id"),
            MarketSnapshot.id.label("id"),
            MarketSnapshot.version.label("version"),
            MarketSnapshot.trade_date.label("trade_date"),
            MarketSnapshot.close.label("close"),
            func.row_number()
            .over(
                partition_by=MarketSnapshot.listing_id,
                order_by=_order_by(),
            )
            .label("rank"),
        )
        .where(*_pit_filters(trade_date, knowledge_cutoff), MarketSnapshot.listing_id.in_(ids))
        .subquery()
    )
    rows = (
        await session.execute(
            select(
                ranked.c.listing_id,
                ranked.c.id,
                ranked.c.version,
                ranked.c.trade_date,
                ranked.c.close,
            ).where(ranked.c.rank == 1)
        )
    ).all()
    return {
        row.listing_id: PricePoint(
            id=row.id, version=row.version, trade_date=row.trade_date, close=row.close
        )
        for row in rows
    }


class SessionPriceLookup:
    """The session-backed :class:`PriceLookup` the ledger service injects."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def __call__(
        self,
        listing_ids: Sequence[Any],
        *,
        trade_date: date,
        knowledge_cutoff: datetime,
    ) -> Mapping[Any, PricePoint]:
        return await latest_prices_as_of(
            self._session,
            listing_ids,
            trade_date=trade_date,
            knowledge_cutoff=knowledge_cutoff,
        )
