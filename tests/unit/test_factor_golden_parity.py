"""Observation-path parity (decision D3): factor values == frozen canonical metrics.

For every golden-corpus sample with retained iFinD responses, an in-memory
observation ``SnapshotView`` is built from the decoded provider rows and each
factor is compared field by field with the frozen canonical ``market.json``
/ ``financial.json`` value the scoring kernel consumes. Equality is exact
(the factor library owns the single definition of every formula).

600406.SH keeps no ``provider_raw.json`` (captured before raw responses were
retained), so it has no provider inputs to rebuild a view from.
"""

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import yaml

from investment_agent.factors.catalog import DEFAULT_REGISTRY
from investment_agent.factors.view import OBSERVATION, ifind_research_view
from investment_agent.research.ifind_chain import parse_proxy_rows

FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "research_kernel"
MANIFEST = yaml.safe_load((FIXTURE_ROOT / "manifest.yaml").read_text(encoding="utf-8"))

# canonical payload field -> factor name
MARKET_PARITY = {
    "pe": "pe_fy",
    "pb": "pb_fy",
    "ps": "ps_fy",
    "return_1y": "return_1y",
    "volatility_1y": "volatility_1y",
    "max_drawdown_1y": "max_drawdown_1y",
    "market_cap": "market_cap",
}
FINANCIAL_PARITY = {
    "roe": "roe",
    "roe_stability": "roe_stability",
    "gross_margin": "gross_margin",
    "net_margin": "net_margin",
    "debt_ratio": "debt_ratio",
    "revenue_growth_3y": "revenue_growth_3y",
    "net_profit_growth_3y": "net_profit_growth_3y",
    "free_cash_flow": "free_cash_flow",
}


def _samples_with_raw() -> list[dict[str, Any]]:
    return [
        sample
        for sample in MANIFEST["samples"]
        if (FIXTURE_ROOT / sample["ticker"].split(".")[0] / "provider_raw.json").is_file()
    ]


def _item(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    (item,) = payload["items"]
    return item


def _view(sample: dict[str, Any]):
    fixture_dir = FIXTURE_ROOT / sample["ticker"].split(".")[0]
    raw = json.loads((fixture_dir / "provider_raw.json").read_text(encoding="utf-8"))
    rows = {name: parse_proxy_rows(entry["body"]) for name, entry in raw["requests"].items()}
    return ifind_research_view(
        ticker=sample["ticker"],
        as_of=date.fromisoformat(str(sample["collected_at"])),
        report_year=int(MANIFEST["report_year"]),
        rows=rows,
    )


def test_parity_covers_every_sample_with_provider_inputs():
    tickers = {sample["ticker"] for sample in _samples_with_raw()}
    assert len(tickers) == len(MANIFEST["samples"]) - 1
    assert "600406.SH" not in tickers


@pytest.mark.parametrize("sample", _samples_with_raw(), ids=lambda sample: sample["ticker"])
def test_factor_values_equal_frozen_canonical_metrics(sample: dict[str, Any]):
    fixture_dir = FIXTURE_ROOT / sample["ticker"].split(".")[0]
    view = _view(sample)
    assert view.policy == OBSERVATION
    expected = {
        **{
            factor: _item(fixture_dir / "market.json")[field]
            for field, factor in MARKET_PARITY.items()
        },
        **{
            factor: _item(fixture_dir / "financial.json")[field]
            for field, factor in FINANCIAL_PARITY.items()
        },
    }

    mismatches = []
    for factor_name, canonical in expected.items():
        value = DEFAULT_REGISTRY.get(factor_name).compute(view, enforce_applicability=False)
        if value.value != canonical:
            mismatches.append(f"{factor_name}: factor={value.value!r} canonical={canonical!r}")
        if canonical is None:
            assert value.quality == "missing", factor_name
        else:
            assert value.quality in {"ok", "approx"}, factor_name
    assert not mismatches, f"{sample['ticker']} drifted from canonical:\n" + "\n".join(mismatches)


@pytest.mark.parametrize("sample", _samples_with_raw(), ids=lambda sample: sample["ticker"])
def test_short_history_flag_matches_canonical_quality_flags(sample: dict[str, Any]):
    fixture_dir = FIXTURE_ROOT / sample["ticker"].split(".")[0]
    canonical_short = any(
        flag["flag"] == "short_price_history"
        for flag in _item(fixture_dir / "market.json")["quality_flags"]
    )
    value = DEFAULT_REGISTRY.get("return_1y").compute(_view(sample))
    assert ("short_history" in value.flags) is canonical_short


@pytest.mark.parametrize("sample", _samples_with_raw(), ids=lambda sample: sample["ticker"])
def test_non_meaningful_multiples_carry_the_canonical_flag(sample: dict[str, Any]):
    fixture_dir = FIXTURE_ROOT / sample["ticker"].split(".")[0]
    canonical = {
        flag["field"]: flag["flag"]
        for flag in _item(fixture_dir / "market.json")["quality_flags"]
        if flag["flag"] in {"negative_earnings", "negative_book_value", "non_positive_revenue"}
    }
    view = _view(sample)
    for field, factor in (("pe", "pe_fy"), ("pb", "pb_fy"), ("ps", "ps_fy")):
        flags = DEFAULT_REGISTRY.get(factor).compute(view).flags
        if field in canonical:
            assert canonical[field] in flags
        else:
            assert not {"negative_earnings", "negative_book_value", "non_positive_revenue"} & set(
                flags
            )
