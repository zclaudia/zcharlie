"""Portable offline replay of real HK statements and SEC submission shards."""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from investment_agent.cli.main import app
from investment_agent.domain.security import DEFAULT_PILOT_LIST_PATH, load_pilot_identities
from investment_agent.materials.bootstrap import initialize_pilot
from investment_agent.materials.context import financial_context
from investment_agent.materials.handlers import default_adapters
from investment_agent.materials.offline_import import import_offline_evidence
from investment_agent.materials.replay import replay_acquisition
from investment_agent.storage.database import engine_lifespan, session_scope
from investment_agent.storage.models import Base

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "materials"


def _invoke_replay(acquisition_id: int):
    return CliRunner().invoke(
        app, ["materials", "replay", "--acquisition-id", str(acquisition_id)]
    )


def _view_signature(view):
    """Compare the public fact and calculated-metric surfaces after relocation."""

    return (
        tuple((item.metric, item.period_end, item.amount, item.unit, item.currency,
               item.verification, item.conflict, item.locator) for item in view.facts),
        tuple((item.key, item.status, item.value, item.unit, item.currency,
               item.basis, item.periods, item.notes) for item in view.metrics),
    )


def _copy_sqlite(source: Path, target: Path) -> None:
    with sqlite3.connect(source) as source_db, sqlite3.connect(target) as target_db:
        source_db.backup(target_db)


async def _import_real_sources(db_path: Path, data_dir: Path, evidence: Path):
    identities = tuple(
        item for item in load_pilot_identities(DEFAULT_PILOT_LIST_PATH)
        if item.company_key in {"hsbc-holdings", "jpmorgan-chase"}
    )
    async with engine_lifespan(f"sqlite+aiosqlite:///{db_path}") as engine:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with session_scope() as session:
            setup = await initialize_pilot(session, identities)
            report = await import_offline_evidence(
                session,
                data_dir=data_dir,
                source_dir=evidence,
                identities=identities,
                company_ids=setup.company_ids,
                adapters=default_adapters(),
                imported_at=datetime(2026, 10, 15, tzinfo=UTC),
            )
            snapshots = {}
            for company_key in ("hsbc-holdings", "jpmorgan-chase"):
                identity = next(item for item in identities if item.company_key == company_key)
                listing = setup.listings[(identity.market_code, identity.ticker)]
                snapshots[company_key] = _view_signature(
                    await financial_context(session, listing.id)
                )
            return identities, report.acquisition_ids, snapshots


@pytest.mark.asyncio
async def test_real_hk_and_sec_acquisitions_replay_after_relocation_without_inputs_or_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def forbid_network(*_args, **_kwargs):
        raise AssertionError("archive replay must never access the network")

    monkeypatch.setattr(httpx.AsyncClient, "get", forbid_network)
    evidence = tmp_path / "input-evidence"
    shutil.copytree(FIXTURES / "bootstrap", evidence)
    shutil.copytree(FIXTURES / "us" / "JPM", evidence / "JPM")
    for path in (evidence / "JPM").rglob("*.json"):
        body = path.read_bytes()
        path.with_name(path.name + ".meta.json").write_text(json.dumps({
            "status": 200,
            "bytes": len(body),
            "sha256": hashlib.sha256(body).hexdigest(),
            "fetched_at_utc": "2026-10-12T00:00:00+00:00",
        }), encoding="utf-8")

    original_db = tmp_path / "original" / "research.db"
    original_db.parent.mkdir()
    original_data = tmp_path / "original" / "data"
    identities, acquisition_ids, before = await _import_real_sources(
        original_db, original_data, evidence
    )
    assert len(acquisition_ids) == 3  # HSBC statements + its empty SEC attempt + JPM SEC.
    assert before["hsbc-holdings"][0]
    assert before["jpmorgan-chase"][0]

    relocated = tmp_path / "relocated"
    relocated.mkdir()
    copied_db = relocated / "research.db"
    _copy_sqlite(original_db, copied_db)
    copied_data = relocated / "data"
    shutil.copytree(original_data, copied_data)
    shutil.rmtree(evidence)

    async with engine_lifespan(f"sqlite+aiosqlite:///{copied_db}"), session_scope() as session:
        for acquisition_id in acquisition_ids:
            await replay_acquisition(
                session,
                acquisition_id=acquisition_id,
                data_dir=copied_data,
                identities=identities,
                adapters=default_adapters(),
            )
        from sqlalchemy import select

        from investment_agent.storage.models.reference import Listing

        for identity in identities:
            if identity.company_key not in before:
                continue
            listing = await session.scalar(select(Listing).where(
                Listing.market_code == identity.market_code,
                Listing.ticker == identity.ticker,
            ))
            assert listing is not None
            after = _view_signature(await financial_context(session, listing.id))
            assert after == before[identity.company_key]

    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{copied_db}")
    monkeypatch.setenv("DATA_DIR", str(copied_data))
    monkeypatch.setenv("API_KEY", "secret")
    cli = await asyncio.to_thread(_invoke_replay, acquisition_ids[-1])
    assert cli.exit_code == 0, cli.output
    assert json.loads(cli.output)["acquisition_id"] == acquisition_ids[-1]


@pytest.mark.parametrize("field", ["sha256", "bytes"])
@pytest.mark.asyncio
async def test_replay_never_parses_sidecar_rejected_body(
    tmp_path: Path, field: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence = tmp_path / "evidence"
    shutil.copytree(FIXTURES / "bootstrap", evidence)
    target = evidence / "00005" / "income-four-years.json"
    sidecar = target.with_name(target.name + ".meta.json")
    body = target.read_bytes()
    metadata = {
        "status": 200,
        "bytes": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
        "fetched_at_utc": "2026-10-12T00:00:00+00:00",
    }
    metadata[field] = "0" * 64 if field == "sha256" else len(body) + 1
    sidecar.write_text(json.dumps(metadata), encoding="utf-8")

    identities = load_pilot_identities(DEFAULT_PILOT_LIST_PATH)
    identity = next(item for item in identities if item.company_key == "hsbc-holdings")
    db_path = tmp_path / "research.db"
    data_dir = tmp_path / "data"
    async with engine_lifespan(f"sqlite+aiosqlite:///{db_path}") as engine:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with session_scope() as session:
            setup = await initialize_pilot(session, identities)
            report = await import_offline_evidence(
                session,
                data_dir=data_dir,
                source_dir=evidence,
                identities=(identity,),
                company_ids=setup.company_ids,
                adapters={identity.sources[0].source: default_adapters()[identity.sources[0].source]},
                imported_at=datetime(2026, 10, 15, tzinfo=UTC),
            )
            assert len(report.acquisition_ids) == 1
            assert any("response" in problem for problem in report.problems)
            from investment_agent.materials.archive import verify_capture
            from investment_agent.storage.models.material_acquisition import (
                ResearchMaterialAcquisition,
            )

            acquisition = await session.get(ResearchMaterialAcquisition, report.acquisition_ids[0])
            assert acquisition is not None
            archived = verify_capture(
                data_dir, acquisition.archive_ref, expected_sha256=acquisition.manifest_sha256
            )
            rejected = next(item for item in archived.responses if item.name == "income")
            assert rejected.content == body
            assert rejected.parse_eligible is False

            replay = await replay_acquisition(
                session,
                acquisition_id=acquisition.id,
                data_dir=data_dir,
                identities=identities,
                adapters=default_adapters(),
            )
            assert replay.problems
            assert tuple((item.id, item.revision, item.content_hash) for item in replay.saved) == tuple(
                (item.id, item.revision, item.content_hash) for item in report.saved
            )
