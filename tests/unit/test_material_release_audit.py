"""Offline release-audit regression tests using a complete reviewed package."""

from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from tools.hk208_release_check import _report_target

from investment_agent.domain.security import DEFAULT_PILOT_LIST_PATH, load_pilot_identities
from investment_agent.materials.bootstrap import initialize_pilot
from investment_agent.materials.publication_prepare import prepare_material_publication
from investment_agent.materials.release_audit import audit_release
from investment_agent.materials.reviewed_import import import_reviewed_annual
from investment_agent.storage.database import engine_lifespan, session_scope
from investment_agent.storage.models import Base
from tests.unit.test_annual_validation import _make_package


async def _valid_package(tmp_path: Path) -> tuple[Path, Path]:
    database = tmp_path / "app.db"
    data_dir = tmp_path / "app-data"
    data_dir.mkdir(parents=True)
    manifest, source_root, _ = _make_package(tmp_path)
    identities = load_pilot_identities(DEFAULT_PILOT_LIST_PATH)
    async with engine_lifespan(f"sqlite+aiosqlite:///{database}") as engine:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        with sqlite3.connect(database) as connection:
            connection.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
            connection.execute("INSERT INTO alembic_version VALUES ('051')")
        async with session_scope() as session:
            setup = await initialize_pilot(session, identities)
            report = await import_reviewed_annual(
                session,
                manifest_path=manifest,
                source_root=source_root,
                data_dir=data_dir,
                identities=identities,
                company_ids=setup.company_ids,
                imported_at=datetime(2026, 10, 16, 12, tzinfo=UTC),
            )
            assert len(report.saved) == 8
            for identity in identities:
                listing = setup.listings[(identity.market_code, identity.ticker)]
                prepared = await prepare_material_publication(
                    session,
                    listing_id=listing.id,
                    as_of=datetime(2026, 10, 17, 12, tzinfo=UTC),
                )
                assert any(item.calculation_ref for item in prepared.metrics)
            await session.commit()
    return database, data_dir


def _copy_package(tmp_path: Path, source: tuple[Path, Path]) -> tuple[Path, Path]:
    database, data_dir = source
    target_db = tmp_path / "copy.db"
    target_data = tmp_path / "copy-data"
    tmp_path.mkdir(parents=True, exist_ok=True)
    shutil.copy2(database, target_db)
    shutil.copytree(data_dir, target_data)
    with sqlite3.connect(target_db) as connection:
        triggers = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='research_dataset'"
        ).fetchall()
        for (name,) in triggers:
            connection.execute(f'DROP TRIGGER "{name}"')
    return target_db, target_data


@pytest.mark.asyncio
async def test_valid_package_passes_m1_and_keeps_missing_values_explicit(tmp_path):
    database, data_dir = await _valid_package(tmp_path)

    report = audit_release(database, data_dir, require_m1=True)

    assert report["ok"], report["issues"]
    assert report["materials"]["reviewed_dataset_count"] == 8
    assert report["materials"]["valid_review_count"] == 8
    assert all(item["covered"] for item in report["materials"]["identity_coverage"])
    assert report["materials"]["valid_calculation_count"] >= 8
    assert report["source_switches"] == {
        "online_refresh_enabled": False,
        "production_readiness": False,
    }
    assert isinstance(report["materials"]["missing_values"], list)


@pytest.mark.asyncio
async def test_deleted_or_changed_reviewed_original_fails(tmp_path):
    source = await _valid_package(tmp_path / "valid")
    for changed in ("deleted", "changed"):
        database, data_dir = _copy_package(tmp_path / changed, source)
        with sqlite3.connect(database) as connection:
            payload = json.loads(connection.execute(
                "SELECT payload FROM research_dataset WHERE source='issuer.reviewed_annual.v1' LIMIT 1"
            ).fetchone()[0])
        original = data_dir / payload["review"]["documents"][0]["raw_ref"]
        if changed == "deleted":
            original.unlink()
        else:
            original.write_bytes(original.read_bytes() + b"tampered")
        report = audit_release(database, data_dir, require_m1=True)
        assert not report["ok"]
        assert any("original" in issue.lower() for issue in report["issues"])


