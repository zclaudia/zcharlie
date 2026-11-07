"""One conservative full-fill-or-expire opening-price proxy, never intraday inference."""

from datetime import date, datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, localcontext
from typing import Literal, Self

from pydantic import model_validator

from investment_agent.clock import SHANGHAI_TZ
from investment_agent.investor.common import Contract, utc_timestamp
from investment_agent.simulation.contracts import (
    Identifier,
    NonnegativeMoney,
    PositiveInt,
    PositiveMoney,
    RulePackage,
    SimulationFill,
    SimulationOrder,
)
from investment_agent.simulation.data import ExecutionEligibility
from investment_agent.simulation.errors import SimulationError
from investment_agent.simulation.risk import (
    ExecutionTerms,
    check_terms,
    exact_decimal,
    fee_for,
    stable_id,
)
from investment_agent.storage.models.pit_snapshot import MarketSnapshot


class ExecutionBar(Contract):
    listing_id: PositiveInt
    trade_date: date
    open: PositiveMoney | None
    volume: NonnegativeMoney | None
    suspended: bool | None
    source_ref: Identifier
    confirmed_at: datetime
    verified: bool = False


class MatchResult(Contract):
    status: Literal["filled", "expired", "awaiting_data", "blocked"]
    fill: SimulationFill | None = None
    reason: str | None = None

    @model_validator(mode="after")
    def outcome_matches_fill(self) -> Self:
        if (self.status == "filled") != (self.fill is not None):
            raise ValueError("only filled result contains a fill")
        if self.status != "filled" and not self.reason:
            raise ValueError("unfilled result requires a reason")
        return self


@exact_decimal
def match_daily_open(
    order: SimulationOrder,
    bar: ExecutionBar | None,
    terms: ExecutionTerms | None,
    rules: RulePackage,
    *,
    confirmed_at: datetime,
) -> MatchResult:
    """Missing evidence waits; proven non-execution expires without invented fills."""
    confirmed_at = utc_timestamp(confirmed_at)
    if order.status != "locked":
        raise SimulationError("invalid_transition", "only locked orders can be matched")
    if order.rule_version != rules.version:
        raise SimulationError("unsupported_rule", "order rule differs from frozen package")
    if (order.valid_from != order.valid_until
            or order.valid_from.astimezone(SHANGHAI_TZ).date() != order.execution_date):
        raise SimulationError("invalid_transition", "daily opening order has an invalid execution window")
    if terms is None or bar is None:
        return MatchResult(status="awaiting_data", reason="missing_execution_evidence")
    try:
        check_terms(terms, order.listing_id, order.execution_date, rules)
    except SimulationError as exc:
        if exc.code == "incomplete_data":
            return MatchResult(status="awaiting_data", reason=str(exc))
        if exc.code == "settlement_blocked":
            return MatchResult(status="blocked", reason=str(exc))
        raise
    if bar.listing_id != order.listing_id or bar.trade_date != order.execution_date:
        raise SimulationError("scope_mismatch", "bar does not match order listing/session")
    if (not bar.verified or bar.confirmed_at > confirmed_at or terms.known_at > confirmed_at
            or bar.confirmed_at < order.valid_from):
        return MatchResult(status="awaiting_data", reason="unverified_execution_evidence")
    if confirmed_at < order.valid_from:
        raise SimulationError("invalid_transition", "execution event has not occurred")
    if bar.suspended is None or bar.volume is None:
        return MatchResult(status="awaiting_data", reason="missing_execution_status")
    if bar.suspended or bar.volume == 0:
        return MatchResult(status="expired", reason="suspended_or_zero_volume")
    if bar.open is None:
        return MatchResult(status="awaiting_data", reason="missing_open_price")
    if bar.open % terms.tick_size or not terms.lower_limit <= bar.open <= terms.upper_limit:
        return MatchResult(status="blocked", reason="invalid_open_price")
    if (order.side == "buy" and bar.open >= terms.upper_limit) or (
        order.side == "sell" and bar.open <= terms.lower_limit
    ):
        return MatchResult(status="expired", reason="adverse_limit_open")
    with localcontext() as ctx:
        ctx.prec = 140
        rate = rules.slippage.adverse_rate
        raw = bar.open * (1 + rate if order.side == "buy" else 1 - rate)
        rounding = ROUND_CEILING if order.side == "buy" else ROUND_FLOOR
        price = (raw / terms.tick_size).to_integral_value(rounding=rounding) * terms.tick_size
        if price <= 0 or not terms.lower_limit <= price <= terms.upper_limit:
            return MatchResult(status="expired", reason="slippage_outside_legal_limits")
        if (order.side == "buy" and price > order.protection_price) or (
            order.side == "sell" and price < order.protection_price
        ):
            return MatchResult(status="expired", reason="price_protection")
        fee = fee_for(order.quantity, price, order.side, rules)
        if order.side == "buy" and order.quantity * price + fee > order.reserved_cash:
            raise SimulationError("insufficient_cash", "fill exceeds protected cash reservation")
    execution_key = stable_id("execution", order.id, order.execution_date, rules.version)
    scope = {
        field: getattr(order, field) for field in ("experiment_id", "participant_id", "account_id")
    }
    fill = SimulationFill(
        **scope,
        id=stable_id("fill", execution_key, 1),
        round_id=order.round_id,
        order_id=order.id,
        execution_key=execution_key,
        listing_id=order.listing_id,
        side=order.side,
        quantity=order.quantity,
        price=price,
        fee=fee,
        economic_at=order.valid_from,
        confirmed_at=confirmed_at,
        market_data_ref=bar.source_ref,
        rule_version=rules.version,
    )
    return MatchResult(status="filled", fill=fill)


