from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from investment_agent.evidence.snapshots import materialize
from investment_agent.evidence.store import EvidenceService
from investment_agent.investor.common import ObjectRef
from investment_agent.investor.foundations import InvestorError
from investment_agent.materials.contracts import Verification
from investment_agent.materials.evidence import material_evidence
from investment_agent.providers.shenwan_directory import default_shenwan_directory
from investment_agent.storage.models.pit_snapshot import FinancialSnapshot, IndustryClassification
from investment_agent.storage.models.reference import Company, Listing, Market
from investment_agent.supplemental_rules.contracts import (
    CategoryExpression,
    CategorySelector,
    FrozenPair,
    FrozenSubject,
    IndustryCategoryTerm,
    ListingSelector,
    MetricCondition,
    MetricLeaf,
    ResearchRequirementDefinition,
    RuleDefinition,
)
from investment_agent.supplemental_rules.evaluation import evaluate_pair
from investment_agent.supplemental_rules.inputs import freeze_subject
from investment_agent.supplemental_rules.matching import match_selector
from investment_agent.supplemental_rules.metric_evaluation import evaluate_metric
from tests.unit.test_material_evidence import fact, store_dataset


def test_unsupported_factor_parameters_cannot_silently_use_default_value() -> None:
    """Distinct parameterized leaves must not reuse one fixed factor result."""
    subject = FrozenSubject(
        listing_ref=ObjectRef(kind="listing", id=123, revision=1),
        company_id=12,
        ticker="600001.SH",
        name="Example",
        as_of=datetime(2026, 10, 16, 4, tzinfo=UTC),
        input_status="ready",
        selector_inputs={"factor_metadata": [{"name": "roe", "unit": "ratio"}]},
        factor_values={"roe": {"value": 0.2, "quality": "ok", "version": "v1",
                               "flags": [], "inputs_ref": {}}},
        evidence=(),
        source_manifest=(),
    )
    result = evaluate_metric(subject, MetricCondition(kind="all", conditions=(
        MetricLeaf(factor_id="roe", operator=">", value=Decimal("0.1"),
                   params={"window_years": Decimal("3")}),
        MetricLeaf(factor_id="roe", operator="<", value=Decimal("0.3"),
                   params={"window_years": Decimal("10")}),
    )))

    assert result.status == "unknown"
    assert [leaf["params"] for leaf in result.leaves] == [
        {"window_years": "3"}, {"window_years": "10"},
    ]
    assert [leaf["reason"] for leaf in result.leaves] == [
        "factor_parameters_unsupported", "factor_parameters_unsupported",
    ]
    assert [leaf["factor_value"] for leaf in result.leaves] == [None, None]


def test_expired_classification_interval_does_not_prove_category_absence() -> None:
    source_ref = ObjectRef(kind="evidence", id="11111111-1111-4111-8111-111111111111",
                           revision=1)
    subject = FrozenSubject(
        listing_ref=ObjectRef(kind="listing", id=123, revision=1),
        company_id=12,
        ticker="600001.SH",
        name="Example",
        as_of=datetime(2026, 10, 16, 4, tzinfo=UTC),
        input_status="ready",
        selector_inputs={"classifications": [{
            "system": "sw2021", "code": "610000", "name": "Old category",
            "valid_from": "2020-01-01", "valid_to": "2024-12-31",
        }]},
        factor_values={},
        evidence=({"source_ref": source_ref.model_dump(mode="json"),
                   "verification": "verified", "conflict": "none"},),
        source_manifest=({"kind": "saved_evidence", "ref": source_ref.model_dump(mode="json"),
                          "read_status": "ok", "read_warnings": []},),
    )
    version = default_shenwan_directory().version
    selector = CategorySelector(kind="category", expression=CategoryExpression(
        operator="all", terms=(IndustryCategoryTerm(
            kind="industry", system="sw2021", scheme_version=version,
            code="610000", source_ref=source_ref,
        ),),
    ))

    result = match_selector(selector, subject)

    assert result.status == "unknown"
    assert result.leaves[0]["reason"] == "classification_interval_not_covering_as_of"


