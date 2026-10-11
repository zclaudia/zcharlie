"""HK/US material into the research case and the publication path (plan 10).

The flow here is the production one: a pilot listing gets a case, plan 08's
checked fixture amounts are stored through plan 05's ``MaterialStore``, plan 07's
``material_evidence`` conversion is saved through the bridge, the evidence is
attached to the case and the publication check resolves the exact original
amounts. Every amount is the checked fixture value of
``tests/fixtures/materials/metrics/pilots.json`` (``clp-holdings``) in the
source's own currency; no CNY conversion and no invented publication time
appears anywhere, and a dataset row whose publication time is unknown stays in
the material layer.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from investment_agent.agent.contracts import ContextBinding
from investment_agent.agent.execution import ExecutionGrant, admit
from investment_agent.agent.tasks import TaskService
from investment_agent.config import Settings
from investment_agent.evidence.contracts import DatePrecision, EvidenceRecord, Period
from investment_agent.evidence.store import EvidenceService
from investment_agent.investor.common import ObjectRef, Quantity
from investment_agent.investor.contracts import (
    DIMENSIONS,
    Case,
    Claim,
    CompanyRecommendation,
    Dependency,
    DimensionAssessment,
    Mandate,
    NumericNode,
    ResearchAuthorization,
    TextNode,
    ThesisRevision,
)
from investment_agent.investor.enums import CalculationStatus, NumericRole
from investment_agent.investor.foundations import CreateCase, InvestorError, InvestorService
from investment_agent.investor.publication import (
    FIXED_METHOD,
    PublicationCandidate,
    PublicationService,
    SemanticAssessment,
    SemanticReview,
    candidate_hash,
)
from investment_agent.investor.publication_numbers import format_quantity
from investment_agent.materials.contracts import (
    SCHEMA_VERSION,
    AccountingStandard,
    DatasetDraft,
    DatasetKind,
    EquityScope,
    FactPeriod,
    FactProvenance,
    FactRecord,
    FactSemantics,
    FactValue,
    PeriodBasis,
    PerShareBasis,
    ReportingScope,
    SubjectKind,
    Verification,
    fact_to_payload,
    payload_facts,
)
from investment_agent.materials.contracts import (
    DatePrecision as MaterialDatePrecision,
)
from investment_agent.materials.evidence import METRIC_NOT_SELECTED, UNKNOWN_PUBLICATION_TIME
from investment_agent.materials.investor_bridge import (
    MATERIAL_CALCULATION_METHOD,
    MATERIAL_CALCULATION_VERSION,
    attach_material_evidence,
    material_calculation_input_hash,
    save_material_calculation,
    save_material_evidence,
)
from investment_agent.materials.store import MaterialStore, SavedDataset
from investment_agent.storage.models.evidence import SavedEvidence
from investment_agent.storage.models.investor import InvestorCase
from investment_agent.storage.models.publication import PublicationArtifact, PublicationOutbox
from investment_agent.storage.models.reference import Listing
from investment_agent.storage.models.research_material import ResearchDataset
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository, RunLease
from tests.unit._hk_us_support import seed_pilot_listings

PILOTS: dict[str, Any] = json.loads(
    (
        Path(__file__).resolve().parents[1] / "fixtures" / "materials" / "metrics" / "pilots.json"
    ).read_text(encoding="utf-8")
)["companies"]
CLP = "clp-holdings"
FY2024_PERIOD = FactPeriod(
    start=date(2024, 1, 1), end=date(2024, 12, 31), basis=PeriodBasis.ANNUAL, label="FY2024"
)
DATED_METRICS = ("revenue_operating", "operating_cash_flow", "capex_ppe")


def metadata() -> dict[str, Any]:
    return dict(
        id=uuid4(), revision=1, actor="untrusted", origin_ref=None, created_at=datetime.now(UTC)
    )


def settings() -> Settings:
    return Settings(
        _env_file=None,
        api_key="ci-inert-api-key",
        investor_agent_enabled=True,
        investor_agent_task_seconds=600,
    )


def listing_ref(listing: Listing) -> ObjectRef:
    return ObjectRef(kind="listing", id=listing.id, revision=1)


async def count(session: Any, model: Any) -> int:
    return int((await session.scalar(select(func.count()).select_from(model))) or 0)


# --- the fixture amount chain ------------------------------------------------


def fixture_amount(company: str, metric: str, label: str) -> str:
    block = PILOTS[company]
    entry = next(item for item in block["facts"] if item["metric"] == metric)
    return str(entry["values"][label])


def fixture_fact(
    company: str,
    metric: str,
    label: str,
    *,
    precision: MaterialDatePrecision,
    published_date: date | None = None,
) -> FactRecord:
    """One fixture fact with the fixture's own amount, unit and period.

    ``DATE`` precision uses the block's stated publication date; the ``UNKNOWN``
    case keeps both publication fields null, exactly as a source that states no
    provable time.
    """

    block = PILOTS[company]
    entry = next(item for item in block["facts"] if item["metric"] == metric)
    fiscal = next(year for year in block["fiscal_years"] if year["label"] == label)
    if precision is MaterialDatePrecision.DATE and published_date is None:
        published_date = date.fromisoformat(block["source_published_date"])
    currency = block["identity"]["report_currency"]
    return FactRecord(
        metric=metric,
        period=FactPeriod(
            start=None if entry["basis"] == "instant" else date.fromisoformat(fiscal["start"]),
            end=date.fromisoformat(fiscal["end"]),
            basis=PeriodBasis.INSTANT if entry["basis"] == "instant" else PeriodBasis.ANNUAL,
            label=fiscal["label"],
        ),
        semantics=FactSemantics(
            metric=metric,
            source_label=entry["source_labels"][label],
            source_field=None,
            accounting_standard=AccountingStandard(block["accounting_standard"]),
            reporting_scope=ReportingScope.CONSOLIDATED,
            equity_scope=EquityScope(entry["equity_scope"]),
            per_share_basis=PerShareBasis.NA,
        ),
        value=FactValue(
            raw_value=fixture_amount(company, metric, label),
            raw_unit=entry["unit"],
            scale=entry["scale"],
            unit=entry["unit"],
            currency=currency,
            sign_convention=entry["sign_convention"],
        ),
        provenance=FactProvenance(
            source=block["source"],
            accession=None,
            original_hash=None,
            source_published_at=None,
            source_published_date=published_date,
            source_published_precision=precision,
            accepted_at=None,
            source_available_at=None,
        ),
        verification=(
            Verification.VERIFIED
            if label in entry.get("verified_years", [])
            else Verification.CANDIDATE
        ),
        conflict="none",
        applicability="applicable",
    )


async def store_dataset(
    session: Any,
    data_dir: Path,
    facts: tuple[FactRecord, ...],
    *,
    company_id: int,
    captured_at: datetime,
    subject_kind: SubjectKind = SubjectKind.COMPANY,
    subject_id: int | None = None,
    period: FactPeriod | None = FY2024_PERIOD,
) -> SavedDataset:
    """Store one validated dataset version through plan 05's single write path."""

    draft = DatasetDraft(
        kind=DatasetKind.ANNUAL_FACTS,
        subject_kind=subject_kind,
        subject_id=company_id if subject_id is None else subject_id,
        source=PILOTS[CLP]["source"],
        schema_version=SCHEMA_VERSION,
        adapter_version="hk-native-1",
        captured_at=captured_at,
        payload={
            "facts": [fact_to_payload(fact) for fact in facts],
            "pageNumber": 1,
            "pageSize": 20,
        },
        period=period,
    )
    return await MaterialStore(session, data_dir=data_dir).save(draft)


