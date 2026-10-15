"""Source-bound tests for derived opening-equity facts and conflict closure."""

from __future__ import annotations

import copy
import json
import unittest
from datetime import date

from investment_agent.materials.annual_validation import (
    _HSBC_RECONCILIATION_COLUMNS,
    _check_fact,
    _validate_conflict_resolutions,
    _validate_equity_reconciliations,
)
from investment_agent.materials.contracts import (
    AccountingStandard,
    DatePrecision,
    EquityScope,
    FactPeriod,
    FactProvenance,
    FactRecord,
    FactSemantics,
    FactValue,
    PeriodBasis,
    PerShareBasis,
    ReportingScope,
    Verification,
    fact_to_payload,
)
from investment_agent.materials.equity_reconciliation import evaluate_equity_reconciliation

REPORT_URL = "https://www.hsbc.com/annual-report-2023.pdf"
REPORT_HASH = "a" * 64
INDEX_HASH = "b" * 64
REPORT_ID = "hsbc:annual-report-2023"
INDEX_ID = "hsbc:official-index-2023"
PUBLISHED_ON = "2024-02-21"
AS_OF = date(2026, 10, 15)
PUBLICATION_EXCERPT = (
    "21 Feb 2024 Annual Annual Results 2023 documents 24 items "
    "Annual Report and Accounts 2023 PDF 7MB 450 pages"
)


def _display_value(value: str) -> str:
    if value.startswith("-"):
        return f"({int(value[1:]):,})"
    return f"{int(value):,}"


def _valid_record_and_documents() -> tuple[dict, dict[str, dict]]:
    pages = [""] * 427
    inputs = []
    values_by_id = {
        "old_parent": "198250",
        "old_instruments": "22414",
        "old_ordinary": "175836",
        "new_parent": "189015",
        "new_instruments": "22414",
        "adjustment_1": "92738",
        "adjustment_2": "4558",
        "adjustment_3": "-99631",
        "adjustment_4": "-8847",
        "adjustment_5": "1947",
    }
    basis_by_id = {
        "old_parent": "ifrs4",
        "old_instruments": "ifrs4",
        "old_ordinary": "ifrs4",
        "new_parent": "ifrs17",
        "new_instruments": "ifrs17",
        **{f"adjustment_{i}": "transition" for i in range(1, 6)},
    }
    excerpts_by_id: dict[str, str] = {}
    for input_id, value in values_by_id.items():
        spec = _HSBC_RECONCILIATION_COLUMNS[input_id]
        excerpt = spec["label"] + " " + " ".join(_display_value(token) for token in spec["values"])
        excerpts_by_id[input_id] = excerpt
        page = spec["page"]
        if excerpt not in pages[page - 1]:
            pages[page - 1] += (" " if pages[page - 1] else "") + excerpt
        inputs.append(
            {
                "id": input_id,
                "value": value,
                "basis": basis_by_id[input_id],
                "scale": 1_000_000,
                "currency": "USD",
                "period_end": "2021-12-31",
                "source_ref": {
                    "document_id": REPORT_ID,
                    "sha256": REPORT_HASH,
                    "locator": {
                        "kind": "pdf",
                        "page": page,
                        "row": spec["label"],
                        "column_index": spec["index"],
                        "column_values": list(spec["values"]),
                        "column_headers": list(spec["headers"]),
                        "selected_column": spec["headers"][spec["index"]],
                    },
                    "excerpt": excerpt,
                    "source_label": spec["label"],
                    "published_on": PUBLISHED_ON,
                },
            }
        )
    # The transition-row citations share a fuller excerpt than their individual row values.
    transition_excerpt = excerpts_by_id["adjustment_1"]
    for item in inputs:
        if item["id"].startswith("adjustment_"):
            item["source_ref"]["excerpt"] = transition_excerpt

    evaluation = evaluate_equity_reconciliation(
        {"method_version": "hsbc.ifrs17.ordinary_opening.v1", "inputs": inputs}
    )
    record = {
        key: evaluation[key] for key in ("method_version", "inputs", "output", "calculation_ref")
    }
    publication_excerpt = PUBLICATION_EXCERPT
    entry = {
        "company_key": "hsbc-holdings",
        "reconciliation_publication_evidence": {
            "document_id": INDEX_ID,
            "locator": {"kind": "text", "anchor": publication_excerpt},
            "excerpt": publication_excerpt,
            "published_on": PUBLISHED_ON,
            "link_url": REPORT_URL,
        },
        "equity_reconciliations": [record],
    }
    documents = {
        REPORT_ID: {
            "id": REPORT_ID,
            "role": "issuer_annual",
            "url": REPORT_URL,
            "sha256": REPORT_HASH,
            "published_on": PUBLISHED_ON,
            "_valid": True,
            "_text": "\f".join(pages),
        },
        INDEX_ID: {
            "id": INDEX_ID,
            "role": "official_index",
            "url": "https://www.hsbc.com/investors/results-and-announcements/all-reporting/group",
            "sha256": INDEX_HASH,
            "published_on": PUBLISHED_ON,
            "_valid": True,
            "_text": publication_excerpt,
            "_links": ["/annual-report-2023.pdf"],
        },
    }
    return entry, documents


