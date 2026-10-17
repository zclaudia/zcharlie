from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import delete, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from investment_agent.investor.foundations import InvestorError
from investment_agent.storage.models import Base
from investment_agent.storage.models.supplemental_updates import (
    SupplementalChangeReceiptRow,
)
from investment_agent.storage.repositories.supplemental_updates import (
    SupplementalUpdatesRepository,
)
from investment_agent.supplemental_rules.canonical import canonical_hash


@pytest.mark.asyncio
async def test_receipts_are_immutable_idempotent_owner_scoped_and_members_are_unique(db_session):
    repo = SupplementalUpdatesRepository(db_session)
    operation = uuid4()
    first = await repo.append_change(
        "local_owner",
        change_key="rule:one",
        kind="rule_revision",
        operation_id=operation,
        request_hash="a" * 64,
        payload={"source_ref": {"id": "one"}},
    )
    replay = await repo.append_change(
        "local_owner",
        change_key="rule:one",
        kind="rule_revision",
        operation_id=operation,
        request_hash="a" * 64,
        payload={"source_ref": {"id": "one"}},
        committed_at=first.committed_at,
    )
    assert replay.id == first.id and replay.sequence == first.sequence
    with pytest.raises(InvestorError):
        await repo.append_change(
            "local_owner",
            change_key="rule:one",
            kind="rule_revision",
            operation_id=operation,
            request_hash="a" * 64,
            payload={"source_ref": {"id": "one"}},
            committed_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
    for statement in (
        update(SupplementalChangeReceiptRow)
        .where(SupplementalChangeReceiptRow.sequence == first.sequence)
        .values(payload={"tampered": True}),
        delete(SupplementalChangeReceiptRow).where(
            SupplementalChangeReceiptRow.sequence == first.sequence
        ),
    ):
        with pytest.raises(IntegrityError):
            async with db_session.begin_nested():
                await db_session.execute(statement)
    with pytest.raises(InvestorError) as conflict:
        await repo.append_change(
            "local_owner",
            change_key="rule:one",
            kind="rule_revision",
            operation_id=operation,
            request_hash="a" * 64,
            payload={"source_ref": {"id": "different"}},
        )
    assert conflict.value.detail.code == "idempotency_conflict"

    member = await repo.append_change(
        "local_owner",
        change_key="member:1:7:company_research",
        kind="scope_member",
        parent_sequence=first.sequence,
        listing_id=7,
        context="company_research",
        payload={"manifest_hash": "b" * 64},
    )
    with pytest.raises(InvestorError):
        await repo.append_change(
            "local_owner",
            change_key="member:retry",
            kind="scope_member",
            parent_sequence=first.sequence,
            listing_id=7,
            context="company_research",
            payload={"manifest_hash": "b" * 64},
        )
    assert (await repo.get_change("other_owner", first.id)) is None
    assert [
        row.sequence
        for row in await repo.list_changes(
            "local_owner",
            parent_sequence=first.sequence,
        )
    ] == [member.sequence]


@pytest.mark.asyncio
async def test_source_and_refresh_states_use_revision_cas(db_session):
    repo = SupplementalUpdatesRepository(db_session)
    config_hash = canonical_hash({"adapter": "local-v1"})
    source = await repo.ensure_source(
        "local_owner",
        "controlled_local_v1",
        "listing:7:company_research",
        config_hash,
        listing_id=7,
        context="company_research",
    )
    assert source.coverage_state == "unknown"
    assert await repo.list_sources("local_owner", 7, "company_research") == [source]
    updated_source = source.model_copy(
        update={
            "revision": 1,
            "coverage_state": "complete",
            "success_sequence": 4,
            "success_receipt_hash": "c" * 64,
        }
    )
    await repo.save_source(updated_source, expected_revision=0)
    with pytest.raises(InvestorError) as conflict:
        await repo.save_source(updated_source, expected_revision=0)
    assert conflict.value.detail.code == "revision_conflict"
    assert (
        await repo.get_source("local_owner", source.feed_id, source.scope_key)
    ).success_sequence == 4

    refresh = await repo.ensure_refresh("local_owner", 7, "company_research")
    next_state = refresh.model_copy(
        update={
            "revision": 1,
            "requested_sequence": 3,
            "confirmed_sequence": 2,
        }
    )
    await repo.save_refresh(next_state, expected_revision=0)
    with pytest.raises(InvestorError):
        await repo.save_refresh(next_state, expected_revision=0)


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", ["payload_hash", "schema_version", "origin_run_id"])
async def test_receipt_read_rejects_damaged_immutable_header_or_hash(db_session, tamper):
    repo = SupplementalUpdatesRepository(db_session)
    receipt = await repo.append_change(
        "local_owner", change_key=f"corrupt:{tamper}", kind="bootstrap",
        payload={"source": "fixture"},
    )
    # Simulate on-disk corruption below the normal immutable trigger boundary.
    await db_session.execute(text("DROP TRIGGER trg_supplemental_change_immutable_update"))
    await db_session.execute(text("PRAGMA ignore_check_constraints=ON"))
    if tamper == "payload_hash":
        await db_session.execute(
            text("UPDATE supplemental_change_receipt SET payload_hash=:value WHERE sequence=:seq"),
            {"value": "0" * 64, "seq": receipt.sequence},
        )
    elif tamper == "schema_version":
        await db_session.execute(
            text("UPDATE supplemental_change_receipt SET schema_version=2 WHERE sequence=:seq"),
            {"seq": receipt.sequence},
        )
    else:
        await db_session.execute(
            text("UPDATE supplemental_change_receipt SET origin_run_id=0 WHERE sequence=:seq"),
            {"seq": receipt.sequence},
        )
    with pytest.raises(InvestorError) as exc_info:
        await repo.get_change("local_owner", receipt.sequence)
    assert exc_info.value.detail.code == "invalid_reference"


@pytest.mark.asyncio
async def test_latest_member_outcome_lookup_is_unbounded_and_uses_its_index(db_session):
    repo = SupplementalUpdatesRepository(db_session)
    root = await repo.append_change(
        "local_owner", change_key="outcome-root", kind="bootstrap", payload={},
    )
    member = await repo.append_change(
        "local_owner", change_key="outcome-member", kind="scope_member",
        parent_sequence=root.sequence, listing_id=7, context="company_research",
        payload={"source_sequence": root.sequence},
    )
    for attempt in range(501):
        await repo.append_change(
            "local_owner", change_key=f"outcome:{attempt}", kind="scope_outcome",
            parent_sequence=root.sequence, listing_id=7, context="company_research",
            payload={"member_sequence": member.sequence, "attempt": attempt},
        )
    latest = await repo.get_latest_member_outcome("local_owner", member.sequence)
    assert latest is not None
    assert latest.payload["attempt"] == 500
    plan = (await db_session.execute(text(
        "EXPLAIN QUERY PLAN SELECT sequence FROM supplemental_change_receipt "
        "WHERE owner_id='local_owner' AND kind='scope_outcome' "
        "AND json_extract(payload, '$.member_sequence')=:sequence ORDER BY sequence DESC LIMIT 1"
    ), {"sequence": member.sequence})).all()
    assert any("ix_supplemental_change_owner_member_outcome_latest" in str(row) for row in plan)


@pytest.mark.asyncio
async def test_local_evidence_proof_scope_lookup_uses_partial_index(db_session):
    repo = SupplementalUpdatesRepository(db_session)
    await repo.append_change(
        "local_owner", change_key="evidence-proof-index", kind="evidence_revision",
        listing_id=7, context="account_fit", payload={"source_refs_hash": "a" * 64},
    )
    plan = (await db_session.execute(text(
        "EXPLAIN QUERY PLAN SELECT sequence FROM supplemental_change_receipt "
        "WHERE owner_id='local_owner' AND listing_id=7 AND context='account_fit' "
        "AND kind='evidence_revision' AND committed_at <= '2026-10-17' "
        "ORDER BY committed_at DESC, sequence DESC LIMIT 1"
    ))).all()
    assert any("ix_supplemental_change_evidence_proof" in str(row) for row in plan)


@pytest.mark.asyncio
async def test_two_sqlite_connections_serialize_duplicate_receipt_and_cas(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'updates.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def write_receipt():
        async with sessions() as session, session.begin():
            return await SupplementalUpdatesRepository(session).append_change(
                "local_owner",
                change_key="concurrent:one",
                kind="bootstrap",
                payload={"generation": 2},
            )

    left, right = await asyncio.gather(write_receipt(), write_receipt())
    assert left.id == right.id
    async with sessions() as session, session.begin():
        repo = SupplementalUpdatesRepository(session)
        await repo.ensure_refresh("local_owner", 9, "company_research")

    ready = 0
    both_read = asyncio.Event()

    async def cas_update(confirmed: int):
        nonlocal ready
        async with sessions() as session, session.begin():
            repo = SupplementalUpdatesRepository(session)
            current = await repo.get_refresh("local_owner", 9, "company_research")
            assert current is not None
            ready += 1
            if ready == 2:
                both_read.set()
            await both_read.wait()
            candidate = current.model_copy(
                update={
                    "revision": current.revision + 1,
                    "confirmed_sequence": confirmed,
                    "requested_sequence": max(current.requested_sequence, confirmed),
                }
            )
            await repo.save_refresh(candidate, expected_revision=current.revision)

    outcomes = await asyncio.gather(cas_update(1), cas_update(2), return_exceptions=True)
    assert sum(isinstance(item, InvestorError) for item in outcomes) == 1
    assert sum(item is None for item in outcomes) == 1
    await engine.dispose()
