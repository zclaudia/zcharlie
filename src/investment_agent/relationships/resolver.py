"""Resolve LLM-extracted counterparty names → company_id (match-or-create)."""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.storage.models.reference import Company, Listing


async def resolve_counterparty(
    session: AsyncSession,
    counterparty_name: str,
    counterparty_ticker: str | None = None,
) -> int:
    """Return company_id. Strategy: ticker > fuzzy name match > create placeholder Company."""
    # 1. Ticker match (most reliable)
    if counterparty_ticker:
        stmt = select(Listing).where(Listing.ticker == counterparty_ticker)
        listing = (await session.execute(stmt)).scalar_one_or_none()
        if listing:
            return listing.company_id

    # 2. Case-insensitive fuzzy name match (LIKE %name%)
    stmt2 = (
        select(Company)
        .where(func.lower(Company.canonical_name).like(f"%{counterparty_name.lower()}%"))
        .limit(1)
    )
    found = (await session.execute(stmt2)).scalar_one_or_none()
    if found:
        return found.id

    # 3. Placeholder
    new_company = Company(
        canonical_name=counterparty_name,
        description="Placeholder (auto-created from LLM extraction)",
    )
    session.add(new_company)
    await session.flush()
    return new_company.id
