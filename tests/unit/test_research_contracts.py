from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from investment_agent.research.contracts import FieldSource, MarketDataItem


def _market_item(**overrides: object) -> dict[str, object]:
    timestamp = datetime(2026, 10, 5, tzinfo=UTC).isoformat()
    research_fields = (
        "latest_price",
        "pe",
        "pb",
        "ps",
        "dividend_yield",
        "return_1y",
        "volatility_1y",
        "max_drawdown_1y",
        "market_cap",
    )
    payload: dict[str, object] = {
        "symbol": "600406",
        "latest_price": 22.52,
        "pe": 21.65,
        "pb": 3.44,
        "ps": 2.73,
        "dividend_yield": None,
        "return_1y": 0.12,
        "volatility_1y": 0.21,
        "max_drawdown_1y": 0.30,
        "market_cap": 180_875_148_633.12,
        "source": "ifind-proxy",
        "updated_at": timestamp,
        "field_sources": {
            field: {
                "field": field,
                "provider": "ifind",
                "source": "/fixture",
                "quality": "missing" if field == "dividend_yield" else "direct",
                "updated_at": timestamp,
                "note": "",
            }
            for field in research_fields
        },
        "missing_fields": ["dividend_yield"],
        "quality_flags": [
            {
                "field": "dividend_yield",
                "flag": "missing_field",
                "message": "not available",
                "provider": "ifind",
                "severity": "review",
            }
        ],
        "provider_conflicts": {},
        "warnings": ["not available"],
        "needs_agent_review": True,
    }
    payload.update(overrides)
    return payload


def test_market_contract_preserves_null_and_provenance():
    item = MarketDataItem.model_validate(_market_item())

    assert item.dividend_yield is None
    assert item.needs_agent_review is True
    assert isinstance(item.field_sources["pe"], FieldSource)


def test_market_contract_rejects_empty_numeric_value():
    with pytest.raises(ValidationError):
        MarketDataItem.model_validate(_market_item(pe=""))


def test_market_contract_rejects_missing_field_without_review():
    with pytest.raises(ValidationError, match="needs_agent_review"):
        MarketDataItem.model_validate(_market_item(needs_agent_review=False))


def test_market_contract_requires_field_provenance():
    payload = _market_item()
    field_sources = dict(payload["field_sources"])  # type: ignore[arg-type]
    field_sources.pop("pe")
    payload["field_sources"] = field_sources

    with pytest.raises(ValidationError, match="missing provenance for: pe"):
        MarketDataItem.model_validate(payload)
