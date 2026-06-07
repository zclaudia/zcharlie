"""Seed NASDAQ universe and bootstrap constituents from YAML."""

from datetime import date
from pathlib import Path

import yaml

from investment_agent.storage.database import session_scope
from investment_agent.storage.models.reference import (
    Market,
    Sector,
    Universe,
    UniverseMembership,
)
from investment_agent.storage.repositories.listing import ListingRepository


_DEFAULT_SECTORS = [
    ("TECH", "Information Technology"),
    ("COMMUNICATION", "Communication Services"),
    ("CONSUMER_DISCRETIONARY", "Consumer Discretionary"),
    ("CONSUMER_STAPLES", "Consumer Staples"),
    ("HEALTHCARE", "Health Care"),
    ("INDUSTRIALS", "Industrials"),
    ("MATERIALS", "Materials"),
    ("ENERGY", "Energy"),
    ("FINANCIALS", "Financials"),
    ("UTILITIES", "Utilities"),
    ("REAL_ESTATE", "Real Estate"),
]


async def seed_nasdaq_100(yaml_path: Path) -> int:
    with yaml_path.open() as f:
        data = yaml.safe_load(f)

    async with session_scope() as session:
        # Markets
        if await session.get(Market, "NASDAQ") is None:
            session.add(
                Market(
                    code="NASDAQ",
                    name="NASDAQ Stock Market",
                    timezone="America/New_York",
                    currency="USD",
                )
            )
        if await session.get(Market, "NYSE") is None:
            session.add(
                Market(
                    code="NYSE",
                    name="New York Stock Exchange",
                    timezone="America/New_York",
                    currency="USD",
                )
            )
        # Sectors
        for code, name in _DEFAULT_SECTORS:
            if await session.get(Sector, code) is None:
                session.add(Sector(code=code, name=name))
        # Universe
        if await session.get(Universe, data["code"]) is None:
            session.add(
                Universe(
                    code=data["code"],
                    name=data["name"],
                    description=data.get("description"),
                )
            )
        await session.flush()

        # Constituents
        repo = ListingRepository(session)
        n = 0
        for c in data["constituents"]:
            listing = await repo.upsert_listing(
                market_code=data["market"],
                ticker=c["ticker"],
                company_name=c["company_name"],
                sector_code=c.get("sector_code"),
            )
            membership = await session.get(
                UniverseMembership,
                (data["code"], listing.id, date(2024, 1, 1)),
            )
            if membership is None:
                session.add(
                    UniverseMembership(
                        universe_code=data["code"],
                        listing_id=listing.id,
                        valid_from=date(2024, 1, 1),
                    )
                )
                n += 1
        return n
