"""Deterministic research change tracking tests."""

from investment_agent.research.change_tracking import build_research_diff


def test_first_snapshot_is_an_explicit_baseline():
    diff = build_research_diff(
        current_run_id=7,
        current_score={"business_score": 55.0},
        previous_run_id=None,
        previous_score=None,
    )

    assert diff.baseline is True
    assert diff.previous_run_id is None
    assert diff.current_run_id == 7
    assert diff.has_meaningful_changes is False
    assert diff.changes == []
    assert diff.as_dict()["schema_version"] == "research-change.v2"


def test_diff_covers_scores_strategy_financials_and_quality():
    previous = {
        "business_score": 50.0,
        "investment_score": 50.0,
        "research_confidence_score": 90.0,
        "factor_scores": {"growth_durability": 60.0},
        "primary_strategy": "quality_growth",
        "suggestion": "needs_review",
        "key_metrics": {"roe": 0.10, "pe": 20.0},
        "data_quality_score": 0.92,
        "missing_fields": ["dividend_yield"],
        "provider_conflicts": {},
        "quality_flags": [
            {
                "field": "pe",
                "flag": "derived_field",
                "severity": "info",
                "provider": "ifind",
                "message": "old display text",
            }
        ],
        "warnings": [],
        "freshness": {
            "market_data": {
                "is_stale": False,
                "age_hours": 12.0,
                "updated_at": "2026-10-04T00:00:00Z",
            }
        },
        "updated_at": "2026-10-04T00:00:00Z",
    }
    current = {
        "business_score": 50.5,
        "investment_score": 52.0,
        "research_confidence_score": 90.0,
        "factor_scores": {"growth_durability": 62.0},
        "primary_strategy": "steady_quality_growth",
        "suggestion": "watch",
        "key_metrics": {"roe": 0.102, "pe": 20.1},
        "data_quality_score": 0.925,
        "missing_fields": [],
        "provider_conflicts": {},
        "quality_flags": [
            {
                "provider": "ifind",
                "severity": "info",
                "flag": "derived_field",
                "field": "pe",
                "message": "new display text is intentionally ignored",
            }
        ],
        "warnings": [],
        "freshness": {
            "market_data": {
                "is_stale": True,
                "age_hours": 36.0,
                "updated_at": "2026-10-05T00:00:00Z",
            }
        },
        "updated_at": "2026-10-05T00:00:00Z",
    }

    diff = build_research_diff(
        current_run_id=8,
        current_score=current,
        previous_run_id=7,
        previous_score=previous,
    )
    changes = {change["field"]: change for change in diff.changes}

    assert diff.baseline is False
    assert diff.previous_run_id == 7
    assert diff.has_meaningful_changes is True
    assert set(diff.category_counts) == {
        "score",
        "strategy",
        "financial_metric",
        "quality",
    }
    assert changes["business_score"]["delta"] == 0.5
    assert changes["business_score"]["meaningful"] is False
    assert changes["investment_score"]["meaningful"] is True
    assert changes["factor_scores.growth_durability"]["meaningful"] is True
    assert changes["primary_strategy"]["meaningful"] is True
    assert changes["key_metrics.roe"]["relative_change_pct"] == 2.0
    # 0.2pp ROE move is below the 1pp fundamental threshold.
    assert changes["key_metrics.roe"]["meaningful"] is False
    assert changes["key_metrics.pe"]["meaningful"] is False
    assert changes["data_quality_score"]["meaningful"] is False
    assert changes["missing_fields"]["meaningful"] is True
    assert changes["freshness.market_data.is_stale"]["meaningful"] is True
    assert "quality_flags" not in changes
    assert "updated_at" not in changes


def test_no_stable_field_changes_produces_empty_diff():
    previous = {
        "business_score": 55.0,
        "freshness": {"stock": {"is_stale": False, "age_hours": 1.0}},
        "updated_at": "2026-10-04T00:00:00Z",
    }
    current = {
        "business_score": 55.0,
        "freshness": {"stock": {"is_stale": False, "age_hours": 25.0}},
        "updated_at": "2026-10-05T00:00:00Z",
    }

    diff = build_research_diff(
        current_run_id=2,
        current_score=current,
        previous_run_id=1,
        previous_score=previous,
    )

    assert diff.baseline is False
    assert diff.change_count == 0
    assert diff.meaningful_change_count == 0
    assert diff.has_meaningful_changes is False
    assert diff.changes == []


def _metrics_diff(before: dict[str, object], after: dict[str, object]):
    diff = build_research_diff(
        current_run_id=2,
        current_score={"scoring_version": "v1.2", "key_metrics": after},
        previous_run_id=1,
        previous_score={"scoring_version": "v1.2", "key_metrics": before},
    )
    return diff, {change["field"]: change for change in diff.changes}


