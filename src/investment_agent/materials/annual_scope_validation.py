"""Source-layer validation for reviewed annual equity-scope resolutions.

This module binds a pure scope review to an already captured and hashed source
document, verifies each cited row in extracted source text, and checks both the
superseded total-equity facts and currently selected parent-equity facts.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import date, datetime
from typing import Any
from urllib.parse import urlparse

from investment_agent.materials.equity_scope_review import evaluate_equity_scope_review

_PARENT_AMOUNTS = {"2022-08-28": "20642", "2023-09-03": "25058"}
_TOTAL_AMOUNTS = {"2022-08-28": "20647", "2023-09-03": "25058"}
_PARENT_SOURCE_LABEL = "Total Costco stockholders’ equity"


def validate_scope_reviews(
    entry: Mapping[str, Any],
    documents: Mapping[str, Mapping[str, Any]],
    report_currency: str | None,
    as_of: date,
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Validate each scope review against source documents and selected facts.

    Entries without an ``equity_scope_reviews`` key have no scope review to
    validate and return an empty result. Invalid reviews return no accepted
    mapping and one or more actionable issues.
    """

    if "equity_scope_reviews" not in entry:
        return {}, []

    # Late import avoids coupling the annual package's main validator to this
    # optional scope-resolution layer during module initialization.
    from investment_agent.materials.annual_validation import (  # noqa: PLC0415
        _check_fact,
        _located_context,
    )

    issues: list[str] = []
    accepted: dict[str, dict[str, Any]] = {}
    raw_reviews = entry.get("equity_scope_reviews")
    if not isinstance(raw_reviews, (list, tuple)) or not raw_reviews:
        return {}, ["equity_scope_reviews must be a non-empty list"]
    if report_currency != "USD":
        issues.append("equity_scope_reviews requires report_currency USD")
    if isinstance(as_of, datetime) or not isinstance(as_of, date):
        return {}, issues + ["equity_scope_reviews as_of must be a date"]

    seen_ids: set[str] = set()
    for review_index, raw_review in enumerate(raw_reviews):
        where = f"equity_scope_reviews[{review_index}]"
        if not isinstance(raw_review, Mapping):
            issues.append(f"{where} must be an object")
            continue
        review_id = raw_review.get("id")
        if not isinstance(review_id, str) or not review_id:
            issues.append(f"{where}.id must be non-empty text")
            continue
        if review_id in seen_ids:
            issues.append(f"duplicate equity scope review id: {review_id}")
            continue
        seen_ids.add(review_id)

        review_issues: list[str] = []
        try:
            evaluation = evaluate_equity_scope_review(raw_review)
        except (ValueError, TypeError, KeyError) as exc:
            issues.append(f"{where} is invalid: {exc}")
            continue

        inputs = evaluation["inputs"]
        if inputs["currency"] != report_currency:
            review_issues.append(f"{where}.currency does not match report_currency")
        source_ref = inputs["source_ref"]
        source_id = source_ref["document_id"]
        document = documents.get(source_id)
        if not isinstance(document, Mapping):
            review_issues.append(f"{where} references missing source document {source_id!r}")
        else:
            review_issues.extend(_validate_document(document, source_id, source_ref, as_of, where))
            for period in inputs["periods"]:
                for component_name in ("parent", "nci", "total"):
                    component_ref = period[component_name]["source_ref"]
                    locator = _locator_with_normalized_anchor(component_ref.get("locator"))
                    context = _located_context(document, locator)
                    component_where = f"{where}.{period['date']}.{component_name}.source_ref"
                    if context is None:
                        review_issues.append(
                            f"{component_where}.locator does not resolve in source text"
                        )
                        continue
                    excerpt = component_ref["excerpt"]
                    if not _includes_source_excerpt(context, excerpt):
                        review_issues.append(
                            f"{component_where}.excerpt does not match located source context"
                        )

        review_issues.extend(
            _validate_superseded_facts(
                entry,
                documents,
                report_currency,
                as_of,
                source_ref["sha256"],
                inputs["periods"],
                _check_fact,
                where,
            )
        )
        review_issues.extend(
            _validate_selected_facts(
                entry,
                documents,
                report_currency,
                as_of,
                source_id,
                inputs["periods"],
                _check_fact,
                where,
            )
        )
        if review_issues:
            issues.extend(review_issues)
        else:
            accepted[review_id] = evaluation

    if issues:
        return {}, issues
    return accepted, []


