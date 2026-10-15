"""Offline validation of annual evidence packages for the HK/US pilot.

The manifest is an index, not an authority for the facts it describes. Every
document is re-hashed from disk and each reviewed fact is decoded by the frozen
``fact_from_payload`` contract, then tied to a document excerpt and locator.
No network requests are made by this module.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, time, timedelta
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urljoin, urlparse

from investment_agent.domain.security import (
    DEFAULT_PILOT_LIST_PATH,
    SOURCE_SEC_COMPANYFACTS,
    load_pilot_identities,
)
from investment_agent.investor.enums import CalculationStatus
from investment_agent.materials.contracts import (
    CURRENCY_UNITS,
    DatasetDraft,
    DatasetKind,
    FactPeriod,
    FactRecord,
    PeriodBasis,
    SubjectKind,
    Verification,
    fact_from_payload,
    fact_to_payload,
    validate_draft,
)
from investment_agent.materials.contracts import (
    SCHEMA_VERSION as CONTRACT_SCHEMA_VERSION,
)
from investment_agent.materials.equity_reconciliation import (
    evaluate_equity_reconciliation,
)
from investment_agent.materials.metrics import compute_metrics, profile_from_identity

SCHEMA_VERSION = 1
FIELD_CATEGORIES = frozenset(
    {"revenue", "profit", "assets", "equity", "operating_cash_flow", "capex"}
)
_CATEGORY_METRICS: dict[str, frozenset[str]] = {
    "revenue": frozenset({"revenue_operating", "revenue_gross", "revenue_net_credit"}),
    "profit": frozenset({"net_profit", "profit_parent", "profit_ordinary_shareholders"}),
    "assets": frozenset({"total_assets"}),
    "equity": frozenset({"total_equity", "equity_parent", "equity_ordinary"}),
    "operating_cash_flow": frozenset({"operating_cash_flow"}),
    "capex": frozenset({"capex_ppe"}),
}
_ISSUER_HOSTS: dict[str, frozenset[str]] = {
    "clp-holdings": frozenset({"clpgroup.com", "clp.com.cn", "clp.com.hk"}),
    "hsbc-holdings": frozenset({"hsbc.com"}),
    "tencent-holdings": frozenset({"tencent.com"}),
    "apple": frozenset({"apple.com", "q4cdn.com"}),
    "costco": frozenset({"costco.com"}),
    "jpmorgan-chase": frozenset({"jpmorganchase.com"}),
    "exxon-mobil": frozenset({"exxonmobil.com"}),
    "tsmc": frozenset({"tsmc.com"}),
}
_SEC_HOSTS = frozenset({"sec.gov"})
_HKEX_HOSTS = frozenset({"hkexnews.hk"})
_SEC_ANNUAL_FORMS = frozenset({"10-K", "10-K/A", "20-F", "20-F/A"})
_BANK_COMPANIES = frozenset({"hsbc-holdings", "jpmorgan-chase"})
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


def verify_annual_package(
    manifest_path: Path,
    source_root: Path,
    as_of_date: date | None = None,
    identities: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Validate an annual evidence package without contacting its sources.

    ``identities`` is an optional explicit subset of controlled ``company_key``
    values. A subset may report subset completion, but never package completion.
    The default scope is every identity in the controlled pilot list.
    """

    manifest_path, source_root = Path(manifest_path), Path(source_root)
    manifest, load_error = _load_manifest(manifest_path)
    pilot = load_pilot_identities(DEFAULT_PILOT_LIST_PATH)
    pilot_by_key = {identity.company_key: identity for identity in pilot}
    requested = tuple(identities) if identities is not None else tuple(pilot_by_key)
    scope_errors: list[str] = []
    if len(set(requested)) != len(requested):
        scope_errors.append("requested identities contain duplicates")
    for key in requested:
        if key not in pilot_by_key:
            scope_errors.append(f"unknown controlled company_key: {key}")

    manifest_date = _date(manifest.get("as_of")) if manifest else None
    cutoff = as_of_date or manifest_date
    if cutoff is None:
        cutoff = date.min
    if as_of_date is not None and manifest_date is not None and as_of_date != manifest_date:
        scope_errors.append(
            f"requested as_of_date {as_of_date.isoformat()} disagrees with manifest as_of "
            f"{manifest_date.isoformat()}"
        )
    if load_error:
        scope_errors.append(load_error)
    if manifest is not None:
        if (
            type(manifest.get("schema_version")) is not int
            or manifest.get("schema_version") != SCHEMA_VERSION
        ):
            scope_errors.append(
                f"manifest schema_version must be {SCHEMA_VERSION}, got "
                f"{manifest.get('schema_version')!r}"
            )
        if manifest_date is None:
            scope_errors.append("manifest as_of must be an ISO date")

    companies_raw = manifest.get("companies", []) if manifest else []
    entries: dict[str, Mapping[str, Any]] = {}
    if isinstance(companies_raw, list):
        for index, row in enumerate(companies_raw):
            if not isinstance(row, Mapping) or not isinstance(row.get("company_key"), str):
                scope_errors.append(f"companies[{index}] must have a string company_key")
                continue
            key = row["company_key"]
            if key in entries:
                scope_errors.append(f"duplicate company entry: {key}")
            entries[key] = row
    else:
        scope_errors.append("manifest companies must be a list")

    per_company: list[dict[str, Any]] = []
    for key in requested:
        identity = pilot_by_key.get(key)
        entry = entries.get(key)
        report = _validate_company(
            key=key,
            entry=entry,
            identity=identity,
            report_currency=None if identity is None else identity.report_currency,
            requires_sec_shards=(
                identity is not None
                and any(binding.source == SOURCE_SEC_COMPANYFACTS for binding in identity.sources)
            ),
            source_root=source_root,
            as_of=cutoff,
        )
        if scope_errors:
            report["issues"].extend(scope_errors)
            report["status"] = "partial"
        per_company.append(report)

    selected_complete = bool(per_company) and all(
        item["status"] == "complete" for item in per_company
    )
    all_pilot_selected = set(requested) == set(pilot_by_key) and len(requested) == len(pilot_by_key)
    return {
        "schema_version": SCHEMA_VERSION,
        "as_of": cutoff.isoformat(),
        "status": "complete" if selected_complete and all_pilot_selected else "partial",
        "complete": selected_complete and all_pilot_selected,
        "subset_complete": selected_complete,
        "scope": "all_controlled" if all_pilot_selected else "explicit_subset",
        "required_companies": list(requested),
        "companies": per_company,
        "issues": scope_errors,
    }


