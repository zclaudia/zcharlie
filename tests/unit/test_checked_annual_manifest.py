"""Acceptance invariants for the dated, reviewed eight-company evidence index."""

import json
from datetime import date, timedelta
from pathlib import Path

from investment_agent.materials.annual_validation import _located_context, verify_annual_package

MANIFEST = (
    Path(__file__).resolve().parents[2]
    / "config/research_material/annual_validation_2026-10-15.json"
)
EXPECTED = {
    "clp-holdings": ("2025-01-01", "2025-12-31", "2026-03-12", "HKD"),
    "hsbc-holdings": ("2025-01-01", "2025-12-31", "2026-02-26", "USD"),
    "tencent-holdings": ("2025-01-01", "2025-12-31", "2026-04-09", "CNY"),
    "apple": ("2024-09-29", "2025-09-27", "2025-10-31", "USD"),
    "costco": ("2025-09-01", "2026-08-30", "2026-10-07", "USD"),
    "jpmorgan-chase": ("2025-01-01", "2025-12-31", "2026-02-13", "USD"),
    "exxon-mobil": ("2025-01-01", "2025-12-31", "2026-02-18", "USD"),
    "tsmc": ("2025-01-01", "2025-12-31", "2026-04-16", "TWD"),
}


def test_dated_manifest_retains_actual_fiscal_dates_fields_and_original_currencies():
    manifest = json.loads(MANIFEST.read_text())
    companies = {company["company_key"]: company for company in manifest["companies"]}
    assert manifest["as_of"] == "2026-10-15"
    assert set(companies) == set(EXPECTED)
    for key, (start, end, published, currency) in EXPECTED.items():
        company = companies[key]
        annual = company["latest_annual"]
        assert (annual["period_start"], annual["period_end"], annual["published_on"]) == (
            start, end, published,
        )
        fields = company["fields"]
        assert len(fields) == 6
        assert {field["category"] for field in fields} == {
            "revenue", "profit", "assets", "equity", "operating_cash_flow", "capex",
        }
        for field in fields:
            fact = field["fact"]
            assert fact["period"]["end"] == end
            assert fact["value"]["currency"] == currency
        years = sorted(company["fiscal_years"], key=lambda year: year["period_end"])
        assert len(years) == 4
        assert years[-1]["period_end"] == end
        for previous, current in zip(years, years[1:], strict=False):
            assert date.fromisoformat(previous["period_end"]) + timedelta(days=1) == (
                date.fromisoformat(current["period_start"])
            )


def test_manifest_cannot_replace_missing_original_bodies(tmp_path):
    report = verify_annual_package(MANIFEST, tmp_path)
    assert not report["complete"]
    assert len(report["companies"]) == 8
    assert all(company["status"] == "partial" for company in report["companies"])
    assert not any(company["latest_annual_checked"] for company in report["companies"])


def test_exact_statement_anchor_wins_over_earlier_case_variant_summary():
    excerpt = "Total Assets 448,980 453,475"
    text = "Total assets 448,980 453,475 376,317 " + "x" * 2000 + excerpt
    context = _located_context({"_text": text}, {"kind": "text", "anchor": excerpt})
    assert context is not None and excerpt in context
