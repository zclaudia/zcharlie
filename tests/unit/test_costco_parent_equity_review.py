from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[2]
REVIEW_PATH = (
    ROOT / "tests/fixtures/materials/annual_validation/costco-parent-equity-reviewed-facts.json"
)
HISTORY_PATH = ROOT / "tests/fixtures/materials/annual_validation/us-history-reviewed-facts.json"


def _review() -> dict[str, Any]:
    return json.loads(REVIEW_PATH.read_text())


def _validate_parent_rows(review: dict[str, Any]) -> None:
    contexts = review["source_contexts"]
    for row in review["reviewed_parent_equity_rows"]:
        inline = row["inline_xbrl"]
        detail = row["locator_detail"]
        assert row["company_key"] == "costco"
        assert row["metric"] == "equity_parent"
        assert row["category"] in {"equity", "beginning_equity"}
        assert row["currency"] == "USD"
        assert row["scale"] == 1_000_000
        assert row["raw_unit"] == "USD million"
        assert row["period_start"] is None
        assert row["source_label"] == "Total Costco stockholders’ equity"
        assert inline["name"] == "us-gaap:StockholdersEquity"
        assert inline["unit_ref"] == "usd"
        assert inline["scale"] == "6"
        assert inline["context_ref"] == detail["context_ref"]
        assert detail["xbrl_tag"] == inline["name"]
        assert inline["fact_value"] == row["value"]
        assert row["period_end"] == contexts[inline["context_ref"]]["instant"]
        assert contexts[inline["context_ref"]]["entity_cik"] == "0000909832"
        assert row["source_sha256"] == review["source_capture"]["sha256"]
        assert row["accession"] == review["source_capture"]["accession"]


def test_parent_equity_rows_have_exact_scope_period_and_source_units() -> None:
    review = _review()
    _validate_parent_rows(review)

    rows = {row["year"]: row for row in review["reviewed_parent_equity_rows"]}
    assert rows[2022]["category"] == "beginning_equity"
    assert rows[2022]["beginning_of_year_for_fy"] == 2023
    assert rows[2022]["period_end"] == "2022-08-28"
    assert rows[2022]["value"] == "20,642"
    assert rows[2023]["category"] == "equity"
    assert rows[2023]["period_end"] == "2023-09-03"
    assert rows[2023]["value"] == "25,058"


def test_source_excerpts_keep_parent_nci_and_reported_total_distinct() -> None:
    review = _review()
    for bridge in review["reported_total_and_nci"]:
        parent = int(bridge["parent_equity_usd_millions"].replace(",", ""))
        nci = int(bridge["nci_usd_millions"].replace(",", ""))
        total = int(bridge["total_equity_usd_millions"].replace(",", ""))
        assert parent + nci == total
        assert bridge["parent_tag"] == "us-gaap:StockholdersEquity"
        assert bridge["nci_tag"] == "us-gaap:MinorityInterest"
        assert (
            bridge["total_tag"]
            == "us-gaap:StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"
        )
        assert bridge["context_ref"] in review["source_contexts"]
        detail_by_role = {detail["role"]: detail for detail in bridge["source_fact_details"]}
        assert set(detail_by_role) == {
            "parent_equity",
            "noncontrolling_interest",
            "reported_total_equity",
        }
        expected_tags = {
            "parent_equity": "us-gaap:StockholdersEquity",
            "noncontrolling_interest": "us-gaap:MinorityInterest",
            "reported_total_equity": "us-gaap:StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
        }
        for role, detail in detail_by_role.items():
            assert detail["tag"] == expected_tags[role]
            assert detail["context_ref"] == bridge["context_ref"]
            assert detail["unit_ref"] == "usd"
            assert detail["currency"] == "USD"
            assert detail["xbrl_scale"] == "6"
            assert detail["xbrl_decimals"] == "-6"
            extracted = BeautifulSoup(detail["raw_html_row"], "html.parser")
            inline = extracted.find(attrs={"id": detail["fact_id"]})
            assert inline is not None
            assert inline.get("name") == detail["tag"]
            assert inline.get("contextref") == detail["context_ref"]
            assert inline.get("unitref") == detail["unit_ref"]
            assert inline.get("scale") == detail["xbrl_scale"]
            assert inline.get_text(strip=True) == detail["raw_value"]

    assert review["reported_total_and_nci"][0]["reconciliation"] == "20,642 + 5 = 20,647"
    assert review["reported_total_and_nci"][1]["reconciliation"] == "25,058 + 0 = 25,058"
    assert (
        "Noncontrolling interests | 5"
        in review["reported_total_and_nci"][0]["reported_statement_excerpt"]
    )
    assert (
        "Noncontrolling interests | —"
        in review["reported_total_and_nci"][1]["reported_statement_excerpt"]
    )


