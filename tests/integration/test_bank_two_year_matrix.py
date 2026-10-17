"""Independent acceptance of the ten-bank FY2024/FY2025 evidence matrix."""

from __future__ import annotations

import asyncio
import copy
import json
import shutil
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from typer.testing import CliRunner

from investment_agent.cli.commands.bank_annual import BANK_CODES, import_bank_annual_snapshots
from investment_agent.cli.commands.peer_cohort import import_sample_roster
from investment_agent.industry_peers.bank_annual_matrix import (
    import_reviewed_bank_year,
    load_reviewed_bank_bundle,
)
from investment_agent.industry_peers.batch import submit_offline_batch
from investment_agent.industry_peers.originals import validate_field_original
from investment_agent.industry_peers.resolver import PeerContextResolver
from investment_agent.industry_peers.service import (
    get_industry_peer_context,
    peer_context_binding_from,
)
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.disclosure import DisclosureBlob, DisclosureOriginal
from investment_agent.storage.models.peer import PeerAnnualSnapshot
from investment_agent.storage.repositories.listing import ListingRepository
from tests.integration.test_bank_verified_import import (
    AUDIT_PATH,
    BANK_EVIDENCE,
    ORIGINALS_ROOT,
    REPO_ROOT,
)
from tests.integration.test_bank_verified_import import bank_world as bank_world

AUDITS = [
    REPO_ROOT / f"tests/fixtures/bank_fy2024/{name}.json"
    for name in ("icbc", "large", "regional", "scope")
]


