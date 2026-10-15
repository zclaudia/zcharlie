from __future__ import annotations

import copy
import json
from datetime import date

from investment_agent.materials.annual_scope_validation import validate_scope_reviews
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

DOCUMENT_ID = "0000909832-23-000042"
DOCUMENT_HASH = "4ad0ed774dc46baca606e6d6db5f86d841211c3de97986861df71194e6bd8cc1"
DOCUMENT_URL = "https://www.sec.gov/Archives/edgar/data/909832/000090983223000042/cost-20230903.htm"
SOURCE_ROWS = [
    "Total Costco stockholders’ equity 25,058 20,642",
    "Noncontrolling interests — 5",
    "TOTAL EQUITY 25,058 20,647",
]


def _review_source_ref(
    *, context_ref: str, tag: str, source_label: str, excerpt: str
) -> dict[str, str]:
    return {
        "document_id": DOCUMENT_ID,
        "sha256": DOCUMENT_HASH,
        "published_on": "2023-10-11",
        "url": DOCUMENT_URL,
        "context_ref": context_ref,
        "tag": tag,
        "source_label": source_label,
        "locator": {"kind": "text", "anchor": excerpt.replace(" | ", " ")},
        "excerpt": excerpt,
    }


def _component(
    scope: str,
    amount: int,
    tag: str,
    label: str,
    excerpt: str,
    context_ref: str,
    *,
    notation: str | None = None,
) -> dict[str, object]:
    item: dict[str, object] = {
        "scope": scope,
        "amount": amount,
        "source_ref": _review_source_ref(
            context_ref=context_ref,
            tag=tag,
            source_label=label,
            excerpt=excerpt,
        ),
    }
    if notation is not None:
        item["amount_notation"] = notation
    return item


def _review_record() -> dict[str, object]:
    periods = []
    for end, context, parent, nci, total, notation in [
        ("2022-08-28", "c-13", 20_642, 5, 20_647, "reported"),
        ("2023-09-03", "c-12", 25_058, 0, 25_058, "em_dash_zero"),
    ]:
        periods.append(
            {
                "date": end,
                "context_ref": context,
                "parent": _component(
                    "parent",
                    parent,
                    "us-gaap:StockholdersEquity",
                    "Total Costco stockholders’ equity",
                    "Total Costco stockholders’ equity | 25,058 | 20,642",
                    context,
                ),
                "nci": _component(
                    "nci",
                    nci,
                    "us-gaap:MinorityInterest",
                    "Noncontrolling interests",
                    "Noncontrolling interests | — | 5",
                    context,
                    notation=notation,
                ),
                "total": _component(
                    "total",
                    total,
                    "us-gaap:StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
                    "TOTAL EQUITY",
                    "TOTAL EQUITY | 25,058 | 20,647",
                    context,
                ),
            }
        )
    return {
        "id": "costco.parent-equity.v1",
        "currency": "USD",
        "raw_unit": "USD million",
        "scale": 1_000_000,
        "period_type": "instant",
        "source_ref": {
            "document_id": DOCUMENT_ID,
            "sha256": DOCUMENT_HASH,
            "published_on": "2023-10-11",
            "url": DOCUMENT_URL,
        },
        "periods": periods,
    }


def _fact_field(
    *,
    end: str,
    amount: str,
    metric: str,
    scope: EquityScope,
    source_label: str,
    row: str,
) -> dict[str, object]:
    parsed_end = date.fromisoformat(end)
    locator = {"kind": "text", "anchor": row}
    fact = FactRecord(
        metric=metric,
        period=FactPeriod(
            start=None,
            end=parsed_end,
            basis=PeriodBasis.INSTANT,
            label=f"FY{parsed_end.year}",
        ),
        semantics=FactSemantics(
            metric=metric,
            source_label=source_label,
            source_field=source_label,
            accounting_standard=AccountingStandard.US_GAAP,
            reporting_scope=ReportingScope.CONSOLIDATED,
            equity_scope=scope,
            per_share_basis=PerShareBasis.NA,
        ),
        value=FactValue(
            raw_value=amount,
            raw_unit="USD million",
            scale=1_000_000,
            unit="USD",
            currency="USD",
            sign_convention="as_reported",
        ),
        provenance=FactProvenance(
            source="issuer.annual_report",
            accession=DOCUMENT_ID,
            original_hash=DOCUMENT_HASH,
            source_published_at=None,
            source_published_date=date(2023, 10, 11),
            source_published_precision=DatePrecision.DATE,
            accepted_at=None,
            source_available_at=None,
        ),
        verification=Verification.VERIFIED,
        conflict="none",
        applicability="applicable",
        original_locator=json.dumps(locator, sort_keys=True, separators=(",", ":")),
    )
    return {
        "category": "equity",
        "fact": fact_to_payload(fact),
        "evidence": {
            "document_id": DOCUMENT_ID,
            "locator": locator,
            "excerpt": row,
            "source_label": source_label,
        },
    }


