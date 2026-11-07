from datetime import UTC, date, datetime
from decimal import Decimal

from investment_agent.simulation.baselines import baseline_decision
from investment_agent.simulation.contracts import (
    AccountState,
    DataManifest,
    Participant,
    SimulationAccount,
    SimulationRound,
)
from investment_agent.simulation.risk import ReservationTerms, reserve_decision

NOW = datetime(2026, 11, 9, 9, tzinfo=UTC)


def test_equal_weight_compiles_known_fixed_lots_and_never_rebalances(sim_config):
    participant = Participant(
        id="p",
        experiment_id="e",
        account_id="a",
        config=sim_config.participants[1],
        config_hash="a" * 64,
    )
    account = SimulationAccount(
        experiment_id="e",
        participant_id="p",
        account_id="a",
        initial_cash=sim_config.initial_cash,
        state=AccountState(cash=sim_config.initial_cash),
    )
    round_ = SimulationRound(
        id="r",
        experiment_id="e",
        observation_date=date(2026, 11, 9),
        cutoff=NOW,
        decision_deadline=datetime(2026, 11, 10, 0, tzinfo=UTC),
        execution_date=date(2026, 11, 10),
        execution_at=datetime(2026, 11, 10, 1, 30, tzinfo=UTC),
        status="collecting_decisions",
        manifest=DataManifest(
            version="v1",
            content_hash="a" * 64,
            cutoff=NOW,
            universe=(1, 2),
            artifact_path="objects/fixture.json",
            complete=True,
        ),
    )
    terms = {
        listing_id: ReservationTerms(
            listing_id=listing_id,
            trade_date=round_.execution_date,
            rule_version=sim_config.rules.version,
            source_ref="proof",
            verified=True,
            tick_size="0.01",
            lot_size=100,
            known_at=NOW,
            historical_turnover="1000000000",
            historical_available_at=NOW,
            corporate_actions_complete=True,
        )
        for listing_id in (1, 2)
    }
    decision = baseline_decision(
        participant,
        account,
        round_,
        sim_config.rules,
        terms,
        {1: Decimal("10"), 2: Decimal("10")},
        submitted_at=NOW,
    )
    assert decision.action == "trade" and len(decision.orders) == 2
    reserved = reserve_decision(
        account,
        decision,
        round_,
        sim_config.rules,
        terms,
        position_prices={1: Decimal("10"), 2: Decimal("10")},
    )
    assert reserved.account.state.reserved_cash <= account.state.cash
    held = baseline_decision(
        participant,
        account,
        round_,
        sim_config.rules,
        terms,
        {1: Decimal("10"), 2: Decimal("10")},
        submitted_at=NOW,
        already_attempted=True,
    )
    assert held.action == "hold" and not held.orders