async def saved_material_facts(session: Any, dataset_id: int) -> tuple[EvidenceRecord, ...]:
    rows = (
        await session.scalars(
            select(SavedEvidence).where(
                SavedEvidence.payload["source_ref"]["id"].as_integer() == dataset_id
            )
        )
    ).all()
    return tuple(EvidenceRecord.model_validate(row.payload) for row in rows)


# --- the case / evidence / candidate scaffolding -----------------------------


@dataclass(frozen=True)
class MaterialFlow:
    """One CLP case with its saved evidence."""

    listing: Listing
    case: Case
    mandate: Mandate
    as_of: datetime
    captured_at: datetime
    dataset: SavedDataset
    refs: dict[str, ObjectRef]
    refs_ordered: tuple[ObjectRef, ...]
    skipped: tuple[str, ...]


async def clp_case(session: Any, data_dir: Path, *, unknown_time: bool) -> MaterialFlow:
    """Seed the CLP pilot, store the fixture material and save its evidence."""

    listing = (await seed_pilot_listings(session))[("HKEX", "00002")]
    service = InvestorService(session)
    unknown = Quantity(value=None, unit="USD", unknown_reason="Not specified")
    mandate = await service.create_mandate(
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
    case = await service.create_case(
        "local_owner",
        CreateCase(
            listing_ref=listing_ref(listing),
            question="Review the reporting company from its saved annual material",
            idempotency_key=uuid4(),
        ),
    )
    now = datetime.now(UTC)
    as_of = now - timedelta(seconds=1)
    captured_at = now - timedelta(hours=6)
    facts = tuple(
        fixture_fact(CLP, metric, "FY2024", precision=MaterialDatePrecision.DATE)
        for metric in DATED_METRICS
    )
    if unknown_time:
        facts = facts + (
            fixture_fact(CLP, "revenue_operating", "FY2023", precision=MaterialDatePrecision.UNKNOWN),
        )
    dataset = await store_dataset(
        session, data_dir, facts, company_id=listing.company_id, captured_at=captured_at
    )
    batch = await save_material_evidence(session, listing_id=listing.id, dataset_ids=[dataset.id])
    return MaterialFlow(
        listing=listing,
        case=case,
        mandate=mandate,
        as_of=as_of,
        captured_at=captured_at,
        dataset=dataset,
        refs=dict(zip(DATED_METRICS, batch.refs, strict=True)),
        refs_ordered=batch.refs,
        skipped=batch.skipped,
    )


def dimensions() -> tuple[DimensionAssessment, ...]:
    return tuple(
        DimensionAssessment(
            dimension=name,
            status="missing",
            applicable=True,
            reason="The saved material covers only part of this dimension",
            refs=(),
        )
        for name in DIMENSIONS
    )


def numeric_claim(
    ref: ObjectRef,
    *,
    field_path: str | None,
    role: NumericRole,
    text: str,
    kind: str = "fact",
    materiality: str = "critical",
    verification: str = "verified",
) -> Claim:
    node = NumericNode(
        type="numeric_claim",
        ref=ref.model_copy(update={"field_path": field_path}),
        role=role,
        display_format="decimal",
    )
    return Claim(
        id=uuid4(),
        kind=kind,
        text=(TextNode(type="text", text=text), node),
        materiality=materiality,
        support_refs=(ref,),
        opposition_refs=(),
        assumption_refs=(),
        verification=verification,
        impact_if_wrong="A wrong amount changes the research observation",
    )


def candidate_for(
    *,
    case: Case,
    mandate: Mandate,
    claims: tuple[Claim, ...],
    as_of: datetime,
) -> PublicationCandidate:
    assessed = dimensions()
    critical = tuple(
        Dependency(ref=ref, materiality="critical", fields=("value",))
        for claim in claims
        if claim.materiality == "critical"
        for ref in claim.support_refs
    )
    thesis = ThesisRevision(
        **metadata(),
        case_ref=ObjectRef(kind="case", id=case.id, revision=case.revision),
        previous_ref=None,
        change_reason="Initial material review",
        question_scope="Material facts of the reporting company with explicit gaps",
        assessed_dimensions=assessed,
        claims=claims,
        assumptions=(),
        counter_case=(TextNode(type="text", text="The source figures may reflect non-recurring items"),),
        evidence_set_hash="hk-us-material-fixture",
        method_ref=FIXED_METHOD,
        as_of=as_of,
    )
    recommendation = CompanyRecommendation(
        **metadata(),
        scope="company_view",
        case_ids=(case.id,),
        thesis_revision_refs=(ObjectRef(kind="thesis", id=thesis.id, revision=1),),
        as_of=as_of,
        question_scope=thesis.question_scope,
        assessed_dimensions=assessed,
        sufficiency="bounded",
        claims=claims,
        counter_case=thesis.counter_case,
        assumptions=(),
        gaps=("Reporting coverage is partial",),
        conditions=(),
        valid_until=as_of + timedelta(days=14),
        next_review_at=as_of + timedelta(days=7),
        monitor_refs=(),
        dependency_refs=critical,
        mandate_revision_id=ObjectRef(kind="mandate", id=mandate.id, revision=mandate.revision),
        method_revision_id=FIXED_METHOD,
        provenance=(),
        business_view="undetermined",
        price_view="undetermined",
        conditional_actions=(),
    )
    return PublicationCandidate(thesis=thesis, recommendation=recommendation)


def semantic_review(candidate: PublicationCandidate) -> SemanticReview:
    claims = {c.id: c for c in (*candidate.thesis.claims, *candidate.recommendation.claims)}
    checked = tuple(
        dict.fromkeys(
            ref
            for claim in claims.values()
            for ref in (*claim.support_refs, *claim.opposition_refs, *claim.assumption_refs)
        )
    )
    return SemanticReview(
        **metadata(),
        candidate_hash=candidate_hash(candidate),
        material_set_hash=candidate.thesis.evidence_set_hash,
        method_ref=FIXED_METHOD,
        assessments=tuple(
            SemanticAssessment(
                claim_id=identifier,
                fact_relation="The saved material fact states the amount for its own period",
                counter_evidence_relevance="No opposing saved source is known for this exact fact",
                materiality_reason="The amount is the subject of the published node",
            )
            for identifier in claims
        ),
        checked_sources=checked,
        unresolved_questions=(),
        conclusion="pass",
        reviewer="trusted_research_step",
    )


async def prepared_action(
    session: Any, candidate: PublicationCandidate, review: SemanticReview
) -> tuple[Any, UUID]:
    """Admit a trusted task and prepare its exact publication action."""

    grant = ExecutionGrant(
        mode="interactive",
        source="authenticated_message",
        prompt="Review the company from its saved material",
        tool_names=("publish_recommendation",),
        scope_refs=(candidate.thesis.case_ref,),
        mandate_id=candidate.recommendation.mandate_revision_id.id,
    )
    task = await admit(
        session,
        base=settings(),
        input_key=str(uuid4()),
        binding=ContextBinding(
            context_refs=(candidate.thesis.case_ref,),
            base_refs=(candidate.thesis.case_ref,),
            as_of=datetime.now(UTC),
        ),
        grant=grant,
    )
    run = await PipelineRunRepository(session).claim_next(
        worker_id=str(task.id), kinds=["agent_turn"]
    )
    claim = await TaskService(session).claim(
        task.id, RunLease(run.id, str(task.id), run.attempt_count)
    )
    action = await TaskService(session).prepare_action(
        claim,
        intent_key=f"publish:{candidate.thesis.case_ref.id}",
        parameters={
            "candidate": candidate.model_dump(mode="json"),
            "semantic": review.model_dump(mode="json"),
        },
    )
    return claim, UUID(action.operation_id)


# --- case registration -------------------------------------------------------


async def test_case_registration_binds_the_pilot_listing(db_session) -> None:
    listing = (await seed_pilot_listings(db_session))[("HKEX", "00002")]
    case = await InvestorService(db_session).create_case(
        "local_owner",
        CreateCase(
            listing_ref=listing_ref(listing),
            question="Review the reporting company from its saved annual material",
            idempotency_key=uuid4(),
        ),
    )

    assert case.listing_ref.kind == "listing"
    assert case.listing_ref.id == listing.id
    assert case.listing_ref.revision == 1
    assert case.revision == 1

    service = InvestorService(db_session)
    with pytest.raises(InvestorError) as refused:
        await service.create_case(
            "local_owner",
            CreateCase(
                listing_ref=ObjectRef(kind="listing", id=listing.id + 1000, revision=1),
                question="Review a listing that does not exist",
                idempotency_key=uuid4(),
            ),
        )
    assert refused.value.detail.code == "invalid_reference"
    assert await count(db_session, InvestorCase) == 1


# --- evidence saving ---------------------------------------------------------


async def test_evidence_is_saved_from_the_listing_company_dataset(db_session, tmp_path) -> None:
    flow = await clp_case(db_session, tmp_path / "data", unknown_time=True)

    assert flow.dataset.id > 0
    assert flow.skipped == (UNKNOWN_PUBLICATION_TIME,)
    assert len(flow.refs_ordered) == len(DATED_METRICS)
    assert set(flow.refs) == set(DATED_METRICS)

    saved = await saved_material_facts(db_session, flow.dataset.id)
    assert {record.id for record in saved} == {ref.id for ref in flow.refs_ordered}
    by_amount = {
        record.value.value: record for record in saved if record.value is not None
    }
    for metric in DATED_METRICS:
        record = by_amount[Decimal(fixture_amount(CLP, metric, "FY2024"))]
        assert record.source_kind == "research_dataset"
        assert record.source_ref == ObjectRef(
            kind="research_dataset", id=flow.dataset.id, revision=flow.dataset.revision
        )
        assert record.subject_ref == listing_ref(flow.listing)
        assert record.date_precision is DatePrecision.DATE
        assert record.verification == "verified"
        assert record.conflict == "none"
        assert record.acquired_at == flow.captured_at
        assert record.published_at == datetime(2025, 2, 24, tzinfo=UTC)
        assert record.available_from == datetime(2025, 2, 25, tzinfo=UTC)
        assert record.period == Period(start=date(2024, 1, 1), end=date(2024, 12, 31), basis="annual")
        assert record.value == Quantity(
            value=Decimal(fixture_amount(CLP, metric, "FY2024")), unit="HKD", currency="HKD"
        )
        # The facts of one dataset version share source_ref; their locator and
        # content hash are what tell them apart.
        assert record.locator == f"{PILOTS[CLP]['source']}:annual_facts:FY2024"
    assert len({record.content_hash for record in saved}) == len(DATED_METRICS)

    # A second call over the same dataset version returns the same references and
    # adds no row: the fact identity is the fact-level content hash, not source_ref.
    again = await save_material_evidence(
        db_session, listing_id=flow.listing.id, dataset_ids=[flow.dataset.id, flow.dataset.id]
    )
    assert again.refs == flow.refs_ordered
    assert again.dataset_ids == (flow.dataset.id,)
    assert again.skipped == (UNKNOWN_PUBLICATION_TIME,)
    assert await count(db_session, SavedEvidence) == len(DATED_METRICS)


async def test_metric_selection_reports_every_unconverted_fact(db_session, tmp_path) -> None:
    listing = (await seed_pilot_listings(db_session))[("HKEX", "00002")]
    facts = tuple(
        fixture_fact(CLP, metric, "FY2024", precision=MaterialDatePrecision.DATE)
        for metric in DATED_METRICS
    )
    dataset = await store_dataset(
        db_session,
        tmp_path / "data",
        facts,
        company_id=listing.company_id,
        captured_at=datetime.now(UTC) - timedelta(hours=6),
    )

    batch = await save_material_evidence(
        db_session,
        listing_id=listing.id,
        dataset_ids=[dataset.id],
        metrics=["revenue_operating"],
    )

    assert len(batch.refs) == 1
    assert batch.skipped == (METRIC_NOT_SELECTED, METRIC_NOT_SELECTED)
    assert batch.dataset_ids == (dataset.id,)
    assert await count(db_session, SavedEvidence) == 1


async def test_material_evidence_refuses_foreign_or_missing_datasets(db_session, tmp_path) -> None:
    listings = await seed_pilot_listings(db_session)
    clp = listings[("HKEX", "00002")]
    hsbc = listings[("HKEX", "00005")]
    captured_at = datetime.now(UTC) - timedelta(hours=6)
    facts = (fixture_fact(CLP, "revenue_operating", "FY2024", precision=MaterialDatePrecision.DATE),)

    foreign = await store_dataset(
        db_session, tmp_path / "data", facts, company_id=hsbc.company_id, captured_at=captured_at
    )
    listing_level = await store_dataset(
        db_session,
        tmp_path / "data",
        facts,
        company_id=clp.company_id,
        subject_kind=SubjectKind.LISTING,
        subject_id=clp.id,
        captured_at=captured_at,
    )
    own = await store_dataset(
        db_session, tmp_path / "data", facts, company_id=clp.company_id, captured_at=captured_at
    )

    for dataset_id in (foreign.id, listing_level.id, 10**6):
        with pytest.raises(InvestorError) as refused:
            await save_material_evidence(db_session, listing_id=clp.id, dataset_ids=[dataset_id])
        assert refused.value.detail.code == "invalid_reference"
    with pytest.raises(InvestorError):
        await save_material_evidence(db_session, listing_id=10**6, dataset_ids=[listing_level.id])
    # A batch is validated before it is written: the valid first dataset does not
    # leave evidence behind when a later dataset is foreign.
    with pytest.raises(InvestorError):
        await save_material_evidence(db_session, listing_id=clp.id, dataset_ids=[own.id, foreign.id])
    assert await count(db_session, SavedEvidence) == 0


# --- attachment --------------------------------------------------------------


async def test_attaching_material_evidence_advances_the_case(db_session, tmp_path) -> None:
    flow = await clp_case(db_session, tmp_path / "data", unknown_time=False)
    service = InvestorService(db_session)

    case = await attach_material_evidence(
        db_session, case_id=flow.case.id, refs=flow.refs_ordered
    )
    assert case.revision == flow.case.revision + 1

    detail = await service.detail("local_owner", flow.case.id)
    assert detail.case.revision == case.revision
    assert detail.episodes[-1].artifact_refs == flow.refs_ordered

    with pytest.raises(InvestorError) as stale:
        await attach_material_evidence(
            db_session, case_id=flow.case.id, refs=flow.refs_ordered, expected_revision=1
        )
    assert stale.value.detail.code == "revision_conflict"

    # Evidence of another listing's subject is refused by the existing validator.
    other = await service.create_case(
        "local_owner",
        CreateCase(
            listing_ref=listing_ref((await seed_pilot_listings(db_session))[("HKEX", "00005")]),
            question="Review a different listing entirely",
            idempotency_key=uuid4(),
        ),
    )
    with pytest.raises(InvestorError) as foreign:
        await attach_material_evidence(db_session, case_id=other.id, refs=flow.refs_ordered)
    assert foreign.value.detail.code == "invalid_reference"
    assert (await service.get_case("local_owner", other.id)).revision == 1


# --- unknown publication time ------------------------------------------------


async def test_unknown_publication_time_stays_in_the_material_layer(db_session, tmp_path) -> None:
    flow = await clp_case(db_session, tmp_path / "data", unknown_time=True)
    saved = await saved_material_facts(db_session, flow.dataset.id)
    unknown_amount = Decimal(fixture_amount(CLP, "revenue_operating", "FY2023"))

    assert flow.skipped == (UNKNOWN_PUBLICATION_TIME,)
    assert len(saved) == len(DATED_METRICS)
    assert all(record.date_precision is DatePrecision.DATE for record in saved)
    assert unknown_amount not in {
        record.value.value for record in saved if record.value is not None
    }
    dataset = await db_session.get(ResearchDataset, flow.dataset.id)
    assert dataset is not None
    assert len(payload_facts(dataset.payload)) == len(DATED_METRICS) + 1


# --- original-currency publication -------------------------------------------


async def evidence_record(session: Any, flow: MaterialFlow, metric: str) -> EvidenceRecord:
    result = await EvidenceService(session).get_evidence(
        "local_owner", flow.refs[metric], flow.as_of
    )
    assert result.status == "ok"
    assert result.data is not None and result.data.value is not None
    return result.data


async def test_publication_resolves_the_material_value_in_its_own_currency(
    db_session, tmp_path
) -> None:
    flow = await clp_case(db_session, tmp_path / "data", unknown_time=False)
    await attach_material_evidence(db_session, case_id=flow.case.id, refs=flow.refs_ordered)
    case = await InvestorService(db_session).get_case("local_owner", flow.case.id)
    revenue = flow.refs["revenue_operating"]
    claim = numeric_claim(
        revenue,
        field_path="value",
        role=NumericRole.FACT,
        text="The saved annual revenue is ",
    )
    candidate = candidate_for(case=case, mandate=flow.mandate, claims=(claim,), as_of=flow.as_of)
    review = semantic_review(candidate)
    task_claim, operation = await prepared_action(db_session, candidate, review)

    result = await PublicationService(db_session).publish(
        task_claim, operation, candidate, review, base=settings()
    )

    assert result.published
    assert result.check.passed and not result.check.failures
    node = result.resolved_nodes["recommendation.claims.0.text.1"]
    amount = Decimal(fixture_amount(CLP, "revenue_operating", "FY2024"))
    stored = await evidence_record(db_session, flow, "revenue_operating")
    assert Decimal(node.value) == stored.value.value == amount  # type: ignore[union-attr]
    assert node.unit == "HKD"
    assert node.precision == 2
    assert node.text == f"{amount}.00 HKD"
    assert "CNY" not in node.text
    assert node.period == Period(start=date(2024, 1, 1), end=date(2024, 12, 31), basis="annual")
    assert node.quality == ("verification=verified",)
    assert result.recommendation_ref is not None
    assert await count(db_session, PublicationOutbox) == 1
    current = await InvestorService(db_session).get_case("local_owner", case.id)
    assert current.current_recommendation_ref == result.recommendation_ref


async def test_calculation_reference_publishes_the_original_currency_result(
    db_session, tmp_path
) -> None:
    flow = await clp_case(db_session, tmp_path / "data", unknown_time=False)
    await attach_material_evidence(db_session, case_id=flow.case.id, refs=flow.refs_ordered)
    case = await InvestorService(db_session).get_case("local_owner", flow.case.id)
    operating_cash_flow = await evidence_record(db_session, flow, "operating_cash_flow")
    capital_expenditure = await evidence_record(db_session, flow, "capex_ppe")
    expected = (
        operating_cash_flow.value.value - capital_expenditure.value.value  # type: ignore[union-attr]
    )
    assert expected == Decimal("8064000000")
    calculation = await save_material_calculation(
        db_session,
        listing_id=flow.listing.id,
        fact_refs=(flow.refs["operating_cash_flow"], flow.refs["capex_ppe"]),
        results={"fcf_simple": Quantity(value=expected, unit="HKD", currency="HKD")},
        as_of=flow.as_of,
    )
    claim = numeric_claim(
        calculation,
        field_path="results.fcf_simple",
        role=NumericRole.OUTPUT,
        kind="inference",
        text="The simplified free cash flow is ",
    )
    candidate = candidate_for(case=case, mandate=flow.mandate, claims=(claim,), as_of=flow.as_of)

    check, resolved = await PublicationService(db_session).check(
        candidate, semantic_review(candidate)
    )

    assert check.passed and not check.failures, check.field_errors
    node = resolved["recommendation.claims.0.text.1"]
    assert Decimal(node.value) == expected
    assert node.unit == "HKD"
    assert node.text == f"{expected}.00 HKD"
    assert "CNY" not in node.text
    assert node.quality == ()


# --- calculations ------------------------------------------------------------


async def test_material_calculation_hash_covers_value_unit_currency_and_period(
    db_session, tmp_path
) -> None:
    flow = await clp_case(db_session, tmp_path / "data", unknown_time=False)
    operating_cash_flow = await evidence_record(db_session, flow, "operating_cash_flow")
    capital_expenditure = await evidence_record(db_session, flow, "capex_ppe")
    same = {"method": MATERIAL_CALCULATION_METHOD, "method_version": MATERIAL_CALCULATION_VERSION}
    amount = operating_cash_flow.value.value  # type: ignore[union-attr]
    base = material_calculation_input_hash((operating_cash_flow, capital_expenditure), **same)

    assert base == material_calculation_input_hash(
        (operating_cash_flow.model_copy(), capital_expenditure.model_copy()), **same
    )
    changed_value = operating_cash_flow.model_copy(
        update={"value": Quantity(value=amount + 1, unit="HKD", currency="HKD")}
    )
    assert base != material_calculation_input_hash((changed_value, capital_expenditure), **same)
    changed_unit = operating_cash_flow.model_copy(
        update={"value": Quantity(value=amount, unit="USD", currency="USD")}
    )
    assert base != material_calculation_input_hash((changed_unit, capital_expenditure), **same)
    changed_currency = operating_cash_flow.model_copy(
        update={"value": Quantity(value=amount, unit="HKD", currency="CNY")}
    )
    assert base != material_calculation_input_hash(
        (changed_currency, capital_expenditure), **same
    )
    changed_period = operating_cash_flow.model_copy(
        update={
            "period": Period(start=date(2023, 1, 1), end=date(2023, 12, 31), basis="annual")
        }
    )
    assert base != material_calculation_input_hash(
        (changed_period, capital_expenditure), **same
    )
    assert base != material_calculation_input_hash(
        (capital_expenditure, operating_cash_flow), **same
    )
    assert base != material_calculation_input_hash(
        (operating_cash_flow, capital_expenditure), method=MATERIAL_CALCULATION_METHOD,
        method_version="material.metrics.v2",
    )


async def test_material_calculation_saves_its_facts_and_exact_input_hash(
    db_session, tmp_path
) -> None:
    flow = await clp_case(db_session, tmp_path / "data", unknown_time=False)
    operating_cash_flow_ref = flow.refs["operating_cash_flow"]
    capital_expenditure_ref = flow.refs["capex_ppe"]
    operating_cash_flow = await evidence_record(db_session, flow, "operating_cash_flow")
    capital_expenditure = await evidence_record(db_session, flow, "capex_ppe")
    expected = (
        operating_cash_flow.value.value - capital_expenditure.value.value  # type: ignore[union-attr]
    )
    result = Quantity(value=expected, unit="HKD", currency="HKD")

    ref = await save_material_calculation(
        db_session,
        listing_id=flow.listing.id,
        fact_refs=(operating_cash_flow_ref, capital_expenditure_ref),
        results={"fcf_simple": result},
        as_of=flow.as_of,
    )
    saved = (
        await EvidenceService(db_session).get_calculation("local_owner", ref, flow.as_of)
    ).data

    assert saved is not None
    assert saved.status is CalculationStatus.OK and saved.applicable
    assert saved.method == MATERIAL_CALCULATION_METHOD
    assert saved.method_version == MATERIAL_CALCULATION_VERSION == "material.metrics.v1"
    assert saved.fact_refs == (operating_cash_flow_ref, capital_expenditure_ref)
    assert saved.results == {"fcf_simple": result}
    assert saved.input_hash == material_calculation_input_hash(
        (operating_cash_flow, capital_expenditure),
        method=MATERIAL_CALCULATION_METHOD,
        method_version=MATERIAL_CALCULATION_VERSION,
    )
    # The hash is over the inputs, not just the method version: a genuinely
    # different input set never shares a calculation identity.
    assert saved.input_hash != material_calculation_input_hash(
        (
            operating_cash_flow.model_copy(update={"value": result}),
            capital_expenditure,
        ),
        method=MATERIAL_CALCULATION_METHOD,
        method_version=MATERIAL_CALCULATION_VERSION,
    )


# --- currency rules ----------------------------------------------------------


@pytest.mark.parametrize("currency", ["HKD", "USD", "TWD"])
def test_original_currencies_format_without_conversion(currency: str) -> None:
    money = format_quantity(Quantity(value="1234.5", unit=currency, currency=currency), "decimal")
    assert money == (f"1234.50 {currency}", currency, 2, "1234.50")
    assert "CNY" not in money[0] and "CNY" not in money[1]
    per_share = format_quantity(
        Quantity(value="10", unit=f"{currency}/share", currency=currency), "decimal"
    )
    assert per_share == (f"10.00 {currency}/share", f"{currency}/share", 2, "10.00")
    undeclared = format_quantity(Quantity(value="123", unit=currency), "decimal")
    assert undeclared == (f"123.00 {currency}", currency, 2, "123.00")


@pytest.mark.parametrize(
    "quantity",
    [
        # An HKD amount written in the CNY display unit is a conversion claim.
        Quantity(value="123", unit="亿元", currency="HKD"),
        Quantity(value="123", unit="CNY", currency="HKD"),
        Quantity(value="123", unit="HKD", currency="CNY"),
        Quantity(value="123", unit="HKD", currency="USD"),
        Quantity(value="123", unit="HKD/share", currency="CNY"),
        Quantity(value="123", unit="HKD", currency="JPY"),
        Quantity(value="123", unit="shares", currency="JPY"),
    ],
)
def test_cross_currency_quantities_keep_the_existing_refusal(quantity: Quantity) -> None:
    with pytest.raises(
        InvestorError, match="Cross currency publication requires a verified conversion"
    ):
        format_quantity(quantity, "decimal")


async def test_declared_cross_currency_publication_is_refused(db_session, tmp_path) -> None:
    flow = await clp_case(db_session, tmp_path / "data", unknown_time=False)
    await attach_material_evidence(db_session, case_id=flow.case.id, refs=flow.refs_ordered)
    case = await InvestorService(db_session).get_case("local_owner", flow.case.id)
    operating_cash_flow = await evidence_record(db_session, flow, "operating_cash_flow")
    capital_expenditure = await evidence_record(db_session, flow, "capex_ppe")
    declared = Quantity(
        value=operating_cash_flow.value.value - capital_expenditure.value.value,  # type: ignore[union-attr]
        unit="亿元",
        currency="HKD",
    )
    calculation = await save_material_calculation(
        db_session,
        listing_id=flow.listing.id,
        fact_refs=(flow.refs["operating_cash_flow"], flow.refs["capex_ppe"]),
        results={"fcf_simple": declared},
        as_of=flow.as_of,
    )
    claim = numeric_claim(
        calculation,
        field_path="results.fcf_simple",
        role=NumericRole.OUTPUT,
        kind="inference",
        text="The simplified free cash flow is ",
    )
    candidate = candidate_for(case=case, mandate=flow.mandate, claims=(claim,), as_of=flow.as_of)
    review = semantic_review(candidate)
    task_claim, operation = await prepared_action(db_session, candidate, review)

    result = await PublicationService(db_session).publish(
        task_claim, operation, candidate, review, base=settings()
    )

    assert not result.published
    assert result.draft_ref is not None and result.draft_ref.kind == "draft"
    assert set(result.check.field_errors.values()) == {
        "Cross currency publication requires a verified conversion"
    }
    kinds = set((await db_session.scalars(select(PublicationArtifact.kind))).all())
    assert kinds.isdisjoint({"thesis", "recommendation"})
    assert await count(db_session, PublicationOutbox) == 0
    current = await InvestorService(db_session).get_case("local_owner", case.id)
    assert current.current_recommendation_ref is None


# --- numbers must be bound to a saved reference ------------------------------


async def test_numbers_without_a_bound_reference_fail_the_check(db_session, tmp_path) -> None:
    flow = await clp_case(db_session, tmp_path / "data", unknown_time=False)
    await attach_material_evidence(db_session, case_id=flow.case.id, refs=flow.refs_ordered)
    case = await InvestorService(db_session).get_case("local_owner", flow.case.id)
    service = PublicationService(db_session)

    prose = Claim(
        id=uuid4(),
        kind="fact",
        text=(TextNode(type="text", text="Revenue reached 90964000000"),),
        materiality="context",
        support_refs=(flow.refs["revenue_operating"],),
        opposition_refs=(),
        assumption_refs=(),
        verification="verified",
        impact_if_wrong="A wrong amount changes the research observation",
    )
    candidate = candidate_for(case=case, mandate=flow.mandate, claims=(prose,), as_of=flow.as_of)
    check, resolved = await service.check(candidate, semantic_review(candidate))
    assert not check.passed
    assert any("Unreferenced quantity" in message for message in check.field_errors.values())
    assert resolved == {}

    dangling = ObjectRef(kind="evidence", id=uuid4(), revision=1)
    stub = numeric_claim(
        dangling,
        field_path="value",
        role=NumericRole.FACT,
        text="The saved annual revenue is ",
        materiality="context",
    )
    candidate = candidate_for(case=case, mandate=flow.mandate, claims=(stub,), as_of=flow.as_of)
    check, resolved = await service.check(candidate, semantic_review(candidate))
    assert not check.passed
    assert any(
        "missing, conflicted or unverified" in message
        for message in check.field_errors.values()
    )
    assert resolved == {}