@pytest.mark.asyncio
async def test_wrong_identity_migration_and_calculation_reference_fail(tmp_path):
    source = await _valid_package(tmp_path / "valid")
    for mutation in ("identity", "migration", "calculation", "input_hash", "evidence_locator"):
        database, data_dir = _copy_package(tmp_path / mutation, source)
        with sqlite3.connect(database) as connection:
            if mutation == "identity":
                row_id, raw = connection.execute(
                    "SELECT id, payload FROM research_dataset WHERE source='issuer.reviewed_annual.v1' LIMIT 1"
                ).fetchone()
                payload = json.loads(raw)
                payload["review"]["company_key"] = "wrong-company"
                connection.execute("UPDATE research_dataset SET payload=? WHERE id=?", (json.dumps(payload), row_id))
            elif mutation == "migration":
                connection.execute("UPDATE alembic_version SET version_num='049'")
            elif mutation == "calculation":
                row_id, raw = connection.execute("SELECT id, payload FROM investor_calculation LIMIT 1").fetchone()
                payload = json.loads(raw)
                payload["fact_refs"][0]["id"] = "00000000-0000-4000-8000-000000000000"
                connection.execute("UPDATE investor_calculation SET payload=? WHERE id=?", (json.dumps(payload), row_id))
            else:
                if mutation == "input_hash":
                    row_id, raw = connection.execute("SELECT id, payload FROM investor_calculation LIMIT 1").fetchone()
                    payload = json.loads(raw)
                    payload["input_hash"] = "0" * 64
                    connection.execute("UPDATE investor_calculation SET payload=? WHERE id=?", (json.dumps(payload), row_id))
                else:
                    row_id, raw = connection.execute("SELECT id, payload FROM investor_evidence LIMIT 1").fetchone()
                    payload = json.loads(raw)
                    payload["locator"] += ":tampered"
                    connection.execute("UPDATE investor_evidence SET payload=? WHERE id=?", (json.dumps(payload), row_id))
        report = audit_release(database, data_dir, require_m1=True)
        assert not report["ok"]
        if mutation == "identity":
            assert any("identity" in issue.lower() or "receipt" in issue.lower() for issue in report["issues"])
        elif mutation == "migration":
            assert any("migration" in issue.lower() for issue in report["issues"])
        elif mutation == "calculation":
            assert any("fact_refs" in issue or "reference" in issue.lower() for issue in report["issues"])
        elif mutation == "input_hash":
            assert any("input_hash" in issue for issue in report["issues"])
        else:
            assert any("saved evidence" in issue and "locator" in issue for issue in report["issues"])


@pytest.mark.asyncio
async def test_missing_and_partial_material_coverage_fail_m1(tmp_path):
    source = await _valid_package(tmp_path / "valid")
    database, data_dir = _copy_package(tmp_path / "empty", source)
    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM research_dataset")
        connection.execute("DELETE FROM investor_calculation")
        connection.execute("DELETE FROM investor_evidence")
    report = audit_release(database, data_dir, require_m1=True)
    assert not report["ok"]
    assert report["materials"]["dataset_count"] == 0

    database, data_dir = _copy_package(tmp_path / "partial", source)
    with sqlite3.connect(database) as connection:
        keep = connection.execute(
            "SELECT id FROM research_dataset WHERE source='issuer.reviewed_annual.v1' ORDER BY id LIMIT 1"
        ).fetchone()[0]
        connection.execute(
            "DELETE FROM research_dataset WHERE source='issuer.reviewed_annual.v1' AND id != ?", (keep,)
        )
        # Force at least one controlled company to lack a saved calculation.
        connection.execute("DELETE FROM investor_calculation")
    report = audit_release(database, data_dir, require_m1=True)
    assert not report["ok"]
    assert any("M1 requires" in issue for issue in report["issues"])


def test_missing_database_is_not_created(tmp_path):
    missing = tmp_path / "missing.db"
    report = audit_release(missing, tmp_path / "data")
    assert not report["ok"]
    assert not missing.exists()
    assert any("no database was created" in issue for issue in report["issues"])


def test_report_cannot_replace_database_sidecars_or_hardlinks(tmp_path):
    database = tmp_path / "app.db"
    database.write_bytes(b"database")
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    with pytest.raises(ValueError, match="database"):
        _report_target(database, database, data_dir)
    sidecar = Path(str(database) + "-wal")
    with pytest.raises(ValueError, match="sidecar"):
        _report_target(sidecar, database, data_dir)
    hardlink = tmp_path / "report.json"
    hardlink.hardlink_to(database)
    with pytest.raises(ValueError, match="sidecar"):
        _report_target(hardlink, database, data_dir)
