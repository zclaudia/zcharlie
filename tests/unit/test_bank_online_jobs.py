"""Closed online bank metric-pack dispatch and snapshot conversion."""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
from sqlalchemy import func, select, update

from investment_agent.cli.commands.peer_cohort import import_bank_roster, import_sample_roster
from investment_agent.industry_peers.artifacts import mapping_artifact_path, read_batch_mapping
from investment_agent.industry_peers.jobs import (
    CollectArtifactStore,
    _adapter_for_pack,
    _bank_snapshot_hashes,
    _validate_online_scope,
    admit_peer_collect,
    build_snapshot_fields,
    enqueue_peer_collect,
)
from investment_agent.industry_peers.metric_packs import BAIJIU_PACK, BANK_PACK
from investment_agent.providers.eastmoney_annual import PROVIDER, RawAnnualResponse
from investment_agent.providers.eastmoney_bank_annual import (
    BANK_ADAPTER_VERSION,
    BANK_ANNUAL_METRICS,
)
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.peer import PeerAnnualSnapshot, PeerCohortSnapshot
from investment_agent.storage.repositories.listing import ListingRepository
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository
from tests.support.peer_world import (
    COLLECT_PERIOD,
    DirectCtx,
    claim_job,
    make_settings,
)
from tests.support.peer_world import (
    peer_db as peer_db,  # noqa: F401
)


def test_online_scope_is_closed_by_pack() -> None:
    assert _adapter_for_pack(BAIJIU_PACK)[0] == PROVIDER
    assert _adapter_for_pack(BANK_PACK) == (PROVIDER, BANK_ADAPTER_VERSION)
    _validate_online_scope(BANK_PACK, PROVIDER, BANK_ADAPTER_VERSION)
    with pytest.raises(ValueError, match="requires provider"):
        _validate_online_scope(BANK_PACK, PROVIDER, "1.0")
    with pytest.raises(ValueError, match="does not support metric pack"):
        _adapter_for_pack("unknown")


def test_bank_snapshot_fields_keep_five_canonical_metrics() -> None:
    captured = datetime(2026, 4, 1, tzinfo=UTC)
    fields = {
        metric: SimpleNamespace(
            value=Decimal("0.125") if metric != "equity_parent" else None,
            unit="fraction" if metric != "equity_parent" else "CNY",
            currency="CNY" if metric == "equity_parent" else None,
            source_field=f"raw.{metric}",
            raw_value="12.5",
            raw_unit="percent",
            missing_reason="unverified_mapping_reconcile_annual_report"
            if metric == "equity_parent"
            else None,
            response_hash="a" * 64,
        )
        for metric in BANK_ANNUAL_METRICS
    }
    collection = SimpleNamespace(
        fields=fields,
        source_rows={},
        report_period=date(2025, 12, 31),
        captured_at=captured,
        company_type=None,
    )
    converted, proofs = build_snapshot_fields(collection, metric_pack=BANK_PACK)
    assert tuple(converted) == BANK_ANNUAL_METRICS
    assert converted["equity_parent"].value is None
    assert converted["equity_parent"].missing_reason == "unverified_mapping_reconcile_annual_report"
    assert converted["npl_ratio"].verification.value == "candidate"
    assert proofs["npl_ratio"]["source_response_sha256"] == "a" * 64


