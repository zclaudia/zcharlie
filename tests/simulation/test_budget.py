from decimal import Decimal

import pytest
from sqlalchemy import select

from investment_agent.simulation.budget import SimulationBudget, priced_usage
from investment_agent.simulation.contracts import AccountScope, BudgetConfig
from investment_agent.simulation.errors import SimulationError
from investment_agent.storage.models.simulation import SimulationCostRecord
from tests.simulation.test_runtime import runtime_arena as runtime_arena


async def allowed():
    return None


def budget(arena, **changes):
    participant = arena["participant"]
    return SimulationBudget(
        scope=AccountScope(
            experiment_id=participant.experiment_id,
            participant_id=participant.id,
            account_id=participant.account_id,
        ),
        round_id=arena["round"].id,
        config=BudgetConfig.model_validate(participant.config.budget.model_dump() | changes),
        model_ref="selected/model",
        guard=allowed,
        sessions=arena["sessions"],
        now=lambda: arena["clock"][0],
    )


def test_exact_pricing_and_missing_prices_never_fabricate_zero():
    config = BudgetConfig(input_price_per_million="0.3", output_price_per_million="1.7")
    assert priced_usage(config, 123, 456) == Decimal("0.0008121")
    assert priced_usage(BudgetConfig(), 123, 456) is None
    for count in (-1, True, 1.5):
        with pytest.raises(ValueError):
            priced_usage(config, count, 1)


async def test_reserve_is_committed_before_send_and_unknown_charge_retained(runtime_arena):
    arena = runtime_arena
    ledger = budget(arena)
    identity = await ledger.reserve(100)
    async with arena["sessions"]() as session:
        row = (await session.scalars(select(SimulationCostRecord))).one()
        assert row.status == "reserved" and row.payload["amount"] == "1"
    await ledger.sent(identity)
    await ledger.settle(identity, None, sent=True)
    assert (await ledger.costs())[0].status == "unknown"
    assert (await ledger.costs())[0].amount == 1
    with pytest.raises(SimulationError) as error:
        await budget(arena).reserve(100)
    assert error.value.code == "unknown_call"


async def test_calls_round_season_limits_and_unsent_release(runtime_arena):
    arena = runtime_arena
    ledger = budget(arena, max_calls_per_round=1)
    identity = await ledger.reserve(1)
    await ledger.settle(identity, None, sent=False)
    assert (await ledger.costs())[0].amount == 0
    with pytest.raises(SimulationError) as error:
        await ledger.reserve(1)
    assert error.value.code == "budget_exhausted"


async def test_actual_cost_overrun_is_saved_in_full_and_blocks_next_request(runtime_arena):
    arena = runtime_arena
    ledger = budget(arena, input_price_per_million="1000000", output_price_per_million="1000000")
    identity = await ledger.reserve(1)
    await ledger.sent(identity)
    await ledger.settle(identity, {"input_tokens": 3, "output_tokens": 2}, sent=True)
    assert (await ledger.costs())[0].amount == 5
    assert ledger.overrun
    with pytest.raises(SimulationError) as error:
        await ledger.reserve(1)
    assert error.value.code == "budget_exhausted"
    # Settlement replay cannot shrink an already recorded charge.
    await ledger.settle(identity, {"input_tokens": 0, "output_tokens": 0}, sent=True)
    assert (await ledger.costs())[0].amount == 5


async def test_season_debit_carries_across_rounds_and_prices_can_be_unknown(runtime_arena):
    arena = runtime_arena
    ledger = budget(
        arena,
        round_cost_limit="1",
        season_cost_limit="1",
        input_price_per_million=None,
        output_price_per_million=None,
    )
    identity = await ledger.reserve(10)
    await ledger.sent(identity)
    await ledger.settle(identity, {"input_tokens": 10, "output_tokens": 10}, sent=True)
    assert (await ledger.costs())[0].status == "settled"
    assert (await ledger.costs())[0].estimated
    later = budget(arena, round_cost_limit="1", season_cost_limit="1")
    later.round_id = "later-round"
    with pytest.raises(SimulationError) as error:
        await later.reserve(10)
    assert error.value.code == "budget_exhausted"


async def test_send_rechecks_guard_and_can_release_unsent_reservation(runtime_arena):
    arena = runtime_arena
    ledger = budget(arena)
    identity = await ledger.reserve(1)

    async def paused():
        raise SimulationError("invalid_transition", "paused")

    ledger.guard = paused
    with pytest.raises(SimulationError):
        await ledger.sent(identity)
    await ledger.settle(identity, None, sent=False)
    assert (await ledger.costs())[0].amount == 0


async def test_successful_unpriced_usage_can_fund_bounded_review_without_delivery_replay(
    runtime_arena,
):
    ledger = budget(
        runtime_arena,
        round_cost_limit="3",
        input_price_per_million=None,
        output_price_per_million=None,
    )
    for _ in range(3):
        identity = await ledger.reserve(100)
        await ledger.sent(identity)
        await ledger.settle(identity, {"input_tokens": 100, "output_tokens": 30}, sent=True)
    costs = await ledger.costs()
    assert len(costs) == 3 and all(
        cost.status == "settled" and cost.estimated and cost.amount == 1 for cost in costs
    )
    with pytest.raises(SimulationError) as error:
        await ledger.reserve(100)
    assert error.value.code == "budget_exhausted"


async def test_persisted_budget_boundary_preserves_all_decimal_digits(runtime_arena):
    cap = "1000000000000000000000.000000000000000001"
    original = budget(
        runtime_arena,
        round_cost_limit=cap,
        season_cost_limit=cap,
        conservative_call_reservation="0.000000000000000001",
        input_price_per_million="1",
        output_price_per_million="0.000000000001",
    )
    identity = await original.reserve(1)
    await original.sent(identity)
    await original.settle(identity, {"input_tokens": 10**27, "output_tokens": 1}, sent=True)
    later = budget(
        runtime_arena,
        round_cost_limit=cap,
        season_cost_limit=cap,
        conservative_call_reservation="0.000000000000000001",
    )
    with pytest.raises(SimulationError) as error:
        await later.reserve(1)
    assert error.value.code == "budget_exhausted"


async def test_known_request_price_bound_is_reserved_before_provider_send(runtime_arena):
    ledger = budget(runtime_arena, input_price_per_million="1000000",
        output_price_per_million="1000000", round_cost_limit="10")
    ledger.output_tokens_bound = 5
    with pytest.raises(SimulationError) as error:
        await ledger.reserve(10)
    assert error.value.code == "budget_exhausted"
    assert not await ledger.costs()


async def test_request_binding_prevents_cross_round_receipt_reuse(runtime_arena):
    ledger = budget(runtime_arena)
    identity = await ledger.reserve(1)
    other = budget(runtime_arena)
    other.round_id = "other-round"
    with pytest.raises(SimulationError) as error:
        await other.sent(identity)
    assert error.value.code == "scope_mismatch"
    await ledger.settle(identity, None, sent=False)
    assert (await ledger.costs())[0].status == "failed"
