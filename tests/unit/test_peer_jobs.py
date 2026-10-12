"""WP8 ``industry_peer_collect``: trusted enqueue, scope dedup, generations,
per-company checkpoints and resume, lease fencing, partial-failure mapping,
online-data materialization, roster binding and the admission cooldown.

Everything runs offline against the injected fake transport
(:class:`FakeAnnualSource`): the suite never reaches the network, and the
collector's retry/throttle knobs are pinned through the settings.
"""

from __future__ import annotations

import json
import re
import uuid as uuid_module
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from typer.testing import CliRunner

from investment_agent.cli.commands import peer_cohort as peer_cohort_cmd
from investment_agent.config import Settings
from investment_agent.industry_peers.artifacts import (
    PeerBatchMapping,
    mapping_artifact_path,
    mappings_in_scope,
    read_batch_mapping,
    scan_batch_mappings,
    select_current_mapping,
    write_batch_mapping,
)
from investment_agent.industry_peers.jobs import (
    PEER_COLLECT_KIND,
    CollectArtifactStore,
    PeerCollectHandler,
    PeerCollectPayload,
    SavedResponseCache,
    admit_peer_collect,
    enqueue_peer_collect,
    peer_collect_idempotency_key,
    request_identity,
)
from investment_agent.jobs.base import ClaimedJob
from investment_agent.providers.eastmoney_annual import (
    EastmoneyAnnualTransientError,
    PROVIDER,
    RawAnnualResponse,
)
from investment_agent.storage.database import init_engine, session_scope
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.base import Base
from investment_agent.storage.models.evidence import SavedCalculation, SavedEvidence
from investment_agent.storage.models.peer import PeerAnnualSnapshot, PeerCohortSnapshot
from investment_agent.storage.repositories.listing import ListingRepository
from investment_agent.storage.repositories.peer import (
    PeerAnnualSnapshotRepository,
    PeerCohortSnapshotRepository,
)
from investment_agent.storage.repositories.pipeline_run import (
    LeaseLostError,
    PipelineRunRepository,
    RunLease,
)

PERIOD = date(2025, 12, 31)
NOTICE_DATE = "2026-04-25 00:00:00"
UPDATE_DATE = "2026-04-30 00:00:00"
ADAPTER = "1.0"

ROSTER: tuple[tuple[str, str], ...] = (
    ("600001", "目标公司"),
    ("600002", "同行二"),
    ("600003", "同行三"),
    ("600004", "同行四"),
    ("600005", "同行五"),
    ("600006", "同行六"),
    ("600007", "同行七"),
)


# --------------------------------------------------------------------------- #
# Fake transport and answer builders
# --------------------------------------------------------------------------- #


class NoSleep:
    async def __call__(self, seconds: float) -> None:
        self.slept.append(seconds)

    def __init__(self) -> None:
        self.slept: list[float] = []


class DirectCtx:
    """A JobContext stand-in: no heartbeats, work runs inline."""

    worker_id = "test-worker"

    async def run_with_heartbeat(self, lease: RunLease, work: Any) -> Any:
        return await work()


_OPERATION_BY_PATH = {
    "/PC_HSF10/NewFinanceAnalysis/Index": "f10_ctype",
    "/PC_HSF10/NewFinanceAnalysis/lrbAjaxNew": "f10_income",
    "/PC_HSF10/NewFinanceAnalysis/zcfzbAjaxNew": "f10_balance",
    "/PC_HSF10/NewFinanceAnalysis/xjllbAjaxNew": "f10_cashflow",
    "/securities/api/data/v1/get": "main_fina",
}


def _code_of(params: dict[str, str]) -> str:
    match = re.search(r"\d{6}", json.dumps(params))
    assert match is not None, f"no company code in {params}"
    return match.group(0)


class FakeAnnualSource:
    """Answers every operation from a per-company body table; counts fetches.

    ``failures`` maps ``(code, operation)`` to a list of exceptions raised on
    consecutive fetches of that request (transient simulation); each raised
    entry is consumed, so a later attempt of the same request succeeds.
    """

    def __init__(
        self,
        companies: dict[str, dict[str, bytes]],
        *,
        failures: dict[tuple[str, str], list[BaseException]] | None = None,
    ) -> None:
        self.companies = companies
        self.failures = failures or {}
        self.fetches: list[tuple[str, str]] = []

    async def fetch(self, url: str, params: dict[str, str]) -> RawAnnualResponse:
        operation = _OPERATION_BY_PATH[urlsplit(url).path]
        code = _code_of(params)
        self.fetches.append((code, operation))
        pending = self.failures.get((code, operation))
        if pending:
            raise pending.pop(0)
        return RawAnnualResponse(status=200, body=self.companies[code][operation])

    def fetch_counts(self, code: str) -> int:
        return sum(1 for fetched, _ in self.fetches if fetched == code)


