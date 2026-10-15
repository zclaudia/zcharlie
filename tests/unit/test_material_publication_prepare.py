from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import func, select

from investment_agent.evidence.store import EvidenceService
from investment_agent.investor.common import ObjectRef
from investment_agent.investor.foundations import InvestorError
from investment_agent.investor.publication_numbers import NumericResolver
from investment_agent.materials.context import financial_context
from investment_agent.materials.contracts import (
    SCHEMA_VERSION,
    DatasetDraft,
    DatasetKind,
    DatePrecision,
    FactPeriod,
    PeriodBasis,
    SubjectKind,
    fact_to_payload,
)
from investment_agent.materials.publication_prepare import prepare_material_publication
from investment_agent.materials.store import MaterialStore
from investment_agent.storage.models.evidence import SavedCalculation, SavedEvidence
from tests.integration.test_hk_us_publication_flow import (
    CLP,
    PILOTS,
    fixture_fact,
    seed_pilot_listings,
)


async def _four_year_dataset(
    session,
    tmp_path,
    *,
    precision=DatePrecision.DATE,
    change=None,
    source=None,
    published_date=None,
    omit=(),
):
    listing = (await seed_pilot_listings(session))[("HKEX", "00002")]
    fiscal_years = PILOTS[CLP]["fiscal_years"]
    facts = tuple(
        fixture_fact(
            CLP,
            entry["metric"],
            fiscal["label"],
            precision=precision,
            published_date=published_date,
        )
        for entry in PILOTS[CLP]["facts"]
        if entry["metric"] not in omit
        for fiscal in fiscal_years
    )
    if change is not None:
        target_metric, target_label, amount = change
        revised = []
        for fact in facts:
            if fact.metric == target_metric and fact.period.label == target_label:
                fact = replace(fact, value=replace(fact.value, raw_value=str(amount)))
            revised.append(fact)
        facts = tuple(revised)
    captured_at = datetime.now(UTC) - timedelta(hours=1)
    draft = DatasetDraft(
        kind=DatasetKind.ANNUAL_FACTS,
        subject_kind=SubjectKind.COMPANY,
        subject_id=listing.company_id,
        source=source or PILOTS[CLP]["source"],
        schema_version=SCHEMA_VERSION,
        adapter_version="hk-native-1",
        captured_at=captured_at,
        payload={"facts": [fact_to_payload(fact) for fact in facts]},
        period=FactPeriod(
            start=date(2024, 1, 1),
            end=date(2024, 12, 31),
            basis=PeriodBasis.ANNUAL,
            label="FY2024",
        ),
    )
    dataset = await MaterialStore(session, data_dir=tmp_path).save(draft)
    return listing, dataset


async def _count(session, model):
    return int(await session.scalar(select(func.count()).select_from(model)) or 0)


@pytest.mark.asyncio
async def test_method_upgrade_preserves_old_calculations_and_uses_distinct_receipts(
    db_session, tmp_path, monkeypatch
):
    import investment_agent.materials.publication_prepare as preparation

    listing, dataset = await _four_year_dataset(db_session, tmp_path)
    as_of = datetime.now(UTC) + timedelta(seconds=1)
    monkeypatch.setattr(preparation, "MATERIAL_CALCULATION_VERSION", "material.metrics.v1")
    old = await prepare_material_publication(
        db_session, listing_id=listing.id, dataset_ids=(dataset.id,), as_of=as_of
    )
    monkeypatch.setattr(preparation, "MATERIAL_CALCULATION_VERSION", "material.metrics.v2")
    new = await prepare_material_publication(
        db_session, listing_id=listing.id, dataset_ids=(dataset.id,), as_of=as_of
    )
    repeated = await prepare_material_publication(
        db_session, listing_id=listing.id, dataset_ids=(dataset.id,), as_of=as_of
    )
    old_refs = [item.calculation_ref for item in old.metrics if item.calculation_ref]
    new_refs = [item.calculation_ref for item in new.metrics if item.calculation_ref]
    assert old_refs and new_refs and not set(old_refs).intersection(new_refs)
    assert [item.calculation_ref for item in repeated.metrics if item.calculation_ref] == new_refs
    service = EvidenceService(db_session)
    for ref in old_refs:
        record = await service.get_calculation("local_owner", ref, as_of)
        assert record.data is not None and record.data.method_version == "material.metrics.v1"
    for ref in new_refs:
        record = await service.get_calculation("local_owner", ref, as_of)
        assert record.data is not None and record.data.method_version == "material.metrics.v2"
    assert await _count(db_session, SavedCalculation) == len(old_refs) + len(new_refs)


