"""Database-free projection: hand-built records and an injected price loader.

These tests pin the engine's observable snapshot without a session at all - the
gap the audit named - including the price cutoff instant, the unknown/conflict
string sets and the per-row trace.
"""

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, localcontext
from typing import Any, Literal
from uuid import UUID, uuid4

from investment_agent.investor.common import ObjectRef
from investment_agent.portfolio.contracts import (
    AppendEvent,
    Balance,
    CashFact,
    CorporateAction,
    EventFact,
    EventRecord,
    Holding,
    Reversal,
    Trade,
)
from investment_agent.portfolio.prices import PricePoint
from investment_agent.portfolio.projection import Projection, ProjectionEngine

ACCOUNT = UUID("00000000-0000-0000-0000-0000000000a1")
T = datetime(2025, 1, 6, 1, 30, tzinfo=UTC)  # 09:30 Asia/Shanghai
D = Decimal
LISTING = ObjectRef(kind="listing", id=101, revision=1)
OTHER = ObjectRef(kind="listing", id=202, revision=1)


class FakePrices:
    """A :class:`PriceLookup` over an in-memory page; records every call."""

    def __init__(self, points: dict[int, PricePoint]) -> None:
        self.points = points
        self.calls: list[tuple[tuple[Any, ...], date, datetime]] = []

    async def __call__(
        self, listing_ids: Sequence[Any], *, trade_date: date, knowledge_cutoff: datetime
    ) -> Mapping[Any, PricePoint]:
        self.calls.append((tuple(listing_ids), trade_date, knowledge_cutoff))
        return {
            listing_id: self.points[listing_id]
            for listing_id in listing_ids
            if listing_id in self.points
        }


def point(
    close: str = "10",
    *,
    trade_date: date | None = None,
    identity: int = 1,
    version: int = 1,
) -> PricePoint:
    return PricePoint(
        id=identity, version=version, trade_date=trade_date or T.date(), close=D(close)
    )


def record(fact: EventFact, revision: int, *, recorded_at: datetime = T) -> EventRecord:
    return EventRecord(
        id=uuid4(),
        revision=revision,
        recorded_at=recorded_at,
        request=AppendEvent(
            account_id=ACCOUNT,
            expected_revision=revision - 1,
            idempotency_key=uuid4(),
            fact=fact,
        ),
    )


def opening(
    *,
    quantity: str = "10",
    cash: str | None = "1000",
    coverage: Literal["complete", "partial"] = "complete",
    average_cost: str | None = None,
    opened_on: date | None = None,
    at: datetime = T,
    holdings: tuple[Holding, ...] | None = None,
) -> Balance:
    if holdings is None:
        holdings = (
            Holding(
                listing_ref=LISTING,
                quantity=D(quantity),
                average_cost=D(average_cost) if average_cost is not None else None,
                opened_on=opened_on,
            ),
        )
    return Balance(
        kind="opening_balance",
        occurred_at=at,
        source="user statement",
        user_confirmed=True,
        coverage=coverage,
        asset_scope="This explicit brokerage account",
        holdings=holdings,
        cash=D(cash) if cash is not None else None,
        prior_history="included_in_confirmed_balance",
    )


def reconciliation(*, cash: str, quantity: str, at: datetime = T + timedelta(days=2)) -> Balance:
    return Balance(
        kind="reconciliation",
        occurred_at=at,
        source="user statement",
        user_confirmed=True,
        coverage="complete",
        asset_scope="This explicit brokerage account",
        holdings=(Holding(listing_ref=LISTING, quantity=D(quantity)),),
        cash=D(cash),
        unexplained_difference="unexplained: 50 CNY; no synthetic transfer",
        prior_history="included_in_confirmed_balance",
    )


