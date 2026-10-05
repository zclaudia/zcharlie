"""Structural assertions for the golden corpus matrix (work package A1).

The manifest declares which A-share samples the golden parity tests must
cover. Every coverage dimension from docs/product/mvp-completion-plan.md
work package A1 is asserted directly from the manifest so the matrix cannot
silently lose coverage. Fixture collection (work package A2) may swap
tickers only while keeping every dimension satisfied.
"""

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from investment_agent.research.contracts import CANONICAL_SCHEMA_VERSION
from investment_agent.research_kernel.scoring import SCHEMA_VERSION, SCORING_VERSION

FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "research_kernel"
MANIFEST_PATH = FIXTURE_ROOT / "manifest.yaml"

TICKER_RE = re.compile(r"^\d{6}\.(SH|SZ|BJ)$")
VALID_EXCHANGES = {"SSE", "SZSE", "BSE"}
VALID_BUSINESS_CATEGORIES = {
    "bank_finance",
    "consumer",
    "high_end_manufacturing",
    "cyclical_resource",
    "software_asset_light",
    "high_growth",
}
VALID_TRAITS = {
    "baseline",
    "key_missing_fields",
    "low_or_negative_profitability",
    "borderline_freshness",
    "low_model_fit",
}
VALID_SCALES = {"large_cap", "mid_cap", "small_cap"}
VALID_FIXTURE_STATUS = {"sanitized", "pending"}
REQUIRED_FIXTURE_FILES = ("pool.json", "market.json", "financial.json")

SAMPLE_REQUIRED_FIELDS = (
    "ticker",
    "name",
    "exchange",
    "industry",
    "business_category",
    "scale",
    "traits",
    "report_year",
    "collected_at",
    "fixture_status",
)


def _load_manifest() -> dict[str, Any]:
    payload = yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8"))
    assert isinstance(payload, dict), "manifest must be a mapping"
    return payload


def _samples() -> list[dict[str, Any]]:
    samples = _load_manifest()["samples"]
    assert isinstance(samples, list) and samples, "manifest must list samples"
    return [sample for sample in samples if isinstance(sample, dict)]


def test_manifest_pins_current_kernel_and_canonical_versions():
    manifest = _load_manifest()
    assert manifest["canonical_schema_version"] == CANONICAL_SCHEMA_VERSION
    assert manifest["scoring_version"] == SCORING_VERSION
    assert manifest["score_schema_version"] == SCHEMA_VERSION
    assert manifest["fixture_policy"]["provider"] == "ifind-proxy"
    assert manifest["fixture_policy"]["sanitized"] is True
    assert manifest["fixture_policy"]["offline"] is True


def test_corpus_size_within_planned_bounds():
    requirements = _load_manifest()["coverage_requirements"]
    samples = _samples()
    assert requirements["min_samples"] <= len(samples) <= requirements["max_samples"]


def test_every_sample_has_valid_complete_metadata():
    tickers = set()
    for sample in _samples():
        ticker = sample["ticker"]
        assert TICKER_RE.match(ticker), f"invalid ticker format: {ticker}"
        assert ticker not in tickers, f"duplicate ticker in manifest: {ticker}"
        tickers.add(ticker)

        for field in SAMPLE_REQUIRED_FIELDS:
            assert field in sample, f"{ticker} missing required field {field}"
        assert sample["exchange"] in VALID_EXCHANGES, ticker
        assert sample["business_category"] in VALID_BUSINESS_CATEGORIES, ticker
        assert sample["scale"] in VALID_SCALES, ticker
        assert set(sample["traits"]) <= VALID_TRAITS, ticker
        assert sample["traits"], f"{ticker} must declare at least one trait"
        assert sample["fixture_status"] in VALID_FIXTURE_STATUS, ticker
        assert isinstance(sample["name"], str) and sample["name"], ticker
        assert isinstance(sample["industry"], str) and sample["industry"], ticker
        assert isinstance(sample["report_year"], int), ticker
        if sample["fixture_status"] == "sanitized":
            assert sample["collected_at"], f"{ticker} sanitized sample needs collected_at"


