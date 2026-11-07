from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, localcontext

import pytest
from pydantic import ValidationError

from investment_agent.simulation.contracts import (
    AccountState,
    BudgetConfig,
    ConfigEntry,
    DataManifest,
    Decision,
    EvidenceRef,
    ExperimentConfig,
    LedgerEvent,
    NavSnapshot,
    OrderIntent,
    ParticipantConfig,
    SimulationFill,
    SimulationOrder,
    SimulationReview,
    SimulationRound,
)
from investment_agent.simulation.errors import SimulationError

SCOPE = {"experiment_id": "experiment", "participant_id": "participant", "account_id": "account"}
NOW = datetime(2026, 11, 9, 10, tzinfo=UTC)


def test_config_exact_wire_roundtrip_and_deep_freeze(sim_config):
    wire = sim_config.model_dump(mode="json")
    assert wire["initial_cash"] == "1000000"
    assert wire["rules"]["fee_schedule"]["commission_rate"] == "0.0003"
    assert ExperimentConfig.model_validate_json(sim_config.model_dump_json()) == sim_config
    with pytest.raises(ValidationError, match="frozen"):
        sim_config.initial_cash = Decimal("2")
    with pytest.raises(ValidationError, match="frozen"):
        sim_config.participants[0].name = "changed"
    assert isinstance(sim_config.universe, tuple)
    assert isinstance(sim_config.participants, tuple)
    assert isinstance(sim_config.participants[2].model_snapshot, tuple)


@pytest.mark.parametrize(
    "value", [float("nan"), float("inf"), 0.1, True, "NaN", "Infinity", "-Infinity", "sNaN"]
)
def test_money_rejects_nonfinite_float_and_bool(value):
    with pytest.raises(ValidationError):
        AccountState(cash=value)


@pytest.mark.parametrize(
    "patch",
    [
        {"universe": []},
        {"universe": [1, 1]},
        {"universe": [True]},
        {"initial_cash": "0"},
        {"initial_cash": "-1"},
        {"end_date": "2026-01-01"},
        {"run_mode": "historical"},
        {"unexpected": "field"},
    ],
)
def test_invalid_experiment_config(sim_config, patch):
    body = sim_config.model_dump(mode="json") | patch
    with pytest.raises(ValidationError):
        ExperimentConfig.model_validate(body)


def test_requires_fair_deterministic_baselines(sim_config):
    body = sim_config.model_dump(mode="json")
    body["participants"] = body["participants"][1:]
    with pytest.raises(ValidationError, match="cash"):
        ExperimentConfig.model_validate(body)
    with pytest.raises(ValidationError, match="agent configuration"):
        ParticipantConfig(name="cash", kind="cash", baseline_version="v1", model_ref="model")
    with pytest.raises(ValidationError, match="frozen model"):
        ParticipantConfig(name="agent", kind="agent")


def test_comparison_modes_freeze_declared_controls(sim_config):
    body = sim_config.model_dump(mode="json")
    second_agent = body["participants"][-1] | {"name": "other", "model_ref": "other"}
    body["participants"].append(second_agent)
    body["mode"] = "model_comparison"
    # Different model tariffs are compatible with the same monetary envelope.
    second_agent["budget"] = second_agent["budget"] | {
        "input_price_per_million": "2",
        "output_price_per_million": "8",
        "conservative_call_reservation": "0.5",
    }
    ExperimentConfig.model_validate(body)
    second_agent["method_ref"] = "changed"
    with pytest.raises(ValidationError, match="freeze method"):
        ExperimentConfig.model_validate(body)
    body["mode"] = "method_comparison"
    with pytest.raises(ValidationError, match="freeze model"):
        ExperimentConfig.model_validate(body)


@pytest.mark.parametrize(
    "patch",
    [
        {"season_cost_limit": "0.1"},
        {"conservative_call_reservation": "4"},
        {"input_price_per_million": "1"},
        {"format_repair_attempts": 6},
        {"max_calls_per_round": True},
        {"currency": "CNY"},
    ],
)
def test_budget_invalid_config(patch):
    with pytest.raises(ValidationError):
        BudgetConfig(**patch)


@pytest.mark.parametrize(
    "key",
    [
        "api_key",
        "secret",
        "authorization",
        "access_token",
        "password",
        "apiKey",
        "API.KEY",
        "refreshToken",
        "private-key",
    ],
)
def test_snapshot_rejects_credentials(key):
    with pytest.raises(ValidationError, match="credentials"):
        ConfigEntry(key=key, value="do-not-store")


def manifest(**changes):
    return DataManifest(
        version="v1",
        content_hash="a" * 64,
        cutoff=NOW,
        universe=(1, 2),
        artifact_path="simulation-runs/e/r/manifest.json",
        **changes,
    )