async def test_two_complete_years_revalidate_originals_batches_history_and_restore(
    bank_world, monkeypatch
):
    bundle = load_reviewed_bank_bundle(AUDITS)
    if any(
        not (REPO_ROOT / source[key]).is_file()
        for source in bundle["sources"].values()
        for key in ("pdf_path", "metadata_path")
    ):
        pytest.skip("requires the exact ten original FY2024 bank report captures")

    async def no_http(*_args, **_kwargs):
        raise AssertionError("offline evidence import/readback must not access HTTP")

    monkeypatch.setattr("investment_agent.evidence.disclosure_http.OfficialHTTPS.request", no_http)
    async with session_scope() as session:
        listings = ListingRepository(session)
        for code in BANK_CODES:
            await listings.ensure_a_share_listing(code, company_name=code)
        await import_sample_roster(
            session, sample_path=BANK_EVIDENCE / "sample.json", industry="bank"
        )
        report25 = await import_bank_annual_snapshots(
            session,
            evidence_root=BANK_EVIDENCE,
            report_period=date(2025, 12, 31),
            acquired_at=datetime.now(UTC),
            audit_path=AUDIT_PATH,
            originals_root=ORIGINALS_ROOT,
        )
        assert report25["verified_fields"] == 48
        baseline = {
            row.id: copy.deepcopy(row.fields)
            for row in await session.scalars(select(PeerAnnualSnapshot))
        }
        initial_batch = await submit_offline_batch(
            session,
            data_dir=bank_world,
            cohort_key="bank_run02_sample",
            report_period=date(2025, 12, 31),
        )
        old_context = await get_industry_peer_context(
            session,
            ticker="601398.SH",
            batch_run_id=initial_batch.mapping.batch_run_id,
            data_dir=bank_world,
        )
        historical_values = [m.model_dump(mode="json") for m in old_context.metrics]
    async with session_scope() as session:
        await import_sample_roster(
            session, sample_path=BANK_EVIDENCE / "sample.json", industry="bank", bank_online=True
        )
        result = await import_reviewed_bank_year(
            session, cohort_key="bank_run02_sample", paths=AUDITS, evidence_root=REPO_ROOT
        )
        assert len(result["banks"]) == 10
        statuses = [
            field["status"] for bank in result["banks"] for field in bank["fields"].values()
        ]
        assert statuses.count("verified") == 47
        assert statuses.count("candidate") == 1
        assert statuses.count("scope_mismatch") == 2
        repeated = await import_reviewed_bank_year(
            session, cohort_key="bank_run02_sample", paths=AUDITS, evidence_root=REPO_ROOT
        )
        assert [b["snapshot_id"] for b in repeated["banks"]] == [
            b["snapshot_id"] for b in result["banks"]
        ]
        rows = list(await session.scalars(select(PeerAnnualSnapshot)))
        assert len(rows) == 20
        verified = 0
        for row in rows:
            for metric, field in row.fields.items():
                if field["verification"] == "verified":
                    assert await validate_field_original(session, row, metric), (
                        row.id,
                        metric,
                        row.verification[metric],
                    )
                    verified += 1
            if row.id in baseline:
                assert row.fields == baseline[row.id]
        assert verified == 95
        icbc24 = await session.get(
            PeerAnnualSnapshot,
            next(b["snapshot_id"] for b in result["banks"] if b["code"] == "601398.SH"),
        )
        probe = SimpleNamespace(
            **{
                key: getattr(icbc24, key)
                for key in ("listing_id", "report_period", "captured_at", "fields", "verification")
            }
        )
        proof = copy.deepcopy(icbc24.verification["provision_coverage"])
        proof["raw_value"] = "999.99"
        proof["audit_record"]["raw_value"] = "999.99"
        from investment_agent.industry_peers.bank_annual_v2 import validate_bank_annual_v2

        assert not await validate_bank_annual_v2(session, probe, "provision_coverage", proof)
        probe.report_period = date(2025, 12, 31)
        assert not await validate_field_original(session, probe, "provision_coverage")
        assert not await validate_field_original(
            session, icbc24, "provision_coverage", cutoff=datetime(2025, 12, 31, tzinfo=UTC)
        )
        blob = await session.get(
            DisclosureBlob, icbc24.verification["provision_coverage"]["pdf_hash"]
        )
        original_body = blob.body
        # Simulate corruption outside the ORM: normal ORM writes are immutable.
        await session.execute(
            update(DisclosureBlob)
            .where(DisclosureBlob.content_hash == blob.content_hash)
            .values(body=original_body + b"tampered")
        )
        assert not await validate_field_original(session, icbc24, "provision_coverage")
        await session.execute(
            update(DisclosureBlob)
            .where(DisclosureBlob.content_hash == blob.content_hash)
            .values(body=original_body)
        )
        ningbo24 = await session.get(
            PeerAnnualSnapshot,
            next(b["snapshot_id"] for b in result["banks"] if b["code"] == "002142.SZ"),
        )
        supplemental_ref = ningbo24.verification["npl_ratio"]["supplemental_originals"][0][
            "original_ref"
        ]
        supplemental = await session.get(DisclosureOriginal, supplemental_ref["id"])
        await session.execute(
            update(DisclosureOriginal)
            .where(DisclosureOriginal.id == supplemental.id)
            .values(owner_id="wrong-owner")
        )
        assert not await validate_field_original(session, ningbo24, "npl_ratio")
        await session.execute(
            update(DisclosureOriginal)
            .where(DisclosureOriginal.id == supplemental.id)
            .values(owner_id="local_owner")
        )
        assert await validate_field_original(session, ningbo24, "npl_ratio")
        fy24 = await submit_offline_batch(
            session,
            data_dir=bank_world,
            cohort_key="bank_run02_sample",
            adapter_version="bank-annual-v1",
            report_period=date(2024, 12, 31),
        )
        new_context = await get_industry_peer_context(
            session, ticker="601398.SH", batch_run_id=fy24.mapping.batch_run_id, data_dir=bank_world
        )
        resolver = PeerContextResolver(
            session, binding=peer_context_binding_from(new_context), data_dir=bank_world
        )
        for metric in new_context.metrics:
            assert (
                await resolver.verify(metric.calculation_ref, require_verified_inputs=True)
            ).inputs_verified
        old = await get_industry_peer_context(
            session,
            ticker="601398.SH",
            batch_run_id=initial_batch.mapping.batch_run_id,
            cutoff=old_context.data_as_of,
            data_dir=bank_world,
        )
        assert [m.model_dump(mode="json") for m in old.metrics] == historical_values
        assert await session.scalar(select(func.count()).select_from(DisclosureBlob)) == 21
        matrix = {"FY2024": result, "FY2025": {"verified": 48, "scope_mismatch": 2}}
        (bank_world / "research-runs" / "two-year-matrix.json").write_text(
            json.dumps(matrix, ensure_ascii=False, indent=2)
        )

    from investment_agent.cli.commands.bank_rollover import app

    output = bank_world / "research-runs" / "cli-fy2024-matrix.json"
    args = [
        "import-reviewed",
        "--cohort",
        "bank_run02_sample",
        "--evidence-root",
        str(REPO_ROOT),
        "--output",
        str(output),
    ]
    for path in AUDITS:
        args.extend(["--bundle", str(path)])
    command = await asyncio.to_thread(CliRunner().invoke, app, args)
    assert command.exit_code == 0, (command.output, command.exception)
    assert len(json.loads(output.read_text())["banks"]) == 10
    from investment_agent.api.app import create_app

    response = TestClient(create_app()).get(
        f"/industries/bank_run02_sample?batch_run_id={fy24.mapping.batch_run_id}",
        headers={"X-API-Key": "bank-proof-integration"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["report_period"] == "2024-12-31"

    # Use the existing SQLite/artifact backup implementation and re-open it.
    from tools.backup_research import backup_sqlite

    restored = bank_world.parent / (bank_world.name + "-restored")
    restored.mkdir()
    backup_sqlite(bank_world / "bank-proof.db", restored / "app.db")
    shutil.copytree(bank_world / "research-runs", restored / "research-runs")
    engine = create_async_engine(f"sqlite+aiosqlite:///{restored / 'app.db'}")
    try:
        async with async_sessionmaker(engine)() as session:
            verified = 0
            for row in await session.scalars(select(PeerAnnualSnapshot)):
                for metric, field in row.fields.items():
                    if field["verification"] == "verified":
                        assert await validate_field_original(session, row, metric)
                        verified += 1
            assert verified == 95
            context = await get_industry_peer_context(
                session,
                ticker="601398.SH",
                batch_run_id=fy24.mapping.batch_run_id,
                data_dir=restored,
            )
            resolver = PeerContextResolver(
                session, binding=peer_context_binding_from(context), data_dir=restored
            )
            for metric in context.metrics:
                assert (
                    await resolver.verify(metric.calculation_ref, require_verified_inputs=True)
                ).inputs_verified
    finally:
        await engine.dispose()