def _entry() -> dict[str, object]:
    superseded = [
        _fact_field(
            end="2022-08-28",
            amount="20647",
            metric="total_equity",
            scope=EquityScope.TOTAL,
            source_label="TOTAL EQUITY",
            row="TOTAL EQUITY 25,058 20,647",
        ),
        _fact_field(
            end="2023-09-03",
            amount="25058",
            metric="total_equity",
            scope=EquityScope.TOTAL,
            source_label="TOTAL EQUITY",
            row="TOTAL EQUITY 25,058 20,647",
        ),
    ]
    selected_2022 = _fact_field(
        end="2022-08-28",
        amount="20642",
        metric="equity_parent",
        scope=EquityScope.ALL_PARENT,
        source_label="Total Costco stockholders’ equity",
        row="Total Costco stockholders’ equity 25,058 20,642",
    )
    selected_2023 = _fact_field(
        end="2023-09-03",
        amount="25058",
        metric="equity_parent",
        scope=EquityScope.ALL_PARENT,
        source_label="Total Costco stockholders’ equity",
        row="Total Costco stockholders’ equity 25,058 20,642",
    )
    return {
        "equity_scope_reviews": [_review_record()],
        "superseded_facts": superseded,
        "fiscal_years": [
            {"year": 2023, "facts": [selected_2023], "beginning_equity_fact": selected_2022},
            {"year": 2024, "facts": [], "beginning_equity_fact": copy.deepcopy(selected_2023)},
        ],
    }


def _documents() -> dict[str, dict[str, object]]:
    return {
        DOCUMENT_ID: {
            "id": DOCUMENT_ID,
            "role": "sec_filing",
            "url": DOCUMENT_URL,
            "sha256": DOCUMENT_HASH,
            "published_on": "2023-10-11",
            "_valid": True,
            "_text": "\n".join(SOURCE_ROWS),
        }
    }


def _validate(entry=None, docs=None):
    return validate_scope_reviews(
        entry if entry is not None else _entry(),
        docs if docs is not None else _documents(),
        "USD",
        date(2026, 10, 15),
    )


def test_valid_review_is_bound_to_source_text_and_both_fact_scopes() -> None:
    result, issues = _validate()
    assert not issues
    assert result["costco.parent-equity.v1"]["review_ref"].startswith("sha256:")


def test_invalid_source_document_flag_or_hash_rejects_review() -> None:
    docs = _documents()
    docs[DOCUMENT_ID]["_valid"] = False
    result, issues = _validate(docs=docs)
    assert result == {}
    assert any("source document is invalid" in issue for issue in issues)

    docs = _documents()
    docs[DOCUMENT_ID]["sha256"] = "0" * 64
    result, issues = _validate(docs=docs)
    assert result == {}
    assert any("SHA-256 does not match" in issue for issue in issues)


def test_locator_must_land_on_excerpt_in_document_context() -> None:
    entry = _entry()
    docs = _documents()
    docs[DOCUMENT_ID]["_text"] = (
        str(docs[DOCUMENT_ID]["_text"]) + " distant " * 120 + "locator anchor"
    )
    review = entry["equity_scope_reviews"][0]
    review["periods"][0]["parent"]["source_ref"]["locator"] = {
        "kind": "text",
        "anchor": "locator anchor",
    }
    result, issues = _validate(entry=entry, docs=docs)
    assert result == {}
    assert any("excerpt does not match located source context" in issue for issue in issues)


def test_selected_total_equity_instead_of_parent_scope_is_rejected() -> None:
    entry = _entry()
    field = entry["fiscal_years"][0]["facts"][0]
    payload = field["fact"]
    payload["semantics"]["equity_scope"] = "total"
    field["fact"] = payload
    result, issues = _validate(entry=entry)
    assert result == {}
    assert any("must select all_parent equity" in issue for issue in issues)


def test_missing_superseded_total_equity_fact_is_rejected() -> None:
    entry = _entry()
    entry["superseded_facts"].pop(0)
    result, issues = _validate(entry=entry)
    assert result == {}
    assert any(
        "exactly one superseded total-equity fact at 2022-08-28" in issue for issue in issues
    )


def test_non_sec_or_currency_mismatch_is_rejected() -> None:
    docs = _documents()
    docs[DOCUMENT_ID]["role"] = "issuer_annual"
    result, issues = _validate(docs=docs)
    assert result == {}
    assert any("role must be sec_filing" in issue for issue in issues)

    result, issues = validate_scope_reviews(_entry(), _documents(), "HKD", date(2026, 10, 15))
    assert result == {}
    assert any("requires report_currency USD" in issue for issue in issues)