@pytest.mark.asyncio
async def test_real_saved_evidence_freeze_matches_only_its_exact_subject(db_session) -> None:
    db_session.add(Market(code="SSE", name="Shanghai", timezone="Asia/Shanghai", currency="CNY"))
    companies = [Company(canonical_name="Issuer A"), Company(canonical_name="Issuer B")]
    db_session.add_all(companies)
    await db_session.flush()
    listings = [Listing(company_id=company.id, market_code="SSE", ticker=ticker)
                for company, ticker in zip(companies, ("600001", "600002"), strict=True)]
    db_session.add_all(listings)
    await db_session.flush()
    cutoff = datetime(2030, 1, 1, tzinfo=UTC)
    for listing in listings:
        db_session.add(IndustryClassification(
            listing_id=listing.id, system="sw2021", code="610000", name="Industry",
            valid_from=date(2020, 1, 1), valid_to=None, source="fixture",
            captured_at=datetime(2025, 1, 1, tzinfo=UTC),
        ))
    rows = [FinancialSnapshot(
        listing_id=listing.id, report_period=date(2024, 12, 31), period_kind="annual",
        available_from=date(2025, 3, 1), availability="actual",
        fields={"roe": 0.2, "verification": {"roe": "verified"}},
        provider="baostock", captured_at=datetime(2025, 3, 2, tzinfo=UTC),
        raw_ref=f"financial:{listing.id}", is_backfill=False, version=1,
    ) for listing in listings]
    db_session.add_all(rows)
    await db_session.flush()

    evidence_store = EvidenceService(db_session)
    evidence_refs = []
    for row in rows:
        evidence_refs.append(await evidence_store.save_evidence(
            "local_owner", materialize(row, "roe"), uuid4(),
        ))
    await db_session.flush()

    own_ref = ObjectRef(kind="listing", id=listings[0].id, revision=1)
    own_subject = await freeze_subject(
        db_session, own_ref, cutoff, owner_id="local_owner", factor_names=(),
        evidence_refs=(evidence_refs[0],),
    )
    own_selector = CategorySelector(kind="category", expression=CategoryExpression(
        operator="all", terms=(IndustryCategoryTerm(
            kind="industry", system="sw2021", scheme_version=default_shenwan_directory().version,
            code="610000", source_ref=evidence_refs[0],
        ),),
    ))
    assert match_selector(own_selector, own_subject).status == "applicable"
    assert next(item for item in own_subject.source_manifest
                if item["kind"] == "saved_evidence")["read_status"] == "ok"

    with pytest.raises(InvestorError, match="another subject"):
        await freeze_subject(
            db_session, own_ref, cutoff, owner_id="local_owner", factor_names=(),
            evidence_refs=(evidence_refs[1],),
        )
    other_subject = await freeze_subject(
        db_session, own_ref, cutoff, owner_id="local_owner", factor_names=(),
        evidence_refs=(evidence_refs[1],), strict_source_subjects=False,
    )
    assert any(item.startswith("requested_evidence_subject_mismatch:")
               for item in other_subject.diagnostics)
    other_selector = own_selector.model_copy(update={
        "expression": own_selector.expression.model_copy(update={
            "terms": (own_selector.expression.terms[0].model_copy(update={
                "source_ref": evidence_refs[1],
            }),),
        }),
    })
    assert match_selector(other_selector, other_subject).status == "unknown"