def _row(code: str, values: dict[str, Any], *, notice: str = NOTICE_DATE, update: str = UPDATE_DATE):
    row: dict[str, Any] = {
        "SECUCODE": f"{code}.SH",
        "REPORT_DATE": "2025-12-31 00:00:00",
        "NOTICE_DATE": notice,
        "UPDATE_DATE": update,
        "CURRENCY": "CNY",
    }
    row.update(values)
    return row


def _statement_body(row: dict[str, Any]) -> bytes:
    return json.dumps({"version": "4.0", "data": [row], "success": True}).encode("utf-8")


def _main_body(row: dict[str, Any]) -> bytes:
    return json.dumps(
        {
            "version": "4.0",
            "success": True,
            "result": {"pages": 1, "pageNo": 1, "pageSize": 100, "total": 1, "data": [row]},
        }
    ).encode("utf-8")


def _ctype_page(ctype: str | None = "4") -> bytes:
    if ctype is None:
        return b"<!DOCTYPE html><html><body><p>no metadata</p></body></html>"
    return (
        "<!DOCTYPE html><html><head></head><body>"
        f'<input type="hidden" id="hidctype" value="{ctype}" />'
        "</body></html>"
    ).encode()


def company_bodies(code: str, *, ctype: str | None = "4") -> dict[str, bytes]:
    """The five happy answers of one company (ctype page, three tables, main)."""

    income = _row(
        code,
        {
            "OPERATE_INCOME": 25731010647.32,
            "TOTAL_OPERATE_INCOME": 25898434324.18,
            "PARENT_NETPROFIT": 86228146421.62,
        },
    )
    balance = _row(
        code,
        {"TOTAL_PARENT_EQUITY": 200456087654.32, "CONTRACT_LIAB": 32338664843.91},
    )
    cash = _row(code, {"NETCASH_OPERATE": 92435296907.06})
    main = _row(code, {"ROEJQ": 8.62})
    return {
        "f10_ctype": _ctype_page(ctype),
        "f10_income": _statement_body(income),
        "f10_balance": _statement_body(balance),
        "f10_cashflow": _statement_body(cash),
        "main_fina": _main_body(main),
    }


def happy_companies(codes: list[str]) -> dict[str, dict[str, bytes]]:
    return {code: company_bodies(code) for code in codes}


# --------------------------------------------------------------------------- #
# Fixtures and helpers
# --------------------------------------------------------------------------- #


def make_settings(tmp_path: Path, *, retries: int = 2) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        api_key="test",
        llm_tier3_api_key="test",
        data_dir=tmp_path,
        peer_collect_min_interval_seconds=0,
        peer_collect_retries=retries,
    )


async def seed_roster(
    session: Any,
    tickers: tuple[tuple[str, str], ...] = ROSTER,
    *,
    cohort_key: str = "baijiu_current",
    revision: int | None = None,
) -> PeerCohortSnapshot:
    """Directory listings plus one roster version over the given members."""

    repo = ListingRepository(session)
    entries = []
    for code, name in tickers:
        listing = await repo.ensure_a_share_listing(f"{code}.SH", company_name=name)
        entries.append(
            {
                "ticker": f"{code}.SH",
                "company_id": listing.company_id,
                "listing_id": listing.id,
                "name": name,
            }
        )
    cohort_repo = PeerCohortSnapshotRepository(session)
    return await cohort_repo.add(
        PeerCohortSnapshot(
            id=str(uuid_module.uuid4()),
            cohort_key=cohort_key,
            revision=(
                revision
                if revision is not None
                else await cohort_repo.next_revision(cohort_key)
            ),
            observed_on=PERIOD,
            acquired_at=datetime.now(UTC),
            verification="candidate",
            inclusion_rule_version="test/1",
            content_hash="c" * 64,
            payload={"schema": "test-roster/1", "entries": entries},
        )
    )


async def seed_collection_run(
    cohort: PeerCohortSnapshot,
    tmp_path: Path,
    *,
    mode: str = "full",
    provider: str = PROVIDER,
    adapter_version: str = ADAPTER,
    report_period: date = PERIOD,
) -> Any:
    async with session_scope() as session:
        return await enqueue_peer_collect(
            session,
            report_period=report_period,
            mode=mode,  # type: ignore[arg-type]
            triggered_by="test",
            data_dir=tmp_path,
            cohort_row=cohort,
            provider=provider,
            adapter_version=adapter_version,
        )


