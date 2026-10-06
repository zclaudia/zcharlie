"""Listing repository."""

from sqlalchemy import select

from investment_agent.domain.a_share import AShareMarket, parse_a_share_ticker
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

    async def ensure_a_share_market(self, market: AShareMarket) -> Market:
        existing = await self.session.get(Market, market.market_code)
        if existing is not None:
            return existing
        created = Market(
            code=market.market_code,
            name=market.name,
            timezone=market.timezone,
            currency=market.currency,
        )
        self.session.add(created)
        await self.session.flush()
        return created

    async def get_a_share_listing(self, ticker: str) -> Listing | None:
        """Look up a listing by canonical A-share ticker (``600519.SH``)."""

        parsed = parse_a_share_ticker(ticker)
        if parsed is None:
            raise ValueError(f"not a canonical A-share ticker: {ticker!r}")
        symbol, market = parsed
        return await self.get_by_ticker(market.market_code, symbol)

    async def ensure_a_share_listing(
        self, ticker: str, *, company_name: str | None = None
    ) -> Listing:
        """Return the listing for a canonical A-share ticker, creating it if absent.

        Creates the exchange ``market`` row and the company on first sight; an
        existing listing is returned unchanged. Without a ``company_name`` the
        ticker itself names the new company.
        """

        parsed = parse_a_share_ticker(ticker)
        if parsed is None:
            raise ValueError(f"not a canonical A-share ticker: {ticker!r}")
        symbol, market = parsed
        existing = await self.get_by_ticker(market.market_code, symbol)
        if existing is not None:
            return existing
        await self.ensure_a_share_market(market)
        return await self.upsert_listing(
            market_code=market.market_code,
            ticker=symbol,
            company_name=company_name or f"{symbol}.{market.suffix}",
        )