def _validate_document(
    document: Mapping[str, Any],
    source_id: str,
    source_ref: Mapping[str, Any],
    as_of: date,
    where: str,
) -> list[str]:
    issues: list[str] = []
    if document.get("id") != source_id:
        issues.append(f"{where} source document id does not match its document map key")
    if document.get("_valid") is not True:
        issues.append(f"{where} source document is invalid")
    if document.get("role") != "sec_filing":
        issues.append(f"{where} source document role must be sec_filing")
    url = document.get("url")
    parsed = urlparse(url) if isinstance(url, str) else None
    if (
        parsed is None
        or parsed.scheme != "https"
        or parsed.hostname not in {"sec.gov", "www.sec.gov"}
        or url != source_ref["url"]
    ):
        issues.append(f"{where} source document URL must be the reviewed SEC filing URL")
    if document.get("sha256") != source_ref["sha256"]:
        issues.append(f"{where} source document SHA-256 does not match the review")
    if document.get("published_on") != source_ref["published_on"]:
        issues.append(f"{where} source document publication date does not match the review")
    published = _parse_date(document.get("published_on"))
    if published is None:
        issues.append(f"{where} source document published_on must be an ISO date")
    elif published > as_of:
        issues.append(f"{where} source document publication date is after as_of")
    if not isinstance(document.get("_text"), str):
        issues.append(f"{where} source document has no extracted text")
    return issues


def _validate_superseded_facts(
    entry: Mapping[str, Any],
    documents: Mapping[str, Mapping[str, Any]],
    report_currency: str | None,
    as_of: date,
    source_hash: str,
    periods: list[Mapping[str, Any]],
    check_fact: Any,
    where: str,
) -> list[str]:
    issues: list[str] = []
    raw_superseded = entry.get("superseded_facts")
    if not isinstance(raw_superseded, (list, tuple)):
        return [f"{where} requires superseded_facts retaining the former total-equity inputs"]

    expected_amounts = {period["date"]: str(period["total"]["amount"]) for period in periods}
    found: dict[str, int] = {}
    for index, raw_field in enumerate(raw_superseded):
        field = _equity_field(raw_field)
        result = check_fact(
            field,
            documents,
            report_currency,
            as_of,
            where=f"{where}.superseded_facts[{index}]",
        )
        issues.extend(result["issues"])
        fact = result.get("fact")
        if fact is None or field is None:
            continue
        end = fact.period.end.isoformat()
        if end not in expected_amounts:
            continue
        found[end] = found.get(end, 0) + 1
        if fact.metric != "total_equity":
            issues.append(f"{where}.superseded_facts[{index}] must retain total_equity metric")
        if fact.semantics.equity_scope.value != "total":
            issues.append(f"{where}.superseded_facts[{index}] must retain total equity scope")
        if fact.value.raw_value != expected_amounts[end]:
            issues.append(
                f"{where}.superseded_facts[{index}] must retain the reported total for {end}"
            )
        if fact.value.scale != 1_000_000 or fact.value.currency != "USD":
            issues.append(f"{where}.superseded_facts[{index}] must retain USD million units")
        if fact.semantics.source_label != "TOTAL EQUITY":
            issues.append(f"{where}.superseded_facts[{index}] must retain the TOTAL EQUITY label")
        evidence = field.get("evidence", {})
        if not isinstance(evidence, Mapping):
            issues.append(
                f"{where}.superseded_facts[{index}] must retain original document evidence"
            )
        else:
            old_doc_id = evidence.get("document_id")
            old_doc = documents.get(old_doc_id) if isinstance(old_doc_id, str) else None
            if not isinstance(old_doc, Mapping) or old_doc.get("sha256") != source_hash:
                issues.append(
                    f"{where}.superseded_facts[{index}] must retain the original filed total-equity source"
                )
    for period_date in expected_amounts:
        if found.get(period_date) != 1:
            issues.append(
                f"{where} requires exactly one superseded total-equity fact at {period_date}"
            )
    return issues


