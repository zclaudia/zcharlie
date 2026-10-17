"""Offline release rehearsal against a real 048-to-head database upgrade."""

from __future__ import annotations

import json
import shutil
import sqlite3
import uuid
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import func, select

from investment_agent.storage.database import engine_lifespan, session_scope
from investment_agent.storage.models.peer import PeerAnnualSnapshot
from tests.integration.test_bank_verified_import import (
    AUDIT_PATH,
    BANK_EVIDENCE,
    ORIGINALS_ROOT,
    REPO_ROOT,
)

CURRENT_HEAD = ScriptDirectory(str(REPO_ROOT / "alembic")).get_current_head()

AUDITS = [
    REPO_ROOT / f"tests/fixtures/bank_fy2024/{name}.json"
    for name in ("icbc", "large", "regional", "scope")
]
REHEARSAL_ROOT = Path("/tmp/zcharlie-bank-release-2026-10-17/rehearsal")


@pytest.fixture
def migrated_release_root(monkeypatch):
    """Build from the actual predecessor schema in an isolated temp directory."""
    root = REHEARSAL_ROOT / uuid.uuid4().hex
    root.mkdir(parents=True)
    database_path = root / "bank-release.db"
    database_url = f"sqlite+aiosqlite:///{database_path}"
    for key, value in {
        "DATABASE_URL": database_url,
        "DATA_DIR": str(root),
        "API_KEY": "bank-release-rehearsal",
        "INDUSTRY_PEER_CONTEXT_ENABLED": "true",
        "INVESTOR_AGENT_ENABLED": "false",
        "INVESTOR_AGENT_BACKGROUND_ENABLED": "false",
        "BACKGROUND_ENABLED": "false",
    }.items():
        monkeypatch.setenv(key, value)

    config = Config()
    config.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "048")
    command.upgrade(config, "head")
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (CURRENT_HEAD,)
    yield root


@pytest_asyncio.fixture
async def release_world(migrated_release_root):
    database_url = f"sqlite+aiosqlite:///{migrated_release_root / 'bank-release.db'}"
    try:
        async with engine_lifespan(database_url):
            yield migrated_release_root
    finally:
        shutil.rmtree(migrated_release_root, ignore_errors=True)


def _missing_real_inputs() -> list[Path]:
    required = [BANK_EVIDENCE / "sample.json", AUDIT_PATH, ORIGINALS_ROOT / "source-manifest.json"]
    required.extend(
        BANK_EVIDENCE / "raw/financial" / f"{code}_main.json"
        for code in (
            "601398.SH",
            "601939.SH",
            "601288.SH",
            "600036.SH",
            "601166.SH",
            "000001.SZ",
            "600919.SH",
            "601009.SH",
            "002142.SZ",
            "601128.SH",
        )
    )
    if AUDIT_PATH.is_file():
        manifest = json.loads(AUDIT_PATH.read_text(encoding="utf-8"))
        for source in manifest.get("sources", {}).values():
            for key in ("pdf_path", "metadata_path"):
                if source.get(key):
                    required.append(REPO_ROOT / source[key])
            for item in source.get("supplemental_originals", {}).values():
                if item.get("pdf_path"):
                    required.append(REPO_ROOT / item["pdf_path"])
    bundle = None
    try:
        from investment_agent.industry_peers.bank_annual_matrix import load_reviewed_bank_bundle

        bundle = load_reviewed_bank_bundle(AUDITS)
    except (OSError, ValueError, KeyError):
        pass
    if bundle:
        for source in bundle["sources"].values():
            for key in ("pdf_path", "metadata_path"):
                if source.get(key):
                    required.append(REPO_ROOT / source[key])
    return [path for path in required if not path.is_file()]