def _revalidated_record(record: dict) -> dict:
    value = evaluate_equity_reconciliation(
        {"method_version": record["method_version"], "inputs": record["inputs"]}
    )
    return {key: value[key] for key in ("method_version", "inputs", "output", "calculation_ref")}


def _derived_field(calculation: dict) -> dict:
    output = calculation["output"]
    ref = calculation["calculation_ref"]
    fact = FactRecord(
        metric="equity_ordinary",
        period=FactPeriod(
            start=None,
            end=date(2021, 12, 31),
            basis=PeriodBasis.INSTANT,
            label="FY2021",
        ),
        semantics=FactSemantics(
            metric="equity_ordinary",
            source_label="Derived IFRS 17 ordinary opening equity",
            source_field="IFRS 17 parent equity less other equity instruments",
            accounting_standard=AccountingStandard.IFRS,
            reporting_scope=ReportingScope.CONSOLIDATED,
            equity_scope=EquityScope.ORDINARY,
            per_share_basis=PerShareBasis.NA,
        ),
        value=FactValue(
            raw_value=output["value"],
            raw_unit="USD million",
            scale=output["scale"],
            unit="USD",
            currency=output["currency"],
            sign_convention="reported",
        ),
        provenance=FactProvenance(
            source="material.equity_reconciliation",
            accession=None,
            original_hash=calculation["input_hash"],
            source_published_at=None,
            source_published_date=date(2024, 2, 21),
            source_published_precision=DatePrecision.DATE,
            accepted_at=None,
            source_available_at=None,
        ),
        verification=Verification.VERIFIED,
        conflict="none",
        applicability="applicable",
        original_locator=json.dumps(
            {"kind": "calculation", "ref": ref}, sort_keys=True, separators=(",", ":")
        ),
    )
    return {
        "category": "equity",
        "fact": fact_to_payload(fact),
        "calculation_ref": ref,
        "evidence": {
            "kind": "derived_calculation",
            "calculation_ref": ref,
            "availability_basis": (
                "Latest publication date of the cited calculation inputs; not a directly disclosed amount."
            ),
        },
    }


