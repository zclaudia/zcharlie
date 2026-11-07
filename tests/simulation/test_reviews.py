from datetime import UTC, date, datetime

from investment_agent.simulation.contracts import Decision, NavSnapshot
from investment_agent.simulation.reviews import review_decision

SCOPE = dict(experiment_id="e", participant_id="p", account_id="a")


def test_review_waits_for_actual_end_valuation_and_weekend_matures_on_monday():
    decision = Decision(
        **SCOPE,
        id="d",
        round_id="r",
        account_revision=1,
        status="locked",
        action="hold",
        rationale="hypothesis unchanged",
        submitted_at=datetime(2026, 11, 6, 9, tzinfo=UTC),
        expected_holding_days=1,
        idempotency_key="k",
    )
    now = datetime(2026, 11, 9, 9, tzinfo=UTC)
    pending = review_decision(decision, (), observation_start=date(2026, 11, 6), now=now)
    assert pending.status == "observing" and not pending.memory_eligible
    nav = NavSnapshot(
        **SCOPE,
        id="n",
        account_revision=1,
        valuation_date=date(2026, 11, 9),
        cash="100",
        position_value="0",
        equity="100",
        quality="complete",
        valuation_version="v1",
        metrics_version="v1",
        input_hash="a" * 64,
        created_at=now,
    )
    mature = review_decision(decision, (nav,), observation_start=date(2026, 11, 6), now=now)
    assert mature.status == "matured" and mature.observation_end == date(2026, 11, 9)
    assert mature.nav_refs == ("n",) and not mature.memory_eligible
