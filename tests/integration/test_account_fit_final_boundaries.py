"""Final backend boundary tests using isolated file-backed SQLite databases."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select, update
from sqlalchemy.exc import OperationalError

from investment_agent.config import Settings
from investment_agent.investor.common import ObjectRef
from investment_agent.jobs.base import ClaimedJob
from investment_agent.portfolio.suitability import AccountFitService
from investment_agent.portfolio.suitability_contracts import AccountFitRequest, FitRequestItem
from investment_agent.portfolio.suitability_jobs import (
    ACCOUNT_FIT_JOB,
    AccountFitAdmission,
    AccountFitHandler,
    AdmissionReceipt,
)
from investment_agent.research.worker import ResearchWorker
from investment_agent.storage.database import dispose_engine, init_engine, session_scope
from investment_agent.storage.models import Base
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.evidence import SavedCalculation
from investment_agent.storage.models.investment_assessment import InvestmentAssessment
from investment_agent.storage.models.portfolio import PortfolioAccount, PortfolioValuation
from investment_agent.storage.repositories.pipeline_run import (
    LeaseLostError,
    PipelineRunRepository,
    RunLease,
)
from tests.support.account_fit import assumptions
from tests.support.account_fit_db import make_inputs
from tests.support.account_fit_flow import persist_inputs

API_KEY = {"X-API-Key": "secret"}


@pytest.fixture
def enabled_features(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PORTFOLIO_LEDGER_ENABLED", "true")
    monkeypatch.setenv("ACCOUNT_FIT_ENABLED", "true")
    monkeypatch.setenv("CANDIDATE_RANKING_ENABLED", "false")


@pytest_asyncio.fixture
async def fit_world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'account_fit_final_boundaries.sqlite'}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    engine = init_engine(database_url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield tmp_path
    await dispose_engine()


def _settings(root: Path) -> Settings:
    return Settings(
        api_key="integration-test-key",
        llm_tier3_api_key="inert",
        data_dir=root,
        portfolio_ledger_enabled=True,
        account_fit_enabled=True,
        candidate_ranking_enabled=False,
    )


async def _prepare(*, with_valuation: bool = True) -> tuple[Any, AccountFitRequest]:
    inputs = make_inputs(with_valuation=with_valuation, rules=())
    async with session_scope() as session:
        await persist_inputs(session, inputs)
    request = AccountFitRequest(
        operation_id=uuid4(),
        account_id=inputs.account_id,
        snapshot_ref=inputs.snapshot.reference,
        mandate_ref=ObjectRef(kind="mandate", id=inputs.mandate.id, revision=1),
        valuation_binding=inputs.valuation_binding,
        purpose_binding=inputs.purpose_binding,
        items=(
            FitRequestItem(
                listing_ref=ObjectRef(kind="listing", id=600000, revision=1),
                action="open",
                trade_assumptions=assumptions(),
            ),
        ),
    )
    return inputs, request


async def _admit(request: AccountFitRequest, root: Path) -> AdmissionReceipt:
    async with session_scope() as session:
        return await AccountFitAdmission(session, settings=_settings(root)).accept(request)


async def _counts() -> tuple[int, int]:
    async with session_scope() as session:
        fits = int(
            await session.scalar(select(func.count()).select_from(InvestmentAssessment)) or 0
        )
        calculations = int(
            await session.scalar(select(func.count()).select_from(SavedCalculation)) or 0
        )
        return fits, calculations


async def _claim(run_id: int, worker_id: str) -> ClaimedJob:
    async with session_scope() as session:
        run = await PipelineRunRepository(session).claim_next(
            worker_id=worker_id, kinds=[ACCOUNT_FIT_JOB], run_id=run_id
        )
        assert run is not None
        return ClaimedJob(
            run_id=run.id,
            kind=run.kind,
            lease=RunLease(run.id, worker_id, run.attempt_count),
            request_json=dict(run.request_json),
        )


class _DirectContext:
    worker_id = "direct-final-boundary-test"

    async def run_with_heartbeat(self, lease: RunLease, work):
        return await work()


def _worker(root: Path, worker_id: str) -> ResearchWorker:
    settings = _settings(root)
    return ResearchWorker(
        settings=settings,
        worker_id=worker_id,
        handlers=[AccountFitHandler(settings=settings)],
    )


@pytest.mark.asyncio
async def test_claimed_handler_is_fenced_after_second_session_commits_new_attempt(
    fit_world: Path,
) -> None:
    _, request = await _prepare()
    receipt = await _admit(request, fit_world)
    job = await _claim(int(receipt.run_ref.id), "old-worker")

    # A different transaction commits a replacement owner/attempt after claim,
    # before the old handler's first fenced checkpoint.
    async with session_scope() as session:
        await session.execute(
            update(PipelineRun)
            .where(PipelineRun.id == job.run_id)
            .values(worker_id="replacement-worker", attempt_count=job.lease.attempt + 1)
        )

    with pytest.raises(LeaseLostError):
        await AccountFitHandler(settings=_settings(fit_world)).execute(job, _DirectContext())

    assert await _counts() == (0, 0)
    async with session_scope() as session:
        current = await session.get(PipelineRun, job.run_id)
        assert current is not None
        assert current.status == "running"
        assert current.worker_id == "replacement-worker"
        assert current.attempt_count == job.lease.attempt + 1


@pytest.mark.asyncio
async def test_worker_retries_sqlite_lock_without_partial_then_succeeds(
    fit_world: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, request = await _prepare()
    receipt = await _admit(request, fit_world)
    original_save = AccountFitService.save
    should_fail = True

    async def fail_once(self, inputs, item, *, operation_id):
        nonlocal should_fail
        if should_fail:
            should_fail = False
            raise OperationalError("INSERT", {}, Exception("database is locked"))
        return await original_save(self, inputs, item, operation_id=operation_id)

    with monkeypatch.context() as patcher:
        patcher.setattr(AccountFitService, "save", fail_once)
        await _worker(fit_world, "retry-worker").run_once(run_id=int(receipt.run_ref.id))

    async with session_scope() as session:
        queued = await session.get(PipelineRun, int(receipt.run_ref.id))
        assert queued is not None
        assert queued.status == "queued"
        assert queued.error_summary is not None and "retry scheduled" in queued.error_summary
        queued.available_at = datetime.now(UTC) - timedelta(seconds=1)
    assert await _counts() == (0, 0)

    await _worker(fit_world, "retry-worker").run_once(run_id=int(receipt.run_ref.id))
    async with session_scope() as session:
        succeeded = await session.get(PipelineRun, int(receipt.run_ref.id))
        assert succeeded is not None and succeeded.status == "success"
        assert succeeded.attempt_count == 2
        assert succeeded.metadata_json["completed"]
        assert not succeeded.metadata_json["failed_items"]
    assert await _counts() == (1, 1)


@pytest.mark.asyncio
async def test_new_valuation_makes_missing_valuation_fit_stale_without_mutating_payload(
    api_client, enabled_features: None, fit_world: Path
) -> None:
    inputs, request = await _prepare(with_valuation=False)
    accepted = api_client.post(
        "/portfolio/fit-assessments", json=request.model_dump(mode="json"), headers=API_KEY
    )
    assert accepted.status_code == 202, accepted.text
    receipt = accepted.json()
    await _worker(fit_world, "valuation-worker").run_once(run_id=receipt["run_ref"]["id"])
    run_response = api_client.get(
        f"/investment-assessment-runs/{receipt['run_ref']['id']}", headers=API_KEY
    )
    assert run_response.status_code == 200, run_response.text
    fit_ref = run_response.json()["fit_refs"][0]

    exact_url = f"/portfolio/fit-assessments/{fit_ref['id']}"
    exact_params = {"account_id": str(inputs.account_id), "revision": fit_ref["revision"]}
    before_response = api_client.get(exact_url, params=exact_params, headers=API_KEY)
    assert before_response.status_code == 200, before_response.text
    before = before_response.json()
    assert before["payload"]["financial_status"] == "unknown"
    assert before["payload"]["frozen_inputs"]["valuation"] is None

    available = make_inputs(with_valuation=True).valuation
    assert available is not None
    exact_valuation = available.model_copy(
        update={"snapshot_id": inputs.snapshot.id, "ledger_revision": inputs.snapshot.revision}
    )
    async with session_scope() as session:
        session.add(
            PortfolioValuation(
                id=str(exact_valuation.id),
                account_id=str(inputs.account_id),
                snapshot_id=str(inputs.snapshot.id),
                ledger_revision=inputs.snapshot.revision,
                valuation_date=exact_valuation.valuation_date,
                algorithm_version=exact_valuation.algorithm_version,
                input_digest=exact_valuation.input_digest,
                payload=exact_valuation.model_dump(mode="json"),
            )
        )
        account = await session.get(PortfolioAccount, str(inputs.account_id))
        assert account is not None
        account.current_valuation_id = str(exact_valuation.id)

    after_response = api_client.get(exact_url, params=exact_params, headers=API_KEY)
    assert after_response.status_code == 200, after_response.text
    after = after_response.json()
    assert after["current_limits"]["current_status"] == "stale"
    assert "valuation_changed" in after["current_limits"]["reasons"]
    assert after["payload"] == before["payload"]