def test_bank_hash_ignores_observation_times_but_keeps_supplier_evidence() -> None:
    from investment_agent.evidence.contracts import Period
    from investment_agent.industry_peers.contracts import PeerAnnualField, PeerFieldVerification

    field = PeerAnnualField(
        metric="npl_ratio",
        value="0.012",
        unit="fraction",
        currency=None,
        period=Period(start=date(2025, 1, 1), end=date(2025, 12, 31), basis="annual_ratio"),
        source_field="NONPERLOAN",
        raw_value="1.2",
        raw_unit="percent",
        acquired_at=datetime(2026, 4, 1, tzinfo=UTC),
        verification=PeerFieldVerification.CANDIDATE,
    )
    proof = {
        "npl_ratio": {
            "source_response_sha256": "a" * 64,
            "supplier_comparison": {"response_hash": "a" * 64, "checked_at": "one"},
        }
    }
    first = _bank_snapshot_hashes({"npl_ratio": field}, proof)
    proof["npl_ratio"]["supplier_comparison"]["checked_at"] = "two"
    second = _bank_snapshot_hashes({"npl_ratio": field}, proof)
    assert first == second
    proof["npl_ratio"]["source_response_sha256"] = "b" * 64
    assert _bank_snapshot_hashes({"npl_ratio": field}, proof) != first


def _bank_response(
    code: str, *, version: str = "4.0", notice: str = "2026-04-25 00:00:00", org="银行", roe=8.62
) -> bytes:
    row = {
        "SECUCODE": f"{code}.SH",
        "REPORT_DATE": "2025-12-31 00:00:00",
        "REPORT_TYPE": "年报",
        "NOTICE_DATE": notice,
        "UPDATE_DATE": "2026-04-30 00:00:00",
        "CURRENCY": "CNY",
        "ORG_TYPE": org,
        "ROEJQ": roe,
        "NET_INTEREST_MARGIN": 2.15,
        "NONPERLOAN": 1.28,
        "BLDKBBL": 373.16,
    }
    return json.dumps(
        {
            "version": version,
            "success": True,
            "result": {"pages": 1, "pageNo": 1, "pageSize": 100, "total": 1, "data": [row]},
        }
    ).encode()


class FakeBankSource:
    def __init__(self, bodies: dict[str, bytes]):
        self.bodies = bodies
        self.fetches: list[str] = []

    async def fetch(self, url, params):
        assert urlsplit(url).path == "/securities/api/data/v1/get"
        code = re.search(r'SECUCODE="(\d{6})\.SH"', params["filter"]).group(1)
        self.fetches.append(code)
        return RawAnnualResponse(status=200, body=self.bodies[code])


async def _bank_cohort(session, tickers=(("600001", "银行一"),), *, online_scope=True):
    entries = []
    listing_repo = ListingRepository(session)
    for code, name in tickers:
        listing = await listing_repo.ensure_a_share_listing(f"{code}.SH", company_name=name)
        entries.append(
            {
                "ticker": f"{code}.SH",
                "company_id": listing.company_id,
                "listing_id": listing.id,
                "name": name,
            }
        )
    payload = {
        "schema": "peer_cohort_snapshot/1",
        "cohort_key": "bank_test",
        "entries": entries,
        "metric_pack": BANK_PACK,
    }
    if online_scope:
        payload["read_scope"] = {
            "provider": PROVIDER,
            "adapter_version": BANK_ADAPTER_VERSION,
        }
    cohort = PeerCohortSnapshot(
        id=str(uuid4()),
        cohort_key="bank_test",
        revision=1,
        observed_on=COLLECT_PERIOD,
        acquired_at=datetime.now(UTC),
        verification="candidate",
        inclusion_rule_version="test-bank-online/1",
        content_hash="b" * 64,
        payload=payload,
    )
    session.add(cohort)
    await session.commit()
    return cohort


async def _enqueue_bank(cohort, data_dir, mode="full"):
    async with session_scope() as session:
        row = await session.get(PeerCohortSnapshot, cohort.id)
        return await enqueue_peer_collect(
            session,
            report_period=COLLECT_PERIOD,
            mode=mode,
            triggered_by="test",
            data_dir=data_dir,
            cohort_row=row,
        )


