"""Synthetic AG22 goldens produced through original save/get services only."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.investor.common import ObjectRef
from investment_agent.investor.contracts import Mandate, PortfolioConstraints, ResearchAuthorization
from investment_agent.investor.foundations import InvestorService
from investment_agent.portfolio.calculation_contracts import (
    PortfolioCalculationRequest,
    PriceSelection,
    Scenario,
    ScenarioAction,
    TradingRule,
)
from investment_agent.portfolio.calculations import PortfolioCalculationService, TrustedTradingRule
from investment_agent.portfolio.contracts import AppendEvent, Balance, CreateAccount, Holding
from investment_agent.portfolio.service import PortfolioService
from investment_agent.portfolio.stress_canonical import canonical_hash
from investment_agent.portfolio.valuation import ValuationService
from investment_agent.storage.models.pit_snapshot import IndustryClassification, MarketSnapshot
from investment_agent.storage.models.reference import Company, Listing, Market

ROOT = Path(__file__).resolve().parents[1] / "fixtures/portfolio_stress"
FACT_AT = datetime(2026, 11, 5, 9, 30, tzinfo=UTC)
VALUATION_AT = datetime(2026, 11, 6, 9, 30, tzinfo=UTC)
RECORDED_AT = datetime(2026, 11, 7, 9, 30, tzinfo=UTC)
ACCOUNT_ID = UUID(int=200)
LISTING = ObjectRef(kind="listing", id=18, revision=1)
NEW_LISTING = ObjectRef(kind="listing", id=19, revision=1)


class FixtureClock(datetime):
    instant = RECORDED_AT

    @classmethod
    def now(cls, tz=None):
        return cls.instant.astimezone(tz) if tz else cls.instant.replace(tzinfo=None)


def fixture_rule(ref: ObjectRef) -> TradingRule:
    return TradingRule(
        listing_ref=ref,
        source="synthetic local rule; not exchange certification",
        version="synthetic-rule-v1",
        nature="user_scenario",
        buy_minimum=Decimal(1),
        buy_increment=Decimal(1),
        sell_minimum=Decimal(1),
        sell_increment=Decimal(1),
        full_odd_lot_sell=True,
    )


class FixtureRules:
    async def resolve(self, listing_ref: ObjectRef, as_of: datetime) -> TrustedTradingRule:
        return TrustedTradingRule(
            rule=fixture_rule(listing_ref),
            market_code="SSE",
            valid_from=date(2026, 1, 1),
            available_from=FACT_AT - timedelta(days=30),
            trusted_source="synthetic test seam; no provider call",
            source_version="fixture-v1",
        )


def price(listing_id: int, value: str, at: datetime, *, version: int = 1) -> MarketSnapshot:
    return MarketSnapshot(
        listing_id=listing_id,
        trade_date=at.date(),
        open=Decimal(value),
        high=Decimal(value),
        low=Decimal(value),
        close=Decimal(value),
        volume=Decimal(1),
        adjust="none",
        provider="synthetic-offline",
        captured_at=at,
        version=version,
    )


async def build_ag22_goldens(session: AsyncSession) -> dict[str, dict]:
    """Use a fresh temporary SQLite session; all source rows are synthetic.

    Only the clock/UUID/rule seams are fixed. Ledger projection, immutable
    valuation, mandate, AG22 calculation and owner-scoped reads remain real.
    """
    import investment_agent.investor.foundations as foundations
    import investment_agent.portfolio.calculations as calculations
    import investment_agent.portfolio.projection as projection
    import investment_agent.portfolio.service as ledger_module
    import investment_agent.portfolio.valuation as valuation_module

    session.add_all(
        [
            Market(code="SSE", name="Synthetic Shanghai", timezone="Asia/Shanghai", currency="CNY"),
            Company(id=18, canonical_name="Synthetic A"),
            Company(id=19, canonical_name="Synthetic B"),
            Listing(id=18, company_id=18, market_code="SSE", ticker="600036"),
            Listing(id=19, company_id=19, market_code="SSE", ticker="600000"),
            price(18, "10", FACT_AT),
        ]
    )
    await session.flush()
    with pytest.MonkeyPatch.context() as patch:
        for module in (foundations, calculations, ledger_module, valuation_module):
            patch.setattr(module, "datetime", FixtureClock)
        patch.setattr(ledger_module, "uuid4", lambda: UUID(int=201))
        patch.setattr(projection, "uuid4", lambda: UUID(int=202))
        patch.setattr(foundations, "uuid4", lambda: UUID(int=203))
        patch.setattr(valuation_module, "uuid4", lambda: UUID(int=204))
        FixtureClock.instant = FACT_AT
        ledger = PortfolioService(session)
        account = await ledger.create_account(
            CreateAccount(idempotency_key=ACCOUNT_ID, name="Synthetic fixture account")
        )
        appended = await ledger.append(
            AppendEvent(
                account_id=account.id,
                expected_revision=0,
                idempotency_key=UUID(int=205),
                fact=Balance(
                    kind="opening_balance",
                    occurred_at=FACT_AT,
                    source="synthetic confirmed statement",
                    user_confirmed=True,
                    coverage="complete",
                    asset_scope="synthetic local test account only",
                    holdings=(
                        Holding(
                            listing_ref=LISTING, quantity=Decimal(100), average_cost=Decimal(8)
                        ),
                    ),
                    cash=Decimal(1000),
                    prior_history="included_in_confirmed_balance",
                ),
            )
        )
        await session.flush()
        snapshot = await ledger.snapshot(account.id, snapshot_id=appended.snapshot.id, revision=1)
        assert snapshot == appended.snapshot
        FixtureClock.instant = RECORDED_AT
        session.add_all([price(18, "12", VALUATION_AT), price(19, "5", VALUATION_AT)])
        for ref in (LISTING, NEW_LISTING):
            for system in ("CSRC", "csrc"):
                session.add(
                    IndustryClassification(
                        listing_id=ref.id,
                        system=system,
                        code="J66",
                        name="Synthetic sector",
                        valid_from=date(2026, 1, 1),
                        source="synthetic-offline",
                        captured_at=FACT_AT,
                    )
                )
        await session.flush()
        candidate = Mandate(
            id=UUID(int=206),
            revision=1,
            created_at=FACT_AT,
            actor="synthetic_input",
            origin_ref=None,
            goals=(),
            scope_refs=(LISTING, NEW_LISTING),
            hard_constraints=(),
            unknowns=(),
            portfolio_constraints=PortfolioConstraints(
                account_id=account.id,
                max_single_weight=Decimal("0.8"),
                max_sector_weight=Decimal("0.9"),
                minimum_cash=Decimal(0),
            ),
            authorization=ResearchAuthorization(
                max_leads_per_batch=0, max_new_tasks_per_batch=0, source_refs=()
            ),
        )
        mandate = await InvestorService(session).create_mandate(
            "local_owner", candidate, UUID(int=207)
        )
        valuation = await ValuationService(session).ensure(
            account.id,
            valuation_date=VALUATION_AT.date(),
            now=VALUATION_AT,
            is_trading_day=lambda _day: True,
        )
        assert valuation is not None
        assert await ValuationService(session).get(account.id, valuation.id) == valuation
        new_price = await session.get(MarketSnapshot, 3)
        assert new_price is not None and new_price.listing_id == NEW_LISTING.id

        def action(ref, direction, quantity, fee):
            return ScenarioAction(
                listing_ref=ref,
                direction=direction,
                quantity=Decimal(quantity),
                execution_price=Decimal("12" if ref == LISTING else "5"),
                fixed_fee=Decimal(fee) if fee is not None else None,
                fee_rate=Decimal(0),
                assumption_source="synthetic user scenario",
                nature="user_scenario",
            )

        request = PortfolioCalculationRequest(
            operation_id=UUID(int=208),
            account_id=account.id,
            snapshot_ref=snapshot.reference,
            valuation_ref=valuation.reference,
            mandate_ref=ObjectRef(kind="mandate", id=mandate.id, revision=mandate.revision),
            prices=(
                PriceSelection(
                    listing_ref=NEW_LISTING,
                    price_ref=ObjectRef(
                        kind="market_snapshot", id=new_price.id, revision=new_price.version
                    ),
                ),
            ),
            scenarios=(
                Scenario(name="sell_with_fee", actions=(action(LISTING, "sell", "10", "1"),)),
                Scenario(name="new_holding", actions=(action(NEW_LISTING, "buy", "5", "2"),)),
                Scenario(name="missing_fee", actions=(action(LISTING, "sell", "1", None),)),
            ),
        )
        service = PortfolioCalculationService(session, rules=FixtureRules())
        saved = await service.save(request)
        await session.commit()
        read = await service.get(account.id, saved.id)
        assert read == saved and read.method_version == "ag22-v2"
        legacy_request = PortfolioCalculationRequest.model_validate(
            {
                **request.model_dump(mode="json"),
                "operation_id": str(UUID(int=209)),
                "valuation_ref": None,
                "prices": [],
                "scenarios": [request.scenarios[0].model_dump(mode="json")],
            }
        )
        legacy = await service.save(legacy_request)
        await session.commit()
        assert await service.get(account.id, legacy.id) == legacy
        common = {
            "synthetic": True,
            "owner_id": "local_owner",
            "created_via": [
                "PortfolioService.create_account/append/snapshot",
                "InvestorService.create_mandate",
                "ValuationService.ensure/get",
                "PortfolioCalculationService.save/get",
            ],
            "account": account.model_dump(mode="json"),
            "event": appended.event.model_dump(mode="json"),
            "snapshot": snapshot.model_dump(mode="json"),
            "valuation": valuation.model_dump(mode="json"),
            "mandate": mandate.model_dump(mode="json"),
        }
        return {
            "complete": {
                **common,
                "request": request.model_dump(mode="json"),
                "calculation": read.model_dump(mode="json"),
            },
            "legacy_v1": {
                "synthetic": True,
                "created_via": "PortfolioCalculationService.save/get",
                "supported_for_stress": False,
                "reason": "unsupported_basis",
                "calculation": legacy.model_dump(mode="json"),
            },
        }


def write_goldens(values: dict[str, dict], directory: Path = ROOT) -> None:
    """Explicit maintenance action, never called by ordinary tests."""
    destination = directory / "ag22_v2"
    destination.mkdir(parents=True, exist_ok=True)
    entries = []
    for name, value in sorted(values.items()):
        path = destination / f"{name}.json"
        path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
        entries.append(
            {
                "path": path.relative_to(directory).as_posix(),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "content_hash": canonical_hash(value),
                "calculation_input_hash": value["calculation"]["input_hash"],
                "method_version": value["calculation"]["method_version"],
            }
        )
    manifest = {
        "schema_version": 1,
        "synthetic": True,
        "production_data": False,
        "generation": "Original local save/get services; frozen clock, deterministic UUIDs, synthetic source/rule seams",
        "entries": entries,
        "saved_fields": [
            "request",
            "snapshot",
            "mandate",
            "sources",
            "valuation",
            "scenarios",
            "result_paths",
            "trusted_rules",
            "price_basis_at",
            "fact_as_of",
        ],
        "coverage": [
            "valuation price differs from snapshot",
            "no_action",
            "fee already in cash",
            "new holding exact price ref",
            "unknown fee remains null",
        ],
        "unsupported": [
            "ag22-v1 as same-basis stress alternative",
            "unknown method_version",
            "cross-account or cross-valuation paths",
        ],
        "not_yet_fixture_verified": [
            "published plan option adapter (PT-007)",
            "real account/provider material",
            "real E2E",
        ],
    }
    (directory / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    )