def trade(
    *,
    direction: Literal["buy", "sell"] = "buy",
    quantity: str = "3",
    price: str = "10",
    fee: str | None = "1",
    at: datetime = T + timedelta(days=1),
    ref: ObjectRef = LISTING,
) -> Trade:
    return Trade(
        kind="trade",
        occurred_at=at,
        source="actual execution statement",
        user_confirmed=True,
        holding=Holding(listing_ref=ref, quantity=D(quantity)),
        direction=direction,
        actual_price=D(price),
        fee=D(fee) if fee is not None else None,
    )


def cash_fact(
    *,
    amount: str,
    kind: Literal["cash_transfer", "dividend"] = "cash_transfer",
    at: datetime = T + timedelta(days=1),
) -> CashFact:
    return CashFact(
        kind=kind,
        occurred_at=at,
        source="actual statement",
        user_confirmed=True,
        amount=D(amount),
        listing_ref=LISTING if kind == "dividend" else None,
    )


async def project(
    records: list[EventRecord],
    *,
    points: dict[int, PricePoint] | None = None,
    trace: dict[UUID, dict[str, Any]] | None = None,
) -> tuple[Projection, FakePrices]:
    loader = FakePrices(points if points is not None else {LISTING.id: point(), OTHER.id: point()})
    result = await ProjectionEngine(loader).project(
        ACCOUNT, records, T + timedelta(days=3), trace=trace
    )
    return result, loader


async def test_engine_projects_a_full_ledger_without_a_database() -> None:
    records = [
        record(opening(quantity="12", average_cost="10", opened_on=date(2024, 12, 20)), 1),
        record(trade(quantity="8", fee="0"), 2),
        record(cash_fact(amount="12"), 3),
        record(cash_fact(amount="7", kind="dividend"), 4),
        record(trade(direction="sell", quantity="5", price="12"), 5),
    ]
    result, _loader = await project(records)
    snapshot = result.snapshot

    assert result.confirmable_deficits == []
    assert snapshot.revision == 5
    assert snapshot.coverage == "complete"
    assert snapshot.known_cash == D(998)  # 1000 - 80 + 12 + 7 + 60 - 1
    assert snapshot.cash_subtotal == D(998)
    assert snapshot.known_subtotal == D(1148) and snapshot.total_assets == D(1148)
    assert snapshot.unknowns == () and snapshot.historical_unknowns == ()
    assert snapshot.cost_unknowns == ()
    assert snapshot.price_coverage == "complete"
    assert snapshot.cost_method_version == "weighted_average_remaining_v1"
    assert snapshot.excluded_prior_event_ids == ()

    holding = snapshot.holdings[0]
    assert holding.confirmed_quantity == D(15) and holding.quantity_subtotal == D(15)
    assert holding.market_value == D(150)  # 15 * 10
    assert holding.remaining_cost == D(150)  # 120 + 80 - 50
    assert holding.average_cost == D(10)
    assert holding.opened_on == date(2024, 12, 20)
    assert holding.price == D(10) and holding.price_date == T.date()
    assert holding.price_ref == ObjectRef(kind="market_snapshot", id=1, revision=1)
    with localcontext() as context:
        context.prec = 40
        assert holding.weight == D(150) / D(1148)

    sold = snapshot.realized[0]
    assert sold.quantity == D(5) and sold.gross == D(60) and sold.fee == D(1)
    assert sold.cost_carried == D(50) and sold.realized == D(9)
    assert sold.cycle_opened_on == date(2024, 12, 20)
    assert [(d.listing_ref, d.amount) for d in snapshot.dividends] == [(LISTING, D(7))]
    assert [r.id for r in records] == list(snapshot.source_event_ids)


async def test_price_cutoff_matches_the_projection_instant() -> None:
    recorded = datetime(2025, 1, 6, 16, 30, tzinfo=UTC)  # 2025-01-07 00:30 Shanghai
    loader = FakePrices({LISTING.id: point(trade_date=date(2025, 1, 7))})
    result = await ProjectionEngine(loader).project(ACCOUNT, [record(opening(), 1)], recorded)

    assert loader.calls == [((LISTING.id,), date(2025, 1, 7), recorded)]
    assert result.snapshot.holdings[0].price_date == date(2025, 1, 7)
    assert result.snapshot.recorded_at == recorded and result.snapshot.valuation_at == recorded