async def claim_job(run_id: int, worker_id: str = "test-worker") -> ClaimedJob:
    async with session_scope() as session:
        claimed = await PipelineRunRepository(session).claim_next(
            worker_id=worker_id, kinds=[PEER_COLLECT_KIND], run_id=run_id
        )
        assert claimed is not None
        return ClaimedJob(
            run_id=claimed.id,
            kind=claimed.kind,
            lease=RunLease(
                run_id=claimed.id, worker_id=worker_id, attempt=claimed.attempt_count
            ),
            request_json=dict(claimed.request_json),
        )


def make_handler(settings: Settings, source: FakeAnnualSource) -> PeerCollectHandler:
    return PeerCollectHandler(
        settings=settings,
        source=source,
        retry_backoff_seconds=0.0,
        sleep=NoSleep(),
    )


async def run_row(run_id: int) -> PipelineRun:
    async with session_scope() as session:
        row = await PipelineRunRepository(session).get(run_id)
        assert row is not None
        session.expunge(row)
        return row


async def read_mapping(tmp_path: Path, run_id: int) -> PeerBatchMapping:
    return read_batch_mapping(mapping_artifact_path(tmp_path, str(run_id)))


def collect_index(tmp_path: Path, run_id: int) -> dict[str, Any]:
    return CollectArtifactStore(tmp_path, run_id).load_index()


async def run_count() -> int:
    async with session_scope() as session:
        return int(
            await session.scalar(
                select(func.count()).select_from(PipelineRun).where(
                    PipelineRun.kind == PEER_COLLECT_KIND
                )
            )
        )


async def roster_entry(cohort: PeerCohortSnapshot, ticker: str) -> dict[str, Any]:
    return next(e for e in cohort.payload["entries"] if e["ticker"] == ticker)


@pytest.fixture
async def peer_db(db_session, tmp_path):
    """The shared in-memory engine plus the test's data directory."""
    del db_session  # initializes the engine and the schema
    return tmp_path


@pytest.fixture
async def seeded_roster(peer_db):
    async with session_scope() as session:
        cohort = await seed_roster(session)
        await session.commit()
    return cohort


# --------------------------------------------------------------------------- #
# Payload and idempotency key
# --------------------------------------------------------------------------- #


def test_payload_rejects_non_annual_period_and_bad_generation() -> None:
    with pytest.raises(ValueError, match="12-31"):
        PeerCollectPayload(
            cohort_snapshot_id="a" * 32,
            cohort_revision=1,
            report_period=date(2025, 6, 30),
            provider=PROVIDER,
            adapter_version=ADAPTER,
            generation=1,
        )
    with pytest.raises(ValueError):
        PeerCollectPayload(
            cohort_snapshot_id="a" * 32,
            cohort_revision=0,
            report_period=PERIOD,
            provider=PROVIDER,
            adapter_version=ADAPTER,
            generation=1,
        )


def test_idempotency_key_covers_scope_and_generation_canonically() -> None:
    base = PeerCollectPayload(
        cohort_snapshot_id="a" * 32,
        cohort_revision=2,
        report_period=PERIOD,
        provider=PROVIDER,
        adapter_version=ADAPTER,
        mode="full",
        generation=3,
    )
    reordered = PeerCollectPayload(
        adapter_version=ADAPTER,
        generation=3,
        provider=PROVIDER,
        report_period=PERIOD,
        cohort_revision=2,
        cohort_snapshot_id="a" * 32,
        mode="full",
    )
    key = peer_collect_idempotency_key(base)
    assert key == peer_collect_idempotency_key(reordered)
    assert len(key) <= 128
    assert key.startswith(f"{PEER_COLLECT_KIND}:g3:")
    variants = [
        base.model_copy(update={"generation": 4}),
        base.model_copy(update={"report_period": date(2024, 12, 31)}),
        base.model_copy(update={"cohort_revision": 3}),
        base.model_copy(update={"provider": "rival"}),
    ]
    assert len({key, *(peer_collect_idempotency_key(item) for item in variants)}) == 5


