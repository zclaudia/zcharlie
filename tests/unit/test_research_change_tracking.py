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
    assert diff.as_dict()["schema_version"] == "research-change.v1"


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
    assert changes["key_metrics.roe"]["meaningful"] is True
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
