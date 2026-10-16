from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from investment_agent.factors.industry_groups import default_industry_groups
from investment_agent.investor.common import ObjectRef
from investment_agent.providers.shenwan_directory import default_shenwan_directory
from investment_agent.supplemental_rules.contracts import (
    CategoryExpression,
    CategorySelector,
    CompanySelector,
    FrozenSubject,
    IndustryCategoryTerm,
    ListingSelector,
    MembershipCategoryTerm,
)
from investment_agent.supplemental_rules.matching import match_subject

NOW = datetime(2026, 10, 16, 4, tzinfo=UTC)
LISTING = ObjectRef(kind="listing", id=123, revision=1)


def _subject(*, classifications=(), evidence=()) -> FrozenSubject:
    normalized_classifications = tuple({
        "valid_from": "2020-01-01", "valid_to": None, **row
    } for row in classifications)
    source_manifest = tuple({
        "kind": "saved_evidence",
        "ref": item["source_ref"],
        "read_status": "ok", "read_warnings": [],
    } for item in evidence if item.get("id") is not None and item.get("revision") is not None)
    return FrozenSubject(
        listing_ref=LISTING, company_id=12, ticker="600001.SH", name="example",
        as_of=NOW, input_status="ready",
        selector_inputs={"listing": {"listing_id": 123}, "company": {"company_id": 12},
                         "classifications": list(normalized_classifications)},
        factor_values={}, evidence=tuple(evidence), source_manifest=source_manifest,
    )


def _verified_source(ref: ObjectRef) -> dict:
    return {"id": str(ref.id), "revision": ref.revision,
            "source_ref": ref.model_dump(mode="json"), "verification": "verified",
            "conflict": "none"}


def test_exact_listing_and_company_match_without_name_fuzzy_matching() -> None:
    listing_match = match_subject(ListingSelector(kind="listing", listing_ref=LISTING), _subject())
    assert listing_match.status == "applicable"
    assert listing_match.manifest_hash == match_subject(
        ListingSelector(kind="listing", listing_ref=LISTING), _subject()
    ).manifest_hash
    company_match = match_subject(CompanySelector(kind="company", company_id=13), _subject())
    assert company_match.status == "not_applicable"


def test_industry_match_preserves_system_version_and_actual_classification_row() -> None:
    source = ObjectRef(kind="evidence", id=uuid4(), revision=1)
    selector = CategorySelector(kind="category", expression=CategoryExpression(
        operator="all", terms=(IndustryCategoryTerm(
            kind="industry", system="sw2021", scheme_version=default_shenwan_directory().version,
            code="610000", source_ref=source,
        ),)
    ))
    subject = _subject(classifications=(
        {"id": 1, "system": "csrc", "code": "610000", "name": "CSRC"},
        {"id": 2, "system": "sw2021", "code": "610000", "name": "SW"},
    ), evidence=(_verified_source(source),))
    result = match_subject(selector, subject)
    assert result.status == "applicable"
    assert result.leaves[0]["evidence"][0]["system"] == "sw2021"

    # Identical codes from another classification system cannot satisfy the term.
    only_csrc = _subject(classifications=({"id": 1, "system": "csrc", "code": "610000"},),
                         evidence=(_verified_source(source),))
    assert match_subject(selector, only_csrc).status == "unknown"


def test_classification_conflict_and_interval_cutoff_are_conservative() -> None:
    source = ObjectRef(kind="evidence", id=uuid4(), revision=1)
    term = IndustryCategoryTerm(kind="industry", system="csrc",
        scheme_version=default_industry_groups().version, code="A01", source_ref=source)
    selector = CategorySelector(kind="category", expression=CategoryExpression(
        operator="all", terms=(term,)))
    evidence = (_verified_source(source),)
    conflict = _subject(classifications=(
        {"system": "csrc", "code": "A01", "valid_from": "2020-01-01", "valid_to": None},
        {"system": "csrc", "code": "A02", "valid_from": "2021-01-01", "valid_to": None},
    ), evidence=evidence)
    result = match_subject(selector, conflict)
    assert result.status == "unknown"
    assert result.leaves[0]["reason"] == "classification_source_conflicted"

    expired = _subject(classifications=(
        {"system": "csrc", "code": "A01", "valid_from": "2020-01-01",
         "valid_to": "2025-01-01"},
    ), evidence=evidence)
    expired_result = match_subject(selector, expired)
    assert expired_result.status == "unknown"
    assert expired_result.leaves[0]["reason"] == "classification_interval_not_covering_as_of"

    incomplete = _subject(classifications=(
        {"system": "csrc", "code": "A01", "valid_from": "2020-01-01", "valid_to": None},
    ), evidence=evidence)
    incomplete = incomplete.model_copy(update={
        "diagnostics": ("classification_rows_truncated:count_at_least_limit_plus_one",),
    })
    assert match_subject(selector, incomplete).status == "unknown"


def test_shenwan_ancestor_selector_accepts_a_valid_descendant_code() -> None:
    directory = default_shenwan_directory()
    chain = directory.chain("340501")
    assert len(chain) >= 2
    source = ObjectRef(kind="evidence", id=uuid4(), revision=1)
    selector = CategorySelector(kind="category", expression=CategoryExpression(
        operator="all", terms=(IndustryCategoryTerm(kind="industry", system="sw2021",
            scheme_version=directory.version, code=chain[0].code, source_ref=source),)))
    subject = _subject(classifications=(
        {"system": "sw2021", "code": "340501", "valid_from": "2020-01-01", "valid_to": None},
    ), evidence=(_verified_source(source),))
    result = match_subject(selector, subject)
    assert result.status == "applicable"
    assert result.leaves[0]["evidence"][0]["code"] == "340501"


def test_missing_source_future_version_and_membership_adapter_stay_unknown() -> None:
    source = ObjectRef(kind="evidence", id=uuid4(), revision=1)
    industry = IndustryCategoryTerm(kind="industry", system="csrc",
        scheme_version=default_industry_groups().version, code="A01", source_ref=source)
    selector = CategorySelector(kind="category", expression=CategoryExpression(
        operator="all", terms=(industry,)))
    no_source = match_subject(selector, _subject(
        classifications=({"system": "csrc", "code": "A01"},)))
    assert no_source.status == "unknown"
    wrong_version = industry.model_copy(update={"scheme_version": "future"})
    assert match_subject(CategorySelector(kind="category", expression=CategoryExpression(
        operator="all", terms=(wrong_version,))), _subject(
            classifications=({"system": "csrc", "code": "A01"},),
            evidence=(_verified_source(source),))).status == "unknown"

    membership = MembershipCategoryTerm(kind="product", members=("solar",), complete=True,
                                        source_ref=source)
    assert match_subject(CategorySelector(kind="category", expression=CategoryExpression(
        operator="all", terms=(membership,))), _subject(
            evidence=(_verified_source(source),))).status == "unknown"


def test_shallow_nested_all_any_propagates_three_valued_membership() -> None:
    source = ObjectRef(kind="evidence", id=uuid4(), revision=1)
    industry = IndustryCategoryTerm(kind="industry", system="csrc",
        scheme_version=default_industry_groups().version, code="A01", source_ref=source)
    unavailable = MembershipCategoryTerm(kind="product", members=("solar",), complete=False,
                                          source_ref=source)
    selector = CategorySelector(kind="category", expression=CategoryExpression(
        operator="all", terms=(CategoryExpression(operator="any", terms=(industry, unavailable)),)
    ))
    subject = _subject(classifications=({"system": "csrc", "code": "A01"},),
                       evidence=(_verified_source(source),))
    assert match_subject(selector, subject).status == "applicable"
