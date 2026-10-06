"""Shared rule fixtures: a minimal valid template and instance."""

from __future__ import annotations

import copy
from typing import Any

from investment_agent.rules.validation import RuleContext

CONTEXT = RuleContext(
    factor_names=frozenset(
        {
            "roe",
            "pe_percentile_5y",
            "debt_ratio",
            "gross_margin",
            "revenue_growth_3y",
            "is_st",
            "report_overdue",
            "market_cap",
        }
    ),
    industry_groups=frozenset({"consumer", "utility", "banking", "other"}),
)


def template_data(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": "quality_test",
        "name": "Quality test",
        "description": "test template",
        "applicable_groups": ["consumer", "utility"],
        "screen": {
            "all": [
                {"factor": "roe", "op": ">=", "value": "$roe_min"},
                {"factor": "pe_percentile_5y", "op": "<=", "value": "$pe_pct_max"},
                {
                    "any": [
                        {"factor": "debt_ratio", "op": "<", "value": 0.6},
                        {"factor": "gross_margin", "op": "between", "value": [0.3, 0.9]},
                    ]
                },
            ]
        },
        "candidate_zones": {
            "buy_zone": {"all": [{"factor": "pe_percentile_5y", "op": "<=", "value": 0.2}]},
            "drop": {"any": [{"factor": "roe", "op": "<", "value": 0.08}]},
        },
        "holding_zones": {
            "add": {
                "all": [
                    {"factor": "pe_percentile_5y", "op": "<=", "value": 0.25},
                    {"factor": "position_weight", "op": "<", "value": "$weight_max"},
                ]
            },
            "trim": {"any": [{"factor": "gain_vs_cost", "op": ">=", "value": 0.4}]},
            "exit_review": {"any": [{"factor": "roe", "op": "<", "value": 0.06}]},
        },
        "params": {"roe_min": 0.15, "pe_pct_max": 0.4, "weight_max": 0.08},
        "param_ranges": {
            "roe_min": {"min": 0.08, "max": 0.3},
            "pe_pct_max": {"min": 0.1, "max": 0.7},
            "weight_max": {"min": 0.02, "max": 0.2},
        },
        "calibration_status": "uncalibrated",
    }
    result = copy.deepcopy(data)
    result.update(overrides)
    return result


def instance_data(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "template_ref": "quality_test@1",
        "symbol": "600519.SH",
        "scope": "candidate",
        "params": {"pe_pct_max": 0.3},
        "zones": {
            "buy_zone": {"all": [{"factor": "pe_percentile_5y", "op": "<=", "value": "$pe_pct_max"}]}
        },
        "drafted_by": "llm:test",
        "draft_rationale": "valuation in the lower third of its 5y range",
    }
    data.update(overrides)
    return data