@pytest.mark.asyncio
async def test_bank_online_roster_binding_is_explicit_and_immutable(peer_db, tmp_path):
    async with session_scope() as session:
        await _bank_cohort(session, (("600001", "银行一"), ("600002", "银行二")))
    source = tmp_path / "bank_sample.json"
    data = {
        "captured_at": "2026-04-01T00:00:00+08:00",
        "bank": ["600001.SH", "600002.SH"],
        "bank_classifications": {
            "600001.SH": {"symbol": "600001"},
            "600002.SH": {"symbol": "600002"},
        },
    }
    source.write_text(json.dumps(data), encoding="utf-8")
    async with session_scope() as session:
        offline = await import_bank_roster(session, sample_path=source, cohort_key="import-test")
        online = await import_bank_roster(
            session, sample_path=source, cohort_key="import-test", online=True
        )
        again = await import_bank_roster(
            session, sample_path=source, cohort_key="import-test", online=True
        )
        assert offline.created and offline.snapshot.revision == 1
        assert "read_scope" not in offline.snapshot.payload
        assert online.created and online.snapshot.revision == 2
        assert online.snapshot.payload["read_scope"] == {
            "provider": PROVIDER,
            "adapter_version": BANK_ADAPTER_VERSION,
        }
        assert not again.created and again.snapshot.id == online.snapshot.id
        with pytest.raises(ValueError, match="only valid with --industry bank"):
            await import_sample_roster(
                session, sample_path=source, industry="baijiu", bank_online=True
            )


async def _run_bank_job(enqueued, data_dir, source):
    from investment_agent.industry_peers.jobs import PeerCollectHandler

    job = await claim_job(enqueued.run.id)
    handler = PeerCollectHandler(
        settings=make_settings(data_dir),
        source=source,
        retry_backoff_seconds=0,
        sleep=lambda _: None,
    )
    return await handler.execute(job, DirectCtx()), job


@pytest.mark.asyncio
async def test_bank_online_job_saves_four_metrics_and_refresh_hashes_versions(peer_db):
    async with session_scope() as session:
        cohort = await _bank_cohort(session)
    source = FakeBankSource({"600001": _bank_response("600001")})
    first = await _enqueue_bank(cohort, peer_db)
    assert first.payload.adapter_version == BANK_ADAPTER_VERSION
    outcome, _ = await _run_bank_job(first, peer_db, source)
    mapping = read_batch_mapping(mapping_artifact_path(peer_db, str(first.run.id)))
    assert outcome == "partial", (
        [gap.model_dump(mode="json") for gap in mapping.manifest.target_gaps],
        [gap.model_dump(mode="json") for gap in mapping.manifest.missing_companies],
        mapping.status,
    )
    assert mapping.status == "partial"
    assert {entry.metric_key for entry in mapping.targets[0].metric_calculation_refs} == {
        "roe",
        "net_interest_margin",
        "npl_ratio",
        "provision_coverage",
    }
    async with session_scope() as session:
        saved = list((await session.scalars(select(PeerAnnualSnapshot))).all())
        assert len(saved) == 1
        assert saved[0].fields["npl_ratio"]["source_version"] == "4.0"
        assert saved[0].verification["equity_parent"]["source_version"] == "4.0"
    same = await _enqueue_bank(cohort, peer_db, mode="refresh")
    assert (await _run_bank_job(same, peer_db, source))[0] == "partial"
    async with session_scope() as session:
        assert await session.scalar(select(func.count()).select_from(PeerAnnualSnapshot)) == 1
    changed = await _enqueue_bank(cohort, peer_db, mode="refresh")
    changed_source = FakeBankSource({"600001": _bank_response("600001", version="4.1", roe=8.73)})
    assert (await _run_bank_job(changed, peer_db, changed_source))[0] == "partial"
    async with session_scope() as session:
        assert await session.scalar(select(func.count()).select_from(PeerAnnualSnapshot)) == 2


