"""WP1 peer storage and shared contracts: repositories, immutability, ObjectRef
kinds, contract round-trips and the default-off configuration."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import delete, text, update
from sqlalchemy.exc import IntegrityError

from investment_agent.agent.execution import ExecutionGrant
from investment_agent.config import Settings
from investment_agent.evidence.contracts import Period
from investment_agent.industry_peers.contracts import (
    BINDING_METRIC_KEYS,
    PeerAnnualField,
    PeerContextBinding,
    PeerContextStatus,
    PeerFieldVerification,
    PeerMetricBinding,
    PeerMetricStatus,
    PeerQuantity,
)
from investment_agent.investor.common import ObjectRef, Quantity
from investment_agent.storage.models.analysis import ResearchSnapshot
from investment_agent.storage.models.peer import (
    ImmutablePeerSnapshotError,
    PeerAnnualSnapshot,
    PeerCohortSnapshot,
)
from investment_agent.storage.repositories.peer import (
    PeerAnnualSnapshotRepository,
    PeerCohortSnapshotRepository,
)

ACQUIRED_AT = datetime(2026, 10, 12, 3, 30, tzinfo=UTC)


def make_cohort(**overrides: Any) -> PeerCohortSnapshot:
    fields: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "cohort_key": "baijiu_current",
        "revision": 1,
        "observed_on": date(2026, 9, 30),
        "acquired_at": ACQUIRED_AT,
        "verification": "verified",
        "inclusion_rule_version": "run-02@2026-10-12",
        "content_hash": "a" * 64,
        "payload": {
            "classification": {"system": "sw", "version": "2021", "code": "340501"},
            "index_code": "851251.SI",
            "entries": [
                {
                    "company_id": 1,
                    "listing_id": 11,
                    "ticker": "600809.SH",
                    "name": "山西汾酒",
                    "flags": {"is_st": False, "mixed_business": False},
                }
            ],
            "included": [{"company_id": 1, "reason": "classification member"}],
            "excluded": [],
        },
    }
    fields.update(overrides)
    return PeerCohortSnapshot(**fields)


def make_annual(**overrides: Any) -> PeerAnnualSnapshot:
    fields: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "company_id": 1,
        "listing_id": 11,
        "report_period": date(2025, 12, 31),
        "provider": "eastmoney",
        "adapter_version": "annual-1",
        "captured_at": ACQUIRED_AT,
        "content_hash": "b" * 64,
        "semantic_hash": "c" * 64,
        "fields": {
            "operating_revenue": {
                "metric": "operating_revenue",
                "value": "31978000000.00",
                "unit": "CNY",
                "currency": "CNY",
                "period": {"start": "2025-01-01", "end": "2025-12-31", "basis": "annual"},
                "source_field": "OPERATE_INCOME",
                "raw_value": "319.78",
                "raw_unit": "亿元",
                "acquired_at": ACQUIRED_AT.isoformat(),
                "verification": "verified",
            }
        },
        "verification": {
            "operating_revenue": {"page": 4, "checked_at": ACQUIRED_AT.isoformat()}
        },
    }
    fields.update(overrides)
    return PeerAnnualSnapshot(**fields)


# --- cohort repository ------------------------------------------------------


async def test_cohort_revisions_append_and_query_by_version(db_session) -> None:
    repo = PeerCohortSnapshotRepository(db_session)
    first = await repo.add(make_cohort())
    assert await repo.next_revision("baijiu_current") == 2

    second = await repo.add(
        make_cohort(revision=2, content_hash="d" * 64, observed_on=date(2026, 10, 12))
    )
    assert second.revision == 2

    latest = await repo.latest(cohort_key="baijiu_current")
    assert latest is not None and latest.id == second.id and latest.id != first.id
    found = await repo.by_revision(cohort_key="baijiu_current", revision=1)
    assert found is not None and found.id == first.id
    assert await repo.by_revision(cohort_key="baijiu_current", revision=3) is None
    assert [row.revision for row in await repo.history(cohort_key="baijiu_current")] == [1, 2]
    # Another cohort starts at its own revision 1.
    other = await repo.add(make_cohort(cohort_key="baijiu_next", content_hash="e" * 64))
    assert other.revision == 1
    found = await repo.by_content_hash(cohort_key="baijiu_current", content_hash="a" * 64)
    assert found is not None and found.id == first.id
    assert await repo.by_content_hash(cohort_key="baijiu_next", content_hash="a" * 64) is None


async def test_cohort_add_rejects_unidentifiable_rows(db_session) -> None:
    repo = PeerCohortSnapshotRepository(db_session)
    with pytest.raises(ValueError, match="cohort_key"):
        await repo.add(make_cohort(cohort_key=""))
    with pytest.raises(ValueError, match="revision"):
        await repo.add(make_cohort(revision=0))
    with pytest.raises(ValueError, match="content_hash"):
        await repo.add(make_cohort(content_hash=""))


# --- annual repository ------------------------------------------------------


async def test_annual_unique_key_deduplicates_concurrent_inserts(db_session) -> None:
    repo = PeerAnnualSnapshotRepository(db_session)
    first = await repo.add(make_annual())
    found = await repo.by_unique_key(
        company_id=1,
        report_period=date(2025, 12, 31),
        provider="eastmoney",
        adapter_version="annual-1",
        semantic_hash="c" * 64,
    )
    assert found is not None and found.id == first.id
    # A second insert with the same key is rejected: dedup happens by lookup,
    # never by overwriting (a retry reuses the saved row).
    with pytest.raises(IntegrityError):
        await repo.add(make_annual())
    await db_session.rollback()
    # A different semantic hash is a new version, same identity otherwise.
    version = await repo.add(make_annual(semantic_hash="f" * 64, content_hash="9" * 64))
    assert version.id != first.id


async def test_annual_versions_query_selects_explicit_versions(db_session) -> None:
    repo = PeerAnnualSnapshotRepository(db_session)
    eastmoney = await repo.add(make_annual())
    akshare = await repo.add(
        make_annual(
            provider="akshare",
            semantic_hash="d" * 64,
            content_hash="e" * 64,
            captured_at=ACQUIRED_AT.replace(hour=4),
        )
    )
    other_period = await repo.add(
        make_annual(report_period=date(2024, 12, 31), semantic_hash="f" * 64, content_hash="1" * 64)
    )
    other_company = await repo.add(
        make_annual(company_id=2, listing_id=12, semantic_hash="2" * 64, content_hash="3" * 64)
    )

    all_sources = await repo.versions(company_id=1, report_period=date(2025, 12, 31))
    assert [row.id for row in all_sources] == [eastmoney.id, akshare.id]
    one_source = await repo.versions(
        company_id=1, report_period=date(2025, 12, 31), provider="eastmoney"
    )
    assert [row.id for row in one_source] == [eastmoney.id]
    # Companies and periods stay separated - no cross-identity mixing.
    assert [row.id for row in await repo.versions(company_id=1, report_period=date(2024, 12, 31))] == [
        other_period.id
    ]
    assert [
        row.id for row in await repo.versions(company_id=2, report_period=date(2025, 12, 31))
    ] == [other_company.id]
    assert await repo.versions(company_id=2, report_period=date(2024, 12, 31)) == []


async def test_annual_add_rejects_unidentifiable_rows(db_session) -> None:
    repo = PeerAnnualSnapshotRepository(db_session)
    with pytest.raises(ValueError, match="company_id"):
        await repo.add(make_annual(company_id=0))
    with pytest.raises(ValueError, match="provider"):
        await repo.add(make_annual(provider=""))
    with pytest.raises(ValueError, match="semantic_hash"):
        await repo.add(make_annual(semantic_hash=""))


# --- immutability (no update/delete path + trigger protection) --------------


async def test_cohort_rows_refuse_orm_updates_and_deletes(db_session) -> None:
    repo = PeerCohortSnapshotRepository(db_session)
    row = await repo.add(make_cohort())
    row_id = row.id
    await db_session.commit()

    stored = await db_session.get(PeerCohortSnapshot, row_id)
    assert stored is not None
    stored.payload = {"tampered": True}
    with pytest.raises(ImmutablePeerSnapshotError):
        await db_session.flush()
    await db_session.rollback()

    stored = await db_session.get(PeerCohortSnapshot, row_id)
    assert stored is not None
    await db_session.delete(stored)
    with pytest.raises(ImmutablePeerSnapshotError):
        await db_session.flush()
    await db_session.rollback()
    assert await db_session.get(PeerCohortSnapshot, row_id) is not None


async def test_annual_rows_refuse_orm_updates_and_deletes(db_session) -> None:
    repo = PeerAnnualSnapshotRepository(db_session)
    row = await repo.add(make_annual())
    row_id = row.id
    await db_session.commit()

    stored = await db_session.get(PeerAnnualSnapshot, row_id)
    assert stored is not None
    stored.fields = {"tampered": True}
    with pytest.raises(ImmutablePeerSnapshotError):
        await db_session.flush()
    await db_session.rollback()

    stored = await db_session.get(PeerAnnualSnapshot, row_id)
    assert stored is not None
    await db_session.delete(stored)
    with pytest.raises(ImmutablePeerSnapshotError):
        await db_session.flush()
    await db_session.rollback()
    assert await db_session.get(PeerAnnualSnapshot, row_id) is not None


@pytest.mark.parametrize("model_name", ["cohort", "annual"])
@pytest.mark.parametrize("operation", ["update", "delete", "replace"])
async def test_bulk_sql_cannot_mutate_saved_peer_snapshots(db_session, model_name, operation) -> None:
    model = PeerCohortSnapshot if model_name == "cohort" else PeerAnnualSnapshot
    if model is PeerCohortSnapshot:
        row: PeerCohortSnapshot | PeerAnnualSnapshot = await (
            PeerCohortSnapshotRepository(db_session).add(make_cohort())
        )
    else:
        row = await PeerAnnualSnapshotRepository(db_session).add(make_annual())
    await db_session.commit()
    row_id = row.id
    statement = (
        # ``verification`` is the one mutable-looking column both tables share;
        # the point is that no saved row may change at all.
        update(model).where(model.id == row_id).values(verification="{}")
        if operation == "update"
        else delete(model).where(model.id == row_id)
        if operation == "delete"
        else text(
            f"INSERT OR REPLACE INTO {model.__tablename__} "
            f"SELECT * FROM {model.__tablename__} WHERE id = :id"
        ).bindparams(id=row_id)
    )
    with pytest.raises(IntegrityError, match="immutable"):
        await db_session.execute(statement)
    await db_session.rollback()
    saved = await db_session.get(model, row_id)
    assert saved is not None
    if isinstance(saved, PeerCohortSnapshot):
        assert saved.payload != {"tampered": True}
        assert saved.verification == "verified"
    else:
        assert saved.fields != {"tampered": True}


# --- ObjectRef kinds --------------------------------------------------------


def test_peer_objectref_kinds_use_uuids() -> None:
    cohort_id, annual_id = uuid.uuid4(), uuid.uuid4()
    cohort_ref = ObjectRef(kind="peer_cohort_snapshot", id=cohort_id, revision=1)
    annual_ref = ObjectRef(
        kind="peer_annual_snapshot", id=annual_id, revision=1, field_path="fields.roe"
    )
    assert cohort_ref.id == cohort_id and annual_ref.field_path == "fields.roe"
    # Existing storage keeps integer ids; the peer snapshots are new UUID objects.
    with pytest.raises(ValidationError):
        ObjectRef(kind="peer_cohort_snapshot", id=1, revision=1)
    with pytest.raises(ValidationError):
        ObjectRef(kind="peer_annual_snapshot", id=1, revision=1)


# --- contracts --------------------------------------------------------------


def test_status_enums_match_the_design() -> None:
    assert {member.value for member in PeerContextStatus} == {
        "ready", "partial", "pending", "not_supported", "missing", "conflict", "stale", "failed",
    }
    assert {member.value for member in PeerMetricStatus} == {
        "comparable", "target_not_applicable", "insufficient_peers", "missing", "conflict",
    }
    assert {member.value for member in PeerFieldVerification} == {"candidate", "verified"}


def test_peer_quantity_is_the_existing_quantity_contract() -> None:
    assert PeerQuantity is Quantity
    assert PeerQuantity.model_json_schema() == Quantity.model_json_schema()
    quantity = PeerQuantity(value="32.53", unit="percent", currency=None)
    dumped = quantity.model_dump(mode="json")
    assert dumped == {"value": "32.53", "unit": "percent", "currency": None, "unknown_reason": None}
    # Decimal precision survives the round trip; floats do not get in.
    assert PeerQuantity.model_validate(dumped) == quantity
    with pytest.raises(ValidationError):
        PeerQuantity(value=32.53, unit="percent")
    unknown = PeerQuantity(value=None, unit="CNY", unknown_reason="source missing the field")
    assert PeerQuantity.model_validate(unknown.model_dump(mode="json")) == unknown
    with pytest.raises(ValidationError):
        PeerQuantity(value=None, unit="CNY")


def test_peer_annual_field_serialization_round_trip() -> None:
    field = PeerAnnualField(
        metric="operating_revenue",
        value="31978000000.00",
        unit="CNY",
        currency="CNY",
        period=Period(
            start=date(2025, 1, 1), end=date(2025, 12, 31), basis="period_to_date/annual"
        ),
        source_field="OPERATE_INCOME",
        raw_value="319.78",
        raw_unit="亿元",
        acquired_at=ACQUIRED_AT,
        verification=PeerFieldVerification.VERIFIED,
    )
    dumped = field.model_dump(mode="json")
    assert dumped["value"] == "31978000000.00"  # decimal string, never a float
    assert dumped["period"] == {
        "start": "2025-01-01", "end": "2025-12-31", "basis": "period_to_date/annual",
    }
    assert PeerAnnualField.model_validate(dumped) == field

    # Point-in-time stocks use the Dec 31 point with an explicit basis.
    stock = PeerAnnualField(
        metric="equity_parent",
        value="18000000000.00",
        unit="CNY",
        currency="CNY",
        period=Period(start=date(2025, 12, 31), end=date(2025, 12, 31), basis="point_in_time"),
        acquired_at=ACQUIRED_AT,
    )
    assert PeerAnnualField.model_validate(stock.model_dump(mode="json")) == stock

    # Missing is explicit, zero is a value, and floats/NaN are rejected.
    missing = PeerAnnualField(
        metric="contract_liabilities",
        value=None,
        unit="CNY",
        period=stock.period,
        acquired_at=ACQUIRED_AT,
        missing_reason="source column absent",
    )
    assert missing.value is None and missing.missing_reason is not None
    with pytest.raises(ValidationError):
        PeerAnnualField(
            metric="contract_liabilities",
            value=None,
            unit="CNY",
            period=stock.period,
            acquired_at=ACQUIRED_AT,
        )
    with pytest.raises(ValidationError):
        PeerAnnualField(
            metric="contract_liabilities",
            value=19.5,
            unit="CNY",
            period=stock.period,
            acquired_at=ACQUIRED_AT,
        )


# --- execution grant / research snapshot compatibility ----------------------


def test_execution_grant_defaults_peer_context_to_null() -> None:
    grant = ExecutionGrant(
        mode="interactive",
        source="authenticated_message",
        prompt="研究一下这家公司",
    )
    assert grant.peer_context is None
    dumped = grant.model_dump(mode="json")
    assert dumped["peer_context"] is None
    assert ExecutionGrant.model_validate(dumped).peer_context is None

    # Grants serialized before WP1 (no key at all) keep validating.
    del dumped["peer_context"]
    assert ExecutionGrant.model_validate(dumped).peer_context is None

    # WP11 typed the reserved placeholder as the exact PeerContextBinding
    # contract: a bound grant round-trips that one strongly typed shape, and a
    # partial dict (any drifted second form) is rejected.
    bound = grant.model_copy(
        update={
            "peer_context": PeerContextBinding(
                target="600809.SH",
                cohort_ref=ObjectRef(
                    kind="peer_cohort_snapshot", id=uuid.UUID(int=1), revision=1
                ),
                batch_run_id="r1",
                report_period=date(2025, 12, 31),
                cutoff=datetime(2026, 10, 12, 4, 0, tzinfo=UTC),
                metric_calculation_refs=(
                    PeerMetricBinding(
                        metric_key=key,
                        state="gap",
                        ref=None,
                        gap_reason="fixture",
                    )
                    for key in BINDING_METRIC_KEYS
                ),
            )
        }
    )
    restored = ExecutionGrant.model_validate(bound.model_dump(mode="json"))
    assert restored.peer_context is not None
    assert restored.peer_context.target == "600809.SH"
    assert restored.peer_context.batch_run_id == "r1"
    assert bound.model_dump(mode="json") == restored.model_dump(mode="json")
    with pytest.raises(ValidationError):
        ExecutionGrant.model_validate(
            {**bound.model_dump(mode="json"), "peer_context": {"target": "600809.SH"}}
        )


async def test_research_snapshot_row_keeps_a_null_binding(db_session) -> None:
    snapshot = ResearchSnapshot(
        pipeline_run_id=1,
        symbol="600809.SH",
        market="SSE",
        company_name="山西汾酒",
        industry="白酒",
        as_of=date(2026, 10, 12),
        report_year=2025,
        canonical_schema_version="v1",
        scoring_version="v1",
        score_schema_version="v1",
        score_json={"items": {}},
        explanation_json=None,
        artifact_manifest={},
    )
    db_session.add(snapshot)
    await db_session.flush()
    # Snapshots written before the feature (and without a binding) stay null;
    # the column must never be backfilled with a newer result.
    assert snapshot.industry_peer_context is None


# --- configuration ----------------------------------------------------------


def test_industry_peer_context_defaults_to_off() -> None:
    settings = Settings(_env_file=None, api_key="test", llm_tier3_api_key="k")  # type: ignore[call-arg]
    assert settings.industry_peer_context_enabled is False
    assert settings.peer_collect_min_interval_seconds == 1.0
    assert settings.peer_collect_retries == 2

    from investment_agent.settings.registry import RuntimeSettings, defaults

    runtime = RuntimeSettings()
    assert runtime.industry_peer_context_enabled is False
    flat = defaults()
    assert flat["industry_peer_context_enabled"] is False
    assert flat["peer_collect_min_interval_seconds"] == 1.0
    assert flat["peer_collect_retries"] == 2