async def test_sell_beyond_a_confirmed_basis_is_a_deficit_and_a_conflict() -> None:
    sell = record(trade(direction="sell", quantity="12"), 2)
    result, _loader = await project([record(opening(quantity="10", average_cost="10"), 1), sell])
    snapshot = result.snapshot

    assert result.confirmable_deficits == [(sell.id, LISTING)]
    assert snapshot.conflicts == (f"negative_quantity:{sell.id}:{LISTING.id}",)
    assert snapshot.holdings[0].quantity_subtotal == D(-2)
    assert snapshot.holdings[0].confirmed_quantity is None
    assert snapshot.unknowns == (f"unknown_quantity:{LISTING.id}",)
    assert snapshot.total_assets is None  # never a negative position total
    assert snapshot.known_cash == D(1119)  # 1000 + 120 - 1


async def test_reconciliation_names_its_difference() -> None:
    confirmed = record(reconciliation(cash="950", quantity="9"), 2)
    result, _loader = await project([record(opening(average_cost="10"), 1), confirmed])
    snapshot = result.snapshot

    assert snapshot.conflicts == (f"reconciliation_difference:{confirmed.id}",)
    assert len(snapshot.reconciliations) == 1
    comparison = snapshot.reconciliations[0]
    assert comparison.before_cash == D(1000) and comparison.confirmed_cash == D(950)
    assert comparison.cash_delta == D(-50) and comparison.cash_unknown_reason is None
    assert comparison.holdings[0].before == D(10) and comparison.holdings[0].confirmed == D(9)
    assert comparison.holdings[0].delta == D(-1)
    assert comparison.user_difference_explanation == "unexplained: 50 CNY; no synthetic transfer"
    assert snapshot.known_cash == D(950)
    assert snapshot.holdings[0].confirmed_quantity == D(9)


async def test_backfilled_event_before_the_baseline_is_audit_only() -> None:
    backfill = record(trade(quantity="2", at=T - timedelta(days=1)), 3)
    records = [record(opening(), 1), record(trade(), 2), backfill]
    trace: dict[UUID, dict[str, Any]] = {backfill.id: {}}
    result, _loader = await project(records, trace=trace)

    assert result.snapshot.excluded_prior_event_ids == (backfill.id,)
    assert trace[backfill.id] == {"inclusion": "audit_prior_history"}
    assert result.snapshot.holdings[0].confirmed_quantity == D(13)
    assert result.snapshot.known_cash == D(969)  # the backfill changed nothing


async def test_trace_holds_the_applied_row_deltas() -> None:
    buy = record(trade(quantity="3", fee="1"), 2)
    trace: dict[UUID, dict[str, Any]] = {buy.id: {}}
    result, _loader = await project([record(opening(average_cost="0"), 1), buy], trace=trace)

    assert trace[buy.id] == {
        "inclusion": "applied",
        "quantity_delta": {str(LISTING.id): D(3)},
        "cash_delta": D(-31),
        "cost_remaining": {str(LISTING.id): D(31)},  # 0 + 3 * 10 + 1
    }
    assert result.snapshot.holdings[0].remaining_cost == D(31)