@pytest.mark.asyncio
async def test_service_persists_all_six_automatic_metric_outputs_and_resolves_publication(
    db_session, tmp_path
):
    listing, dataset = await _four_year_dataset(db_session, tmp_path)
    as_of = datetime.now(UTC) + timedelta(seconds=1)

    report = await prepare_material_publication(
        db_session,
        listing_id=listing.id,
        dataset_ids=(dataset.id,),
        as_of=as_of,
    )

    by_key = {}
    for item in report.metrics:
        if item.calculation_ref is not None:
            by_key.setdefault(item.key, item)
            resolver = NumericResolver(
                EvidenceService(db_session),
                ObjectRef(kind="listing", id=listing.id, revision=1),
                as_of,
            )
            calculation = await resolver.calculation(item.calculation_ref)
            assert calculation.results[item.key].value is not None
            assert calculation.fact_refs == item.fact_refs
            assert calculation.results[item.key].currency in {"HKD", None}
            assert calculation.results[item.key].unit in {"HKD", "ratio"}
            assert calculation.as_of == as_of.astimezone(UTC)
            input_records = [
                await EvidenceService(db_session).get_evidence(
                    "local_owner",
                    ref,
                    as_of,
                    subject_ref=ObjectRef(kind="listing", id=listing.id, revision=1),
                )
                for ref in item.fact_refs
            ]
            assert all(record.data is not None for record in input_records)
            assert item.period_end in {
                record.data.period.end
                for record in input_records
                if record.data is not None and record.data.period is not None
            }
    assert set(by_key) == {
        "revenue_yoy",
        "revenue_cagr_3y",
        "roe_avg_equity",
        "debt_ratio",
        "cfo_to_profit",
        "fcf_simple",
    }
    page = await financial_context(db_session, listing.id)
    for key, prepared in by_key.items():
        calculation = await EvidenceService(db_session).get_calculation(
            "local_owner", prepared.calculation_ref, as_of
        )
        assert calculation.data is not None
        published = calculation.data.results[key]
        assert any(
            metric.key == key
            and metric.value == str(published.value)
            and metric.unit == published.unit
            and metric.currency == published.currency
            for metric in page.metrics
        )
    assert await _count(db_session, SavedCalculation) == len(
        [item for item in report.metrics if item.calculation_ref is not None]
    )


@pytest.mark.asyncio
async def test_duplicate_preparation_reuses_refs_and_does_not_duplicate_rows(db_session, tmp_path):
    listing, dataset = await _four_year_dataset(db_session, tmp_path)
    as_of = datetime.now(UTC) + timedelta(seconds=1)
    first = await prepare_material_publication(
        db_session, listing_id=listing.id, dataset_ids=(dataset.id,), as_of=as_of
    )
    evidence_count = await _count(db_session, SavedEvidence)
    calculation_count = await _count(db_session, SavedCalculation)

    second = await prepare_material_publication(
        db_session,
        listing_id=listing.id,
        dataset_ids=(dataset.id,),
        as_of=as_of + timedelta(days=1),
    )

    assert [item.calculation_ref for item in second.metrics] == [
        item.calculation_ref for item in first.metrics
    ]
    assert [item.fact_refs for item in second.metrics] == [item.fact_refs for item in first.metrics]
    assert await _count(db_session, SavedEvidence) == evidence_count
    assert await _count(db_session, SavedCalculation) == calculation_count


@pytest.mark.asyncio
async def test_missing_inputs_remain_metric_gaps_without_numeric_calculations(db_session, tmp_path):
    listing, dataset = await _four_year_dataset(
        db_session,
        tmp_path,
        omit=(
            "profit_parent",
            "profit_ordinary_shareholders",
            "equity_parent",
            "total_assets",
            "total_liabilities",
            "operating_cash_flow",
            "capex_ppe",
        ),
    )
    report = await prepare_material_publication(
        db_session,
        listing_id=listing.id,
        dataset_ids=(dataset.id,),
        as_of=datetime.now(UTC) + timedelta(seconds=1),
    )
    assert report.status == "partial"
    missing = [
        item
        for item in report.metrics
        if item.key != "revenue_yoy" and item.key != "revenue_cagr_3y"
    ]
    assert missing and all(item.calculation_ref is None for item in missing)
    assert all(any("metric_status:missing_inputs" in gap for gap in item.gaps) for item in missing)


