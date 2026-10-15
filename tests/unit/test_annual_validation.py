from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

from investment_agent.domain.security import DEFAULT_PILOT_LIST_PATH, load_pilot_identities
from investment_agent.materials.annual_validation import (
    _contains_exact_amount,
    _contains_source_date,
    verify_annual_package,
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

AS_OF = date(2026, 10, 15)
IDENTITIES = load_pilot_identities(DEFAULT_PILOT_LIST_PATH)
METRICS = {
    "revenue": "revenue_operating",
    "profit": "profit_parent",
    "assets": "total_assets",
    "equity": "equity_parent",
    "operating_cash_flow": "operating_cash_flow",
    "capex": "capex_ppe",
}
HOSTS = {
    "clp-holdings": "clpgroup.com",
    "hsbc-holdings": "hsbc.com",
    "tencent-holdings": "tencent.com",
    "apple": "apple.com",
    "costco": "costco.com",
    "jpmorgan-chase": "jpmorganchase.com",
    "exxon-mobil": "exxonmobil.com",
    "tsmc": "tsmc.com",
}


def _make_package(
    tmp_path: Path,
    *,
    company_keys: list[str] | None = None,
    apple_weeks: bool = False,
) -> tuple[Path, Path, dict[str, Any]]:
    selected = company_keys or [identity.company_key for identity in IDENTITIES]
    by_key = {identity.company_key: identity for identity in IDENTITIES}
    root = tmp_path / "raw"
    root.mkdir()
    companies = []
    for company_key in selected:
        identity = by_key[company_key]
        if apple_weeks and company_key == "apple":
            bounds = [
                (date(2021, 9, 26), date(2022, 9, 24)),
                (date(2022, 9, 25), date(2023, 9, 30)),
                (date(2023, 10, 1), date(2024, 9, 28)),
                (date(2024, 9, 29), date(2025, 9, 27)),
            ]
        else:
            bounds = [(date(year, 1, 1), date(year, 12, 31)) for year in range(2022, 2026)]
        documents = []
        rows = []
        equity_values: list[str] = []
        for year_index, (start, end) in enumerate(bounds):
            previous_equity = str(90 + 10 * year_index)
            ending_equity = str(100 + 10 * year_index)
            equity_values.append(ending_equity)
            year_label = f"FY{end.year}"
            published = date(end.year + 1, 2, 15)
            path = f"{company_key}/{end.year}-annual.txt"
            amounts = {
                "revenue": str(1000 + 100 * year_index),
                "profit": str(120 + 10 * year_index),
                "assets": str(5000 + 100 * year_index),
                "equity": ending_equity,
                "operating_cash_flow": str(200 + 10 * year_index),
                "capex": str(40 + year_index),
            }
            text_parts = [
                f"Official annual report {company_key} {year_label} published {published}"
            ]
            for category, value in amounts.items():
                text_parts.append(f"{category} {value} {identity.report_currency}")
            text_parts.append(f"beginning equity {previous_equity} {identity.report_currency}")
            body = "\n".join(text_parts).encode()
            (root / path).parent.mkdir(parents=True, exist_ok=True)
            (root / path).write_bytes(body)
            doc_id = f"{company_key}-{end.year}"
            documents.append(
                {
                    "id": doc_id,
                    "path": path,
                    "url": f"https://www.{HOSTS[company_key]}/{company_key}/{end.year}.pdf",
                    "sha256": hashlib.sha256(body).hexdigest(),
                    "bytes": len(body),
                    "captured_at": f"{end.year + 1}-02-16T01:00:00Z",
                    "http_status": 200,
                    "media_type": "text/plain",
                    "published_on": published.isoformat(),
                    "role": "issuer_annual",
                }
            )
            year_facts = []
            for category, amount in amounts.items():
                fact_doc_hash = hashlib.sha256(body).hexdigest()
                fact = _fact(
                    metric=METRICS[category],
                    start=start,
                    end=end,
                    currency=identity.report_currency,
                    amount=amount,
                    published=published,
                    original_hash=fact_doc_hash,
                    locator={
                        "kind": "text",
                        "anchor": f"{category} {amount} {identity.report_currency}",
                    },
                    source_label=category,
                )
                year_facts.append(
                    {
                        "category": category,
                        "fact": fact_to_payload(fact),
                        "evidence": {
                            "document_id": doc_id,
                            "locator": {
                                "kind": "text",
                                "anchor": f"{category} {amount} {identity.report_currency}",
                            },
                            "excerpt": f"{category} {amount} {identity.report_currency}",
                            "source_label": category,
                        },
                    }
                )
            beginning_fact = _fact(
                metric="equity_parent",
                start=None,
                end=start - timedelta(days=1),
                currency=identity.report_currency,
                amount=previous_equity,
                published=published,
                original_hash=hashlib.sha256(body).hexdigest(),
                locator={
                    "kind": "text",
                    "anchor": f"beginning equity {previous_equity} {identity.report_currency}",
                },
                basis=PeriodBasis.INSTANT,
                source_label="beginning equity",
            )
            rows.append(
                {
                    "label": year_label,
                    "period_start": start.isoformat(),
                    "period_end": end.isoformat(),
                    "facts": year_facts,
                    "beginning_equity_fact": {
                        "category": "equity",
                        "fact": fact_to_payload(beginning_fact),
                        "evidence": {
                            "document_id": doc_id,
                            "locator": {
                                "kind": "text",
                                "anchor": f"beginning equity {previous_equity} {identity.report_currency}",
                            },
                            "excerpt": f"beginning equity {previous_equity} {identity.report_currency}",
                            "source_label": "beginning equity",
                        },
                    },
                }
            )
        shard_name = f"{company_key}-submissions-shard.json"
        sec_doc_path = f"{company_key}/{shard_name}"
        sec_body = json.dumps(
            {"accessionNumber": [], "form": [], "reportDate": [], "filingDate": []}
        ).encode()
        (root / sec_doc_path).write_bytes(sec_body)
        documents.append(
            {
                "id": f"{company_key}-shard",
                "path": sec_doc_path,
                "url": f"https://www.sec.gov/Archives/{company_key}/shard.json",
                "sha256": hashlib.sha256(sec_body).hexdigest(),
                "bytes": len(sec_body),
                "captured_at": "2026-10-14T01:00:00Z",
                "http_status": 200,
                "media_type": "application/json",
                "published_on": None,
                "role": "sec_shard",
            }
        )
        submissions_path = f"{company_key}/submissions.json"
        latest_form = "20-F" if identity.company_key == "tsmc" else "10-K"
        submissions_body = json.dumps(
            {
                "filings": {
                    "files": [{"name": shard_name}],
                    "recent": {
                        "form": [latest_form],
                        "reportDate": [bounds[-1][1].isoformat()],
                        "filingDate": [date(bounds[-1][1].year + 1, 2, 15).isoformat()],
                    },
                }
            }
        ).encode()
        (root / submissions_path).write_bytes(submissions_body)
        documents.append(
            {
                "id": f"{company_key}-submissions",
                "path": submissions_path,
                "url": f"https://www.sec.gov/Archives/{company_key}/submissions.json",
                "sha256": hashlib.sha256(submissions_body).hexdigest(),
                "bytes": len(submissions_body),
                "captured_at": "2026-10-14T01:00:00Z",
                "http_status": 200,
                "media_type": "application/json",
                "published_on": None,
                "role": "submissions_index",
            }
        )
        if any(binding.source == "sec.companyfacts.v1" for binding in identity.sources):
            facts_path = f"{company_key}/companyfacts.json"
            facts_body = b'{"facts":{}}'
            (root / facts_path).write_bytes(facts_body)
            documents.append(
                {
                    "id": f"{company_key}-companyfacts",
                    "path": facts_path,
                    "url": f"https://www.sec.gov/Archives/{company_key}/companyfacts.json",
                    "sha256": hashlib.sha256(facts_body).hexdigest(),
                    "bytes": len(facts_body),
                    "captured_at": "2026-10-14T01:00:00Z",
                    "http_status": 200,
                    "media_type": "application/json",
                    "published_on": None,
                    "role": "sec_companyfacts",
                }
            )
        index_path = f"{company_key}/official-index.html"
        index_excerpt = f"Annual Report {bounds[-1][1].year}"
        link_url = f"https://www.{HOSTS[company_key]}/{company_key}/{bounds[-1][1].year}.pdf"
        index_body = (
            f'<html><body><a href="{link_url}">{index_excerpt}</a> '
            f"published {date(bounds[-1][1].year + 1, 2, 15).isoformat()}</body></html>"
        ).encode()
        (root / index_path).write_bytes(index_body)
        documents.append(
            {
                "id": f"{company_key}-index",
                "path": index_path,
                "url": f"https://www.{HOSTS[company_key]}/{company_key}/filings",
                "sha256": hashlib.sha256(index_body).hexdigest(),
                "bytes": len(index_body),
                "captured_at": "2026-10-15T01:00:00Z",
                "http_status": 200,
                "media_type": "text/html",
                "published_on": None,
                "role": "official_index",
            }
        )
        latest_row = rows[-1]
        latest_document = next(
            doc for doc in documents if doc["id"] == f"{company_key}-{bounds[-1][1].year}"
        )
        is_sec = any(binding.source == "sec.companyfacts.v1" for binding in identity.sources)
        discovery_id = f"{company_key}-submissions" if is_sec else f"{company_key}-index"
        companies.append(
            {
                "company_key": company_key,
                "report_currency": identity.report_currency,
                "latest_annual": {
                    "period_start": latest_row["period_start"],
                    "period_end": latest_row["period_end"],
                    "published_on": latest_document["published_on"],
                    "document_id": latest_document["id"],
                    "form": latest_form,
                    "discovery": {
                        "document_id": discovery_id,
                        "checked_through": AS_OF.isoformat(),
                        **(
                            {}
                            if is_sec
                            else {
                                "locator": {"kind": "text", "anchor": index_excerpt},
                                "excerpt": f"{index_excerpt} published {date(bounds[-1][1].year + 1, 2, 15).isoformat()}",
                                "link_url": link_url,
                            }
                        ),
                    },
                },
                "documents": documents,
                "fields": latest_row["facts"],
                "fiscal_years": rows,
                "expected_shards": (
                    [{"path": sec_doc_path, "document_id": f"{company_key}-shard"}]
                    if any(binding.source == "sec.companyfacts.v1" for binding in identity.sources)
                    else []
                ),
            }
        )
    manifest = {"schema_version": 1, "as_of": AS_OF.isoformat(), "companies": companies}
    manifest_path = tmp_path / "annual.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return manifest_path, root, manifest


def _fact(
    *,
    metric: str,
    start: date | None,
    end: date,
    currency: str,
    amount: str,
    published: date,
    original_hash: str,
    locator: str,
    basis: PeriodBasis = PeriodBasis.ANNUAL,
    source_label: str = "issuer annual report",
) -> FactRecord:
    return FactRecord(
        metric=metric,
        period=FactPeriod(
            None
            if basis == PeriodBasis.INSTANT
            or metric in {"total_assets", "equity_parent", "equity_ordinary"}
            else start,
            end,
            PeriodBasis.INSTANT
            if metric in {"total_assets", "equity_parent", "equity_ordinary"}
            else basis,
            label=f"FY{end.year}",
        ),
        semantics=FactSemantics(
            metric=metric,
            source_label=source_label,
            source_field=source_label,
            accounting_standard=AccountingStandard.US_GAAP,
            reporting_scope=ReportingScope.CONSOLIDATED,
            equity_scope=EquityScope.ALL_PARENT,
            per_share_basis=PerShareBasis.NA,
        ),
        value=FactValue(amount, currency, 1, currency, currency, "reported sign"),
        provenance=FactProvenance(
            source="issuer annual report",
            accession=None,
            original_hash=original_hash,
            source_published_at=None,
            source_published_date=published,
            source_published_precision=DatePrecision.DATE,
            accepted_at=None,
            source_available_at=None,
        ),
        verification=Verification.VERIFIED,
        conflict="none",
        applicability="applicable",
        original_locator=json.dumps(locator, sort_keys=True, separators=(",", ":"))
        if isinstance(locator, dict)
        else locator,
    )


def test_eight_complete_companies_are_required_for_full_completion(tmp_path: Path) -> None:
    manifest_path, source_root, _ = _make_package(tmp_path)

    report = verify_annual_package(manifest_path, source_root)

    assert report["complete"] is True
    assert report["status"] == "complete"
    assert len(report["companies"]) == 8
    assert all(item["status"] == "complete" for item in report["companies"])


def test_explicit_subset_can_be_validated_but_never_marks_eight_company_package_complete(
    tmp_path: Path,
) -> None:
    manifest_path, source_root, _ = _make_package(tmp_path, company_keys=["apple"])

    report = verify_annual_package(manifest_path, source_root, identities=["apple"])

    assert report["subset_complete"] is True
    assert report["complete"] is False
    assert report["scope"] == "explicit_subset"


def test_missing_or_tampered_source_and_missing_shard_remain_partial(tmp_path: Path) -> None:
    manifest_path, source_root, manifest = _make_package(tmp_path, company_keys=["apple"])
    company = manifest["companies"][0]
    doc = company["documents"][0]
    (source_root / doc["path"]).write_bytes(b"tampered body")
    company["expected_shards"][0]["path"] = "apple/missing-shard.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    report = verify_annual_package(manifest_path, source_root, identities=["apple"])
    company_report = report["companies"][0]

    assert company_report["status"] == "partial"
    assert any("sha256" in item for item in company_report["issues"])
    assert company_report["sec_shards"]["complete"] is False


def test_future_publication_and_wrong_period_are_rejected(tmp_path: Path) -> None:
    manifest_path, source_root, manifest = _make_package(tmp_path, company_keys=["apple"])
    company = manifest["companies"][0]
    company["latest_annual"]["published_on"] = "2027-01-01"
    fact = company["fields"][0]["fact"]
    fact["period"]["basis"] = "interim"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    report = verify_annual_package(manifest_path, source_root, identities=["apple"])
    issues = " ".join(report["companies"][0]["issues"])

    assert "published after" in issues
    assert "annual period basis" in issues


def test_currency_mismatch_and_float_amount_do_not_validate(tmp_path: Path) -> None:
    manifest_path, source_root, manifest = _make_package(tmp_path, company_keys=["hsbc-holdings"])
    company = manifest["companies"][0]
    fact = company["fields"][0]["fact"]
    fact["value"]["currency"] = "HKD"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    report = verify_annual_package(manifest_path, source_root, identities=["hsbc-holdings"])
    issues = " ".join(report["companies"][0]["issues"])

    assert "currency" in issues
    company["fields"][0]["fact"]["value"]["raw_value"] = 1.25
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    float_report = verify_annual_package(manifest_path, source_root, identities=["hsbc-holdings"])
    assert "raw_value must be a decimal string" in " ".join(float_report["companies"][0]["issues"])


def test_52_53_week_years_and_beginning_equity_are_preserved(tmp_path: Path) -> None:
    manifest_path, source_root, _ = _make_package(
        tmp_path, company_keys=["apple"], apple_weeks=True
    )

    report = verify_annual_package(manifest_path, source_root, identities=["apple"])
    company = report["companies"][0]

    assert company["continuous_four_years"] is True
    assert company["roe_beginning_equity_complete"] is True
    assert [item["period_end"] for item in company["fiscal_years"]] == [
        "2022-09-24",
        "2023-09-30",
        "2024-09-28",
        "2025-09-27",
    ]


def test_missing_beginning_equity_blocks_roe_coverage(tmp_path: Path) -> None:
    manifest_path, source_root, manifest = _make_package(tmp_path, company_keys=["jpmorgan-chase"])
    manifest["companies"][0]["fiscal_years"][0]["beginning_equity_fact"] = None
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    report = verify_annual_package(manifest_path, source_root, identities=["jpmorgan-chase"])

    assert report["companies"][0]["roe_beginning_equity_complete"] is False
    assert any("beginning_equity_fact" in issue for issue in report["companies"][0]["issues"])


def test_path_escape_and_symlink_are_rejected(tmp_path: Path) -> None:
    manifest_path, source_root, manifest = _make_package(tmp_path, company_keys=["apple"])
    company = manifest["companies"][0]
    outside = tmp_path / "outside.txt"
    outside.write_text("revenue 1000 USD", encoding="utf-8")
    (source_root / "apple" / "escape.txt").symlink_to(outside)
    field = company["fields"][0]
    company["documents"].append(
        {
            "id": "escaped",
            "path": "apple/escape.txt",
            "url": "https://www.apple.com/escape",
            "sha256": hashlib.sha256(outside.read_bytes()).hexdigest(),
            "bytes": outside.stat().st_size,
            "captured_at": "2026-10-15T01:00:00Z",
            "http_status": 200,
            "media_type": "text/plain",
            "role": "issuer_annual",
        }
    )
    field["evidence"]["document_id"] = "escaped"
    field["evidence"]["excerpt"] = "revenue 1000 USD"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    report = verify_annual_package(manifest_path, source_root, identities=["apple"])

    assert any(
        "resolves outside source_root" in issue for issue in report["companies"][0]["issues"]
    )
    assert report["companies"][0]["status"] == "partial"


@pytest.mark.parametrize("failure", ["http", "missing_body", "missing_citation", "missing_year"])
def test_incomplete_documents_or_coverage_cannot_be_promoted(tmp_path: Path, failure: str) -> None:
    manifest_path, source_root, manifest = _make_package(tmp_path, company_keys=["apple"])
    company = manifest["companies"][0]
    if failure == "http":
        company["documents"][0]["http_status"] = 503
    elif failure == "missing_body":
        (source_root / company["documents"][0]["path"]).unlink()
    elif failure == "missing_citation":
        company["fields"][0]["evidence"]["document_id"] = "does-not-exist"
    else:
        company["fiscal_years"].pop(0)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    report = verify_annual_package(manifest_path, source_root, identities=["apple"])
    result = report["companies"][0]

    assert result["status"] == "partial"
    assert report["complete"] is False


def test_duplicate_period_and_declared_source_conflict_are_visible(tmp_path: Path) -> None:
    manifest_path, source_root, manifest = _make_package(tmp_path, company_keys=["apple"])
    company = manifest["companies"][0]
    duplicate = json.loads(json.dumps(company["fiscal_years"][-1]))
    company["fiscal_years"].append(duplicate)
    company["conflicts"] = [{"reason": "reporting source values differ"}]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    report = verify_annual_package(manifest_path, source_root, identities=["apple"])
    issues = " ".join(report["companies"][0]["issues"])

    assert "duplicate fiscal-year" in issues
    assert "conflicts" in issues
    assert report["companies"][0]["annual_inputs_complete"] is False


def test_sec_shards_are_derived_from_submissions_and_latest_needs_index_proof(
    tmp_path: Path,
) -> None:
    manifest_path, source_root, manifest = _make_package(tmp_path, company_keys=["apple"])
    company = manifest["companies"][0]
    company["expected_shards"] = []
    company["latest_annual"].pop("discovery")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    report = verify_annual_package(manifest_path, source_root, identities=["apple"])
    result = report["companies"][0]
    issues = " ".join(result["issues"])

    assert result["sec_shards"]["complete"] is False
    assert "submissions_index" in issues or "shard manifest disagrees" in issues
    assert "discovery" in issues
    assert result["latest_annual_checked"] is False


def test_sec_historical_shard_top_level_arrays_extend_latest_filing_window(tmp_path: Path) -> None:
    manifest_path, source_root, manifest = _make_package(tmp_path, company_keys=["apple"])
    company = manifest["companies"][0]
    latest_end = company["latest_annual"]["period_end"]
    latest_published = company["latest_annual"]["published_on"]
    shard = next(doc for doc in company["documents"] if doc["role"] == "sec_shard")
    shard_body = json.dumps(
        {
            "accessionNumber": ["0000320193-25-000079"],
            "form": ["10-K"],
            "reportDate": [latest_end],
            "filingDate": [latest_published],
        }
    ).encode()
    (source_root / shard["path"]).write_bytes(shard_body)
    shard["sha256"] = hashlib.sha256(shard_body).hexdigest()
    shard["bytes"] = len(shard_body)
    submissions = next(doc for doc in company["documents"] if doc["role"] == "submissions_index")
    submissions_body = json.dumps(
        {
            "filings": {
                "files": [{"name": Path(shard["path"]).name}],
                "recent": {
                    "form": ["10-K"],
                    "reportDate": ["2024-12-31"],
                    "filingDate": ["2025-02-15"],
                },
            }
        }
    ).encode()
    (source_root / submissions["path"]).write_bytes(submissions_body)
    submissions["sha256"] = hashlib.sha256(submissions_body).hexdigest()
    submissions["bytes"] = len(submissions_body)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = verify_annual_package(manifest_path, source_root, identities=["apple"])["companies"][0]

    assert result["sec_shards"]["complete"] is True
    assert result["latest_annual_checked"] is True


def test_document_locator_and_raw_source_coverage_are_auditable(tmp_path: Path) -> None:
    manifest_path, source_root, manifest = _make_package(tmp_path, company_keys=["apple"])
    company = manifest["companies"][0]
    company["fields"][0]["evidence"]["locator"] = {"kind": "pdf", "page": 999}
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    report = verify_annual_package(manifest_path, source_root, identities=["apple"])
    result = report["companies"][0]

    assert result["status"] == "partial"
    assert result["source_raw_coverage"]["verified"] == result["source_raw_coverage"]["listed"]
    assert any("identify a source PDF page or text anchor" in issue for issue in result["issues"])


def test_exact_amount_label_and_structured_locator_are_bound_to_source(tmp_path: Path) -> None:
    manifest_path, source_root, manifest = _make_package(tmp_path, company_keys=["apple"])
    field = manifest["companies"][0]["fields"][0]
    field["fact"]["value"]["raw_value"] = "100"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    report = verify_annual_package(manifest_path, source_root, identities=["apple"])
    issues = " ".join(report["companies"][0]["issues"])
    assert "exact canonical raw amount" in issues
    assert report["companies"][0]["status"] == "partial"


@pytest.mark.parametrize("change", ["year", "publication", "unrelated"])
def test_latest_discovery_requires_selected_row_form_and_publication_date(
    tmp_path: Path, change: str
) -> None:
    manifest_path, source_root, manifest = _make_package(tmp_path, company_keys=["clp-holdings"])
    company = manifest["companies"][0]
    discovery = company["latest_annual"]["discovery"]
    if change == "year":
        discovery["excerpt"] = discovery["excerpt"].replace(
            "Annual Report 2025", "Annual Report 2024"
        )
    elif change == "publication":
        discovery["excerpt"] = discovery["excerpt"].replace("2026-02-15", "2026-02-14")
    else:
        discovery["excerpt"] = "An unrelated filing row for 2025"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = verify_annual_package(manifest_path, source_root, identities=["clp-holdings"])[
        "companies"
    ][0]
    assert result["latest_annual_checked"] is False
    assert result["status"] == "partial"


def test_malformed_category_and_boolean_schema_version_are_partial_not_crashes(
    tmp_path: Path,
) -> None:
    manifest_path, source_root, manifest = _make_package(tmp_path, company_keys=["apple"])
    manifest["companies"][0]["fields"][0]["category"] = []
    manifest["schema_version"] = True
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    report = verify_annual_package(manifest_path, source_root, identities=["apple"])
    assert report["complete"] is False
    assert any("schema_version" in issue for issue in report["issues"])


@pytest.mark.parametrize("mutation", ["fields_null", "document_role_list"])
def test_malformed_company_collections_and_roles_are_partial_not_crashes(
    tmp_path: Path, mutation: str
) -> None:
    manifest_path, source_root, manifest = _make_package(tmp_path, company_keys=["apple"])
    company = manifest["companies"][0]
    if mutation == "fields_null":
        company["fields"] = None
    else:
        company["documents"][0]["role"] = []
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    report = verify_annual_package(manifest_path, source_root, identities=["apple"])

    assert report["complete"] is False
    issues = report["companies"][0]["issues"]
    assert any("fields" in issue or "role" in issue for issue in issues)


def test_missing_company_entry_is_reported_without_crashing(tmp_path: Path) -> None:
    manifest_path, source_root, manifest = _make_package(tmp_path, company_keys=["apple"])
    manifest["companies"] = []
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    report = verify_annual_package(manifest_path, source_root, identities=["apple"])

    assert report["complete"] is False
    assert any(
        "company is absent from manifest" in issue for issue in report["companies"][0]["issues"]
    )


def test_exact_amount_matching_preserves_multi_column_boundaries() -> None:
    excerpt = "Revenue 751,766 660,257"
    assert _contains_exact_amount(excerpt, "751766")
    assert _contains_exact_amount(excerpt, "660257")
    assert not _contains_exact_amount("Revenue 1234", "123")
    assert _contains_exact_amount("Loss (1,234)", "-1234")
    assert not _contains_exact_amount("Capital expenditure (1,234)", "1234")
    assert _contains_exact_amount(
        "Capital expenditure (1,234)", "1234", parenthesized_is_positive=True
    )


def test_source_date_matching_accepts_issuer_display_format() -> None:
    assert _contains_source_date(
        "Annual Report – Publication Dates Online: 12 March 2026", date(2026, 3, 12)
    )
    assert not _contains_source_date(
        "Annual Report – Publication Dates Online: 12 March 2026", date(2026, 3, 13)
    )
    assert _contains_source_date(
        "Release Time: 09/04/2026", date(2026, 4, 9), day_first_numeric=True
    )
    assert not _contains_source_date(
        "Release Time: 09/04/2026", date(2026, 9, 4), day_first_numeric=True
    )


def test_issuer_index_link_must_bind_to_selected_report(tmp_path: Path) -> None:
    manifest_path, source_root, manifest = _make_package(tmp_path, company_keys=["clp-holdings"])
    discovery = manifest["companies"][0]["latest_annual"]["discovery"]
    discovery["link_url"] = "https://www.tencent.com/annual/2024.pdf"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = verify_annual_package(manifest_path, source_root, identities=["clp-holdings"])[
        "companies"
    ][0]

    assert result["latest_annual_checked"] is False
    assert any("original index link" in issue for issue in result["issues"])


def test_issuer_publication_date_can_be_verified_at_a_separate_locator(tmp_path: Path) -> None:
    manifest_path, source_root, manifest = _make_package(tmp_path, company_keys=["clp-holdings"])
    company = manifest["companies"][0]
    discovery = company["latest_annual"]["discovery"]
    discovery["excerpt"] = "Annual Report 2025"
    annual_doc = next(
        doc for doc in company["documents"] if doc["id"] == company["latest_annual"]["document_id"]
    )
    publication_date = company["latest_annual"]["published_on"]
    body = (source_root / annual_doc["path"]).read_text(encoding="utf-8")
    date_line = next(line for line in body.splitlines() if publication_date in line)
    company["latest_annual"]["publication_evidence"] = {
        "document_id": annual_doc["id"],
        "locator": {"kind": "text", "anchor": date_line},
        "excerpt": date_line,
        "published_on": publication_date,
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = verify_annual_package(manifest_path, source_root, identities=["clp-holdings"])[
        "companies"
    ][0]

    assert result["latest_annual_checked"] is True


def test_core_formula_status_rejects_unscoped_net_profit_for_roe(tmp_path: Path) -> None:
    manifest_path, source_root, manifest = _make_package(tmp_path, company_keys=["apple"])
    company = manifest["companies"][0]
    profit_fields = [
        field
        for year in company["fiscal_years"]
        for field in year["facts"]
        if field["category"] == "profit"
    ]
    profit_fields.extend(field for field in company["fields"] if field["category"] == "profit")
    for field in profit_fields:
        field["fact"]["metric"] = "net_profit"
        field["fact"]["semantics"]["metric"] = "net_profit"
        field["fact"]["semantics"]["equity_scope"] = "total"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = verify_annual_package(manifest_path, source_root, identities=["apple"])["companies"][0]

    assert result["annual_category_inputs_complete"] is True
    assert result["annual_inputs_complete"] is False
    assert result["core_metrics"]["roe_avg_equity"][-1]["status"] == "missing_inputs"
    assert result["core_metrics"]["revenue_cagr_3y"]["status"] == "ok"
