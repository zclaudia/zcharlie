"""Plan 2.10 daily valuation: NAV discipline, reuse, revision guard."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, localcontext
from uuid import uuid4

import pytest

from investment_agent.investor.common import ObjectRef
from investment_agent.investor.foundations import InvestorError
from investment_agent.portfolio.contracts import (
    AppendEvent,
    Balance,
    CreateAccount,
    Holding,
    Reversal,
    Trade,
)
from investment_agent.portfolio.service import PortfolioService
from investment_agent.portfolio.valuation import (
    Valuation,
    ValuationService,
    day_knowledge_cutoff,
    default_knowledge_cutoff,
    trading_days_after,
)
from investment_agent.storage.database import engine_lifespan, session_scope
from investment_agent.storage.models.base import Base
from investment_agent.storage.models.pit_snapshot import MarketSnapshot
from investment_agent.storage.models.portfolio import PortfolioValuation
from investment_agent.storage.models.reference import Company, Listing, Market

T = datetime(2025, 1, 1, tzinfo=UTC)
D = Decimal
DAY = T.date()


@pytest.fixture
async def ledger(tmp_path):
    async with engine_lifespan(f"sqlite+aiosqlite:///{tmp_path / 'valuation.db'}") as engine:
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
            session.add(MarketSnapshot(listing_id=listing.id, trade_date=DAY, open=D(10), high=D(10), low=D(10), close=D(10), volume=D(1), adjust="none", provider="fixture", captured_at=T, version=1))
            account = await PortfolioService(session).create_account(CreateAccount(idempotency_key=uuid4(), name="Valuation"))
        yield account.id, ref


async def append(account, fact, revision):
    async with session_scope() as session:
        return await PortfolioService(session).append(AppendEvent(account_id=account, expected_revision=revision, idempotency_key=uuid4(), fact=fact))


def opening(ref, *, quantity="100", average_cost="10", cash="10000"):
    return Balance(kind="opening_balance", occurred_at=T, source="user statement", user_confirmed=True, coverage="complete", asset_scope="This explicit brokerage account", holdings=(Holding(listing_ref=ref, quantity=D(quantity), average_cost=D(average_cost), opened_on=DAY - timedelta(days=30)),), cash=D(cash) if cash is not None else None, prior_history="included_in_confirmed_balance")


def buy(ref, *, quantity="10", fee="0"):
    return Trade(kind="trade", occurred_at=T + timedelta(days=1), source="actual execution statement", user_confirmed=True, holding=Holding(listing_ref=ref, quantity=D(quantity)), direction="buy", actual_price=D(10), fee=D(fee))


@pytest.mark.asyncio
async def test_nav_weights_and_pnl(ledger):
    account, ref = ledger
    await append(account, opening(ref), 0)
    await append(account, buy(ref), 1)
    async with session_scope() as session:
        value = await ValuationService(session).ensure(account, valuation_date=DAY + timedelta(days=1), now=T + timedelta(days=1))
        assert value is not None
        assert value.ledger_revision == 2
        item = value.holdings[0]
        assert item.quantity == D(110)
        assert item.price == D(10)
        assert item.market_value == D(1100)
        assert item.remaining_cost == D(1100)
        assert item.floating_pnl == D(0)
        assert item.holding_days == 31
        assert value.known_cash == D(9900)
        assert value.total_assets == D(11000)
        assert value.weight_available
        assert item.weight == D(1100) / D(11000)
        assert value.unknowns == ()


@pytest.mark.asyncio
async def test_reuse_across_reads_and_revision_guard(ledger):
    account, ref = ledger
    await append(account, opening(ref), 0)
    async with session_scope() as session:
        service = ValuationService(session)
        first = await service.ensure(account, valuation_date=DAY + timedelta(days=1))
        again = await service.ensure(account, valuation_date=DAY + timedelta(days=1))
        assert first is not None and again is not None
        assert first.id == again.id  # the day's knowledge boundary keeps reads identical
        current = await service.current(account)
        assert current is not None and current.id == first.id
    await append(account, buy(ref), 1)
    async with session_scope() as session:
        service = ValuationService(session)
        # Yesterday's valuation stays retrievable as audit history.
        old = await service.get(account, first.id)
        assert old.ledger_revision == 1
        fresh = await service.ensure(account, valuation_date=DAY + timedelta(days=1))
        assert fresh is not None and fresh.ledger_revision == 2 and fresh.id != first.id
        assert (await service.current(account)).id == fresh.id
    # A price captured after the day's boundary creates a new identity, never
    # a rewritten history.
    async with session_scope() as session:
        session.add(MarketSnapshot(listing_id=ref.id, trade_date=DAY + timedelta(days=1), open=D(11), high=D(11), low=D(11), close=D(11), volume=D(1), adjust="none", provider="fixture", captured_at=T + timedelta(days=1, hours=1), version=1))
    async with session_scope() as session:
        service = ValuationService(session)
        revised = await service.ensure(account, valuation_date=DAY + timedelta(days=1))
        assert revised is not None and revised.id not in {first.id, fresh.id}
        assert revised.holdings[0].price == D(11)
        assert (await service.get(account, fresh.id)).holdings[0].price == D(10)


@pytest.mark.asyncio
async def test_advancing_cutoff_reuses_identical_inputs_but_calendar_changes_do_not(ledger):
    account, ref = ledger
    await append(account, opening(ref), 0)
    async with session_scope() as session:
        service = ValuationService(session)
        first = await service.ensure(account, valuation_date=DAY, now=T + timedelta(hours=1))
        later = await service.ensure(account, valuation_date=DAY, now=T + timedelta(hours=2))
        assert first.id == later.id
        assert later.knowledge_cutoff == first.knowledge_cutoff
        # An earlier explicit cutoff must retain its own temporal provenance.
        earlier = await service.ensure(account, valuation_date=DAY, now=T)
        assert earlier.id != first.id and earlier.knowledge_cutoff == T
        assert (await service.current(account)).id == first.id
        usable = await service.ensure(account, valuation_date=DAY + timedelta(days=6), now=T + timedelta(days=6), is_trading_day=lambda day: False)
        stale = await service.ensure(account, valuation_date=DAY + timedelta(days=6), now=T + timedelta(days=6), is_trading_day=lambda day: True)
        assert usable.id != stale.id
        assert usable.total_assets is not None and stale.total_assets is None


@pytest.mark.asyncio
async def test_missing_active_opening_blocks_nav_even_after_complete_reconciliation(ledger):
    account, ref = ledger
    first = await append(account, opening(ref), 0)
    await append(account, Balance(kind="reconciliation", occurred_at=T + timedelta(days=1), source="statement", user_confirmed=True, coverage="complete", asset_scope="all account assets", holdings=(), cash=D(500), unexplained_difference="none", prior_history="included_in_confirmed_balance"), 1)
    await append(account, Reversal(kind="reversal", occurred_at=T + timedelta(days=2), source="correction", user_confirmed=True, original_event_id=first.event.id, reason="wrong opening"), 2)
    async with session_scope() as session:
        value = await ValuationService(session).ensure(account, valuation_date=DAY + timedelta(days=2))
        assert "no_active_opening" in value.unknowns
        assert value.total_assets is None and not value.weight_available


@pytest.mark.asyncio
async def test_stale_and_missing_prices_block_nav_not_holding_days(ledger):
    account, ref = ledger
    await append(account, opening(ref), 0)
    async with session_scope() as session:
        # 30 natural days past the fixture close: stale under any threshold.
        value = await ValuationService(session).ensure(account, valuation_date=DAY + timedelta(days=30), now=T + timedelta(days=30))
        assert value is not None
        item = value.holdings[0]
        assert item.price is None and item.price_unknown_reason == "stale_price"
        assert item.market_value is None and item.weight is None
        assert item.holding_days == 60  # days do not depend on prices
        assert value.total_assets is None
        assert value.market_value_subtotal == D(0)
        assert any(entry.startswith("stale_price:") for entry in value.unknowns)


@pytest.mark.asyncio
async def test_weekend_price_is_not_stale_with_trading_calendar(ledger):
    account, ref = ledger
    await append(account, opening(ref), 0)
    friday = DAY + timedelta(days=4)  # 2025-01-03 is a Friday
    sunday = friday + timedelta(days=2)
    sessions = {DAY + timedelta(days=offset) for offset in (1, 2, 3, 4)}  # Mon-Fri

    def is_trading_day(value):
        return value in sessions

    async with session_scope() as session:
        value = await ValuationService(session).ensure(account, valuation_date=sunday, now=T + timedelta(days=6), is_trading_day=is_trading_day)
        assert value is not None
        assert trading_days_after(DAY, sunday, is_trading_day) == 4  # never counts the weekend
        assert value.holdings[0].price is not None
        assert value.total_assets is not None


@pytest.mark.asyncio
async def test_partial_scope_keeps_subtotal_without_weights(ledger):
    account, ref = ledger
    partial = opening(ref, cash=None)
    partial = partial.model_copy(update={"coverage": "partial"})
    await append(account, partial, 0)
    async with session_scope() as session:
        value = await ValuationService(session).ensure(account, valuation_date=DAY, now=T)
        assert value is not None
        assert value.total_assets is None and not value.weight_available
        assert value.market_value_subtotal == D(1000)
        assert "unlisted_assets_unknown" in value.unknowns
        assert "known_cash_missing" in value.unknowns


@pytest.mark.asyncio
async def test_historical_ensure_never_regresses_current_pointer(ledger):
    account, ref = ledger
    await append(account, opening(ref), 0)
    async with session_scope() as session:
        service = ValuationService(session)
        latest = await service.ensure(account, valuation_date=DAY + timedelta(days=2))
        earlier = await service.ensure(account, valuation_date=DAY + timedelta(days=1))
        assert earlier is not None and latest is not None
        assert (await service.current(account)).id == latest.id
        # Reusing an audit result also must not move the pointer backwards.
        assert (await service.ensure(account, valuation_date=DAY + timedelta(days=1))).id == earlier.id
        assert (await service.current(account)).id == latest.id
    await append(account, buy(ref), 1)
    async with session_scope() as session:
        assert await ValuationService(session).current(account) is None


@pytest.mark.asyncio
async def test_valuation_preserves_decimal_arithmetic(ledger):
    account, ref = ledger
    await append(account, opening(ref, quantity="100.000000000000000001", average_cost="10.000000000000000001"), 0)
    async with session_scope() as session:
        value = await ValuationService(session).ensure(account, valuation_date=DAY, now=T)
        with localcontext() as context:
            context.prec = 128
            cost = D("100.000000000000000001") * D("10.000000000000000001")
            assert value.holdings[0].floating_pnl == value.holdings[0].market_value - cost


@pytest.mark.asyncio
async def test_valuation_cannot_apply_future_facts_to_earlier_day(ledger):
    account, ref = ledger
    await append(account, opening(ref), 0)
    await append(account, buy(ref), 1)
    async with session_scope() as session:
        with pytest.raises(InvestorError, match="facts"):
            await ValuationService(session).ensure(account, valuation_date=DAY, now=T)


def test_live_default_cutoff_never_claims_future_knowledge():
    before = datetime.now(UTC)
    from investment_agent.clock import SHANGHAI_TZ
    cutoff = default_knowledge_cutoff(before.astimezone(SHANGHAI_TZ).date())
    assert cutoff <= datetime.now(UTC)


def test_day_knowledge_cutoff_is_a_deterministic_day_end():
    """The queued job's payload must not depend on the wall clock (P1: fan-out)."""
    from investment_agent.clock import SHANGHAI_TZ
    day = date(2030, 6, 7)
    assert day_knowledge_cutoff(day) == day_knowledge_cutoff(day)
    assert day_knowledge_cutoff(day) == datetime(2030, 6, 7, 23, 59, 59, 999999, tzinfo=SHANGHAI_TZ).astimezone(UTC)
    assert day_knowledge_cutoff(day) > datetime.now(UTC)  # today's run may claim the whole session
    # The service default keeps the never-claim-future-knowledge cap.
    assert default_knowledge_cutoff(day) <= datetime.now(UTC)