def test_extracted_parent_row_fragments_match_the_reviewed_facts() -> None:
    review = _review()
    rows_by_date = {row["period_end"]: row for row in review["reviewed_parent_equity_rows"]}
    for date, fact_id in [("2022-08-28", "f-161"), ("2023-09-03", "f-160")]:
        row = rows_by_date[date]
        soup = BeautifulSoup(row["locator_detail"]["raw_html_row"], "html.parser")
        visible = " | ".join(soup.stripped_strings)
        assert visible == row["row_visible_text"]
        fact = soup.find(attrs={"id": fact_id})
        assert fact is not None
        assert fact.get("name") == "us-gaap:StockholdersEquity"
        assert fact.get("contextref") == row["inline_xbrl"]["context_ref"]
        assert fact.get("unitref") == "usd"
        assert fact.get("scale") == "6"
        assert fact.get_text(strip=True) == row["value"]


def test_mixed_or_unknown_scope_and_period_are_rejected() -> None:
    review = _review()
    bad_scope = json.loads(json.dumps(review))
    bad_scope["reviewed_parent_equity_rows"][0]["inline_xbrl"]["name"] = (
        "us-gaap:StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"
    )
    with pytest.raises(AssertionError):
        _validate_parent_rows(bad_scope)

    bad_period = json.loads(json.dumps(review))
    bad_period["reviewed_parent_equity_rows"][0]["period_end"] = "2023-09-03"
    with pytest.raises(AssertionError):
        _validate_parent_rows(bad_period)


def test_original_total_equity_values_and_tags_remain_in_history_review() -> None:
    history = json.loads(HISTORY_PATH.read_text())
    original = {
        row["year"]: row
        for row in history
        if row.get("company_key") == "costco"
        and row.get("metric") == "equity_parent"
        and row.get("category") in {"equity", "beginning_equity"}
        and row["year"] in {2022, 2023}
    }
    assert original[2022]["value"] == "20,647"
    assert original[2023]["value"] == "25,058"
    total_tag = "us-gaap:StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"
    assert original[2022]["inline_xbrl"]["name"] == total_tag
    assert original[2023]["inline_xbrl"]["name"] == total_tag


@pytest.mark.skipif(
    not (ROOT / "data/hk2-latest-evidence/2026-10-15/raw/costco-equity/cost-20230903.htm").exists(),
    reason="full captured SEC filing is an ignored artifact",
)
def test_reviewed_fragments_are_byte_exact_rows_of_the_hashed_official_capture() -> None:
    review = _review()
    body = (ROOT / review["source_capture"]["raw_path"]).read_bytes()
    assert hashlib.sha256(body).hexdigest() == review["source_capture"]["sha256"]
    source = body.decode("utf-8")
    for row in review["reviewed_parent_equity_rows"]:
        assert row["locator_detail"]["raw_html_row"] in source
    for period in review["reported_total_and_nci"]:
        for detail in period["source_fact_details"]:
            assert detail["raw_html_row"] in source
