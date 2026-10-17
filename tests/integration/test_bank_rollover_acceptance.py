"""Root acceptance using separate FY2024/FY2025 official bank originals."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
import shutil
import subprocess
import sys
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from typer.testing import CliRunner

from investment_agent.cli.commands.bank_annual import BANK_CODES
from investment_agent.cli.commands.bank_rollover import app
from investment_agent.cli.commands.peer_cohort import import_sample_roster
from investment_agent.evidence.disclosure_http import DIRECTORY, QUERY, SourceResponse
from investment_agent.industry_peers.bank_annual_review import load_bank_annual_review_events
from investment_agent.industry_peers.bank_annual_sources import acquire_bank_annual_originals
from investment_agent.industry_peers.bank_annual_v2 import BANK_ANNUAL_V2_RULE
from investment_agent.industry_peers.bank_online_reconciliation import reconcile_bank_online_fields
from investment_agent.industry_peers.bank_pdf_text import bank_page_text
from investment_agent.industry_peers.bank_review_states import record_bank_online_reviews
from investment_agent.industry_peers.batch import submit_offline_batch
from investment_agent.industry_peers.evidence import peer_field_states
from investment_agent.industry_peers.jobs import _bank_snapshot_hashes, build_snapshot_fields
from investment_agent.industry_peers.originals import (
    load_annual_original,
    store_annual_original,
    validate_field_original,
)
from investment_agent.industry_peers.resolver import PeerContextResolver
from investment_agent.industry_peers.service import (
    get_industry_peer_context,
    peer_context_binding_from,
)
from investment_agent.providers.eastmoney_annual import RawAnnualResponse
from investment_agent.providers.eastmoney_bank_annual import (
    BANK_ADAPTER_VERSION,
    EastmoneyBankAnnualCollector,
)
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.disclosure import DisclosureBlob, DisclosureOriginal
from investment_agent.storage.models.peer import PeerAnnualSnapshot
from investment_agent.storage.repositories.listing import ListingRepository
from tests.integration.test_bank_verified_import import BANK_EVIDENCE, REPO_ROOT
from tests.integration.test_bank_verified_import import bank_world as bank_world

RAW = REPO_ROOT / "research/industry-data-validation/evidence/bank-rollover-2026-10-17/root"


class RecordedOfficialHTTP:
    """Replays exact bytes from root's bounded official-source requests."""

    def __init__(self):
        self.calls = []

    async def request(self, url, purpose, **kwargs):
        self.calls.append((url, purpose))
        path = {DIRECTORY: "directory.json", QUERY: "query-601398-2024.json"}.get(url)
        if path:
            return SourceResponse(
                200, {"content-type": "application/json"}, (RAW / path).read_bytes(), url
            )
        assert url == "https://static.cninfo.com.cn/finalpage/2025-03-29/1222948914.PDF"
        return SourceResponse(
            200, {"content-type": "application/pdf"}, (RAW / "601398.SH_2024.pdf").read_bytes(), url
        )