def test_exchange_coverage_meets_minimums():
    requirements = _load_manifest()["coverage_requirements"]["min_per_exchange"]
    counts: dict[str, int] = {}
    for sample in _samples():
        counts[sample["exchange"]] = counts.get(sample["exchange"], 0) + 1
    for exchange, minimum in requirements.items():
        assert counts.get(exchange, 0) >= minimum, (
            f"{exchange} needs >= {minimum} samples, found {counts.get(exchange, 0)}"
        )


def test_blocked_exchanges_are_documented_and_empty():
    requirements = _load_manifest()["coverage_requirements"]
    blocked = requirements.get("blocked_exchanges", {})
    for exchange, record in blocked.items():
        assert record.get("required_when_available", 0) >= 1, (
            f"blocked exchange {exchange} must state how many samples to restore"
        )
        assert record.get("reason"), f"blocked exchange {exchange} must document why"
        assert requirements["min_per_exchange"].get(exchange, 0) == 0, (
            f"blocked exchange {exchange} must not also carry a live minimum"
        )
        carrying = [sample["ticker"] for sample in _samples() if sample["exchange"] == exchange]
        assert not carrying, f"exchange {exchange} is blocked but still has samples: {carrying}"


def test_business_category_coverage_complete():
    categories = {sample["business_category"] for sample in _samples()}
    missing = VALID_BUSINESS_CATEGORIES - categories
    assert not missing, f"business categories missing from corpus: {sorted(missing)}"


def test_boundary_trait_coverage_meets_minimums():
    requirements = _load_manifest()["coverage_requirements"]["min_per_boundary_trait"]
    counts: dict[str, int] = {}
    for sample in _samples():
        for trait in sample["traits"]:
            counts[trait] = counts.get(trait, 0) + 1
    for trait, minimum in requirements.items():
        assert counts.get(trait, 0) >= minimum, (
            f"trait {trait} needs >= {minimum} samples, found {counts.get(trait, 0)}"
        )


def test_scale_bands_all_covered():
    requirements = set(_load_manifest()["coverage_requirements"]["scales"])
    scales = {sample["scale"] for sample in _samples()}
    assert requirements <= scales, f"scale bands missing from corpus: {sorted(requirements - scales)}"


def test_sanitized_samples_have_frozen_fixture_files():
    for sample in _samples():
        if sample["fixture_status"] != "sanitized":
            continue
        fixture_dir = FIXTURE_ROOT / sample["ticker"].split(".")[0]
        for filename in REQUIRED_FIXTURE_FILES:
            path = fixture_dir / filename
            assert path.is_file(), f"{sample['ticker']} fixture missing {filename}"
        has_legacy_oracle = all(
            (fixture_dir / name).is_file()
            for name in ("legacy_scores.json", "legacy_report_context.json")
        )
        has_frozen_golden = all(
            (fixture_dir / name).is_file()
            for name in ("golden_scores.json", "golden_report_context.json")
        )
        assert has_legacy_oracle or has_frozen_golden, (
            f"{sample['ticker']} needs a legacy oracle or frozen golden kernel output"
        )


def test_existing_golden_sample_stays_in_corpus():
    sanitized = {
        sample["ticker"] for sample in _samples() if sample["fixture_status"] == "sanitized"
    }
    assert "600406.SH" in sanitized, "validated 600406.SH golden sample must not be dropped"


@pytest.mark.parametrize(
    "trait",
    sorted(VALID_TRAITS - {"baseline"}),
)
def test_boundary_trait_declares_intent_in_notes(trait: str):
    samples = [sample for sample in _samples() if trait in sample["traits"]]
    assert samples, f"trait {trait} has no samples"
    for sample in samples:
        notes = sample.get("notes", "")
        assert notes, f"{sample['ticker']} must explain why it carries trait {trait}"
