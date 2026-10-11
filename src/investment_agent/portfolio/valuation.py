"""Daily valuation over one ledger snapshot (watch-holding-plan 2.10).

Facts and valuation are separate versions: appending facts advances the account
revision and writes an immutable snapshot; prices only ever produce new
``portfolio_valuation`` rows bound to ``(snapshot, valuation_date, input digest,
algorithm version)``. A same-input re-run reuses the stored row, a late or
revised price becomes a new identity, and the account's current-valuation
pointer is only moved under the ledger revision it was computed from - if facts
advanced meanwhile the old result stays audit and the read reports "更新中".
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, localcontext
from typing import Any, Literal
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.clock import SHANGHAI_TZ, shanghai_today
from investment_agent.investor.common import Contract, DecimalValue, ObjectRef
from investment_agent.investor.foundations import InvestorError, storage_conflicts
from investment_agent.portfolio.contracts import Snapshot
from investment_agent.portfolio.service import PortfolioService, digest
from investment_agent.storage.models.pit_snapshot import MarketSnapshot
from investment_agent.storage.models.portfolio import PortfolioAccount, PortfolioValuation

VALUATION_ALGORITHM = "daily_valuation_v1"
MAX_PRICE_STALENESS_DAYS = 5
"""Trading days after the last close before a price counts as stale (plan 2.10)."""


def default_knowledge_cutoff(valuation_date: date) -> datetime:
    """Beijing day-end for historical dates, capped at actual knowledge now."""

    end_of_day = datetime(
        valuation_date.year,
        valuation_date.month,
        valuation_date.day,
        23,
        59,
        59,
        999999,
        tzinfo=SHANGHAI_TZ,
    )
    return min(end_of_day.astimezone(UTC), datetime.now(UTC))


class ValuationHolding(Contract):
    listing_ref: ObjectRef
    quantity: DecimalValue | None
    price: DecimalValue | None = None
    price_ref: ObjectRef | None = None
    price_date: date | None = None
    market_value: DecimalValue | None = None
    weight: DecimalValue | None = None
    remaining_cost: DecimalValue | None = None
    average_cost: DecimalValue | None = None
    opened_on: date | None = None
    holding_days: int | None = None
    floating_pnl: DecimalValue | None = None
    gain_vs_cost: DecimalValue | None = None
    cost_unknown_reason: str | None = None
    price_unknown_reason: str | None = None


class Valuation(Contract):
    id: UUID
    account_id: UUID
    snapshot_id: UUID
    ledger_revision: int
    valuation_date: date
    knowledge_cutoff: datetime
    algorithm_version: str
    input_digest: str
    currency: Literal["CNY"] = "CNY"
    coverage: Literal["complete", "partial"]
    known_cash: DecimalValue | None
    holdings: tuple[ValuationHolding, ...]
    market_value_subtotal: DecimalValue | None
    """Known holdings only; never called total assets (plan 2.9)."""
    total_assets: DecimalValue | None
    """NAV, only when scope, cash, quantities and prices are all usable."""
    weight_available: bool
    unknowns: tuple[str, ...]
    created_at: datetime
    reference: ObjectRef


def trading_days_after(
    start: date,
    end: date,
    is_trading_day: Callable[[date], bool | None] | None,
) -> int | None:
    """Trading days in ``(start, end]``; natural days without a calendar.

    ``None`` when a day's calendar status is unreviewed: staleness cannot be
    settled then, and silently counting weekdays as reviewed market days is
    exactly what plan 2.10 / WP10 forbids.
    """

    if end <= start:
        return 0
    if is_trading_day is None:
        return (end - start).days
    days = 0
    cursor = start
    while cursor < end:
        cursor += timedelta(days=1)
        status = is_trading_day(cursor)
        if status is None:
            return None
        if status:
            days += 1
    return days


class ValuationService:
    def __init__(self, session: AsyncSession, owner: str = "local_owner") -> None:
        self.session = session
        self.owner = owner

    async def _account(self, account_id: UUID) -> PortfolioAccount:
        row = await self.session.scalar(
            select(PortfolioAccount).where(
                PortfolioAccount.id == str(account_id), PortfolioAccount.owner_id == self.owner
            )
        )
        if row is None:
            raise InvestorError("not_found", "Portfolio account not found")
        return row

    async def compute(
        self,
        snapshot: Snapshot,
        *,
        valuation_date: date,
        knowledge_cutoff: datetime,
        is_trading_day: Callable[[date], bool | None] | None = None,
        max_staleness_days: int = MAX_PRICE_STALENESS_DAYS,
    ) -> Valuation:
        """Pure price/P&L projection of one snapshot (no persistence)."""

        if knowledge_cutoff.tzinfo is None or max_staleness_days < 0:
            raise InvestorError(
                "invalid_reference", "Valuation needs an aware cutoff and nonnegative price age"
            )
        if snapshot.fact_as_of.astimezone(SHANGHAI_TZ).date() > valuation_date:
            raise InvestorError(
                "invalid_reference", "Snapshot contains facts after the valuation date"
            )
        with localcontext() as context:
            context.prec = max(
                [
                    128,
                    *(
                        len(h.remaining_cost.as_tuple().digits)
                        for h in snapshot.holdings
                        if h.remaining_cost is not None
                    ),
                ]
            )
            return await self._compute_decimal(
                snapshot,
                valuation_date=valuation_date,
                knowledge_cutoff=knowledge_cutoff,
                is_trading_day=is_trading_day,
                max_staleness_days=max_staleness_days,
            )

    async def _compute_decimal(
        self,
        snapshot: Snapshot,
        *,
        valuation_date: date,
        knowledge_cutoff: datetime,
        is_trading_day: Callable[[date], bool | None] | None,
        max_staleness_days: int,
    ) -> Valuation:

        holdings: list[ValuationHolding] = []
        unknowns: set[str] = {
            reason for reason in snapshot.unknowns if reason == "no_active_opening"
        }
        prices: list[dict[str, Any]] = []
        subtotal: Decimal | None = Decimal(0)
        valued_complete = True
        for item in snapshot.holdings:
            ref = item.listing_ref
            quantity = item.confirmed_quantity
            price = await self.session.scalar(
                select(MarketSnapshot)
                .where(
                    MarketSnapshot.listing_id == ref.id,
                    MarketSnapshot.adjust == "none",
                    MarketSnapshot.trade_date <= valuation_date,
                    MarketSnapshot.captured_at <= knowledge_cutoff,
                    MarketSnapshot.close > 0,
                )
                .order_by(
                    MarketSnapshot.trade_date.desc(),
                    MarketSnapshot.version.desc(),
                    MarketSnapshot.id.desc(),
                )
                .limit(1)
            )
            if price is not None:
                prices.append(
                    {
                        "listing_id": ref.id,
                        "price_id": price.id,
                        "version": price.version,
                        "trade_date": price.trade_date.isoformat(),
                        "close": str(price.close),
                    }
                )
            price_reason: str | None = None
            effective_price: Decimal | None = price.close if price is not None else None
            if quantity is None:
                unknowns.add(f"unknown_quantity:{ref.id}")
            if quantity is not None and quantity != 0:
                if price is None:
                    effective_price = None
                    price_reason = "missing_price"
                    unknowns.add(f"missing_price:{ref.id}")
                else:
                    age = trading_days_after(price.trade_date, valuation_date, is_trading_day)
                    if age is None:
                        # An unreviewed calendar year cannot settle staleness
                        # (plan 2.10 / WP10): report the limitation instead of
                        # treating weekdays as reviewed market data.
                        effective_price = None
                        price_reason = "staleness_unreviewed"
                        unknowns.add(f"price_staleness_unreviewed:{ref.id}")
                    elif age > max_staleness_days:
                        effective_price = None
                        price_reason = "stale_price"
                        unknowns.add(f"stale_price:{ref.id}")
            market_value = (
                Decimal(0)
                if quantity == 0
                else quantity * effective_price
                if quantity is not None and effective_price is not None
                else None
            )
            if quantity != 0 and market_value is None:
                valued_complete = False
            if market_value is not None:
                subtotal = (subtotal or Decimal(0)) + market_value
            holding_days = (
                (valuation_date - item.opened_on).days
                if item.opened_on is not None and quantity not in {None, Decimal(0)}
                else None
            )
            floating = (
                market_value - item.remaining_cost
                if market_value is not None and item.remaining_cost is not None
                else None
            )
            gain = (
                floating / item.remaining_cost
                if floating is not None
                and item.remaining_cost is not None
                and item.remaining_cost > 0
                else None
            )
            holdings.append(
                ValuationHolding(
                    listing_ref=ref,
                    quantity=quantity,
                    price=effective_price,
                    price_ref=(
                        ObjectRef(kind="market_snapshot", id=price.id, revision=price.version)
                        if price is not None and effective_price is not None
                        else None
                    ),
                    price_date=price.trade_date
                    if price is not None and effective_price is not None
                    else None,
                    market_value=market_value,
                    remaining_cost=item.remaining_cost,
                    average_cost=item.average_cost,
                    opened_on=item.opened_on if quantity not in {None, Decimal(0)} else None,
                    holding_days=holding_days,
                    floating_pnl=floating,
                    gain_vs_cost=gain,
                    cost_unknown_reason=item.cost_unknown_reason,
                    price_unknown_reason=price_reason,
                )
            )
        if snapshot.coverage == "partial":
            unknowns.add("unlisted_assets_unknown")
        if snapshot.known_cash is None:
            unknowns.add("known_cash_missing")
        nav: Decimal | None = None
        if (
            snapshot.coverage == "complete"
            and snapshot.known_cash is not None
            and valued_complete
            and not any(entry.startswith("unknown_quantity:") for entry in unknowns)
            and "no_active_opening" not in unknowns
        ):
            nav = snapshot.known_cash + (subtotal or Decimal(0))
        if nav is not None and nav <= 0:
            unknowns.add("nonpositive_nav_ratio_undefined")
        weight_available = nav is not None and nav > 0
        if weight_available:
            assert nav is not None
            with localcontext() as context:
                context.prec = 40
                holdings = [
                    h.model_copy(
                        update={
                            "weight": h.market_value / nav if h.market_value is not None else None
                        }
                    )
                    for h in holdings
                ]
        created = datetime.now(UTC)
        input_digest = digest(
            {
                "snapshot": snapshot.model_dump(mode="json"),
                "prices": prices,
                "valuation_date": valuation_date.isoformat(),
                # Identical resolved inputs reuse an earlier immutable result;
                # a later read alone is not new market information.
                "unknowns": sorted(unknowns),
                "algorithm": VALUATION_ALGORITHM,
                "max_staleness_days": max_staleness_days,
            }
        )
        valuation_id = uuid4()
        return Valuation(
            id=valuation_id,
            account_id=snapshot.account_id,
            snapshot_id=snapshot.id,
            ledger_revision=snapshot.revision,
            valuation_date=valuation_date,
            knowledge_cutoff=knowledge_cutoff,
            algorithm_version=VALUATION_ALGORITHM,
            input_digest=input_digest,
            coverage=snapshot.coverage,
            known_cash=snapshot.known_cash,
            holdings=tuple(holdings),
            market_value_subtotal=subtotal,
            total_assets=nav,
            weight_available=weight_available,
            unknowns=tuple(sorted(unknowns)),
            created_at=created,
            reference=ObjectRef(kind="portfolio_valuation", id=valuation_id, revision=1),
        )

    async def _stored(self, valuation: Valuation) -> Valuation | None:
        row = await self.session.scalar(
            select(PortfolioValuation).where(
                PortfolioValuation.account_id == str(valuation.account_id),
                PortfolioValuation.ledger_revision == valuation.ledger_revision,
                PortfolioValuation.valuation_date == valuation.valuation_date,
                PortfolioValuation.algorithm_version == valuation.algorithm_version,
                PortfolioValuation.input_digest == valuation.input_digest,
            )
        )
        return Valuation.model_validate(row.payload) if row is not None else None

    @storage_conflicts
    async def ensure(
        self,
        account_id: UUID,
        *,
        valuation_date: date | None = None,
        now: datetime | None = None,
        is_trading_day: Callable[[date], bool | None] | None = None,
        max_staleness_days: int = MAX_PRICE_STALENESS_DAYS,
    ) -> Valuation | None:
        """The current valuation for today, computing and storing it when absent.

        Returns ``None`` when facts advanced while computing: the audit row is
        kept, the current pointer is not moved, and the caller reports
        "估值更新中" instead of mixing new quantities with the old NAV.
        """

        # Reserve before reading on SQLite; avoid upgrading a stale read
        # transaction after a concurrent fact/valuation commit.
        await PortfolioService(self.session, self.owner)._reserve()
        day = valuation_date or shanghai_today()
        cutoff = now or default_knowledge_cutoff(day)
        snapshot = await PortfolioService(self.session, self.owner).snapshot(account_id)
        computed = await self.compute(
            snapshot,
            valuation_date=day,
            knowledge_cutoff=cutoff,
            is_trading_day=is_trading_day,
            max_staleness_days=max_staleness_days,
        )
        stored = await self._stored(computed)
        if stored is not None and stored.knowledge_cutoff > cutoff:
            # A historical request may use identical prices but must not claim
            # knowledge from a later cutoff than the caller requested.
            computed = computed.model_copy(
                update={
                    "input_digest": digest(
                        {"inputs": computed.input_digest, "cutoff": cutoff.isoformat()}
                    )
                }
            )
            stored = await self._stored(computed)
        if stored is None:
            self.session.add(
                PortfolioValuation(
                    id=str(computed.id),
                    account_id=str(computed.account_id),
                    snapshot_id=str(computed.snapshot_id),
                    ledger_revision=computed.ledger_revision,
                    valuation_date=computed.valuation_date,
                    algorithm_version=computed.algorithm_version,
                    input_digest=computed.input_digest,
                    payload=computed.model_dump(mode="json"),
                )
            )
            await self.session.flush()
        desired = stored or computed
        account = await self._account(account_id)
        if account.revision != snapshot.revision:
            return None
        current = await self.current(account_id)
        if current is not None and (current.valuation_date, current.knowledge_cutoff) > (
            desired.valuation_date,
            desired.knowledge_cutoff,
        ):
            return desired  # Older requests only create/read audit results.
        result = await self.session.execute(
            update(PortfolioAccount)
            .where(
                PortfolioAccount.id == str(account_id),
                PortfolioAccount.owner_id == self.owner,
                PortfolioAccount.revision == snapshot.revision,
                PortfolioAccount.current_valuation_id == account.current_valuation_id,
            )
            .values(current_valuation_id=str(desired.id))
        )
        await self.session.flush()
        if not result.rowcount:  # type: ignore[attr-defined]
            # Facts advanced meanwhile: keep the immutable audit row only.
            return None
        return desired

    async def current(self, account_id: UUID) -> Valuation | None:
        row = await self._account(account_id)
        if row.current_valuation_id is None:
            return None
        stored = await self.session.get(PortfolioValuation, row.current_valuation_id)
        if stored is None or stored.account_id != row.id or stored.ledger_revision != row.revision:
            return None
        return Valuation.model_validate(stored.payload)

    async def get(self, account_id: UUID, valuation_id: UUID) -> Valuation:
        row = await self._account(account_id)
        stored = await self.session.get(PortfolioValuation, str(valuation_id))
        if stored is None or stored.account_id != row.id:
            raise InvestorError("not_found", "Portfolio valuation not found")
        return Valuation.model_validate(stored.payload)

    async def latest_at(self, account_id: UUID, *, cutoff: datetime) -> Valuation | None:
        """The newest valuation the knowledge ``cutoff`` could know (plan 2.10).

        Historical research binds this instead of today's valuation; a later
        one would import knowledge after the task's cutoff.
        """

        await self._account(account_id)
        if cutoff.tzinfo is None:
            raise InvestorError("invalid_reference", "A valuation cutoff must be timezone-aware")
        best: Valuation | None = None
        for valuation in await self.history(account_id, limit=200):
            if valuation.knowledge_cutoff > cutoff:
                continue
            if best is None or (valuation.valuation_date, valuation.knowledge_cutoff) > (
                best.valuation_date,
                best.knowledge_cutoff,
            ):
                best = valuation
        return best

    async def history(self, account_id: UUID, *, limit: int = 50) -> tuple[Valuation, ...]:
        await self._account(account_id)
        rows = await self.session.scalars(
            select(PortfolioValuation)
            .where(PortfolioValuation.account_id == str(account_id))
            .order_by(
                PortfolioValuation.ledger_revision.desc(), PortfolioValuation.created_at.desc()
            )
            .limit(limit)
        )
        return tuple(Valuation.model_validate(row.payload) for row in rows)
