"""HK/US test support shared by the hk-us-expansion tasks.

Task 01 owns this module: it seeds the three registry markets and the eight
pilot listings into a test session, so the later wave tasks (material
adapters, storage, jobs, read model) can reuse the same hand-checked
identities instead of re-declaring them. The identity authority itself is the
versioned list file loaded here, never a copy pasted into a test.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.domain.security import (
    DEFAULT_PILOT_LIST_PATH,
    MARKETS,
    ControlledIdentity,
    load_pilot_identities,
)
from investment_agent.storage.models.reference import Listing, Market
from investment_agent.storage.repositories.listing import ListingRepository

PILOT_LIST_PATH: Path = DEFAULT_PILOT_LIST_PATH
"""The checked-in pilot list (``config/research_material/pilot_list.yaml``)."""


def pilot_identities() -> tuple[ControlledIdentity, ...]:
    """The eight checked identities, validated on every load."""

    return load_pilot_identities(PILOT_LIST_PATH)


async def seed_hk_us_markets(session: AsyncSession) -> dict[str, Market]:
    """Create the HKEX/NASDAQ/NYSE rows from the registry; return them by code."""

    repository = ListingRepository(session)
    return {code: await repository.ensure_market(market) for code, market in MARKETS.items()}


async def seed_pilot_listings(session: AsyncSession) -> dict[tuple[str, str], Listing]:
    """Seed markets plus the eight pilot listings; return them by (market, ticker)."""

    await seed_hk_us_markets(session)
    return await ListingRepository(session).ensure_controlled_listings(pilot_identities())