def test_request_identity_and_response_store_roundtrip(peer_db) -> None:
    assert request_identity("f10_income", {"a": "1", "b": "2"}) == request_identity(
        "f10_income", {"b": "2", "a": "1"}
    )
    store = CollectArtifactStore(peer_db, run_id=99)
    assert store.load_response("f10_income", {"a": "1"}) is None
    store.save_response("f10_income", {"a": "1"}, b"body")
    assert SavedResponseCache(store).saved_response("f10_income", {"a": "1"}) == b"body"
    # A mutated body is refused (refetched), never parsed.
    body_file = next((store.root / "responses").glob("*.body"))
    body_file.write_bytes(b"tampered")
    assert store.load_response("f10_income", {"a": "1"}) is None


# --------------------------------------------------------------------------- #
# Enqueue: scope dedup, unique constraint, generations
# --------------------------------------------------------------------------- #


async def test_enqueue_reuses_active_scope_run_and_blocks_concurrent_duplicate(
    seeded_roster, peer_db
) -> None:
    first = await seed_collection_run(seeded_roster, peer_db)
    assert first.created
    assert first.generation == 1
    assert first.payload.mode == "full"
    second = await seed_collection_run(seeded_roster, peer_db)
    assert not second.created and second.scope_reused
    assert second.run.id == first.run.id

    # A concurrent enqueue that loses the (kind, idempotency_key) race is
    # blocked by the unique constraint - exactly one row survives.
    key = peer_collect_idempotency_key(first.payload)
    with pytest.raises(IntegrityError):
        async with session_scope() as session:
            await PipelineRunRepository(session).create(
                kind=PEER_COLLECT_KIND,
                idempotency_key=key,
                triggered_by="race",
                request_json={},
            )
    async with session_scope() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(PipelineRun)
            .where(PipelineRun.idempotency_key == key)
        )
        assert count == 1


async def test_generation_advances_over_finished_batches_and_failed_runs(
    seeded_roster, peer_db
) -> None:
    first = await seed_collection_run(seeded_roster, peer_db)
    job = await claim_job(first.run.id)
    codes = [code for code, _ in ROSTER]
    assert await make_handler(make_settings(peer_db), FakeAnnualSource(happy_companies(codes))).execute(
        job, DirectCtx()
    ) == "completed"

    # A succeeded batch does not block a refresh: the explicit retry derives
    # the next generation from the submitted mapping.
    retry = await seed_collection_run(seeded_roster, peer_db, mode="refresh")
    assert retry.created and retry.generation == 2
    assert retry.payload.mode == "refresh"

    async with session_scope() as session:
        await PipelineRunRepository(session).mark_failed(
            retry.run.id, "terminal failure for the test"
        )
    after_failure = await seed_collection_run(seeded_roster, peer_db, mode="refresh")
    # A failed run without a mapping must not pin the idempotency key.
    assert after_failure.created and after_failure.generation == 3


async def test_missing_roster_fails_with_import_hint(peer_db) -> None:
    async with session_scope() as session:
        with pytest.raises(ValueError, match="import-roster"):
            await enqueue_peer_collect(
                session,
                report_period=PERIOD,
                mode="full",
                triggered_by="test",
                data_dir=peer_db,
                cohort_key="missing_cohort",
            )


# --------------------------------------------------------------------------- #
# Happy path: online facts materialize (dated), mapping success
# --------------------------------------------------------------------------- #


async def test_online_collection_saves_dated_snapshots_and_cited_mapping(
    seeded_roster, peer_db
) -> None:
    enqueued = await seed_collection_run(seeded_roster, peer_db)
    job = await claim_job(enqueued.run.id)
    codes = [code for code, _ in ROSTER]

    outcome = await make_handler(
        make_settings(peer_db), FakeAnnualSource(happy_companies(codes))
    ).execute(job, DirectCtx())

    assert outcome == "completed"
    run = await run_row(enqueued.run.id)
    assert run.status == "success"
    mapping = await read_mapping(peer_db, enqueued.run.id)
    assert mapping.status == "success"
    assert mapping.generation == 1
    assert str(mapping.cohort_ref.id) == seeded_roster.id
    assert mapping.cohort_ref.revision == seeded_roster.revision
    assert len(mapping.targets) == len(ROSTER)

    # Online facts carry NOTICE_DATE / UPDATE_DATE into the field metadata,
    # which is what makes them materializable (WP4 date gate).
    async with session_scope() as session:
        rows = list(
            (
                await session.execute(
                    select(PeerAnnualSnapshot).where(PeerAnnualSnapshot.provider == PROVIDER)
                )
            ).scalars()
        )
        assert len(rows) == len(ROSTER)
        for row in rows:
            for field in row.fields.values():
                assert field["disclosed_on"] == "2026-04-25"
                assert field["updated_on"] == "2026-04-30"
                assert field["verification"] == "candidate"
            assert row.verification["operating_revenue"]["source_response_sha256"]

        # Unlike the run-02 offline batches, the mapped records cite evidence.
        target = next(t for t in mapping.targets if t.ticker == "600001.SH")
        for entry in target.metric_calculation_refs:
            assert entry.ref is not None
            saved = await session.get(SavedCalculation, (str(entry.ref.id), entry.ref.revision))
            assert saved is not None
            assert len(saved.payload["fact_refs"]) > 0
            for ref in saved.payload["fact_refs"]:
                evidence = await session.get(SavedEvidence, (str(ref["id"]), ref["revision"]))
                assert evidence is not None