def annual_audit(original, year, listing_id, checked_at):
    """Explicit independently reviewed locators, never inferred from a status."""
    body = (
        (RAW / "601398.SH_2024.pdf").read_bytes()
        if year == 2024
        else (BANK_EVIDENCE / "raw/official/601398.SH_2025.pdf").read_bytes()
    )
    page = bank_page_text(body, 16)
    scope_page = bank_page_text(body, 14)
    scope = scope_page[
        scope_page.index("本年度报告所载") : scope_page.index("以人民币列示。")
        + len("以人民币列示。")
    ].strip()
    notes = page[page.index("注：") :]
    values = (
        {
            "roe_weighted_annual": "9.88",
            "net_interest_margin": "1.42",
            "npl_ratio": "1.34",
            "provision_coverage": "214.91",
            "equity_parent": "3969841",
        }
        if year == 2024
        else {
            "roe_weighted_annual": "9.45",
            "net_interest_margin": "1.28",
            "npl_ratio": "1.31",
            "provision_coverage": "213.60",
            "equity_parent": "4244259",
        }
    )
    labels = {
        "roe_weighted_annual": ("加权平均净资产收益率", 2),
        "net_interest_margin": ("净利息收益率", 4),
        "npl_ratio": ("不良贷款率", 7),
        "provision_coverage": ("拨备覆盖率", 8),
        "equity_parent": ("归属于母公司股东的权益", None),
    }
    fields = {}
    for metric, (header, note_number) in labels.items():
        equity = metric == "equity_parent"
        excerpt = bank_page_text(body, 207 if year == 2024 else 204) if equity else page
        note = (
            "除特别注明外，金额单位均为人民币百万元"
            if equity
            else re.search(rf"（{note_number}）(.*?)（{note_number + 1}）", notes, re.S)
            .group(1)
            .strip()
        )
        fields[metric] = {
            "metric": metric,
            "check_rule_version": BANK_ANNUAL_V2_RULE,
            "status": "verified",
            "period": f"{year}-12-31",
            "report_identity": {
                "code": "601398.SH",
                "period": f"{year}-12-31",
                "disclosure_id": original.payload["disclosure_id"],
            },
            "original_ref": {"kind": "disclosure_original", "id": original.id, "revision": 1},
            "pdf_hash": original.content_hash,
            "source_url": original.payload["source_url"],
            "pdf_page": (207 if year == 2024 else 204) if equity else 16,
            "table_title": "合并及公司资产负债表（续）" if equity else "财务指标",
            "period_header": f"{year}年 {year - 1}年 {year}年 {year - 1}年"
            if equity
            else f"{year} {year - 1} {year - 2}",
            "current_column": f"本集团/{year}年" if equity else str(year),
            "header": header,
            "raw_value": values[metric],
            "raw_unit": "CNY_million" if equity else "percent",
            "parsed_value": str(
                Decimal(values[metric]) * (Decimal(1_000_000) if equity else Decimal(".01"))
            ),
            "accounting_scope": "group_consolidated",
            "locator_excerpt": excerpt,
            "locator_excerpt_sha256": hashlib.sha256(excerpt.encode()).hexdigest(),
            "definition_or_footnote": note,
            "scope_context": None
            if equity
            else {
                "page": 14,
                "kind": "report_default",
                "excerpt": scope,
                "sha256": hashlib.sha256(scope.encode()).hexdigest(),
            },
            "checked_at": checked_at.isoformat(),
            "acquired_at": original.payload["acquired_at"],
        }
    return {
        "schema": "bank-annual-review-audit/2",
        "code": "601398",
        "listing_id": listing_id,
        "report_period": f"{year}-12-31",
        "fields": fields,
    }


async def cli(*args):
    result = await asyncio.to_thread(CliRunner().invoke, app, list(args))
    assert result.exit_code == 0, (result.output, repr(result.exception))
    start = re.search(r"(?m)^[{\[]", result.output)
    assert start is not None, result.output
    return json.loads(result.output[start.start() :])


