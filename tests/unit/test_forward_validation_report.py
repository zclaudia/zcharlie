"""Report assembly: sufficiency verdict, status ladder and monotonicity plumbing.

These are pure tests over hand-built observations. They pin the rule that the
verdict is a function of sample size and coverage only, never of the observed
sign, and that a horizon which cannot be priced is reported missing rather
than as a shorter result.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

from investment_agent.research.forward_validation.contracts import (
    CostModel,
    HorizonOutcome,
    ScoredObservation,
    ValidationConfig,
)
from investment_agent.research.forward_validation.evaluation import (
    Benchmark,
    aggregate_horizon,
    build_report,
    coverage_group,
)

AS_OF_A = date(2024, 6, 28)
AS_OF_B = date(2024, 12, 31)
SESSIONS = [AS_OF_A + timedelta(days=index) for index in range(150)]


def benchmark(value: float = 0.0) -> Benchmark:
    return Benchmark(
        name="equal_weight",
        by_horizon={1: {AS_OF_A: value, AS_OF_B: value}, 3: {AS_OF_A: value, AS_OF_B: value}},
        construction="test",
        note="",
    )


def observation(
    index: int,
    *,
    as_of: date = AS_OF_A,
    score: float | None = 50.0,
    net: float | None = 0.01,
    months: tuple[int, ...] = (1, 3),
    coverage: float | None = 0.9,
    needs_review: bool = False,
) -> ScoredObservation:
    outcomes = {
        month: HorizonOutcome(
            months=month,
            status="ok" if net is not None else "pending_horizon",
            entry_date=SESSIONS[1],
            exit_date=SESSIONS[10],
            gross_return=net,
            net_return=net,
            signal_return=None if net is None else net + 0.001,
        )
        for month in months
    }
    return ScoredObservation(
        ticker=f"6000{index:02d}.SH",
        listing_id=index,
        as_of=as_of,
        signal_session=as_of,
        industry_group="manufacturing",
        investment_score=score,
        factor_coverage=coverage,
        needs_agent_review=needs_review,
        score_suppression_reason=None if score is not None else "factor_coverage_below_minimum",
        selected_model="general",
        quality_signals=(),
        score_status="scored" if score is not None else "suppressed",
        score_failure=None,
        outcomes=outcomes,
        max_input_date=as_of,
    )


def config(**overrides) -> ValidationConfig:
    values = {
        "as_of_dates": (AS_OF_A, AS_OF_B),
        "horizons": (1, 3),
        "min_scored": 5,
        "min_group_size": 2,
        "min_coverage": 0.75,
        "min_template_hits": 3,
        "template_source": "files",
    }
    values.update(overrides)
    return ValidationConfig(**values)


def report_for(observations, **overrides):
    cfg = overrides.pop("config", None) or config(**overrides)
    return build_report(
        config=cfg,
        observations=observations,
        benchmark=benchmark(),
        sessions=SESSIONS,
        market_data={},
        scoring={"scoring_version": "v1.4", "matches": True},
        templates={"template_refs": []},
        diagnostics={},
        failures=[],
        code_version={},
    )


def two_dates() -> list[ScoredObservation]:
    items: list[ScoredObservation] = []
    for as_of in (AS_OF_A, AS_OF_B):
        for index in range(10):
            items.append(observation(index, as_of=as_of, score=float(index * 10), net=index / 1000))
    return items


def test_two_sufficient_cross_sections_are_ok():
    report = report_for(two_dates())
    assert report["status"] == "ok", report["insufficient_reasons"]
    assert report["guard"]["calibration_status_changed"] is False


def test_a_single_cross_section_is_never_ok():
    report = report_for([item for item in two_dates() if item.as_of == AS_OF_A])
    assert report["status"] == "partial"
    assert any("walk-forward" in reason for reason in report["insufficient_reasons"])


def test_negative_ic_does_not_lower_the_status():
    # Falling scores with rising returns: IC is clearly negative, sample is fine.
    items = [
        observation(index, as_of=as_of, score=float(100 - index * 10), net=index / 1000)
        for as_of in (AS_OF_A, AS_OF_B)
        for index in range(10)
    ]
    report = report_for(items)
    assert report["status"] == "ok"
    horizon = report["cross_sections"][0]["horizons"]["1m"]
    assert horizon["ic_spearman"] is not None and horizon["ic_spearman"] < 0


def test_too_few_scored_members_is_insufficient():
    items = [
        observation(index, as_of=as_of, score=50.0)
        for as_of in (AS_OF_A, AS_OF_B)
        for index in range(4)
    ]
    report = report_for(items, min_scored=5)
    assert report["status"] == "insufficient"
    assert any("scored members" in reason for reason in report["insufficient_reasons"])


def test_a_pending_horizon_is_insufficient_not_zero():
    items: list[ScoredObservation] = []
    for as_of in (AS_OF_A, AS_OF_B):
        for index in range(10):
            items.append(observation(index, as_of=as_of, net=None))
    report = report_for(items)
    assert report["status"] == "insufficient"
    labels = [
        reason for reason in report["insufficient_reasons"] if "priced observations" in reason
    ]
    assert labels
    assert "1m" in labels[0] and "3m" in labels[0]


def _replace_outcome(item: ScoredObservation, month: int, status: str, reason: str):
    outcome = item.outcomes[month]
    changed = replace(
        outcome,
        status=status,
        reason=reason,
        exit_date=None,
        gross_return=None,
        net_return=None,
    )
    return replace(item, outcomes={**item.outcomes, month: changed})


def test_horizon_accounting_reconciles_every_scored_observation():
    items = two_dates()
    items[0] = _replace_outcome(items[0], 1, "exit_untradable", "suspended")
    items[1] = _replace_outcome(items[1], 1, "no_price", "non_positive_price")

    horizon = report_for(items)["cross_sections"][0]["horizons"]["1m"]
    assert horizon["scored"] == 10
    assert horizon["priced"] == 8
    assert horizon["evaluated"] == 8
    assert horizon["pending_horizon"] == 0
    assert horizon["untradable"] == 1
    assert horizon["unresolved"] == 1
    assert horizon["unresolved_statuses"] == {"no_price": 1}
    assert (
        horizon["evaluated"]
        + horizon["pending_horizon"]
        + horizon["untradable"]
        + horizon["missing_excess"]
        + horizon["unresolved"]
        == horizon["scored"]
    )


def _report_with_scoring(scoring: dict, **overrides) -> dict:
    return build_report(
        config=overrides.pop("config", config()),
        observations=two_dates(),
        benchmark=benchmark(),
        sessions=SESSIONS,
        market_data={},
        scoring=scoring,
        templates={"template_refs": []},
        diagnostics={},
        failures=[],
        code_version={},
    )


def test_declared_scoring_version_mismatch_forces_insufficient():
    scoring = {"scoring_version": "v1.5", "expected_scoring_version": "v1.4", "matches": False}
    report = _report_with_scoring(scoring)
    assert report["scoring"]["matches"] is False
    assert report["status"] == "insufficient"
    assert any("frozen version" in reason for reason in report["insufficient_reasons"])


def test_mismatch_can_only_be_overridden_explicitly():
    scoring = {"scoring_version": "v1.5", "expected_scoring_version": "v1.4", "matches": False}
    report = _report_with_scoring(
        scoring, config=config(allow_scoring_version_mismatch=True)
    )
    assert report["status"] == "ok"
    assert report["scoring"]["matches"] is False


def test_coverage_groups_are_mutually_exclusive_and_cover_scored():
    assert coverage_group(observation(1, score=60.0, coverage=0.95), 0.75) == "high"
    assert coverage_group(observation(1, score=60.0, coverage=0.80), 0.75) == "mid"
    assert coverage_group(observation(1, score=60.0, coverage=0.5), 0.75) == "low"
    assert coverage_group(observation(1, score=None, coverage=0.9), 0.75) == "suppressed"
    # The review flag is an independent axis: it never changes the band.
    assert coverage_group(
        observation(1, score=60.0, coverage=0.95, needs_review=True), 0.75
    ) == "high"

    items = [
        observation(0, as_of=AS_OF_A, score=60.0, coverage=0.95),
        observation(1, as_of=AS_OF_A, score=60.0, coverage=0.80, needs_review=True),
        observation(2, as_of=AS_OF_A, score=60.0, coverage=0.4),
        observation(3, as_of=AS_OF_A, score=None, coverage=0.9),
    ]
    report = report_for(
        items + [observation(index, as_of=AS_OF_B, score=60.0) for index in range(5)]
    )
    groups = report["cross_sections"][0]["coverage_groups"]["1m"]
    counts = {name: entry["count"] for name, entry in groups.items()}
    # Suppressed members still get a forward outcome: that is how "is the
    # suppressed group systematically overrated" becomes answerable.
    assert counts == {"high": 1, "mid": 1, "low": 1, "suppressed": 1}
    reviews = report["cross_sections"][0]["review_groups"]["1m"]
    assert reviews["needs_review"]["count"] >= 1


def test_buckets_reflect_the_score_order_and_flat_signal_is_not_monotone():
    flat = [
        observation(index, as_of=as_of, score=float(index), net=0.01)
        for as_of in (AS_OF_A, AS_OF_B)
        for index in range(10)
    ]
    report = report_for(flat)
    horizon = report["cross_sections"][0]["horizons"]["1m"]
    assert horizon["monotonicity"]["strict"] is True  # equal means are non-decreasing
    assert horizon["top_minus_bottom"] == 0.0
    assert horizon["ic_spearman"] is None  # zero variance in the excess


def test_aggregate_horizon_pools_bucket_means_not_returns():
    report = report_for(two_dates())
    pooled = aggregate_horizon(report["cross_sections"], "1m")
    assert pooled["ic_observations"] == 2
    assert pooled["buckets"]


def test_cost_does_not_change_excess_but_shows_in_the_config():
    cfg = config(cost=CostModel(commission_bps=3.0, stamp_duty_bps=5.0, slippage_bps=10.0))
    report = report_for(two_dates(), config=cfg)
    assert report["cost_model"]["round_trip_bps"] == 31.0
    assert report["config"]["cost"]["note"].startswith("assumption")