@pytest.mark.asyncio
async def test_latest_at_selects_in_sql_beyond_the_200_row_window(ledger):
    account, ref = ledger
    await append(account, opening(ref), 0)
    async with session_scope() as session:
        snapshot = await PortfolioService(session).snapshot(account)
    day = DAY + timedelta(days=4)

    def row(knowledge_cutoff: datetime, *, revision: int) -> PortfolioValuation:
        value = Valuation(
            id=uuid4(), account_id=account, snapshot_id=snapshot.id, ledger_revision=revision,
            valuation_date=day, knowledge_cutoff=knowledge_cutoff, algorithm_version="daily_valuation_v1",
            input_digest=uuid4().hex, coverage="complete", known_cash=D(10000), holdings=(),
            market_value_subtotal=D(0), total_assets=D(10000), weight_available=True, unknowns=(),
            created_at=knowledge_cutoff,
            reference=ObjectRef(kind="portfolio_valuation", id=uuid4(), revision=1),
        )
        return PortfolioValuation(
            id=str(value.id), account_id=str(account), snapshot_id=str(snapshot.id),
            ledger_revision=revision, valuation_date=day, algorithm_version=value.algorithm_version,
            input_digest=value.input_digest, payload=value.model_dump(mode="json"),
        )

    winner_cutoff = datetime(2025, 1, 5, 15, 59, 59, tzinfo=UTC)
    async with session_scope() as session:
        # 250 newer-revision noise rows push the true winner out of any
        # "newest 200" window; only an SQL cutoff filter finds it.
        for index in range(250):
            session.add(row(datetime(2025, 1, 5, 12, tzinfo=UTC) + timedelta(minutes=index), revision=2))
        session.add(row(winner_cutoff, revision=1))
        await session.flush()
    async with session_scope() as session:
        service = ValuationService(session)
        best = await service.latest_at(account, cutoff=winner_cutoff)
        assert best is not None and best.ledger_revision == 1
        assert best.knowledge_cutoff == winner_cutoff
        earlier = await service.latest_at(account, cutoff=datetime(2025, 1, 5, 12, tzinfo=UTC))
        assert earlier is not None and earlier.knowledge_cutoff <= datetime(2025, 1, 5, 12, tzinfo=UTC)
        assert await service.latest_at(account, cutoff=T - timedelta(seconds=1)) is None
