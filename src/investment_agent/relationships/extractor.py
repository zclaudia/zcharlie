"""Persist LLM-extracted relationships to company_relationship table."""

from datetime import UTC, date, datetime
from decimal import Decimal

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.domain.strategy_types import ExtractedRelationship
from investment_agent.relationships.resolver import resolve_counterparty
from investment_agent.storage.models.relationship import CompanyRelationship

log = structlog.get_logger(__name__)

_VALID_TYPES = {"supplier_of", "competes_with", "subsidiary_of", "partner_of"}


async def persist_extracted_relationships(
    session: AsyncSession,
    *,
    from_company_id: int,
    extracted: list[ExtractedRelationship],
    source: str = "llm_extracted",
) -> int:
    """For each extracted relationship: resolve counterparty, upsert CompanyRelationship row.

    Returns the number of new rows inserted (existing rows are touched via last_verified_at).
    """
    inserted = 0
    today = date.today()
    now = datetime.now(UTC)

    for rel in extracted:
        if rel.relationship_type not in _VALID_TYPES:
            log.warning(
                "relationship.invalid_type",
                relationship_type=rel.relationship_type,
                from_company_id=from_company_id,
            )
            continue

        to_company_id = await resolve_counterparty(
            session, rel.counterparty_name, rel.counterparty_ticker
        )
        if to_company_id == from_company_id:
            # Self-relationship — skip
            continue

        # Check existing open relationship (valid_to IS NULL)
        stmt = select(CompanyRelationship).where(
            CompanyRelationship.from_company_id == from_company_id,
            CompanyRelationship.to_company_id == to_company_id,
            CompanyRelationship.relationship_type == rel.relationship_type,
            CompanyRelationship.valid_to.is_(None),
        )
        existing = (await session.execute(stmt)).scalar_one_or_none()

        if existing is not None:
            existing.last_verified_at = now
            # If new strength is meaningfully different, update strength + last_verified
            if abs(float(existing.strength) - rel.strength) > 0.05:
                existing.strength = Decimal(str(rel.strength))
            continue

        # LLM-extracted relationships start at confidence < 0.7 (per spec)
        session.add(
            CompanyRelationship(
                from_company_id=from_company_id,
                to_company_id=to_company_id,
                relationship_type=rel.relationship_type,
                strength=Decimal(str(rel.strength)),
                revenue_share_pct=(
                    Decimal(str(rel.revenue_share_pct))
                    if rel.revenue_share_pct is not None
                    else None
                ),
                description=rel.description,
                source=source,
                confidence=Decimal("0.6"),  # LLM extractions start below 0.7 per spec
                valid_from=today,
                discovered_at=now,
                last_verified_at=now,
            )
        )
        inserted += 1

    await session.flush()
    log.info(
        "relationship.persisted",
        from_company_id=from_company_id,
        total_extracted=len(extracted),
        inserted=inserted,
    )
    return inserted
