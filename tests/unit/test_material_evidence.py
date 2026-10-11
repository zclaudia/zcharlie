"""Material facts to evidence, and the research_dataset save branch (plan 07).

Covers ``docs/product/plans/hk-us-expansion/07-material-evidence.md``: the two
additive contract extensions (``source_kind="research_dataset"`` and the
optional ``date_precision``), the conversion table (time precision and its
conservative date boundary, missing values, units and currencies, verification
and conflict pass-through, locator, content hash, document provenance) and the
store branch's re-materialization refusals. Rows are built through the frozen
02 contract and repository, so this file needs neither the material store (05)
nor the adapters (03/04).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.evidence.contracts import DatePrecision, EvidenceRecord
from investment_agent.evidence.store import EvidenceService
from investment_agent.investor.common import ObjectRef
from investment_agent.investor.foundations import InvestorError
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
    content_hash,
    fact_to_payload,
    validate_draft,
)
from investment_agent.materials.contracts import (
    DatePrecision as MaterialDatePrecision,
)
from investment_agent.materials.evidence import (
    METRIC_NOT_SELECTED,
    UNKNOWN_PERIOD_START,
    UNKNOWN_PUBLICATION_TIME,
    dataset_facts,
    material_evidence,
)
from investment_agent.storage.models.evidence import SavedEvidence
from investment_agent.storage.models.research_material import ResearchDataset
from investment_agent.storage.repositories.research_material import ResearchDatasetRepository
from tests.unit._hk_us_support import seed_pilot_listings

CAPTURED_AT = datetime(2026, 10, 12, 4, 0, tzinfo=UTC)
FY2023 = FactPeriod(
    start=date(2023, 1, 1), end=date(2023, 12, 31), basis=PeriodBasis.ANNUAL, label="FY2023"
)
LEGACY_HASH = "a" * 64

LEGACY_PAYLOAD = {
    "id": "0f0ac9dd-2f83-4a1d-9e5b-8a2f7bb4e6f5",
    "revision": 1,
    "created_at": "2026-10-12T04:00:00+00:00",
    "actor": "snapshot_adapter",
    "origin_ref": None,
    "source_ref": {
        "kind": "financial_snapshot",
        "id": 7,
        "revision": 1,
        "field_path": "fields.roe",
    },
    "source_kind": "snapshot",
    "locator": "fields.roe",
    "content_hash": LEGACY_HASH,
    "subject_ref": {"kind": "listing", "id": 3, "revision": 1, "field_path": None},
    "period": {"start": "2025-01-01", "end": "2025-12-31", "basis": "annual"},
    "published_at": "2026-10-12T04:00:00+00:00",
    "acquired_at": "2026-10-12T04:00:00+00:00",
    "available_from": "2026-10-12T04:00:00+00:00",
    "verification": "candidate",
    "conflict": "none",
    "value": {"value": "0.2", "unit": "fraction", "currency": None, "unknown_reason": None},
    "text": "availability=actual; date precision; registry=v1",
    "correction_refs": [],
    "document_provenance": None,
}


def fact(
    *,
    metric: str = "revenue_operating",
    raw_value: str | None = "1200000000",
    unit: str = "HKD",
    currency: str | None = "HKD",
    raw_unit: str = "million HKD",
    scale: int = 1000000,
    missing_reason: str | None = None,
    period: FactPeriod = FY2023,
    precision: MaterialDatePrecision = MaterialDatePrecision.DATE,
    published_at: datetime | None = None,
    published_date: date | None = date(2024, 2, 16),
    accepted_at: datetime | None = None,
    source_available_at: datetime | None = None,
    original_locator: str | None = None,
    original_hash: str | None = "original-document-hash",
    verification: Verification = Verification.CANDIDATE,
    conflict: str = "none",
    conflict_note: str | None = None,
    source: str = "eastmoney_hk",
) -> FactRecord:
    """One contract-valid material fact; defaults are a dated HKEX revenue row."""

    return FactRecord(
        metric=metric,
        period=period,
        semantics=FactSemantics(
            metric=metric,
            source_label="营业额",
            source_field="REVENUE",
            accounting_standard=AccountingStandard.IFRS,
            reporting_scope=ReportingScope.CONSOLIDATED,
            equity_scope=EquityScope.UNKNOWN,
            per_share_basis=PerShareBasis.NA,
        ),
        value=FactValue(
            raw_value=raw_value,
            raw_unit=raw_unit,
            scale=scale,
            unit=unit,
            currency=currency,
            sign_convention="positive_is_credit",
            missing_reason=missing_reason,
        ),
        provenance=FactProvenance(
            source=source,
            accession=None,
            original_hash=original_hash,
            source_published_at=published_at,
            source_published_date=published_date,
            source_published_precision=precision,
            accepted_at=accepted_at,
            source_available_at=source_available_at,
        ),
        verification=verification,
        conflict=conflict,
        applicability="applicable",
        conflict_note=conflict_note,
        original_locator=original_locator,
        source_version=None,
    )


async def store_dataset(
    session: AsyncSession,
    facts: tuple[FactRecord, ...],
    *,
    kind: DatasetKind = DatasetKind.ANNUAL_FACTS,
    subject_kind: SubjectKind = SubjectKind.COMPANY,
    subject_id: int = 1,
    source: str = "eastmoney_hk",
    period: FactPeriod | None = FY2023,
    captured_at: datetime = CAPTURED_AT,
    adapter_version: str = "hk-native-1",
    source_specific: dict[str, Any] | None = None,
) -> ResearchDataset:
    """Insert one dataset version exactly as a validated draft would be stored."""

    payload: dict[str, Any] = {
        "facts": [fact_to_payload(item) for item in facts],
        **(source_specific or {"pageNumber": 1, "pageSize": 20}),
    }
    draft = DatasetDraft(
        kind=kind,
        subject_kind=subject_kind,
        subject_id=subject_id,
        source=source,
        schema_version=SCHEMA_VERSION,
        adapter_version=adapter_version,
        captured_at=captured_at,
        payload=payload,
        period=period,
    )
    problems = validate_draft(draft)
    assert problems == (), problems  # the fixture must be what 02 admits
    return await ResearchDatasetRepository(session).insert_version(
        ResearchDataset(
            kind=kind.value,
            subject_kind=subject_kind.value,
            subject_id=subject_id,
            source=source,
            schema_version=SCHEMA_VERSION,
            adapter_version=adapter_version,
            period_end=None if period is None else period.end,
            period_basis=None if period is None else period.basis.value,
            content_hash=content_hash(draft),
            captured_at=captured_at,
            payload=payload,
        )
    )


def listing_ref(listing: Any) -> ObjectRef:
    return ObjectRef(kind="listing", id=listing.id, revision=1)


def listing_ref_for(listing_id: int) -> ObjectRef:
    """A syntactically valid listing subject; company datasets are not bound to it."""

    return ObjectRef(kind="listing", id=listing_id, revision=1)


async def evidence_count(session: AsyncSession) -> int:
    return int((await session.scalar(select(func.count()).select_from(SavedEvidence))) or 0)


# --- contract extensions ---------------------------------------------------


def test_evidence_and_material_date_precisions_share_one_vocabulary() -> None:
    assert {member.value for member in DatePrecision} == {
        member.value for member in MaterialDatePrecision
    }
    assert [member.value for member in DatePrecision] == ["timestamp", "date", "unknown"]


def test_existing_payloads_without_date_precision_still_deserialize() -> None:
    legacy = EvidenceRecord.model_validate(LEGACY_PAYLOAD)
    assert legacy.date_precision is None
    assert legacy.source_kind == "snapshot"
    # A stored A-share payload keeps validating and re-serializes with the new key.
    dumped = legacy.model_dump(mode="json")
    assert dumped["date_precision"] is None
    assert EvidenceRecord.model_validate(dumped) == legacy


def test_research_dataset_references_use_integer_ids() -> None:
    ref = ObjectRef(kind="research_dataset", id=12, revision=3)
    assert ref.field_path is None
    with pytest.raises(ValueError, match="integer IDs"):
        ObjectRef(kind="research_dataset", id=uuid4(), revision=1)


# --- conversion table ------------------------------------------------------


async def test_date_precision_keeps_the_day_and_waits_for_the_next_day(db_session) -> None:
    row = await store_dataset(db_session, (fact(),))
    records, skipped = material_evidence(row, subject_ref=listing_ref_for(1))
    assert skipped == () and len(records) == 1
    record = records[0]
    assert record.published_at == datetime(2024, 2, 16, tzinfo=UTC)
    assert record.available_from == datetime(2024, 2, 17, tzinfo=UTC)
    assert record.date_precision is DatePrecision.DATE
    assert record.acquired_at == CAPTURED_AT
    assert record.source_kind == "research_dataset"


async def test_date_precision_is_never_earlier_than_the_acceptance_time(db_session) -> None:
    early = fact(accepted_at=datetime(2024, 2, 16, 1, 30, tzinfo=UTC))
    late = fact(
        metric="net_profit",
        raw_value="77",
        accepted_at=datetime(2024, 2, 18, 9, 0, tzinfo=UTC),
    )
    row = await store_dataset(db_session, (early, late))
    records, _ = material_evidence(row, subject_ref=listing_ref_for(1))
    by_amount = {record.value.value: record for record in records}
    # The next day's start is later than that acceptance time, so it wins.
    assert by_amount[Decimal("1200000000")].available_from == datetime(2024, 2, 17, tzinfo=UTC)
    assert (
        by_amount[Decimal("1200000000")].available_from
        > by_amount[Decimal("1200000000")].published_at
    )
    # A later acceptance time is the honest boundary: the day alone says nothing.
    assert by_amount[Decimal("77")].available_from == datetime(2024, 2, 18, 9, 0, tzinfo=UTC)
    assert by_amount[Decimal("77")].date_precision is DatePrecision.DATE


async def test_timestamp_precision_keeps_the_moment_and_the_acceptance_time(db_session) -> None:
    accepted = datetime(2023, 2, 16, 21, 19, 28, tzinfo=UTC)
    row = await store_dataset(
        db_session,
        (
            fact(
                precision=MaterialDatePrecision.TIMESTAMP,
                published_at=accepted,
                published_date=None,
                accepted_at=accepted,
            ),
        ),
    )
    records, skipped = material_evidence(row, subject_ref=listing_ref_for(1))
    assert skipped == ()
    assert records[0].published_at == accepted
    assert records[0].available_from == accepted
    assert records[0].date_precision is DatePrecision.TIMESTAMP


async def test_unknown_publication_time_stays_in_the_material_layer(db_session) -> None:
    row = await store_dataset(
        db_session,
        (
            fact(
                precision=MaterialDatePrecision.UNKNOWN,
                published_at=None,
                published_date=None,
                accepted_at=datetime(2024, 3, 1, tzinfo=UTC),
                source_available_at=datetime(2024, 3, 2, tzinfo=UTC),
            ),
            # A "timestamp" claim without the stated moment is not a date either.
            fact(
                metric="net_profit",
                precision=MaterialDatePrecision.TIMESTAMP,
                published_at=None,
                published_date=date(2024, 2, 16),
            ),
        ),
    )
    records, skipped = material_evidence(row, subject_ref=listing_ref_for(1))
    assert records == ()
    assert skipped == (UNKNOWN_PUBLICATION_TIME, UNKNOWN_PUBLICATION_TIME)
    # Neither accepted_at nor source_available_at is promoted to a publication time.
    assert [item.metric for item in dataset_facts(row)] == ["revenue_operating", "net_profit"]


async def test_missing_value_is_kept_with_its_reason_and_never_becomes_zero(db_session) -> None:
    row = await store_dataset(
        db_session, (fact(raw_value=None, missing_reason="not_reported_in_source"),)
    )
    records, skipped = material_evidence(row, subject_ref=listing_ref_for(1))
    assert skipped == ()
    record = records[0]
    assert record.value is not None
    assert record.value.value is None
    assert record.value.unknown_reason == "not_reported_in_source"
    assert record.value.unit == "HKD" and record.value.currency == "HKD"
    assert record.text == "source states no value for revenue_operating: not_reported_in_source"
    assert record.document_provenance is None


async def test_units_and_currencies_are_carried_through(db_session) -> None:
    row = await store_dataset(
        db_session,
        (
            fact(unit="USD", currency="USD", raw_unit="USD"),
            fact(
                metric="net_profit",
                unit="TWD",
                currency="TWD",
                raw_unit="TWD thousand",
                raw_value="410000",
                scale=1000,
            ),
            fact(
                metric="eps_basic",
                unit="USD/share",
                currency="USD",
                raw_unit="USD/share",
                raw_value="6.13",
                scale=1,
            ),
            fact(metric="total_equity", unit="ratio", currency=None, raw_unit="ratio"),
        ),
    )
    records, skipped = material_evidence(row, subject_ref=listing_ref_for(1))
    assert skipped == ()
    assert [record.value.currency for record in records if record.value] == [
        "USD",
        "TWD",
        "USD",
        None,
    ]
    assert records[1].value is not None and records[1].value.value == Decimal("410000")
    assert records[3].value is not None
    assert records[3].value.unit == "ratio" and records[3].value.currency is None


async def test_verification_and_conflict_states_are_copied_verbatim(db_session) -> None:
    row = await store_dataset(
        db_session,
        (
            fact(),
            fact(metric="net_profit", verification=Verification.VERIFIED),
            fact(metric="total_equity", conflict="resolved"),
        ),
    )
    records, _ = material_evidence(row, subject_ref=listing_ref_for(1))
    assert [record.verification for record in records] == ["candidate", "verified", "candidate"]
    assert [record.conflict for record in records] == ["none", "none", "resolved"]


async def test_unresolved_conflict_is_passed_through_and_read_as_a_conflict(db_session) -> None:
    listing = (await seed_pilot_listings(db_session))[("HKEX", "00002")]
    row = await store_dataset(
        db_session,
        (fact(conflict="unresolved", conflict_note="two sources disagree on revenue"),),
        subject_kind=SubjectKind.LISTING,
        subject_id=listing.id,
    )
    records, _ = material_evidence(row, subject_ref=listing_ref(listing))
    assert records[0].conflict == "unresolved"
    store = EvidenceService(db_session)
    ref = await store.save_evidence("local_owner", records[0], uuid4())
    result = await store.get_evidence("local_owner", ref, datetime.now(UTC) + timedelta(days=1))
    assert result.status == "conflict"
    assert result.data is not None and result.data.conflict == "unresolved"


async def test_period_basis_is_copied_and_instant_points_keep_one_date(db_session) -> None:
    interim = FactPeriod(
        start=date(2024, 1, 1), end=date(2024, 6, 30), basis=PeriodBasis.INTERIM, label=None
    )
    instant = FactPeriod(start=None, end=date(2024, 6, 30), basis=PeriodBasis.INSTANT)
    row = await store_dataset(
        db_session,
        (
            fact(period=interim),
            fact(metric="total_equity", period=instant, raw_unit="HKD", raw_value="88"),
            fact(
                metric="net_profit",
                period=FactPeriod(start=None, end=date(2023, 12, 31), basis=PeriodBasis.YTD),
            ),
        ),
    )
    records, skipped = material_evidence(row, subject_ref=listing_ref_for(1))
    assert [(record.period.basis, record.period.start) for record in records if record.period] == [
        ("interim", date(2024, 1, 1)),
        ("instant", date(2024, 6, 30)),
    ]
    assert records[1].period is not None and records[1].period.end == date(2024, 6, 30)
    assert skipped == (UNKNOWN_PERIOD_START,)


async def test_locator_content_hash_and_provenance_identify_the_fact(db_session) -> None:
    row = await store_dataset(
        db_session,
        (
            fact(original_locator="page 42"),
            fact(metric="net_profit", original_locator="page 43"),
        ),
    )
    records, _ = material_evidence(row, subject_ref=listing_ref_for(1))
    first, second = records
    assert first.locator == "eastmoney_hk:annual_facts:FY2023:page 42"
    assert set(first.locator.split(":")) == {"eastmoney_hk", "annual_facts", "FY2023", "page 42"}
    assert first.content_hash != second.content_hash
    assert first.content_hash != row.content_hash
    assert first.id != second.id
    provenance = first.document_provenance
    assert provenance is not None
    assert provenance.metric == "revenue_operating"
    assert provenance.raw_unit == "million HKD"
    assert provenance.raw_currency == "HKD"
    assert provenance.raw_value == "1200000000"
    assert provenance.original_hash == "original-document-hash"
    assert provenance.reporting_scope == "consolidated"
    assert provenance.parser_version == "hk-native-1"
    assert provenance.fragment_hash and provenance.verification_reason


async def test_a_fact_without_a_locator_has_no_document_provenance(db_session) -> None:
    row = await store_dataset(db_session, (fact(original_locator=None),))
    records, _ = material_evidence(row, subject_ref=listing_ref_for(1))
    assert records[0].document_provenance is None
    assert records[0].text is None
    assert records[0].locator == "eastmoney_hk:annual_facts:FY2023"


async def test_same_fact_converts_to_the_same_record(db_session) -> None:
    row = await store_dataset(db_session, (fact(), fact(metric="net_profit")))
    first, _ = material_evidence(row, subject_ref=listing_ref_for(1), created_at=CAPTURED_AT)
    second, _ = material_evidence(row, subject_ref=listing_ref_for(1), created_at=CAPTURED_AT)
    assert first == second
    assert first[0].content_hash == second[0].content_hash
    assert first[0].id == second[0].id


async def test_metric_filter_explains_every_skipped_fact(db_session) -> None:
    row = await store_dataset(
        db_session,
        (fact(), fact(metric="net_profit"), fact(metric="total_equity")),
    )
    records, skipped = material_evidence(
        row, subject_ref=listing_ref_for(1), metrics=["net_profit"]
    )
    assert skipped == (METRIC_NOT_SELECTED, METRIC_NOT_SELECTED)
    assert len(records) == 1
    assert records[0].document_provenance is None  # the metric is the fact's own
    everything, _ = material_evidence(row, subject_ref=listing_ref_for(1))
    assert len(everything) == 3


async def test_dataset_facts_uses_the_frozen_payload_contract(db_session) -> None:
    row = await store_dataset(
        db_session,
        (fact(), fact(metric="net_profit")),
        source_specific={"pageNumber": 2, "pageSize": 50, "unmapped_labels": ["other"]},
    )
    facts = dataset_facts(row)
    assert [item.metric for item in facts] == ["revenue_operating", "net_profit"]
    assert facts[0].period.label == "FY2023"
    assert facts[0].value.raw_value == "1200000000"
    assert facts[0].original_locator is None
    # Source-specific top-level keys are never interpreted here; a payload
    # without the canonical facts list is stored-data drift.
    detached = ResearchDataset(
        kind="annual_facts",
        subject_kind="company",
        subject_id=1,
        source="eastmoney_hk",
        schema_version=SCHEMA_VERSION,
        adapter_version="hk-native-1",
        content_hash=LEGACY_HASH,
        captured_at=CAPTURED_AT,
        payload={"pageNumber": 1},
    )
    with pytest.raises(ValueError, match="facts"):
        dataset_facts(detached)


async def test_naive_captured_at_from_sqlite_is_read_as_utc(db_session) -> None:
    row = await store_dataset(db_session, (fact(),))
    await db_session.commit()
    await db_session.refresh(row)
    assert row.captured_at.tzinfo is None  # SQLite drops the offset
    records, _ = material_evidence(row, subject_ref=listing_ref_for(1))
    assert records[0].acquired_at == CAPTURED_AT


# --- subject binding -------------------------------------------------------


async def test_company_and_listing_subject_bindings_are_checked(db_session) -> None:
    company_dataset = await store_dataset(
        db_session, (fact(),), subject_kind=SubjectKind.COMPANY, subject_id=11
    )
    # ObjectRef has no company kind: the check is on the company id space.
    company = ObjectRef(kind="research_dataset", id=11, revision=1)
    records, _ = material_evidence(
        company_dataset, subject_ref=listing_ref_for(1), company_ref=company
    )
    assert len(records) == 1
    with pytest.raises(ValueError, match="company reference"):
        material_evidence(
            company_dataset,
            subject_ref=listing_ref_for(1),
            company_ref=ObjectRef(kind="research_dataset", id=12, revision=1),
        )
    listing_dataset = await store_dataset(
        db_session, (fact(),), subject_kind=SubjectKind.LISTING, subject_id=5
    )
    assert len(material_evidence(listing_dataset, subject_ref=listing_ref_for(5))[0]) == 1
    with pytest.raises(ValueError, match="listing does not match"):
        material_evidence(listing_dataset, subject_ref=listing_ref_for(6))
    with pytest.raises(ValueError, match="listing subject reference"):
        material_evidence(
            listing_dataset, subject_ref=ObjectRef(kind="market_snapshot", id=5, revision=1)
        )


# --- store branch ----------------------------------------------------------


async def test_save_evidence_stores_a_material_fact_and_replays(db_session) -> None:
    listing = (await seed_pilot_listings(db_session))[("HKEX", "00002")]
    row = await store_dataset(
        db_session, (fact(),), subject_kind=SubjectKind.LISTING, subject_id=listing.id
    )
    records, _ = material_evidence(row, subject_ref=listing_ref(listing))
    store = EvidenceService(db_session)
    key = uuid4()
    ref = await store.save_evidence("local_owner", records[0], key)
    assert ref.id != records[0].id and ref.revision == 1
    assert ref == await store.save_evidence("local_owner", records[0], key)
    # Strict visibility: nothing is readable before acquired/available boundaries
    # (the boundary itself is inclusive, so one second earlier is the probe).
    probe = CAPTURED_AT - timedelta(seconds=1)
    assert (await store.get_evidence("local_owner", ref, probe)).data is None
    saved = (
        await store.get_evidence("local_owner", ref, CAPTURED_AT + timedelta(days=1))
    ).data
    assert saved is not None
    assert saved.actor == "material_adapter"
    assert saved.source_kind == "research_dataset"
    assert saved.source_ref == ObjectRef(kind="research_dataset", id=row.id, revision=row.revision)
    assert saved.date_precision is DatePrecision.DATE
    assert saved.value is not None and saved.value.currency == "HKD"
    payload = (await db_session.get(SavedEvidence, (str(ref.id), 1))).payload
    assert payload["date_precision"] == "date" and payload["source_kind"] == "research_dataset"
    assert payload["locator"] == "eastmoney_hk:annual_facts:FY2023"


async def test_save_evidence_rejects_a_tampered_amount(db_session) -> None:
    listing = (await seed_pilot_listings(db_session))[("HKEX", "00002")]
    row = await store_dataset(
        db_session, (fact(),), subject_kind=SubjectKind.LISTING, subject_id=listing.id
    )
    records, _ = material_evidence(row, subject_ref=listing_ref(listing))
    store = EvidenceService(db_session)
    ref = await store.save_evidence("local_owner", records[0], uuid4())
    stored = (await db_session.get(SavedEvidence, (str(ref.id), 1))).payload
    stored["value"]["value"] = "999"
    tampered = EvidenceRecord.model_validate(stored)
    with pytest.raises(InvestorError) as error:
        await store.save_evidence("local_owner", tampered, uuid4())
    assert "value does not match saved source" in str(error.value)
    assert await evidence_count(db_session) == 1


async def test_save_evidence_rejects_every_forged_field(db_session) -> None:
    listing = (await seed_pilot_listings(db_session))[("HKEX", "00002")]
    other_listing = (await seed_pilot_listings(db_session))[("HKEX", "00005")]
    row = await store_dataset(
        db_session, (fact(),), subject_kind=SubjectKind.LISTING, subject_id=listing.id
    )
    records, _ = material_evidence(row, subject_ref=listing_ref(listing))
    store = EvidenceService(db_session)
    original = records[0]
    assert original.value is not None
    forged: dict[str, Any] = {
        # No material says "verified", so claiming it is refused, not trusted.
        "verification": "verified",
        "published_at": datetime(2024, 2, 16, 12, tzinfo=UTC),
        "available_from": datetime(2024, 2, 16, 12, tzinfo=UTC),
        "acquired_at": CAPTURED_AT + timedelta(days=1),
        "date_precision": DatePrecision.TIMESTAMP,
        "locator": "eastmoney_hk:annual_facts:FY2023:page 1",
        "content_hash": "forged-hash",
        "conflict": "resolved",
        "period": original.period.model_copy(update={"end": date(2022, 12, 31)})
        if original.period
        else None,
        "value": original.value.model_copy(update={"value": Decimal("1")}),
        "subject_ref": listing_ref(other_listing),
        "source_ref": ObjectRef(kind="listing", id=listing.id, revision=1),
    }
    for name, value in forged.items():
        with pytest.raises(InvestorError) as error:
            await store.save_evidence(
                "local_owner", original.model_copy(update={name: value}), uuid4()
            )
        assert error.value.detail.code == "invalid_reference", name
    assert await evidence_count(db_session) == 0


async def test_save_evidence_requires_the_exact_stored_version_and_subject(db_session) -> None:
    listing = (await seed_pilot_listings(db_session))[("HKEX", "00002")]
    row = await store_dataset(
        db_session, (fact(),), subject_kind=SubjectKind.LISTING, subject_id=listing.id
    )
    records, _ = material_evidence(row, subject_ref=listing_ref(listing))
    store = EvidenceService(db_session)
    record = records[0]
    # A research_dataset reference without its kind, and a version that does not exist.
    with pytest.raises(InvestorError, match="source reference is required"):
        await store.save_evidence(
            "local_owner",
            record.model_copy(
                update={"source_ref": ObjectRef(kind="listing", id=listing.id, revision=1)}
            ),
            uuid4(),
        )
    with pytest.raises(InvestorError, match="version does not exist"):
        await store.save_evidence(
            "local_owner",
            record.model_copy(
                update={
                    "source_ref": ObjectRef(kind="research_dataset", id=row.id + 10_000, revision=1)
                }
            ),
            uuid4(),
        )
    with pytest.raises(InvestorError, match="version does not exist"):
        await store.save_evidence(
            "local_owner",
            record.model_copy(
                update={
                    "source_ref": ObjectRef(
                        kind="research_dataset", id=row.id, revision=row.revision + 1
                    )
                }
            ),
            uuid4(),
        )
    assert await evidence_count(db_session) == 0


async def test_a_share_snapshot_evidence_still_saves_without_date_precision(db_session) -> None:
    from investment_agent.evidence.snapshots import materialize
    from investment_agent.storage.repositories.financial_snapshot import (
        FinancialSnapshotRepository,
    )
    from investment_agent.storage.repositories.listing import ListingRepository

    listing = await ListingRepository(db_session).ensure_a_share_listing(
        "600519.SH", company_name="贵州茅台"
    )
    row = await FinancialSnapshotRepository(db_session).insert(
        listing_id=listing.id,
        report_period=date(2025, 12, 31),
        period_kind="annual",
        fields={"net_profit": 123456789},
        provider="baostock",
        captured_at=CAPTURED_AT - timedelta(days=1),
        availability="actual",
        available_from=date(2026, 4, 17),
    )
    store = EvidenceService(db_session)
    ref = await store.save_evidence("local_owner", materialize(row, "net_profit_total"), uuid4())
    saved = (await store.get_evidence("local_owner", ref, datetime.now(UTC))).data
    assert saved is not None
    assert saved.source_kind == "snapshot"
    assert saved.date_precision is None
    assert saved.actor == "snapshot_adapter"