@pytest.mark.asyncio
async def test_unknown_and_future_publication_evidence_never_creates_numeric_calculation(
    db_session, tmp_path
):
    listing, dataset = await _four_year_dataset(
        db_session, tmp_path, precision=DatePrecision.UNKNOWN
    )
    report = await prepare_material_publication(
        db_session,
        listing_id=listing.id,
        dataset_ids=(dataset.id,),
        as_of=datetime.now(UTC) + timedelta(days=1),
    )
    assert all(item.calculation_ref is None for item in report.metrics)
    assert any("unknown_publication_time" in gap for gap in report.gaps)

    future_day = datetime.now(UTC).date() + timedelta(days=4)
    listing2, future_dataset = await _four_year_dataset(
        db_session, tmp_path / "future", published_date=future_day
    )
    future_report = await prepare_material_publication(
        db_session,
        listing_id=listing2.id,
        dataset_ids=(future_dataset.id,),
        as_of=datetime.now(UTC),
    )
    assert all(item.calculation_ref is None for item in future_report.metrics)
    assert any("fact_not_visible_at_as_of" in gap for gap in future_report.gaps)


@pytest.mark.asyncio
async def test_equal_cross_source_inputs_are_reported_ambiguous(db_session, tmp_path):
    listing, first = await _four_year_dataset(db_session, tmp_path, source="source-one")
    second_listing, second = await _four_year_dataset(db_session, tmp_path, source="source-two")
    assert second_listing.id == listing.id
    # The second import uses identical values, but comes from a distinct source.
    as_of = datetime.now(UTC) + timedelta(seconds=1)
    report = await prepare_material_publication(
        db_session,
        listing_id=listing.id,
        dataset_ids=(first.id, second.id),
        as_of=as_of,
    )
    assert all(item.calculation_ref is None for item in report.metrics)
    assert any("ambiguous_source" in gap for gap in report.gaps)


@pytest.mark.asyncio
async def test_new_material_revision_gets_new_calculation_and_keeps_historical_ref(
    db_session, tmp_path
):
    listing, original = await _four_year_dataset(db_session, tmp_path)
    as_of = datetime.now(UTC) + timedelta(seconds=1)
    original_report = await prepare_material_publication(
        db_session, listing_id=listing.id, dataset_ids=(original.id,), as_of=as_of
    )
    old_growth = next(
        item
        for item in original_report.metrics
        if item.key == "revenue_yoy" and item.calculation_ref is not None
    )
    revised, second = await _four_year_dataset(
        db_session,
        tmp_path,
        change=("revenue_operating", "FY2024", "1234567890123"),
    )
    assert revised.id == listing.id
    assert second.id != original.id
    revised_report = await prepare_material_publication(
        db_session, listing_id=listing.id, dataset_ids=(second.id,), as_of=as_of
    )
    new_growth = next(
        item
        for item in revised_report.metrics
        if item.key == "revenue_yoy" and item.calculation_ref is not None
    )

    assert new_growth.calculation_ref != old_growth.calculation_ref
    old_record = await EvidenceService(db_session).get_calculation(
        "local_owner", old_growth.calculation_ref, as_of
    )
    assert old_record.data is not None
    assert all(ref in old_record.data.fact_refs for ref in old_growth.fact_refs)


@pytest.mark.asyncio
async def test_failed_save_rolls_back_every_material_write(db_session, tmp_path, monkeypatch):
    listing, dataset = await _four_year_dataset(db_session, tmp_path)
    before_evidence = await _count(db_session, SavedEvidence)
    before_calculations = await _count(db_session, SavedCalculation)

    async def fail_after_evidence(*args, **kwargs):
        raise InvestorError("invalid_reference", "injected calculation write failure")

    monkeypatch.setattr(
        "investment_agent.materials.publication_prepare.save_material_calculation",
        fail_after_evidence,
    )
    with pytest.raises(InvestorError, match="injected calculation write failure"):
        await prepare_material_publication(
            db_session,
            listing_id=listing.id,
            dataset_ids=(dataset.id,),
            as_of=datetime.now(UTC) + timedelta(seconds=1),
        )
    assert await _count(db_session, SavedEvidence) == before_evidence
    assert await _count(db_session, SavedCalculation) == before_calculations
