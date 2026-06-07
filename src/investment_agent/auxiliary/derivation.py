"""Derive auxiliary_listing rows from company_relationship (strength >= threshold).

Auxiliary listings provide lightweight cross-market context for the Discovery
pipeline. They are auto-derived from CompanyRelationship rows whose strength
crosses a threshold (default 0.5), and bounded so the auxiliary universe never
grows unbounded.
"""

from collections import defaultdict
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import TypedDict

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.storage.models.reference import Listing, UniverseMembership
from investment_agent.storage.models.relationship import CompanyRelationship
from investment_agent.storage.models.user_domain import AuxiliaryListing

log = structlog.get_logger(__name__)


class DerivationResult(TypedDict):
    added: int
    skipped_already_universe: int
    skipped_no_listing: int
    skipped_cap_reached: int
    current_total: int


async def derive_auxiliary_listings(
    session: AsyncSession,
    *,
    strength_threshold: float = 0.5,
    max_per_company: int = 5,
    total_cap: int = 200,
    today: date | None = None,
) -> DerivationResult:
    """Scan CompanyRelationship (open + strong); add up to ``max_per_company`` per
    source company as ``AuxiliaryListing`` rows. Skip counterparties that are
    already in any Universe today. Respect ``total_cap`` over the whole
    auxiliary set.
    """
    today = today or date.today()
    threshold = Decimal(str(strength_threshold))

    # 1. Pull strong open relationships, strongest first.
    rel_stmt = (
        select(CompanyRelationship)
        .where(CompanyRelationship.strength >= threshold)
        .where(CompanyRelationship.valid_to.is_(None))
        .order_by(CompanyRelationship.strength.desc())
    )
    relationships = list((await session.execute(rel_stmt)).scalars().all())

    # 2. Group by from_company_id, take top max_per_company by strength.
    by_source: dict[int, list[CompanyRelationship]] = defaultdict(list)
    for r in relationships:
        if len(by_source[r.from_company_id]) < max_per_company:
            by_source[r.from_company_id].append(r)

    # 3. Existing auxiliary set (for cap math + dedupe).
    aux_count_stmt = select(AuxiliaryListing).where(AuxiliaryListing.enabled.is_(True))
    existing_aux = list((await session.execute(aux_count_stmt)).scalars().all())
    existing_listing_ids: set[int] = {a.listing_id for a in existing_aux}
    current_total = len(existing_aux)

    # 4. Universe members today (any universe currently valid).
    uni_stmt = select(UniverseMembership.listing_id).where(
        UniverseMembership.valid_from <= today,
        (UniverseMembership.valid_to.is_(None))
        | (UniverseMembership.valid_to >= today),
    )
    universe_listing_ids: set[int] = {
        row for (row,) in (await session.execute(uni_stmt)).all()
    }

    added = 0
    skipped_already_universe = 0
    skipped_no_listing = 0
    skipped_cap_reached = 0
    now = datetime.now(UTC)

    for rels in by_source.values():
        for rel in rels:
            if current_total + added >= total_cap:
                skipped_cap_reached += 1
                continue

            # Find any Listing for this counterparty company.
            listing_stmt = (
                select(Listing)
                .where(Listing.company_id == rel.to_company_id)
                .limit(1)
            )
            listing = (await session.execute(listing_stmt)).scalar_one_or_none()
            if listing is None:
                skipped_no_listing += 1
                continue

            if listing.id in universe_listing_ids:
                skipped_already_universe += 1
                continue

            if listing.id in existing_listing_ids:
                # Already an aux row — leave it.
                continue

            session.add(
                AuxiliaryListing(
                    listing_id=listing.id,
                    added_via_relationship_id=rel.id,
                    added_by="derivation",
                    added_at=now,
                    enabled=True,
                )
            )
            existing_listing_ids.add(listing.id)
            added += 1

    await session.flush()

    result: DerivationResult = {
        "added": added,
        "skipped_already_universe": skipped_already_universe,
        "skipped_no_listing": skipped_no_listing,
        "skipped_cap_reached": skipped_cap_reached,
        "current_total": current_total + added,
    }
    log.info("auxiliary.derivation.complete", **result)
    return result
