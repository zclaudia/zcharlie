"""Real temporary SQLite and typed account-fit fixtures for AF-P1 tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from investment_agent.investor.common import ObjectRef, Quantity
from investment_agent.investor.contracts import (
    Mandate,
    PortfolioConstraints,
    ResearchAuthorization,
)
from investment_agent.portfolio.calculation_contracts import TradingRule
from investment_agent.portfolio.calculations import TrustedTradingRule
from investment_agent.portfolio.contracts import Snapshot, SnapshotHolding
from investment_agent.portfolio.purposes import PurposeVersion
from investment_agent.portfolio.suitability_contracts import (
    FrozenAccountInputs,
    FrozenListingSource,
    FrozenPrice,
    FrozenSector,
    PurposeBinding,
    ValuationBinding,
)
from investment_agent.portfolio.valuation import Valuation, ValuationHolding
from investment_agent.storage.models import Base

AS_OF = datetime(2026, 10, 16, 4, tzinfo=UTC)


@asynccontextmanager
async def account_fit_sqlite(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    """Open a file-backed temporary SQLite session; never connects to configured DB."""

    database = tmp_path / "account_fit_p1_test.sqlite"
    engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            yield session
    finally:
        await engine.dispose()


def make_trusted_rule(listing_ref: ObjectRef, *, minimum: str = "100") -> TrustedTradingRule:
    return TrustedTradingRule(
        rule=TradingRule(
            listing_ref=listing_ref,
            source="synthetic_fixture",
            version="v1",
            nature="user_scenario",
            buy_minimum=Decimal(minimum),
            buy_increment=Decimal(minimum),
            sell_minimum=Decimal(minimum),
            sell_increment=Decimal(minimum),
            full_odd_lot_sell=False,
        ),
        market_code="SSE",
        valid_from=date(2020, 1, 1),
        available_from=AS_OF - timedelta(days=1),
        trusted_source="synthetic_fixture",
        source_version="v1",
    )


def make_inputs(
    *,
    target_listing: int = 600000,
    extra_listings: tuple[int, ...] = (),
    cash: str = "200000",
    holdings: tuple[tuple[int, str, str], ...] = (),
    prices: dict[int, str] | None = None,
    rules: tuple[int, ...] = (600000,),
    constraints: PortfolioConstraints | None = None,
    purpose_binding: PurposeBinding | None = None,
    purpose_version: PurposeVersion | None = None,
    with_valuation: bool = True,
    snapshot_coverage: Literal["complete", "partial"] = "complete",
) -> FrozenAccountInputs:
    account_id = UUID("00000000-0000-0000-0000-000000000101")
    snapshot_id = uuid4()
    refs = {target_listing, *extra_listings, *(listing for listing, _, _ in holdings)}
    listing_refs = {
        listing_id: ObjectRef(kind="listing", id=listing_id, revision=1) for listing_id in refs
    }
    snapshot_holdings = tuple(
        SnapshotHolding(
            listing_ref=listing_refs[listing_id],
            confirmed_quantity=Decimal(quantity),
            quantity_subtotal=Decimal(quantity),
            price=Decimal(price),
            price_ref=ObjectRef(kind="market_snapshot", id=listing_id + 9000000, revision=1),
            price_date=date(2026, 10, 15),
            market_value=Decimal(quantity) * Decimal(price),
            weight=None,
        )
        for listing_id, quantity, price in holdings
    )
    snapshot = Snapshot(
        id=snapshot_id,
        account_id=account_id,
        revision=1,
        recorded_at=AS_OF - timedelta(hours=1),
        fact_as_of=AS_OF - timedelta(hours=1),
        valuation_at=AS_OF - timedelta(hours=1),
        coverage=snapshot_coverage,
        asset_scope="synthetic complete CNY portfolio",
        known_cash=Decimal(cash),
        cash_subtotal=Decimal(cash),
        holdings=snapshot_holdings,
        known_subtotal=Decimal(cash)
        + sum((h.market_value or Decimal(0) for h in snapshot_holdings), Decimal(0)),
        total_assets=Decimal(cash)
        + sum((h.market_value or Decimal(0) for h in snapshot_holdings), Decimal(0)),
        unknowns=(),
        historical_unknowns=(),
        reconciliations=(),
        conflicts=(),
        source_event_ids=(),
        excluded_prior_event_ids=(),
        reference=ObjectRef(kind="portfolio_snapshot", id=snapshot_id, revision=1),
        price_coverage="complete",
    )
    mandate_id = uuid4()
    mandate = Mandate(
        id=mandate_id,
        schema_version=1,
        revision=1,
        created_at=AS_OF - timedelta(days=2),
        actor="synthetic_fixture",
        origin_ref=None,
        goals=(),
        scope_refs=(),
        hard_constraints=(),
        portfolio_constraints=constraints
        or PortfolioConstraints(account_id=account_id, max_single_weight=Decimal("1")),
        unknowns=(),
        authorization=ResearchAuthorization(
            daily_budget=Quantity(value=Decimal(1), unit="token"),
            per_task_budget=Quantity(value=Decimal(1), unit="token"),
            max_leads_per_batch=0,
            max_new_tasks_per_batch=0,
            source_refs=(),
        ),
    )
    valuation_id = uuid4()
    valuation_ref = ObjectRef(kind="portfolio_valuation", id=valuation_id, revision=1)
    valuation = None
    binding = ValuationBinding(state="missing", reason="valuation_missing")
    if with_valuation:
        valuation_holdings = tuple(
            ValuationHolding(
                listing_ref=listing_refs[listing_id],
                quantity=Decimal(quantity),
                price=Decimal(price),
                price_ref=ObjectRef(kind="market_snapshot", id=listing_id + 9000000, revision=1),
                price_date=date(2026, 10, 15),
                market_value=Decimal(quantity) * Decimal(price),
                weight=None,
            )
            for listing_id, quantity, price in holdings
        )
        total_assets = Decimal(cash) + sum(
            (h.market_value or Decimal(0) for h in valuation_holdings), Decimal(0)
        )
        valuation = Valuation(
            id=valuation_id,
            account_id=account_id,
            snapshot_id=snapshot_id,
            ledger_revision=1,
            valuation_date=date(2026, 10, 15),
            knowledge_cutoff=AS_OF - timedelta(minutes=30),
            algorithm_version="daily_valuation_v1",
            input_digest="a" * 64,
            coverage="complete" if snapshot_coverage == "complete" else "partial",
            known_cash=Decimal(cash),
            holdings=valuation_holdings,
            market_value_subtotal=total_assets - Decimal(cash),
            total_assets=total_assets,
            weight_available=True,
            unknowns=(),
            created_at=AS_OF - timedelta(minutes=20),
            reference=valuation_ref,
        )
        binding = ValuationBinding(state="available", valuation_ref=valuation_ref)
    source_prices = prices or {target_listing: "1500"}
    sources = []
    for listing_id in sorted(refs):
        ref = listing_refs[listing_id]
        price_value = source_prices.get(listing_id)
        frozen_price = FrozenPrice(
            listing_ref=ref,
            price=Decimal(price_value) if price_value is not None else None,
            price_ref=ObjectRef(kind="market_snapshot", id=listing_id + 9000000, revision=1)
            if price_value is not None
            else None,
            price_date=date(2026, 10, 15) if price_value is not None else None,
            provider="synthetic_fixture" if price_value is not None else None,
            captured_at=AS_OF - timedelta(minutes=30) if price_value is not None else None,
            available_at=AS_OF - timedelta(minutes=30) if price_value is not None else None,
            acquired_at=AS_OF,
            unknown_reason=None if price_value is not None else "synthetic_price_missing",
        )
        sector = FrozenSector(
            listing_ref=ref,
            row_id=listing_id + 8000000,
            code="C39",
            name="synthetic technology sector",
            valid_from=date(2020, 1, 1),
            valid_to=None,
            source="synthetic_fixture",
            captured_at=AS_OF - timedelta(days=1),
            available_at=AS_OF - timedelta(days=1),
            acquired_at=AS_OF,
        )
        trusted = make_trusted_rule(ref) if listing_id in rules else None
        sources.append(
            FrozenListingSource(
                listing_ref=ref,
                market_code="SSE",
                name=f"Synthetic {listing_id}",
                ticker=str(listing_id),
                listing_source_ref=ref,
                price=frozen_price,
                sector=sector,
                trusted_rule=trusted,
                rule_unknown_reason=None if trusted else "synthetic_rule_unavailable",
            )
        )
    return FrozenAccountInputs(
        account_id=account_id,
        snapshot=snapshot,
        valuation=valuation,
        valuation_binding=binding,
        mandate=mandate,
        purpose_version=purpose_version,
        purpose_binding=purpose_binding or PurposeBinding(state="none_recorded"),
        listing_sources=tuple(sources),
        assessment_as_of=AS_OF,
        price_basis_at=valuation.knowledge_cutoff if valuation else None,
        mode="live",
        manifest_hash="a" * 64,
        source_fingerprint="b" * 64,
    )
