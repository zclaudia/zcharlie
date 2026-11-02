"""HSBC equity-reconciliation checks for the annual evidence package.

Column labels, page numbers, and amounts are the checked golden sample.
Do not edit them when moving this module.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import date
from typing import Any
from urllib.parse import urljoin

from investment_agent.materials.annual_validation import (
    _check_fact,
    _date,
    _located_context,
    _numeric_token,
)
from investment_agent.materials.contracts import FactRecord, PeriodBasis

_HSBC_RECONCILIATION_COLUMNS: dict[str, dict[str, Any]] = {
    "old_parent": {
        "page": 427,
        "label": "Total shareholders‘ equity",
        "values": ["177833", "189015", "187484", "198250"],
        "headers": [
            "ifrs17:2022-12-31",
            "ifrs17:2022-01-01",
            "ifrs4:2022-12-31",
            "ifrs4:2022-01-01",
        ],
        "index": 3,
    },
    "new_parent": {
        "page": 427,
        "label": "Total shareholders‘ equity",
        "values": ["177833", "189015", "187484", "198250"],
        "headers": [
            "ifrs17:2022-12-31",
            "ifrs17:2022-01-01",
            "ifrs4:2022-12-31",
            "ifrs4:2022-01-01",
        ],
        "index": 1,
    },
    "old_instruments": {
        "page": 427,
        "label": "Other equity instruments",
        "values": ["19746", "22414", "19746", "22414"],
        "headers": [
            "ifrs17:2022-12-31",
            "ifrs17:2022-01-01",
            "ifrs4:2022-12-31",
            "ifrs4:2022-01-01",
        ],
        "index": 3,
    },
    "new_instruments": {
        "page": 427,
        "label": "Other equity instruments",
        "values": ["19746", "22414", "19746", "22414"],
        "headers": [
            "ifrs17:2022-12-31",
            "ifrs17:2022-01-01",
            "ifrs4:2022-12-31",
            "ifrs4:2022-01-01",
        ],
        "index": 1,
    },
    "old_ordinary": {
        "page": 135,
        "label": "Total ordinary shareholders’ equity",
        "values": ["167610", "158087", "175836"],
        "headers": ["2023-12-31", "2022-12-31", "2021-12-31"],
        "index": 2,
    },
    "adjustment_1": {
        "page": 424,
        "label": "Total shareholders’ equity",
        "values": ["198250", "92738", "4558", "-99631", "-8847", "1947", "189015", "-9235"],
        "headers": [
            "ifrs4",
            "removal",
            "remeasurement",
            "fulfilment",
            "csm",
            "tax",
            "ifrs17",
            "movement",
        ],
        "index": 1,
    },
    "adjustment_2": {
        "page": 424,
        "label": "Total shareholders’ equity",
        "values": ["198250", "92738", "4558", "-99631", "-8847", "1947", "189015", "-9235"],
        "headers": [
            "ifrs4",
            "removal",
            "remeasurement",
            "fulfilment",
            "csm",
            "tax",
            "ifrs17",
            "movement",
        ],
        "index": 2,
    },
    "adjustment_3": {
        "page": 424,
        "label": "Total shareholders’ equity",
        "values": ["198250", "92738", "4558", "-99631", "-8847", "1947", "189015", "-9235"],
        "headers": [
            "ifrs4",
            "removal",
            "remeasurement",
            "fulfilment",
            "csm",
            "tax",
            "ifrs17",
            "movement",
        ],
        "index": 3,
    },
    "adjustment_4": {
        "page": 424,
        "label": "Total shareholders’ equity",
        "values": ["198250", "92738", "4558", "-99631", "-8847", "1947", "189015", "-9235"],
        "headers": [
            "ifrs4",
            "removal",
            "remeasurement",
            "fulfilment",
            "csm",
            "tax",
            "ifrs17",
            "movement",
        ],
        "index": 4,
    },
    "adjustment_5": {
        "page": 424,
        "label": "Total shareholders’ equity",
        "values": ["198250", "92738", "4558", "-99631", "-8847", "1947", "189015", "-9235"],
        "headers": [
            "ifrs4",
            "removal",
            "remeasurement",
            "fulfilment",
            "csm",
            "tax",
            "ifrs17",
            "movement",
        ],
        "index": 5,
    },
}



def _validate_hsbc_reconciliation_sources(
    entry: Mapping[str, Any],
    evaluation: Mapping[str, Any],
    documents: Mapping[str, Mapping[str, Any]],
    as_of: date,
    where: str,
) -> list[str]:
    issues: list[str] = []
    input_rows = evaluation.get("inputs")
    if not isinstance(input_rows, list):
        return [f"{where}.inputs are malformed after calculation validation"]
    report_documents: set[str] = set()
    for item in input_rows:
        input_id = item["id"]
        source_ref = item["source_ref"]
        source_where = f"{where}.{input_id}.source_ref"
        doc_id = source_ref["document_id"]
        doc = documents.get(doc_id)
        expected = _HSBC_RECONCILIATION_COLUMNS[input_id]
        if not isinstance(doc, Mapping) or doc.get("_valid") is not True:
            issues.append(f"{source_where} references a missing or invalid source document")
            continue
        report_documents.add(doc_id)
        if doc.get("role") not in {"issuer_annual", "sec_filing"}:
            issues.append(f"{source_where} must cite an issuer annual report or SEC filing")
        if doc.get("sha256") != source_ref["sha256"]:
            issues.append(f"{source_where}.sha256 does not match the captured document")
        published = _date(source_ref.get("published_on"))
        if published is None or published > as_of:
            issues.append(f"{source_where}.published_on is missing, invalid, or after as_of")
        if doc.get("published_on") != source_ref.get("published_on"):
            issues.append(f"{source_where}.published_on disagrees with source document metadata")
        if source_ref.get("published_on") != "2024-02-21":
            issues.append(
                f"{source_where}.published_on must be the documented report date 2024-02-21"
            )
        if item["scale"] != 1_000_000 or item["currency"] != "USD":
            issues.append(f"{where}.{input_id} must retain USD million units")
        locator = source_ref.get("locator")
        if not isinstance(locator, Mapping):
            issues.append(f"{source_where}.locator must be a structured PDF row citation")
            continue
        expected_locator_keys = {
            "kind",
            "page",
            "row",
            "column_index",
            "column_values",
            "column_headers",
            "selected_column",
        }
        if set(locator) != expected_locator_keys or locator.get("kind") != "pdf":
            issues.append(f"{source_where}.locator must include exact page, row, and column proof")
            continue
        if (
            locator.get("page") != expected["page"]
            or locator.get("row") != expected["label"]
            or source_ref.get("source_label") != expected["label"]
            or locator.get("column_index") != expected["index"]
            or locator.get("column_values") != expected["values"]
            or locator.get("column_headers") != expected["headers"]
            or locator.get("selected_column") != expected["headers"][expected["index"]]
        ):
            issues.append(f"{source_where}.locator selects the wrong audited row or column")
        if not isinstance(locator.get("page"), int) or isinstance(locator.get("page"), bool):
            issues.append(f"{source_where}.locator.page must be an integer")
            continue
        context = _located_context(doc, locator)
        excerpt = source_ref.get("excerpt")
        source_label = source_ref.get("source_label")
        if context is None:
            issues.append(f"{source_where}.locator does not resolve to a source PDF page and row")
            continue
        if (
            not isinstance(excerpt, str)
            or not excerpt.strip()
            or _normalize_source_whitespace(excerpt) not in _normalize_source_whitespace(context)
        ):
            issues.append(f"{source_where}.excerpt does not occur in the located source page")
            continue
        if not isinstance(source_label, str) or source_label not in excerpt:
            issues.append(f"{source_where}.source_label is not present in its excerpt")
        try:
            parsed_values = _numeric_row_tokens(excerpt, str(source_label))
        except ValueError:
            parsed_values = []
        if parsed_values != expected["values"]:
            issues.append(f"{source_where}.excerpt does not prove the published row values")
        if item["value"] != expected["values"][expected["index"]]:
            issues.append(f"{where}.{input_id}.value does not match the selected source column")
    if len(report_documents) != 1:
        issues.append(f"{where} inputs must cite one consistent original issuer report")
    publication_issues = _validate_reconciliation_publication_evidence(
        entry, documents, report_documents, as_of, where
    )
    issues.extend(publication_issues)
    return issues


def _numeric_row_tokens(excerpt: str, label: str) -> list[str]:
    offset = excerpt.find(label)
    if offset < 0:
        raise ValueError("source row label is absent")
    row = excerpt[offset + len(label) :]
    tokens = re.findall(
        r"\([0-9][0-9,_]*(?:\.[0-9]+)?\)|[+-]?[0-9][0-9,_]*(?:\.[0-9]+)?",
        row,
    )
    normalized: list[str] = []
    for token in tokens:
        parenthesized = token.startswith("(") and token.endswith(")")
        number = _numeric_token(token[1:-1] if parenthesized else token)
        normalized.append(f"-{number}" if parenthesized else number)
    return normalized


def _normalize_source_whitespace(value: str) -> str:
    """Collapse layout whitespace without changing source words or values."""
    return re.sub(r"\s+", " ", value).strip()


def _validate_reconciliation_publication_evidence(
    entry: Mapping[str, Any],
    documents: Mapping[str, Mapping[str, Any]],
    report_documents: set[str],
    as_of: date,
    where: str,
) -> list[str]:
    issues: list[str] = []
    evidence = entry.get("reconciliation_publication_evidence")
    if not isinstance(evidence, Mapping):
        return [f"{where} requires original publication-index evidence"]
    doc_id = evidence.get("document_id")
    index = documents.get(doc_id) if isinstance(doc_id, str) else None
    if not isinstance(index, Mapping) or index.get("_valid") is not True:
        return [f"{where} publication evidence references a missing or invalid document"]
    if index.get("role") != "official_index":
        issues.append(f"{where} publication evidence must cite the official issuer reporting index")
    published = _date(evidence.get("published_on"))
    if published is None or published > as_of:
        issues.append(f"{where} publication evidence date is invalid or after as_of")
    if evidence.get("published_on") != index.get("published_on"):
        issues.append(f"{where} publication evidence date disagrees with index metadata")
    if evidence.get("published_on") != "2024-02-21":
        issues.append(f"{where} publication date must be proven as 2024-02-21")
    locator = evidence.get("locator")
    context = _located_context(index, locator)
    excerpt = evidence.get("excerpt")
    if (
        context is None
        or not isinstance(excerpt, str)
        or _normalize_source_whitespace(excerpt) not in _normalize_source_whitespace(context)
        or "21 Feb 2024" not in excerpt
        or "Annual Results 2023" not in excerpt
        or "Annual Report and Accounts 2023" not in excerpt
    ):
        issues.append(f"{where} publication-index excerpt does not prove the report date and title")
    report_url = evidence.get("link_url")
    links = index.get("_links", [])
    if not isinstance(report_url, str) or not isinstance(links, list):
        issues.append(f"{where} publication evidence must link to the captured annual report")
    else:
        linked_urls = {
            urljoin(str(index.get("url", "")), link) for link in links if isinstance(link, str)
        }
        target_docs = [documents.get(doc_id) for doc_id in report_documents]
        if not any(
            isinstance(doc, Mapping)
            and _same_hsbc_report_download(doc.get("url"), report_url)
            and report_url in linked_urls
            for doc in target_docs
        ):
            issues.append(f"{where} publication-index link does not resolve to the cited report")
    if report_documents and evidence.get("published_on") != "2024-02-21":
        issues.append(f"{where} report publication date does not match its source evidence")
    return issues


def _same_hsbc_report_download(captured_url: object, indexed_url: str) -> bool:
    """Preserve original URLs while matching this report's download flag only."""
    if captured_url == indexed_url:
        return True
    report = (
        "https://www.hsbc.com/-/files/hsbc/investors/hsbc-results/2023/annual/"
        "pdfs/hsbc-holdings-plc/240226-annual-report-and-accounts-2023.pdf"
    )
    return indexed_url == report and captured_url == report + "?download=1"