def test_manifest_point_in_time_and_quality():
    evidence = EvidenceRef(
        id="evidence",
        content_hash="b" * 64,
        published_at=NOW - timedelta(days=1),
        acquired_at=NOW,
        available_at=NOW,
        source="archive",
    )
    manifest(complete=True, evidence=(evidence,))
    with pytest.raises(ValidationError, match="later than common cutoff"):
        manifest(
            complete=True,
            evidence=(evidence.model_copy(update={"available_at": NOW + timedelta(seconds=1)}),),
        )
    with pytest.raises(ValidationError, match="missing reasons"):
        manifest(complete=False)
    with pytest.raises(ValidationError, match="availability cannot precede"):
        EvidenceRef(
            id="e",
            content_hash="a" * 64,
            published_at=NOW,
            acquired_at=NOW,
            available_at=NOW - timedelta(seconds=1),
            source="archive",
        )
    with pytest.raises(ValidationError, match="timezone"):
        EvidenceRef.model_validate(
            evidence.model_dump() | {"available_at": NOW.replace(tzinfo=None)}
        )


def round_body():
    return dict(
        id="round",
        experiment_id="experiment",
        observation_date=date(2026, 11, 9),
        cutoff=NOW,
        decision_deadline=NOW + timedelta(hours=10),
        execution_date=date(2026, 11, 10),
        execution_at=NOW + timedelta(hours=15, minutes=30),
        manifest=manifest(complete=True),
        status="collecting_decisions",
    )


def test_round_validates_exchange_clock_and_common_data():
    item = SimulationRound(**round_body())
    assert item.cutoff.tzinfo == UTC
    for patch in (
        {"decision_deadline": NOW},
        {"execution_at": NOW},
        {"execution_date": date(2026, 11, 9)},
        {"cutoff": NOW.replace(tzinfo=None)},
        {"execution_at": NOW + timedelta(hours=40)},
    ):
        with pytest.raises(ValidationError):
            SimulationRound(**(round_body() | patch))
    with pytest.raises(ValidationError, match="blocks all decisions"):
        SimulationRound(
            **(
                round_body()
                | {"manifest": manifest(complete=False, missing_reasons=("missing limits",))}
            )
        )


def test_positive_fixed_quantity_and_reservation_conflicts():
    for value in ("0", "-1", "NaN", 0.1, True):
        with pytest.raises(ValidationError):
            OrderIntent(listing_id=1, side="buy", quantity=value, protection_price="10")
    body = SCOPE | dict(
        id="o",
        round_id="r",
        decision_id="d",
        sequence=1,
        account_revision=0,
        listing_id=1,
        side="buy",
        quantity="100",
        protection_price="10",
        execution_date=date(2026, 11, 10),
        valid_from=NOW + timedelta(days=1),
        valid_until=NOW + timedelta(days=1),
        reserved_cash="1005",
        rule_version="v1",
        idempotency_key="o-key",
    )
    SimulationOrder(**body)
    with pytest.raises(ValidationError, match="protected notional"):
        SimulationOrder(**(body | {"reserved_cash": "999"}))
    with pytest.raises(ValidationError, match="exact shares"):
        SimulationOrder(
            **(body | {"side": "sell", "reserved_cash": "0", "reserved_quantity": "99"})
        )
    with pytest.raises(ValidationError, match="exceeds cash"):
        AccountState(cash="1", reserved_cash="2")


def decision(**changes):
    return Decision(
        **(
            SCOPE
            | dict(
                id="d",
                round_id="r",
                account_revision=0,
                status="submitted",
                action="hold",
                rationale="hold deliberately",
                submitted_at=NOW,
                idempotency_key="decision-key",
            )
            | changes
        )
    )


def test_failures_distinct_from_hold_and_no_self_crossing():
    decision()
    with pytest.raises(ValidationError, match="failure status"):
        decision(action="no_submission")
    with pytest.raises(ValidationError, match="failure code"):
        decision(status="timeout")
    decision(status="timeout", action="no_submission", failure_code="deadline_passed")
    buy = OrderIntent(listing_id=1, side="buy", quantity="100", protection_price="10")
    sell = buy.model_copy(update={"side": "sell"})
    with pytest.raises(ValidationError, match="one final direction"):
        decision(action="trade", orders=(buy, sell))
    with pytest.raises(ValidationError, match="trade requires orders"):
        decision(action="trade")


def test_fill_and_event_dual_clocks():
    fill = SCOPE | dict(
        id="f",
        round_id="r",
        order_id="o",
        execution_key="open-v1",
        listing_id=1,
        side="buy",
        quantity="100",
        price="10",
        fee="5",
        economic_at=NOW,
        confirmed_at=NOW - timedelta(seconds=1),
        market_data_ref="snapshot",
        rule_version="v1",
    )
    with pytest.raises(ValidationError, match="confirmation"):
        SimulationFill(**fill)
    event = SCOPE | dict(
        id="event",
        sequence=1,
        account_revision=1,
        idempotency_key="event-key",
        kind="initial_cash",
        economic_at=NOW,
        confirmed_at=NOW,
        cash_delta="100",
        source_ref="creation",
        state_after=AccountState(cash="100"),
    )
    LedgerEvent(**event)
    for patch in (
        {"cash_delta": "0"},
        {"sequence": 0},
        {"account_revision": 0},
        {"kind": "correction"},
        {"quantity_delta": "100"},
    ):
        with pytest.raises(ValidationError):
            LedgerEvent(**(event | patch))