def _validate_company(
    *,
    key: str,
    entry: Mapping[str, Any] | None,
    identity: Any,
    report_currency: str | None,
    requires_sec_shards: bool,
    source_root: Path,
    as_of: date,
) -> dict[str, Any]:
    issues: list[str] = []
    documents: dict[str, dict[str, Any]] = {}
    latest_checked = False
    report_located = False
    fields_checked: set[str] = set()
    field_verifications: list[dict[str, Any]] = []
    metric_facts: dict[tuple[Any, ...], FactRecord] = {}
    year_rows: list[Mapping[str, Any]] = []
    missing_shards: list[str] = []
    expected_shard_count = 0
    unresolved_conflicts = False
    fields: list[object] = []
    if entry is None:
        issues.append("company is absent from manifest")
        calculations: dict[str, dict[str, Any]] = {}
        calculation_superseded: set[str] = set()
        equity_scope_reviews: dict[str, dict[str, Any]] = {}
    else:
        if entry.get("report_currency") != report_currency:
            issues.append(
                f"report_currency {entry.get('report_currency')!r} does not match controlled "
                f"currency {report_currency!r}"
            )
        documents = _validate_documents(
            entry.get("documents"), source_root, as_of, issues, company_key=key
        )
        calculations, reconciliation_issues = _validate_equity_reconciliations(
            entry,
            documents,
            report_currency,
            as_of,
        )
        issues.extend(reconciliation_issues)
        calculation_superseded, superseded_issues = _validate_reconciliation_superseded_facts(
            entry,
            calculations,
            documents,
            report_currency,
            as_of,
        )
        issues.extend(superseded_issues)
        try:
            from investment_agent.materials.annual_scope_validation import (  # noqa: PLC0415
                validate_scope_reviews,
            )

            equity_scope_reviews, scope_review_issues = validate_scope_reviews(
                entry, documents, report_currency, as_of
            )
            issues.extend(scope_review_issues)
        except (ImportError, ValueError, TypeError, KeyError) as exc:
            equity_scope_reviews = {}
            if "equity_scope_reviews" in entry:
                issues.append(f"equity scope review validation failed: {type(exc).__name__}: {exc}")
        annual = entry.get("latest_annual")
        latest_period: tuple[date, date] | None = None
        latest_discovery_checked = False
        if not isinstance(annual, Mapping):
            issues.append("latest_annual is missing")
        else:
            annual_document_id = annual.get("document_id")
            annual_doc = (
                documents.get(annual_document_id) if isinstance(annual_document_id, str) else None
            )
            annual_start = _date(annual.get("period_start"))
            annual_end = _date(annual.get("period_end"))
            published = _date(annual.get("published_on"))
            if annual_start is None or annual_end is None:
                issues.append("latest_annual period_start/period_end must be ISO dates")
            elif annual_start >= annual_end or not 330 <= (annual_end - annual_start).days <= 400:
                issues.append(
                    "latest_annual must describe a complete annual period (330..400 days)"
                )
            else:
                latest_period = (annual_start, annual_end)
            if annual_end and annual_end > as_of:
                issues.append("latest_annual period ends after the as_of date")
            if published is None:
                issues.append("latest_annual published_on must be an ISO date")
            elif published > as_of:
                issues.append("latest_annual document was published after the as_of date")
            elif annual_end and published <= annual_end:
                issues.append(
                    "published_on must be later than period_end; period end is not publication"
                )
            if annual_doc is None or not annual_doc.get("_valid"):
                issues.append("latest_annual references a missing or invalid document")
            elif annual_doc.get("published_on") != published.isoformat() if published else True:
                issues.append("latest_annual published_on disagrees with its document metadata")
            elif not isinstance(annual_doc.get("role"), str) or annual_doc.get("role") not in {
                "issuer_annual",
                "sec_filing",
            }:
                issues.append(
                    "latest_annual document must be an issuer_annual or sec_filing source"
                )
            discovery = annual.get("discovery")
            latest_discovery_checked = _check_latest_discovery(
                discovery,
                documents,
                annual_end,
                annual.get("form"),
                published,
                annual_doc.get("url") if annual_doc is not None else None,
                annual.get("publication_evidence"),
                requires_sec_shards,
                as_of,
                issues,
            )
            if (
                annual_doc is not None
                and annual_doc.get("_valid")
                and annual_start
                and annual_end
                and published
            ):
                report_located = True
            latest_checked = (
                report_located
                and latest_discovery_checked
                and not any(
                    text.startswith("latest_annual") or "published_on" in text for text in issues
                )
            )

        raw_fields = entry.get("fields", [])
        if not isinstance(raw_fields, list):
            issues.append("fields must be a list")
        else:
            fields = raw_fields
        for index, field in enumerate(fields):
            result = _check_fact(
                field,
                documents,
                report_currency,
                as_of,
                where=f"fields[{index}]",
                expected_period=latest_period,
            )
            issues.extend(result["issues"])
            record = result["fact"]
            if result["ok"] and record is not None:
                metric_facts[_metric_fact_key(record)] = record
            evidence = field.get("evidence") if isinstance(field, Mapping) else None
            field_verifications.append(
                {
                    "category": field.get("category") if isinstance(field, Mapping) else None,
                    "metric": None if record is None else record.metric,
                    "period_end": None if record is None else record.period.end.isoformat(),
                    "status": "verified" if result["ok"] else "partial",
                    "amount": None if record is None else record.value.raw_value,
                    "raw_unit": None if record is None else record.value.raw_unit,
                    "scale": None if record is None else record.value.scale,
                    "unit": None if record is None else record.value.unit,
                    "currency": None if record is None else record.value.currency,
                    "definition": None if record is None else record.semantics.source_field,
                    "source_published_date": (
                        None
                        if record is None or record.provenance.source_published_date is None
                        else record.provenance.source_published_date.isoformat()
                    ),
                    "source_published_precision": (
                        None
                        if record is None
                        else record.provenance.source_published_precision.value
                    ),
                    "document_id": evidence.get("document_id")
                    if isinstance(evidence, Mapping)
                    else None,
                    "locator": evidence.get("locator") if isinstance(evidence, Mapping) else None,
                    "issues": result["issues"],
                }
            )
            if result["ok"]:
                category = field.get("category") if isinstance(field, Mapping) else None
                if isinstance(category, str) and category in FIELD_CATEGORIES:
                    if category in fields_checked:
                        issues.append(f"duplicate latest-year field category: {category}")
                    fields_checked.add(category)
        required_latest = set(FIELD_CATEGORIES)
        latest_checked = (
            latest_checked
            and required_latest.issubset(fields_checked)
            and not any(issue.startswith("fields[") for issue in issues)
        )

        year_rows_raw = entry.get("fiscal_years", [])
        if not isinstance(year_rows_raw, list):
            issues.append("fiscal_years must be a list")
        else:
            year_rows = [item for item in year_rows_raw if isinstance(item, Mapping)]
            if len(year_rows) != len(year_rows_raw):
                issues.append("each fiscal_years item must be an object")
        unresolved_conflicts = False

        shards = entry.get("expected_shards", [])
        if "expected_shards" not in entry:
            issues.append(
                "expected_shards must be explicitly derived from the saved submissions index"
            )
        if not isinstance(shards, list):
            issues.append("expected_shards must be a list")
            shards = []
        expected_shard_count = len(shards)
        for shard in shards:
            path = shard.get("path") if isinstance(shard, Mapping) else None
            doc_id = shard.get("document_id") if isinstance(shard, Mapping) else None
            doc = documents.get(doc_id) if isinstance(doc_id, str) else None
            if not isinstance(path, str) or doc is None or doc.get("path") != path:
                missing_shards.append(str(path or doc_id or "<invalid shard reference>"))
            elif not isinstance(doc.get("role"), str) or doc.get("role") not in {
                "sec_shard",
                "submissions_shard",
            }:
                issues.append(f"expected shard {path} is not marked as an SEC submissions shard")
        index_docs = [doc for doc in documents.values() if doc.get("role") == "submissions_index"]
        if requires_sec_shards:
            if not any(doc.get("role") == "sec_companyfacts" for doc in documents.values()):
                issues.append("SEC-bound company is missing its raw sec_companyfacts response")
                missing_shards.append("<sec_companyfacts>")
            if not index_docs:
                issues.append("SEC-bound company is missing its raw submissions_index response")
                missing_shards.append("<submissions_index>")
            elif len(index_docs) > 1:
                issues.append(
                    "exactly one submissions_index document is required for the selected window"
                )
            else:
                index_text = index_docs[0].get("_body")
                expected_names = _submission_shard_names(index_text)
                declared_names = {
                    PurePosixPath(str(item.get("path", ""))).name
                    for item in shards
                    if isinstance(item, Mapping)
                }
                if expected_names is None:
                    issues.append(
                        "submissions_index has no readable filings.files[].name shard list"
                    )
                    missing_shards.append("<unreadable filings.files>")
                elif expected_names != declared_names:
                    absent = sorted(expected_names - declared_names)
                    extra = sorted(declared_names - expected_names)
                    missing_shards.extend(absent)
                    missing_shards.extend(f"<unexpected:{name}>" for name in extra)
                    issues.append(
                        f"SEC shard manifest disagrees with submissions_index (missing={absent}, extra={extra})"
                    )
            sec_latest_checked = _check_sec_latest_annual(
                annual if isinstance(annual, Mapping) else None,
                documents,
                as_of,
                issues,
            )
            latest_checked = latest_checked and sec_latest_checked
        elif shards:
            issues.append("expected_shards supplied for a company with no controlled SEC binding")
        if missing_shards:
            issues.append(f"missing SEC shards: {', '.join(missing_shards)}")

    periods: list[tuple[date, date, str]] = []
    year_category_coverage: dict[str, set[str]] = {}
    year_numeric_coverage: dict[str, set[str]] = {}
    equity_by_end: dict[date, tuple[str | None, int, str | None]] = {}
    equity_facts: dict[date, FactRecord] = {}
    beginnings: dict[date, tuple[FactRecord | None, list[str]]] = {}
    beginning_calculation_facts: dict[str, tuple[date, FactRecord, bool]] = {}
    invalid_beginnings: set[date] = set()
    fact_values: dict[tuple[date, str], tuple[str | None, int, str | None]] = {}
    for field in fields:
        if not isinstance(field, Mapping):
            continue
        category = field.get("category")
        raw_fact = field.get("fact")
        if (
            not isinstance(category, str)
            or category not in FIELD_CATEGORIES
            or not isinstance(raw_fact, Mapping)
        ):
            continue
        try:
            fact_record = fact_from_payload(raw_fact)
        except (ValueError, TypeError, KeyError):
            continue
        if fact_record.value.raw_value is not None:
            fact_values[(fact_record.period.end, category)] = (
                fact_record.value.raw_value,
                fact_record.value.scale,
                fact_record.value.currency,
            )
    for index, year in enumerate(year_rows):
        start, end = _date(year.get("period_start")), _date(year.get("period_end"))
        label = str(year.get("label") or year.get("fiscal_year") or "")
        if start is None or end is None or start >= end or not 330 <= (end - start).days <= 400:
            issues.append(f"fiscal_years[{index}] has invalid annual period")
            continue
        periods.append((start, end, label))
        categories: set[str] = set()
        numeric_categories: set[str] = set()
        facts = year.get("facts", [])
        if not isinstance(facts, list):
            issues.append(f"fiscal_years[{index}].facts must be a list")
            facts = []
        for fact_index, field in enumerate(facts):
            result = _check_fact(
                field,
                documents,
                report_currency,
                as_of,
                where=f"fiscal_years[{index}].facts[{fact_index}]",
                expected_period=(start, end),
            )
            issues.extend(result["issues"])
            if result["ok"]:
                category = field.get("category")
                if category in categories:
                    issues.append(
                        f"duplicate {category} field evidence for fiscal year {end.isoformat()}"
                    )
                categories.add(category)
                fact_record = result["fact"]
                if fact_record is not None:
                    metric_facts[_metric_fact_key(fact_record)] = fact_record
                if (
                    fact_record.value.raw_value is not None
                    and fact_record.applicability == "applicable"
                ):
                    numeric_categories.add(category)
                    fact_value = (
                        fact_record.value.raw_value,
                        fact_record.value.scale,
                        fact_record.value.currency,
                    )
                    prior_value = fact_values.get((end, category))
                    if prior_value is not None and prior_value != fact_value:
                        issues.append(
                            f"conflicting facts for {category} at {end.isoformat()} disagree"
                        )
                    fact_values[(end, category)] = fact_value
                if category == "equity":
                    equity_by_end[end] = (
                        fact_record.value.raw_value,
                        fact_record.value.scale,
                        fact_record.value.currency,
                    )
                    equity_facts[end] = fact_record
        year_category_coverage[end.isoformat()] = categories
        year_numeric_coverage[end.isoformat()] = numeric_categories
        beginning = year.get("beginning_equity_fact")
        if beginning is None:
            beginnings[end] = (None, ["beginning equity fact is missing"])
            invalid_beginnings.add(end)
            issues.append(f"fiscal_years[{index}] is missing beginning_equity_fact for ROE")
        else:
            result = _check_fact(
                beginning,
                documents,
                report_currency,
                as_of,
                where=f"fiscal_years[{index}].beginning_equity_fact",
                calculations=calculations,
            )
            issues.extend(result["issues"])
            fact_record = result["fact"]
            if result["ok"] and fact_record is not None:
                metric_facts[_metric_fact_key(fact_record)] = fact_record
                calculation_ref = (
                    beginning.get("calculation_ref") if isinstance(beginning, Mapping) else None
                )
                if isinstance(calculation_ref, str):
                    beginning_calculation_facts[calculation_ref] = (end, fact_record, result["ok"])
            if result["ok"] and (
                fact_record.metric not in _CATEGORY_METRICS["equity"]
                or fact_record.period.basis != PeriodBasis.INSTANT
                or fact_record.period.end != start - timedelta(days=1)
            ):
                invalid_beginnings.add(end)
                issues.append(
                    f"fiscal_years[{index}].beginning_equity_fact must be the equity instant on "
                    "the day before period_start"
                )
            closing_fact = equity_facts.get(end)
            if (
                result["ok"]
                and closing_fact is not None
                and (
                    fact_record.value.currency != closing_fact.value.currency
                    or fact_record.semantics.reporting_scope
                    != closing_fact.semantics.reporting_scope
                    or fact_record.semantics.equity_scope != closing_fact.semantics.equity_scope
                )
            ):
                invalid_beginnings.add(end)
                issues.append(
                    f"fiscal_years[{index}].beginning_equity_fact scope/currency is incompatible "
                    "with year-end equity"
                )
            beginnings[end] = (fact_record if result["ok"] else None, result["issues"])
            if not result["ok"]:
                invalid_beginnings.add(end)

    unresolved_conflicts, resolution_issues, resolved_conflict_summaries = (
        _validate_conflict_resolutions(
            entry,
            calculations,
            calculation_superseded,
            beginning_calculation_facts,
            equity_scope_reviews,
        )
    )
    issues.extend(resolution_issues)

    periods.sort(key=lambda item: item[0])
    if len({end for _, end, _ in periods}) != len(periods):
        issues.append("duplicate fiscal-year period_end values are not permitted")
    if entry is not None:
        annual = entry.get("latest_annual")
        annual_start = _date(annual.get("period_start")) if isinstance(annual, Mapping) else None
        annual_end = _date(annual.get("period_end")) if isinstance(annual, Mapping) else None
        if periods and (annual_start, annual_end) != periods[-1][:2]:
            issues.append("latest_annual period does not match the latest fiscal_years input")
    continuous = len(periods) >= 4 and _four_latest_contiguous(periods[-4:])
    if len(periods) < 4:
        issues.append(f"only {len(periods)} fiscal years supplied; four are required")
    elif not continuous:
        issues.append("latest four fiscal years are not continuous complete annual periods")
    latest_four = periods[-4:] if len(periods) >= 4 else periods
    annual_category_inputs_complete = (
        bool(latest_four)
        and all(
            {"revenue", "profit", "equity"}.issubset(
                year_category_coverage.get(end.isoformat(), set())
            )
            for _, end, _ in latest_four
        )
        and all(
            {"revenue", "profit", "equity"}.issubset(
                year_numeric_coverage.get(end.isoformat(), set())
            )
            for _, end, _ in latest_four
        )
    )
    if latest_four and not annual_category_inputs_complete:
        issues.append("latest fiscal-year inputs lack revenue, profit, or equity coverage")
    annual_field_evidence_complete = bool(latest_four) and all(
        FIELD_CATEGORIES.issubset(year_category_coverage.get(end.isoformat(), set()))
        for _, end, _ in latest_four
    )
    # The six classes are always checked for the latest report. Older years
    # only gate the metrics that consume a four-year series (revenue, profit,
    # and equity); other classes remain visible as additional coverage.

    beginning_equity_complete = bool(latest_four) and all(
        end in beginnings and beginnings[end][0] is not None and end not in invalid_beginnings
        for _, end, _ in latest_four
    )
    if beginning_equity_complete:
        for previous, current in zip(latest_four, latest_four[1:], strict=False):
            prior_start, prior_end, _ = previous
            current_start, _, _ = current
            beginning = beginnings[current[1]][0]
            prior_value = equity_by_end.get(prior_end)
            if beginning is not None and prior_value is not None:
                beg_value = (
                    beginning.value.raw_value,
                    beginning.value.scale,
                    beginning.value.currency,
                )
                if beg_value != prior_value:
                    invalid_beginnings.add(current[1])
                    beginning_equity_complete = False
                    issues.append(
                        f"beginning equity for {current[2] or current_start.isoformat()} "
                        "does not match prior-year closing equity; restatement/conflict review required"
                    )
    if not beginning_equity_complete:
        issues.append(
            "beginning equity is not available for every latest-four year; ROE is incomplete"
        )

    formula_metrics: dict[str, Any] = {}
    formula_inputs_complete = False
    try:
        profile = profile_from_identity(identity, is_bank=key in _BANK_COMPANIES)
        metric_periods = [
            FactPeriod(start, end, PeriodBasis.ANNUAL, label=label or f"FY{end.year}")
            for start, end, label in latest_four
        ]
        computed = compute_metrics(
            facts=tuple(metric_facts.values()),
            profile=profile,
            fiscal_years=metric_periods,
        )
        results_by_key: dict[str, list[Any]] = {}
        for metric_result in computed.metrics:
            results_by_key.setdefault(metric_result.key, []).append(metric_result)
        yoy_result = next(iter(results_by_key.get("revenue_yoy", [])), None)
        cagr_result = next(iter(results_by_key.get("revenue_cagr_3y", [])), None)
        roe_results = results_by_key.get("roe_avg_equity", [])
        formula_metrics = {
            "revenue_yoy": _metric_result_summary(yoy_result),
            "revenue_cagr_3y": _metric_result_summary(cagr_result),
            "roe_avg_equity": [_metric_result_summary(item) for item in roe_results],
            "gaps": list(computed.gaps),
        }
        expected_roe_unavailable = key in _BANK_COMPANIES or profile.is_ads
        revenue_formulas_ok = (
            yoy_result is not None
            and yoy_result.status == CalculationStatus.OK
            and cagr_result is not None
            and cagr_result.status == CalculationStatus.OK
        )
        roe_formulas_ok = len(roe_results) == len(metric_periods) and all(
            result.status == CalculationStatus.OK
            or (expected_roe_unavailable and result.status == CalculationStatus.NOT_APPLICABLE)
            for result in roe_results
        )
        formula_inputs_complete = revenue_formulas_ok and roe_formulas_ok
    except Exception as exc:
        issues.append(f"offline core metric evaluation failed: {type(exc).__name__}: {exc}")
    annual_inputs_complete = (
        annual_category_inputs_complete and formula_inputs_complete and not unresolved_conflicts
    )
    if annual_category_inputs_complete and not formula_inputs_complete:
        issues.append("annual inputs are present but one or more core metric formulas reject them")

    required_latest_fields = set(FIELD_CATEGORIES)
    if not required_latest_fields.issubset(fields_checked):
        absent = sorted(required_latest_fields - fields_checked)
        issues.append(f"latest-year field evidence incomplete: {', '.join(absent)}")
    shard_complete = not missing_shards and not any(
        "shard manifest disagrees" in issue for issue in issues
    )
    if requires_sec_shards:
        latest_checked = latest_checked and shard_complete
    raw_doc_total = len(documents)
    raw_doc_valid = sum(1 for document in documents.values() if document.get("_valid"))
    status = (
        "complete"
        if (
            report_located
            and latest_checked
            and not issues
            and continuous
            and annual_inputs_complete
            and beginning_equity_complete
            and shard_complete
            and required_latest_fields.issubset(fields_checked)
        )
        else "partial"
    )
    return {
        "company_key": key,
        "status": status,
        "located_annual_report": report_located,
        "latest_annual_checked": latest_checked,
        "fields_checked": sorted(fields_checked),
        "field_verifications": field_verifications,
        "required_latest_fields": sorted(required_latest_fields),
        "annual_inputs_complete": annual_inputs_complete,
        "annual_category_inputs_complete": annual_category_inputs_complete,
        "formula_inputs_complete": formula_inputs_complete,
        "core_metrics": formula_metrics,
        "annual_field_evidence_complete": annual_field_evidence_complete,
        "fiscal_years": [
            {"label": label, "period_start": start.isoformat(), "period_end": end.isoformat()}
            for start, end, label in periods
        ],
        "annual_field_coverage": [
            {
                "period_end": end,
                "evidence_fields": sorted(year_category_coverage.get(end, set())),
                "numeric_inputs": sorted(year_numeric_coverage.get(end, set())),
            }
            for end in sorted(year_category_coverage)
        ],
        "continuous_four_years": continuous,
        "roe_beginning_equity_complete": beginning_equity_complete,
        "source_documents_checked": sum(
            1 for document in documents.values() if document.get("_valid")
        ),
        "source_raw_coverage": {
            "listed": raw_doc_total,
            "verified": raw_doc_valid,
            "invalid_or_missing": raw_doc_total - raw_doc_valid,
        },
        "source_documents": _document_summaries(documents),
        "equity_reconciliations": [
            {
                "calculation_ref": ref,
                "method_version": calculation.get("method_version"),
                "output": calculation.get("output"),
                "input_count": len(calculation.get("inputs", [])),
                "superseded_fact_retained": ref in calculation_superseded,
            }
            for ref, calculation in sorted(calculations.items())
        ],
        "equity_scope_reviews": [
            {
                "review_id": review_id,
                "review_ref": result.get("review_ref"),
            }
            for review_id, result in sorted(equity_scope_reviews.items())
        ],
        "validated_conflict_resolutions": resolved_conflict_summaries,
        "latest_annual": _latest_annual_summary(entry, documents) if entry else None,
        "latestness_method": _latestness_method(entry, documents),
        "sec_shards": {
            "expected": expected_shard_count,
            "missing": missing_shards,
            "complete": shard_complete,
        },
        "issues": issues,
    }