def test_normal_daily_price_move_is_not_meaningful():
    # A ~2% price move shifts every price-derived metric by ~2%.
    diff, changes = _metrics_diff(
        {
            "market_cap": 1.0e11,
            "pe": 20.0,
            "pb": 3.0,
            "dividend_yield": 0.030,
            "max_drawdown_1y": -0.20,
        },
        {
            "market_cap": 1.02e11,
            "pe": 20.4,
            "pb": 3.06,
            "dividend_yield": 0.0294,
            "max_drawdown_1y": -0.21,
        },
    )

    assert set(changes) == {
        "key_metrics.market_cap",
        "key_metrics.pe",
        "key_metrics.pb",
        "key_metrics.dividend_yield",
        "key_metrics.max_drawdown_1y",
    }
    assert all(change["meaningful"] is False for change in changes.values())
    assert diff.has_meaningful_changes is False


def test_large_valuation_and_fundamental_moves_are_meaningful():
    _, changes = _metrics_diff(
        {
            "pe": 20.0,
            "roe": 0.15,
            "debt_ratio": 0.50,
            "max_drawdown_1y": -0.10,
            "revenue_growth_3y": 0.20,
        },
        {
            "pe": 23.0,
            "roe": 0.13,
            "debt_ratio": 0.53,
            "max_drawdown_1y": -0.25,
            "revenue_growth_3y": 0.21,
        },
    )

    assert changes["key_metrics.pe"]["meaningful"] is True
    assert changes["key_metrics.roe"]["meaningful"] is True
    assert changes["key_metrics.debt_ratio"]["meaningful"] is True
    assert changes["key_metrics.max_drawdown_1y"]["meaningful"] is True
    assert changes["key_metrics.revenue_growth_3y"]["meaningful"] is False


def test_metric_appearing_is_meaningful_and_unknown_metric_uses_default():
    _, changes = _metrics_diff(
        {"pe": None, "brand_new_metric": 100.0},
        {"pe": 18.0, "brand_new_metric": 105.0, "ps": None},
    )

    assert changes["key_metrics.pe"]["meaningful"] is True
    assert changes["key_metrics.brand_new_metric"]["meaningful"] is False
    assert "key_metrics.ps" not in changes


def test_scoring_version_change_is_recorded_once_and_suppresses_algorithm_noise():
    previous = {
        "scoring_version": "v1.2",
        "business_score": 50.0,
        "investment_score": 50.0,
        "factor_scores": {"growth_durability": 60.0},
        "suggestion": "needs_review",
        "data_quality_score": 0.90,
        "key_metrics": {"roe": 0.10},
        "missing_fields": [],
    }
    current = {
        "scoring_version": "v1.3",
        "business_score": 58.0,
        "investment_score": 58.0,
        "factor_scores": {"growth_durability": 70.0, "new_factor": 40.0},
        "suggestion": "watch",
        "data_quality_score": 0.95,
        "key_metrics": {"roe": 0.13},
        "missing_fields": ["dividend_yield"],
        "new_output_field": None,
    }

    diff = build_research_diff(
        current_run_id=2,
        current_score=current,
        previous_run_id=1,
        previous_score=previous,
    )
    changes = {change["field"]: change for change in diff.changes}

    version_changes = [
        c for c in diff.changes if c.get("kind") == "scoring_version_changed"
    ]
    assert len(version_changes) == 1
    assert version_changes[0]["category"] == "methodology"
    assert version_changes[0]["before"] == "v1.2"
    assert version_changes[0]["after"] == "v1.3"
    assert version_changes[0]["meaningful"] is False
    for field in (
        "business_score",
        "investment_score",
        "factor_scores.growth_durability",
        "factor_scores.new_factor",
        "suggestion",
        "data_quality_score",
    ):
        assert changes[field]["meaningful"] is False, field
        assert changes[field]["suppressed_reason"] == "scoring_version_changed"
    # Input-data changes still count.
    assert changes["key_metrics.roe"]["meaningful"] is True
    assert changes["missing_fields"]["meaningful"] is True
    assert diff.meaningful_change_count == 2


def test_pure_algorithm_change_has_no_meaningful_changes():
    diff = build_research_diff(
        current_run_id=2,
        current_score={"business_score": 70.0, "suggestion": "watch"},
        previous_run_id=1,
        previous_score={"business_score": 50.0, "suggestion": "avoid"},
        current_scoring_version="v1.3",
        previous_scoring_version="v1.2",
    )

    assert diff.change_count == 3
    assert diff.has_meaningful_changes is False


def test_unknown_scoring_version_does_not_trigger_guard():
    diff = build_research_diff(
        current_run_id=2,
        current_score={"scoring_version": "v1.3", "business_score": 70.0},
        previous_run_id=1,
        previous_score={"business_score": 50.0},
    )

    assert all(c.get("kind") != "scoring_version_changed" for c in diff.changes)
    assert diff.has_meaningful_changes is True
