"""Idempotent, offline setup and import of the checked HK/US pilot list."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.domain.security import (
    MARKETS,
    SOURCE_YAHOO_CHART,
    ControlledIdentity,
    validate_pilot_list,
)
from investment_agent.materials.handlers import default_adapters
from investment_agent.materials.offline_import import ImportReport, import_offline_evidence
from investment_agent.storage.models.reference import Listing
from investment_agent.storage.models.research_material import ResearchSourceBinding
from investment_agent.storage.repositories.listing import ListingRepository
from investment_agent.storage.repositories.research_material import ResearchSourceBindingRepository


@dataclass(frozen=True)
class PilotSetup:
    listings: dict[tuple[str, str], Listing]
    company_ids: dict[str, int]
    bindings_created: int


async def initialize_pilot(
    session: AsyncSession, identities: Sequence[ControlledIdentity]
) -> PilotSetup:
    """Register checked markets/listings and source bindings without rewriting identities.

    Refusals roll back this entire setup, including new markets and listings.
    The caller owns the outer transaction/commit. Existing material is untouched.
    """

    validate_pilot_list(identities)
    async with session.begin_nested():
        listings_repo = ListingRepository(session)
        for market_code in sorted({identity.market_code for identity in identities}):
            await listings_repo.ensure_market(MARKETS[market_code])
        listings = await listings_repo.ensure_controlled_listings(identities)
        company_ids: dict[str, int] = {}
        bindings = ResearchSourceBindingRepository(session)
        created = 0
        for identity in identities:
            listing = listings[(identity.market_code, identity.ticker)]
            company_ids[identity.company_key] = listing.company_id
            for source in identity.sources:
                subject_kind = "listing" if source.source == SOURCE_YAHOO_CHART else "company"
                subject_id = listing.id if subject_kind == "listing" else listing.company_id
                existing = [
                    row for row in await bindings.list_for(subject_kind, subject_id)
                    if row.source == source.source
                ]
                exact = [
                    row for row in existing
                    if row.source_symbol == source.symbol
                    and row.valid_from == identity.valid_from
                    and row.valid_to == identity.valid_to
                ]
                if exact:
                    continue
                # Source bindings are immutable. Changed mappings need an explicit
                # reviewed transition, never an automatic overwrite by this command.
                if existing:
                    raise ValueError(
                        f"{identity.company_key}/{source.source}: existing source binding "
                        "differs from the checked list; refusing to rewrite it"
                    )
                session.add(ResearchSourceBinding(
                    subject_kind=subject_kind,
                    subject_id=subject_id,
                    source=source.source,
                    source_symbol=source.symbol,
                    valid_from=identity.valid_from,
                    valid_to=identity.valid_to,
                    verified_at=datetime.now(UTC),
                    verified_by="pilot_list:init",
                    payload={"company_key": identity.company_key, "note": source.note},
                ))
                await session.flush()
                created += 1
    return PilotSetup(listings=listings, company_ids=company_ids, bindings_created=created)


async def import_pilot_materials(
    session: AsyncSession,
    *,
    identities: Sequence[ControlledIdentity],
    company_ids: dict[str, int],
    source_dir: Path,
    data_dir: Path,
) -> ImportReport:
    """Replay local files through the real, bound adapters; never fetch upstream."""

    reports: list[ImportReport] = []
    for identity in identities:
        adapters = default_adapters()
        bound = {binding.source for binding in identity.sources}
        reports.append(await import_offline_evidence(
            session,
            data_dir=data_dir,
            source_dir=source_dir,
            identities=(identity,),
            company_ids=company_ids,
            adapters={source: adapter for source, adapter in adapters.items() if source in bound},
        ))
    return ImportReport(
        saved=tuple(item for report in reports for item in report.saved),
        problems=tuple(item for report in reports for item in report.problems),
        skipped=tuple(item for report in reports for item in report.skipped),
        informational=tuple(item for report in reports for item in report.informational),
    )