def terms_from_evidence(eligibility: ExecutionEligibility) -> ExecutionTerms:
    """Adapt S03's hash-checked ExecutionEligibility after its trusted scope gate.

    No legal limits or corporate action completeness are inferred here. A caller
    must obtain eligibility for the order's experiment/listing/session; match
    independently checks the listing, session and frozen version again.
    """
    if not eligibility.ready or eligibility.evidence is None or not eligibility.evidence_id:
        raise SimulationError("incomplete_data", "execution eligibility is not ready")
    proof = eligibility.evidence
    return ExecutionTerms(
        listing_id=proof.listing_id,
        trade_date=proof.trade_date,
        rule_version=proof.rule_version,
        source_ref=eligibility.evidence_id,
        verified=proof.rule_verified,
        tick_size=proof.tick_size,
        lot_size=proof.lot_size,
        upper_limit=proof.upper_limit,
        lower_limit=proof.lower_limit,
        known_at=max(proof.published_at, proof.captured_at),
        corporate_actions_complete=proof.corporate_actions_complete,
        unsupported_corporate_actions=proof.unsupported_corporate_actions,
    )


def bar_from_snapshot(snapshot: MarketSnapshot) -> ExecutionBar:
    """Adapt an explicitly selected unadjusted MarketSnapshot, preserving its clock.

    Selection, archive integrity and experiment scope belong to S03/S04. Zero or
    missing open remains unknown rather than becoming a fabricated trade price.
    SQLite UTC timestamps must be made timezone aware by the repository adapter.
    """
    if snapshot.adjust != "none":
        raise SimulationError("incomplete_data", "execution requires unadjusted market prices")
    if snapshot.id is None:
        raise SimulationError("incomplete_data", "execution market row is not archived")
    return ExecutionBar(
        listing_id=snapshot.listing_id,
        trade_date=snapshot.trade_date,
        open=snapshot.open if snapshot.open is not None and snapshot.open > 0 else None,
        volume=snapshot.volume,
        suspended=None if snapshot.trade_status not in {0, 1} else snapshot.trade_status == 0,
        source_ref=f"market_snapshot:{snapshot.id}",
        confirmed_at=snapshot.captured_at,
        verified=True,
    )