def _validate_documents(
    value: object,
    root: Path,
    as_of: date,
    issues: list[str],
    *,
    company_key: str,
) -> dict[str, dict[str, Any]]:
    if not isinstance(value, list):
        issues.append("documents must be a list")
        return {}
    result: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(value):
        where = f"documents[{index}]"
        if not isinstance(item, Mapping):
            issues.append(f"{where} must be an object")
            continue
        doc_id = item.get("id")
        if not isinstance(doc_id, str) or not doc_id:
            issues.append(f"{where}.id must be a non-empty string")
            continue
        if doc_id in result:
            issues.append(f"duplicate document id: {doc_id}")
            continue
        doc = dict(item)
        result[doc_id] = doc
        if not isinstance(item.get("role"), str):
            issues.append(f"{where}.role must be a string")
        rel = _safe_relative_path(item.get("path"))
        if rel is None:
            issues.append(f"{where}.path is unsafe or not a relative POSIX path")
            continue
        resolved_root = root.resolve()
        path = (resolved_root / rel).resolve()
        if not path.is_relative_to(resolved_root):
            issues.append(f"{where}.path resolves outside source_root")
            continue
        try:
            body = path.read_bytes()
        except OSError:
            issues.append(f"{where}.path body is missing or unreadable: {rel.as_posix()}")
            continue
        expected_hash = item.get("sha256")
        actual_hash = hashlib.sha256(body).hexdigest()
        if len(body) == 0:
            issues.append(f"{where} body is empty")
        if not isinstance(item.get("bytes"), int) or isinstance(item.get("bytes"), bool):
            issues.append(f"{where}.bytes must be an integer")
        elif item["bytes"] != len(body):
            issues.append(f"{where}.bytes does not match body length")
        if expected_hash != actual_hash:
            issues.append(f"{where}.sha256 does not match body")
        status = item.get("http_status")
        if not isinstance(status, int) or isinstance(status, bool) or not 200 <= status < 300:
            issues.append(f"{where}.http_status must be a successful HTTP status")
        if not isinstance(item.get("media_type"), str) or not item["media_type"].strip():
            issues.append(f"{where}.media_type is required")
        url = item.get("url")
        parsed = urlparse(url) if isinstance(url, str) else None
        if (
            parsed is None
            or parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            issues.append(f"{where}.url must be an https URL")
        elif not _document_host_allowed(company_key, item.get("role"), parsed.hostname):
            issues.append(
                f"{where}.url host {parsed.hostname!r} is not allowed for controlled issuer "
                f"{company_key!r} and role {item.get('role')!r}"
            )
        captured = _datetime(item.get("captured_at"))
        if captured is None:
            issues.append(f"{where}.captured_at must be an ISO timezone-aware timestamp")
        elif captured.date() > as_of:
            issues.append(f"{where}.captured_at is after the as_of date")
        published = _date(item.get("published_on"))
        if item.get("published_on") is not None and published is None:
            issues.append(f"{where}.published_on must be an ISO date when known")
        elif published is not None and published > as_of:
            issues.append(f"{where}.published_on is after the as_of date")
        doc["_body"] = body
        doc["_text"] = _extract_document_text(body, str(item.get("media_type", "")))
        doc["_links"] = _extract_document_links(body, str(item.get("media_type", "")))
        if doc["_text"] is None:
            issues.append(
                f"{where} text could not be extracted; install the optional pypdf dependency "
                "for PDF evidence"
            )
        doc["_valid"] = not any(issue.startswith(where) for issue in issues)
        doc["_relative_path"] = rel.as_posix()
    return result


def _validate_equity_reconciliations(
    entry: Mapping[str, Any],
    documents: Mapping[str, Mapping[str, Any]],
    report_currency: str | None,
    as_of: date,
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Validate audited calculation inputs against frozen issuer source bodies."""

    if "equity_reconciliations" not in entry:
        return {}, []
    issues: list[str] = []
    company_key = entry.get("company_key")
    if company_key != "hsbc-holdings" or report_currency != "USD":
        return {}, ["equity_reconciliations are only valid for HSBC Holdings USD facts"]
    records = entry.get("equity_reconciliations")
    if not isinstance(records, list) or not records:
        return {}, ["equity_reconciliations must be a non-empty list"]
    accepted: dict[str, dict[str, Any]] = {}
    for index, record in enumerate(records):
        where = f"equity_reconciliations[{index}]"
        if not isinstance(record, Mapping):
            issues.append(f"{where} must be an object")
            continue
        try:
            evaluated = evaluate_equity_reconciliation(record)
        except (ValueError, TypeError, KeyError) as exc:
            issues.append(f"{where} is invalid: {exc}")
            continue
        calculation_ref = evaluated["calculation_ref"]
        if calculation_ref in accepted:
            issues.append(f"duplicate equity reconciliation reference: {calculation_ref}")
            continue
        source_issues = _validate_hsbc_reconciliation_sources(
            entry, evaluated, documents, as_of, where
        )
        if source_issues:
            issues.extend(source_issues)
            continue
        accepted[calculation_ref] = evaluated
    return accepted, issues


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


def _validate_conflict_resolutions(
    entry: Mapping[str, Any] | None,
    calculations: Mapping[str, Mapping[str, Any]],
    calculation_superseded: set[str],
    beginning_facts: Mapping[str, tuple[date, FactRecord, bool]],
    equity_scope_reviews: Mapping[str, Mapping[str, Any]],
) -> tuple[bool, list[str], list[dict[str, Any]]]:
    if entry is None:
        return False, [], []
    conflicts = entry.get("conflicts", [])
    if not isinstance(conflicts, list):
        return True, ["conflicts must be a list"], []
    issues: list[str] = []
    summaries: list[dict[str, Any]] = []
    unresolved = False
    scope_refs = {
        value.get("review_ref"): (review_id, value)
        for review_id, value in equity_scope_reviews.items()
        if isinstance(value, Mapping) and isinstance(value.get("review_ref"), str)
    }
    for index, conflict in enumerate(conflicts):
        where = f"conflicts[{index}]"
        if not isinstance(conflict, Mapping):
            unresolved = True
            issues.append(f"{where} must be an object")
            continue
        if conflict.get("status") != "resolved":
            unresolved = True
            issues.append(f"{where} is unresolved")
            continue
        ref = conflict.get("resolution_ref")
        kind = conflict.get("kind")
        accepted = False
        if (
            kind == "ifrs_17_restatement"
            and isinstance(ref, str)
            and ref in calculations
            and ref in calculation_superseded
        ):
            beginning = beginning_facts.get(ref)
            evaluation = calculations[ref]
            output = evaluation["output"]
            if beginning is not None:
                fiscal_end, fact, fact_ok = beginning
                accepted = (
                    fact_ok
                    and fiscal_end == date(2022, 12, 31)
                    and fact.metric == output["metric"]
                    and fact.value.raw_value == output["value"]
                    and fact.value.scale == output["scale"]
                    and fact.value.currency == output["currency"]
                    and fact.period.end.isoformat() == output["period_end"]
                    and fact.semantics.equity_scope.value == output["equity_scope"]
                )
        elif kind == "equity_scope_tag_change" and isinstance(ref, str):
            accepted = (
                ref in scope_refs
                and scope_refs[ref][0] == "costco.parent-equity.v1"
                and entry.get("company_key") == "costco"
                and conflict.get("period") == "FY2022–FY2023 vs FY2024–FY2026"
                and conflict.get("affected_period_ends") == ["2022-08-28", "2023-09-03"]
            )
        if not accepted:
            unresolved = True
            issues.append(f"{where} resolution_ref is not backed by its matching validated review")
            continue
        summaries.append({"kind": kind, "resolution_ref": ref, "status": "validated"})
    if unresolved:
        issues.append("manifest declares unresolved or unbacked source/restatement conflicts")
    return unresolved, issues, summaries


def _check_fact(
    field: object,
    documents: Mapping[str, Mapping[str, Any]],
    report_currency: str | None,
    as_of: date,
    *,
    where: str,
    expected_period: tuple[date, date] | None = None,
    calculations: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    issues: list[str] = []
    if not isinstance(field, Mapping):
        return {"ok": False, "fact": None, "issues": [f"{where} must be an object"]}
    category = field.get("category")
    if not isinstance(category, str) or category not in FIELD_CATEGORIES:
        issues.append(f"{where}.category is unknown: {category!r}")
    raw_fact = field.get("fact")
    fact: FactRecord | None = None
    if not isinstance(raw_fact, Mapping):
        issues.append(f"{where}.fact must be a canonical serialized FactRecord")
    else:
        try:
            fact = fact_from_payload(raw_fact)
            if fact_to_payload(fact) != dict(raw_fact):
                issues.append(f"{where}.fact does not round-trip through frozen serializers")
            contract_draft = DatasetDraft(
                kind=DatasetKind.ANNUAL_FACTS,
                subject_kind=SubjectKind.COMPANY,
                subject_id=1,
                source=fact.provenance.source,
                schema_version=CONTRACT_SCHEMA_VERSION,
                adapter_version="annual-validation",
                captured_at=datetime.combine(as_of, time.min, tzinfo=UTC),
                payload={"facts": [dict(raw_fact)]},
            )
            issues.extend(
                f"{where}.fact contract: {problem}" for problem in validate_draft(contract_draft)
            )
        except (ValueError, TypeError, KeyError) as exc:
            issues.append(f"{where}.fact is invalid under frozen serializer: {exc}")
    if fact is not None:
        if (
            isinstance(category, str)
            and category in FIELD_CATEGORIES
            and fact.metric not in _CATEGORY_METRICS[category]
        ):
            issues.append(f"{where}.fact metric {fact.metric!r} does not match {category}")
        if fact.verification != Verification.VERIFIED:
            issues.append(f"{where}.fact verification is not verified")
        if fact.conflict != "none":
            issues.append(f"{where}.fact has unresolved/resolved conflict state {fact.conflict!r}")
        if expected_period is not None:
            if category == "assets" or category == "equity":
                if fact.period.basis != PeriodBasis.INSTANT or fact.period.start is not None:
                    issues.append(f"{where}.fact must use instant period basis for {category}")
                if fact.period.end != expected_period[1]:
                    issues.append(f"{where}.fact instant period_end does not match the fiscal year")
            else:
                if fact.period.basis != PeriodBasis.ANNUAL:
                    issues.append(f"{where}.fact must use annual period basis")
                if fact.period.start != expected_period[0] or fact.period.end != expected_period[1]:
                    issues.append(f"{where}.fact period does not match its fiscal_years row")
        if fact.value.currency != report_currency and fact.value.unit in CURRENCY_UNITS:
            issues.append(
                f"{where}.fact currency {fact.value.currency!r} does not match report currency "
                f"{report_currency!r}"
            )
        if fact.value.raw_value is not None and (
            fact.value.unit != report_currency or fact.value.currency != report_currency
        ):
            issues.append(
                f"{where}.fact canonical unit/currency must both equal report currency "
                f"{report_currency!r}"
            )
        if not fact.semantics.source_label or not fact.semantics.source_field:
            issues.append(f"{where}.fact must retain source labels/field definition")
        if fact.value.raw_value is None and not fact.value.missing_reason:
            issues.append(f"{where}.fact missing amount has no reason")
        if fact.value.raw_value is not None and not isinstance(fact.value.raw_value, str):
            issues.append(f"{where}.fact amount must be a decimal string, never a JSON number")
        if fact.period.end > as_of:
            issues.append(f"{where}.fact period ends after as_of")
        source_published = fact.provenance.source_published_date
        if (
            source_published is None
            or fact.provenance.source_published_precision.value == "unknown"
        ):
            issues.append(f"{where}.fact publication date precision is unknown")
        if source_published is not None and source_published > as_of:
            issues.append(f"{where}.fact publication date is after as_of")
        if source_published is not None and source_published <= fact.period.end:
            issues.append(f"{where}.fact publication date must be later than its period end")
        source_published_at = fact.provenance.source_published_at
        cutoff_moment = datetime.combine(as_of, time.max, tzinfo=UTC)
        if source_published_at is not None and source_published_at.astimezone(UTC) > cutoff_moment:
            issues.append(f"{where}.fact publication timestamp is after as_of")
        source_available_at = fact.provenance.source_available_at
        if source_available_at is not None and source_available_at.astimezone(UTC) > cutoff_moment:
            issues.append(f"{where}.fact source_available_at is after as_of")
        if fact.semantics.accounting_standard.value == "unknown":
            issues.append(f"{where}.fact accounting standard is unknown")
        if fact.semantics.reporting_scope.value == "unknown":
            issues.append(f"{where}.fact reporting scope is unknown")
        if (
            isinstance(category, str)
            and category in {"profit", "equity"}
            and fact.semantics.equity_scope.value == "unknown"
        ):
            issues.append(f"{where}.fact equity scope is unknown")

    if fact is not None and fact.provenance.source == "material.equity_reconciliation":
        issues.extend(
            _check_equity_reconciliation_fact(
                field,
                fact,
                calculations or {},
                report_currency,
                where,
            )
        )
        return {"ok": not issues, "fact": fact, "issues": issues}
    if field.get("calculation_ref") is not None:
        issues.append(
            f"{where}.calculation_ref is only permitted for a derived reconciliation fact"
        )

    evidence = field.get("evidence")
    if not isinstance(evidence, Mapping):
        issues.append(f"{where}.evidence must identify an original document, locator, and excerpt")
        evidence = {}
    doc_id = evidence.get("document_id")
    doc = documents.get(doc_id) if isinstance(doc_id, str) else None
    if doc is None or not doc.get("_valid"):
        issues.append(f"{where}.evidence references a missing or invalid document")
    elif not isinstance(doc.get("role"), str) or doc.get("role") not in {
        "issuer_annual",
        "sec_filing",
    }:
        issues.append(f"{where}.evidence must cite the issuer annual report or an SEC filing")
    locator = evidence.get("locator")
    excerpt = evidence.get("excerpt")
    context = _located_context(doc, locator) if doc is not None else None
    if context is None:
        issues.append(f"{where}.evidence.locator must identify a source PDF page or text anchor")
    if fact is not None:
        canonical_locator = (
            json.dumps(locator, sort_keys=True, separators=(",", ":"))
            if isinstance(locator, Mapping)
            else None
        )
        if fact.original_locator != canonical_locator:
            issues.append(f"{where}.evidence.locator disagrees with FactRecord.original_locator")
    if not isinstance(excerpt, str) or not excerpt.strip():
        issues.append(f"{where}.evidence.excerpt is required")
    elif (
        doc is not None
        and isinstance(doc.get("_text"), str)
        and (context is None or excerpt not in context)
    ):
        issues.append(f"{where}.evidence.excerpt does not occur in original document text")
    if fact is not None and isinstance(excerpt, str):
        label = fact.semantics.source_label
        if not label or label.casefold() not in excerpt.casefold():
            issues.append(f"{where}.evidence excerpt does not contain the fact's source label")
    if fact is not None and fact.value.raw_value is not None and isinstance(excerpt, str):
        positive_capex_outflow = (
            category == "capex"
            and fact.metric == "capex_ppe"
            and fact.value.sign_convention.strip().casefold()
            in {"capital expenditure", "positive_outflow_magnitude"}
        )
        if not _contains_exact_amount(
            excerpt,
            fact.value.raw_value,
            parenthesized_is_positive=positive_capex_outflow,
        ):
            issues.append(
                f"{where}.evidence excerpt does not contain the exact canonical raw amount"
            )
        row_values = evidence.get("row_values")
        column_years = evidence.get("column_years")
        selected_year = evidence.get("selected_year")
        if row_values is not None or column_years is not None or selected_year is not None:
            if (
                not isinstance(row_values, list)
                or not isinstance(column_years, list)
                or len(row_values) != len(column_years)
            ):
                issues.append(
                    f"{where}.evidence row_values and column_years must be equally sized lists"
                )
            elif selected_year not in column_years:
                issues.append(f"{where}.evidence selected_year is absent from column_years")
            else:
                selected_index = column_years.index(selected_year)
                if not _same_numeric_token(row_values[selected_index], fact.value.raw_value):
                    issues.append(
                        f"{where}.evidence selected row value disagrees with the fact amount"
                    )
                period_year = str(fact.period.end.year)
                if period_year not in str(selected_year):
                    issues.append(f"{where}.evidence selected_year disagrees with fact period_end")
    if fact is not None and fact.value.raw_value is None and isinstance(excerpt, str):
        reason = fact.value.missing_reason or ""
        review_reason = evidence.get("review_reason")
        if not isinstance(review_reason, str) or review_reason != reason:
            issues.append(f"{where}.evidence.review_reason must match the explicit missing reason")
        if fact.applicability == "not_applicable":
            source_label = fact.semantics.source_label or ""
            if source_label and source_label.casefold() not in excerpt.casefold():
                issues.append(f"{where}.evidence excerpt does not contain the source label for N/A")
    if fact is not None and fact.provenance.original_hash:
        if doc is not None and fact.provenance.original_hash != doc.get("sha256"):
            issues.append(f"{where}.fact original_hash disagrees with referenced document SHA-256")
    elif fact is not None:
        issues.append(f"{where}.fact original_hash is required")
    if fact is not None and doc is not None:
        doc_published = _date(doc.get("published_on"))
        if doc_published is not None and fact.provenance.source_published_date != doc_published:
            issues.append(f"{where}.fact publication date disagrees with its document")
    return {"ok": not issues, "fact": fact, "issues": issues}


def _check_equity_reconciliation_fact(
    field: Mapping[str, Any],
    fact: FactRecord,
    calculations: Mapping[str, Mapping[str, Any]],
    report_currency: str | None,
    where: str,
) -> list[str]:
    issues: list[str] = []
    ref = field.get("calculation_ref")
    if not isinstance(ref, str) or not ref.startswith("sha256:"):
        return [f"{where}.calculation_ref must identify a validated calculation"]
    calculation = calculations.get(ref)
    if not isinstance(calculation, Mapping):
        return [f"{where}.calculation_ref does not identify a validated reconciliation"]
    output = calculation.get("output")
    if not isinstance(output, Mapping):
        return [f"{where}.calculation_ref has no validated output"]
    expected_locator = json.dumps(
        {"kind": "calculation", "ref": ref},
        sort_keys=True,
        separators=(",", ":"),
    )
    expected_published_dates = {
        item.get("source_ref", {}).get("published_on")
        for item in calculation.get("inputs", [])
        if isinstance(item, Mapping) and isinstance(item.get("source_ref"), Mapping)
    }
    expected_published_date = (
        next(iter(expected_published_dates)) if len(expected_published_dates) == 1 else None
    )
    checks = (
        (field.get("category") == "equity", "category must be equity"),
        (
            fact.metric == output.get("metric") == "equity_ordinary",
            "metric must match derived ordinary equity",
        ),
        (
            fact.value.raw_value == output.get("value"),
            "amount must exactly match the calculation output",
        ),
        (
            fact.value.scale == output.get("scale") == 1_000_000,
            "scale must match USD million output",
        ),
        (
            fact.value.currency == output.get("currency") == report_currency == "USD",
            "currency must match output and company",
        ),
        (
            fact.value.unit == "USD" and fact.value.raw_unit == "USD million",
            "raw and canonical units must be USD million and USD",
        ),
        (
            fact.period.basis == PeriodBasis.INSTANT and fact.period.start is None,
            "period must be an instant",
        ),
        (
            fact.period.end.isoformat() == output.get("period_end") == "2021-12-31",
            "period_end must match the opening balance",
        ),
        (
            fact.semantics.equity_scope.value
            == output.get("equity_scope")
            == "ordinary_shareholders",
            "scope must be ordinary shareholders",
        ),
        (
            fact.semantics.reporting_scope.value == "consolidated",
            "reporting scope must be consolidated",
        ),
        (fact.semantics.accounting_standard.value == "ifrs", "accounting standard must be IFRS"),
        (
            fact.provenance.original_hash == calculation.get("input_hash"),
            "original_hash must match the calculation input hash",
        ),
        (
            fact.original_locator == expected_locator,
            "original_locator must identify this calculation",
        ),
        (
            expected_published_date is not None
            and fact.provenance.source_published_date is not None
            and fact.provenance.source_published_date.isoformat() == expected_published_date,
            "source publication date must match the cited calculation inputs",
        ),
        (
            fact.provenance.source_published_precision.value == "date",
            "source publication precision must be date",
        ),
    )
    for okay, message in checks:
        if not okay:
            issues.append(f"{where}.fact {message}")
    evidence = field.get("evidence")
    if not isinstance(evidence, Mapping):
        issues.append(f"{where}.evidence must describe the derived calculation")
    elif (
        evidence.get("kind") != "derived_calculation"
        or evidence.get("calculation_ref") != ref
        or not isinstance(evidence.get("availability_basis"), str)
        or "not a directly disclosed amount"
        not in evidence.get("availability_basis", "").casefold()
    ):
        issues.append(
            f"{where}.evidence must identify this derived calculation and its availability basis"
        )
    return issues


def _numeric_token(value: str) -> str:
    return re.sub(r"[,_\s]", "", value).strip()


def _same_numeric_token(left: object, right: str) -> bool:
    return (
        isinstance(left, (str, int))
        and not isinstance(left, bool)
        and _numeric_token(str(left)) == _numeric_token(right)
    )


def _contains_exact_amount(
    text: str, amount: str, *, parenthesized_is_positive: bool = False
) -> bool:
    normalized_amount = _numeric_token(amount)
    if not normalized_amount:
        return False
    # Tokenize before removing thousands separators. Removing whitespace from
    # the whole excerpt would concatenate adjacent year columns into one
    # apparently valid amount (for example, "751,766 660,257").
    token_pattern = re.compile(
        r"(?<![A-Za-z0-9_.])(?:\([0-9][0-9,_]*(?:\.[0-9]+)?\)|"
        r"[+-]?[0-9][0-9,_]*(?:\.[0-9]+)?)(?![A-Za-z0-9_.])"
    )
    for match in token_pattern.finditer(text):
        token = _numeric_token(match.group())
        if token.startswith("(") and token.endswith(")"):
            token = token[1:-1]
            if not parenthesized_is_positive:
                token = f"-{token}"
        if token == normalized_amount:
            return True
    return False


def _metric_fact_key(fact: FactRecord) -> tuple[Any, ...]:
    return (
        fact.metric,
        fact.period.start,
        fact.period.end,
        fact.period.basis,
        fact.value.raw_value,
        fact.value.scale,
        fact.value.currency,
        fact.semantics.reporting_scope,
        fact.semantics.equity_scope,
        fact.semantics.accounting_standard,
    )


def _metric_result_summary(result: Any) -> dict[str, Any] | None:
    if result is None:
        return None
    value = result.value
    return {
        "key": result.key,
        "status": result.status.value,
        "value": None if value is None or value.value is None else str(value.value),
        "unit": None if value is None else value.unit,
        "currency": None if value is None else value.currency,
        "period_ends": [period.end.isoformat() for period in result.periods],
        "basis": result.basis,
        "notes": list(result.notes),
        "inputs": list(result.inputs),
    }


def _four_latest_contiguous(periods: Sequence[tuple[date, date, str]]) -> bool:
    if len(periods) != 4:
        return False
    for previous, current in zip(periods, periods[1:], strict=False):
        # Fiscal year boundaries can be 52/53-week dates. Allow a one-day gap
        # convention around inclusive/exclusive boundaries while preserving the
        # actual period lengths supplied by the issuer.
        if abs((current[0] - previous[1]).days) > 1:
            return False
    ends = [row[1] for row in periods]
    return len(set(ends)) == 4


def _load_manifest(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return None, f"cannot read annual evidence manifest: {exc}"
    if not isinstance(value, dict):
        return None, "annual evidence manifest must be a JSON object"
    return value, None


def _safe_relative_path(value: object) -> PurePosixPath | None:
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or any(ord(character) < 32 for character in value)
    ):
        return None
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return None
    return path


def _date(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        result = date.fromisoformat(value)
    except ValueError:
        return None
    return result if result.isoformat() == value else None


def _datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _extract_document_text(body: bytes, media_type: str) -> str | None:
    """Extract searchable text from the original body, including issuer PDFs."""

    if "pdf" in media_type.casefold() or body.startswith(b"%PDF-"):
        try:
            from io import BytesIO

            from pypdf import PdfReader

            return "\n\f\n".join(
                page.extract_text() or "" for page in PdfReader(BytesIO(body)).pages
            )
        except Exception:
            return None
    text = body.decode("utf-8", errors="replace")
    if "html" in media_type.casefold() or re.search(r"<html(?:\s|>)", text[:1024], re.IGNORECASE):

        class VisibleTextParser(HTMLParser):
            def __init__(self) -> None:
                super().__init__(convert_charrefs=True)
                self.parts: list[str] = []

            def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
                if tag.casefold() in {
                    "br",
                    "p",
                    "div",
                    "tr",
                    "td",
                    "th",
                    "li",
                    "section",
                    "h1",
                    "h2",
                    "h3",
                }:
                    self.parts.append(" ")

            def handle_endtag(self, tag: str) -> None:
                if tag.casefold() in {"p", "div", "tr", "li", "section", "h1", "h2", "h3"}:
                    self.parts.append(" ")

            def handle_data(self, data: str) -> None:
                self.parts.append(data)

        parser = VisibleTextParser()
        try:
            parser.feed(text)
            parser.close()
        except Exception:
            return None
        return re.sub(r"\s+", " ", "".join(parser.parts)).strip()
    return text


def _extract_document_links(body: bytes, media_type: str) -> list[str]:
    if "html" not in media_type.casefold():
        return []

    class LinkParser(HTMLParser):
        def __init__(self) -> None:
            super().__init__(convert_charrefs=True)
            self.links: list[str] = []

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            if tag.casefold() == "a":
                href = dict(attrs).get("href")
                if href:
                    self.links.append(href)

    parser = LinkParser()
    try:
        parser.feed(body.decode("utf-8", errors="replace"))
        parser.close()
    except Exception:
        return []
    return parser.links


def _submission_shard_names(body: object) -> set[str] | None:
    if not isinstance(body, bytes):
        return None
    try:
        value = json.loads(body)
    except (UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, Mapping):
        return None
    filings = value.get("filings")
    if not isinstance(filings, Mapping):
        return None
    files = filings.get("files")
    if not isinstance(files, list):
        return None
    names: set[str] = set()
    for row in files:
        if not isinstance(row, Mapping) or not isinstance(row.get("name"), str):
            return None
        names.add(row["name"])
    return names


def _document_host_allowed(company_key: str, role: object, hostname: str | None) -> bool:
    if hostname is None or not isinstance(role, str):
        return False
    host = hostname.casefold().rstrip(".")
    is_sec = _matches_host(host, _SEC_HOSTS)
    is_hkex = _matches_host(host, _HKEX_HOSTS)
    issuer_hosts = _ISSUER_HOSTS.get(company_key, frozenset())
    is_issuer = _matches_host(host, issuer_hosts)
    if role in {"sec_companyfacts", "submissions_index", "sec_shard", "sec_filing"}:
        return is_sec
    if role == "issuer_annual":
        return is_issuer or is_hkex
    if role == "official_index":
        return is_issuer or is_hkex or is_sec
    # Provider response materials are retained separately from official facts.
    # Their source host does not promote them to field evidence.
    return True


def _matches_host(host: str, domains: Sequence[str] | frozenset[str]) -> bool:
    return any(host == domain or host.endswith(f".{domain}") for domain in domains)


def _sec_annual_filings(
    documents: Mapping[str, Mapping[str, Any]],
) -> tuple[list[tuple[date, date, str]], list[str]]:
    rows: list[tuple[date, date, str]] = []
    problems: list[str] = []
    for doc_id, document in documents.items():
        if not isinstance(document.get("role"), str) or document.get("role") not in {
            "submissions_index",
            "sec_shard",
            "submissions_shard",
        }:
            continue
        body = document.get("_body")
        if not isinstance(body, bytes):
            problems.append(f"SEC filing window document {doc_id} has no raw body")
            continue
        try:
            payload = json.loads(body)
        except (UnicodeError, json.JSONDecodeError):
            problems.append(f"SEC filing window document {doc_id} is not valid JSON")
            continue
        if not isinstance(payload, Mapping):
            problems.append(f"SEC filing window document {doc_id} must be a JSON object")
            continue
        filings = payload.get("filings")
        if document.get("role") == "submissions_index":
            table = filings.get("recent") if isinstance(filings, Mapping) else None
        else:
            # SEC historical submissions JSON stores the filing columns at the
            # top level (accessionNumber/form/reportDate/filingDate).
            table = (
                payload
                if "accessionNumber" in payload
                else (
                    filings
                    if isinstance(filings, Mapping) and "accessionNumber" in filings
                    else filings.get("recent")
                    if isinstance(filings, Mapping)
                    else None
                )
            )
        if not isinstance(table, Mapping):
            problems.append(f"SEC filing window document {doc_id} has no filing table")
            continue
        forms = table.get("form")
        reports = table.get("reportDate")
        filed = table.get("filingDate")
        if (
            not isinstance(forms, list)
            or not isinstance(reports, list)
            or not isinstance(filed, list)
        ):
            problems.append(
                f"SEC filing window document {doc_id} lacks form/reportDate/filingDate arrays"
            )
            continue
        if not (len(forms) == len(reports) == len(filed)):
            problems.append(
                f"SEC filing window document {doc_id} has mismatched filing array lengths"
            )
            continue
        for form, report_date, filing_date in zip(forms, reports, filed, strict=True):
            if form not in _SEC_ANNUAL_FORMS:
                continue
            period_end, published = _date(report_date), _date(filing_date)
            if period_end is None or published is None:
                problems.append(f"SEC annual filing in {doc_id} has invalid reportDate/filingDate")
                continue
            rows.append((period_end, published, form))
    return rows, problems


def _check_sec_latest_annual(
    annual: Mapping[str, Any] | None,
    documents: Mapping[str, Mapping[str, Any]],
    as_of: date,
    issues: list[str],
) -> bool:
    issue_count = len(issues)
    rows, problems = _sec_annual_filings(documents)
    issues.extend(problems)
    eligible = [row for row in rows if row[0] <= as_of and row[1] <= as_of]
    if not eligible:
        issues.append("saved SEC submissions/shards contain no annual filing available by as_of")
        return False
    latest = max(eligible, key=lambda row: (row[0], row[1]))
    if annual is None:
        issues.append("latest_annual is missing the SEC selected filing comparison")
        return False
    if _date(annual.get("period_end")) != latest[0]:
        issues.append("latest_annual period_end disagrees with latest SEC reportDate")
    if _date(annual.get("published_on")) != latest[1]:
        issues.append("latest_annual published_on disagrees with latest SEC filingDate")
    if annual.get("form") != latest[2]:
        issues.append("latest_annual form disagrees with the latest SEC annual form")
    return len(issues) == issue_count


def _check_latest_discovery(
    value: object,
    documents: Mapping[str, Mapping[str, Any]],
    period_end: date | None,
    form: object,
    published_on: date | None,
    annual_document_url: object,
    publication_evidence: object,
    requires_sec_shards: bool,
    as_of: date,
    issues: list[str],
) -> bool:
    issue_count = len(issues)
    if not isinstance(value, Mapping):
        issues.append("latest_annual.discovery is required to prove latest known annual")
        return False
    doc_id = value.get("document_id")
    doc = documents.get(doc_id) if isinstance(doc_id, str) else None
    if doc is None or not doc.get("_valid"):
        issues.append("latest_annual.discovery references a missing or invalid index document")
        return False
    if not isinstance(doc.get("role"), str) or doc.get("role") not in {
        "official_index",
        "submissions_index",
    }:
        issues.append("latest_annual.discovery must cite an official index or SEC submissions")
    if requires_sec_shards and doc.get("role") != "submissions_index":
        issues.append(
            "SEC-bound latest_annual.discovery must use the saved submissions index arrays"
        )
    checked_through = _date(value.get("checked_through"))
    if checked_through != as_of:
        issues.append("latest_annual.discovery.checked_through must equal as_of")
    captured = _datetime(doc.get("captured_at"))
    if captured is not None and captured.date() > as_of:
        issues.append("latest_annual discovery index was captured after as_of")
    if doc.get("role") == "submissions_index":
        # SEC latestness is derived from the saved aligned arrays, including
        # every listed historical shard; a free-form excerpt is not evidence.
        rows, problems = _sec_annual_filings(documents)
        issues.extend(problems)
        eligible = [row for row in rows if row[0] <= as_of and row[1] <= as_of]
        if not eligible:
            issues.append("saved SEC submissions contain no annual filing available by as_of")
        else:
            selected = max(eligible, key=lambda row: (row[0], row[1]))
            if selected[0] != period_end or selected[1] != published_on or selected[2] != form:
                issues.append(
                    "latest_annual discovery disagrees with latest saved SEC filing arrays"
                )
        return (
            checked_through == as_of
            and doc.get("_valid") is True
            and doc.get("role") == "submissions_index"
            and len(issues) == issue_count
        )
    locator = value.get("locator")
    excerpt = value.get("excerpt")
    context = _located_context(doc, locator)
    if context is None:
        issues.append("latest_annual.discovery.locator is required")
    if not isinstance(excerpt, str) or not excerpt.strip():
        issues.append("latest_annual.discovery.excerpt is required")
        return False
    if context is None or excerpt not in context:
        issues.append("latest_annual.discovery excerpt does not occur in the original index text")
        return False
    if "annual" not in excerpt.casefold():
        issues.append("latest_annual.discovery excerpt must identify an annual report")
    if period_end is None or str(period_end.year) not in excerpt:
        issues.append("latest_annual.discovery excerpt does not identify the selected annual year")
    linked_url = value.get("link_url")
    indexed_url = doc.get("url")
    annual_url = annual_document_url
    actual_links = doc.get("_links")
    try:
        resolved_link = urljoin(str(indexed_url), str(linked_url))
    except ValueError:
        resolved_link = ""
    if (
        not isinstance(linked_url, str)
        or not isinstance(annual_url, str)
        or resolved_link != annual_url
        or not isinstance(actual_links, list)
        or linked_url not in actual_links
    ):
        issues.append(
            "latest_annual.discovery link_url must be an original index link to the selected annual document"
        )
    if publication_evidence is None:
        publication_evidence = value.get("publication_evidence")
    index_host = urlparse(str(doc.get("url", ""))).hostname or ""
    date_in_index = published_on is not None and _contains_source_date(
        excerpt, published_on, day_first_numeric=_matches_host(index_host, _HKEX_HOSTS)
    )
    if not date_in_index and not _check_publication_evidence(
        publication_evidence, documents, published_on, as_of, issues
    ):
        issues.append("latest_annual.discovery lacks source evidence for publication date")
    if not isinstance(locator, Mapping) or locator.get("kind") not in {"pdf", "text"}:
        issues.append("latest_annual.discovery locator must be a structured pdf or text locator")
    if captured is None:
        issues.append("latest_annual discovery index capture time is unknown")
    return (
        doc.get("role") in {"official_index", "submissions_index"}
        and checked_through == as_of
        and len(issues) == issue_count
    )


def _check_publication_evidence(
    value: object,
    documents: Mapping[str, Mapping[str, Any]],
    published_on: date | None,
    as_of: date,
    issues: list[str],
) -> bool:
    before = len(issues)
    if not isinstance(value, Mapping):
        return False
    doc_id = value.get("document_id")
    doc = documents.get(doc_id) if isinstance(doc_id, str) else None
    if doc is None or not doc.get("_valid"):
        issues.append(
            "latest_annual.discovery.publication_evidence references a missing or invalid document"
        )
        return False
    locator, excerpt = value.get("locator"), value.get("excerpt")
    context = _located_context(doc, locator)
    if context is None or not isinstance(excerpt, str) or excerpt not in context:
        issues.append(
            "latest_annual.discovery.publication_evidence excerpt is not at its source locator"
        )
        return False
    evidence_date = _date(value.get("published_on"))
    if evidence_date is None or evidence_date != published_on or evidence_date > as_of:
        issues.append(
            "latest_annual.discovery.publication_evidence date disagrees with latest_annual"
        )
    else:
        evidence_host = urlparse(str(doc.get("url", ""))).hostname or ""
        if not _contains_source_date(
            excerpt, evidence_date, day_first_numeric=_matches_host(evidence_host, _HKEX_HOSTS)
        ):
            issues.append(
                "latest_annual.discovery.publication_evidence excerpt does not contain the publication date"
            )
    return len(issues) == before


def _located_context(document: Mapping[str, Any], locator: object) -> str | None:
    """Return text belonging to a structured PDF page or text anchor."""
    text = document.get("_text")
    if not isinstance(text, str) or not isinstance(locator, Mapping):
        return None
    kind = locator.get("kind")
    if kind == "pdf":
        page = locator.get("page")
        if type(page) is not int or page < 1:
            return None
        pages = text.split("\f")
        if page > len(pages):
            return None
        context = pages[page - 1]
        for key in ("section", "row", "column"):
            value = locator.get(key)
            if value is not None and (
                not isinstance(value, str)
                or _normalize_source_whitespace(value).casefold()
                not in _normalize_source_whitespace(context).casefold()
            ):
                return None
        return context
    if kind == "text":
        anchor = locator.get("anchor")
        if (
            not isinstance(anchor, str)
            or not anchor.strip()
            or anchor.casefold() not in text.casefold()
        ):
            return None
        # Keep adjacent source context around the navigation anchor.
        # Prefer the exact source spelling: a case-insensitive earlier summary
        # can have different adjacent columns than the reviewed statement row.
        offset = text.find(anchor)
        if offset < 0:
            offset = text.casefold().find(anchor.casefold())
        return text[max(0, offset - 500) : offset + len(anchor) + 1000]
    return None


def _contains_source_date(text: str, expected: date, *, day_first_numeric: bool = False) -> bool:
    """Recognize common literal publication-date formats in issuer material."""
    if expected.isoformat() in text:
        return True
    if day_first_numeric:
        for candidate in re.findall(r"\b\d{1,2}/\d{1,2}/\d{4}\b", text):
            try:
                if datetime.strptime(candidate, "%d/%m/%Y").date() == expected:
                    return True
            except ValueError:
                continue
    candidates = re.findall(
        r"\b(?:\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}|[A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4})\b",
        text,
    )
    for candidate in candidates:
        for date_format in ("%d %B %Y", "%d %b %Y", "%B %d %Y", "%b %d %Y"):
            try:
                if datetime.strptime(candidate.replace(",", ""), date_format).date() == expected:
                    return True
            except ValueError:
                continue
    return False


def _latest_annual_summary(
    entry: Mapping[str, Any], documents: Mapping[str, Mapping[str, Any]]
) -> dict[str, Any] | None:
    annual = entry.get("latest_annual")
    if not isinstance(annual, Mapping):
        return None
    document_id = annual.get("document_id")
    document = documents.get(document_id) if isinstance(document_id, str) else None
    return {
        "period_start": annual.get("period_start"),
        "period_end": annual.get("period_end"),
        "published_on": annual.get("published_on"),
        "form": annual.get("form"),
        "document_id": document_id,
        "document_path": None if document is None else document.get("path"),
        "document_sha256": None if document is None else document.get("sha256"),
        "date_precision": "date" if annual.get("published_on") is not None else "unknown",
    }


def _latestness_method(
    entry: Mapping[str, Any] | None,
    documents: Mapping[str, Mapping[str, Any]],
) -> dict[str, str] | None:
    if entry is None:
        return None
    annual = entry.get("latest_annual")
    discovery = annual.get("discovery") if isinstance(annual, Mapping) else None
    doc_id = discovery.get("document_id") if isinstance(discovery, Mapping) else None
    doc = documents.get(doc_id) if isinstance(doc_id, str) else None
    if doc is not None and doc.get("role") == "submissions_index":
        return {
            "method": "saved_sec_submissions_recent_and_historical_shards",
            "limitation": "Latest annual filing is derived from the captured aligned SEC form/reportDate/filingDate arrays.",
        }
    return {
        "method": "reviewed_official_issuer_index",
        "limitation": "Issuer-index completeness is a source-grounded reviewer attestation; the verifier cannot independently prove that the saved index omitted no newer annual link.",
    }


def _document_summaries(
    documents: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    return [
        {
            "id": doc_id,
            "role": document.get("role"),
            "path": document.get("path"),
            "url": document.get("url"),
            "sha256": document.get("sha256"),
            "bytes": document.get("bytes"),
            "captured_at": document.get("captured_at"),
            "http_status": document.get("http_status"),
            "media_type": document.get("media_type"),
            "published_on": document.get("published_on"),
            "verified": bool(document.get("_valid")),
        }
        for doc_id, document in sorted(documents.items())
    ]
