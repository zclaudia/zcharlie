"""Ranking recovery across committed fits and the atomic final publish."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from investment_agent.candidates import ranking_jobs
from investment_agent.candidates.ranking_contracts import (
    BatchTradeAssumptions,
    RankingRequest,
    RankingSourcePolicy,
)
from investment_agent.candidates.ranking_jobs import (
    CandidateRankingAdmission,
    CandidateRankingHandler,
)
from investment_agent.config import Settings
from investment_agent.investor.common import ObjectRef
from investment_agent.portfolio.suitability_inputs import SuitabilityInputService
from investment_agent.research.worker import ResearchWorker
from investment_agent.storage.database import dispose_engine, init_engine, session_scope
from investment_agent.storage.models import Base
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.evidence import SavedCalculation
from investment_agent.storage.models.investment_assessment import (
    CandidateRankingEntry,
    InvestmentAssessment,
)
from investment_agent.storage.models.rules import ScreenResultRecord
from investment_agent.storage.repositories.investment_assessment import (
    InvestmentAssessmentRepository,
)
from investment_agent.storage.repositories.pipeline_run import (
    LeaseLostError,
    PipelineRunRepository,
    RunLease,
)
from tests.support.account_fit_db import make_inputs
from tests.support.account_fit_flow import persist_inputs

LISTING_ID = 600000
OWNER = "local_owner"


@pytest_asyncio.fixture
async def recovery_world(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncGenerator[tuple[Path, UUID]]:
    """Use a dedicated file database and the production worker/handler path."""
    url = f"sqlite+aiosqlite:///{tmp_path / 'ranking_recovery.sqlite'}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("API_KEY", "ci-inert-api-key")
    monkeypatch.setenv("LLM_TIER3_API_KEY", "ci-inert-tier3")
    engine = init_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    inputs = make_inputs(extra_listings=(LISTING_ID,))
    async with session_scope() as session:
        await persist_inputs(session, inputs)
        run = await PipelineRunRepository(session).create(
            kind="screen_run",
            triggered_by="ranking-recovery-followup",
            status="success",
            stage="done",
            request_json={"as_of": "2026-10-15"},
        )
        run.finished_at = datetime.now(UTC) - timedelta(minutes=1)
        run.metadata_json = {
            "as_of": "2026-10-15",
            "templates": {
                "rank_a@1": {
                    "counts": {
                        "hit": 1,
                        "truncated": 0,
                        "miss": 0,
                        "skipped": 0,
                        "excluded": 0,
                    }
                }
            },
            "result_rows": 1,
            "load_errors": {},
        }
        from investment_agent.storage.models.reference import Listing

        listing = await session.get(Listing, LISTING_ID)
        assert listing is not None
        session.add(
            ScreenResultRecord(
                screen_run_id=run.id,
                as_of=datetime(2026, 10, 15).date(),
                template_id="rank_a",
                template_version=1,
                listing_id=LISTING_ID,
                ticker=listing.ticker,
                outcome="hit",
                hit=True,
                rank=1,
                strength=0.5,
                reasons=[{"factor": "synthetic"}],
                context={"fixture": "ranking-recovery"},
            )
        )
        await session.flush()

    yield tmp_path, inputs.account_id
    await dispose_engine()


def _settings(root: Path) -> Settings:
    return Settings(
        _env_file=None,
        api_key="ci-inert-api-key",
        llm_tier3_api_key="ci-inert-tier3",
        data_dir=root,
        portfolio_ledger_enabled=True,
        account_fit_enabled=True,
        candidate_ranking_enabled=True,
    )


async def _admit(account_id: UUID, root: Path) -> tuple[int, UUID]:
    async with session_scope() as session:
        context = await SuitabilityInputService(session).context(account_id)
        assert context.snapshot is not None and context.mandate_ref is not None
        request = RankingRequest(
            operation_id=uuid4(),
            account_id=account_id,
            snapshot_ref=context.snapshot.reference,
            mandate_ref=context.mandate_ref,
            valuation_binding=context.valuation_binding,
            purpose_binding=context.purpose_binding,
            source_policy=RankingSourcePolicy(template_ids=("rank_a",)),
            trade_assumptions=BatchTradeAssumptions(
                fixed_fee=Decimal("10"),
                fee_rate=Decimal("0"),
                assumption_source="synthetic_user_confirmation",
                unknown_fields=(),
                price_policy="valuation_price_as_scenario",
                confirmed=True,
            ),
        )
        receipt = await CandidateRankingAdmission(
            session, settings=_settings(root)
        ).accept(request)
        return int(receipt.run_ref.id), request.operation_id


async def _run(run_id: int, root: Path, *, retry_base_delay_seconds: float = 5.0) -> None:
    settings = _settings(root)
    worker = ResearchWorker(
        settings=settings,
        worker_id="ranking-recovery-followup",
        handlers=[CandidateRankingHandler(settings=settings)],
        retry_base_delay_seconds=retry_base_delay_seconds,
    )
    await worker.run_once(run_id=run_id)


async def _recover(run_id: int) -> None:
    now = datetime.now(UTC)
    async with session_scope() as session:
        requeued, failed = await PipelineRunRepository(session).recover_stale(
            kind="candidate_rank",
            stale_before=now + timedelta(seconds=1),
            now=now,
        )
        assert requeued == [run_id]
        assert failed == []


async def _committed_fit_state(run_id: int) -> tuple[dict, dict, dict]:
    async with session_scope() as session:
        run = await session.get(PipelineRun, run_id)
        assert run is not None
        fit_ref = run.metadata_json["completed"][str(LISTING_ID)]["fit_ref"]
        fit_id = fit_ref["id"]
        fit_row = await session.get(InvestmentAssessment, (fit_id, 1))
        assert fit_row is not None and fit_row.kind == "account_fit"
        calculation_rows = list(
            (
                await session.execute(
                    select(SavedCalculation).order_by(SavedCalculation.id)
                )
            ).scalars()
        )
        assert len(calculation_rows) == 1
        return fit_row.payload, calculation_rows[0].payload, fit_ref


async def _assert_no_ranking() -> None:
    async with session_scope() as session:
        assert await session.scalar(
            select(func.count())
            .select_from(InvestmentAssessment)
            .where(InvestmentAssessment.kind == "candidate_ranking")
        ) == 0
        assert await session.scalar(
            select(func.count()).select_from(CandidateRankingEntry)
        ) == 0


async def _assert_final_ranking(run_id: int) -> tuple[dict, dict]:
    async with session_scope() as session:
        run = await session.get(PipelineRun, run_id)
        assert run is not None and run.status == "success"
        fit_payload, _, fit_ref = await _committed_fit_state(run_id)
        ranking_ref = ObjectRef.model_validate(run.metadata_json["ranking_ref"])
        ranking = await InvestmentAssessmentRepository(session).read_ranking(
            OWNER, ranking_ref
        )
        assert ranking is not None
        assert ranking.snapshot.entry_count == 1
        assert len(ranking.entries) == 1
        assert ranking.entries[0].fit_ref == ObjectRef.model_validate(fit_ref)
        assert ranking.entries[0].ranking_ref == ranking_ref
        assert await session.scalar(
            select(func.count()).select_from(CandidateRankingEntry)
        ) == 1
        return fit_payload, ranking.entries[0].model_dump(mode="json")


@pytest.mark.asyncio
async def test_process_crash_after_committed_fit_reuses_fit_and_calculation_on_new_attempt(
    recovery_world: tuple[Path, UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, account_id = recovery_world
    run_id, _ = await _admit(account_id, root)
    original_save = InvestmentAssessmentRepository.save_ranking

    async def crash_before_final_publish(self, owner_id, snapshot, entries):
        raise TimeoutError("simulated transient I/O loss after per-item fit commit")

    monkeypatch.setattr(
        InvestmentAssessmentRepository, "save_ranking", crash_before_final_publish
    )
    await _run(run_id, root, retry_base_delay_seconds=0)

    fit_before, calculation_before, fit_ref_before = await _committed_fit_state(run_id)
    await _assert_no_ranking()
    async with session_scope() as session:
        run = await session.get(PipelineRun, run_id)
        assert run is not None and run.status == "queued" and run.attempt_count == 1

    # Retry classification schedules the same authorized run; its next claim is
    # attempt 2 and AccountFitService must return the already committed objects.
    monkeypatch.setattr(InvestmentAssessmentRepository, "save_ranking", original_save)
    await _run(run_id, root)

    fit_after, calculation_after, fit_ref_after = await _committed_fit_state(run_id)
    assert fit_after == fit_before
    assert calculation_after == calculation_before
    assert fit_ref_after == fit_ref_before
    await _assert_final_ranking(run_id)
    async with session_scope() as session:
        run = await session.get(PipelineRun, run_id)
        assert run is not None and run.attempt_count == 2


@pytest.mark.asyncio
async def test_lease_loss_after_ranking_flush_rolls_back_bundle_and_new_attempt_publishes(
    recovery_world: tuple[Path, UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, account_id = recovery_world
    run_id, _ = await _admit(account_id, root)
    original_save = InvestmentAssessmentRepository.save_ranking
    stale_lease = RunLease(
        run_id=run_id, worker_id="ranking-recovery-followup", attempt=1
    )

    async def flush_then_lose_lease(self, owner_id, snapshot, entries):
        stored = await original_save(self, owner_id, snapshot, entries)
        run = await self.session.get(PipelineRun, run_id, populate_existing=True)
        assert run is not None and run.status == "running"
        # Simulate a competing worker taking ownership in this same transaction,
        # after both the ranking header and entries have flushed.
        run.worker_id = "superseding-worker"
        run.attempt_count = 2
        await self.session.flush()
        return stored

    monkeypatch.setattr(
        InvestmentAssessmentRepository, "save_ranking", flush_then_lose_lease
    )
    await _run(run_id, root)

    await _assert_no_ranking()
    fit_after_failure, calculation_after_failure, fit_ref = await _committed_fit_state(run_id)
    async with session_scope() as session:
        run = await session.get(PipelineRun, run_id)
        assert run is not None and run.status == "running" and run.attempt_count == 1

    # Recovery issues a genuinely newer lease for the same authorized run.
    await _recover(run_id)
    monkeypatch.setattr(InvestmentAssessmentRepository, "save_ranking", original_save)
    await _run(run_id, root)
    fit_after, ranking_entry = await _assert_final_ranking(run_id)
    assert fit_after == fit_after_failure
    _, calculation_after_success, _ = await _committed_fit_state(run_id)
    assert calculation_after_success == calculation_after_failure
    assert ranking_entry["fit_ref"] == fit_ref
    assert ranking_entry["ranking_bucket"] in {"blocked", "incomplete"}

    async with session_scope() as session:
        run = await session.get(PipelineRun, run_id)
        assert run is not None and run.attempt_count == 2
        with pytest.raises(LeaseLostError):
            await PipelineRunRepository(session).mark_success(
                run_id, {"stale_attempt_publish": True}, lease=stale_lease
            )


@pytest.mark.asyncio
async def test_competing_session_lease_takeover_before_final_publish_is_fenced_and_recovered(
    recovery_world: tuple[Path, UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, account_id = recovery_world
    run_id, _ = await _admit(account_id, root)
    original_current_state = ranking_jobs._ranking_current_state
    stale_lease = RunLease(
        run_id=run_id, worker_id="ranking-recovery-followup", attempt=1
    )

    async def take_lease_in_competing_session(*args, **kwargs):
        current = await original_current_state(*args, **kwargs)
        # This hook runs after each fit has committed and before the handler
        # opens its final write transaction. The second session commits a real
        # ownership change, so the old handler must fail its final lease fence.
        async with session_scope() as session:
            run = await session.get(PipelineRun, run_id, populate_existing=True)
            assert run is not None and run.status == "running"
            assert run.attempt_count == 1
            now = datetime.now(UTC)
            run.heartbeat_at = now - timedelta(minutes=1)
            await session.flush()
            repository = PipelineRunRepository(session)
            requeued, failed = await repository.recover_stale(
                kind="candidate_rank",
                stale_before=now,
                now=now,
            )
            assert requeued == [run_id] and failed == []
            competing_claim = await repository.claim_next(
                worker_id="competing-worker",
                kinds=("candidate_rank",),
                run_id=run_id,
            )
            assert competing_claim is not None
            assert competing_claim.worker_id == "competing-worker"
            assert competing_claim.attempt_count == 2
        return current

    monkeypatch.setattr(
        ranking_jobs, "_ranking_current_state", take_lease_in_competing_session
    )
    await _run(run_id, root)

    fit_after_loss, calculation_after_loss, fit_ref = await _committed_fit_state(run_id)
    await _assert_no_ranking()
    async with session_scope() as session:
        run = await session.get(PipelineRun, run_id)
        assert run is not None
        assert run.status == "running"
        assert run.worker_id == "competing-worker"
        assert run.attempt_count == 2
        with pytest.raises(LeaseLostError):
            await PipelineRunRepository(session).mark_success(
                run_id, {"stale_attempt_publish": True}, lease=stale_lease
            )

    # The competing claim is now stale; the supported recovery path requeues
    # this same run, and the next worker claim receives attempt 3.
    await _recover(run_id)
    monkeypatch.setattr(
        ranking_jobs, "_ranking_current_state", original_current_state
    )
    await _run(run_id, root)

    fit_after, ranking_entry = await _assert_final_ranking(run_id)
    _, calculation_after, _ = await _committed_fit_state(run_id)
    assert fit_after == fit_after_loss
    assert calculation_after == calculation_after_loss
    assert ranking_entry["fit_ref"] == fit_ref
    async with session_scope() as session:
        run = await session.get(PipelineRun, run_id)
        assert run is not None and run.attempt_count == 3
        with pytest.raises(LeaseLostError):
            await PipelineRunRepository(session).mark_success(
                run_id, {"stale_attempt_publish": True}, lease=stale_lease
            )
