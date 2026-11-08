"""Optional portfolio context for candidate ranking (P1 portfolio layer).

This module connects the *existing* mandate constraints
(:class:`~investment_agent.investor.contracts.PortfolioConstraints`) and an
optional liquidity gate to candidate ranking/display.  It deliberately does
not merge or de-duplicate candidates: the listing merge in
:mod:`investment_agent.candidates.screen_intake` and the round-robin de-dup in
:mod:`investment_agent.candidates.ranking` stay the only such mechanisms.

Design rules (see ``docs/review/valuation-methodology-review-2026-11-07.md``
item 10 / C6):

* the context is **optional**; ``None`` everywhere keeps the research-only
  ranking byte-for-byte unchanged;
* single-name and sector checks describe the *current frozen portfolio
  exposure* against the mandate limit.  They are not a trade simulation, and
  they never reorder by screen strength (cross-template strength is not
  comparable);
* cash constraints are account/scenario level.  A single candidate is not a
  trade, so the check is explicitly ``not_applicable`` with a reason, never a
  silent pass;
* liquidity prefers the 20-trading-day average traded amount.  Free-float
  market cap is never derived from total shares / total market cap: when the
  free-float share count is unavailable the value is ``MISSING``
  (``unknown_reason``) and the assessment is not a pass;
* every threshold is supplied by the caller in an explicit policy marked
  ``uncalibrated``.  This module defines no numeric default that could look
  calibrated.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import Field, model_validator

from investment_agent.clock import SHANGHAI_TZ
from investment_agent.investor.common import Contract, DecimalValue, ObjectRef, Quantity, Text
from investment_agent.portfolio.calculation_contracts import Nonnegative
from investment_agent.portfolio.suitability_contracts import FrozenAccountInputs
from investment_agent.storage.repositories.financial_snapshot import (
    FinancialSnapshotRepository,
)
from investment_agent.storage.repositories.market_snapshot import MarketSnapshotRepository

LIQUIDITY_WINDOW_DAYS: Literal[20] = 20
"""The review asks for a 20-trading-day average; it is a window, not a threshold."""
LIQUIDITY_LOOKBACK_DAYS = 60
"""Calendar lookback for 20 trading days; 60 also covers long holidays."""
BUDGET_BASIS: Literal["current_portfolio_exposure"] = "current_portfolio_exposure"
"""The budget checks describe frozen current exposure, not a proposed order."""

LiquidityState = Literal["pass", "insufficient", "missing", "not_configured", "not_assessed"]
BudgetState = Literal[
    "within_budget", "at_or_over_budget", "unknown", "not_configured", "not_applicable"
]
ContextState = Literal["eligible", "unverified", "not_assessed", "constrained"]

# Stable display partition used only when an explicit context is supplied.
# The key is constraint visibility, never a strength or expected return.
CONTEXT_ORDER: Mapping[ContextState, int] = {
    "eligible": 0,
    "unverified": 1,
    "not_assessed": 2,
    "constrained": 3,
}


class LiquidityPolicy(Contract):
    """Caller/config supplied thresholds; there is no calibrated default.

    Both fields default to ``None`` so nothing is assumed.  A policy without at
    least one explicit threshold is rejected instead of silently evaluating as
    a pass.  ``calibration_status`` is fixed to ``uncalibrated``: the layer
    refuses to imply that a caller-supplied number was validated.
    """

    assumption_source: Text
    calibration_status: Literal["uncalibrated"] = "uncalibrated"
    window_days: Literal[20] = LIQUIDITY_WINDOW_DAYS
    min_average_amount_20d: Nonnegative | None = None
    min_free_float_market_cap: Nonnegative | None = None

    @model_validator(mode="after")
    def at_least_one_explicit_threshold(self) -> LiquidityPolicy:
        if self.min_average_amount_20d is None and self.min_free_float_market_cap is None:
            raise ValueError("liquidity policy requires at least one explicit threshold")
        return self


class CandidatePortfolioContextRequest(Contract):
    """Explicit opt-in to attach portfolio information to a candidate ranking.

    ``None`` on the ranking request (the default) leaves the research-only
    ordering and entry payload unchanged.  ``include_mandate_budget`` can be
    disabled to request liquidity-only research annotations without a mandate
    budget projection.
    """

    include_mandate_budget: bool = True
    liquidity: LiquidityPolicy | None = None

    @model_validator(mode="after")
    def at_least_one_context(self) -> CandidatePortfolioContextRequest:
        if not self.include_mandate_budget and self.liquidity is None:
            raise ValueError("portfolio context request must enable budget or liquidity")
        return self


class LiquidityObservation(Contract):
    """Frozen market evidence for one listing.

    ``average_amount_20d`` is a :class:`Quantity`; insufficient history is an
    explicit unknown, never a zero.  ``free_float_market_cap`` is also a
    :class:`Quantity` and is only ever populated from free-float shares.  A
    missing free-float share count stays unknown; total shares are never read
    here.
    """

    listing_ref: ObjectRef
    as_of: datetime
    window_days: Literal[20] = LIQUIDITY_WINDOW_DAYS
    observed_amount_days: Annotated[int, Field(strict=True, ge=0, le=20)]
    average_amount_20d: Quantity
    free_float_market_cap: Quantity
    source_refs: tuple[ObjectRef, ...] = ()

    @model_validator(mode="after")
    def listing_identity(self) -> LiquidityObservation:
        if (
            self.listing_ref.kind != "listing"
            or not isinstance(self.listing_ref.id, int)
            or self.listing_ref.revision != 1
            or self.listing_ref.field_path is not None
        ):
            raise ValueError("liquidity observations require an exact listing reference")
        if self.average_amount_20d.unit != "CNY" or self.free_float_market_cap.unit != "CNY":
            raise ValueError("liquidity amounts are stated in CNY")
        if (self.average_amount_20d.value is None) != (self.observed_amount_days < self.window_days):
            raise ValueError(
                "a known 20-day average requires a complete observation window"
            )
        return self


class LiquidityCheck(Contract):
    metric: Literal["average_amount_20d", "free_float_market_cap"]
    state: Literal["pass", "insufficient", "missing", "not_configured"]
    observed: Quantity
    threshold: Quantity | None = None
    reason_codes: tuple[Text, ...] = ()

    @model_validator(mode="after")
    def explicit_reason(self) -> LiquidityCheck:
        if self.state in {"missing", "insufficient", "not_configured"} and not self.reason_codes:
            raise ValueError("a non-pass liquidity check requires an explicit reason")
        if self.state == "pass" and (
            self.observed.value is None or self.threshold is None
        ):
            raise ValueError("a pass requires a known observation and an explicit threshold")
        return self


class LiquidityAssessment(Contract):
    state: LiquidityState
    window_days: Literal[20] = LIQUIDITY_WINDOW_DAYS
    observed_amount_days: Annotated[int, Field(strict=True, ge=0, le=20)]
    average_amount_20d: Quantity
    free_float_market_cap: Quantity
    checks: tuple[LiquidityCheck, ...] = ()
    policy_source: Text | None = None
    calibration_status: Literal["uncalibrated"] | None = None
    # Frozen evidence time and exact market/financial snapshot references so a
    # display reader can see which bars and share count produced the numbers.
    observed_at: datetime | None = None
    source_refs: tuple[ObjectRef, ...] = ()
    reason_codes: tuple[Text, ...] = ()

    @model_validator(mode="after")
    def shape(self) -> LiquidityAssessment:
        if self.state == "not_assessed" and self.checks:
            raise ValueError("an unassessed liquidity result carries no checks")
        if self.state in {"missing", "insufficient", "not_configured"} and not self.reason_codes:
            raise ValueError("a non-pass liquidity result requires a reason")
        if self.state == "pass" and any(
            check.state not in {"pass", "not_configured"} for check in self.checks
        ):
            raise ValueError("a passing liquidity result cannot contain a failing check")
        return self


class BudgetCheck(Contract):
    dimension: Literal["single_name", "sector", "cash"]
    state: BudgetState
    limit: DecimalValue | None = None
    observed: DecimalValue | None = None
    headroom: DecimalValue | None = None
    reason_codes: tuple[Text, ...] = ()

    @model_validator(mode="after")
    def shape(self) -> BudgetCheck:
        if self.state == "not_configured" and (
            self.limit is not None or self.observed is not None or self.headroom is not None
        ):
            raise ValueError("a not-configured budget carries no limit or observation")
        if self.state == "not_applicable" and not self.reason_codes:
            raise ValueError("a not-applicable budget requires an explicit reason")
        if self.state in {"unknown"} and not self.reason_codes:
            raise ValueError("an unknown budget requires an explicit reason")
        if self.state in {"within_budget", "at_or_over_budget"} and (
            self.limit is None or self.observed is None
        ):
            raise ValueError("an evaluated budget requires its limit and observation")
        if self.headroom is not None and self.headroom < 0:
            raise ValueError("budget headroom cannot be negative")
        return self


class CandidateBudgetContext(Contract):
    """Mandate single-name / sector / cash projection for one listing."""

    basis: Literal["current_portfolio_exposure"] = BUDGET_BASIS
    mandate_ref: ObjectRef
    constraints_configured: bool
    single_name: BudgetCheck
    sector: BudgetCheck
    cash: BudgetCheck
    reason_codes: tuple[Text, ...] = ()

    @model_validator(mode="after")
    def identity(self) -> CandidateBudgetContext:
        if (
            self.mandate_ref.kind != "mandate"
            or not self.mandate_ref.id
            or self.mandate_ref.field_path is not None
        ):
            raise ValueError("mandate budget context requires an exact mandate reference")
        if (self.single_name.dimension, self.sector.dimension, self.cash.dimension) != (
            "single_name",
            "sector",
            "cash",
        ):
            raise ValueError("budget dimensions must be single_name, sector, cash")
        return self


class CandidatePortfolioContext(Contract):
    """Display-only portfolio annotation for one ranked candidate.

    The overall ``state`` is a visibility partition: it expresses known
    constraint status, not desirability.  Ranking never compares it with
    screen strength across templates.
    """

    budget: CandidateBudgetContext | None = None
    liquidity: LiquidityAssessment | None = None
    state: ContextState = "not_assessed"
    reason_codes: tuple[Text, ...] = ()

    @model_validator(mode="after")
    def at_least_one_source(self) -> CandidatePortfolioContext:
        if self.budget is None and self.liquidity is None:
            raise ValueError("a portfolio context requires a budget or liquidity result")
        return self


def _quantity(value: Decimal | None, *, reason: str | None = None) -> Quantity:
    return Quantity(
        value=value,
        unit="CNY",
        currency="CNY",
        unknown_reason=None if value is not None else (reason or "missing"),
    )


def _threshold(value: Decimal | None) -> Quantity | None:
    if value is None:
        return None
    return Quantity(value=value, unit="CNY", currency="CNY")


def _missing_reason(quantity: Quantity, fallback: str) -> str:
    return quantity.unknown_reason or fallback


def _liquidity_check(
    *,
    metric: Literal["average_amount_20d", "free_float_market_cap"],
    observed: Quantity,
    threshold: Decimal | None,
    not_configured_reason: str,
    below_threshold_reason: str,
    missing_fallback: str,
) -> LiquidityCheck | None:
    """One configured-or-not metric check; ``None`` means not configured."""

    if threshold is None:
        return LiquidityCheck(
            metric=metric,
            state="not_configured",
            observed=observed,
            reason_codes=(not_configured_reason,),
        )
    threshold_quantity = Quantity(value=threshold, unit="CNY", currency="CNY")
    if observed.value is None:
        return LiquidityCheck(
            metric=metric,
            state="missing",
            observed=observed,
            threshold=threshold_quantity,
            reason_codes=(_missing_reason(observed, missing_fallback),),
        )
    if observed.value < threshold:
        return LiquidityCheck(
            metric=metric,
            state="insufficient",
            observed=observed,
            threshold=threshold_quantity,
            reason_codes=(below_threshold_reason,),
        )
    return LiquidityCheck(
        metric=metric,
        state="pass",
        observed=observed,
        threshold=threshold_quantity,
    )


def assess_observation_liquidity(
    observation: LiquidityObservation | None, policy: LiquidityPolicy | None
) -> LiquidityAssessment:
    """Pure liquidity gate for one frozen observation.

    ``policy is None`` yields ``not_assessed`` (no threshold was supplied), not
    a pass.  A configured metric with missing evidence yields ``missing`` and
    therefore never satisfies the gate.
    """

    if policy is None:
        empty = _quantity(None, reason="liquidity_not_assessed")
        return LiquidityAssessment(
            state="not_assessed",
            observed_amount_days=observation.observed_amount_days if observation else 0,
            average_amount_20d=observation.average_amount_20d if observation else empty,
            free_float_market_cap=observation.free_float_market_cap if observation else empty,
            observed_at=observation.as_of if observation else None,
            source_refs=observation.source_refs if observation else (),
            reason_codes=("liquidity_not_assessed",),
        )

    average = (
        observation.average_amount_20d
        if observation is not None
        else _quantity(None, reason="market_observation_missing")
    )
    free_float = (
        observation.free_float_market_cap
        if observation is not None
        else _quantity(None, reason="market_observation_missing")
    )
    checks: list[LiquidityCheck] = []
    configured = False

    average_check = _liquidity_check(
        metric="average_amount_20d",
        observed=average,
        threshold=policy.min_average_amount_20d,
        not_configured_reason="average_amount_threshold_not_configured",
        below_threshold_reason="average_amount_below_threshold",
        missing_fallback="average_amount_missing",
    )
    if average_check is not None:
        checks.append(average_check)
        configured = True

    free_float_check = _liquidity_check(
        metric="free_float_market_cap",
        observed=free_float,
        threshold=policy.min_free_float_market_cap,
        not_configured_reason="free_float_market_cap_threshold_not_configured",
        below_threshold_reason="free_float_market_cap_below_threshold",
        missing_fallback="free_float_market_cap_missing",
    )
    if free_float_check is not None:
        checks.append(free_float_check)
        configured = True

    states = {check.state for check in checks}
    assessed_state: LiquidityState
    reasons: tuple[str, ...]
    if not configured:
        assessed_state = "not_configured"
        reasons = ("liquidity_threshold_not_configured",)
    elif "insufficient" in states:
        assessed_state = "insufficient"
        reasons = tuple(
            reason for check in checks for reason in check.reason_codes if check.state == "insufficient"
        )
    elif "missing" in states:
        assessed_state = "missing"
        reasons = tuple(
            reason for check in checks for reason in check.reason_codes if check.state == "missing"
        )
    else:
        assessed_state = "pass"
        reasons = ()

    return LiquidityAssessment(
        state=assessed_state,
        observed_amount_days=observation.observed_amount_days if observation else 0,
        average_amount_20d=average,
        free_float_market_cap=free_float,
        checks=tuple(checks),
        policy_source=policy.assumption_source,
        calibration_status=policy.calibration_status,
        observed_at=observation.as_of if observation else None,
        source_refs=observation.source_refs if observation else (),
        reason_codes=reasons,
    )


def _weight(value: Decimal | None, total: Decimal | None) -> Decimal | None:
    if value is None or total is None or total <= 0:
        return None
    return value / total


def _current_weights(frozen: FrozenAccountInputs) -> tuple[dict[ObjectRef, Decimal], Decimal | None, Decimal | None, bool]:
    """Frozen current weights, NAV, cash and complete-holding-presence flag."""

    valuation = frozen.valuation
    complete_valuation = (
        valuation is not None
        and valuation.coverage == "complete"
        and valuation.total_assets is not None
        and valuation.total_assets > 0
    )
    nav = valuation.total_assets if valuation is not None and complete_valuation else None
    if nav is None and frozen.snapshot.coverage == "complete":
        nav = frozen.snapshot.total_assets
    cash = (
        valuation.known_cash
        if valuation is not None and valuation.known_cash is not None
        else frozen.snapshot.known_cash
    )
    weights: dict[ObjectRef, Decimal] = {}
    if valuation is not None and complete_valuation:
        for holding in valuation.holdings:
            value = holding.market_value
            if value is None and holding.quantity is not None and holding.price is not None:
                value = holding.quantity * holding.price
            weight = _weight(value, nav)
            if weight is not None:
                weights[holding.listing_ref] = weight
    elif frozen.snapshot.coverage == "complete" and nav is not None:
        for snapshot_holding in frozen.snapshot.holdings:
            value = snapshot_holding.market_value
            if value is None and snapshot_holding.price is not None:
                value = snapshot_holding.quantity_subtotal * snapshot_holding.price
            weight = _weight(value, nav)
            if weight is not None:
                weights[snapshot_holding.listing_ref] = weight
    return weights, nav, cash, frozen.snapshot.coverage == "complete"


def build_mandate_budget_contexts(
    frozen: FrozenAccountInputs, listing_refs: Sequence[ObjectRef]
) -> dict[int, CandidateBudgetContext]:
    """Project the frozen mandate limits onto current portfolio exposure.

    No order quantity or trade simulation is invented: ``observed`` is the
    exposure already present in the frozen valuation/snapshot.  Headroom is
    therefore informational and the exact execution fit stays with
    :mod:`investment_agent.portfolio.suitability_calculations`.
    """

    constraints = frozen.mandate.portfolio_constraints
    if constraints is not None and constraints.account_id != frozen.account_id:
        constraints = None
    mandate_ref = ObjectRef(
        kind="mandate", id=frozen.mandate.id, revision=frozen.mandate.revision
    )
    weights, nav, cash, presence_complete = _current_weights(frozen)
    held_refs = {holding.listing_ref for holding in frozen.snapshot.holdings}
    sectors: dict[ObjectRef, str | None] = {
        source.listing_ref: source.sector.code for source in frozen.listing_sources
    }
    sector_complete = all(
        sectors.get(ref) is not None for ref in held_refs
    )
    sector_weights: dict[str, Decimal] = {}
    sector_unknown = False
    if nav is not None:
        for ref, weight in weights.items():
            code = sectors.get(ref)
            if code is None:
                sector_unknown = True
                continue
            sector_weights[code] = sector_weights.get(code, Decimal(0)) + weight

    result: dict[int, CandidateBudgetContext] = {}
    for ref in listing_refs:
        code = sectors.get(ref)
        reasons: list[str] = []
        configured = constraints is not None

        # Single-name exposure against the current frozen position.
        if constraints is None or constraints.max_single_weight is None:
            single = BudgetCheck(dimension="single_name", state="not_configured")
        else:
            if ref in weights:
                observed = weights[ref]
            elif ref in held_refs or not presence_complete:
                observed = None
                reasons.append("holding_weight_unavailable")
            else:
                observed = Decimal(0)
            if nav is None:
                observed = None
                reasons.append("nav_unavailable")
            if observed is None:
                single = BudgetCheck(
                    dimension="single_name",
                    state="unknown",
                    limit=constraints.max_single_weight,
                    reason_codes=tuple(dict.fromkeys(reasons)),
                )
            else:
                limit = constraints.max_single_weight
                headroom = max(Decimal(0), limit - observed)
                single = BudgetCheck(
                    dimension="single_name",
                    state="at_or_over_budget" if observed >= limit else "within_budget",
                    limit=limit,
                    observed=observed,
                    headroom=headroom,
                    reason_codes=()
                    if observed < limit
                    else ("single_name_limit_reached",),
                )

        # Sector exposure of the current portfolio (candidate sector budget).
        if constraints is None or constraints.max_sector_weight is None:
            sector = BudgetCheck(dimension="sector", state="not_configured")
        elif code is None:
            sector = BudgetCheck(
                dimension="sector",
                state="unknown",
                limit=constraints.max_sector_weight,
                reason_codes=("sector_unknown",),
            )
        elif nav is None or sector_unknown or not sector_complete:
            missing = (
                "nav_unavailable" if nav is None else "sector_coverage_incomplete"
            )
            sector = BudgetCheck(
                dimension="sector",
                state="unknown",
                limit=constraints.max_sector_weight,
                reason_codes=(missing,),
            )
        else:
            observed = sector_weights.get(code, Decimal(0))
            limit = constraints.max_sector_weight
            headroom = max(Decimal(0), limit - observed)
            sector = BudgetCheck(
                dimension="sector",
                state="at_or_over_budget" if observed >= limit else "within_budget",
                limit=limit,
                observed=observed,
                headroom=headroom,
                reason_codes=()
                if observed < limit
                else ("sector_limit_reached",),
            )

        # Cash is an account/scenario reserve, not a property of one candidate.
        if constraints is None or (
            constraints.minimum_cash is None and constraints.minimum_cash_weight is None
        ):
            cash_check = BudgetCheck(dimension="cash", state="not_configured")
        else:
            reserved_limit: Decimal | None
            reserved_observed: Decimal | None
            if constraints.minimum_cash_weight is not None:
                reserved_limit = constraints.minimum_cash_weight
                reserved_observed = _weight(cash, nav)
            else:
                reserved_limit = constraints.minimum_cash
                reserved_observed = cash
            cash_reasons: tuple[str, ...] = (
                "candidate_is_not_a_trade",
                "applies_to_account_scenario",
            )
            if reserved_observed is None:
                cash_reasons = (*cash_reasons, "nav_or_cash_unavailable")
            cash_check = BudgetCheck(
                dimension="cash",
                state="not_applicable",
                limit=reserved_limit,
                observed=reserved_observed,
                reason_codes=cash_reasons,
            )

        result[int(ref.id)] = CandidateBudgetContext(
            mandate_ref=mandate_ref,
            constraints_configured=configured,
            single_name=single,
            sector=sector,
            cash=cash_check,
            reason_codes=tuple(dict.fromkeys(reasons)),
        )
    return result


def combine_context(
    budget: CandidateBudgetContext | None,
    liquidity: LiquidityAssessment | None,
) -> CandidatePortfolioContext:
    """Combine budget and liquidity visibility into one display state."""

    fails = False
    unknown = False
    assessed = False
    reasons: list[str] = []
    if budget is not None:
        for check in (budget.single_name, budget.sector):
            if check.state == "at_or_over_budget":
                fails = True
                assessed = True
                reasons.extend(check.reason_codes)
            elif check.state == "unknown":
                unknown = True
                assessed = True
                reasons.extend(check.reason_codes)
            elif check.state == "within_budget":
                assessed = True
    if liquidity is not None:
        if liquidity.state == "insufficient":
            fails = True
            assessed = True
        elif liquidity.state == "missing":
            unknown = True
            assessed = True
        elif liquidity.state == "pass":
            assessed = True
        reasons.extend(liquidity.reason_codes)

    if fails:
        state: ContextState = "constrained"
    elif unknown:
        state = "unverified"
    elif not assessed:
        state = "not_assessed"
        reasons.append("no_portfolio_context_evidence")
    else:
        state = "eligible"
    return CandidatePortfolioContext(
        budget=budget,
        liquidity=liquidity,
        state=state,
        reason_codes=tuple(dict.fromkeys(reasons)),
    )


def context_order(state: ContextState) -> int:
    """Stable display partition rank (never a strength comparison)."""

    return CONTEXT_ORDER[state]


def _decimal_field(value: object) -> Decimal | None:
    """Parse a stored JSON numeric without accepting bool/NaN/Infinity."""

    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except (ArithmeticError, ValueError):
        return None
    if not parsed.is_finite():
        return None
    return parsed


async def load_liquidity_observations(
    session: object,
    listing_refs: Sequence[ObjectRef],
    *,
    as_of: datetime,
    window_days: int = LIQUIDITY_WINDOW_DAYS,
) -> dict[int, LiquidityObservation]:
    """Read read-only market/financial evidence for the liquidity gate.

    The 20-day window uses the last ``window_days`` trade dates known by
    ``as_of``.  A suspended row (``trade_status == 0``) contributes a real zero
    amount; a missing amount on a trading day makes the average explicitly
    unknown.  Free-float market cap uses ``float_shares`` × the latest close.
    ``total_shares`` is never consulted - a missing free-float count remains
    ``MISSING``.
    """

    if window_days != LIQUIDITY_WINDOW_DAYS:
        raise ValueError("liquidity window must be 20 trading days")
    market = MarketSnapshotRepository(session)  # type: ignore[arg-type]
    financial = FinancialSnapshotRepository(session)  # type: ignore[arg-type]
    end = as_of.astimezone(SHANGHAI_TZ).date()
    start = end - timedelta(days=LIQUIDITY_LOOKBACK_DAYS)
    observations: dict[int, LiquidityObservation] = {}
    for ref in listing_refs:
        listing_id = int(ref.id)
        bars = await market.series_as_of(
            listing_id,
            start,
            end,
            as_of,
            adjust="none",
            include_backfill=True,
        )
        window = bars[-LIQUIDITY_WINDOW_DAYS:]
        amounts: list[Decimal] = []
        source_refs: list[ObjectRef] = []
        for bar in window:
            if bar.trade_status == 0:
                amounts.append(Decimal(0))
            elif bar.amount is not None:
                amounts.append(bar.amount)
            source_refs.append(
                ObjectRef(kind="market_snapshot", id=bar.id, revision=bar.version)
            )
        observed_days = len(amounts)
        if observed_days == len(window) == LIQUIDITY_WINDOW_DAYS:
            average = _quantity(sum(amounts, Decimal(0)) / Decimal(LIQUIDITY_WINDOW_DAYS))
        else:
            average = _quantity(None, reason="amount_history_incomplete")

        free_float = _quantity(None, reason="free_float_shares_missing")
        if bars:
            latest = bars[-1]
            shares_row = await financial.latest_available(listing_id, end)
            if shares_row is not None:
                shares = _decimal_field((shares_row.fields or {}).get("float_shares"))
                if shares is not None and shares > 0:
                    free_float = _quantity(latest.close * shares)
                    source_refs.append(
                        ObjectRef(
                            kind="financial_snapshot",
                            id=shares_row.id,
                            revision=shares_row.version,
                        )
                    )
                elif shares is not None:
                    free_float = _quantity(None, reason="free_float_shares_invalid")

        observations[listing_id] = LiquidityObservation(
            listing_ref=ref,
            as_of=as_of,
            observed_amount_days=observed_days,
            average_amount_20d=average,
            free_float_market_cap=free_float,
            source_refs=tuple(source_refs),
        )
    return observations


def build_candidate_portfolio_contexts(
    *,
    frozen: FrozenAccountInputs | None,
    listing_refs: Sequence[ObjectRef],
    request: CandidatePortfolioContextRequest | None,
    observations: Mapping[int, LiquidityObservation] | None = None,
) -> dict[int, CandidatePortfolioContext] | None:
    """Assemble the optional per-listing context; ``None`` keeps legacy mode."""

    if request is None:
        return None
    budgets = (
        build_mandate_budget_contexts(frozen, listing_refs)
        if request.include_mandate_budget and frozen is not None
        else {}
    )
    by_listing = observations or {}
    contexts: dict[int, CandidatePortfolioContext] = {}
    for ref in listing_refs:
        listing_id = int(ref.id)
        liquidity = (
            assess_observation_liquidity(by_listing.get(listing_id), request.liquidity)
            if request.liquidity is not None
            else None
        )
        contexts[listing_id] = combine_context(budgets.get(listing_id), liquidity)
    return contexts


__all__ = [
    "BUDGET_BASIS",
    "BudgetCheck",
    "BudgetState",
    "CONTEXT_ORDER",
    "CandidateBudgetContext",
    "CandidatePortfolioContext",
    "CandidatePortfolioContextRequest",
    "ContextState",
    "LIQUIDITY_WINDOW_DAYS",
    "LiquidityAssessment",
    "LiquidityCheck",
    "LiquidityObservation",
    "LiquidityPolicy",
    "LiquidityState",
    "assess_observation_liquidity",
    "build_candidate_portfolio_contexts",
    "build_mandate_budget_contexts",
    "combine_context",
    "context_order",
    "load_liquidity_observations",
]
