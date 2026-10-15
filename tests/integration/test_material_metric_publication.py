from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from investment_agent.evidence.store import EvidenceService
from investment_agent.investor.common import ObjectRef
from investment_agent.investor.enums import NumericRole
from investment_agent.investor.foundations import InvestorService
from investment_agent.investor.publication import PublicationService
from investment_agent.investor.publication_numbers import format_quantity
from investment_agent.materials.contracts import DatePrecision
from investment_agent.materials.publication_prepare import (
    PreparedMetric,
    prepare_material_publication,
)
from tests.integration.test_hk_us_publication_flow import (
    CLP,
    PILOTS,
    attach_material_evidence,
    candidate_for,
    clp_case,
    fixture_fact,
    numeric_claim,
    semantic_review,
    store_dataset,
)


async def _four_year_source(session, flow, tmp_path, *, changed_revenue: bool = False):
    facts = tuple(
        fixture_fact(
            CLP,
            item["metric"],
            year["label"],
            precision=DatePrecision.DATE,
        )
        for item in PILOTS[CLP]["facts"]
        for year in PILOTS[CLP]["fiscal_years"]
    )
    if changed_revenue:
        updated = []
        for fact in facts:
            if fact.metric == "revenue_operating" and fact.period.label == "FY2024":
                fact = replace(fact, value=replace(fact.value, raw_value="1234567890123"))
            updated.append(fact)
        facts = tuple(updated)
    return await store_dataset(
        session,
        tmp_path,
        facts,
        company_id=flow.listing.company_id,
        captured_at=datetime.now(UTC) - timedelta(minutes=1),
    )


def _one_per_key(report) -> dict[str, PreparedMetric]:
    selected: dict[str, PreparedMetric] = {}
    for metric in report.metrics:
        if metric.calculation_ref is not None:
            selected.setdefault(metric.key, metric)
    return selected


async def _publish_check(session, flow, metric: PreparedMetric, *, as_of: datetime):
    assert metric.calculation_ref is not None
    case = await InvestorService(session).get_case("local_owner", flow.case.id)
    claim = numeric_claim(
        metric.calculation_ref,
        field_path=f"results.{metric.key}",
        role=NumericRole.OUTPUT,
        kind="inference",
        materiality="context",
        text="The automatically prepared result is ",
    )
    candidate = candidate_for(
        case=case,
        mandate=flow.mandate,
        claims=(claim,),
        as_of=as_of,
    )
    check, resolved = await PublicationService(session).check(
        candidate, semantic_review(candidate)
    )
    return check, resolved, metric.calculation_ref


@pytest.mark.asyncio
async def test_six_automatic_metrics_pass_the_real_numeric_publication_checker(
    db_session, tmp_path
):
    flow = await clp_case(db_session, tmp_path / "initial", unknown_time=False)
    dataset = await _four_year_source(db_session, flow, tmp_path / "full")
    as_of = datetime.now(UTC) - timedelta(seconds=1)
    report = await prepare_material_publication(
        db_session,
        listing_id=flow.listing.id,
        dataset_ids=(dataset.id,),
        as_of=as_of,
    )
    selected = _one_per_key(report)
    assert set(selected) == {
        "revenue_yoy",
        "revenue_cagr_3y",
        "roe_avg_equity",
        "debt_ratio",
        "cfo_to_profit",
        "fcf_simple",
    }
    refs = tuple(
        dict.fromkeys(
            ref
            for item in selected.values()
            for ref in (*item.fact_refs, item.calculation_ref)
            if ref is not None
        )
    )
    await attach_material_evidence(db_session, case_id=flow.case.id, refs=refs)

    for item in selected.values():
        check, resolved, calculation_ref = await _publish_check(
            db_session, flow, item, as_of=as_of
        )
        assert check.passed, check.field_errors
        node = resolved["recommendation.claims.0.text.1"]
        saved = await EvidenceService(db_session).get_calculation(
            "local_owner", calculation_ref, as_of
        )
        assert saved.data is not None
        quantity = saved.data.results[item.key]
        text, unit, precision, value = format_quantity(quantity, "decimal")
        assert node.value == value
        assert node.unit == unit
        assert node.precision == precision
        assert node.text == text
        assert quantity.currency in {"HKD", None}
        assert item.period_end is not None
        assert item.period_end in {
            fact.data.period.end
            for ref in item.fact_refs
            for fact in [
                await EvidenceService(db_session).get_evidence(
                    "local_owner",
                    ref,
                    as_of,
                    subject_ref=ObjectRef(kind="listing", id=flow.listing.id, revision=1),
                )
            ]
            if fact.data is not None and fact.data.period is not None
        }


@pytest.mark.asyncio
async def test_revised_metric_publishes_new_refs_and_old_calculation_remains_valid(
    db_session, tmp_path
):
    flow = await clp_case(db_session, tmp_path / "initial", unknown_time=False)
    original = await _four_year_source(db_session, flow, tmp_path / "original")
    first_as_of = datetime.now(UTC) - timedelta(seconds=1)
    original_report = await prepare_material_publication(
        db_session,
        listing_id=flow.listing.id,
        dataset_ids=(original.id,),
        as_of=first_as_of,
    )
    old_growth = next(
        item for item in original_report.metrics
        if item.key == "revenue_yoy" and item.calculation_ref is not None
    )
    assert old_growth.calculation_ref is not None
    await attach_material_evidence(
        db_session,
        case_id=flow.case.id,
        refs=(*old_growth.fact_refs, old_growth.calculation_ref),
    )
    old_check, old_resolved, _ = await _publish_check(
        db_session, flow, old_growth, as_of=first_as_of
    )
    assert old_check.passed, old_check.field_errors
    old_value = Decimal(old_resolved["recommendation.claims.0.text.1"].value)

    revised = await _four_year_source(
        db_session, flow, tmp_path / "revised", changed_revenue=True
    )
    second_as_of = datetime.now(UTC) - timedelta(seconds=1)
    revised_report = await prepare_material_publication(
        db_session,
        listing_id=flow.listing.id,
        dataset_ids=(revised.id,),
        as_of=second_as_of,
    )
    new_growth = next(
        item for item in revised_report.metrics
        if item.key == "revenue_yoy" and item.calculation_ref is not None
    )
    assert new_growth.calculation_ref != old_growth.calculation_ref
    await attach_material_evidence(
        db_session,
        case_id=flow.case.id,
        refs=(*new_growth.fact_refs, new_growth.calculation_ref),
    )

    new_check, new_resolved, _ = await _publish_check(
        db_session, flow, new_growth, as_of=second_as_of
    )
    retained_check, retained_resolved, _ = await _publish_check(
        db_session, flow, old_growth, as_of=second_as_of
    )
    assert new_check.passed, new_check.field_errors
    assert retained_check.passed, retained_check.field_errors
    new_value = Decimal(new_resolved["recommendation.claims.0.text.1"].value)
    retained_value = Decimal(retained_resolved["recommendation.claims.0.text.1"].value)
    assert new_value != old_value
    assert retained_value == old_value
