"""Deterministic bitemporal projection of one account's ledger replay.

The engine replays the account's active facts in ``(occurred_at, recorded
revision)`` order, materialises the holdings at one knowledge instant and
assembles the immutable snapshot. Prices arrive as an injected callable, so a
projection runs from hand-built records without a database.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, localcontext
from typing import Any, Literal
from uuid import UUID, uuid4

from investment_agent.clock import SHANGHAI_TZ
from investment_agent.investor.common import ObjectRef
from investment_agent.portfolio.contracts import (
    Balance,
    EventRecord,
    Reversal,
    Snapshot,
    SnapshotHolding,
)
from investment_agent.portfolio.prices import PriceLookup
from investment_agent.portfolio.projection_events import HANDLERS, apply_balance
from investment_agent.portfolio.projection_state import ProjectionState

COST_METHOD_VERSION = "weighted_average_remaining_v1"
"""Plan 2.7口径: remaining weighted acquisition cost, fees included."""


@dataclass
class Projection:
    snapshot: Snapshot
    confirmable_deficits: list[tuple[UUID, ObjectRef]] = field(default_factory=list)
    """Sells (event id, listing id) whose confirmable basis was known and insufficient."""


@dataclass(frozen=True)
class Materialised:
    """What the holdings pass valued; consumed by :func:`build_snapshot`."""

    holdings: tuple[SnapshotHolding, ...]
    unknowns: tuple[str, ...]
    cost_unknowns: tuple[str, ...]
    price_coverage: Literal["complete", "partial"]
    known_cash: Decimal | None
    subtotal: Decimal | None
    total: Decimal | None


def build_snapshot(
    *,
    account_id: UUID,
    records: Sequence[EventRecord],
    recorded: datetime,
    state: ProjectionState,
    valued: Materialised,
) -> Snapshot:
    """Assemble the immutable snapshot; a flat constructor, no branches."""

    snapshot_id = uuid4()
    # An empty records list has no facts to name: fall back to sentinels
    # (the projection instant itself, revision 0) instead of crashing, so a
    # first import's before-projection can replay an empty account. Every
    # caller guards that case today (and ObjectRef itself requires revision
    # >= 1, exactly as the pre-refactor replay did).
    fact_as_of = max((r.request.fact.occurred_at for r in records), default=recorded)
    head_revision = records[-1].revision if records else 0
    return Snapshot(
        id=snapshot_id,
        account_id=account_id,
        revision=head_revision,
        recorded_at=recorded,
        fact_as_of=fact_as_of,
        valuation_at=recorded,
        coverage=state.coverage,
        asset_scope=state.scope,
        known_cash=valued.known_cash,
        cash_subtotal=state.cash,
        holdings=valued.holdings,
        known_subtotal=valued.subtotal,
        total_assets=valued.total,
        unknowns=valued.unknowns,
        historical_unknowns=tuple(sorted(state.history)),
        reconciliations=tuple(state.comparisons),
        conflicts=tuple(state.conflicts),
        source_event_ids=tuple(r.id for r in records),
        excluded_prior_event_ids=tuple(state.excluded),
        reference=ObjectRef(kind="portfolio_snapshot", id=snapshot_id, revision=head_revision),
        price_coverage=valued.price_coverage,
        cost_method_version=COST_METHOD_VERSION,
        realized=tuple(state.realized_records),
        dividends=tuple(state.dividends),
        cost_unknowns=valued.cost_unknowns,
    )


def _select(
    records: list[EventRecord], *, trace: dict[UUID, dict[str, Any]] | None
) -> ProjectionState:
    """The active facts and the confirmed baseline they replay against."""

    reversed_ids = {r.request.fact.original_event_id for r in records if isinstance(r.request.fact, Reversal)}
    active = sorted((r for r in records if r.id not in reversed_ids and not isinstance(r.request.fact, Reversal)), key=lambda r: (r.request.fact.occurred_at, r.revision))
    opening = next((r for r in active if isinstance(r.request.fact, Balance) and r.request.fact.kind == "opening_balance"), None)
    balances = [r for r in active if isinstance(r.request.fact, Balance)]
    # Earlier backfills are included in the confirmed baseline. Same-time
    # later-recorded events are applied, never silently discarded.
    baseline = opening or (balances[0] if balances else None)
    baseline_key = (baseline.request.fact.occurred_at, baseline.revision) if baseline else None
    return ProjectionState(active=active, opening=opening, baseline_key=baseline_key, trace=trace)


class ProjectionEngine:
    """Replay one account's facts into a snapshot, with prices injected.

    The engine holds no session and no account: the caller passes the records
    and the projection instant, and the price source is a callable.
    """

    def __init__(self, prices: PriceLookup) -> None:
        self._prices = prices

    async def project(
        self,
        account_id: UUID,
        records: list[EventRecord],
        recorded: datetime,
        *,
        trace: dict[UUID, dict[str, Any]] | None = None,
    ) -> Projection:
        """Replay ``records`` as of ``recorded`` under the bounded precision.

        The engine establishes the arithmetic precision itself (the service's
        entry point sets the same value), so a direct caller - a test with an
        injected price loader - cannot silently fall back to the default
        28-digit context.
        """

        with localcontext() as context:
            # Bounded arithmetic precision, no binary floats or silent rounding.
            context.prec = max(128, 80 * len(records))
            return await self._replay(account_id, records, recorded, trace=trace)

    async def _replay(
        self,
        account_id: UUID,
        records: list[EventRecord],
        recorded: datetime,
        *,
        trace: dict[UUID, dict[str, Any]] | None,
    ) -> Projection:
        state = _select(records, trace=trace)
        for record in state.active:
            state.begin_row(record)
            fact = record.request.fact
            if isinstance(fact, Balance):
                apply_balance(state, record)
                continue
            if state.baseline_key is not None and (fact.occurred_at, record.revision) < state.baseline_key:
                state.exclude_before_baseline(record)
                continue
            handler = HANDLERS.get(type(fact))
            if handler is not None:
                handler(state, record)
            state.close_row(record, fact)
        valued = await self._value_holdings(state, recorded)
        snapshot = build_snapshot(
            account_id=account_id,
            records=records,
            recorded=recorded,
            state=state,
            valued=valued,
        )
        return Projection(snapshot=snapshot, confirmable_deficits=state.deficits)

    async def _value_holdings(self, state: ProjectionState, recorded: datetime) -> Materialised:
        """Value every held listing at the projection instant's PIT prices."""

        holdings: list[SnapshotHolding] = []
        unknowns = set(state.cash_unknown)
        cost_unknowns: set[str] = set()
        if state.opening is None:
            unknowns.add("no_active_opening")
        if state.coverage == "partial":
            unknowns.add("unlisted_assets_unknown")
        price_complete = True
        known_cash = state.cash if not state.cash_unknown else None
        subtotal = known_cash
        # The cutoff instant is fixed before the page is fetched: the Shanghai
        # business date and the instant the prices must not postdate.
        trade_date = recorded.astimezone(SHANGHAI_TZ).date()
        positioned = sorted(state.quantities.items(), key=lambda pair: int(pair[0].id))
        prices = await self._prices(
            [ref.id for ref, _quantity in positioned],
            trade_date=trade_date,
            knowledge_cutoff=recorded,
        )
        for ref, quantity in positioned:
            confirmed = quantity if ref not in state.restricted else None
            if confirmed is None:
                unknowns.add(f"unknown_quantity:{ref.id}")
            price = prices.get(ref.id)
            if price is None and confirmed != 0:
                price_complete = False
                unknowns.add(f"missing_price:{ref.id}")
            value = Decimal(0) if confirmed == 0 else confirmed * price.close if confirmed is not None and price is not None else None
            if value is not None:
                subtotal = (subtotal or Decimal(0)) + value
            cost = state.costs.get(ref)
            cost_reason = cost.reason if cost is not None else None
            if quantity == 0:
                # A flat position carries no remaining cost (plan 2.7).
                remaining_cost: Decimal | None = Decimal(0)
                cost_reason = None
            else:
                remaining_cost = cost.remaining if cost is not None else None
                if cost is None and cost_reason is None:
                    cost_reason = "cost_never_confirmed"
            average_cost = remaining_cost / quantity if remaining_cost is not None and quantity > 0 else None
            opened_on_value = cost.opened_on if cost is not None and quantity > 0 else None
            if quantity != 0 and cost_reason is not None:
                cost_unknowns.add(f"{cost_reason}:{ref.id}")
            holdings.append(
                SnapshotHolding(
                    listing_ref=ref,
                    confirmed_quantity=confirmed,
                    quantity_subtotal=quantity,
                    price=price.close if price else None,
                    price_ref=ObjectRef(kind="market_snapshot", id=price.id, revision=price.version) if price else None,
                    price_date=price.trade_date if price else None,
                    market_value=value,
                    remaining_cost=remaining_cost,
                    average_cost=average_cost,
                    opened_on=opened_on_value,
                    cost_unknown_reason=cost_reason if quantity != 0 else None,
                )
            )
        total = subtotal if state.coverage == "complete" and not unknowns and not any(q < 0 for q in state.quantities.values()) else None
        if total is not None and total > 0:
            with localcontext() as ratio_context:
                ratio_context.prec = 40
                holdings = [
                    h.model_copy(
                        update={"weight": h.market_value / total if h.market_value is not None else None}
                    )
                    for h in holdings
                ]
        return Materialised(
            holdings=tuple(holdings),
            unknowns=tuple(sorted(unknowns)),
            cost_unknowns=tuple(sorted(cost_unknowns | {f"historical:{reason}" for reason in state.cost_history})),
            price_coverage="complete" if price_complete else "partial",
            known_cash=known_cash,
            subtotal=subtotal,
            total=total,
        )
