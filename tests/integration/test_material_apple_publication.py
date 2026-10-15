from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from investment_agent.evidence.store import EvidenceService
from investment_agent.investor.common import ObjectRef, Quantity
from investment_agent.investor.contracts import Mandate, ResearchAuthorization
from investment_agent.investor.enums import NumericRole
from investment_agent.investor.foundations import CreateCase, InvestorService
from investment_agent.investor.publication import PublicationService
from investment_agent.investor.publication_numbers import format_quantity
from investment_agent.materials.contracts import (
    FACT_ENTRY_KEYS,
    SCHEMA_VERSION,
    DatasetDraft,
    DatasetKind,
    FactPeriod,
    PeriodBasis,
    SubjectKind,
    fact_from_payload,
    fact_to_payload,
)
from investment_agent.materials.publication_prepare import prepare_material_publication
from investment_agent.materials.store import MaterialStore
from tests.integration.test_hk_us_publication_flow import (
    attach_material_evidence,
    candidate_for,
    listing_ref,
    metadata,
    numeric_claim,
    semantic_review,
)
from tests.unit._hk_us_support import seed_pilot_listings

MANIFEST = (
    Path(__file__).resolve().parents[2]
    / "config/research_material/annual_validation_2026-10-15.json"
)
APPLE_PERIOD_END = date(2025, 9, 27)


def _reviewed_apple_facts() -> tuple[FactPeriod, tuple]:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    company = next(row for row in manifest["companies"] if row["company_key"] == "apple")
    facts = tuple(
        fact_from_payload({key: item["fact"][key] for key in FACT_ENTRY_KEYS})
        for year in company["fiscal_years"]
        for item in year["facts"]
    )
    beginning_equity = company["fiscal_years"][0]["beginning_equity_fact"]["fact"]
    facts += (fact_from_payload({key: beginning_equity[key] for key in FACT_ENTRY_KEYS}),)
    latest = company["latest_annual"]
    period = FactPeriod(
        start=date.fromisoformat(latest["period_start"]),
        end=date.fromisoformat(latest["period_end"]),
        basis=PeriodBasis.ANNUAL,
        label="FY2025",
    )
    return period, facts


@pytest.mark.asyncio
async def test_reviewed_apple_roe_prepares_and_passes_numeric_publication(db_session, tmp_path):
    listing = (await seed_pilot_listings(db_session))[("NASDAQ", "AAPL")]
    investor = InvestorService(db_session)
    unknown = Quantity(value=None, unit="USD", unknown_reason="Not specified")
    mandate = await investor.create_mandate(
        "local_owner",
        Mandate(
            **metadata(),
            goals=(),
            scope_refs=(listing_ref(listing),),
            hard_constraints=(),
            unknowns=(),
            authorization=ResearchAuthorization(
                daily_budget=unknown,
                per_task_budget=unknown,
                max_leads_per_batch=0,
                max_new_tasks_per_batch=0,
                source_refs=(),
            ),
        ),
        uuid4(),
    )
    case = await investor.create_case(
        "local_owner",
        CreateCase(
            listing_ref=listing_ref(listing),
            question="Review Apple from its saved annual report material",
            idempotency_key=uuid4(),
        ),
    )

    period, facts = _reviewed_apple_facts()
    captured_at = datetime(2026, 10, 14, 12, tzinfo=UTC)
    dataset = await MaterialStore(db_session, data_dir=tmp_path).save(
        DatasetDraft(
            kind=DatasetKind.ANNUAL_FACTS,
            subject_kind=SubjectKind.COMPANY,
            subject_id=listing.company_id,
            source="issuer.annual_report",
            schema_version=SCHEMA_VERSION,
            adapter_version="annual-validation-test-1",
            captured_at=captured_at,
            payload={"facts": [fact_to_payload(fact) for fact in facts]},
            period=period,
        )
    )
    as_of = datetime.now(UTC) - timedelta(seconds=1)
    report = await prepare_material_publication(
        db_session,
        listing_id=listing.id,
        dataset_ids=(dataset.id,),
        as_of=as_of,
    )
    roe = next(
        metric
        for metric in report.metrics
        if metric.key == "roe_avg_equity" and metric.period_end == APPLE_PERIOD_END
    )

    assert roe.status == "prepared"
    assert roe.calculation_ref is not None
    assert len(roe.fact_refs) == 3
    saved_calculation = await EvidenceService(db_session).get_calculation(
        "local_owner", roe.calculation_ref, as_of
    )
    assert saved_calculation.data is not None
    assert saved_calculation.data.results["roe_avg_equity"].value is not None

    resolved_inputs = [
        await EvidenceService(db_session).get_evidence(
            "local_owner",
            ref,
            as_of,
            subject_ref=ObjectRef(kind="listing", id=listing.id, revision=1),
        )
        for ref in roe.fact_refs
    ]
    assert all(record.data is not None for record in resolved_inputs)
    assert "net_profit@2025-09-27" in {
        f"{record.data.document_provenance.metric}@{record.data.period.end.isoformat()}"
        for record in resolved_inputs
        if (
            record.data is not None
            and record.data.period is not None
            and record.data.document_provenance is not None
        )
    }
    assert any(
        record.data.document_provenance is not None
        and record.data.document_provenance.metric == "net_profit"
        and "Net income" in record.data.locator
        and record.data.source_ref.id == dataset.id
        for record in resolved_inputs
        if record.data is not None
    )
    assert all(
        record.data is not None and record.data.subject_ref == case.listing_ref
        for record in resolved_inputs
    )

    await attach_material_evidence(
        db_session, case_id=case.id, refs=(*roe.fact_refs, roe.calculation_ref)
    )
    case = await investor.get_case("local_owner", case.id)
    claim = numeric_claim(
        roe.calculation_ref,
        field_path="results.roe_avg_equity",
        role=NumericRole.OUTPUT,
        kind="inference",
        materiality="context",
        text="Apple's reviewed average-equity ROE is ",
    )
    candidate = candidate_for(
        case=case,
        mandate=mandate,
        claims=(claim,),
        as_of=as_of,
    )
    check, resolved = await PublicationService(db_session).check(
        candidate, semantic_review(candidate)
    )

    assert check.passed, check.field_errors
    quantity = saved_calculation.data.results["roe_avg_equity"]
    expected_text, expected_unit, expected_precision, expected_value = format_quantity(
        quantity, "decimal"
    )
    published = resolved["recommendation.claims.0.text.1"]
    assert (published.text, published.unit, published.precision, published.value) == (
        expected_text,
        expected_unit,
        expected_precision,
        expected_value,
    )