def _validate_reconciliation_superseded_facts(
    entry: Mapping[str, Any],
    calculations: Mapping[str, Mapping[str, Any]],
    documents: Mapping[str, Mapping[str, Any]],
    report_currency: str | None,
    as_of: date,
) -> tuple[set[str], list[str]]:
    if not calculations:
        return set(), []
    issues: list[str] = []
    raw_fields = entry.get("superseded_facts")
    if not isinstance(raw_fields, list):
        return set(), [
            "HSBC equity reconciliation requires superseded_facts retaining IFRS 4 equity"
        ]
    checked: list[tuple[Mapping[str, Any], FactRecord, bool]] = []
    for index, field in enumerate(raw_fields):
        result = _check_fact(
            field,
            documents,
            report_currency,
            as_of,
            where=f"superseded_facts[{index}]",
        )
        issues.extend(result["issues"])
        fact = result.get("fact")
        if isinstance(field, Mapping) and isinstance(fact, FactRecord):
            checked.append((field, fact, result["ok"]))
    accepted: set[str] = set()
    for calculation_ref, evaluation in calculations.items():
        old_input = next(
            (item for item in evaluation["inputs"] if item["id"] == "old_ordinary"), None
        )
        if old_input is None:
            issues.append(f"{calculation_ref} has no original IFRS 4 ordinary-equity input")
            continue
        matches: list[bool] = []
        for field, fact, valid in checked:
            matching = (
                valid
                and field.get("category") == "equity"
                and fact.metric == "equity_ordinary"
                and fact.value.raw_value == old_input["value"]
                and fact.value.scale == old_input["scale"]
                and fact.value.currency == old_input["currency"]
                and fact.period.end.isoformat() == old_input["period_end"]
                and fact.period.basis == PeriodBasis.INSTANT
                and fact.semantics.accounting_standard.value == "ifrs"
                and fact.semantics.equity_scope.value == "ordinary_shareholders"
            )
            if matching:
                matches.append(True)
        if len(matches) == 1:
            accepted.add(calculation_ref)
        else:
            issues.append(
                f"{calculation_ref} requires exactly one valid superseded IFRS 4 ordinary-equity fact matching its source input"
            )
    return accepted, issues