@pytest.mark.asyncio
async def test_correction_limits_old_verified_evidence_as_category_source(db_session) -> None:
    db_session.add(Market(code="SSE", name="Shanghai", timezone="Asia/Shanghai", currency="CNY"))
    company = Company(canonical_name="Corrected evidence issuer")
    db_session.add(company)
    await db_session.flush()
    listing = Listing(company_id=company.id, market_code="SSE", ticker="600003")
    db_session.add(listing)
    await db_session.flush()
    db_session.add(IndustryClassification(
        listing_id=listing.id, system="sw2021", code="610000", name="Industry",
        valid_from=date(2020, 1, 1), valid_to=None, source="fixture",
        captured_at=datetime(2025, 1, 1, tzinfo=UTC),
    ))
    await db_session.flush()

    from tests.unit.test_material_evidence import SubjectKind

    first_dataset = await store_dataset(db_session, (
        fact(metric="net_profit", raw_value="100", verification=Verification.VERIFIED,
             original_locator="p.1 net profit"),
    ), subject_kind=SubjectKind.LISTING, subject_id=listing.id)
    first_records, skipped = material_evidence(
        first_dataset, subject_ref=ObjectRef(kind="listing", id=listing.id, revision=1),
    )
    assert not skipped and len(first_records) == 1
    assert first_records[0].document_provenance is not None
    evidence_store = EvidenceService(db_session)
    old_ref = await evidence_store.save_evidence("local_owner", first_records[0], uuid4())

    corrected_dataset = await store_dataset(db_session, (
        fact(metric="net_profit", raw_value="110", verification=Verification.VERIFIED,
             original_hash="corrected-document-hash", original_locator="p.1 net profit"),
    ), subject_kind=SubjectKind.LISTING, subject_id=listing.id)
    corrected_records, skipped = material_evidence(
        corrected_dataset, subject_ref=ObjectRef(kind="listing", id=listing.id, revision=1),
    )
    assert not skipped and len(corrected_records) == 1
    corrected = corrected_records[0].model_copy(update={"correction_refs": (old_ref,)})
    new_ref = await evidence_store.save_evidence("local_owner", corrected, uuid4())
    cutoff = datetime(2030, 1, 1, tzinfo=UTC)
    subject = await freeze_subject(
        db_session, ObjectRef(kind="listing", id=listing.id, revision=1), cutoff,
        owner_id="local_owner", factor_names=(),
    )

    assert next(item for item in subject.source_manifest
                if item.get("ref", {}).get("id") == str(old_ref.id))["read_status"] == "conflict"
    assert next(item for item in subject.source_manifest
                if item.get("ref", {}).get("id") == str(new_ref.id))["read_status"] == "ok"
    old_selector = CategorySelector(kind="category", expression=CategoryExpression(
        operator="all", terms=(IndustryCategoryTerm(
            kind="industry", system="sw2021", scheme_version=default_shenwan_directory().version,
            code="610000", source_ref=old_ref,
        ),),
    ))
    new_selector = old_selector.model_copy(update={
        "expression": old_selector.expression.model_copy(update={
            "terms": (old_selector.expression.terms[0].model_copy(update={
                "source_ref": new_ref,
            }),),
        }),
    })
    assert match_selector(old_selector, subject).status == "unknown"
    assert match_selector(old_selector, subject).leaves[0]["reason"] == "selector_source_limited_at_read"
    assert match_selector(new_selector, subject).status == "applicable"

    requirement = ResearchRequirementDefinition(
        kind="research_requirement", question="Has corrected profit evidence been reviewed?",
        required_evidence=(first_records[0].locator,),
        completion_criteria="A reviewer reconciles the corrected filing.",
        falsification_criteria="A later filing contradicts the correction.",
        check_by=cutoff, incomplete_effects=("require_review",),
    )
    rule = RuleDefinition(
        id=uuid4(), revision=1, name="Verify corrected evidence",
        rationale="Require review of corrected financial evidence.", source_refs=(),
        selector=ListingSelector(kind="listing", listing_ref=subject.listing_ref),
        contexts=frozenset({"company_research"}), rule_type="research_requirement",
        definition=requirement, dimensions=frozenset({"downside_counter_case"}),
        effects=frozenset({"require_review"}), severity="warning",
        materiality="Correction requires reconciliation.", valid_from=cutoff,
        review_due_at=cutoff, release_requirements=("Review exact corrected evidence.",),
    )
    pair = FrozenPair(
        ordinal=0, listing_ref=subject.listing_ref, context="company_research",
        rule_ref=rule.reference, rule=rule, subject=subject,
        input_hash="a" * 64, match_status="applicable", matching_manifest_hash="b" * 64,
    )
    evaluation = evaluate_pair(pair)
    assert evaluation.evidence_status == "conflicted"
    assert evaluation.condition_result == "triggered"
    assert evaluation.effective_effects
    basis_refs = evaluation.effective_effects[0].basis_refs
    assert old_ref not in basis_refs
    assert new_ref in basis_refs
