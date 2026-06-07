"""Attribution tools — Python functions registered with Pydantic-AI Agent.

Each function must:
- Be async
- Have complete type annotations + docstring (Pydantic-AI infers tool schema)
- Be read-only (no DB writes)
- Be safe to call multiple times (idempotent)
"""

from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.storage.models.market_data import (
    AnalystRatingChange,
    DailyPrice,
    FinancialReport,
    NewsItem,
)
from investment_agent.storage.models.reference import Company, Listing
from investment_agent.storage.models.relationship import CompanyRelationship


async def get_recent_news(
    session: AsyncSession, *, company_id: int, days: int = 7,
) -> list[dict[str, object]]:
    """Return news items for `company_id` published in the last `days` days, newest first."""
    cutoff = datetime.now(UTC) - timedelta(days=days)
    stmt = (
        select(NewsItem)
        .where(NewsItem.company_id == company_id)
        .where(NewsItem.published_at >= cutoff)
        .order_by(NewsItem.published_at.desc())
        .limit(50)
    )
    rows = list((await session.execute(stmt)).scalars().all())
    return [
        {
            "headline": r.title,
            "source": r.source,
            "url": r.url,
            "published_at": r.published_at.isoformat(),
            "summary": r.summary,
        }
        for r in rows
    ]


async def get_recent_ratings(
    session: AsyncSession, *, company_id: int, days: int = 14,
) -> list[dict[str, object]]:
    """Return analyst rating changes for `company_id` in the last `days` days."""
    cutoff = date.today() - timedelta(days=days)
    stmt = (
        select(AnalystRatingChange)
        .where(AnalystRatingChange.company_id == company_id)
        .where(AnalystRatingChange.date >= cutoff)
        .order_by(AnalystRatingChange.date.desc())
        .limit(20)
    )
    rows = list((await session.execute(stmt)).scalars().all())
    return [
        {
            "firm": r.firm,
            "action": r.action,
            "from_rating": r.from_rating,
            "to_rating": r.to_rating,
            "price_target": float(r.price_target) if r.price_target is not None else None,
            "date": r.date.isoformat(),
        }
        for r in rows
    ]


async def get_recent_earnings(
    session: AsyncSession, *, company_id: int, periods: int = 3,
) -> list[dict[str, object]]:
    """Return up to `periods` most-recent financial reports for `company_id`."""
    stmt = (
        select(FinancialReport)
        .where(FinancialReport.company_id == company_id)
        .order_by(FinancialReport.period_end.desc())
        .limit(periods)
    )
    rows = list((await session.execute(stmt)).scalars().all())
    return [
        {
            "fiscal_period": r.fiscal_period,
            "period_end": r.period_end.isoformat(),
            "revenue": float(r.revenue) if r.revenue is not None else None,
            "net_income": float(r.net_income) if r.net_income is not None else None,
            "fcf": float(r.fcf) if r.fcf is not None else None,
        }
        for r in rows
    ]


async def get_related_company_events(
    session: AsyncSession, *, company_id: int, days: int = 7, max_companies: int = 5,
) -> list[dict[str, object]]:
    """For each strong related company (strength>=0.5), return recent news headlines."""
    rel_stmt = (
        select(CompanyRelationship)
        .where(CompanyRelationship.from_company_id == company_id)
        .where(CompanyRelationship.valid_to.is_(None))
        .where(CompanyRelationship.strength >= 0.5)
        .order_by(CompanyRelationship.strength.desc())
        .limit(max_companies)
    )
    rels = list((await session.execute(rel_stmt)).scalars().all())
    cutoff = datetime.now(UTC) - timedelta(days=days)
    out: list[dict[str, object]] = []
    for rel in rels:
        company = await session.get(Company, rel.to_company_id)
        listing_stmt = (
            select(Listing).where(Listing.company_id == rel.to_company_id).limit(1)
        )
        listing = (await session.execute(listing_stmt)).scalar_one_or_none()
        news_stmt = (
            select(NewsItem)
            .where(NewsItem.company_id == rel.to_company_id)
            .where(NewsItem.published_at >= cutoff)
            .order_by(NewsItem.published_at.desc())
            .limit(5)
        )
        news_rows = list((await session.execute(news_stmt)).scalars().all())
        out.append({
            "related_company": company.canonical_name if company else "unknown",
            "related_ticker": listing.ticker if listing else None,
            "relationship_type": rel.relationship_type,
            "strength": float(rel.strength),
            "recent_news": [
                {"headline": n.title, "published_at": n.published_at.isoformat()}
                for n in news_rows
            ],
        })
    return out


async def get_peer_returns(
    session: AsyncSession, *, listing_id: int, days: int = 1, limit: int = 5,
) -> list[dict[str, object]]:
    """Return same-sector peers' adjusted-close return over the trailing `days` window."""
    listing = await session.get(Listing, listing_id)
    if listing is None:
        return []
    company = await session.get(Company, listing.company_id)
    if company is None or company.sector_code is None:
        return []
    peer_stmt = (
        select(Company)
        .where(Company.sector_code == company.sector_code)
        .where(Company.id != company.id)
        .limit(limit)
    )
    peers = list((await session.execute(peer_stmt)).scalars().all())
    out: list[dict[str, object]] = []
    for peer in peers:
        peer_listing_stmt = (
            select(Listing).where(Listing.company_id == peer.id).limit(1)
        )
        peer_listing = (
            await session.execute(peer_listing_stmt)
        ).scalar_one_or_none()
        if peer_listing is None:
            continue
        prices_stmt = (
            select(DailyPrice)
            .where(DailyPrice.listing_id == peer_listing.id)
            .order_by(DailyPrice.date.desc())
            .limit(days + 1)
        )
        prices = list((await session.execute(prices_stmt)).scalars().all())
        if len(prices) < 2:
            continue
        p_latest = float(prices[0].adjusted_close)
        p_old = float(prices[-1].adjusted_close)
        if p_old == 0:
            continue
        ret = (p_latest - p_old) / p_old
        out.append({"ticker": peer_listing.ticker, "return_pct": round(ret, 4)})
    return out