@pytest.mark.asyncio
async def test_real_two_year_offline_release_after_048_upgrade(release_world, monkeypatch):
    missing = _missing_real_inputs()
    if missing:
        pytest.skip(
            "requires exact bank release evidence originals: " + ", ".join(map(str, missing))
        )

    from investment_agent.cli.commands.bank_annual import (
        import_bank_annual_snapshots,
        import_bank_annual_source_snapshots,
    )
    from investment_agent.cli.commands.peer_cohort import import_sample_roster
    from investment_agent.industry_peers.bank_annual_matrix import import_reviewed_bank_year
    from investment_agent.industry_peers.batch import submit_offline_batch
    from investment_agent.industry_peers.service import get_industry_peer_context
    from investment_agent.storage.repositories.listing import ListingRepository

    async def refuse_http(*_args, **_kwargs):
        raise AssertionError("release rehearsal attempted online collection")

    monkeypatch.setattr(
        "investment_agent.evidence.disclosure_http.OfficialHTTPS.request", refuse_http
    )
    monkeypatch.setattr(
        "investment_agent.providers.akshare_shenwan.AkshareShenwanBackend._call", refuse_http
    )
    monkeypatch.setattr(
        "investment_agent.providers.akshare_eastmoney.AkshareEastmoneyBackend._call", refuse_http
    )

    # Settings are read back from the process environment used by the rehearsal.
    from investment_agent.config import Settings

    settings = Settings()
    assert settings.industry_peer_context_enabled is True
    assert settings.investor_agent_enabled is False
    assert settings.investor_agent_background_enabled is False

    async with session_scope() as session:
        listings = ListingRepository(session)
        from investment_agent.cli.commands.bank_annual import BANK_CODES

        for code in BANK_CODES:
            await listings.ensure_a_share_listing(code, company_name=code)
        await import_sample_roster(
            session, sample_path=BANK_EVIDENCE / "sample.json", industry="bank"
        )
        fy25 = await import_bank_annual_snapshots(
            session,
            evidence_root=BANK_EVIDENCE,
            report_period=date(2025, 12, 31),
            acquired_at=datetime.now(UTC),
            audit_path=AUDIT_PATH,
            originals_root=ORIGINALS_ROOT,
        )
        assert fy25["verified_fields"] == 48
        batch25 = await submit_offline_batch(
            session,
            data_dir=release_world,
            cohort_key="bank_run02_sample",
            report_period=date(2025, 12, 31),
        )
        previous = await get_industry_peer_context(
            session,
            ticker="601398.SH",
            batch_run_id=batch25.mapping.batch_run_id,
            data_dir=release_world,
        )
        previous_values = [metric.model_dump(mode="json") for metric in previous.metrics]

        annual_cohort = await import_sample_roster(
            session,
            sample_path=BANK_EVIDENCE / "sample.json",
            industry="bank",
            bank_online=True,
        )
        assert annual_cohort.snapshot.payload["read_scope"]["adapter_version"] == "bank-annual-v1"
        fy25_annual = await import_bank_annual_source_snapshots(
            session,
            cohort_key="bank_run02_sample",
            evidence_root=BANK_EVIDENCE,
            report_period=date(2025, 12, 31),
            acquired_at=datetime.now(UTC),
            audit_path=AUDIT_PATH,
            originals_root=ORIGINALS_ROOT,
        )
        assert fy25_annual["verified_fields"] == 48
        batch25_annual = await submit_offline_batch(
            session,
            data_dir=release_world,
            cohort_key="bank_run02_sample",
            adapter_version="bank-annual-v1",
            report_period=date(2025, 12, 31),
        )
        imported24 = await import_reviewed_bank_year(
            session, cohort_key="bank_run02_sample", paths=AUDITS, evidence_root=REPO_ROOT
        )
        batch24 = await submit_offline_batch(
            session,
            data_dir=release_world,
            cohort_key="bank_run02_sample",
            adapter_version="bank-annual-v1",
            report_period=date(2024, 12, 31),
        )
        repeated_batch24 = await submit_offline_batch(
            session,
            data_dir=release_world,
            cohort_key="bank_run02_sample",
            adapter_version="bank-annual-v1",
            report_period=date(2024, 12, 31),
        )
        assert batch24.mapping.batch_run_id != batch25.mapping.batch_run_id
        assert repeated_batch24.mapping.batch_run_id == batch24.mapping.batch_run_id
        assert batch25_annual.mapping.batch_run_id != batch25.mapping.batch_run_id
        assert len(imported24["banks"]) == 10
        retained25 = await get_industry_peer_context(
            session,
            ticker="601398.SH",
            batch_run_id=batch25.mapping.batch_run_id,
            data_dir=release_world,
        )
        assert [metric.model_dump(mode="json") for metric in retained25.metrics] == previous_values
        assert await session.scalar(select(func.count()).select_from(PeerAnnualSnapshot)) == 30

    # Test the real read-only GET after the import transaction has committed.
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from investment_agent.agent.tasks import AgentTask
    from investment_agent.api.auth import get_settings
    from investment_agent.api.routes import industries
    from investment_agent.config import Settings
    from investment_agent.storage.models.analysis import PipelineRun

    settings = Settings()
    app = FastAPI()
    app.include_router(industries.router)
    app.dependency_overrides[get_settings] = lambda: settings
    async with session_scope() as session:
        before_tasks = await session.scalar(select(func.count()).select_from(AgentTask))
        before_runs = await session.scalar(select(func.count()).select_from(PipelineRun))
    with TestClient(app) as client:
        for period, expected_batch in (
            (date(2024, 12, 31), batch24.mapping.batch_run_id),
            (date(2025, 12, 31), batch25_annual.mapping.batch_run_id),
        ):
            response = client.get(
                "/industries/bank_run02_sample",
                params={"report_period": period.isoformat()},
                headers={"X-API-Key": "bank-release-rehearsal"},
            )
            assert response.status_code == 200, response.text
            overview = response.json()
            assert overview["report_period"] == period.isoformat()
            assert overview["batch_run_id"] == expected_batch
            assert overview["batch_status"] in {"success", "partial"}
            assert overview["metrics"]
    async with session_scope() as session:
        assert await session.scalar(select(func.count()).select_from(AgentTask)) == before_tasks
        assert await session.scalar(select(func.count()).select_from(PipelineRun)) == before_runs

    from alembic.script import ScriptDirectory

    script = ScriptDirectory(str(REPO_ROOT / "alembic"))
    assert script.get_current_head() == CURRENT_HEAD