async def test_missing_price_and_unknown_fee_stay_unknown() -> None:
    two = (
        Holding(listing_ref=LISTING, quantity=D(10), average_cost=D(10)),
        Holding(listing_ref=OTHER, quantity=D(5), average_cost=D(10)),
    )
    unknown_fee = record(trade(quantity="3", fee=None), 2)
    result, loader = await project(
        [record(opening(holdings=two), 1), unknown_fee], points={LISTING.id: point()}
    )
    snapshot = result.snapshot

    assert loader.calls[0][0] == (LISTING.id, OTHER.id)
    assert snapshot.unknowns == (f"missing_price:{OTHER.id}", f"unknown_fee:{unknown_fee.id}")
    assert snapshot.historical_unknowns == (f"unknown_fee:{unknown_fee.id}",)
    assert snapshot.cost_unknowns == (f"buy_fee_unknown:{LISTING.id}",)
    assert snapshot.price_coverage == "partial"
    assert snapshot.known_cash is None and snapshot.total_assets is None
    assert snapshot.known_subtotal == D(130)  # 13 * 10; the other listing has no price
    by_ref = {h.listing_ref.id: h for h in snapshot.holdings}
    assert by_ref[OTHER.id].price is None and by_ref[OTHER.id].market_value is None
    assert by_ref[LISTING.id].cost_unknown_reason == "buy_fee_unknown"
    assert by_ref[LISTING.id].remaining_cost is None


async def test_split_scales_quantity_and_keeps_the_total_cost() -> None:
    split = record(
        CorporateAction(
            kind="corporate_action",
            occurred_at=T + timedelta(days=1),
            source="exchange notice",
            user_confirmed=True,
            listing_ref=LISTING,
            action="split",
            ratio=D(2),
            affects=("quantity",),
            description="two-for-one split",
        ),
        2,
    )
    result, _loader = await project(
        [record(opening(quantity="10", average_cost="10", opened_on=date(2024, 12, 1)), 1), split]
    )
    holding = result.snapshot.holdings[0]

    assert holding.quantity_subtotal == D(20) and holding.confirmed_quantity == D(20)
    assert holding.remaining_cost == D(100) and holding.average_cost == D(5)
    assert holding.opened_on == date(2024, 12, 1)
    assert result.snapshot.unknowns == ()


async def test_pending_action_restricts_quantity_and_keeps_cost_unknown() -> None:
    pending = record(
        CorporateAction(
            kind="corporate_action",
            occurred_at=T + timedelta(days=1),
            source="company announcement",
            user_confirmed=True,
            listing_ref=LISTING,
            action="pending",
            affects=("quantity", "cash"),
            description="unconfirmed dividend and shares",
        ),
        2,
    )
    result, _loader = await project(
        [record(opening(quantity="10", average_cost="10"), 1), pending]
    )
    snapshot = result.snapshot

    assert snapshot.historical_unknowns == (
        f"pending_corporate_action:{pending.id}",
        f"unknown_quantity:{LISTING.id}",
    )
    assert snapshot.conflicts == ()
    assert snapshot.known_cash is None  # affected cash is unknown, never guessed
    holding = snapshot.holdings[0]
    assert holding.confirmed_quantity is None
    assert holding.cost_unknown_reason == "pending_corporate_action"
    assert holding.remaining_cost is None
    assert snapshot.unknowns == (
        f"pending_corporate_action:{pending.id}",
        f"unknown_quantity:{LISTING.id}",
    )


async def test_reversal_removes_the_original_from_the_replay() -> None:
    original = record(opening(), 1)
    buy = record(trade(quantity="3"), 2)
    reversal = record(
        Reversal(
            kind="reversal",
            occurred_at=T + timedelta(days=2),
            source="user correction",
            user_confirmed=True,
            original_event_id=buy.id,
            reason="duplicate entry",
        ),
        3,
    )
    result, _loader = await project([original, buy, reversal])
    snapshot = result.snapshot

    assert snapshot.holdings[0].confirmed_quantity == D(10)  # the buy is replayed out
    assert snapshot.known_cash == D(1000)
    assert snapshot.realized == ()
    # Every record is named (the reversal too); only the reversed fact is not applied.
    assert list(snapshot.source_event_ids) == [original.id, buy.id, reversal.id]
    assert snapshot.revision == 3