def _validate_selected_facts(
    entry: Mapping[str, Any],
    documents: Mapping[str, Mapping[str, Any]],
    report_currency: str | None,
    as_of: date,
    source_id: str,
    periods: list[Mapping[str, Any]],
    check_fact: Any,
    where: str,
) -> list[str]:
    issues: list[str] = []
    expected_amounts = {period["date"]: str(period["parent"]["amount"]) for period in periods}
    found: dict[str, int] = {period_date: 0 for period_date in expected_amounts}
    years = entry.get("fiscal_years")
    if not isinstance(years, (list, tuple)):
        return [f"{where}.fiscal_years must be a list to verify selected equity facts"]

    selected_fields: list[tuple[str, object]] = []
    for year_index, year in enumerate(years):
        if not isinstance(year, Mapping):
            continue
        fields = year.get("facts", [])
        if isinstance(fields, (list, tuple)):
            selected_fields.extend(
                (f"fiscal_years[{year_index}].facts[{index}]", field)
                for index, field in enumerate(fields)
                if isinstance(field, Mapping) and field.get("category") == "equity"
            )
        beginning = year.get("beginning_equity_fact")
        if beginning is not None:
            selected_fields.append((f"fiscal_years[{year_index}].beginning_equity_fact", beginning))

    for path, raw_field in selected_fields:
        field = _equity_field(raw_field)
        result = check_fact(
            field,
            documents,
            report_currency,
            as_of,
            where=f"{where}.{path}",
        )
        issues.extend(result["issues"])
        fact = result.get("fact")
        if fact is None or field is None:
            continue
        end = fact.period.end.isoformat()
        if end not in expected_amounts:
            continue
        found[end] += 1
        expected_raw = expected_amounts[end]
        if fact.metric != "equity_parent":
            issues.append(f"{where}.{path} must select equity_parent at {end}")
        if fact.semantics.equity_scope.value != "all_parent_equity_holders":
            issues.append(f"{where}.{path} must select all_parent equity at {end}")
        if fact.period.start is not None:
            issues.append(f"{where}.{path} must be an instant fact at {end}")
        if fact.period.basis.value != "instant":
            issues.append(f"{where}.{path} must use instant period basis at {end}")
        if fact.value.raw_value != expected_raw:
            issues.append(f"{where}.{path} value must match reviewed parent equity at {end}")
        if (
            fact.value.scale != 1_000_000
            or fact.value.currency != "USD"
            or fact.value.raw_unit != "USD million"
        ):
            issues.append(f"{where}.{path} must retain the reviewed USD million units")
        if fact.semantics.source_label != _PARENT_SOURCE_LABEL:
            issues.append(f"{where}.{path} must preserve the parent-equity source label")
        selected_doc = documents.get(source_id)
        selected_hash = selected_doc.get("sha256") if isinstance(selected_doc, Mapping) else None
        if fact.provenance.original_hash != selected_hash:
            issues.append(f"{where}.{path} must cite the reviewed parent-equity source hash")
        evidence = field.get("evidence", {})
        if not isinstance(evidence, Mapping) or evidence.get("document_id") != source_id:
            issues.append(f"{where}.{path} must cite the reviewed SEC filing")

    for period_date, count in found.items():
        if count == 0:
            issues.append(f"{where} has no selected parent-equity fact at {period_date}")
    return issues


def _equity_field(value: object) -> Mapping[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    if value.get("category") == "beginning_equity":
        return {**value, "category": "equity"}
    return value


def _locator_with_normalized_anchor(locator: object) -> object:
    if not isinstance(locator, Mapping):
        return locator
    normalized = dict(locator)
    if normalized.get("kind") == "text" and isinstance(normalized.get("anchor"), str):
        normalized["anchor"] = _normalize_source_text(normalized["anchor"])
    return normalized


def _includes_source_excerpt(context: str, excerpt: object) -> bool:
    return (
        isinstance(excerpt, str)
        and bool(excerpt.strip())
        and _normalize_source_text(excerpt) in _normalize_source_text(context)
    )


def _normalize_source_text(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"\s*\|\s*", " ", value)).strip()


def _parse_date(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None