async def test_actual_cross_year_verify_review_resolution_and_history(bank_world, monkeypatch):
    required = [RAW / p for p in ("601398.SH_2024.pdf", "directory.json", "query-601398-2024.json")]
    required.extend(
        (
            BANK_EVIDENCE / "raw/official/601398.SH_2025.pdf",
            BANK_EVIDENCE / "derived/filings/601398.SH.json",
        )
    )
    if any(not path.is_file() for path in required):
        pytest.skip(
            "requires exact FY2024/FY2025 ICBC originals and root CNInfo probe in bank-rollover-2026-10-17/root"
        )
    monkeypatch.setenv("LLM_TIER3_API_KEY", "test")
    async with session_scope() as session:
        listings = ListingRepository(session)
        for ticker in BANK_CODES:
            await listings.ensure_a_share_listing(ticker, company_name=ticker)
        target = await listings.get_a_share_listing("601398.SH")
        listing_id, company_id = target.id, target.company_id
        await import_sample_roster(
            session, sample_path=BANK_EVIDENCE / "sample.json", industry="bank", bank_online=True
        )
        official = RecordedOfficialHTTP()
        acquired = await acquire_bank_annual_originals(
            session,
            listing_id=listing_id,
            report_period=date(2024, 12, 31),
            acquired_at=datetime.now(UTC),
            data_dir=bank_world,
            http=official,
        )
        assert acquired["status"] == "ok", acquired
        assert len(official.calls) == 4
        original24 = await session.get(DisclosureOriginal, acquired["originals"][0]["id"])
        assert original24.payload["ticker"] == "601398.SH"
        cached = await acquire_bank_annual_originals(
            session,
            listing_id=listing_id,
            report_period=date(2024, 12, 31),
            acquired_at=datetime.now(UTC),
            data_dir=bank_world,
            http=official,
        )
        assert cached["status"] == "ok" and len(official.calls) == 4
        missing = await acquire_bank_annual_originals(
            session,
            listing_id=listing_id,
            report_period=date(datetime.now(UTC).year, 12, 31),
            acquired_at=datetime.now(UTC),
            data_dir=bank_world,
            http=official,
        )
        assert missing["status"] == "missing" and len(official.calls) == 4
        original25 = await store_annual_original(
            session,
            load_annual_original(BANK_EVIDENCE, "601398.SH", date(2025, 12, 31)),
            listing_id,
            datetime.now(UTC),
        )
        audit24 = annual_audit(original24, 2024, listing_id, datetime.now(UTC))
        audit25 = annual_audit(original25, 2025, listing_id, datetime.now(UTC))
    audit_paths = {}
    for year, audit in ((2024, audit24), (2025, audit25)):
        path = bank_world / f"audit-{year}.json"
        path.write_text(json.dumps(audit, ensure_ascii=False, indent=2))
        audit_paths[year] = path
        template = bank_world / f"template-{year}.json"
        await cli(
            "prepare",
            "--cohort",
            "bank_run02_sample",
            "--listing-id",
            str(listing_id),
            "--period",
            f"{year}-12-31",
            "--output",
            str(template),
        )
        assert set(json.loads(template.read_text())["fields"]) == set(audit["fields"])
        result = await cli(
            "verify",
            "--cohort",
            "bank_run02_sample",
            "--listing-id",
            str(listing_id),
            "--period",
            f"{year}-12-31",
            "--audit",
            str(path),
        )
        async with session_scope() as session:
            snap = await session.get(PeerAnnualSnapshot, result["snapshot_id"])
            assert all(field["verification"] == "verified" for field in snap.fields.values()), (
                snap.verification
            )
            for metric in snap.fields:
                assert await validate_field_original(session, snap, metric)
        repeated = await cli(
            "verify",
            "--cohort",
            "bank_run02_sample",
            "--listing-id",
            str(listing_id),
            "--period",
            f"{year}-12-31",
            "--audit",
            str(path),
        )
        assert repeated["snapshot_id"] == result["snapshot_id"]
    async with session_scope() as session:
        assert await session.scalar(select(func.count()).select_from(PeerAnnualSnapshot)) == 2
        initial = await submit_offline_batch(
            session,
            data_dir=bank_world,
            cohort_key="bank_run02_sample",
            adapter_version=BANK_ADAPTER_VERSION,
            report_period=date(2024, 12, 31),
            tickers=["601398.SH"],
        )
        initial_context = await get_industry_peer_context(
            session,
            ticker="601398.SH",
            batch_run_id=initial.mapping.batch_run_id,
            data_dir=bank_world,
        )
        old_values = [m.model_dump(mode="json") for m in initial_context.metrics]
        binding = peer_context_binding_from(initial_context)
        assert binding is not None
        resolver = PeerContextResolver(session, binding=binding, data_dir=bank_world)
        for metric in initial_context.metrics:
            assert (
                await resolver.verify(metric.calculation_ref, require_verified_inputs=True)
            ).inputs_verified

    # Explicitly controlled supplier revision; this is not an observed official correction.
    class ChangedSupplier:
        async def fetch(self, url, params):
            row = {
                "SECUCODE": "601398.SH",
                "REPORT_DATE": "2024-12-31",
                "REPORT_TYPE": "年报",
                "NOTICE_DATE": "2025-03-29",
                "UPDATE_DATE": "2026-10-17",
                "CURRENCY": "CNY",
                "ORG_TYPE": "银行",
                "ROEJQ": 9.88,
                "NET_INTEREST_MARGIN": 1.42,
                "NONPERLOAN": 1.34,
                "BLDKBBL": 214.92,
            }
            return RawAnnualResponse(
                status=200,
                body=json.dumps(
                    {
                        "success": True,
                        "version": "controlled-update",
                        "result": {"pages": 1, "data": [row]},
                    }
                ).encode(),
            )

    collection = await EastmoneyBankAnnualCollector(
        ChangedSupplier(), min_interval_seconds=0
    ).collect_company_annual("601398.SH", date(2024, 12, 31))
    fields, proofs = build_snapshot_fields(collection, metric_pack="bank_annual_v1")
    async with session_scope() as session:
        fields, proofs, summary = await reconcile_bank_online_fields(
            session,
            listing_id=listing_id,
            company_id=company_id,
            report_period=date(2024, 12, 31),
            fields=fields,
            proofs=proofs,
            captured_at=collection.captured_at,
        )
        semantic, content = _bank_snapshot_hashes(fields, proofs)
        candidate = PeerAnnualSnapshot(
            id=str(uuid4()),
            listing_id=listing_id,
            company_id=company_id,
            report_period=date(2024, 12, 31),
            provider="eastmoney",
            adapter_version=BANK_ADAPTER_VERSION,
            fields={k: v.model_dump(mode="json") for k, v in fields.items()},
            verification=proofs,
            captured_at=collection.captured_at,
            semantic_hash=semantic,
            content_hash=content,
        )
        session.add(candidate)
        await session.flush()
        review_ids = await record_bank_online_reviews(
            session, candidate, summary["baseline_snapshot_by_metric"]
        )
        assert len(review_ids) == 1
        before = await peer_field_states(
            session,
            company_id=company_id,
            metric="provision_coverage",
            report_period=date(2024, 12, 31),
        )
        assert before.status == "pending_recheck"
    reverified = await cli(
        "verify",
        "--cohort",
        "bank_run02_sample",
        "--listing-id",
        str(listing_id),
        "--period",
        "2024-12-31",
        "--audit",
        str(audit_paths[2024]),
    )
    assert reverified["reviews"] == []
    pending_queue = await cli(
        "reviews", "list", "--listing-id", str(listing_id), "--period", "2024-12-31"
    )
    assert len(pending_queue) == 1 and pending_queue[0]["review_id"] == review_ids[0]
    resolve_audit = copy.deepcopy(audit24)
    for item in resolve_audit["fields"].values():
        item["checked_at"] = datetime.now(UTC).isoformat()
    resolution_path = bank_world / "resolution-audit.json"
    resolution_path.write_text(json.dumps(resolve_audit, ensure_ascii=False))
    resolved = await cli(
        "resolve", review_ids[0], "--audit", str(resolution_path), "--decision", "reject_supplier"
    )
    assert resolved["decision"] == "reject_supplier"
    replayed = await cli(
        "resolve", review_ids[0], "--audit", str(resolution_path), "--decision", "reject_supplier"
    )
    assert replayed["replayed"] is True
    async with session_scope() as session:
        snap = await session.get(PeerAnnualSnapshot, resolved["snapshot_id"])
        assert snap.fields["provision_coverage"]["value"] == "2.1491"
        assert await validate_field_original(session, snap, "provision_coverage")
        after = await peer_field_states(
            session,
            company_id=company_id,
            metric="provision_coverage",
            report_period=date(2024, 12, 31),
        )
        assert (
            next(v.state for v in after.versions if str(v.source_ref.id) == snap.id) == "correction"
        ), after
        # Observing the exact same rejected supplier version revalidates the
        # approved original; a different supplier version must be reviewed anew.
        recollection = await EastmoneyBankAnnualCollector(
            ChangedSupplier(), min_interval_seconds=0
        ).collect_company_annual("601398.SH", date(2024, 12, 31))
        fresh_fields, fresh_proofs = build_snapshot_fields(
            recollection, metric_pack="bank_annual_v1"
        )
        reused_fields, reused_proofs, _ = await reconcile_bank_online_fields(
            session,
            listing_id=listing_id,
            company_id=company_id,
            report_period=date(2024, 12, 31),
            fields=fresh_fields,
            proofs=fresh_proofs,
            captured_at=recollection.captured_at,
        )
        assert reused_fields["provision_coverage"].verification == "verified"
        assert reused_fields["provision_coverage"].value == Decimal("2.1491")
        assert (
            reused_proofs["provision_coverage"]["supplier_comparison"][
                "reviewed_supplier_rejection"
            ]
            is True
        )
        # A new vendor source version cannot inherit a previous rejection.
        fresh_fields["provision_coverage"] = fresh_fields["provision_coverage"].model_copy(
            update={"source_version": "changed-again-after-resolution"}
        )
        changed_fields, _, _ = await reconcile_bank_online_fields(
            session,
            listing_id=listing_id,
            company_id=company_id,
            report_period=date(2024, 12, 31),
            fields=fresh_fields,
            proofs=fresh_proofs,
            captured_at=recollection.captured_at,
        )
        assert changed_fields["provision_coverage"].verification != "verified"
        final = await submit_offline_batch(
            session,
            data_dir=bank_world,
            cohort_key="bank_run02_sample",
            adapter_version=BANK_ADAPTER_VERSION,
            report_period=date(2024, 12, 31),
            tickers=["601398.SH"],
        )
        historical = await get_industry_peer_context(
            session,
            ticker="601398.SH",
            batch_run_id=initial.mapping.batch_run_id,
            cutoff=initial_context.data_as_of,
            data_dir=bank_world,
        )
        assert [m.model_dump(mode="json") for m in historical.metrics] == old_values
        current = await get_industry_peer_context(
            session,
            ticker="601398.SH",
            batch_run_id=final.mapping.batch_run_id,
            data_dir=bank_world,
        )
        final_binding = peer_context_binding_from(current)
        assert final_binding is not None
        resolver = PeerContextResolver(session, binding=final_binding, data_dir=bank_world)
        for metric in current.metrics:
            assert (
                await resolver.verify(metric.calculation_ref, require_verified_inputs=True)
            ).inputs_verified
        assert await session.scalar(select(func.count()).select_from(DisclosureBlob)) == 2
        assert len(await load_bank_annual_review_events(session)) >= 2
    from investment_agent.api.app import create_app

    response = TestClient(create_app()).get(
        f"/industries/bank_run02_sample?batch_run_id={final.mapping.batch_run_id}",
        headers={"X-API-Key": "bank-proof-integration"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["report_period"] == "2024-12-31"

    # Exercise the supported backup tools, then validate through a fresh engine.
    backup = bank_world.parent / f"{bank_world.name}-backup"
    restored = bank_world.parent / f"{bank_world.name}-restored"
    for arguments in (
        [
            "tools/backup_research.py",
            "--data-dir",
            str(bank_world),
            "--db",
            str(bank_world / "bank-proof.db"),
            "--output",
            str(backup),
        ],
        ["tools/verify_backup.py", "--backup-dir", str(backup)],
    ):
        result = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, *arguments],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
    shutil.copytree(backup, restored)
    engine = create_async_engine(f"sqlite+aiosqlite:///{restored / 'app.db'}")
    try:
        async with async_sessionmaker(engine)() as session:
            restored_context = await get_industry_peer_context(
                session,
                ticker="601398.SH",
                batch_run_id=final.mapping.batch_run_id,
                data_dir=restored,
            )
            restored_binding = peer_context_binding_from(restored_context)
            assert restored_binding is not None
            resolver = PeerContextResolver(session, binding=restored_binding, data_dir=restored)
            for metric in restored_context.metrics:
                assert (
                    await resolver.verify(metric.calculation_ref, require_verified_inputs=True)
                ).inputs_verified
            history = await get_industry_peer_context(
                session,
                ticker="601398.SH",
                batch_run_id=initial.mapping.batch_run_id,
                cutoff=initial_context.data_as_of,
                data_dir=restored,
            )
            assert [m.model_dump(mode="json") for m in history.metrics] == old_values
            assert len(await load_bank_annual_review_events(session)) >= 2
            assert await session.scalar(select(func.count()).select_from(DisclosureBlob)) == 2
            for snapshot in await session.scalars(select(PeerAnnualSnapshot)):
                for metric, field in snapshot.fields.items():
                    if field["verification"] == "verified":
                        assert await validate_field_original(session, snapshot, metric)
    finally:
        await engine.dispose()
