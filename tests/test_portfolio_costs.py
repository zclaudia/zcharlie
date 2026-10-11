"""Plan 2.7 cost projection and plan 2.8 correction semantics."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from investment_agent.investor.common import ObjectRef
from investment_agent.investor.foundations import InvestorError
from investment_agent.portfolio.contracts import (
    AppendEvent,
    ApplyCorrection,
    Balance,
    CashFact,
    CorporateAction,
    CreateAccount,
    Holding,
    Reversal,
    Trade,
)
from investment_agent.portfolio.service import PortfolioService
from investment_agent.storage.database import engine_lifespan, session_scope
from investment_agent.storage.models.base import Base
from investment_agent.storage.models.pit_snapshot import MarketSnapshot
from investment_agent.storage.models.portfolio import PortfolioEvent
from investment_agent.storage.models.reference import Company, Listing, Market

T = datetime(2025, 1, 1, tzinfo=UTC)
D = Decimal


@pytest.fixture
async def ledger(tmp_path):
    async with engine_lifespan(f"sqlite+aiosqlite:///{tmp_path / 'costs.db'}") as engine:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with session_scope() as session:
            session.add(Market(code="SH", name="Fixture", timezone="UTC", currency="CNY"))
            company = Company(canonical_name="Fixture")
            session.add(company)
            await session.flush()
            listing = Listing(company_id=company.id, market_code="SH", ticker="600000.SH")
            session.add(listing)
            await session.flush()
            ref = ObjectRef(kind="listing", id=listing.id, revision=1)
            session.add(MarketSnapshot(listing_id=listing.id, trade_date=T.date(), open=D(10), high=D(10), low=D(10), close=D(10), volume=D(1), adjust="none", provider="fixture", captured_at=T, version=1))
            account = await PortfolioService(session).create_account(CreateAccount(idempotency_key=uuid4(), name="Costs"))
        yield account.id, ref


def opening(ref, *, quantity="100", average_cost="10", opened_on=None, cash="10000", coverage="complete", kind="opening_balance", occurred_at=T):
    return Balance(
        kind=kind,
        occurred_at=occurred_at,
        source="user statement",
        user_confirmed=True,
        coverage=coverage,
        asset_scope="This explicit brokerage account",
        holdings=(Holding(listing_ref=ref, quantity=D(quantity), average_cost=D(average_cost) if average_cost is not None else None, opened_on=opened_on),),
        cash=D(cash) if cash is not None else None,
        prior_history="included_in_confirmed_balance",
        unexplained_difference="none" if kind == "reconciliation" else None,
    )


def trade(ref, *, quantity="10", price="10", fee="1", direction="buy", occurred_at=T + timedelta(days=1)):
    return Trade(kind="trade", occurred_at=occurred_at, source="actual execution statement", user_confirmed=True, holding=Holding(listing_ref=ref, quantity=D(quantity)), direction=direction, actual_price=D(price), fee=D(fee) if fee is not None else None)


async def append(account, fact, revision):
    async with session_scope() as session:
        return await PortfolioService(session).append(AppendEvent(account_id=account, expected_revision=revision, idempotency_key=uuid4(), fact=fact))


def holding(result):
    return result.snapshot.holdings[0]


@pytest.mark.asyncio
async def test_opening_cost_and_buy_adds_fees(ledger):
    account, ref = ledger
    first = await append(account, opening(ref, opened_on=T.date()), 0)
    assert holding(first).remaining_cost == D(1000)
    assert holding(first).average_cost == D(10)
    assert holding(first).opened_on == T.date()
    assert first.snapshot.cost_method_version == "weighted_average_remaining_v1"

    bought = await append(account, trade(ref, quantity="10", price="10", fee="1"), 1)
    # Fees are part of the acquisition cost (plan 2.7).
    assert holding(bought).remaining_cost == D(1101)
    assert holding(bought).quantity_subtotal == D(110)
    # The projection divides at its own 160-digit context; compare round-trip.
    assert abs(holding(bought).average_cost * D(110) - D(1101)) < D("1e-140")
    assert holding(bought).opened_on == T.date()  # cycle continues


@pytest.mark.asyncio
async def test_partial_sell_keeps_average_and_realizes(ledger):
    account, ref = ledger
    await append(account, opening(ref, opened_on=T.date()), 0)
    sold = await append(account, trade(ref, direction="sell", quantity="40", price="12", fee="2", occurred_at=T + timedelta(days=2)), 1)
    item = holding(sold)
    # Remaining average stays 10; realized = 480 - 2 - 400.
    assert item.quantity_subtotal == D(60)
    assert item.remaining_cost == D(600)
    assert item.average_cost == D(10)
    recorded = sold.snapshot.realized[-1]
    assert recorded.quantity == D(40)
    assert recorded.cost_carried == D(400)
    assert recorded.realized == D(78)
    assert recorded.cycle_opened_on == T.date()


@pytest.mark.asyncio
async def test_full_exit_resets_cycle_and_rebuy_starts_new_one(ledger):
    account, ref = ledger
    await append(account, opening(ref, opened_on=T.date()), 0)
    exited = await append(account, trade(ref, direction="sell", quantity="100", price="11", fee="1", occurred_at=T + timedelta(days=2)), 1)
    assert holding(exited).quantity_subtotal == D(0)
    assert holding(exited).remaining_cost == D(0)
    assert holding(exited).average_cost is None
    assert holding(exited).opened_on is None
    assert exited.snapshot.realized[-1].realized == D(99)  # 1100 - 1 - 1000
    assert exited.snapshot.realized[-1].cycle_opened_on == T.date()

    rebought = await append(account, trade(ref, quantity="5", price="12", fee="0", occurred_at=T + timedelta(days=3)), 2)
    assert holding(rebought).remaining_cost == D(60)
    assert holding(rebought).average_cost == D(12)
    assert holding(rebought).opened_on == (T + timedelta(days=3)).date()


@pytest.mark.asyncio
async def test_backfilled_sell_cannot_strand_a_later_sell(ledger):
    account, ref = ledger
    await append(account, opening(ref), 0)
    await append(account, trade(ref, direction="sell", quantity="80", occurred_at=T + timedelta(days=3)), 1)
    with pytest.raises(InvestorError, match="confirmable quantity"):
        await append(account, trade(ref, direction="sell", quantity="30", occurred_at=T + timedelta(days=2)), 2)
    async with session_scope() as session:
        saved = await PortfolioService(session).snapshot(account)
        assert saved.revision == 2 and saved.holdings[0].confirmed_quantity == D(20)


@pytest.mark.asyncio
async def test_single_reversal_cannot_strand_a_later_sell(ledger):
    account, ref = ledger
    await append(account, opening(ref), 0)
    bought = await append(account, trade(ref, quantity="20"), 1)
    await append(account, trade(ref, direction="sell", quantity="120", occurred_at=T + timedelta(days=2)), 2)
    with pytest.raises(InvestorError, match="confirmable quantity"):
        await append(account, Reversal(kind="reversal", occurred_at=T + timedelta(days=3), source="user", user_confirmed=True, original_event_id=bought.event.id, reason="wrong buy"), 3)


@pytest.mark.asyncio
async def test_correction_rejects_two_replacement_openings(ledger):
    account, ref = ledger
    first = await append(account, opening(ref), 0)
    async with session_scope() as session:
        with pytest.raises(InvestorError, match="one active opening"):
            await PortfolioService(session).apply_correction(ApplyCorrection(
                account_id=account, expected_revision=1, operation_id=uuid4(),
                reason="replace baseline", reversals=(first.event.id,),
                replacements=(opening(ref), opening(ref, occurred_at=T + timedelta(hours=1))),
            ))


@pytest.mark.asyncio
async def test_unknown_quantity_does_not_invent_cost_or_cycle(ledger):
    account, ref = ledger
    partial = opening(ref, coverage="partial").model_copy(update={"holdings": ()})
    await append(account, partial, 0)
    bought = await append(account, trade(ref), 1)
    assert holding(bought).confirmed_quantity is None
    assert holding(bought).remaining_cost is None
    assert holding(bought).opened_on is None
    # A later balance confirms quantity, not an undocumented acquisition cost.
    reconciled = await append(account, opening(ref, kind="reconciliation", quantity="10", average_cost=None, occurred_at=T + timedelta(days=2)), 2)
    assert holding(reconciled).confirmed_quantity == D(10)
    assert holding(reconciled).remaining_cost is None
    assert holding(reconciled).opened_on is None


@pytest.mark.asyncio
async def test_holding_cycle_uses_beijing_date(ledger):
    account, ref = ledger
    await append(account, opening(ref, quantity="0"), 0)
    bought = await append(account, trade(ref, occurred_at=T + timedelta(hours=17)), 1)
    assert holding(bought).opened_on == (T + timedelta(days=1)).date()
    balance = opening(ref, opened_on=(T + timedelta(days=1)).date(), occurred_at=T + timedelta(hours=17))
    assert balance.holdings[0].opened_on == (T + timedelta(days=1)).date()

@pytest.mark.asyncio
async def test_unknown_buy_fee_keeps_cost_unknown_without_blocking_nav(ledger):
    account, ref = ledger
    await append(account, opening(ref, opened_on=T.date()), 0)
    bought = await append(account, trade(ref, quantity="10", fee=None), 1)
    item = holding(bought)
    assert item.remaining_cost is None
    assert item.cost_unknown_reason == "buy_fee_unknown"
    # Cost unknown never blocks the holding's market value (plan 2.9); the NAV
    # stays unavailable only because an unknown fee also makes cash unknown.
    assert item.market_value == D(1100)
    assert bought.snapshot.total_assets is None
    assert any(entry.startswith("buy_fee_unknown:") for entry in bought.snapshot.cost_unknowns)


@pytest.mark.asyncio
async def test_reconciliation_cost_rules(ledger):
    account, ref = ledger
    await append(account, opening(ref, opened_on=T.date()), 0)
    # Pure cash reconciliation with unchanged quantity keeps the cost.
    cash_only = await append(account, opening(ref, average_cost=None, quantity="100", cash="900", kind="reconciliation", occurred_at=T + timedelta(days=1)), 1)
    assert holding(cash_only).remaining_cost == D(1000)
    # A cost-only unknown never blocks NAV (plan 2.9).
    assert cash_only.snapshot.total_assets is not None
    # A changed quantity without a confirmed cost becomes unknown.
    changed = await append(account, opening(ref, average_cost=None, quantity="80", cash="900", kind="reconciliation", occurred_at=T + timedelta(days=2)), 2)
    assert holding(changed).remaining_cost is None
    assert holding(changed).cost_unknown_reason == "balance_cost_not_confirmed"
    # Confirming the new cost restores it.
    confirmed = await append(account, opening(ref, average_cost="9", quantity="80", cash="900", kind="reconciliation", occurred_at=T + timedelta(days=3)), 3)
    assert holding(confirmed).remaining_cost == D(720)


@pytest.mark.asyncio
async def test_split_keeps_total_cost_and_cycle_then_pending_marks_unknown(ledger):
    account, ref = ledger
    await append(account, opening(ref, opened_on=T.date()), 0)
    split = await append(account, CorporateAction(kind="corporate_action", occurred_at=T + timedelta(days=1), source="user confirmed", user_confirmed=True, listing_ref=ref, action="split", ratio=D(2), affects=("quantity",), description="two for one"), 1)
    assert holding(split).quantity_subtotal == D(200)
    assert holding(split).remaining_cost == D(1000)
    assert holding(split).average_cost == D(5)
    assert holding(split).opened_on == T.date()

    pending = await append(account, CorporateAction(kind="corporate_action", occurred_at=T + timedelta(days=2), source="user statement", user_confirmed=True, listing_ref=ref, action="pending", description="await rights issue confirmation"), 2)
    assert holding(pending).remaining_cost is None
    assert holding(pending).cost_unknown_reason == "pending_corporate_action"


@pytest.mark.asyncio
async def test_dividend_never_reduces_cost(ledger):
    account, ref = ledger
    await append(account, opening(ref, opened_on=T.date()), 0)
    paid = await append(account, CashFact(kind="dividend", occurred_at=T + timedelta(days=1), source="user statement", user_confirmed=True, amount=D(50), listing_ref=ref), 1)
    assert holding(paid).remaining_cost == D(1000)
    assert holding(paid).average_cost == D(10)
    assert paid.snapshot.dividends[-1].amount == D(50)
    assert paid.snapshot.known_cash == D(10050)


@pytest.mark.asyncio
async def test_oversell_beyond_unknown_basis_is_recorded_not_rejected(ledger):
    account, ref = ledger
    await append(account, opening(ref, opened_on=T.date()), 0)
    # A pending, unconfirmed action makes the then-current quantity unknowable:
    # the sell cannot be validated, so it is recorded with explicit limits
    # instead of being rejected as a confirmed oversell (plan 2.8).
    await append(account, CorporateAction(kind="corporate_action", occurred_at=T + timedelta(days=1), source="user statement", user_confirmed=True, listing_ref=ref, action="pending", description="await rights issue confirmation"), 1)
    guessed = await append(account, trade(ref, direction="sell", quantity="200", occurred_at=T + timedelta(days=2)), 2)
    assert holding(guessed).quantity_subtotal == D(-100)
    assert holding(guessed).confirmed_quantity is None
    assert any(conflict.startswith("negative_quantity") for conflict in guessed.snapshot.conflicts)


@pytest.mark.asyncio
async def test_correction_reverses_and_replaces_atomically(ledger):
    account, ref = ledger
    await append(account, opening(ref, opened_on=T.date()), 0)
    bought = await append(account, trade(ref, quantity="20", price="10", fee="0"), 1)
    assert holding(bought).quantity_subtotal == D(120)
    async with session_scope() as session:
        service = PortfolioService(session)
        request = ApplyCorrection(
            account_id=account,
            expected_revision=2,
            operation_id=uuid4(),
            reason="wrong quantity entered",
            reversals=(bought.event.id,),
            replacements=(trade(ref, quantity="5", price="12", fee="0", occurred_at=T + timedelta(days=1)),),
        )
        result = await service.apply_correction(request)
        assert result.snapshot.revision == 4
        assert [event.request.row_id for event in result.events] == ["reversal:0", "replacement:0"]
        assert holding(result).quantity_subtotal == D(105)
        assert holding(result).remaining_cost == D(1060)
        # The account head exposes only the final state.
        current = await service.snapshot(account)
        assert current.revision == 4 and current.id == result.snapshot.id
        rows = list(await session.scalars(__import__("sqlalchemy").select(PortfolioEvent).where(PortfolioEvent.batch_id == str(request.operation_id))))
        assert len(rows) == 2
        # A retried identical operation returns the original result.
        again = await service.apply_correction(request)
        assert again.snapshot.id == result.snapshot.id
        assert [event.id for event in again.events] == [event.id for event in result.events]
        # A changed body under the same key conflicts.
        with pytest.raises(InvestorError, match="another body"):
            await service.apply_correction(request.model_copy(update={"reason": "changed"}))


@pytest.mark.asyncio
async def test_correction_rejects_invalid_replays_and_requires_companions(ledger):
    account, ref = ledger
    await append(account, opening(ref, quantity="10", opened_on=T.date(), cash="10000"), 0)
    bought = await append(account, trade(ref, quantity="5", fee="0"), 1)
    sold = await append(account, trade(ref, direction="sell", quantity="12", price="11", fee="0", occurred_at=T + timedelta(days=2)), 2)
    assert holding(sold).quantity_subtotal == D(3)
    async with session_scope() as session:
        service = PortfolioService(session)
        # Reversing the buy alone would strand the later sell beyond the basis.
        lonely = ApplyCorrection(account_id=account, expected_revision=3, operation_id=uuid4(), reason="undo buy", reversals=(bought.event.id,))
        with pytest.raises(InvestorError, match="companion corrections"):
            await service.apply_correction(lonely)
        # Replacements can never be reversals; use the reversals field instead.
        with pytest.raises(ValidationError):
            ApplyCorrection.model_validate({**lonely.model_dump(mode="json"), "replacements": [Reversal(kind="reversal", occurred_at=datetime.now(UTC), source="user", user_confirmed=True, original_event_id=sold.event.id, reason="companion").model_dump(mode="json")]})
        # The companion set (both reversals plus a corrected sell) replays valid.
        request = ApplyCorrection(
            account_id=account,
            expected_revision=3,
            operation_id=uuid4(),
            reason="undo buy and resize the sell",
            reversals=(bought.event.id, sold.event.id),
            replacements=(trade(ref, direction="sell", quantity="8", price="11", fee="0", occurred_at=T + timedelta(days=2)),),
        )
        result = await service.apply_correction(request)
        assert holding(result).quantity_subtotal == D(2)
        assert result.snapshot.revision == 6
        with pytest.raises(InvestorError, match="already reversed"):
            await service.apply_correction(ApplyCorrection(account_id=account, expected_revision=result.snapshot.revision, operation_id=uuid4(), reason="twice", reversals=(bought.event.id,)))


async def test_correction_requires_own_account_events_and_a_reason(ledger):
    account, ref = ledger
    await append(account, opening(ref, quantity="10", opened_on=T.date(), cash="10000"), 0)
    async with session_scope() as session:
        service = PortfolioService(session)
        with pytest.raises(ValidationError):
            ApplyCorrection(account_id=account, expected_revision=1, operation_id=uuid4(), reason="  ", reversals=(uuid4(),))
        with pytest.raises(InvestorError, match="must belong to this account and cannot be reversals"):
            await service.apply_correction(ApplyCorrection(account_id=account, expected_revision=1, operation_id=uuid4(), reason="no such event", reversals=(uuid4(),)))
