"""Real-PDF bank audit import through the saved batch, API, and resolver."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from investment_agent.storage.database import engine_lifespan, session_scope
from investment_agent.storage.models import Base

REPO_ROOT = Path(__file__).resolve().parents[2]
BANK_EVIDENCE = REPO_ROOT / "research/industry-data-validation/evidence/run-02"
ORIGINALS_ROOT = REPO_ROOT / "research/industry-data-validation/evidence/bank-proof-2026-10-15"
AUDIT_PATH = REPO_ROOT / "tests/fixtures/bank_proofs_v2/audit-manifest.json"
SOURCE_MANIFEST = ORIGINALS_ROOT / "source-manifest.json"
PERIOD = date(2025, 12, 31)
TARGET = "601398.SH"
AUTH = {"X-API-Key": "bank-proof-integration"}


def _required_real_inputs() -> list[Path]:
    """Exact source PDFs and metadata that make this a real-document test."""
    required = [BANK_EVIDENCE / "sample.json", AUDIT_PATH, SOURCE_MANIFEST]
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
    if SOURCE_MANIFEST.is_file():
        source = json.loads(SOURCE_MANIFEST.read_text(encoding="utf-8"))
        for item in source.get("sources", {}).values():
            for key in ("pdf_path", "metadata_path"):
                value = item.get(key)
                if value:
                    required.append(REPO_ROOT / value)
            for supplement in item.get("supplemental_originals", {}).values():
                if supplement.get("pdf_path"):
                    required.append(REPO_ROOT / supplement["pdf_path"])
    return required


def _import_cutoff_after_audit() -> datetime:
    """Keep integration import times later than the explicitly recorded reviews."""
    audit = json.loads(AUDIT_PATH.read_text(encoding="utf-8"))
    checked = [
        datetime.fromisoformat(row["checked_at"].replace("Z", "+00:00"))
        for row in audit["records"]
        if row.get("checked_at")
    ]
    return max([datetime.now(UTC), *checked]) + timedelta(seconds=1)


@pytest.fixture
def requires_bank_originals():
    required = _required_real_inputs()
    missing = [str(path.relative_to(REPO_ROOT)) for path in required if not path.is_file()]
    if missing:
        pytest.skip(
            "real bank PDF audit acceptance needs these exact inputs: " + ", ".join(missing)
        )


@pytest_asyncio.fixture
async def bank_world(tmp_path, monkeypatch):
    database = f"sqlite+aiosqlite:///{tmp_path / 'bank-proof.db'}"
    for key, value in {
        "DATABASE_URL": database,
        "DATA_DIR": str(tmp_path),
        "API_KEY": "bank-proof-integration",
        "INDUSTRY_PEER_CONTEXT_ENABLED": "true",
    }.items():
        monkeypatch.setenv(key, value)
    async with engine_lifespan(database) as engine:
        # This isolated database has the current schema; no production DB is used.
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        yield tmp_path


async def test_real_bank_import_reaches_api_and_verified_resolver(
    bank_world, requires_bank_originals
):
    from sqlalchemy import select

    from investment_agent.api.app import create_app
    from investment_agent.cli.commands.bank_annual import BANK_CODES, import_bank_annual_snapshots
    from investment_agent.cli.commands.peer_cohort import import_sample_roster
    from investment_agent.industry_peers.batch import submit_offline_batch
    from investment_agent.industry_peers.resolver import PeerContextResolver
    from investment_agent.industry_peers.service import (
        get_industry_peer_context,
        peer_context_binding_from,
    )
    from investment_agent.storage.models.peer import PeerAnnualSnapshot
    from investment_agent.storage.repositories.listing import ListingRepository

    imported_at = _import_cutoff_after_audit()
    async with session_scope() as session:
        listings = ListingRepository(session)
        for code in BANK_CODES:
            await listings.ensure_a_share_listing(code, company_name=code)
        await import_sample_roster(
            session,
            sample_path=BANK_EVIDENCE / "sample.json",
            industry="bank",
            acquired_at=imported_at,
        )
        report = await import_bank_annual_snapshots(
            session,
            evidence_root=BANK_EVIDENCE,
            report_period=PERIOD,
            acquired_at=imported_at,
            audit_path=AUDIT_PATH,
            originals_root=ORIGINALS_ROOT,
        )
        assert report["verified_fields"] >= 1
        assert report["field_states"]["600919.SH"]["verified"] >= 1
        rows = list((await session.execute(select(PeerAnnualSnapshot))).scalars())
        assert len(rows) == len(BANK_CODES)
        jiangsu_listing = await listings.get_a_share_listing("600919.SH")
        saved = next(row for row in rows if row.listing_id == jiangsu_listing.id)
        assert saved.fields["equity_parent"]["value"] == "337961238000"
        assert saved.fields["equity_parent"]["verification"] == "verified"
        assert saved.verification["equity_parent"]["checked_at"]
        pingan_listing = await listings.get_a_share_listing("000001.SZ")
        pingan = next(row for row in rows if row.listing_id == pingan_listing.id)
        for metric in ("npl_ratio", "provision_coverage"):
            assert pingan.fields[metric]["value"] is None
            assert pingan.fields[metric]["missing_reason"] == (
                "原件已确认本行口径，不参与集团口径比较"
            )
            assert pingan.fields[metric]["raw_value"] is not None
            assert pingan.verification[metric]["status"] == "missing"
            assert pingan.verification[metric]["scope_exclusion"] is True
            assert pingan.verification[metric]["audit_record"]["raw_value"] is not None
        ningbo_listing = await listings.get_a_share_listing("002142.SZ")
        ningbo = next(row for row in rows if row.listing_id == ningbo_listing.id)
        for metric in ("npl_ratio", "provision_coverage"):
            assert ningbo.fields[metric]["verification"] == "verified"
            assert ningbo.verification[metric]["accounting_scope"] == "group_consolidated"
        assert ningbo.fields["npl_ratio"]["value"] == "0.0076"
        assert ningbo.fields["provision_coverage"]["value"] == "3.7316"

    submitted_at = imported_at + timedelta(seconds=3)
    async with session_scope() as session:
        batch = await submit_offline_batch(
            session,
            data_dir=bank_world,
            cohort_key="bank_run02_sample",
            report_period=PERIOD,
            submitted_at=submitted_at,
        )
    assert batch.created is True

    client = TestClient(create_app())
    listing = client.get("/industries", headers=AUTH)
    assert listing.status_code == 200, listing.text
    assert any(item["cohort_key"] == "bank_run02_sample" for item in listing.json()["industries"])
    overview = client.get("/industries/bank_run02_sample", headers=AUTH)
    assert overview.status_code == 200, overview.text
    assert overview.json()["metric_pack"] == "bank_annual_v1"

    async with session_scope() as session:
        context = await get_industry_peer_context(session, ticker=TARGET, data_dir=bank_world)
        binding = peer_context_binding_from(context)
        assert binding is not None and binding.metric_pack == "bank_annual_v1"
        refs = {metric.metric_key: metric.calculation_ref for metric in context.metrics}
        resolver = PeerContextResolver(session, binding=binding, data_dir=bank_world)
        # Ningbo's final consolidated scope supplement completes the remaining
        # candidate inputs. Ping An's two standalone rows remain explicit
        # missing inputs, so they cannot enter the consolidated ratio sample.
        for metric_key, calculation_ref in refs.items():
            assert calculation_ref is not None
            validation = await resolver.verify(calculation_ref, require_verified_inputs=False)
            assert validation.inputs_verified, metric_key
            strict = await resolver.verify(calculation_ref, require_verified_inputs=True)
            assert strict.inputs_verified


async def test_real_original_hash_mismatch_fails_before_any_snapshot_write(
    bank_world, requires_bank_originals, tmp_path
):
    from sqlalchemy import select

    from investment_agent.cli.commands.bank_annual import BANK_CODES, import_bank_annual_snapshots
    from investment_agent.storage.models.peer import PeerAnnualSnapshot
    from investment_agent.storage.repositories.listing import ListingRepository

    tampered_audit = json.loads(AUDIT_PATH.read_text(encoding="utf-8"))
    tampered_audit["records"][0]["pdf_sha256"] = "0" * 64
    audit_path = tmp_path / "tampered-audit.json"
    audit_path.write_text(json.dumps(tampered_audit), encoding="utf-8")
    imported_at = _import_cutoff_after_audit()
    async with session_scope() as session:
        listings = ListingRepository(session)
        for code in BANK_CODES:
            await listings.ensure_a_share_listing(code, company_name=code)
        with pytest.raises(ValueError, match="filing identity/hash does not match"):
            await import_bank_annual_snapshots(
                session,
                evidence_root=BANK_EVIDENCE,
                report_period=PERIOD,
                acquired_at=imported_at,
                audit_path=audit_path,
                originals_root=ORIGINALS_ROOT,
            )
        assert not list((await session.execute(select(PeerAnnualSnapshot))).scalars())


async def test_untrusted_supplement_fails_preflight_before_any_original_or_snapshot_write(
    bank_world, requires_bank_originals, tmp_path,
):
    from sqlalchemy import func, select

    from investment_agent.cli.commands.bank_annual import BANK_CODES, import_bank_annual_snapshots
    from investment_agent.storage.models.disclosure import DisclosureBlob, DisclosureOriginal
    from investment_agent.storage.models.peer import PeerAnnualSnapshot
    from investment_agent.storage.repositories.listing import ListingRepository

    audit = json.loads(AUDIT_PATH.read_text())
    source = json.loads(SOURCE_MANIFEST.read_text())
    supplement = source["sources"]["002142.SZ"]["supplemental_originals"]["ningbo_final_scope"]
    supplement["publisher"] = "宁波银行股份有限公司"
    altered_source = tmp_path / "untrusted-supplement-source.json"
    altered_source.write_text(json.dumps(source, ensure_ascii=False))
    audit["source_manifest"] = str(altered_source)
    altered_audit = tmp_path / "untrusted-supplement-audit.json"
    altered_audit.write_text(json.dumps(audit, ensure_ascii=False))
    async with session_scope() as session:
        listings = ListingRepository(session)
        for code in BANK_CODES:
            await listings.ensure_a_share_listing(code, company_name=code)
        with pytest.raises(ValueError, match="Ningbo|supplemental|CCXI|source identity"):
            await import_bank_annual_snapshots(
                session, evidence_root=BANK_EVIDENCE, report_period=PERIOD,
                acquired_at=_import_cutoff_after_audit(), audit_path=altered_audit,
                originals_root=ORIGINALS_ROOT,
            )
        for model in (PeerAnnualSnapshot, DisclosureOriginal, DisclosureBlob):
            assert await session.scalar(select(func.count()).select_from(model)) == 0


async def test_scope_mismatch_requires_real_pdf_scope_context_before_exclusion(
    bank_world, requires_bank_originals, tmp_path
):
    from sqlalchemy import select

    from investment_agent.cli.commands.bank_annual import BANK_CODES, import_bank_annual_snapshots
    from investment_agent.storage.models.peer import PeerAnnualSnapshot
    from investment_agent.storage.repositories.listing import ListingRepository

    bad_audit = json.loads(AUDIT_PATH.read_text(encoding="utf-8"))
    record = next(
        row
        for row in bad_audit["records"]
        if row["code"] == "000001.SZ" and row["metric"] == "npl_ratio"
    )
    record["status"] = "scope_mismatch"
    record["accounting_scope"] = "bank_standalone"
    # A status and claimed scope alone cannot exclude a supplier field.
    record["scope_context"] = {
        "page": 21,
        "kind": "section_default",
        "excerpt": "其他指标均为本行口径",
        "sha256": "0" * 64,
    }
    audit_path = tmp_path / "scope-status-only.json"
    audit_path.write_text(json.dumps(bad_audit, ensure_ascii=False), encoding="utf-8")
    async with session_scope() as session:
        listings = ListingRepository(session)
        for code in BANK_CODES:
            await listings.ensure_a_share_listing(code, company_name=code)
        with pytest.raises(ValueError, match="scope exclusion check failed"):
            await import_bank_annual_snapshots(
                session,
                evidence_root=BANK_EVIDENCE,
                report_period=PERIOD,
                acquired_at=_import_cutoff_after_audit(),
                audit_path=audit_path,
                originals_root=ORIGINALS_ROOT,
            )
        assert not list((await session.execute(select(PeerAnnualSnapshot))).scalars())


async def test_verified_standalone_rows_become_missing_group_inputs(
    bank_world, requires_bank_originals, tmp_path
):
    from sqlalchemy import select

    from investment_agent.cli.commands.bank_annual import BANK_CODES, import_bank_annual_snapshots
    from investment_agent.storage.models.peer import PeerAnnualSnapshot
    from investment_agent.storage.repositories.listing import ListingRepository

    # Isolate the new exclusion path from other issuers' independent field
    # locator work while retaining the actual Ping An PDFs and audit facts.
    audit = json.loads(AUDIT_PATH.read_text(encoding="utf-8"))
    for record in audit["records"]:
        if (
            record["status"] == "verified"
            and not (
                record["code"] == "000001.SZ"
                and record["metric"] in {"npl_ratio", "provision_coverage"}
            )
        ):
            record["status"] = "candidate"
    audit_path = tmp_path / "scope-isolated-audit.json"
    audit_path.write_text(json.dumps(audit, ensure_ascii=False), encoding="utf-8")
    async with session_scope() as session:
        listings = ListingRepository(session)
        for code in BANK_CODES:
            await listings.ensure_a_share_listing(code, company_name=code)
        await import_bank_annual_snapshots(
            session,
            evidence_root=BANK_EVIDENCE,
            report_period=PERIOD,
            acquired_at=_import_cutoff_after_audit(),
            audit_path=audit_path,
            originals_root=ORIGINALS_ROOT,
        )
        rows = list((await session.execute(select(PeerAnnualSnapshot))).scalars())
        pingan_listing = await listings.get_a_share_listing("000001.SZ")
        pingan = next(row for row in rows if row.listing_id == pingan_listing.id)
        for metric, raw_value in (("npl_ratio", "1.05"), ("provision_coverage", "220.88")):
            assert pingan.fields[metric]["value"] is None
            assert pingan.fields[metric]["raw_value"] == raw_value
            assert pingan.fields[metric]["raw_unit"] == "percent"
            assert pingan.verification[metric]["status"] == "missing"
            assert pingan.verification[metric]["accounting_scope"] == "bank_standalone"
