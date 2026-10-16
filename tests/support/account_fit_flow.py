"""Persist synthetic inputs for real HTTP/worker tests, never production data."""

from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.portfolio.contracts import Account
from investment_agent.portfolio.suitability_contracts import FrozenAccountInputs
from investment_agent.storage.models.investor import MandateHead, MandateRevision
from investment_agent.storage.models.pit_snapshot import IndustryClassification, MarketSnapshot
from investment_agent.storage.models.portfolio import (
    PortfolioAccount,
    PortfolioSnapshot,
    PortfolioValuation,
)
from investment_agent.storage.models.reference import Company, Listing, Market


async def persist_inputs(session: AsyncSession, inputs: FrozenAccountInputs) -> None:
    session.add(Market(code="SSE", name="Synthetic market", timezone="Asia/Shanghai", currency="CNY"))
    for source in inputs.listing_sources:
        company = Company(canonical_name=source.name)
        session.add(company)
        await session.flush()
        session.add(Listing(id=int(source.listing_ref.id), market_code="SSE", company_id=company.id, ticker=source.ticker))
        if source.price.price is not None:
            price = source.price
            assert price.price_ref is not None
            session.add(MarketSnapshot(id=int(price.price_ref.id), listing_id=int(source.listing_ref.id), trade_date=price.price_date, captured_at=price.captured_at, adjust="none", provider=price.provider, close=price.price, open=price.price, high=price.price, low=price.price, volume=Decimal(1), version=price.price_ref.revision))
        sector = source.sector
        if sector.name is not None:
            session.add(IndustryClassification(id=sector.row_id, listing_id=int(source.listing_ref.id), system="CSRC", code=sector.code, name=sector.name, valid_from=sector.valid_from, valid_to=sector.valid_to, source=sector.source, captured_at=sector.captured_at))
    account = Account(id=inputs.account_id, name="Synthetic CNY account", currency="CNY", revision=inputs.snapshot.revision, current_snapshot_id=inputs.snapshot.id)
    session.add(PortfolioAccount(id=str(account.id), owner_id="local_owner", revision=account.revision, current_snapshot_id=str(account.current_snapshot_id), current_valuation_id=str(inputs.valuation.id) if inputs.valuation else None, input_hash="a" * 64, payload=account.model_dump(mode="json")))
    session.add(PortfolioSnapshot(id=str(inputs.snapshot.id), account_id=str(account.id), revision=inputs.snapshot.revision, payload=inputs.snapshot.model_dump(mode="json")))
    session.add(MandateHead(owner_id="local_owner", mandate_id=str(inputs.mandate.id), revision=inputs.mandate.revision))
    session.add(MandateRevision(mandate_id=str(inputs.mandate.id), revision=inputs.mandate.revision, owner_id="local_owner", payload=inputs.mandate.model_dump(mode="json")))
    if inputs.valuation is not None:
        valuation = inputs.valuation
        session.add(PortfolioValuation(id=str(valuation.id), account_id=str(account.id), snapshot_id=str(inputs.snapshot.id), ledger_revision=inputs.snapshot.revision, valuation_date=valuation.valuation_date, algorithm_version=valuation.algorithm_version, input_digest=valuation.input_digest, payload=valuation.model_dump(mode="json")))
    await session.flush()
