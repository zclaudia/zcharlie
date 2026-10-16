"""Read-only input freezing against real temporary SQLite evidence rows."""

from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

from investment_agent.investor.common import ObjectRef
from investment_agent.investor.foundations import InvestorError
from investment_agent.portfolio.purposes import Purpose, PurposeVersion
from investment_agent.portfolio.suitability_calculations import evaluate_fit
from investment_agent.portfolio.suitability_contracts import (
    AccountFitRequest,
    FitRequestItem,
    PurposeBinding,
    ValuationBinding,
)
from investment_agent.portfolio.suitability_inputs import SuitabilityInputService, utc
from investment_agent.storage.models.pit_snapshot import IndustryClassification
from investment_agent.storage.models.portfolio import PortfolioAccount
from investment_agent.storage.models.portfolio import PortfolioValuation as ValuationRow
from investment_agent.storage.models.portfolio_plan import (
    PortfolioPurposeHead,
)
from investment_agent.storage.models.portfolio_plan import (
    PortfolioPurposeVersion as PurposeVersionRow,
)
from tests.support.account_fit import assumptions
from tests.support.account_fit_db import AS_OF, account_fit_sqlite, make_inputs, make_trusted_rule
from tests.support.account_fit_flow import persist_inputs


def _request(
    inputs, *, action: str = "open", purpose_binding: PurposeBinding | None = None
) -> AccountFitRequest:
    listing_ref = ObjectRef(kind="listing", id=600000, revision=1)
    return AccountFitRequest(
        operation_id=uuid4(),
        account_id=inputs.account_id,
        snapshot_ref=inputs.snapshot.reference,
        mandate_ref=ObjectRef(
            kind="mandate", id=inputs.mandate.id, revision=inputs.mandate.revision
        ),
        valuation_binding=inputs.valuation_binding,
        purpose_binding=purpose_binding or PurposeBinding(state="none_recorded"),
        items=(
            FitRequestItem(
                listing_ref=listing_ref,
                action=action,
                trade_assumptions=None if action == "hold" else assumptions(),
            ),
        ),
    )


class _FixtureRuleResolver:
    async def resolve(self, listing_ref: ObjectRef, as_of: datetime):
        assert as_of == AS_OF
        return make_trusted_rule(listing_ref)


@pytest.mark.asyncio
async def test_context_checks_account_owner_and_separates_missing_snapshot_from_missing_valuation(
    tmp_path: Path,
) -> None:
    inputs = make_inputs(with_valuation=False)
    async with account_fit_sqlite(tmp_path) as session:
        await persist_inputs(session, inputs)
        with pytest.raises(InvestorError) as error:
            await SuitabilityInputService(session, owner="another_owner").context(inputs.account_id)
        assert error.value.detail.code == "not_found"

        service = SuitabilityInputService(session)
        context = await service.context(inputs.account_id)
        assert context.snapshot is not None
        assert context.valuation is None
        assert context.valuation_binding.state == "missing"
        frozen = await service.freeze(_request(inputs), as_of=AS_OF)
        assert frozen.valuation is None
        assert frozen.price_basis_at is None
        assert all(source.price.price is None for source in frozen.listing_sources)

        row = await session.get(PortfolioAccount, str(inputs.account_id))
        assert row is not None
        row.current_snapshot_id = None
        await session.flush()
        context_without_snapshot = await service.context(inputs.account_id)
        assert context_without_snapshot.snapshot is None
        with pytest.raises(InvestorError) as missing:
            await service.freeze(_request(inputs), as_of=AS_OF)
        assert missing.value.detail.code == "snapshot_required"


@pytest.mark.asyncio
async def test_action_collision_and_partial_snapshot_missing_target_are_rejected(
    tmp_path: Path,
) -> None:
    held = make_inputs(holdings=((600000, "10", "1500"),), prices={600000: "1500"})
    async with account_fit_sqlite(tmp_path) as session:
        await persist_inputs(session, held)
        with pytest.raises(InvestorError) as collision:
            await SuitabilityInputService(session).freeze(
                _request(held, action="open"), as_of=AS_OF
            )
        assert collision.value.detail.code == "action_holding_mismatch"

    partial = make_inputs(snapshot_coverage="partial")
    partial_path = tmp_path / "partial"
    partial_path.mkdir()
    async with account_fit_sqlite(partial_path) as session:
        await persist_inputs(session, partial)
        with pytest.raises(InvestorError) as unknown:
            await SuitabilityInputService(session).freeze(
                _request(partial, action="add"), as_of=AS_OF
            )
        assert unknown.value.detail.code == "holding_presence_unknown"


