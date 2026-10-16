"""Admission and leased account-fit handler tests on isolated SQLite."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select, update

from investment_agent.config import Settings
from investment_agent.investor.common import ObjectRef
from investment_agent.investor.foundations import InvestorError
from investment_agent.jobs.base import ClaimedJob
from investment_agent.portfolio.suitability import AccountFitService
from investment_agent.portfolio.suitability_calculations import FitItemEvaluationError
from investment_agent.portfolio.suitability_contracts import (
    AccountFitRequest,
    FitRequestItem,
)
from investment_agent.portfolio.suitability_jobs import (
    ACCOUNT_FIT_JOB,
    AccountFitAdmission,
    AccountFitHandler,
    AdmissionReceipt,
    validated_batch_frozen_inputs,
)
from investment_agent.research.worker import ResearchWorker
from investment_agent.settings.loader import ensure_imported
from investment_agent.settings.store import SettingsStore
from investment_agent.storage.database import dispose_engine, init_engine, session_scope
from investment_agent.storage.models import Base
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.evidence import SavedCalculation
from investment_agent.storage.models.investment_assessment import InvestmentAssessment
from investment_agent.storage.repositories.pipeline_run import (
    LeaseLostError,
    PipelineRunRepository,
    RunLease,
)
from tests.support.account_fit import assumptions
from tests.support.account_fit_db import make_inputs
from tests.support.account_fit_flow import persist_inputs


@pytest_asyncio.fixture
async def account_fit_job_world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'account_fit_jobs.sqlite'}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    engine = init_engine(database_url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with session_scope() as session:
        await persist_inputs(session, make_inputs(extra_listings=(600001,)))
    yield tmp_path
    await dispose_engine()


def settings(path: Path, *, enabled: bool = True) -> Settings:
    return Settings(
        _env_file=None,
        api_key="test",
        llm_tier3_api_key="test",
        data_dir=path,
        portfolio_ledger_enabled=enabled,
        account_fit_enabled=enabled,
        candidate_ranking_enabled=False,
    )


async def valid_request(*, operation_id=None, listing_ids=(600000,), fee="10") -> AccountFitRequest:
    from investment_agent.portfolio.suitability_inputs import SuitabilityInputService

    async with session_scope() as session:
        context = await SuitabilityInputService(session).context(make_inputs().account_id)
    assert context.snapshot is not None and context.mandate is not None
    assert context.mandate_ref is not None
    return AccountFitRequest(
        operation_id=operation_id or uuid4(),
        account_id=context.account.id,
        snapshot_ref=context.snapshot.reference,
        mandate_ref=context.mandate_ref,
        valuation_binding=context.valuation_binding,
        purpose_binding=context.purpose_binding,
        items=tuple(
            FitRequestItem(
                listing_ref=ObjectRef(kind="listing", id=listing_id, revision=1),
                action="open",
                trade_assumptions=assumptions(fixed_fee=fee),
            )
            for listing_id in listing_ids
        ),
    )


async def admit(request_body: AccountFitRequest, base: Settings) -> AdmissionReceipt:
    async with session_scope() as session:
        return await AccountFitAdmission(session, settings=base).accept(request_body)


async def fetch_run(run_id: int) -> PipelineRun:
    async with session_scope() as session:
        row = await session.get(PipelineRun, run_id)
        assert row is not None
        session.expunge(row)
        return row


async def artifact_counts() -> tuple[int, int]:
    async with session_scope() as session:
        fit_count = int(
            await session.scalar(select(func.count()).select_from(InvestmentAssessment)) or 0
        )
        calculation_count = int(
            await session.scalar(select(func.count()).select_from(SavedCalculation)) or 0
        )
        return fit_count, calculation_count


async def claim(run_id: int, worker_id: str = "account-fit-test") -> ClaimedJob:
    async with session_scope() as session:
        row = await PipelineRunRepository(session).claim_next(
            worker_id=worker_id, kinds=[ACCOUNT_FIT_JOB], run_id=run_id
        )
        assert row is not None
        return ClaimedJob(
            run_id=row.id,
            kind=row.kind,
            lease=RunLease(row.id, worker_id, row.attempt_count),
            request_json=dict(row.request_json),
        )


class DirectContext:
    worker_id = "account-fit-direct"

    async def run_with_heartbeat(self, lease: RunLease, work):
        return await work()


async def _run_worker(run_id: int, base: Settings) -> None:
    handler = AccountFitHandler(settings=base)
    worker = ResearchWorker(settings=base, worker_id="account-fit-worker", handlers=[handler])
    await worker.run_once(run_id=run_id)


@pytest.mark.asyncio
async def test_admission_replays_same_body_and_rejects_changed_body(
    account_fit_job_world: Path,
) -> None:
    base = settings(account_fit_job_world)
    body = await valid_request()
    first = await admit(body, base)
    replay = await admit(body, base)
    assert replay.replayed
    assert replay.run_ref == first.run_ref
    assert replay.request_hash == first.request_hash

    changed_item = body.items[0].model_copy(
        update={"trade_assumptions": assumptions(fixed_fee="20")}
    )
    changed_body = body.model_copy(update={"items": (changed_item,)})
    with pytest.raises(InvestorError) as conflict:
        await admit(changed_body, base)
    assert conflict.value.detail.code == "idempotency_conflict"


@pytest.mark.asyncio
async def test_disabled_admission_creates_no_run_or_receipt(account_fit_job_world: Path) -> None:
    body = await valid_request()
    with pytest.raises(InvestorError) as disabled:
        await admit(body, settings(account_fit_job_world, enabled=False))
    assert disabled.value.detail.code == "writes_disabled"
    async with session_scope() as session:
        assert await session.scalar(select(func.count()).select_from(PipelineRun)) == 0


@pytest.mark.asyncio
async def test_unknown_financial_result_is_successful_and_persisted(
    account_fit_job_world: Path,
) -> None:
    base = settings(account_fit_job_world)
    receipt = await admit(await valid_request(), base)
    await _run_worker(int(receipt.run_ref.id), base)
    run = await fetch_run(int(receipt.run_ref.id))
    assert run.status == "success"
    assert run.metadata_json["completed"]
    assert not run.metadata_json["failed_items"]
    async with session_scope() as session:
        row = await session.scalar(select(InvestmentAssessment))
        assert row is not None
        assert row.payload["financial_status"] == "unknown"
        assert row.payload["ranking_bucket"] == "incomplete"


@pytest.mark.asyncio
async def test_worker_rejects_mutated_frozen_batch_with_stale_hash(
    account_fit_job_world: Path,
) -> None:
    base = settings(account_fit_job_world)
    receipt = await admit(await valid_request(), base)
    run_id = int(receipt.run_ref.id)
    async with session_scope() as session:
        row = await session.get(PipelineRun, run_id)
        assert row is not None
        payload = dict(row.request_json)
        frozen = dict(payload["frozen_inputs"])
        assert frozen["manifest_hash"] == frozen["source_fingerprint"]
        # This remains a structurally valid FrozenAccountInputs payload, but its
        # batch contents no longer match the immutable admission fingerprint.
        snapshot = dict(frozen["snapshot"])
        snapshot["known_cash"] = "999999"
        frozen["snapshot"] = snapshot
        payload["frozen_inputs"] = frozen
        row.request_json = payload
        with pytest.raises(ValueError, match="fingerprint mismatch"):
            validated_batch_frozen_inputs(frozen)
    await _run_worker(run_id, base)
    run = await fetch_run(run_id)
    assert run.status == "failed"
    assert run.error_summary == "ValueError"
    assert await artifact_counts() == (0, 0)


@pytest.mark.asyncio
async def test_runtime_database_disable_after_acceptance_fails_worker(
    account_fit_job_world: Path,
) -> None:
    base = settings(account_fit_job_world)
    async with session_scope() as session:
        await ensure_imported(session, base, app_yaml_path=account_fit_job_world / "missing.yaml")
    receipt = await admit(await valid_request(), base)
    async with session_scope() as session:
        await SettingsStore(session).apply(
            {"account_fit_enabled": False, "portfolio_ledger_enabled": False},
            actor="synthetic_test",
        )
    await _run_worker(int(receipt.run_ref.id), base)
    run = await fetch_run(int(receipt.run_ref.id))
    assert run.status == "failed"
    assert run.error_summary == "AssessmentWritesDisabledError"
    assert await artifact_counts() == (0, 0)


@pytest.mark.asyncio
async def test_typed_local_item_failure_is_partial_but_oserror_fails(
    account_fit_job_world: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = settings(account_fit_job_world)
    body = await valid_request(listing_ids=(600000, 600001))
    receipt = await admit(body, base)
    original_save = AccountFitService.save

    async def partial_save(self, inputs, item, *, operation_id):
        if item.listing_ref.id == 600000:
            raise FitItemEvaluationError("typed source gap")
        return await original_save(self, inputs, item, operation_id=operation_id)

    monkeypatch.setattr(AccountFitService, "save", partial_save)
    await _run_worker(int(receipt.run_ref.id), base)
    partial = await fetch_run(int(receipt.run_ref.id))
    assert partial.status == "partial"
    assert partial.metadata_json["completed"]
    assert partial.metadata_json["failed_items"][0]["reason_code"] == "evaluation_failed"

    body2 = await valid_request(listing_ids=(600001,))
    receipt2 = await admit(body2, base)

    async def system_error(self, inputs, item, *, operation_id):
        raise OSError("synthetic disk failure")

    monkeypatch.setattr(AccountFitService, "save", system_error)
    await _run_worker(int(receipt2.run_ref.id), base)
    failed = await fetch_run(int(receipt2.run_ref.id))
    assert failed.status == "failed"
    assert failed.error_summary == "OSError"


@pytest.mark.asyncio
async def test_lost_lease_after_result_flush_rolls_back_result_and_calculation(
    account_fit_job_world: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = settings(account_fit_job_world)
    receipt = await admit(await valid_request(), base)
    job = await claim(int(receipt.run_ref.id))
    original = PipelineRunRepository.record_metadata

    async def steal_before_checkpoint(self, run_id, metadata, *, lease=None):
        if metadata.get("completed"):
            await self.session.execute(
                update(PipelineRun)
                .where(PipelineRun.id == run_id)
                .values(worker_id="replacement", attempt_count=job.lease.attempt + 1)
            )
        return await original(self, run_id, metadata, lease=lease)

    monkeypatch.setattr(PipelineRunRepository, "record_metadata", steal_before_checkpoint)
    with pytest.raises(LeaseLostError):
        await AccountFitHandler(settings=base).execute(job, DirectContext())
    assert await artifact_counts() == (0, 0)
    run = await fetch_run(job.run_id)
    # The failed handler transaction rolls back the simulated lease write too.
    assert run.worker_id == job.lease.worker_id
    assert run.attempt_count == job.lease.attempt


@pytest.mark.asyncio
async def test_checkpoint_recovery_reuses_first_result_and_finishes_remaining_items(
    account_fit_job_world: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = settings(account_fit_job_world)
    body = await valid_request(listing_ids=(600000, 600001))
    receipt = await admit(body, base)
    job = await claim(int(receipt.run_ref.id), worker_id="crashed-worker")
    original_save = AccountFitService.save

    async def fail_second_once(self, inputs, item, *, operation_id):
        if item.listing_ref.id == 600001:
            monkeypatch.setattr(AccountFitService, "save", original_save)
            raise OSError("simulated process interruption")
        return await original_save(self, inputs, item, operation_id=operation_id)

    monkeypatch.setattr(AccountFitService, "save", fail_second_once)
    with pytest.raises(OSError):
        await AccountFitHandler(settings=base).execute(job, DirectContext())
    interrupted = await fetch_run(job.run_id)
    assert len(interrupted.metadata_json["completed"]) == 1
    assert await artifact_counts() == (1, 1)

    stale = datetime.now(UTC) - timedelta(hours=1)
    async with session_scope() as session:
        await session.execute(
            update(PipelineRun)
            .where(PipelineRun.id == job.run_id)
            .values(heartbeat_at=stale, claimed_at=stale)
        )
        requeued, failed = await PipelineRunRepository(session).recover_stale(
            kinds=[ACCOUNT_FIT_JOB], stale_before=datetime.now(UTC) - timedelta(minutes=1)
        )
        assert requeued == [job.run_id]
        assert failed == []
    await _run_worker(job.run_id, base)
    recovered = await fetch_run(job.run_id)
    assert recovered.status == "success"
    assert len(recovered.metadata_json["completed"]) == 2
    assert await artifact_counts() == (2, 2)
