"""Focused integrity regressions for admitted assessment batches."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from investment_agent.candidates.ranking_contracts import (
    BatchTradeAssumptions,
    RankingRequest,
    RankingSourcePolicy,
    build_source_manifest,
)
from investment_agent.candidates.ranking_jobs import (
    CandidateRankingAdmission,
    CandidateRankingHandler,
    _validated_source_manifest,
)
from investment_agent.config import Settings
from investment_agent.investor.common import ObjectRef
from investment_agent.investor.foundations import InvestorError
from investment_agent.portfolio.service import digest
from investment_agent.portfolio.suitability_contracts import (
    AccountFitRequest,
    FitRequestItem,
    FrozenAccountInputs,
    HistoricalReplayBinding,
)
from investment_agent.portfolio.suitability_inputs import SuitabilityInputService
from investment_agent.portfolio.suitability_jobs import (
    AccountFitAdmission,
    AccountFitHandler,
    AdmissionReceipt,
    authorized_run,
)
from investment_agent.research.worker import ResearchWorker
from investment_agent.storage.database import dispose_engine, init_engine, session_scope
from investment_agent.storage.models import Base
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.evidence import SavedCalculation
from investment_agent.storage.models.investment_assessment import (
    CandidateRankingEntry,
    InvestmentAssessment,
)
from investment_agent.storage.models.investor import InvestorMutation
from investment_agent.storage.models.reference import Listing
from investment_agent.storage.models.rules import ScreenResultRecord
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository
from tests.support.account_fit import assumptions
from tests.support.account_fit_db import make_inputs
from tests.support.account_fit_flow import persist_inputs


@pytest_asyncio.fixture
async def assessment_integrity_world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    url = f"sqlite+aiosqlite:///{tmp_path / 'assessment_integrity.sqlite'}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("API_KEY", "ci-inert-api-key")
    monkeypatch.setenv("LLM_TIER3_API_KEY", "ci-inert-tier3")
    engine = init_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with session_scope() as session:
        await persist_inputs(
            session,
            make_inputs(
                holdings=((600000, "100", "1500"),),
                extra_listings=(600001,),
                prices={600000: "1500", 600001: "1500"},
                rules=(),
                snapshot_coverage="complete",
            ),
        )
    yield tmp_path
    await dispose_engine()


def _settings(path: Path) -> Settings:
    return Settings(
        _env_file=None,
        api_key="ci-inert-api-key",
        llm_tier3_api_key="ci-inert-tier3",
        data_dir=path,
        portfolio_ledger_enabled=True,
        account_fit_enabled=True,
        candidate_ranking_enabled=True,
    )


async def _fit_request(
    *, mode: str = "live", binding: HistoricalReplayBinding | None = None
) -> AccountFitRequest:
    async with session_scope() as session:
        context = await SuitabilityInputService(session).context(make_inputs().account_id)
    assert context.snapshot is not None and context.mandate_ref is not None
    return AccountFitRequest(
        operation_id=uuid4(),
        account_id=context.account.id,
        snapshot_ref=context.snapshot.reference,
        mandate_ref=context.mandate_ref,
        valuation_binding=context.valuation_binding,
        purpose_binding=context.purpose_binding,
        mode=mode,
        historical_replay=binding,
        items=(
            FitRequestItem(
                listing_ref=ObjectRef(kind="listing", id=600001, revision=1),
                action="open",
                trade_assumptions=assumptions(),
            ),
        ),
    )


async def _ranking_request(
    *, mode: str = "live", binding: HistoricalReplayBinding | None = None
) -> RankingRequest:
    async with session_scope() as session:
        context = await SuitabilityInputService(session).context(make_inputs().account_id)
    assert context.snapshot is not None and context.mandate_ref is not None
    return RankingRequest(
        operation_id=uuid4(),
        account_id=context.account.id,
        snapshot_ref=context.snapshot.reference,
        mandate_ref=context.mandate_ref,
        valuation_binding=context.valuation_binding,
        purpose_binding=context.purpose_binding,
        mode=mode,
        historical_replay=binding,
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


async def _seed_screen() -> None:
    async with session_scope() as session:
        run = await PipelineRunRepository(session).create(
            kind="screen_run",
            triggered_by="followup_test",
            status="success",
            stage="done",
            request_json={"as_of": "2026-10-15"},
        )
        run.finished_at = datetime.now(UTC) - timedelta(minutes=1)
        counts = {"hit": 0, "truncated": 0, "miss": 0, "skipped": 0, "excluded": 0}
        for listing_id, outcome, rank in ((600000, "hit", 1), (600001, "truncated", 2)):
            listing = await session.get(Listing, listing_id)
            assert listing is not None
            counts[outcome] += 1
            session.add(
                ScreenResultRecord(
                    screen_run_id=run.id,
                    as_of=date(2026, 10, 15),
                    template_id="rank_a",
                    template_version=1,
                    listing_id=listing_id,
                    ticker=listing.ticker,
                    outcome=outcome,
                    hit=outcome == "hit",
                    rank=rank,
                    strength=0.5,
                    reasons=[{"factor": "synthetic"}],
                    context={"fixture": True},
                )
            )
        run.metadata_json = {
            "as_of": "2026-10-15",
            "templates": {"rank_a@1": {"counts": counts}},
            "result_rows": 2,
            "load_errors": {},
        }


async def _run_handler(run_id: int, base: Settings) -> None:
    handlers = [
        AccountFitHandler(settings=base),
        CandidateRankingHandler(settings=base),
    ]
    worker = ResearchWorker(settings=base, worker_id="integrity-test", handlers=handlers)
    await worker.run_once(run_id=run_id)


async def _artifact_count() -> int:
    async with session_scope() as session:
        total = 0
        for model in (InvestmentAssessment, CandidateRankingEntry, SavedCalculation):
            total += int(await session.scalar(select(func.count()).select_from(model)) or 0)
        return total


@pytest.mark.asyncio
async def test_authorized_run_recomputes_receipt_from_original_command(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'receipt.sqlite'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    operation_id = uuid4()
    source_hash = "a" * 64
    client_request = {"operation_id": str(operation_id), "quantity": "100"}
    request_hash = digest({"command": "account_fit_evaluate", "body": client_request})
    async with factory() as session:
        run = PipelineRun(
            kind="account_fit_evaluate",
            triggered_by="followup_test",
            started_at=datetime(2026, 10, 16, tzinfo=UTC),
            idempotency_key="account_fit:test",
            request_json={
                "owner_id": "local_owner",
                "operation_id": str(operation_id),
                "request_hash": request_hash,
                "source_manifest_hash": source_hash,
                "client_request": client_request,
            },
        )
        session.add(run)
        await session.flush()
        receipt = AdmissionReceipt(
            operation_id=operation_id,
            run_ref=ObjectRef(kind="pipeline_run", id=run.id, revision=1),
            accepted_as_of=datetime(2026, 10, 16, tzinfo=UTC),
            request_hash=request_hash,
            source_manifest_hash=source_hash,
        )
        session.add(
            InvestorMutation(
                owner_id="local_owner",
                key=str(operation_id),
                request_hash=request_hash,
                result=receipt.model_dump(mode="json"),
            )
        )
        await session.flush()
        assert (await authorized_run(session, run.id)).id == run.id

        run.request_json = {
            **run.request_json,
            "client_request": {"operation_id": str(operation_id), "quantity": "999"},
        }
        with pytest.raises(InvestorError) as mismatch:
            await authorized_run(session, run.id)
        assert mismatch.value.detail.code == "not_found"
    await engine.dispose()


def test_ranking_source_manifest_must_match_admission_digest() -> None:
    manifest = build_source_manifest(
        {
            "source_run_refs": (),
            "source_runs": (),
            "candidate_policy": {"expiry_days": 14, "dismiss_cooldown_days": 30},
            "as_of": datetime(2026, 10, 16, tzinfo=UTC),
            "members": (),
            "exclusions": (),
            "source_issues": (),
            "coverage": "complete",
            "total_unique_count": 0,
            "processed_count": 0,
            "unprocessed_count": 0,
            "source_hashes": (),
        }
    )
    assert (
        _validated_source_manifest(
            {
                "source_manifest": manifest.model_dump(mode="json"),
                "source_manifest_hash": manifest.manifest_hash,
            }
        )
        == manifest
    )
    with pytest.raises(InvestorError) as mismatch:
        _validated_source_manifest(
            {"source_manifest": manifest.model_dump(mode="json"), "source_manifest_hash": "b" * 64}
        )
    assert mismatch.value.detail.code == "historical_manifest_unavailable"


@pytest.mark.asyncio
async def test_fit_worker_and_replay_reject_self_rehashed_frozen_batch(
    assessment_integrity_world: Path,
) -> None:
    base = _settings(assessment_integrity_world)
    original_request = await _fit_request()
    async with session_scope() as session:
        receipt = await AccountFitAdmission(session, settings=base).accept(original_request)
    run_id = int(receipt.run_ref.id)
    async with session_scope() as session:
        run = await session.get(PipelineRun, run_id)
        assert run is not None
        payload = dict(run.request_json)
        frozen = FrozenAccountInputs.model_validate(payload["frozen_inputs"])
        snapshot = frozen.snapshot.model_copy(
            update={"known_cash": Decimal("999999"), "cash_subtotal": Decimal("999999")}
        )
        changed = frozen.model_copy(update={"snapshot": snapshot})
        changed_hash = digest(
            changed.model_dump(mode="json", exclude={"manifest_hash", "source_fingerprint"})
        )
        changed = changed.model_copy(
            update={"manifest_hash": changed_hash, "source_fingerprint": changed_hash}
        )
        payload["frozen_inputs"] = changed.model_dump(mode="json")
        # The historical receipt/top-level admission hash deliberately remains original.
        assert payload["source_manifest_hash"] == receipt.source_manifest_hash
        run.request_json = payload
        replay_as_of = frozen.assessment_as_of

    await _run_handler(run_id, base)
    async with session_scope() as session:
        failed = await session.get(PipelineRun, run_id)
        assert failed is not None and failed.status == "failed"
        assert await _artifact_count() == 0

    binding = HistoricalReplayBinding(
        run_ref=receipt.run_ref,
        manifest_hash=receipt.source_manifest_hash,
        input_as_of=replay_as_of,
    )
    replay = original_request.model_copy(
        update={"operation_id": uuid4(), "mode": "historical_replay", "historical_replay": binding}
    )
    async with session_scope() as session:
        with pytest.raises(InvestorError) as mismatch:
            await AccountFitAdmission(session, settings=base).accept(replay)
    assert mismatch.value.detail.code == "historical_manifest_unavailable"
    async with session_scope() as session:
        assert await session.scalar(select(func.count()).select_from(PipelineRun)) == 1


@pytest.mark.asyncio
async def test_fit_worker_rejects_changed_client_command_without_artifacts(
    assessment_integrity_world: Path,
) -> None:
    base = _settings(assessment_integrity_world)
    async with session_scope() as session:
        receipt = await AccountFitAdmission(session, settings=base).accept(await _fit_request())
    run_id = int(receipt.run_ref.id)
    async with session_scope() as session:
        run = await session.get(PipelineRun, run_id)
        assert run is not None
        payload = dict(run.request_json)
        command = dict(payload["client_request"])
        command_items = [dict(item) for item in command["items"]]
        trade = dict(command_items[0]["trade_assumptions"])
        trade["fixed_fee"] = "999"
        command_items[0]["trade_assumptions"] = trade
        command["items"] = command_items
        payload["client_request"] = command
        run.request_json = payload

    await _run_handler(run_id, base)
    async with session_scope() as session:
        failed = await session.get(PipelineRun, run_id)
        assert failed is not None and failed.status == "failed"
        assert failed.error_summary == "InvestorError"
        assert await _artifact_count() == 0


@pytest.mark.asyncio
async def test_fit_worker_rejects_mode_rewrite_without_artifacts(
    assessment_integrity_world: Path,
) -> None:
    base = _settings(assessment_integrity_world)
    async with session_scope() as session:
        receipt = await AccountFitAdmission(session, settings=base).accept(await _fit_request())
    run_id = int(receipt.run_ref.id)
    async with session_scope() as session:
        run = await session.get(PipelineRun, run_id)
        assert run is not None
        payload = dict(run.request_json)
        frozen = dict(payload["frozen_inputs"])
        frozen["mode"] = "historical_replay"
        payload["frozen_inputs"] = frozen
        run.request_json = payload

    await _run_handler(run_id, base)
    async with session_scope() as session:
        failed = await session.get(PipelineRun, run_id)
        assert failed is not None and failed.status == "failed"
        assert await _artifact_count() == 0


@pytest.mark.asyncio
async def test_ranking_worker_and_replay_reject_self_rehashed_source_manifest(
    assessment_integrity_world: Path,
) -> None:
    await _seed_screen()
    base = _settings(assessment_integrity_world)
    original_request = await _ranking_request()
    async with session_scope() as session:
        receipt = await CandidateRankingAdmission(session, settings=base).accept(original_request)
    run_id = int(receipt.run_ref.id)
    async with session_scope() as session:
        run = await session.get(PipelineRun, run_id)
        assert run is not None
        payload = dict(run.request_json)
        source = dict(payload["source_manifest"])
        source.pop("manifest_hash")
        source.pop("canonical_bytes")
        source["candidate_policy"] = {**source["candidate_policy"], "expiry_days": 15}
        changed_manifest = build_source_manifest(source)
        payload["source_manifest"] = changed_manifest.model_dump(mode="json")
        # Its own canonical digest is valid, while the original receipt is unchanged.
        assert changed_manifest.manifest_hash != receipt.source_manifest_hash
        assert payload["source_manifest_hash"] == receipt.source_manifest_hash
        run.request_json = payload
        frozen = FrozenAccountInputs.model_validate(payload["frozen_inputs"])

    await _run_handler(run_id, base)
    async with session_scope() as session:
        failed = await session.get(PipelineRun, run_id)
        assert failed is not None and failed.status == "failed"
        assert await _artifact_count() == 0

    binding = HistoricalReplayBinding(
        run_ref=receipt.run_ref,
        manifest_hash=receipt.source_manifest_hash,
        input_as_of=frozen.assessment_as_of,
    )
    replay = original_request.model_copy(
        update={"operation_id": uuid4(), "mode": "historical_replay", "historical_replay": binding}
    )
    async with session_scope() as session:
        with pytest.raises(InvestorError) as mismatch:
            await CandidateRankingAdmission(session, settings=base).accept(replay)
    assert mismatch.value.detail.code == "historical_manifest_unavailable"
    async with session_scope() as session:
        assert await session.scalar(select(func.count()).select_from(PipelineRun)) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["stale_manifest_hash", "unknown_policy"])
async def test_ranking_historical_validation_errors_are_mapped_to_unavailable(
    assessment_integrity_world: Path, mutation: str
) -> None:
    await _seed_screen()
    base = _settings(assessment_integrity_world)
    original_request = await _ranking_request()
    async with session_scope() as session:
        receipt = await CandidateRankingAdmission(session, settings=base).accept(original_request)
    run_id = int(receipt.run_ref.id)
    async with session_scope() as session:
        original = await session.get(PipelineRun, run_id)
        assert original is not None
        payload = dict(original.request_json)
        if mutation == "stale_manifest_hash":
            manifest = dict(payload["source_manifest"])
            policy = dict(manifest["candidate_policy"])
            policy["expiry_days"] = policy["expiry_days"] + 1
            manifest["candidate_policy"] = policy
            # Keep its old manifest_hash and byte count to model historical corruption.
            payload["source_manifest"] = manifest
        else:
            payload["ranking_policy_version"] = "future_v2"
        original.request_json = payload
        frozen = FrozenAccountInputs.model_validate(payload["frozen_inputs"])

    binding = HistoricalReplayBinding(
        run_ref=receipt.run_ref,
        manifest_hash=receipt.source_manifest_hash,
        input_as_of=frozen.assessment_as_of,
    )
    replay = original_request.model_copy(
        update={"operation_id": uuid4(), "mode": "historical_replay", "historical_replay": binding}
    )
    async with session_scope() as session:
        with pytest.raises(InvestorError) as unavailable:
            await CandidateRankingAdmission(session, settings=base).accept(replay)
    assert unavailable.value.detail.code == "historical_manifest_unavailable"
    async with session_scope() as session:
        # One screen run plus the original accepted ranking; rejection admits no replay run.
        assert await session.scalar(select(func.count()).select_from(PipelineRun)) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    ["duplicate_listing", "changed_action", "changed_assumption", "mode_mismatch"],
)
async def test_ranking_worker_rejects_changed_derived_actions_before_writes(
    assessment_integrity_world: Path, mutation: str
) -> None:
    await _seed_screen()
    base = _settings(assessment_integrity_world)
    original_request = await _ranking_request()
    async with session_scope() as session:
        receipt = await CandidateRankingAdmission(session, settings=base).accept(
            original_request
        )
    run_id = int(receipt.run_ref.id)
    async with session_scope() as session:
        run = await session.get(PipelineRun, run_id)
        assert run is not None
        payload = dict(run.request_json)
        items = [dict(item) for item in payload["expanded_items"]]
        assert len(items) == 2
        if mutation == "mode_mismatch":
            frozen = dict(payload["frozen_inputs"])
            frozen["mode"] = "historical_replay"
            payload["frozen_inputs"] = frozen
        elif mutation == "duplicate_listing":
            items[1]["listing_ref"] = items[0]["listing_ref"]
        elif mutation == "changed_action":
            items[0]["action"] = "hold"
        else:
            assumptions_payload = dict(items[0]["trade_assumptions"])
            assumptions_payload["fixed_fee"] = "999"
            items[0]["trade_assumptions"] = assumptions_payload
        payload["expanded_items"] = items
        run.request_json = payload

    await _run_handler(run_id, base)
    async with session_scope() as session:
        failed = await session.get(PipelineRun, run_id)
        assert failed is not None and failed.status == "failed"
        assert await _artifact_count() == 0
        frozen = FrozenAccountInputs.model_validate(failed.request_json["frozen_inputs"])

    binding = HistoricalReplayBinding(
        run_ref=receipt.run_ref,
        manifest_hash=receipt.source_manifest_hash,
        input_as_of=frozen.assessment_as_of,
    )
    replay = original_request.model_copy(
        update={"operation_id": uuid4(), "mode": "historical_replay", "historical_replay": binding}
    )
    async with session_scope() as session:
        with pytest.raises(InvestorError) as rejected:
            await CandidateRankingAdmission(session, settings=base).accept(replay)
    assert rejected.value.detail.code == "historical_manifest_unavailable"
    async with session_scope() as session:
        assert await session.scalar(select(func.count()).select_from(PipelineRun)) == 2