@pytest.mark.asyncio
async def test_purpose_after_price_basis_but_before_assessment_is_frozen(tmp_path: Path) -> None:
    inputs = make_inputs()
    async with account_fit_sqlite(tmp_path) as session:
        await persist_inputs(session, inputs)
        purpose_ref = ObjectRef(kind="portfolio_purposes", id=uuid4(), revision=1)
        purpose = Purpose(
            id=uuid4(),
            name="synthetic near-term reserve",
            amount=Decimal("100000"),
            available_budget=Decimal("80000"),
            required_cash=Decimal("20000"),
            horizon="short_term",
            required_on=date(2026, 12, 31),
            source="synthetic_fixture",
            source_version="v1",
        )
        version = PurposeVersion(
            reference=purpose_ref,
            account_id=inputs.account_id,
            snapshot_ref=inputs.snapshot.reference,
            confirmed_at=AS_OF - timedelta(minutes=10),
            purposes=(purpose,),
            confirmed_cash=Decimal("200000"),
            unallocated=Decimal("100000"),
            semantic_hash="f" * 64,
        )
        session.add(
            PurposeVersionRow(
                id=str(purpose_ref.id),
                account_id=str(inputs.account_id),
                owner_id="local_owner",
                input_hash="f" * 64,
                payload=version.model_dump(mode="json"),
            )
        )
        session.add(
            PortfolioPurposeHead(
                account_id=str(inputs.account_id), revision=1, version_id=str(purpose_ref.id)
            )
        )
        await session.flush()
        binding = PurposeBinding(
            state="selected",
            purpose_ref=purpose_ref,
            purpose_id=purpose.id,
            confirmed_at=version.confirmed_at,
        )
        frozen = await SuitabilityInputService(session).freeze(
            _request(inputs, purpose_binding=binding), as_of=AS_OF
        )
        assert frozen.price_basis_at == inputs.valuation.knowledge_cutoff
        assert version.confirmed_at > frozen.price_basis_at
        assert version.confirmed_at < frozen.assessment_as_of
        assert frozen.purpose_version == version


@pytest.mark.asyncio
async def test_freeze_preserves_actual_csrc_row_and_pit_trade_rule(tmp_path: Path) -> None:
    inputs = make_inputs()
    async with account_fit_sqlite(tmp_path) as session:
        await persist_inputs(session, inputs)
        service = SuitabilityInputService(session, rules=_FixtureRuleResolver())
        frozen = await service.freeze(_request(inputs), as_of=AS_OF)
        source = next(item for item in frozen.listing_sources if item.listing_ref.id == 600000)
        classification = await session.scalar(
            select(IndustryClassification).where(IndustryClassification.listing_id == 600000)
        )
        assert classification is not None
        assert source.sector.row_id == classification.id
        assert source.sector.code == classification.code
        assert source.sector.name == classification.name
        assert source.sector.valid_from == classification.valid_from
        assert source.sector.source == classification.source
        assert source.sector.captured_at == utc(classification.captured_at)
        assert source.trusted_rule is not None
        assert source.trusted_rule.rule.buy_minimum == Decimal("100")
        assert source.trusted_rule.trusted_source == "synthetic_fixture"


@pytest.mark.asyncio
async def test_scheduled_future_cutoff_is_an_explicit_missing_valuation(tmp_path: Path) -> None:
    """The old daily worker uses day-end cutoffs even before the day ends."""
    inputs = make_inputs()
    future = inputs.valuation.model_copy(update={"knowledge_cutoff": AS_OF + timedelta(hours=6)})
    async with account_fit_sqlite(tmp_path) as session:
        await persist_inputs(session, inputs)
        row = await session.get(ValuationRow, str(future.id))
        assert row is not None
        row.payload = future.model_dump(mode="json")
        await session.flush()
        service = SuitabilityInputService(session)
        context = await service.context(inputs.account_id, as_of=AS_OF)
        assert context.valuation is None
        assert context.valuation_binding.state == "missing"
        assert "valuation_knowledge_in_future" in context.gaps
        request = _request(inputs).model_copy(update={
            "valuation_binding": ValuationBinding(state="missing", reason="valuation_knowledge_in_future")
        })
        frozen = await service.freeze(request, as_of=AS_OF)
        assert frozen.valuation is None
        assert frozen.price_basis_at is None
        assert all(source.price.price is None for source in frozen.listing_sources)

        # The exact old valuation becomes eligible once its knowledge boundary passes.
        later = await service.context(inputs.account_id, as_of=AS_OF + timedelta(hours=7))
        assert later.valuation == future
        assert later.valuation_binding.state == "available"
        assessment, _ = evaluate_fit(frozen, request.items[0], operation_id=request.operation_id)
        assert assessment.financial_status == "unknown"