# --------------------------------------------------------------------------- #
# Checkpoints: resume skips finished companies, no re-sent requests
# --------------------------------------------------------------------------- #


async def test_crash_resumes_from_checkpoint_without_refetching(seeded_roster, peer_db) -> None:
    settings = make_settings(peer_db, retries=0)
    enqueued = await seed_collection_run(seeded_roster, peer_db)
    codes = [code for code, _ in ROSTER]

    flaky = FakeAnnualSource(
        happy_companies(codes),
        failures={
            # ctype, income and balance succeed (their bodies are saved);
            # the cashflow transport dies after its retries are spent.
            ("600002", "f10_cashflow"): [
                EastmoneyAnnualTransientError("network down", provider=PROVIDER)
            ]
        },
    )
    job = await claim_job(enqueued.run.id)
    with pytest.raises(EastmoneyAnnualTransientError):
        await make_handler(settings, flaky).execute(job, DirectCtx())

    index = collect_index(peer_db, enqueued.run.id)
    assert index["companies"]["600001.SH"]["status"] == "collected"
    assert index["companies"]["600002.SH"]["status"] == "pending"
    assert flaky.fetch_counts("600001") == 5
    assert flaky.fetch_counts("600002") == 4

    # A new attempt (fresh transport) resumes: the finished company is never
    # re-fetched, and the interrupted company's succeeded requests (ctype,
    # income, balance) are served from the run's saved responses.
    fresh = FakeAnnualSource(happy_companies(codes))
    outcome = await make_handler(settings, fresh).execute(job, DirectCtx())
    assert outcome == "completed"
    assert fresh.fetch_counts("600001") == 0
    assert fresh.fetch_counts("600002") == 2  # only cashflow + main are fetched
    run = await run_row(enqueued.run.id)
    assert run.status == "success"

    async with session_scope() as session:
        count = await session.scalar(select(func.count()).select_from(PeerAnnualSnapshot))
        assert count == len(ROSTER)


async def test_identical_refresh_reuses_batch_snapshots_and_records(
    seeded_roster, peer_db
) -> None:
    settings = make_settings(peer_db)
    codes = [code for code, _ in ROSTER]
    first = await seed_collection_run(seeded_roster, peer_db)
    job = await claim_job(first.run.id)
    assert await make_handler(settings, FakeAnnualSource(happy_companies(codes))).execute(
        job, DirectCtx()
    ) == "completed"
    first_mapping = await read_mapping(peer_db, first.run.id)

    async with session_scope() as session:
        calculations_before = await session.scalar(
            select(func.count()).select_from(SavedCalculation)
        )

    retry = await seed_collection_run(seeded_roster, peer_db, mode="refresh")
    assert retry.created and retry.generation == 2
    retry_job = await claim_job(retry.run.id)
    outcome = await make_handler(settings, FakeAnnualSource(happy_companies(codes))).execute(
        retry_job, DirectCtx()
    )
    assert outcome == "completed"
    run = await run_row(retry.run.id)
    metadata = run.metadata_json["peer_collect"]
    # Identical inputs: the submitted mapping is reused, no new generation.
    assert metadata["mapping"]["created"] is False
    assert metadata["mapping"]["batch_run_id"] == first_mapping.batch_run_id
    assert metadata["mapping"]["generation"] == 1
    current = select_current_mapping(
        mappings_in_scope(
            scan_batch_mappings(peer_db).valid,
            cohort_ref=first_mapping.cohort_ref,
            report_period=PERIOD,
            provider=PROVIDER,
            adapter_version=ADAPTER,
        )
    )
    assert current is not None and current.generation == 1

    async with session_scope() as session:
        # Semantic-hash dedup: no second snapshot version, no new records.
        assert await session.scalar(select(func.count()).select_from(PeerAnnualSnapshot)) == len(
            ROSTER
        )
        assert await session.scalar(select(func.count()).select_from(SavedCalculation)) == (
            calculations_before
        )


