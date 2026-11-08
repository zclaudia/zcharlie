"""P1 portfolio layer: optional mandate budget / liquidity context for ranking.

Covers the review's acceptance points:

* repeated hits still de-duplicate (existing listing merge + ranking emit);
* single-name / sector budget visibility against frozen exposure;
* research-only compatibility when no portfolio context is supplied;
* liquidity insufficient / missing / passing, with explicit ``MISSING`` for
  free-float market cap when the free-float share count is unavailable;
* cash constraints are explicitly not applicable to a single candidate.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from investment_agent.candidates.portfolio_context import (
    LIQUIDITY_LOOKBACK_DAYS,
    CandidatePortfolioContextRequest,
    LiquidityPolicy,
    assess_observation_liquidity,
    build_candidate_portfolio_contexts,
    build_mandate_budget_contexts,
    load_liquidity_observations,
)
from investment_agent.investor.common import ObjectRef, Quantity
from investment_agent.investor.contracts import PortfolioConstraints
from tests.support.account_fit_db import AS_OF, PRICE_DATE, make_inputs


def _ref(listing_id: int) -> ObjectRef:
    return ObjectRef(kind="listing", id=listing_id, revision=1)


def _amount(value: Decimal | None, reason: str | None = None) -> Quantity:
    return Quantity(
        value=value, unit="CNY", currency="CNY", unknown_reason=reason
    )


def _policy(**overrides: object) -> LiquidityPolicy:
    base: dict[str, object] = {
        "assumption_source": "synthetic_test",
        "min_average_amount_20d": Decimal("1000000"),
    }
    base.update(overrides)
    return LiquidityPolicy.model_validate(base)


def _policy_observation(
    *,
    listing_id: int = 600001,
    days: int = 20,
    average: Decimal | None = Decimal("5000000"),
    average_reason: str | None = None,
    free_float: Decimal | None = None,
    free_float_reason: str | None = "free_float_shares_missing",
) -> object:
    from investment_agent.candidates.portfolio_context import LiquidityObservation

    return LiquidityObservation(
        listing_ref=_ref(listing_id),
        as_of=AS_OF,
        observed_amount_days=days,
        average_amount_20d=_amount(average, average_reason),
        free_float_market_cap=_amount(free_float, free_float_reason),
    )


# --- policy shape ----------------------------------------------------------


def test_liquidity_policy_has_no_calibrated_default_and_requires_a_threshold() -> None:
    with pytest.raises(ValidationError, match="explicit threshold"):
        LiquidityPolicy(assumption_source="synthetic_test")
    with pytest.raises(ValidationError, match="uncalibrated"):
        LiquidityPolicy.model_validate(
            {
                "assumption_source": "synthetic_test",
                "min_average_amount_20d": "1000",
                "calibration_status": "backtested",
            }
        )
    policy = _policy()
    assert policy.calibration_status == "uncalibrated"
    assert policy.window_days == 20


def test_context_request_must_enable_at_least_one_dimension() -> None:
    with pytest.raises(ValidationError, match="budget or liquidity"):
        CandidatePortfolioContextRequest(include_mandate_budget=False)
    assert CandidatePortfolioContextRequest().include_mandate_budget is True


# --- liquidity states ------------------------------------------------------


def test_liquidity_insufficient_is_visible_and_never_a_pass() -> None:
    result = assess_observation_liquidity(
        _policy_observation(average=Decimal("10")), _policy()
    )
    assert result.state == "insufficient"
    assert "average_amount_below_threshold" in result.reason_codes
    check = next(item for item in result.checks if item.metric == "average_amount_20d")
    assert check.state == "insufficient"
    assert check.threshold is not None and check.threshold.value == Decimal("1000000")


def test_liquidity_missing_history_is_explicit_not_zero() -> None:
    result = assess_observation_liquidity(
        _policy_observation(
            days=7, average=None, average_reason="amount_history_incomplete"
        ),
        _policy(),
    )
    assert result.state == "missing"
    assert result.average_amount_20d.value is None
    assert result.average_amount_20d.unknown_reason == "amount_history_incomplete"
    assert "amount_history_incomplete" in result.reason_codes


def test_liquidity_pass_keeps_free_float_missing_visible() -> None:
    # A configured amount threshold passes while free-float market cap is
    # unconfigured; the missing free-float evidence must still be visible.
    result = assess_observation_liquidity(
        _policy_observation(free_float=None), _policy()
    )
    assert result.state == "pass"
    assert result.free_float_market_cap.value is None
    assert result.free_float_market_cap.unknown_reason == "free_float_shares_missing"
    free_float_check = next(
        item for item in result.checks if item.metric == "free_float_market_cap"
    )
    assert free_float_check.state == "not_configured"


def test_free_float_threshold_without_data_is_missing_not_passing() -> None:
    result = assess_observation_liquidity(
        _policy_observation(free_float=None),
        _policy(min_free_float_market_cap=Decimal("100000000")),
    )
    assert result.state == "missing"
    free_float_check = next(
        item for item in result.checks if item.metric == "free_float_market_cap"
    )
    assert free_float_check.state == "missing"
    assert "free_float_shares_missing" in free_float_check.reason_codes


def test_no_policy_is_not_assessed_rather_than_passing() -> None:
    result = assess_observation_liquidity(_policy_observation(), None)
    assert result.state == "not_assessed"
    assert result.checks == ()
    assert "liquidity_not_assessed" in result.reason_codes


def test_observation_cannot_claim_a_complete_average_without_full_window() -> None:
    with pytest.raises(ValidationError, match="complete observation window"):
        _policy_observation(days=19, average=Decimal("100"))


# --- mandate budget --------------------------------------------------------


def test_single_name_and_sector_budget_use_frozen_exposure() -> None:
    inputs = make_inputs(
        holdings=((600000, "100", "1500"),),
        prices={600000: "1500", 600001: "1500"},
        rules=(),
        constraints=PortfolioConstraints(
            account_id=make_inputs().account_id,
            max_single_weight=Decimal("0.5"),
            max_sector_weight=Decimal("0.4"),
            minimum_cash=Decimal("10000"),
        ),
    )
    contexts = build_mandate_budget_contexts(inputs, (_ref(600000), _ref(600001)))

    # 150000 held of 350000 NAV ~= 0.4286: inside the 0.5 single-name limit.
    held = contexts[600000]
    assert held.single_name.state == "within_budget"
    assert held.single_name.observed is not None
    assert held.single_name.observed < Decimal("0.5")
    assert held.single_name.headroom is not None
    # Both synthetic listings share sector C39 and together exceed 0.4.
    assert held.sector.state == "at_or_over_budget"
    assert "sector_limit_reached" in held.sector.reason_codes

    # A candidate not held has zero current exposure: within budget.
    candidate = contexts[600001]
    assert candidate.single_name.state == "within_budget"
    assert candidate.single_name.observed == Decimal(0)


def test_single_name_limit_reached_is_reported_on_current_exposure() -> None:
    inputs = make_inputs(
        holdings=((600000, "100", "1500"),),
        prices={600000: "1500"},
        rules=(),
        constraints=PortfolioConstraints(
            account_id=make_inputs().account_id,
            max_single_weight=Decimal("0.2"),
        ),
    )
    context = build_mandate_budget_contexts(inputs, (_ref(600000),))[600000]
    assert context.single_name.state == "at_or_over_budget"
    assert "single_name_limit_reached" in context.single_name.reason_codes


def test_cash_constraint_is_not_applicable_to_one_candidate() -> None:
    inputs = make_inputs(
        constraints=PortfolioConstraints(
            account_id=make_inputs().account_id,
            minimum_cash=Decimal("10000"),
            minimum_cash_weight=Decimal("0.1"),
            max_single_weight=Decimal("0.5"),
        )
    )
    context = build_mandate_budget_contexts(inputs, (_ref(600001),))[600001]
    assert context.cash.state == "not_applicable"
    assert "candidate_is_not_a_trade" in context.cash.reason_codes
    assert "applies_to_account_scenario" in context.cash.reason_codes
    # It is still visible with its limit and observation, never silently dropped.
    assert context.cash.limit is not None
    assert context.cash.observed is not None


def test_missing_mandate_constraints_reports_not_configured_without_invented_limits() -> None:
    inputs = make_inputs().model_copy(
        update={"mandate": make_inputs().mandate.model_copy(update={"portfolio_constraints": None})}
    )
    context = build_mandate_budget_contexts(inputs, (_ref(600001),))[600001]
    assert context.constraints_configured is False
    assert context.single_name.state == "not_configured"
    assert context.sector.state == "not_configured"
    assert context.cash.state == "not_configured"
    assert context.single_name.limit is None and context.single_name.observed is None


def test_present_but_empty_mandate_constraints_are_not_configured_per_field() -> None:
    inputs = make_inputs(constraints=PortfolioConstraints(account_id=make_inputs().account_id))
    context = build_mandate_budget_contexts(inputs, (_ref(600001),))[600001]
    # The revision exists, but each unset limit stays an explicit
    # not-configured check rather than becoming an invented threshold.
    assert context.constraints_configured is True
    assert context.single_name.state == "not_configured"
    assert context.sector.state == "not_configured"
    assert context.cash.state == "not_configured"


# --- optional research-only compatibility ---------------------------------


def test_no_portfolio_request_keeps_research_only_mode() -> None:
    inputs = make_inputs()
    assert (
        build_candidate_portfolio_contexts(
            frozen=inputs, listing_refs=(_ref(600001),), request=None
        )
        is None
    )


def test_budget_only_context_still_reports_liquidity_as_absent() -> None:
    inputs = make_inputs()
    contexts = build_candidate_portfolio_contexts(
        frozen=inputs,
        listing_refs=(_ref(600001),),
        request=CandidatePortfolioContextRequest(include_mandate_budget=True),
    )
    assert contexts is not None
    assert contexts[600001].liquidity is None
    assert contexts[600001].budget is not None


# --- evidence loader -------------------------------------------------------


class _FakeSession:
    pass


async def _seed_bars(
    session,
    listing_id: int,
    *,
    days: int,
    amount: Decimal | None = Decimal("1000"),
    start_offset: int = 0,
) -> None:
    from investment_agent.storage.models.pit_snapshot import MarketSnapshot

    for offset in range(start_offset, start_offset + days):
        session.add(
            MarketSnapshot(
                listing_id=listing_id,
                trade_date=PRICE_DATE - timedelta(days=offset),
                open=Decimal(10),
                high=Decimal(10),
                low=Decimal(10),
                close=Decimal(10),
                volume=Decimal(1),
                amount=amount,
                adjust="none",
                provider="baostock",
                captured_at=AS_OF - timedelta(days=1),
                version=1,
            )
        )
    await session.flush()


@pytest.mark.asyncio
async def test_loader_averages_twenty_days_and_marks_short_history_missing(
    db_session,
) -> None:
    from investment_agent.storage.models.pit_snapshot import MarketSnapshot

    listing = await _seed_listing(db_session, "600001.SH")
    await _seed_bars(db_session, listing.id, days=20, amount=Decimal("2000"))
    short = await _seed_listing(db_session, "600002.SH")
    await _seed_bars(db_session, short.id, days=5, amount=Decimal("2000"))

    observations = await load_liquidity_observations(
        db_session, (_ref(listing.id), _ref(short.id)), as_of=AS_OF
    )

    full = observations[listing.id]
    assert full.observed_amount_days == 20
    assert full.average_amount_20d.value == Decimal("2000")
    # No free-float shares were captured: total shares are never substituted.
    assert full.free_float_market_cap.value is None
    assert full.free_float_market_cap.unknown_reason == "free_float_shares_missing"
    assert any(
        isinstance(source.revision, int) for source in full.source_refs
    )

    partial = observations[short.id]
    assert partial.observed_amount_days == 5
    assert partial.average_amount_20d.value is None
    assert partial.average_amount_20d.unknown_reason == "amount_history_incomplete"
    assert await db_session.scalar(
        __import__("sqlalchemy").select(__import__("sqlalchemy").func.count()).select_from(MarketSnapshot)
    ) == 25


@pytest.mark.asyncio
async def test_loader_uses_free_float_shares_when_present(db_session) -> None:
    from investment_agent.storage.models.pit_snapshot import FinancialSnapshot

    listing = await _seed_listing(db_session, "600003.SH")
    await _seed_bars(db_session, listing.id, days=20, amount=Decimal("2000"))
    db_session.add(
        FinancialSnapshot(
            listing_id=listing.id,
            report_period=PRICE_DATE.replace(year=PRICE_DATE.year - 1),
            period_kind="annual",
            available_from=PRICE_DATE - timedelta(days=100),
            availability="actual",
            fields={"float_shares": 1_000_000, "total_shares": 9_000_000},
            provider="baostock",
            captured_at=AS_OF - timedelta(days=1),
            version=1,
        )
    )
    await db_session.flush()

    observations = await load_liquidity_observations(
        db_session, (_ref(listing.id),), as_of=AS_OF
    )
    value = observations[listing.id].free_float_market_cap.value
    # 10 CNY close x 1,000,000 free-float shares; total shares are ignored.
    assert value == Decimal("10000000")


@pytest.mark.asyncio
async def test_loader_marks_suspended_day_as_zero_amount(db_session) -> None:
    from investment_agent.storage.models.pit_snapshot import MarketSnapshot

    listing = await _seed_listing(db_session, "600004.SH")
    await _seed_bars(
        db_session, listing.id, days=19, amount=Decimal("1000"), start_offset=1
    )
    db_session.add(
        MarketSnapshot(
            listing_id=listing.id,
            trade_date=PRICE_DATE,
            open=Decimal(10),
            high=Decimal(10),
            low=Decimal(10),
            close=Decimal(10),
            volume=Decimal(0),
            amount=None,
            trade_status=0,
            adjust="none",
            provider="baostock",
            captured_at=AS_OF - timedelta(days=1),
            version=1,
        )
    )
    await db_session.flush()

    observations = await load_liquidity_observations(
        db_session, (_ref(listing.id),), as_of=AS_OF
    )
    assert observations[listing.id].observed_amount_days == 20
    # 19 x 1000 / 20 = 950
    assert observations[listing.id].average_amount_20d.value == Decimal("950")


async def _seed_listing(session, ticker: str):
    from investment_agent.storage.repositories.listing import ListingRepository

    return await ListingRepository(session).ensure_a_share_listing(ticker, company_name=ticker)


def test_lookback_covers_twenty_trading_days() -> None:
    assert LIQUIDITY_LOOKBACK_DAYS >= 20


def test_assessment_carries_observation_time_and_source_refs() -> None:
    from investment_agent.candidates.portfolio_context import LiquidityObservation

    observation = LiquidityObservation(
        listing_ref=_ref(600001),
        as_of=AS_OF,
        observed_amount_days=20,
        average_amount_20d=_amount(Decimal("5000000")),
        free_float_market_cap=_amount(None, "free_float_shares_missing"),
        source_refs=(
            ObjectRef(kind="market_snapshot", id=1, revision=1),
        ),
    )
    result = assess_observation_liquidity(observation, _policy())
    assert result.state == "pass"
    assert result.observed_at == AS_OF
    assert result.source_refs == (ObjectRef(kind="market_snapshot", id=1, revision=1),)
