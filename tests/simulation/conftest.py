from datetime import date

import pytest

from investment_agent.simulation.contracts import ExperimentConfig


@pytest.fixture
def sim_config():
    return ExperimentConfig.model_validate(
        {
            "name": "daily forward",
            "universe": [1, 2],
            "start_date": date(2026, 11, 9),
            "end_date": date(2026, 12, 9),
            "rules": {
                "version": "mainboard_v1",
                "content_hash": "a" * 64,
                "calendar_version": "sse_2026",
                "fee_schedule": {
                    "version": "fees_v1",
                    "commission_rate": "0.0003",
                    "minimum_commission": "5",
                    "sell_tax_rate": "0.0005",
                },
                "slippage": {"version": "slippage_v1", "adverse_rate": "0.001"},
            },
            "participants": [
                {"name": "cash", "kind": "cash", "baseline_version": "cash_v1"},
                {"name": "equal", "kind": "equal_weight", "baseline_version": "equal_v1"},
                {
                    "name": "model",
                    "kind": "agent",
                    "provider_ref": "catalog-provider",
                    "model_ref": "catalog-model",
                    "method_ref": "method_v1",
                    "method_hash": "b" * 64,
                    "prompt_version": "prompt_v1",
                    "prompt_hash": "c" * 64,
                },
            ],
        }
    )
