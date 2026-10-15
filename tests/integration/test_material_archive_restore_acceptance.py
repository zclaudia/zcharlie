"""Independent acceptance: real originals survive the production backup tool."""

import asyncio
import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import httpx
from sqlalchemy import func, select

from investment_agent.domain.security import DEFAULT_PILOT_LIST_PATH, load_pilot_identities
from investment_agent.materials.bootstrap import initialize_pilot
from investment_agent.materials.context import financial_context
from investment_agent.materials.handlers import default_adapters
from investment_agent.materials.offline_import import import_offline_evidence
from investment_agent.materials.replay import replay_acquisition
from investment_agent.storage.database import engine_lifespan, session_scope
from investment_agent.storage.models import Base
from investment_agent.storage.models.material_acquisition import ResearchMaterialAcquisition
from investment_agent.storage.models.research_material import ResearchDataset

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests/fixtures/materials"


async def test_real_hk_sec_backup_restore_replays_without_original_directory(
    tmp_path: Path, monkeypatch
) -> None:
    def no_http(*args, **kwargs):
        raise AssertionError("offline archive acceptance attempted HTTP")

    monkeypatch.setattr(httpx.AsyncClient, "send", no_http)
    monkeypatch.setattr(httpx.Client, "send", no_http)
    identities = tuple(
        identity for identity in load_pilot_identities(DEFAULT_PILOT_LIST_PATH)
        if identity.company_key in {"hsbc-holdings", "jpmorgan-chase"}
    )
    evidence = tmp_path / "evidence"
    shutil.copytree(FIXTURES / "bootstrap/00005", evidence / "00005")
    shutil.copytree(FIXTURES / "us/JPM", evidence / "JPM")
    for path in (evidence / "JPM").rglob("*.json"):
        if path.name.endswith(".meta.json"):
            continue
        content = path.read_bytes()
        path.with_name(path.name + ".meta.json").write_text(json.dumps({
            "url": "https://data.sec.gov/acceptance-fixture",
            "fetched_at_utc": "2026-10-11T15:00:00+00:00",
            "status": 200, "bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }))
    data = tmp_path / "data"
    data.mkdir()
    acquisition_ids = []
    before = {}
    async with engine_lifespan(f"sqlite+aiosqlite:///{data / 'app.db'}") as engine:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with session_scope() as session:
            setup = await initialize_pilot(session, identities)
            for identity in identities:
                source = (
                    "eastmoney.hk.native.v1" if identity.market_code == "HKEX"
                    else "sec.companyfacts.v1"
                )
                report = await import_offline_evidence(
                    session, data_dir=data, source_dir=evidence, identities=(identity,),
                    company_ids=setup.company_ids,
                    adapters={source: default_adapters()[source]},
                )
                assert not report.problems and not report.skipped, report
                acquisition_ids.extend(report.acquisition_ids)
                listing = setup.listings[(identity.market_code, identity.ticker)]
                view = await financial_context(session, listing.id)
                assert view.facts and view.metrics
                before[listing.id] = (view.facts, view.metrics, view.fiscal_years, view.coverage)
            dataset_count = await session.scalar(select(func.count()).select_from(ResearchDataset))
            await session.commit()

    backup = tmp_path / "backup"
    command = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, str(REPO / "tools/backup_research.py"),
         "--data-dir", str(data), "--output", str(backup)],
        capture_output=True, text=True, check=False,
    )
    assert command.returncode == 0, command.stderr
    restored = tmp_path / "relocated"
    shutil.copytree(backup, restored)
    shutil.rmtree(evidence)
    shutil.rmtree(data)
    shutil.rmtree(backup)
    verification = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, str(REPO / "tools/verify_backup.py"), "--backup-dir", str(restored)],
        capture_output=True, text=True, check=False,
    )
    assert verification.returncode == 0, verification.stderr
    with sqlite3.connect(restored / "app.db") as connection:
        assert connection.execute("SELECT count(*) FROM research_material_acquisition").fetchone() == (2,)

    async with (
        engine_lifespan(f"sqlite+aiosqlite:///{restored / 'app.db'}"),
        session_scope() as session,
    ):
        for acquisition_id in acquisition_ids:
            report = await replay_acquisition(
                session, acquisition_id=acquisition_id, data_dir=restored,
                identities=identities, adapters=default_adapters(),
            )
            assert not report.problems and not report.skipped, report
            assert all(not saved.created for saved in report.saved)
        assert await session.scalar(select(func.count()).select_from(ResearchDataset)) == dataset_count
        assert await session.scalar(select(func.count()).select_from(ResearchMaterialAcquisition)) == 2
        for listing_id, expected in before.items():
            view = await financial_context(session, listing_id)
            assert (view.facts, view.metrics, view.fiscal_years, view.coverage) == expected
        await session.commit()
