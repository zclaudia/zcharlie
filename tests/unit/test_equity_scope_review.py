from __future__ import annotations

import copy

import pytest

from investment_agent.materials.equity_scope_review import (
    REVIEW_ID,
    equity_scope_review_ref,
    evaluate_equity_scope_review,
)

DOCUMENT_ID = "0000909832-23-000042"
DOCUMENT_HASH = "4ad0ed774dc46baca606e6d6db5f86d841211c3de97986861df71194e6bd8cc1"
DOCUMENT_URL = "https://www.sec.gov/Archives/edgar/data/909832/000090983223000042/cost-20230903.htm"


def _source_ref(
    *, context_ref: str, tag: str, source_label: str, excerpt: str, locator: str
) -> dict[str, str]:
    return {
        "document_id": DOCUMENT_ID,
        "sha256": DOCUMENT_HASH,
        "published_on": "2023-10-11",
        "url": DOCUMENT_URL,
        "context_ref": context_ref,
        "tag": tag,
        "source_label": source_label,
        "locator": locator,
        "excerpt": excerpt,
    }


def _component(scope: str, amount: int, source_ref: dict[str, str]) -> dict[str, object]:
    return {"scope": scope, "amount": amount, "source_ref": source_ref}


def _period(
    *,
    date: str,
    context_ref: str,
    parent: int,
    nci: int,
    total: int,
    nci_notation: str,
) -> dict[str, object]:
    parent_ref = _source_ref(
        context_ref=context_ref,
        tag="us-gaap:StockholdersEquity",
        source_label="Total Costco stockholders’ equity",
        excerpt="Total Costco stockholders’ equity | 25,058 | 20,642",
        locator=f"2023 Form 10-K balance sheet, {date} column; parent equity",
    )
    nci_ref = _source_ref(
        context_ref=context_ref,
        tag="us-gaap:MinorityInterest",
        source_label="Noncontrolling interests",
        excerpt="Noncontrolling interests | — | 5",
        locator=f"2023 Form 10-K balance sheet, {date} column; noncontrolling interests",
    )
    total_ref = _source_ref(
        context_ref=context_ref,
        tag="us-gaap:StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
        source_label="TOTAL EQUITY",
        excerpt="TOTAL EQUITY | 25,058 | 20,647",
        locator=f"2023 Form 10-K balance sheet, {date} column; total equity",
    )
    nci_component = _component("nci", nci, nci_ref)
    nci_component["amount_notation"] = nci_notation
    return {
        "date": date,
        "context_ref": context_ref,
        "parent": _component("parent", parent, parent_ref),
        "nci": nci_component,
        "total": _component("total", total, total_ref),
    }


def _valid_record() -> dict[str, object]:
    return {
        "id": REVIEW_ID,
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
        "periods": [
            _period(
                date="2022-08-28",
                context_ref="c-13",
                parent=20_642,
                nci=5,
                total=20_647,
                nci_notation="reported",
            ),
            _period(
                date="2023-09-03",
                context_ref="c-12",
                parent=25_058,
                nci=0,
                total=25_058,
                nci_notation="em_dash_zero",
            ),
        ],
    }


def test_valid_parent_nci_total_review_returns_bound_inputs_and_reference() -> None:
    record = _valid_record()
    result = evaluate_equity_scope_review(record)

    assert result["review_id"] == REVIEW_ID
    assert result["review_ref"].startswith("sha256:")
    assert result["inputs"]["periods"][0]["parent"]["amount"] == 20_642
    assert result["inputs"]["periods"][0]["nci"]["amount"] == 5
    assert result["inputs"]["periods"][0]["total"]["amount"] == 20_647
    assert result["inputs"]["periods"][1]["nci"]["amount_notation"] == "em_dash_zero"
    assert equity_scope_review_ref(record) == result["review_ref"]


def test_review_ref_is_independent_of_input_period_order() -> None:
    record = _valid_record()
    ref = equity_scope_review_ref(record)
    record["periods"].reverse()  # type: ignore[union-attr]
    assert equity_scope_review_ref(record) == ref


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda row: row["periods"][0]["parent"].update(scope="total"),
            "scope must be 'parent'",
        ),
        (
            lambda row: row["periods"][0].update(context_ref="c-12"),
            "context_ref must be c-13",
        ),
        (
            lambda row: row["periods"][1].update(date="2022-08-28"),
            "duplicate reviewed Costco balance instant",
        ),
        (
            lambda row: row["periods"][0]["total"].update(amount=20_648),
            "total.amount must be 20647",
        ),
        (
            lambda row: row["periods"][0]["nci"]["source_ref"].update(
                tag="us-gaap:StockholdersEquity"
            ),
            "source_ref.tag must be us-gaap:MinorityInterest",
        ),
        (
            lambda row: row["periods"][0]["parent"]["source_ref"].update(sha256="0" * 64),
            "sha256 must identify the reviewed original SEC filing",
        ),
        (
            lambda row: row["periods"][1]["total"]["source_ref"].update(
                document_id="another-filing"
            ),
            "document_id must be 0000909832-23-000042",
        ),
        (
            lambda row: row["periods"][1]["nci"].update(amount_notation="reported"),
            "amount_notation must be 'em_dash_zero'",
        ),
        (
            lambda row: row["periods"][1]["nci"]["source_ref"].update(
                excerpt="Noncontrolling interests | 0 | 5"
            ),
            "source_ref.excerpt must preserve the reviewed original source row",
        ),
    ],
)
def test_tampered_scope_period_math_or_source_citation_is_rejected(mutate, message: str) -> None:
    record = copy.deepcopy(_valid_record())
    mutate(record)
    with pytest.raises(ValueError, match=message):
        evaluate_equity_scope_review(record)


def test_mixed_source_document_or_publication_date_is_rejected() -> None:
    record = _valid_record()
    record["periods"][0]["parent"]["source_ref"]["published_on"] = "2024-10-09"  # type: ignore[index]
    with pytest.raises(ValueError, match="published_on must be 2023-10-11"):
        evaluate_equity_scope_review(record)


@pytest.mark.parametrize("bad_amount", [20_642.0, True, "20642"])
def test_amounts_must_be_exact_integers(bad_amount: object) -> None:
    record = _valid_record()
    record["periods"][0]["parent"]["amount"] = bad_amount  # type: ignore[index]
    with pytest.raises(ValueError, match="amount must be an integer"):
        evaluate_equity_scope_review(record)


def test_missing_or_unknown_record_fields_are_rejected() -> None:
    record = _valid_record()
    del record["raw_unit"]
    with pytest.raises(ValueError, match="record fields must be exact"):
        evaluate_equity_scope_review(record)