@pytest.mark.asyncio
async def test_bank_online_partial_failure_future_notice_and_cache_recovery(peer_db):
    from investment_agent.industry_peers.jobs import PeerCollectHandler
    from investment_agent.storage.repositories.pipeline_run import LeaseLostError

    async with session_scope() as session:
        cohort = await _bank_cohort(session, (("600001", "银行一"), ("600002", "银行二")))
    enqueued = await _enqueue_bank(cohort, peer_db)
    source = FakeBankSource(
        {
            "600001": _bank_response("600001"),
            "600002": _bank_response("600002", notice="2027-04-25 00:00:00"),
        }
    )
    job = await claim_job(enqueued.run.id)
    handler = PeerCollectHandler(
        settings=make_settings(peer_db),
        source=source,
        retry_backoff_seconds=0,
        sleep=lambda _: None,
    )
    # Simulate stale recovery taking ownership after the provider response is
    # read. The write fence must reject the old worker's snapshot transaction.
    async with session_scope() as session:
        await session.execute(
            update(PipelineRun)
            .where(PipelineRun.id == enqueued.run.id)
            .values(worker_id="new-worker")
        )
    with pytest.raises(LeaseLostError):
        await handler.execute(job, DirectCtx())
    async with session_scope() as session:
        assert await session.scalar(select(func.count()).select_from(PeerAnnualSnapshot)) == 0
        await session.execute(
            update(PipelineRun)
            .where(PipelineRun.id == enqueued.run.id)
            .values(
                status="queued",
                worker_id=None,
                claimed_at=None,
                heartbeat_at=None,
                available_at=datetime.now(UTC),
            )
        )
    # Saved bank_main_fina response is replayed; no second provider fetch.
    retry_job = await claim_job(enqueued.run.id, worker_id="retry-worker")
    assert await handler.execute(retry_job, DirectCtx()) == "partial"
    assert source.fetches.count("600001") == 1
    assert source.fetches.count("600002") == 1
    mapping = read_batch_mapping(mapping_artifact_path(peer_db, str(enqueued.run.id)))
    assert mapping.status == "partial"
    index = CollectArtifactStore(peer_db, enqueued.run.id).load_index()
    assert index["companies"]["600002.SH"]["status"] == "failed"


@pytest.mark.asyncio
async def test_bank_admission_requires_online_bound_roster(peer_db):
    async with session_scope() as session:
        legacy = await _bank_cohort(session, online_scope=False)
        legacy_entry = legacy.payload["entries"][0]
        offline_only = await admit_peer_collect(
            session,
            ticker=legacy_entry["ticker"],
            data_dir=peer_db,
            cooldown_seconds=60,
            adapter_version=BANK_ADAPTER_VERSION,
        )
        assert offline_only.outcome == "offline_only"
        assert "--bank-online" in offline_only.detail
        assert (
            await session.scalar(
                select(func.count())
                .select_from(PipelineRun)
                .where(PipelineRun.kind == "industry_peer_collect")
            )
            == 0
        )


@pytest.mark.asyncio
async def test_bank_admission_uses_online_bound_scope(peer_db):
    async with session_scope() as session:
        online = await _bank_cohort(session, online_scope=True)
        entry = online.payload["entries"][0]
        session.add(
            PeerAnnualSnapshot(
                id=str(uuid4()),
                company_id=entry["company_id"],
                listing_id=entry["listing_id"],
                report_period=COLLECT_PERIOD,
                provider=PROVIDER,
                adapter_version="offline-run02-v1",
                captured_at=datetime.now(UTC),
                content_hash="c" * 64,
                semantic_hash="d" * 64,
                fields={},
                verification={},
            )
        )
        await session.flush()
        decision = await admit_peer_collect(
            session,
            ticker=entry["ticker"],
            data_dir=peer_db,
            cooldown_seconds=60,
        )
        assert decision.outcome == "enqueued"
        queued = await PipelineRunRepository(session).get(decision.run_id)
        assert queued is not None
        assert queued.request_json["provider"] == PROVIDER
        assert queued.request_json["adapter_version"] == BANK_ADAPTER_VERSION
