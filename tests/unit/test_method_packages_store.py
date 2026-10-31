"""Method package storage (card 03): package artifacts, default head, migration 056.

Covers the storage half of design §5: ``save_package`` dedup and per-key
versioning, package lookup helpers, explicit global activation (idempotent
replay, optimistic concurrency, per-listing heads untouched) and the
per-listing -> default -> legacy v1 resolution order. The v1 binding payload
shape is pinned byte-for-byte because v2 only adds the package fields.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from pydantic import ValidationError
from sqlalchemy import create_engine, select

from investment_agent.investor.common import ObjectRef
from investment_agent.investor.foundations import InvestorError
from investment_agent.investor.method_packages import (
    LEGACY_BASELINE_DEFINITION,
    MethodPackageError,
    canonical_hash,
)
from investment_agent.investor.methods import (
    ActivateGlobal,
    MethodBinding,
    MethodConfig,
    MethodService,
)
from investment_agent.investor.publication import FIXED_METHOD
from investment_agent.storage.database import engine_lifespan, session_scope
from investment_agent.storage.models import Base
from investment_agent.storage.models.investor import MethodArtifact, MethodHead
from investment_agent.storage.models.reference import Company, Listing, Market
from tests.integration.test_migration_046 import _config

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest_asyncio.fixture
async def store_db(tmp_path):
    async with engine_lifespan(f"sqlite+aiosqlite:///{tmp_path / 'method-store.db'}") as engine:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        yield


async def _subject() -> ObjectRef:
    async with session_scope() as session:
        session.add(Market(code="FX", name="Fixture market", timezone="UTC", currency="CNY"))
        company = Company(canonical_name="Method package fixture")
        session.add(company)
        await session.flush()
        listing = Listing(company_id=company.id, market_code="FX", ticker="600001.FX", is_primary=True)
        session.add(listing)
        await session.flush()
        return ObjectRef(kind="listing", id=listing.id, revision=1)


def _definition(**overrides):
    return LEGACY_BASELINE_DEFINITION.model_copy(update=overrides)


def _package_ref(saved: dict) -> ObjectRef:
    return ObjectRef(kind="package", id=UUID(saved["artifact_id"]), revision=saved["version"])


def _activate(saved: dict, *, expected_revision: int = 0, **overrides) -> ActivateGlobal:
    values: dict = {"mutation_id": uuid4(), "package_ref": _package_ref(saved), "expected_revision": expected_revision, "reason": "Enable the reviewed baseline package"}
    values.update(overrides)
    return ActivateGlobal(**values)


# --------------------------------------------------------------------------- #
# MethodBinding v1/v2 payload shapes (design §4.3)
# --------------------------------------------------------------------------- #


def test_v1_binding_payload_stays_byte_identical():
    assert MethodBinding().model_dump(mode="json") == {
        "schema_version": 1,
        "method_ref": {"kind": "method", "id": str(FIXED_METHOD.id), "revision": 1, "field_path": None},
        "config": {
            "schema_version": 1,
            "require_two_counter_hypotheses": False,
            "require_verified_counter_evidence": False,
            "require_explicit_stop_decision": False,
        },
        "listing_ref": None,
    }
    scoped = MethodBinding(listing_ref=ObjectRef(kind="listing", id=7, revision=1))
    assert set(scoped.model_dump(mode="json")) == {"schema_version", "method_ref", "config", "listing_ref"}
    with pytest.raises(ValidationError):
        MethodBinding.model_validate({**MethodBinding().model_dump(mode="json"), "package_hash": "a" * 64})


def test_v2_binding_requires_package_fields_and_round_trips():
    binding = MethodBinding(
        schema_version=2,
        method_ref=ObjectRef(kind="method", id=uuid4(), revision=1),
        package_ref=ObjectRef(kind="package", id=uuid4(), revision=1),
        package_hash="a" * 64,
    )
    data = binding.model_dump(mode="json")
    assert data["package_ref"]["kind"] == "package" and data["package_hash"] == "a" * 64
    assert MethodBinding.model_validate(data) == binding
    with pytest.raises(ValidationError):
        MethodBinding.model_validate({**data, "package_hash": None})
    with pytest.raises(ValidationError):
        MethodBinding(schema_version=1, package_ref=binding.package_ref, package_hash=binding.package_hash)


# --------------------------------------------------------------------------- #
# save_package: dedup, per-key versions, aggregate validation
# --------------------------------------------------------------------------- #


async def test_save_package_dedupes_identical_content(store_db):
    async with session_scope() as session:
        svc = MethodService(session)
        saved = await svc.save_package(LEGACY_BASELINE_DEFINITION, source="sync")
        assert saved["version"] == 1
        assert saved["package_key"] == "company-research"
        assert saved["content_hash"] == canonical_hash(LEGACY_BASELINE_DEFINITION)
        assert await svc.save_package(LEGACY_BASELINE_DEFINITION, source="sync-again") == saved
        rows = (await session.scalars(select(MethodArtifact).where(MethodArtifact.kind == "package"))).all()
        assert len(rows) == 1 and rows[0].id == saved["artifact_id"]


async def test_save_package_versions_are_per_key_and_monotonic(store_db):
    async with session_scope() as session:
        svc = MethodService(session)
        first = await svc.save_package(LEGACY_BASELINE_DEFINITION, source="sync")
        changed = await svc.save_package(_definition(description="已审阅的新说明"), source="sync")
        assert changed["version"] == 2 and changed["content_hash"] != first["content_hash"]
        assert await svc.save_package(_definition(description="已审阅的新说明"), source="sync") == changed
        other = await svc.save_package(_definition(package_key="company-review", applicable_kinds=("review",)), source="sync")
        assert other["version"] == 1
        third = await svc.save_package(_definition(description="第三次内容"), source="sync")
        assert third["version"] == 3 and third["artifact_id"] not in {first["artifact_id"], changed["artifact_id"]}


async def test_save_package_aggregates_invalid_definitions(store_db):
    async with session_scope() as session:
        svc = MethodService(session)
        with pytest.raises(MethodPackageError) as info:
            await svc.save_package(_definition(applicable_kinds=()), source="broken.yaml")
        assert any("applicable_kinds" in message for message in info.value.errors)
        assert info.value.source == "broken.yaml"
        rows = (await session.scalars(select(MethodArtifact).where(MethodArtifact.kind == "package"))).all()
        assert rows == []


async def test_package_and_package_by_ref_lookup(store_db):
    async with session_scope() as session:
        svc = MethodService(session)
        saved = await svc.save_package(LEGACY_BASELINE_DEFINITION, source="sync")
        assert await svc.package("company-research", 1) == saved
        assert await svc.package_by_ref(_package_ref(saved)) == saved
        with pytest.raises(InvestorError, match="不可用"):
            await svc.package("company-research", 9)
        with pytest.raises(InvestorError, match="不可用"):
            await svc.package_by_ref(ObjectRef(kind="method", id=UUID(saved["artifact_id"]), revision=1))
        with pytest.raises(InvestorError, match="不可用"):
            await svc.package_by_ref(ObjectRef(kind="package", id=uuid4(), revision=1))


# --------------------------------------------------------------------------- #
# activate_global: replay, optimistic concurrency, per-listing isolation
# --------------------------------------------------------------------------- #


async def test_activate_global_appends_version_and_event_and_moves_default_head(store_db):
    async with session_scope() as session:
        saved = await MethodService(session).save_package(LEGACY_BASELINE_DEFINITION, source="sync")
        request = _activate(saved)
        result = await MethodService(session).activate_global(request, actor="authenticated_user")
        assert result["head_revision"] == 1 and result["channel"] == "company-research"
        assert result["binding"]["schema_version"] == 2
        svc = MethodService(session)
        revision, binding = await svc.default_head()
        assert revision == 1
        assert binding is not None
        assert binding.schema_version == 2
        assert binding.package_ref == _package_ref(saved)
        assert binding.package_hash == saved["content_hash"]
        assert binding.config == LEGACY_BASELINE_DEFINITION.discipline
        assert binding.listing_ref is None and binding.method_ref.kind == "method"
        versions = (await session.scalars(select(MethodArtifact).where(MethodArtifact.kind == "version"))).all()
        assert len(versions) == 1
        assert versions[0].id == str(binding.method_ref.id)
        assert versions[0].payload["binding"] == binding.model_dump(mode="json")
        events = (await session.scalars(select(MethodArtifact).where(MethodArtifact.kind == "event"))).all()
        assert len(events) == 1
        assert events[0].payload["operation"] == "activate_global"
        assert events[0].payload["request"] == request.model_dump(mode="json")
        assert events[0].payload["package_hash"] == saved["content_hash"]
        # Idempotent replay returns the same receipt and moves nothing.
        assert await svc.activate_global(request, actor="authenticated_user") == result
        assert (await svc.default_head())[0] == 1
        assert len((await session.scalars(select(MethodArtifact).where(MethodArtifact.kind == "event"))).all()) == 1


async def test_activate_global_second_activation_moves_revision_and_stale_is_rejected(store_db):
    async with session_scope() as session:
        svc = MethodService(session)
        saved = await svc.save_package(LEGACY_BASELINE_DEFINITION, source="sync")
        await svc.activate_global(_activate(saved), actor="authenticated_user")
        changed = await svc.save_package(_definition(description="加强后的内容"), source="sync")
        second = await svc.activate_global(_activate(changed, expected_revision=1), actor="authenticated_user")
        assert second["head_revision"] == 2
        assert (await svc.default_head())[1].package_hash == changed["content_hash"]
        with pytest.raises(InvestorError, match="全局默认"):
            await svc.activate_global(_activate(saved, expected_revision=0), actor="authenticated_user")
        assert (await svc.default_head())[0] == 2
        versions = (await session.scalars(select(MethodArtifact).where(MethodArtifact.kind == "version"))).all()
        assert len(versions) == 2  # the stale attempt appended nothing


async def test_activate_global_requires_user_package_applicability_and_first_channel(store_db):
    async with session_scope() as session:
        svc = MethodService(session)
        saved = await svc.save_package(LEGACY_BASELINE_DEFINITION, source="sync")
        with pytest.raises(InvestorError, match="模型"):
            await svc.activate_global(_activate(saved), actor="model")
        assert await svc.default_head() == (0, None)
        with pytest.raises(InvestorError, match="通道"):
            await svc.activate_global(_activate(saved, channel="portfolio"), actor="authenticated_user")
        limited = await svc.save_package(_definition(applicable_kinds=("company_research",)), source="sync")
        with pytest.raises(InvestorError, match="review"):
            await svc.activate_global(_activate(limited), actor="authenticated_user")
        assert await svc.default_head() == (0, None)


async def test_activate_global_never_moves_per_listing_heads(store_db):
    subject = await _subject()
    override = MethodBinding(
        method_ref=ObjectRef(kind="method", id=uuid4(), revision=1),
        config=MethodConfig(require_two_counter_hypotheses=True),
        listing_ref=subject,
    )
    async with session_scope() as session:
        session.add(MethodHead(owner_id="local_owner", listing_id=int(subject.id), revision=1, payload=override.model_dump(mode="json")))
        svc = MethodService(session)
        saved = await svc.save_package(LEGACY_BASELINE_DEFINITION, source="sync")
        await svc.activate_global(_activate(saved), actor="authenticated_user")
        second = await svc.activate_global(_activate(saved, expected_revision=1, mutation_id=uuid4()), actor="authenticated_user")
        assert second["head_revision"] == 2
        head = await session.get(MethodHead, ("local_owner", int(subject.id)))
        assert head.revision == 1 and head.payload == override.model_dump(mode="json")


# --------------------------------------------------------------------------- #
# resolve_binding: per-listing -> default -> legacy v1 (design §5)
# --------------------------------------------------------------------------- #


async def test_resolve_binding_three_tier_order(store_db):
    subject = await _subject()
    async with session_scope() as session:
        svc = MethodService(session)
        assert await svc.resolve_binding(subject, "company_research") == MethodBinding()
        assert await svc.resolve_binding(None, "company_research") == MethodBinding()
        assert await svc.resolve_binding(subject, "comparison") == MethodBinding()
        saved = await svc.save_package(LEGACY_BASELINE_DEFINITION, source="sync")
        await svc.activate_global(_activate(saved), actor="authenticated_user")
        _, default = await svc.default_head()
        assert await svc.resolve_binding(subject, "company_research") == default
        assert await svc.resolve_binding(subject, "review") == default
        assert await svc.resolve_binding(subject, "explanation") == MethodBinding()
        override = MethodBinding(
            method_ref=ObjectRef(kind="method", id=uuid4(), revision=1),
            config=MethodConfig(require_explicit_stop_decision=True),
            listing_ref=subject,
        )
        session.add(MethodHead(owner_id="local_owner", listing_id=int(subject.id), revision=1, payload=override.model_dump(mode="json")))
        assert await svc.resolve_binding(subject, "company_research") == override
        assert await svc.resolve_binding(subject, "review") == override
        # None / non-company kinds stay legacy even with a default head present.
        assert await svc.resolve_binding(None, "company_research") == MethodBinding()


# --------------------------------------------------------------------------- #
# Migration 056 (empty-database upgrade/downgrade round trip)
# --------------------------------------------------------------------------- #


def test_revision_056_follows_055():
    script = ScriptDirectory(str(REPO_ROOT / "alembic"))
    assert script.get_revision("056").down_revision == "055"


def test_056_upgrade_creates_default_head_table_and_action_column(tmp_path, monkeypatch):
    database = tmp_path / "fresh.db"
    config = _config(database, monkeypatch)
    command.upgrade(config, "055")
    with sqlite3.connect(database) as connection:
        assert "investor_method_default_head" not in _tables(connection)
        assert "committed_sequence" not in _columns(connection, "agent_tool_action")
    command.upgrade(config, "056")
    engine = create_engine(f"sqlite:///{database}")
    try:
        with engine.connect() as connection:
            diffs = compare_metadata(MigrationContext.configure(connection), Base.metadata)
    finally:
        engine.dispose()
    assert not [diff for diff in diffs if "investor_method_default_head" in repr(diff) or "committed_sequence" in repr(diff)]
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("056",)
        assert "investor_method_default_head" in _tables(connection)
        info = {row[1]: row for row in connection.execute("PRAGMA table_info(investor_method_default_head)")}
        assert set(info) == {"owner_id", "channel", "revision", "payload"}
        assert {name for name, row in info.items() if row[5] > 0} == {"owner_id", "channel"}
        assert info["revision"][3] == 1 and info["payload"][3] == 1
        action = {row[1]: row for row in connection.execute("PRAGMA table_info(agent_tool_action)")}
        assert action["committed_sequence"][3] == 0
        # The composite primary key actually guards duplicate heads.
        connection.execute(
            "INSERT INTO investor_method_default_head (owner_id, channel, revision, payload) "
            "VALUES ('local_owner', 'company-research', 1, '{}')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO investor_method_default_head (owner_id, channel, revision, payload) "
                "VALUES ('local_owner', 'company-research', 2, '{}')"
            )
    command.downgrade(config, "055")
    with sqlite3.connect(database) as connection:
        assert "investor_method_default_head" not in _tables(connection)
        assert "committed_sequence" not in _columns(connection, "agent_tool_action")
    command.upgrade(config, "056")


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