# --------------------------------------------------------------------------- #
# Lease fencing: a worker that lost its claim cannot write
# --------------------------------------------------------------------------- #


async def test_lost_lease_blocks_fact_writes_and_checkpoint(seeded_roster, peer_db) -> None:
    enqueued = await seed_collection_run(seeded_roster, peer_db)
    job = await claim_job(enqueued.run.id, worker_id="old-worker")
    assert job.lease.attempt == 1
    # The claim is handed over (stale recovery reclaims it for another worker).
    async with session_scope() as session:
        await session.execute(
            update(PipelineRun)
            .where(PipelineRun.id == enqueued.run.id)
            .values(worker_id="new-worker", attempt_count=2)
        )

    source = FakeAnnualSource(happy_companies([code for code, _ in ROSTER]))
    with pytest.raises(LeaseLostError):
        await make_handler(make_settings(peer_db), source).execute(job, DirectCtx())

    async with session_scope() as session:
        assert await session.scalar(select(func.count()).select_from(PeerAnnualSnapshot)) == 0
    assert not collect_index(peer_db, enqueued.run.id).get("companies")


# --------------------------------------------------------------------------- #
# Partial failure: one company fails, the group stays honest
# --------------------------------------------------------------------------- #


async def test_company_failure_marks_mapping_partial_with_reasons(
    seeded_roster, peer_db
) -> None:
    enqueued = await seed_collection_run(seeded_roster, peer_db)
    job = await claim_job(enqueued.run.id)
    companies = happy_companies([code for code, _ in ROSTER])
    companies["600002"] = {
        **companies["600002"],
        "f10_ctype": _ctype_page(None),  # company type cannot be confirmed
    }
    source = FakeAnnualSource(companies)

    outcome = await make_handler(make_settings(peer_db), source).execute(job, DirectCtx())

    assert outcome == "partial"
    run = await run_row(enqueued.run.id)
    assert run.status == "partial"
    mapping = await read_mapping(peer_db, enqueued.run.id)
    assert mapping.status == "partial"
    failed = [gap for gap in mapping.manifest.target_gaps if gap.ticker == "600002.SH"]
    assert failed and "company type" in failed[0].reason
    assert any(item.ticker == "600002.SH" for item in mapping.manifest.missing_companies)
    built = {target.ticker for target in mapping.targets}
    assert "600001.SH" in built and "600003.SH" in built and "600002.SH" not in built
    checkpoint = run.metadata_json["peer_collect"]["checkpoint"]["600002.SH"]
    assert checkpoint["status"] == "failed"
    assert "CompanyTypeUnknownError" in checkpoint["reason"]

    # A rerun of the same run skips the failed company (checkpointed as
    # terminal for this batch) and does not re-collect the finished ones.
    async with session_scope() as session:
        await session.execute(
            update(PipelineRun)
            .where(PipelineRun.id == enqueued.run.id)
            .values(
                status="queued",
                stage="queued",
                worker_id=None,
                claimed_at=None,
                finished_at=None,
            )
        )
    retry_source = FakeAnnualSource(companies)
    rerun = await claim_job(enqueued.run.id, worker_id="retry-worker")
    assert await make_handler(make_settings(peer_db), retry_source).execute(
        rerun, DirectCtx()
    ) == "partial"
    assert retry_source.fetch_counts("600002") == 0
    assert retry_source.fetch_counts("600001") == 0


# --------------------------------------------------------------------------- #
# Frozen batch: a roster change mid-task never enters the running batch
# --------------------------------------------------------------------------- #


async def test_roster_change_after_enqueue_does_not_enter_the_batch(peer_db) -> None:
    async with session_scope() as session:
        cohort = await seed_roster(session, ROSTER[:3])
        await session.commit()
    enqueued = await seed_collection_run(cohort, peer_db)
    assert enqueued.payload.cohort_revision == cohort.revision

    # The roster grows while the task is queued: a new revision is appended.
    async with session_scope() as session:
        await seed_roster(
            session, (*ROSTER[:3], ("600008", "编外公司")), revision=cohort.revision + 1
        )
        await session.commit()

    job = await claim_job(enqueued.run.id)
    source = FakeAnnualSource(happy_companies([code for code, _ in ROSTER[:3]]))
    outcome = await make_handler(make_settings(peer_db), source).execute(job, DirectCtx())

    assert outcome == "completed"
    mapping = await read_mapping(peer_db, enqueued.run.id)
    assert mapping.cohort_ref.revision == cohort.revision
    assert {target.ticker for target in mapping.targets} == {
        "600001.SH",
        "600002.SH",
        "600003.SH",
    }
    assert all(fetched != "600008" for fetched, _ in source.fetches)


