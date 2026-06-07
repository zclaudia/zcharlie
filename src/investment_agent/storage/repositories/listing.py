"""Listing repository."""

from sqlalchemy import select

from investment_agent.storage.models.reference import Company, Listing, Market
from investment_agent.storage.repositories.base import Repository


class ListingRepository(Repository[Listing]):
    model = Listing

    async def get_by_ticker(self, market_code: str, ticker: str) -> Listing | None:
        stmt = select(Listing).where(
            Listing.market_code == market_code, Listing.ticker == ticker
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def upsert_listing(
        self,
        *,
        market_code: str,
        ticker: str,
        company_name: str,
        sector_code: str | None = None,
    ) -> Listing:
        # 1. ensure market exists
        market = await self.session.get(Market, market_code)
        if market is None:
            raise ValueError(f"Unknown market: {market_code}")

        # 2. ensure company exists (match by canonical_name)
        stmt = select(Company).where(Company.canonical_name == company_name)
        company = (await self.session.execute(stmt)).scalar_one_or_none()
        if company is None:
            company = Company(canonical_name=company_name, sector_code=sector_code)
            self.session.add(company)
            await self.session.flush()

        # 3. ensure listing exists
        listing = await self.get_by_ticker(market_code, ticker)
        if listing is None:
            listing = Listing(
                company_id=company.id, market_code=market_code, ticker=ticker
            )
            self.session.add(listing)
            await self.session.flush()
        return listing