class AnnualEquityReconciliationValidationTests(unittest.TestCase):
    def test_original_index_link_matches_only_the_known_report_download_flag(self) -> None:
        entry, documents = _valid_record_and_documents()
        report = (
            "https://www.hsbc.com/-/files/hsbc/investors/hsbc-results/2023/annual/"
            "pdfs/hsbc-holdings-plc/240226-annual-report-and-accounts-2023.pdf"
        )
        documents[REPORT_ID]["url"] = report + "?download=1"
        documents[INDEX_ID]["_links"] = [report]
        entry["reconciliation_publication_evidence"]["link_url"] = report
        calculations, issues = _validate_equity_reconciliations(entry, documents, "USD", AS_OF)
        self.assertEqual(issues, [])
        self.assertTrue(calculations)
        for invalid in (report + "?download=0", report + "?other=1", report + ".other?download=1"):
            documents[REPORT_ID]["url"] = invalid
            calculations, issues = _validate_equity_reconciliations(entry, documents, "USD", AS_OF)
            self.assertEqual(calculations, {})
            self.assertTrue(any("link does not resolve" in issue for issue in issues))

    def test_valid_reconciliation_is_source_bound_and_derives_the_opening_fact(self) -> None:
        entry, documents = _valid_record_and_documents()
        calculations, issues = _validate_equity_reconciliations(entry, documents, "USD", AS_OF)

        self.assertEqual(issues, [])
        ref = entry["equity_reconciliations"][0]["calculation_ref"]
        self.assertIn(ref, calculations)
        self.assertEqual(calculations[ref]["output"]["value"], "166601")
        fact_result = _check_fact(
            _derived_field(calculations[ref]),
            documents,
            "USD",
            AS_OF,
            where="opening_equity",
            calculations=calculations,
        )
        self.assertTrue(fact_result["ok"], fact_result["issues"])

    def test_pdf_layout_whitespace_does_not_break_exact_row_evidence(self) -> None:
        entry, documents = _valid_record_and_documents()
        documents = copy.deepcopy(documents)
        documents[REPORT_ID]["_text"] = documents[REPORT_ID]["_text"].replace(
            "Total shareholders‘ equity", "Total  shareholders‘  equity"
        )

        calculations, issues = _validate_equity_reconciliations(entry, documents, "USD", AS_OF)

        self.assertEqual(issues, [])
        self.assertEqual(next(iter(calculations.values()))["output"]["value"], "166601")

    def test_capture_hash_tampering_rejects_the_reconciliation(self) -> None:
        entry, documents = _valid_record_and_documents()
        entry = copy.deepcopy(entry)
        entry["equity_reconciliations"][0]["inputs"][0]["source_ref"]["sha256"] = "c" * 64
        entry["equity_reconciliations"][0] = _revalidated_record(entry["equity_reconciliations"][0])

        calculations, issues = _validate_equity_reconciliations(entry, documents, "USD", AS_OF)

        self.assertEqual(calculations, {})
        self.assertTrue(any("sha256 does not match" in issue for issue in issues), issues)

    def test_source_body_must_contain_the_cited_row_excerpt(self) -> None:
        entry, documents = _valid_record_and_documents()
        documents = copy.deepcopy(documents)
        documents[REPORT_ID]["_text"] = documents[REPORT_ID]["_text"].replace(
            "Total shareholders‘ equity", "Total parent equity", 1
        )

        calculations, issues = _validate_equity_reconciliations(entry, documents, "USD", AS_OF)

        self.assertEqual(calculations, {})
        self.assertTrue(
            any("does not resolve" in issue or "does not occur" in issue for issue in issues),
            issues,
        )

    def test_input_amount_tampering_fails_audited_arithmetic(self) -> None:
        entry, documents = _valid_record_and_documents()
        entry = copy.deepcopy(entry)
        inputs = entry["equity_reconciliations"][0]["inputs"]
        next(item for item in inputs if item["id"] == "old_ordinary")["value"] = "175837"

        calculations, issues = _validate_equity_reconciliations(entry, documents, "USD", AS_OF)

        self.assertEqual(calculations, {})
        self.assertTrue(any("old ordinary equity" in issue for issue in issues), issues)

    def test_wrong_column_is_rejected_even_when_calculation_hash_is_recomputed(self) -> None:
        entry, documents = _valid_record_and_documents()
        entry = copy.deepcopy(entry)
        inputs = entry["equity_reconciliations"][0]["inputs"]
        item = next(row for row in inputs if row["id"] == "new_parent")
        item["source_ref"]["locator"]["column_index"] = 3
        item["source_ref"]["locator"]["selected_column"] = "ifrs4:2022-01-01"
        entry["equity_reconciliations"][0] = _revalidated_record(entry["equity_reconciliations"][0])

        calculations, issues = _validate_equity_reconciliations(entry, documents, "USD", AS_OF)

        self.assertEqual(calculations, {})
        self.assertTrue(any("wrong audited row or column" in issue for issue in issues), issues)

    def test_unknown_calculation_reference_cannot_back_a_derived_fact(self) -> None:
        entry, documents = _valid_record_and_documents()
        calculations, issues = _validate_equity_reconciliations(entry, documents, "USD", AS_OF)
        self.assertEqual(issues, [])
        valid = next(iter(calculations.values()))
        field = _derived_field(valid)
        field["calculation_ref"] = "sha256:" + "0" * 64
        result = _check_fact(
            field,
            documents,
            "USD",
            AS_OF,
            where="opening_equity",
            calculations=calculations,
        )

        self.assertFalse(result["ok"])
        self.assertTrue(
            any(
                "does not identify a validated reconciliation" in issue
                for issue in result["issues"]
            )
        )

    def test_resolved_conflict_requires_exact_beginning_fact_and_superseded_fact(self) -> None:
        entry, documents = _valid_record_and_documents()
        calculations, issues = _validate_equity_reconciliations(entry, documents, "USD", AS_OF)
        self.assertEqual(issues, [])
        ref = next(iter(calculations))
        entry["conflicts"] = [
            {
                "kind": "ifrs_17_restatement",
                "status": "resolved",
                "resolution_ref": ref,
            }
        ]
        beginning = _derived_field(calculations[ref])
        fact_result = _check_fact(
            beginning,
            documents,
            "USD",
            AS_OF,
            where="opening_equity",
            calculations=calculations,
        )
        fact = fact_result["fact"]

        unresolved, conflict_issues, _ = _validate_conflict_resolutions(
            entry,
            calculations,
            set(),
            {ref: (date(2022, 12, 31), fact, fact_result["ok"])},
            {},
        )

        self.assertTrue(unresolved)
        self.assertTrue(any("not backed" in issue for issue in conflict_issues))


if __name__ == "__main__":
    unittest.main()