# --------------------------------------------------------------------------- #
# Generation semantics: an old batch finishing late never covers a newer one
# --------------------------------------------------------------------------- #


def test_old_generation_submitted_late_never_covers_newer_selection(peer_db) -> None:
    cohort_ref = PeerCollectPayload(
        cohort_snapshot_id="a" * 32,
        cohort_revision=1,
        report_period=PERIOD,
        provider=PROVIDER,
        adapter_version=ADAPTER,
        generation=1,
    ).cohort_ref()

    def mapping(generation: int, submitted_at: datetime) -> PeerBatchMapping:
        return PeerBatchMapping.create(
            batch_run_id=f"peer-offline-gen{generation}",
            cohort_ref=cohort_ref,
            cohort_content_hash="c" * 64,
            report_period=PERIOD,
            provider=PROVIDER,
            adapter_version=ADAPTER,
            generation=generation,
            status="success",
            submitted_at=submitted_at,
        )

    write_batch_mapping(peer_db, mapping(2, datetime(2026, 10, 1, tzinfo=UTC)))
    write_batch_mapping(peer_db, mapping(1, datetime(2026, 10, 11, tzinfo=UTC)))

    scope = mappings_in_scope(
        scan_batch_mappings(peer_db).valid,
        cohort_ref=cohort_ref,
        report_period=PERIOD,
        provider=PROVIDER,
        adapter_version=ADAPTER,
    )
    current = select_current_mapping(scope)
    assert current is not None and current.generation == 2


# --------------------------------------------------------------------------- #
# Admission: first gap, pending, cooldown, stale bypass
# --------------------------------------------------------------------------- #


async def seed_offline_reference(session: Any, listing_id: int, company_id: int) -> None:
    """One run-02-scope snapshot so the target year is locally determinable."""

    from investment_agent.evidence.contracts import Period

    acquired = datetime(2026, 9, 1, tzinfo=UTC)
    await PeerAnnualSnapshotRepository(session).add(
        PeerAnnualSnapshot(
            id="f" * 8 + "-0000-4000-8000-000000000001",
            company_id=company_id,
            listing_id=listing_id,
            report_period=PERIOD,
            provider=PROVIDER,
            adapter_version="offline-run02-v1",
            captured_at=acquired,
            content_hash="d" * 64,
            semantic_hash="e" * 64,
            fields={
                "operating_revenue": {
                    "metric": "operating_revenue",
                    # Same value the online adapter collects, so the two
                    # versions agree and no re-check state is manufactured.
                    "value": "25731010647.32",
                    "unit": "CNY",
                    "currency": "CNY",
                    "period": Period(
                        start=date(2025, 1, 1), end=PERIOD, basis="period_to_date/annual"
                    ).model_dump(mode="json"),
                    "source_field": "OPERATE_INCOME",
                    "acquired_at": acquired.isoformat(),
                    "verification": "verified",
                }
            },
            verification={},
        )
    )


async def admit(ticker: str, tmp_path: Path, at: datetime, cooldown: float = 86400.0):
    async with session_scope() as session:
        return await admit_peer_collect(
            session,
            ticker=ticker,
            data_dir=tmp_path,
            cooldown_seconds=cooldown,
            now=at,
        )


async def test_admission_first_gap_pending_cooldown_then_reenqueue(
    seeded_roster, peer_db
) -> None:
    entry = await roster_entry(seeded_roster, "600001.SH")
    async with session_scope() as session:
        await seed_offline_reference(session, int(entry["listing_id"]), int(entry["company_id"]))
        await session.commit()

    now = datetime.now(UTC)
    first = await admit("600001.SH", peer_db, now)
    assert first.outcome == "enqueued"
    assert first.generation == 1 and first.run_id is not None

    pending = await admit("600001.SH", peer_db, now)
    assert pending.outcome == "pending"
    assert pending.run_id == first.run_id

    # Run the task; one company fails, so the submitted mapping keeps gaps.
    job = await claim_job(first.run_id)
    companies = happy_companies([code for code, _ in ROSTER])
    companies["600002"] = {**companies["600002"], "f10_ctype": _ctype_page(None)}
    assert await make_handler(make_settings(peer_db), FakeAnnualSource(companies)).execute(
        job, DirectCtx()
    ) == "partial"
    finished = await run_row(first.run_id)
    assert finished.status == "partial"
    base = finished.finished_at or datetime.now(UTC)
    if base.tzinfo is None:
        base = base.replace(tzinfo=UTC)

    # Inside the cooldown: the original gaps are returned, nothing enqueued.
    runs_before = await run_count()
    within = await admit("600001.SH", peer_db, base + timedelta(hours=1))
    assert within.outcome == "cooldown"
    assert within.gaps
    assert await run_count() == runs_before

    # After the cooldown the gap is admitted again, as the next generation.
    later = await admit("600001.SH", peer_db, base + timedelta(hours=25))
    assert later.outcome == "enqueued"
    assert later.generation == 2