def test_nav_reconciles_and_fails_closed():
    body = SCOPE | dict(
        id="nav",
        account_revision=1,
        valuation_date=date(2026, 11, 9),
        cash="100",
        receivables="5",
        payables="2",
        position_value="10",
        equity="113",
        quality="complete",
        valuation_version="v1",
        metrics_version="v1",
        input_hash="a" * 64,
        created_at=NOW,
    )
    NavSnapshot(**body)
    with pytest.raises(ValidationError, match="reconcile"):
        NavSnapshot(**(body | {"equity": "115"}))
    incomplete = body | {
        "quality": "incomplete",
        "position_value": None,
        "equity": None,
        "quality_reasons": ("unknown corporate action",),
    }
    NavSnapshot(**incomplete)
    with pytest.raises(ValidationError, match="formal equity"):
        NavSnapshot(**(incomplete | {"cumulative_return": "0"}))


def test_review_maturity_and_explicit_failure():
    body = SCOPE | dict(
        id="review",
        decision_id="d",
        observation_start=date(2026, 11, 9),
        observation_end=date(2026, 11, 10),
        available_at=NOW + timedelta(days=1),
        status="observing",
        findings="still observing",
    )
    with pytest.raises(ValidationError, match="matured"):
        SimulationReview(**(body | {"memory_eligible": True}))
    with pytest.raises(ValidationError, match="observation end"):
        SimulationReview(**(body | {"available_at": NOW}))
    error = SimulationError("scope_mismatch", "another account")
    assert error.failure.model_dump(mode="json") == {
        "code": "scope_mismatch",
        "message": "another account",
        "retryable": False,
        "details": [],
    }


def test_reservation_and_nav_validation_are_exact_independent_of_decimal_context():
    precise = "1000000000000000000000.000000000000000001"
    rounded = "1000000000000000000000"
    order = SCOPE | dict(
        id="o",
        round_id="r",
        decision_id="d",
        sequence=1,
        account_revision=0,
        listing_id=1,
        side="buy",
        quantity=precise,
        protection_price="1",
        execution_date=NOW.date(),
        valid_from=NOW,
        valid_until=NOW,
        reserved_cash=rounded,
        rule_version="v1",
        idempotency_key="o",
    )
    nav = SCOPE | dict(
        id="n",
        account_revision=0,
        valuation_date=NOW.date(),
        cash=precise,
        position_value="0",
        equity=precise,
        quality="complete",
        valuation_version="v1",
        metrics_version="v1",
        input_hash="a" * 64,
        created_at=NOW,
    )
    with localcontext() as context:
        context.prec = 6
        with pytest.raises(ValidationError, match="protected notional"):
            SimulationOrder(**order)
        SimulationOrder(**(order | {"reserved_cash": precise}))
        NavSnapshot(**nav)
        with pytest.raises(ValidationError, match="reconcile"):
            NavSnapshot(**(nav | {"equity": rounded}))


def test_position_reservations_do_not_round_away_excess():
    precise = "1000000000000000000000.000000000000000001"
    rounded = "1000000000000000000000"
    lot = dict(
        id="lot",
        listing_id=1,
        quantity=rounded,
        acquired_on=NOW.date(),
        available_on=NOW.date(),
        unit_cost="1",
    )
    reserved = OrderIntent(listing_id=1, side="sell", quantity=precise, protection_price="1")
    with localcontext() as context:
        context.prec = 6
        with pytest.raises(ValidationError, match="exceeds holdings"):
            AccountState(cash="0", lots=(lot,), reserved_positions=(reserved,))


def test_daily_order_requires_execution_date_to_match_validity():
    order = SCOPE | dict(
        id="o",
        round_id="r",
        decision_id="d",
        sequence=1,
        account_revision=0,
        listing_id=1,
        side="buy",
        quantity="1",
        protection_price="1",
        execution_date=NOW.date() + timedelta(days=1),
        valid_from=NOW,
        valid_until=NOW,
        reserved_cash="1",
        rule_version="v1",
        idempotency_key="o",
    )
    with pytest.raises(ValidationError, match="execution date"):
        SimulationOrder(**order)


def test_review_observation_dates_use_exchange_timezone():
    # 00:30 Shanghai on the observation-end day is still the previous UTC day.
    local_day = datetime(2026, 11, 9, 16, 30, tzinfo=UTC)
    review = SimulationReview(
        **(
            SCOPE
            | dict(
                id="review",
                decision_id="d",
                observation_start=date(2026, 11, 9),
                observation_end=date(2026, 11, 10),
                available_at=local_day,
                status="observing",
                findings="observing",
            )
        )
    )
    assert review.available_at == local_day
