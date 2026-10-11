"""Listing repository."""

from collections.abc import Mapping, Sequence

from sqlalchemy import select

from investment_agent.domain.a_share import AShareMarket, parse_a_share_ticker
from investment_agent.domain.security import ControlledIdentity, SecurityMarket
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

    async def ensure_market(self, market: SecurityMarket) -> Market:
        """Create or re-stamp the ``Market`` row from its registry entry.

        ``name`` / ``timezone`` / ``currency`` come from the registry row, so a
        repeated call neither adds a row nor leaves a stale value behind.
        ``Market.currency`` stays the market's default quote currency; a
        security's reporting currency is not stored here.
        """

        existing = await self.session.get(Market, market.code)
        if existing is None:
            existing = Market(
                code=market.code,
                name=market.name,
                timezone=market.timezone,
                currency=market.quote_currency,
            )
            self.session.add(existing)
        else:
            existing.name = market.name
            existing.timezone = market.timezone
            existing.currency = market.quote_currency
        await self.session.flush()
        return existing

    async def ensure_controlled_listings(
        self, identities: Sequence[ControlledIdentity]
    ) -> dict[tuple[str, str], Listing]:
        """Return ``(market_code, ticker)`` -> listing for controlled identities.

        ``company_key`` is the only token that lets two identities share one
        ``Company`` (the checked cross-market case, e.g. one issuer listed on
        two exchanges). Everything else is resolved conservatively:

        1) identities sharing a ``company_key`` group together and reuse one
           company -- the company of the group's existing listings, else an
           exact ``canonical_name`` match;
        2) a name is reused only when exactly one ``Company`` row carries it
           and this call has not already made a different one;
        3) two companies across the group's listings, or two same-named
           companies, raise ``ValueError`` (naming market/ticker/company_name)
           instead of merging or re-pointing any existing row;
        4) matching is by exact name only -- similar Chinese/English names and
           similar tickers never merge.

        Refusals are detected before anything is written, so a rejected call
        leaves every existing listing and company untouched. Callers seed the
        markets first (``ensure_market``); an unknown market raises
        ``ValueError`` like :meth:`upsert_listing`.
        """

        grouped: dict[str, list[ControlledIdentity]] = {}
        for identity in identities:
            grouped.setdefault(identity.company_key, []).append(identity)

        # Pass 1: look at existing rows only, so a refusal writes nothing.
        existing: dict[tuple[str, str], Listing] = {}
        owners: dict[tuple[str, str], str] = {}
        resolved: dict[str, Company | None] = {}
        for company_key, group in grouped.items():
            for identity in group:
                if await self.session.get(Market, identity.market_code) is None:
                    raise ValueError(f"Unknown market: {identity.market_code}")
                key = (identity.market_code, identity.ticker)
                other_key = owners.setdefault(key, company_key)
                if other_key != company_key:
                    raise ValueError(
                        f"{identity.market_code}/{identity.ticker} "
                        f"({identity.company_name}): the same listing is claimed "
                        f"by company keys {other_key!r} and {company_key!r}"
                    )
                listing = await self.get_by_ticker(*key)
                if listing is not None:
                    existing[key] = listing
            resolved[company_key] = await self._resolve_group_company(
                company_key, group, existing
            )

        # Pass 2: create the missing companies and listings.
        result: dict[tuple[str, str], Listing] = {}
        for company_key, group in grouped.items():
            company = resolved[company_key]
            if company is None:
                company = Company(canonical_name=group[0].company_name)
                self.session.add(company)
                await self.session.flush()
            for identity in group:
                key = (identity.market_code, identity.ticker)
                listing = existing.get(key)
                if listing is None:
                    listing = Listing(
                        company_id=company.id, market_code=key[0], ticker=key[1]
                    )
                    self.session.add(listing)
                    await self.session.flush()
                    existing[key] = listing
                result[key] = listing
        return result

    async def _resolve_group_company(
        self,
        company_key: str,
        group: Sequence[ControlledIdentity],
        existing: Mapping[tuple[str, str], Listing],
    ) -> Company | None:
        """The company a ``company_key`` group attaches to (``None`` = create)."""

        group_listings = [
            listing
            for identity in group
            if (listing := existing.get((identity.market_code, identity.ticker)))
            is not None
        ]
        if group_listings:
            company_id = group_listings[0].company_id
            for identity in group:
                listing = existing.get((identity.market_code, identity.ticker))
                if listing is not None and listing.company_id != company_id:
                    raise ValueError(
                        f"{identity.market_code}/{identity.ticker} "
                        f"({identity.company_name}): existing listing belongs to "
                        f"company {listing.company_id}, while company_key "
                        f"{company_key!r} already lists company {company_id}; "
                        "refusing to re-point an existing listing"
                    )
            return await self.session.get(Company, company_id)

        name = group[0].company_name
        stmt = select(Company).where(Company.canonical_name == name)
        rows = list((await self.session.execute(stmt)).scalars())
        if len(rows) > 1:
            first = group[0]
            raise ValueError(
                f"{first.market_code}/{first.ticker} ({name}): {len(rows)} "
                f"companies share canonical_name {name!r}; refusing to merge by "
                "name"
            )
        return rows[0] if rows else None