async def test_admission_stale_bypasses_cooldown(seeded_roster, peer_db) -> None:
    codes = [code for code, _ in ROSTER]
    enqueued = await seed_collection_run(seeded_roster, peer_db)
    job = await claim_job(enqueued.run.id)
    assert await make_handler(make_settings(peer_db), FakeAnnualSource(happy_companies(codes))).execute(
        job, DirectCtx()
    ) == "completed"

    now = datetime.now(UTC)
    covered = await admit("600001.SH", peer_db, now + timedelta(minutes=1))
    assert covered.outcome == "not_needed"

    # A supplier update for one covered field: the displayed result awaits
    # re-check, and the admission bypasses the cooldown immediately.
    entry = await roster_entry(seeded_roster, "600001.SH")
    async with session_scope() as session:
        await PeerAnnualSnapshotRepository(session).add(
            PeerAnnualSnapshot(
                id=str(uuid_module.uuid4()),
                company_id=int(entry["company_id"]),
                listing_id=int(entry["listing_id"]),
                report_period=PERIOD,
                provider=PROVIDER,
                adapter_version=ADAPTER,
                captured_at=datetime.now(UTC),
                content_hash="9" * 64,
                semantic_hash="8" * 64,
                fields={
                    "operating_revenue": {
                        "metric": "operating_revenue",
                        "value": "999",
                        "unit": "CNY",
                        "currency": "CNY",
                        "period": {
                            "start": "2025-01-01",
                            "end": "2025-12-31",
                            "basis": "period_to_date/annual",
                        },
                        "source_field": "OPERATE_INCOME",
                        "disclosed_on": "2026-05-20",
                        "acquired_at": datetime.now(UTC).isoformat(),
                        "verification": "candidate",
                    }
                },
                verification={},
            )
        )
        await session.commit()

    stale = await admit("600001.SH", peer_db, now + timedelta(minutes=2))
    assert stale.outcome == "enqueued"
    assert stale.generation == 2
    assert stale.detail and "stale" in stale.detail


# --------------------------------------------------------------------------- #
# CLI command forms (management entry points)
# --------------------------------------------------------------------------- #


@pytest.fixture
def cli_db(tmp_path, monkeypatch):
    """File-backed SQLite + CLI env, so the command's own engine sees it."""

    url = f"sqlite+aiosqlite:///{tmp_path / 'peers-cli.db'}"
    monkeypatch.setenv("API_KEY", "secret")
    monkeypatch.setenv("LLM_TIER3_API_KEY", "x")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", url)

    async def create() -> None:
        engine = init_engine(url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await engine.dispose()

    import asyncio

    asyncio.run(create())
    return url


def test_cli_collect_without_roster_fails_with_hint(cli_db) -> None:
    result = CliRunner().invoke(
        peer_cohort_cmd.app, ["collect", "--report-period", "2025-12-31"]
    )
    assert result.exit_code == 1
    assert "import-roster" in result.output


def test_cli_collect_rejects_non_annual_period(cli_db) -> None:
    result = CliRunner().invoke(
        peer_cohort_cmd.app, ["collect", "--report-period", "2025-06-30"]
    )
    assert result.exit_code == 1
    assert "Dec 31" in result.output


def test_cli_admit_reports_uncovered_target(cli_db) -> None:
    async def seed() -> None:
        async with session_scope() as session:
            await ListingRepository(session).ensure_a_share_listing(
                "600519.SH", company_name="贵州茅台"
            )
    import asyncio

    asyncio.run(seed())
    result = CliRunner().invoke(peer_cohort_cmd.app, ["admit", "600519.SH"])
    assert result.exit_code == 0
    assert json.loads(result.output)["outcome"] == "no_roster"
