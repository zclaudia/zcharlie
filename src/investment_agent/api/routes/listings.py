"""/listings endpoints."""

from datetime import UTC, date, datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select

from investment_agent.api.auth import verify_api_key
from investment_agent.api.schemas.listing import (
    ListingOut,
    ListingScoreOut,
    RelationshipCreate,
    RelationshipOut,
)
from investment_agent.relationships.resolver import resolve_counterparty
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.analysis import StockScore
from investment_agent.storage.models.reference import Company, Listing
from investment_agent.storage.models.relationship import CompanyRelationship

router = APIRouter(
    prefix="/listings",
    tags=["listings"],
    dependencies=[Depends(verify_api_key)],
)


@router.get("/{ticker}", response_model=ListingOut)
async def get_listing(ticker: str, market: str = "NASDAQ") -> ListingOut:
    async with session_scope() as session:
        listing = (
            await session.execute(
                select(Listing).where(
                    Listing.market_code == market,
                    Listing.ticker == ticker,
                ),
            )
        ).scalar_one_or_none()
        if listing is None:
            raise HTTPException(
                status_code=404,
                detail=f"Listing not found: {market}/{ticker}",
            )
        company = await session.get(Company, listing.company_id)
        return ListingOut(
            listing_id=listing.id,
            company_id=listing.company_id,
            ticker=listing.ticker,
            market_code=listing.market_code,
            company_name=company.canonical_name if company else "",
            sector_code=company.sector_code if company else None,
        )


@router.get("/{ticker}/scores", response_model=list[ListingScoreOut])
async def get_listing_scores(
    ticker: str,
    market: str = "NASDAQ",
    limit: int = 20,
) -> list[ListingScoreOut]:
    async with session_scope() as session:
        listing = (
            await session.execute(
                select(Listing).where(
                    Listing.market_code == market,
                    Listing.ticker == ticker,
                ),
            )
        ).scalar_one_or_none()
        if listing is None:
            raise HTTPException(status_code=404, detail="Listing not found")
        stmt = (
            select(StockScore)
            .where(StockScore.listing_id == listing.id)
            .order_by(StockScore.id.desc())
            .limit(limit)
        )
        rows = list((await session.execute(stmt)).scalars().all())
        return [
            ListingScoreOut(
                pipeline_run_id=s.pipeline_run_id,
                strategy_code=s.strategy_code,
                tier=s.tier,
                score=s.score,
                confidence=float(s.confidence),
                bull_thesis=s.bull_thesis,
                bear_thesis=s.bear_thesis,
            )
            for s in rows
        ]


@router.get("/{ticker}/relationships", response_model=list[RelationshipOut])
async def list_relationships(
    ticker: str,
    market: str = "NASDAQ",
) -> list[RelationshipOut]:
    async with session_scope() as session:
        listing = (
            await session.execute(
                select(Listing).where(
                    Listing.market_code == market,
                    Listing.ticker == ticker,
                ),
            )
        ).scalar_one_or_none()
        if listing is None:
            raise HTTPException(status_code=404, detail="Listing not found")
        stmt = (
            select(CompanyRelationship)
            .where(CompanyRelationship.from_company_id == listing.company_id)
            .where(CompanyRelationship.valid_to.is_(None))
            .order_by(CompanyRelationship.strength.desc())
        )
        rels = list((await session.execute(stmt)).scalars().all())
        out: list[RelationshipOut] = []
        for rel in rels:
            counterparty = await session.get(Company, rel.to_company_id)
            cp_listing_stmt = (
                select(Listing)
                .where(Listing.company_id == rel.to_company_id)
                .limit(1)
            )
            cp_listing = (
                await session.execute(cp_listing_stmt)
            ).scalar_one_or_none()
            out.append(
                RelationshipOut(
                    id=rel.id,
                    from_company_id=rel.from_company_id,
                    to_company_id=rel.to_company_id,
                    counterparty_name=(
                        counterparty.canonical_name if counterparty else None
                    ),
                    counterparty_ticker=cp_listing.ticker if cp_listing else None,
                    relationship_type=rel.relationship_type,
                    strength=float(rel.strength),
                    confidence=float(rel.confidence),
                    source=rel.source,
                    valid_from=rel.valid_from,
                    valid_to=rel.valid_to,
                ),
            )
        return out


@router.post(
    "/{ticker}/relationships",
    response_model=RelationshipOut,
    status_code=201,
)
async def add_relationship(
    ticker: str,
    body: RelationshipCreate,
    market: str = "NASDAQ",
) -> RelationshipOut:
    async with session_scope() as session:
        listing = (
            await session.execute(
                select(Listing).where(
                    Listing.market_code == market,
                    Listing.ticker == ticker,
                ),
            )
        ).scalar_one_or_none()
        if listing is None:
            raise HTTPException(status_code=404, detail="Listing not found")
        to_id = await resolve_counterparty(
            session,
            body.counterparty_name,
            body.counterparty_ticker,
        )
        if to_id == listing.company_id:
            raise HTTPException(
                status_code=400,
                detail="Cannot relate company to itself",
            )
        now = datetime.now(UTC)
        rel = CompanyRelationship(
            from_company_id=listing.company_id,
            to_company_id=to_id,
            relationship_type=body.relationship_type,
            strength=Decimal(str(body.strength)),
            description=body.description,
            source="manual",
            confidence=Decimal("0.9"),
            valid_from=date.today(),
            discovered_at=now,
            last_verified_at=now,
        )
        session.add(rel)
        await session.flush()
        counterparty = await session.get(Company, to_id)
        cp_listing_stmt = (
            select(Listing).where(Listing.company_id == to_id).limit(1)
        )
        cp_listing = (
            await session.execute(cp_listing_stmt)
        ).scalar_one_or_none()
        return RelationshipOut(
            id=rel.id,
            from_company_id=rel.from_company_id,
            to_company_id=rel.to_company_id,
            counterparty_name=(
                counterparty.canonical_name if counterparty else None
            ),
            counterparty_ticker=cp_listing.ticker if cp_listing else None,
            relationship_type=rel.relationship_type,
            strength=float(rel.strength),
            confidence=float(rel.confidence),
            source=rel.source,
            valid_from=rel.valid_from,
            valid_to=rel.valid_to,
        )
