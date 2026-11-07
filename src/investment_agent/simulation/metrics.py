"""Unadjusted-price NAV and fixed-capital metrics with explicit data quality."""

import json
from datetime import date, datetime, time
from decimal import ROUND_HALF_UP, Decimal
from hashlib import sha256
from typing import Literal, cast

from investment_agent.clock import SHANGHAI_TZ
from investment_agent.investor.common import Contract, utc_timestamp
from investment_agent.simulation.contracts import (
    Identifier,
    NavSnapshot,
    PositiveInt,
    PositiveMoney,
    RulePackage,
    SimulationAccount,
)
from investment_agent.simulation.errors import SimulationError
from investment_agent.simulation.risk import exact_decimal, stable_id

METRIC_QUANTUM = Decimal("0.000000000000000001")


class ValuationMark(Contract):
    listing_id: PositiveInt
    price: PositiveMoney
    price_date: date
    source_ref: Identifier
    known_at: datetime
    verified: bool = False
    stale_reason: str | None = None


def _metric(value: Decimal) -> Decimal:
    return value.quantize(METRIC_QUANTUM, rounding=ROUND_HALF_UP)


@exact_decimal
def cumulative_return(equity: Decimal, initial_equity: Decimal) -> Decimal:
    if (
        not all(isinstance(v, Decimal) and v.is_finite() for v in (equity, initial_equity))
        or initial_equity <= 0
    ):
        raise SimulationError(
            "invalid_config", "return requires finite Decimal equity and positive initial equity"
        )
    return _metric(equity / initial_equity - 1)


@exact_decimal
def maximum_drawdown(equities: tuple[Decimal, ...]) -> Decimal:
    if (
        not equities
        or any(not isinstance(v, Decimal) or not v.is_finite() or v < 0 for v in equities)
        or equities[0] <= 0
    ):
        raise SimulationError(
            "invalid_config",
            "drawdown requires nonnegative finite NAV history with positive anchor",
        )
    peak = equities[0]
    worst = Decimal(0)
    for equity in equities:
        peak = max(peak, equity)
        worst = max(worst, 1 - equity / peak)
    return _metric(worst)


@exact_decimal
def value_account(
    account: SimulationAccount,
    valuation_date: date,
    marks: tuple[ValuationMark, ...],
    rules: RulePackage,
    *,
    created_at: datetime,
    history: tuple[NavSnapshot, ...] = (),
    blocking_reasons: tuple[str, ...] = (),
) -> NavSnapshot:
    """Stale NAV remains inspectable; only uninterrupted complete data has metrics."""
    created_at = utc_timestamp(created_at)
    if len({mark.listing_id for mark in marks}) != len(marks):
        raise SimulationError("incomplete_data", "duplicate valuation mark")
    scope = {
        key: getattr(account, key) for key in ("experiment_id", "participant_id", "account_id")
    }
    previous_date = None
    for snapshot in history:
        if any(getattr(snapshot, key) != value for key, value in scope.items()):
            raise SimulationError("scope_mismatch", "NAV history belongs to another account")
        if (
            snapshot.valuation_version != rules.valuation_version
            or snapshot.metrics_version != rules.metrics_version
        ):
            raise SimulationError("unsupported_rule", "NAV history uses another metrics policy")
        if snapshot.valuation_date >= valuation_date or (
            previous_date is not None and snapshot.valuation_date <= previous_date
        ):
            raise SimulationError(
                "invalid_config", "NAV history must be strictly increasing before valuation date"
            )
        if snapshot.created_at > created_at or snapshot.account_revision > account.revision:
            raise SimulationError("future_evidence", "NAV history was not available for this account revision")
        previous_date = snapshot.valuation_date
    if created_at < datetime.combine(valuation_date, time(15), SHANGHAI_TZ):
        raise SimulationError("future_evidence", "daily NAV cannot precede valuation-day close")
    incomplete = list(blocking_reasons)
    stale = []
    by_listing = {mark.listing_id: mark for mark in marks}
    refs = []
    position_value = Decimal(0)
    for lot in account.state.lots:
        mark = by_listing.get(lot.listing_id)
        if mark is None:
            incomplete.append(f"missing_price:{lot.listing_id}")
            continue
        if (not mark.verified or mark.known_at > created_at or mark.price_date > valuation_date
                or mark.known_at < datetime.combine(mark.price_date, time(15), SHANGHAI_TZ)):
            incomplete.append(f"unverified_price:{lot.listing_id}")
            continue
        if mark.price_date < valuation_date:
            if mark.stale_reason is None:
                incomplete.append(f"unexplained_price_gap:{lot.listing_id}")
                continue
            stale.append(f"stale_price:{lot.listing_id}:{mark.stale_reason}")
        elif mark.stale_reason is not None:
            stale.append(f"stale_price:{lot.listing_id}:{mark.stale_reason}")
        position_value += lot.quantity * mark.price
        refs.append(mark.source_ref)
    state = account.state
    equity = state.cash + state.receivables + position_value - state.payables
    if equity < 0:
        incomplete.append("negative_equity_outside_unlevered_metrics")
    quality: Literal["incomplete", "stale", "complete"] = "incomplete" if incomplete else "stale" if stale else "complete"
    reasons = tuple(dict.fromkeys(incomplete + stale))
    result_return = result_drawdown = None
    if quality == "complete" and all(snapshot.quality == "complete" for snapshot in history):
        result_return = cumulative_return(equity, account.initial_cash)
        result_drawdown = maximum_drawdown(
            (account.initial_cash, *(cast(Decimal, snapshot.equity) for snapshot in history), equity)
        )
    payload = {
        "account": account.model_dump(mode="json"),
        "date": valuation_date.isoformat(),
        "marks": [
            mark.model_dump(mode="json") for mark in sorted(marks, key=lambda m: m.listing_id)
        ],
        "rules": rules.model_dump(mode="json"),
        "history": [n.model_dump(mode="json") for n in history],
        "blocking_reasons": blocking_reasons,
    }
    digest = sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return NavSnapshot(
        **scope,
        id=stable_id("nav", account.account_id, valuation_date, digest),
        account_revision=account.revision,
        valuation_date=valuation_date,
        cash=state.cash,
        receivables=state.receivables,
        payables=state.payables,
        position_value=None if incomplete else position_value,
        equity=None if incomplete else equity,
        quality=quality,
        quality_reasons=reasons,
        valuation_version=rules.valuation_version,
        metrics_version=rules.metrics_version,
        input_hash=digest,
        price_refs=tuple(dict.fromkeys(refs)),
        cumulative_return=result_return,
        max_drawdown=result_drawdown,
        created_at=created_at,
    )
