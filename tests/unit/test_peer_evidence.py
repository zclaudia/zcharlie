"""WP4 trusted field adapter and evidence materialization.

The adapter turns one saved peer annual snapshot field into an
``EvidenceRecord``; ``EvidenceService.save_evidence`` re-derives the record
from the stored row before accepting it, so ``verified`` can only come from
the saved snapshot plus a complete per-field proof. Covered here: faithful
materialization, the verified/candidate boundary, forged-upload rejection,
materialization idempotency (stable operation identity, payload mismatch
refusal), version separation after a snapshot update, the dedicated
conflict/correction query (a supplier update is never a correction), and the
no-date-basis fields that stay in the snapshot only.

The run-02 golden rows imported by WP3 carry no source dates at all, so they
are exactly the no-date-basis case: they stay in the snapshot layer and
produce no ``EvidenceRecord`` until a version that states its dates exists.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest
from pytest_asyncio import fixture as async_fixture
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.evidence.contracts import DatePrecision, EvidenceRecord, Period
from investment_agent.evidence.store import EvidenceService
from investment_agent.industry_peers.contracts import PeerAnnualField, PeerFieldVerification
from investment_agent.industry_peers.evidence import (
    ADAPTER_ACTOR,
    UNKNOWN_PUBLICATION_TIME,
    peer_field_evidence,
    peer_field_operation_id,
    peer_field_states,
    peer_source_ref,
)
from investment_agent.investor.common import ObjectRef
from investment_agent.investor.foundations import InvestorError
from investment_agent.storage.models.evidence import SavedEvidence
from investment_agent.storage.models.peer import PeerAnnualSnapshot
from investment_agent.storage.repositories.listing import ListingRepository
from investment_agent.storage.repositories.peer import PeerAnnualSnapshotRepository

ACQUIRED_AT = datetime(2026, 10, 12, 4, 0, tzinfo=UTC)
NOTICE_DATE = date(2026, 4, 25)
PERIOD_END = date(2025, 12, 31)
EXCERPT = "营业收入（元） 25,731,010,647.32 31,196,248,208.33"
METRIC = "operating_revenue"


def annual_field(
    *,
    metric: str = METRIC,
    value: str | None = "25731010647.32",
    unit: str = "CNY",
    currency: str | None = "CNY",
    period: Period | None = None,
    verification: PeerFieldVerification = PeerFieldVerification.VERIFIED,
    disclosed_on: date | None = NOTICE_DATE,
    updated_on: date | None = None,
    missing_reason: str | None = None,
    source_field: str = "OPERATE_INCOME",
    raw_value: str | None = "25,731,010,647.32",
    raw_unit: str = "CNY",
) -> PeerAnnualField:
    """One contract-valid field entry; defaults are a dated, verified revenue row."""

    return PeerAnnualField(
        metric=metric,
        value=Decimal(value) if value is not None else None,
        unit=unit,
        currency=currency,
        period=period
        or Period(start=date(2025, 1, 1), end=PERIOD_END, basis="period_to_date/annual"),
        source_field=source_field,
        raw_value=raw_value,
        raw_unit=raw_unit,
        disclosed_on=disclosed_on,
        updated_on=updated_on,
        acquired_at=ACQUIRED_AT,
        verification=verification,
        missing_reason=missing_reason,
    )


def audit_proof(metric: str = METRIC, **overrides: Any) -> dict[str, Any]:
    """A complete per-field proof in the WP3 shape (everything non-empty that
    binds the value to the checked original)."""

    proof: dict[str, Any] = {
        "metric": metric,
        "status": "verified",
        "original_ref": None,
        "pdf_hash": None,
        "report_identity": None,
        "pdf_page": 7,
        "header": "营业收入",
        "raw_value": "25,731,010,647.32",
        "raw_unit": "CNY",
        "parsed_value": "25731010647.32",
        "period": PERIOD_END.isoformat(),
        "accounting_scope": "consolidated",
        "locator_excerpt": EXCERPT,
        "locator_excerpt_sha256": hashlib.sha256(EXCERPT.encode("utf-8")).hexdigest(),
        "checked_at": None,
        "checked_by": None,
        "check_rule_version": None,
        "audit_record": {
            "source": "baijiu-official-audit.json",
            "index": 0,
            "code": "600519.SH",
            "field": "营业收入",
            "api_key": "OPERATE_INCOME",
        },
    }
    proof.update(overrides)
    return proof


def candidate_proof(metric: str = "revenue_total_check", **overrides: Any) -> dict[str, Any]:
    """The check-only proof shape: no original audit record exists."""

    proof: dict[str, Any] = {
        "metric": metric,
        "status": "candidate",
        "original_ref": None,
        "pdf_hash": None,
        "report_identity": None,
        "pdf_page": None,
        "header": None,
        "raw_value": "25731010647.32",
        "raw_unit": "CNY",
        "parsed_value": "25731010647.32",
        "period": PERIOD_END.isoformat(),
        "accounting_scope": None,
        "locator_excerpt": None,
        "locator_excerpt_sha256": None,
        "checked_at": None,
        "checked_by": None,
        "check_rule_version": None,
        "audit_record": None,
        "note": "check-only field (never a ratio denominator)",
    }
    proof.update(overrides)
    return proof


async def store_snapshot(
    session: AsyncSession,
    listing: Any,
    *,
    fields: dict[str, PeerAnnualField] | None = None,
    proofs: dict[str, dict[str, Any]] | None = None,
    semantic_hash: str = "a" * 64,
    captured_at: datetime = ACQUIRED_AT,
    provider: str = "eastmoney",
    adapter_version: str = "offline-run02-v1",
) -> PeerAnnualSnapshot:
    """Append one snapshot version exactly as the WP3 importer would store it."""

    field_map = fields if fields is not None else {METRIC: annual_field()}
    proof_map = (
        proofs
        if proofs is not None
        else {METRIC: audit_proof(METRIC)}
    )
    if proofs is None:
        from tests.support.peer_originals import synthetic_original_proofs
        proof_map = await synthetic_original_proofs(session, "600519.SH", listing.id, field_map, captured_at)
    return await PeerAnnualSnapshotRepository(session).add(
        PeerAnnualSnapshot(
            id=str(uuid4()),
            company_id=listing.company_id,
            listing_id=listing.id,
            report_period=PERIOD_END,
            provider=provider,
            adapter_version=adapter_version,
            captured_at=captured_at,
            content_hash=semantic_hash,
            semantic_hash=semantic_hash,
            fields={name: item.model_dump(mode="json") for name, item in field_map.items()},
            verification=proof_map,
        )
    )


async def evidence_count(session: AsyncSession) -> int:
    return int((await session.scalar(select(func.count()).select_from(SavedEvidence))) or 0)


@async_fixture
async def maotai(db_session) -> Any:
    return await ListingRepository(db_session).ensure_a_share_listing(
        "600519.SH", company_name="贵州茅台"
    )


# --- trusted adaptation -------------------------------------------------------


async def test_materialization_carries_subject_source_quantity_period(db_session, maotai) -> None:
    row = await store_snapshot(db_session, maotai)
    result = peer_field_evidence(row, METRIC)
    assert result.materialized and result.reason is None
    record = result.record
    assert record is not None
    assert record.subject_ref == ObjectRef(kind="listing", id=maotai.id, revision=1)
    assert record.source_ref == ObjectRef(
        kind="peer_annual_snapshot", id=UUID(row.id), revision=1, field_path=f"fields.{METRIC}"
    )
    assert record.source_kind == "snapshot"
    assert record.locator == f"fields.{METRIC}"
    assert record.value is not None
    assert record.value.value == Decimal("25731010647.32")  # decimal string preserved, not float
    assert record.value.unit == "CNY" and record.value.currency == "CNY"
    assert record.value.unknown_reason is None
    assert record.period == Period(
        start=date(2025, 1, 1), end=PERIOD_END, basis="period_to_date/annual"
    )
    # NOTICE_DATE keeps version-notice semantics: day precision, never a clock time.
    assert record.date_precision is DatePrecision.DATE
    assert record.published_at == datetime(2026, 4, 25, tzinfo=UTC)
    # Knowable only from the next day - or from the import, which is later here.
    assert record.available_from == ACQUIRED_AT
    assert record.acquired_at == ACQUIRED_AT
    assert record.verification == "verified"
    assert record.conflict == "none"
    assert record.actor == ADAPTER_ACTOR
    # Derived identity: the same version rebuilds byte-identically.
    assert record.id == uuid5(NAMESPACE_URL, record.content_hash)
    assert peer_field_evidence(row, METRIC).record == record


async def test_zero_is_a_value_and_missing_stays_unknown(db_session, maotai) -> None:
    zero = await store_snapshot(
        db_session,
        maotai,
        fields={METRIC: annual_field(value="0.00", raw_value="0.00")},
        semantic_hash="b" * 64,
    )
    record = peer_field_evidence(zero, METRIC).record
    assert record is not None and record.value is not None
    assert record.value.value == Decimal("0.00") and record.value.unknown_reason is None

    missing = await store_snapshot(
        db_session,
        maotai,
        fields={
            "operating_cash_flow": annual_field(
                metric="operating_cash_flow",
                value=None,
                missing_reason="source value is null",
                source_field="NETCASH_OPERATE",
                raw_value=None,
            )
        },
        proofs={"operating_cash_flow": candidate_proof("operating_cash_flow", raw_value=None)},
        semantic_hash="c" * 64,
    )
    record = peer_field_evidence(missing, "operating_cash_flow").record
    assert record is not None and record.value is not None
    assert record.value.value is None
    assert record.value.unknown_reason == "source value is null"


async def test_verified_requires_a_complete_proof(db_session, maotai) -> None:
    row = await store_snapshot(db_session, maotai)
    record = peer_field_evidence(row, METRIC).record
    assert record is not None and record.verification == "verified"

    # A verified field whose proof misses a key item degrades to candidate.
    for missing_key in ("pdf_page", "header", "raw_value", "parsed_value",
                        "locator_excerpt", "locator_excerpt_sha256", "audit_record"):
        incomplete = await store_snapshot(
            db_session,
            maotai,
            fields={METRIC: annual_field()},
            proofs={METRIC: audit_proof(METRIC, **{missing_key: None})},
            semantic_hash=hashlib.sha256(missing_key.encode()).hexdigest(),
        )
        record = peer_field_evidence(incomplete, METRIC).record
        assert record is not None, missing_key
        assert record.verification == "candidate", missing_key
        assert "verified proof incomplete" in (record.text or ""), missing_key

    # No proof at all: candidate, never verified.
    bare = await store_snapshot(
        db_session, maotai, proofs={METRIC: {}}, semantic_hash="d" * 64
    )
    record = peer_field_evidence(bare, METRIC).record
    assert record is not None and record.verification == "candidate"

    # A candidate field stays candidate even with a complete-looking proof.
    candidate = await store_snapshot(
        db_session,
        maotai,
        fields={METRIC: annual_field(verification=PeerFieldVerification.CANDIDATE)},
        proofs={METRIC: audit_proof(METRIC, status="candidate")},
        semantic_hash="e" * 64,
    )
    record = peer_field_evidence(candidate, METRIC).record
    assert record is not None and record.verification == "candidate"


async def test_revision_date_alone_is_the_fallback_notice_basis(db_session, maotai) -> None:
    row = await store_snapshot(
        db_session,
        maotai,
        fields={METRIC: annual_field(disclosed_on=None, updated_on=date(2026, 6, 1))},
        semantic_hash="f" * 64,
    )
    record = peer_field_evidence(row, METRIC).record
    assert record is not None
    assert record.published_at == datetime(2026, 6, 1, tzinfo=UTC)
    assert record.available_from == ACQUIRED_AT
    assert record.date_precision is DatePrecision.DATE
    assert "supplier revision date" in (record.text or "")

    # An explicit disclosure date wins over the revision date.
    both = await store_snapshot(
        db_session,
        maotai,
        fields={
            METRIC: annual_field(disclosed_on=NOTICE_DATE, updated_on=date(2026, 6, 1))
        },
        semantic_hash="0" * 64,
    )
    record = peer_field_evidence(both, METRIC).record
    assert record is not None
    assert record.published_at == datetime(2026, 4, 25, tzinfo=UTC)
    assert "version-notice date" in (record.text or "")


async def test_field_without_any_source_date_is_not_materialized(db_session, maotai) -> None:
    """The run-02 shape: no disclosed_on, no updated_on - snapshot only."""

    row = await store_snapshot(
        db_session,
        maotai,
        fields={METRIC: annual_field(disclosed_on=None, updated_on=None)},
        semantic_hash="1" * 64,
    )
    result = peer_field_evidence(row, METRIC)
    assert result.record is None
    assert result.reason == UNKNOWN_PUBLICATION_TIME

    store = EvidenceService(db_session)
    with pytest.raises(InvestorError) as error:
        await store.save_evidence(
            "local_owner",
            EvidenceRecord(
                id=uuid4(),
                revision=1,
                created_at=ACQUIRED_AT,
                actor="model",
                origin_ref=None,
                source_ref=peer_source_ref(row, METRIC),
                source_kind="snapshot",
                locator=f"fields.{METRIC}",
                content_hash="fabricated",
                subject_ref=ObjectRef(kind="listing", id=maotai.id, revision=1),
                period=Period(
                    start=date(2025, 1, 1), end=PERIOD_END, basis="period_to_date/annual"
                ),
                published_at=ACQUIRED_AT,
                acquired_at=ACQUIRED_AT,
                available_from=ACQUIRED_AT,
                verification="verified",
                conflict="none",
                value=None,
                text="no-date field claimed as materializable and verified",
            ),
            uuid4(),
        )
    assert error.value.detail.code == "invalid_reference"
    assert "not materializable" in str(error.value)
    assert UNKNOWN_PUBLICATION_TIME in str(error.value)
    assert await evidence_count(db_session) == 0


# --- the trusted save branch --------------------------------------------------


async def test_save_evidence_stores_the_field_and_replays(db_session, maotai) -> None:
    row = await store_snapshot(db_session, maotai)
    result = peer_field_evidence(row, METRIC)
    assert result.record is not None
    store = EvidenceService(db_session)
    key = peer_field_operation_id(row, METRIC)
    ref = await store.save_evidence("local_owner", result.record, key)
    # The same rebuild under the same derived operation identity replays.
    again = peer_field_evidence(row, METRIC)
    assert again.record is not None
    assert await store.save_evidence("local_owner", again.record, key) == ref
    assert await evidence_count(db_session) == 1
    # The derived identity is stable: it is a function of version + locator.
    assert key == peer_field_operation_id(row, METRIC)

    probe = ACQUIRED_AT - timedelta(seconds=1)
    assert (await store.get_evidence("local_owner", ref, probe)).data is None
    saved = (await store.get_evidence("local_owner", ref, ACQUIRED_AT)).data
    assert saved is not None
    assert saved.actor == ADAPTER_ACTOR
    assert saved.source_kind == "snapshot"
    assert saved.source_ref == ObjectRef(
        kind="peer_annual_snapshot", id=UUID(row.id), revision=1, field_path=f"fields.{METRIC}"
    )
    assert saved.verification == "verified"
    assert (await store.get_evidence("local_owner", ref, ACQUIRED_AT)).warnings == ()
    payload = (await db_session.get(SavedEvidence, (str(ref.id), 1))).payload
    assert payload["date_precision"] == "date"
    assert payload["value"]["value"] == "25731010647.32"


async def test_candidate_field_saves_as_candidate_and_warns(db_session, maotai) -> None:
    row = await store_snapshot(
        db_session,
        maotai,
        fields={
            "revenue_total_check": annual_field(
                metric="revenue_total_check",
                source_field="TOTAL_OPERATE_INCOME",
                verification=PeerFieldVerification.CANDIDATE,
            )
        },
        proofs={"revenue_total_check": candidate_proof()},
        semantic_hash="2" * 64,
    )
    result = peer_field_evidence(row, "revenue_total_check")
    assert result.record is not None and result.record.verification == "candidate"
    store = EvidenceService(db_session)
    ref = await store.save_evidence(
        "local_owner", result.record, peer_field_operation_id(row, "revenue_total_check")
    )
    saved = (await store.get_evidence("local_owner", ref, ACQUIRED_AT)).data
    assert saved is not None and saved.verification == "candidate"
    assert (await store.get_evidence("local_owner", ref, ACQUIRED_AT)).warnings == (
        "verification=candidate",
    )
    assert await evidence_count(db_session) == 1


async def test_forged_verified_upload_is_rejected(db_session, maotai) -> None:
    row = await store_snapshot(
        db_session,
        maotai,
        fields={
            "revenue_total_check": annual_field(
                metric="revenue_total_check",
                source_field="TOTAL_OPERATE_INCOME",
                verification=PeerFieldVerification.CANDIDATE,
            )
        },
        proofs={"revenue_total_check": candidate_proof()},
        semantic_hash="3" * 64,
    )
    result = peer_field_evidence(row, "revenue_total_check")
    assert result.record is not None
    genuine = result.record
    store = EvidenceService(db_session)
    await store.save_evidence(
        "local_owner", genuine, peer_field_operation_id(row, "revenue_total_check")
    )

    other = await ListingRepository(db_session).ensure_a_share_listing(
        "000858.SZ", company_name="五粮液"
    )
    assert genuine.period is not None and genuine.value is not None
    forgeries: dict[str, Any] = {
        # A client/model claiming verified for a candidate field: refused.
        "verification": "verified",
        "published_at": datetime(2026, 4, 25, 12, tzinfo=UTC),
        "available_from": datetime(2026, 4, 26, tzinfo=UTC),
        "acquired_at": ACQUIRED_AT + timedelta(days=1),
        "date_precision": DatePrecision.TIMESTAMP,
        "locator": "fields.invented",
        "content_hash": "forged-hash",
        "conflict": "resolved",
        "period": genuine.period.model_copy(update={"end": date(2024, 12, 31)}),
        "value": genuine.value.model_copy(update={"value": Decimal("999")}),
        "subject_ref": ObjectRef(kind="listing", id=other.id, revision=1),
        "text": "rewritten quality note",
    }
    for name, value in forgeries.items():
        with pytest.raises(InvestorError) as error:
            await store.save_evidence(
                "local_owner", genuine.model_copy(update={name: value}), uuid4()
            )
        assert error.value.detail.code == "invalid_reference", name
        assert "does not match saved source" in str(error.value), name
    assert await evidence_count(db_session) == 1


async def test_same_operation_identity_with_new_payload_is_rejected(db_session, maotai) -> None:
    row = await store_snapshot(db_session, maotai)
    result = peer_field_evidence(row, METRIC)
    assert result.record is not None
    store = EvidenceService(db_session)
    key = peer_field_operation_id(row, METRIC)
    await store.save_evidence("local_owner", result.record, key)
    # A retry that submits a re-timed payload under the same operation
    # identity is refused: rebuilds must reuse the saved snapshot's times.
    for shifted in (
        result.record.model_copy(update={"acquired_at": ACQUIRED_AT + timedelta(hours=1)}),
        result.record.model_copy(
            update={"published_at": datetime(2026, 4, 25, 8, tzinfo=UTC)}
        ),
    ):
        with pytest.raises(InvestorError) as error:
            await store.save_evidence("local_owner", shifted, key)
        assert error.value.detail.code == "idempotency_conflict"
    assert await evidence_count(db_session) == 1


async def test_snapshot_versions_stay_separate(db_session, maotai) -> None:
    old = await store_snapshot(db_session, maotai, semantic_hash="4" * 64)
    new = await store_snapshot(
        db_session,
        maotai,
        fields={METRIC: annual_field(value="26000000000.00", raw_value="26,000,000,000.00")},
        proofs={METRIC: audit_proof(METRIC, raw_value="26,000,000,000.00",
                                    parsed_value="26000000000.00")},
        semantic_hash="5" * 64,
        captured_at=ACQUIRED_AT + timedelta(days=1),
    )
    assert old.id != new.id
    old_result = peer_field_evidence(old, METRIC)
    new_result = peer_field_evidence(new, METRIC)
    assert old_result.record is not None and new_result.record is not None
    # Different versions are different evidence: identity, hash and source ref.
    assert old_result.record.id != new_result.record.id
    assert old_result.record.content_hash != new_result.record.content_hash
    assert old_result.record.source_ref != new_result.record.source_ref

    store = EvidenceService(db_session)
    old_ref = await store.save_evidence(
        "local_owner", old_result.record, peer_field_operation_id(old, METRIC)
    )
    new_ref = await store.save_evidence(
        "local_owner", new_result.record, peer_field_operation_id(new, METRIC)
    )
    assert old_ref != new_ref
    assert await evidence_count(db_session) == 2
    saved_old = (await store.get_evidence("local_owner", old_ref, ACQUIRED_AT + timedelta(days=2))).data
    saved_new = (await store.get_evidence("local_owner", new_ref, ACQUIRED_AT + timedelta(days=2))).data
    assert saved_old is not None and saved_new is not None
    assert saved_old.value is not None and saved_new.value is not None
    assert saved_old.value.value == Decimal("25731010647.32")
    assert saved_new.value.value == Decimal("26000000000.00")

    # A stale rebuild that claims the new version's locator is refused.
    with pytest.raises(InvestorError) as error:
        await store.save_evidence(
            "local_owner",
            old_result.record.model_copy(update={"source_ref": new_result.record.source_ref}),
            uuid4(),
        )
    assert "does not match saved source" in str(error.value)
    # The old version's own derived identity still replays its own receipt.
    replay = peer_field_evidence(old, METRIC)
    assert replay.record is not None
    assert await store.save_evidence(
        "local_owner", replay.record, peer_field_operation_id(old, METRIC)
    ) == old_ref
    assert await evidence_count(db_session) == 2


async def test_peer_source_through_other_upload_paths_is_rejected(db_session, maotai) -> None:
    row = await store_snapshot(db_session, maotai)
    result = peer_field_evidence(row, METRIC)
    assert result.record is not None
    store = EvidenceService(db_session)
    # A dataset-style upload announcing a peer source is refused by its path.
    with pytest.raises(InvestorError, match="source reference is required"):
        await store.save_evidence(
            "local_owner",
            result.record.model_copy(update={"source_kind": "research_dataset"}),
            uuid4(),
        )
    # A user report must carry its listing subject as the source.
    with pytest.raises(InvestorError, match="User report source must be its listing subject"):
        await store.save_evidence(
            "local_owner",
            result.record.model_copy(update={"source_kind": "user_report"}),
            uuid4(),
        )
    # Cohort rosters are not fact evidence at all.
    with pytest.raises(InvestorError, match="Unsupported saved event source"):
        await store.save_evidence(
            "local_owner",
            result.record.model_copy(
                update={
                    "source_ref": ObjectRef(
                        kind="peer_cohort_snapshot",
                        id=UUID(row.id),
                        revision=1,
                        field_path=f"fields.{METRIC}",
                    )
                }
            ),
            uuid4(),
        )
    assert await evidence_count(db_session) == 0


async def test_branch_rejects_bad_snapshot_references(db_session, maotai) -> None:
    row = await store_snapshot(db_session, maotai)
    result = peer_field_evidence(row, METRIC)
    assert result.record is not None
    store = EvidenceService(db_session)
    with pytest.raises(InvestorError, match="Snapshot version does not exist"):
        await store.save_evidence(
            "local_owner",
            result.record.model_copy(
                update={
                    "source_ref": ObjectRef(
                        kind="peer_annual_snapshot",
                        id=uuid4(),
                        revision=1,
                        field_path=f"fields.{METRIC}",
                    )
                }
            ),
            uuid4(),
        )
    with pytest.raises(InvestorError, match="Snapshot field is not supported"):
        await store.save_evidence(
            "local_owner",
            result.record.model_copy(
                update={
                    "source_ref": ObjectRef(
                        kind="peer_annual_snapshot",
                        id=UUID(row.id),
                        revision=1,
                        field_path="values.operating_revenue",
                    )
                }
            ),
            uuid4(),
        )
    with pytest.raises(InvestorError, match="Snapshot field is not supported"):
        await store.save_evidence(
            "local_owner",
            result.record.model_copy(
                update={
                    "source_ref": ObjectRef(
                        kind="peer_annual_snapshot",
                        id=UUID(row.id),
                        revision=2,
                        field_path=f"fields.{METRIC}",
                    )
                }
            ),
            uuid4(),
        )
    with pytest.raises(InvestorError, match="Snapshot field is not supported"):
        await store.save_evidence(
            "local_owner",
            result.record.model_copy(
                update={
                    "source_ref": ObjectRef(
                        kind="peer_annual_snapshot",
                        id=UUID(row.id),
                        revision=1,
                        field_path="fields.missing_metric",
                    )
                }
            ),
            uuid4(),
        )
    assert await evidence_count(db_session) == 0


# --- conflict and correction lookup -------------------------------------------


async def test_supplier_update_is_pending_recheck_never_a_correction(db_session, maotai) -> None:
    old = await store_snapshot(db_session, maotai, semantic_hash="6" * 64)
    new = await store_snapshot(
        db_session,
        maotai,
        fields={METRIC: annual_field(value="26000000000.00", raw_value="26,000,000,000.00")},
        proofs={METRIC: audit_proof(METRIC, raw_value="26,000,000,000.00",
                                    parsed_value="26000000000.00")},
        semantic_hash="7" * 64,
        captured_at=ACQUIRED_AT + timedelta(days=1),
    )
    report = await peer_field_states(db_session, company_id=maotai.company_id, metric=METRIC)
    # The supplier refresh does not decide the old value was wrong: it is a
    # new version awaiting re-check, with no correction references attached.
    assert report.status == "pending_recheck"
    assert report.correction_refs == ()
    assert {state.state for state in report.versions} == {"pending_recheck"}
    assert [state.source_ref.id for state in report.versions] == [UUID(old.id), UUID(new.id)]
    assert [state.value_text for state in report.versions] == [
        "25731010647.32",
        "26000000000.00",
    ]
    assert report.warnings == ()


async def test_explicit_correction_basis_connects_refs(db_session, maotai) -> None:
    old = await store_snapshot(db_session, maotai, semantic_hash="8" * 64)
    new = await store_snapshot(
        db_session,
        maotai,
        fields={METRIC: annual_field(value="26000000000.00", raw_value="26,000,000,000.00")},
        proofs={
            METRIC: audit_proof(
                METRIC,
                raw_value="26,000,000,000.00",
                parsed_value="26000000000.00",
                corrects=old.id,
            )
        },
        semantic_hash="9" * 64,
        captured_at=ACQUIRED_AT + timedelta(days=1),
    )
    report = await peer_field_states(db_session, company_id=maotai.company_id, metric=METRIC)
    assert report.status == "corrected"
    assert report.correction_refs == (peer_source_ref(old, METRIC),)
    by_ref = {state.source_ref.id: state for state in report.versions}
    assert by_ref[UUID(old.id)].state == "corrected"
    assert by_ref[UUID(new.id)].state == "correction"
    assert by_ref[UUID(new.id)].corrects == (peer_source_ref(old, METRIC),)


async def test_correction_of_an_unknown_version_is_reported_not_connected(
    db_session, maotai
) -> None:
    await store_snapshot(db_session, maotai, semantic_hash="a0" * 32)
    later = await store_snapshot(
        db_session,
        maotai,
        fields={METRIC: annual_field(value="26000000000.00", raw_value="26,000,000,000.00")},
        proofs={
            METRIC: audit_proof(
                METRIC,
                raw_value="26,000,000,000.00",
                parsed_value="26000000000.00",
                corrects=str(uuid4()),
            )
        },
        semantic_hash="a1" * 32,
        captured_at=ACQUIRED_AT + timedelta(days=1),
    )
    report = await peer_field_states(db_session, company_id=maotai.company_id, metric=METRIC)
    assert report.status == "pending_recheck"
    assert report.correction_refs == ()
    assert any("not part of this query" in warning for warning in report.warnings)
    states = {state.state for state in report.versions}
    assert states == {"pending_recheck"}
    assert UUID(later.id) in {state.source_ref.id for state in report.versions}


async def test_query_filters_by_period_provider_unit_and_scope(db_session, maotai) -> None:
    other = await ListingRepository(db_session).ensure_a_share_listing(
        "000568.SZ", company_name="泸州老窖"
    )
    await store_snapshot(db_session, maotai, semantic_hash="b0" * 32, proofs={METRIC: audit_proof(accounting_scope=None)})
    # Same company, same metric semantics, but a different unit: a different
    # comparability group, never a value conflict.
    await store_snapshot(
        db_session,
        maotai,
        fields={
            METRIC: annual_field(
                value="25731010.64732", unit="CNY/million", currency="CNY", raw_unit="million CNY"
            )
        },
        proofs={METRIC: audit_proof(METRIC, raw_unit="million CNY", accounting_scope=None,
                                    raw_value="25,731,010.64732",
                                    parsed_value="25731010.64732")},
        semantic_hash="b1" * 32,
    )
    # Another company's row never enters this company's query.
    await store_snapshot(db_session, other, semantic_hash="b2" * 32)

    report = await peer_field_states(db_session, company_id=maotai.company_id, metric=METRIC)
    assert report.status == "consistent"  # equal values, different units: no conflict
    assert len(report.versions) == 2

    narrowed = await peer_field_states(
        db_session, company_id=maotai.company_id, metric=METRIC, unit="CNY"
    )
    assert narrowed.status == "consistent"
    assert len(narrowed.versions) == 1

    empty = await peer_field_states(
        db_session, company_id=maotai.company_id, metric=METRIC, unit="USD"
    )
    assert empty.versions == () and empty.status == "consistent"

    provider_narrow = await peer_field_states(
        db_session, company_id=maotai.company_id, metric=METRIC, provider="eastmoney"
    )
    assert len(provider_narrow.versions) == 2
    assert (
        await peer_field_states(
            db_session, company_id=maotai.company_id, metric=METRIC, provider="ifind"
        )
    ).versions == ()

    # An accounting scope is matched only when a proof actually states one.
    scoped = await store_snapshot(
        db_session,
        maotai,
        fields={METRIC: annual_field(value="1.00")},
        proofs={METRIC: audit_proof(METRIC, accounting_scope="consolidated")},
        semantic_hash="b3" * 32,
    )
    consolidated = await peer_field_states(
        db_session,
        company_id=maotai.company_id,
        metric=METRIC,
        accounting_scope="consolidated",
    )
    assert [state.source_ref.id for state in consolidated.versions] == [UUID(scoped.id)]

    # A different report period is a separate query scope.
    assert (
        await peer_field_states(
            db_session,
            company_id=maotai.company_id,
            metric=METRIC,
            report_period=date(2024, 12, 31),
        )
    ).versions == ()


async def test_cross_provider_disagreement_is_conflict(db_session, maotai) -> None:
    await store_snapshot(db_session, maotai, semantic_hash="c0" * 32, provider="eastmoney")
    await store_snapshot(
        db_session,
        maotai,
        fields={METRIC: annual_field(value="25731010647.99", raw_value="25,731,010,647.99")},
        proofs={METRIC: audit_proof(METRIC, raw_value="25,731,010,647.99",
                                    parsed_value="25731010647.99")},
        semantic_hash="c1" * 32,
        provider="ifind",
    )
    report = await peer_field_states(db_session, company_id=maotai.company_id, metric=METRIC)
    assert report.status == "conflict"
    assert {state.state for state in report.versions} == {"conflict"}
    assert report.correction_refs == ()


async def test_metric_versions_repository_lookup(db_session, maotai) -> None:
    await store_snapshot(
        db_session,
        maotai,
        fields={METRIC: annual_field()},
        proofs={METRIC: audit_proof(METRIC)},
        semantic_hash="d0" * 32,
    )
    await store_snapshot(
        db_session,
        maotai,
        fields={"revenue_total_check": annual_field(metric="revenue_total_check",
                                                    source_field="TOTAL_OPERATE_INCOME")},
        proofs={"revenue_total_check": candidate_proof()},
        semantic_hash="d1" * 32,
    )
    repo = PeerAnnualSnapshotRepository(db_session)
    revenue_rows = await repo.metric_versions(company_id=maotai.company_id, metric=METRIC)
    assert len(revenue_rows) == 1
    check_rows = await repo.metric_versions(
        company_id=maotai.company_id, metric="revenue_total_check"
    )
    assert len(check_rows) == 1
    provider_rows = await repo.metric_versions(
        company_id=maotai.company_id, metric=METRIC, provider="ifind"
    )
    assert provider_rows == []
    # The lookup stays read-only: nothing was appended by querying.
    assert int(
        (await db_session.scalar(select(func.count()).select_from(PeerAnnualSnapshot))) or 0
    ) == 2
