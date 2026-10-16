"""Ranking admission and worker behavior against a disposable SQLite DB."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from investment_agent.candidates.ranking_contracts import (
    RANKING_POLICY_VERSION,
    BatchTradeAssumptions,
    RankingRequest,
    RankingSourcePolicy,
    SourceManifest,
)
from investment_agent.candidates.ranking_jobs import (
    AdmissionReceipt,
    CandidateRankingAdmission,
    CandidateRankingHandler,
    _previous_ranking_ref,
    _ranking_current_state,
    _ranking_result_has_unknown,
    read_ranking_view,
)
from investment_agent.config import Settings
from investment_agent.investor.common import ObjectRef
from investment_agent.investor.foundations import InvestorError
from investment_agent.portfolio.calculations import TrustedTradingRule
from investment_agent.portfolio.service import digest
from investment_agent.portfolio.suitability_contracts import (
    FitCheck,
    FrozenAccountInputs,
    HistoricalReplayBinding,
)
from investment_agent.portfolio.suitability_inputs import SuitabilityInputService
from investment_agent.research.worker import ResearchWorker
from investment_agent.storage.database import dispose_engine, init_engine, session_scope
from investment_agent.storage.models import Base
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.investment_assessment import (
    CandidateRankingEntry,
    InvestmentAssessment,
)
from investment_agent.storage.models.reference import Listing
from investment_agent.storage.models.rules import ScreenResultRecord
from investment_agent.storage.repositories.investment_assessment import (
    InvestmentAssessmentRepository,
)
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository
from tests.support.account_fit_db import make_inputs, make_trusted_rule
from tests.support.account_fit_flow import persist_inputs


@pytest_asyncio.fixture
async def ranking_job_world(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncGenerator[Path]:
    url = f"sqlite+aiosqlite:///{tmp_path / 'candidate_ranking_jobs.sqlite'}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("API_KEY", "test")
    monkeypatch.setenv("LLM_TIER3_API_KEY", "test")
    engine = init_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with session_scope() as session:
        await persist_inputs(
            session,
            make_inputs(extra_listings=(600001,), snapshot_coverage="partial"),
        )
    yield tmp_path
    await dispose_engine()


@pytest_asyncio.fixture
async def known_holding_ranking_world(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncGenerator[Path]:
    url = f"sqlite+aiosqlite:///{tmp_path / 'known_holding_ranking.sqlite'}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("API_KEY", "ci-inert-api-key")
    monkeypatch.setenv("LLM_TIER3_API_KEY", "ci-inert-tier3")
    engine = init_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    inputs = make_inputs(
        holdings=((600000, "100", "1500"),),
        extra_listings=(600001,),
        prices={600000: "1500", 600001: "1500"},
        rules=(),
        snapshot_coverage="complete",
    )
    async with session_scope() as session:
        await persist_inputs(session, inputs)
    yield tmp_path
    await dispose_engine()


def settings(path: Path) -> Settings:
    return Settings(
        _env_file=None,
        api_key="test",
        llm_tier3_api_key="test",
        data_dir=path,
        portfolio_ledger_enabled=True,
        account_fit_enabled=True,
        candidate_ranking_enabled=True,
    )


class _AvailableRules:
    async def resolve(self, listing_ref: ObjectRef, as_of: datetime) -> TrustedTradingRule:
        return make_trusted_rule(listing_ref)


async def seed_complete_screen_run(*, include_rows: bool = True) -> int:
    async with session_scope() as session:
        rows: list[tuple[int, str, int]] = []
        if include_rows:
            rows = [(600000, "hit", 1), (600001, "truncated", 2)]
        run = await PipelineRunRepository(session).create(
            kind="screen_run",
            triggered_by="test",
            status="success",
            stage="done",
            request_json={"as_of": "2026-10-15"},
        )
        counts = {"hit": 0, "truncated": 0, "miss": 0, "skipped": 0, "excluded": 0}
        for listing_id, outcome, rank in rows:
            listing = await session.get(Listing, listing_id)
            assert listing is not None
            counts[outcome] += 1
            session.add(
                ScreenResultRecord(
                    screen_run_id=run.id,
                    as_of=datetime(2026, 10, 15).date(),
                    template_id="rank_a",
                    template_version=1,
                    listing_id=listing_id,
                    ticker=listing.ticker,
                    outcome=outcome,
                    hit=outcome == "hit",
                    rank=rank,
                    strength=0.5,
                    reasons=[{"factor": "roe", "actual": 0.2}],
                    context={"provider": "synthetic_test"},
                )
            )
        run.finished_at = datetime.now(UTC) - timedelta(minutes=1)
        run.metadata_json = {
            "as_of": "2026-10-15",
            "templates": {"rank_a@1": {"counts": counts}},
            "result_rows": len(rows),
            "load_errors": {},
        }
        await session.flush()
        return run.id


async def seed_single_screen_run(listing_id: int) -> None:
    async with session_scope() as session:
        run = await PipelineRunRepository(session).create(
            kind="screen_run",
            triggered_by="test",
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
        listing = await session.get(Listing, listing_id)
        assert listing is not None
        session.add(
            ScreenResultRecord(
                screen_run_id=run.id,
                as_of=datetime(2026, 10, 15).date(),
                template_id="rank_a",
                template_version=1,
                listing_id=listing_id,
                ticker=listing.ticker,
                outcome="hit",
                hit=True,
                rank=1,
                strength=0.5,
                reasons=[{"factor": "roe", "actual": 0.2}],
                context={"provider": "synthetic_test"},
            )
        )


async def valid_request(*, operation_id: UUID | None = None) -> RankingRequest:
    async with session_scope() as session:
        context = await SuitabilityInputService(session).context(make_inputs().account_id)
    assert context.snapshot is not None and context.mandate_ref is not None
    return RankingRequest(
        operation_id=operation_id or uuid4(),
        account_id=context.account.id,
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


async def admit(request: RankingRequest, base: Settings) -> AdmissionReceipt:
    async with session_scope() as session:
        return await CandidateRankingAdmission(session, settings=base).accept(request)


async def run_worker(run_id: int, base: Settings) -> None:
    worker = ResearchWorker(
        settings=base,
        worker_id="candidate-ranking-test",
        handlers=[CandidateRankingHandler(settings=base)],
    )
    await worker.run_once(run_id=run_id)


@pytest.mark.asyncio
async def test_partial_holding_presence_is_unknown_and_complete_result_is_persisted(
    ranking_job_world: Path,
) -> None:
    await seed_complete_screen_run()
    base = settings(ranking_job_world)
    receipt = await admit(await valid_request(), base)
    async with session_scope() as session:
        admitted_run = await session.get(PipelineRun, int(receipt.run_ref.id))
        assert admitted_run is not None
        frozen_payload = admitted_run.request_json
        assert frozen_payload["ranking_policy_version"] == RANKING_POLICY_VERSION
        manifest_members = {
            int(member["listing_ref"]["id"])
            for member in frozen_payload["source_manifest"]["members"]
        }
        frozen_actions = {
            int(item["listing_ref"]["id"]) for item in frozen_payload["expanded_items"]
        }
        unknown_actions = set(frozen_payload["unknown_action_listing_ids"])
        assert frozen_actions.isdisjoint(unknown_actions)
        assert frozen_actions | unknown_actions == manifest_members
    await run_worker(int(receipt.run_ref.id), base)

    async with session_scope() as session:
        run = await session.get(PipelineRun, int(receipt.run_ref.id))
        assert run is not None and run.status == "success"
        ranking_ref = ObjectRef.model_validate(run.metadata_json["ranking_ref"])
        header = await session.scalar(select(InvestmentAssessment))
        assert header is not None and header.kind == "candidate_ranking"
        assert header.entry_count == 2
        stored = await InvestmentAssessmentRepository(session).read_ranking(
            "local_owner", ranking_ref
        )
        assert stored is not None
        assert stored.snapshot.ranking_policy_version == RANKING_POLICY_VERSION
        expected_hash = digest(
            {
                "request": admitted_run.request_json["client_request"],
                "frozen_inputs": admitted_run.request_json["frozen_inputs"],
                "source_manifest_hash": admitted_run.request_json["source_manifest_hash"],
                "ranking_policy_version": RANKING_POLICY_VERSION,
            }
        )
        assert stored.snapshot.input_hash == expected_hash
        entries = list(
            (
                await session.execute(
                    select(CandidateRankingEntry).order_by(CandidateRankingEntry.ordinal)
                )
            ).scalars()
        )
        assert len(entries) == 2
        for row in entries:
            assert row.payload["ranking_ref"] == ranking_ref.model_dump(mode="json")
            assert row.payload["financial_status"] == "unknown"
            assert row.payload["ranking_bucket"] == "incomplete"
            assert row.payload["resolved_action"] is None
            assert row.payload["action_unknown_reason"] == "holding_presence_unknown"
            assert row.payload["fit_ref"] is None
            assert row.payload["execution_error"] is None


@pytest.mark.asyncio
async def test_empty_source_batch_is_a_successful_zero_entry_ranking(
    ranking_job_world: Path,
) -> None:
    await seed_complete_screen_run(include_rows=False)
    base = settings(ranking_job_world)
    receipt = await admit(await valid_request(), base)
    await run_worker(int(receipt.run_ref.id), base)

    async with session_scope() as session:
        run = await session.get(PipelineRun, int(receipt.run_ref.id))
        assert run is not None and run.status == "success"
        ranking_ref = ObjectRef.model_validate(run.metadata_json["ranking_ref"])
        view = await read_ranking_view(
            session,
            make_inputs().account_id,
            ranking_ref,
            settings=base,
        )
        assert view.status == "available"
        assert view.snapshot is not None and view.snapshot.entry_count == 0
        assert view.entries == () and view.count == 0
        assert view.snapshot.coverage == "complete"
        assert view.current_limits is not None
        assert view.current_limits.current_status == "valid"
        with pytest.raises(InvestorError) as invalid_cursor:
            await read_ranking_view(
                session,
                make_inputs().account_id,
                ranking_ref,
                cursor="not-a-valid-cursor",
                settings=base,
            )
        assert invalid_cursor.value.detail.code == "invalid_cursor"
        assert await session.scalar(select(func.count()).select_from(CandidateRankingEntry)) == 0


@pytest.mark.asyncio
async def test_candidate_ranking_disabled_rejects_admission_before_creating_run(
    ranking_job_world: Path,
) -> None:
    base = settings(ranking_job_world).model_copy(update={"candidate_ranking_enabled": False})

    with pytest.raises(InvestorError) as disabled:
        await admit(await valid_request(), base)

    assert disabled.value.detail.code == "writes_disabled"
    async with session_scope() as session:
        assert await session.scalar(select(func.count()).select_from(PipelineRun)) == 0


@pytest.mark.asyncio
async def test_new_snapshot_links_previous_and_records_source_changes(
    ranking_job_world: Path,
) -> None:
    base = settings(ranking_job_world)
    await seed_complete_screen_run()
    first = await admit(await valid_request(), base)
    await run_worker(int(first.run_ref.id), base)
    first_run_id = int(first.run_ref.id)

    await seed_complete_screen_run()
    second = await admit(await valid_request(), base)
    await run_worker(int(second.run_ref.id), base)

    async with session_scope() as session:
        first_run = await session.get(PipelineRun, first_run_id)
        second_run = await session.get(PipelineRun, int(second.run_ref.id))
        assert first_run is not None and second_run is not None
        first_ref = ObjectRef.model_validate(first_run.metadata_json["ranking_ref"])
        second_ref = ObjectRef.model_validate(second_run.metadata_json["ranking_ref"])
        second_snapshot = await InvestmentAssessmentRepository(session).read_ranking(
            "local_owner", second_ref
        )
        assert second_snapshot is not None
        assert second_snapshot.snapshot.previous_ref == first_ref
        assert "candidate_sources_changed" in second_snapshot.snapshot.change_reasons


@pytest.mark.asyncio
async def test_historical_replay_is_not_selected_as_previous_live_ranking(
    ranking_job_world: Path,
) -> None:
    account_id = make_inputs().account_id
    live_id, replay_id = uuid4(), uuid4()
    async with session_scope() as session:
        live_run = await PipelineRunRepository(session).create(
            kind="candidate_rank", triggered_by="test", status="success",
            stage="done", request_json={"frozen_inputs": {"mode": "live"}},
        )
        replay_run = await PipelineRunRepository(session).create(
            kind="candidate_rank", triggered_by="test", status="success",
            stage="done", request_json={"frozen_inputs": {"mode": "historical_replay"}},
        )
        live_run.metadata_json = {"ranking_ref": {"id": str(live_id)}}
        replay_run.metadata_json = {"ranking_ref": {"id": str(replay_id)}}
        session.add_all(
            [
                InvestmentAssessment(
                    id=str(live_id), revision=1, owner_id="local_owner",
                    kind="candidate_ranking", account_id=str(account_id),
                    listing_id=None, as_of=datetime.now(UTC), input_hash="a" * 64,
                    entry_count=0, manifest_hash="b" * 64, universe_hash="c" * 64,
                    payload={"frozen_inputs": {"mode": "live"}},
                ),
                InvestmentAssessment(
                    id=str(replay_id), revision=1, owner_id="local_owner",
                    kind="candidate_ranking", account_id=str(account_id),
                    listing_id=None, as_of=datetime.now(UTC) + timedelta(days=1),
                    input_hash="d" * 64, entry_count=0, manifest_hash="e" * 64,
                    universe_hash="f" * 64,
                    payload={"frozen_inputs": {"mode": "historical_replay"}},
                ),
            ]
        )
        await session.flush()
        previous = await _previous_ranking_ref(session, account_id)

    assert previous == ObjectRef(kind="candidate_ranking", id=live_id, revision=1)


@pytest.mark.asyncio
async def test_old_ranking_job_without_policy_version_replays_as_v1(
    ranking_job_world: Path,
) -> None:
    await seed_complete_screen_run()
    base = settings(ranking_job_world)
    original_request = await valid_request()
    receipt = await admit(original_request, base)
    async with session_scope() as session:
        original = await session.get(PipelineRun, int(receipt.run_ref.id))
        assert original is not None
        legacy_payload = dict(original.request_json)
        legacy_payload.pop("ranking_policy_version")
        original.request_json = legacy_payload
        frozen = legacy_payload["frozen_inputs"]
        binding = HistoricalReplayBinding(
            run_ref=receipt.run_ref,
            manifest_hash=receipt.source_manifest_hash,
            input_as_of=datetime.fromisoformat(frozen["assessment_as_of"].replace("Z", "+00:00")),
        )

    replay_request = original_request.model_copy(
        update={
            "operation_id": uuid4(),
            "mode": "historical_replay",
            "historical_replay": binding,
        }
    )
    replay_receipt = await admit(replay_request, base)
    async with session_scope() as session:
        replay = await session.get(PipelineRun, int(replay_receipt.run_ref.id))
        assert replay is not None
        assert replay.request_json["ranking_policy_version"] == RANKING_POLICY_VERSION
        assert replay.request_json["frozen_inputs"]["mode"] == "historical_replay"


@pytest.mark.asyncio
async def test_worker_fails_unknown_ranking_policy_without_partial_artifacts(
    ranking_job_world: Path,
) -> None:
    await seed_complete_screen_run()
    base = settings(ranking_job_world)
    receipt = await admit(await valid_request(), base)
    async with session_scope() as session:
        run = await session.get(PipelineRun, int(receipt.run_ref.id))
        assert run is not None
        payload = dict(run.request_json)
        payload["ranking_policy_version"] = "future_v2"
        run.request_json = payload

    await run_worker(int(receipt.run_ref.id), base)
    async with session_scope() as session:
        run = await session.get(PipelineRun, int(receipt.run_ref.id))
        assert run is not None and run.status == "failed"
        assert run.error_summary == "ValueError"
        assert "ranking_ref" not in run.metadata_json
        assert await session.scalar(select(func.count()).select_from(InvestmentAssessment)) == 0


@pytest.mark.asyncio
async def test_ranking_ttl_ignores_not_applicable_rule_but_expires_for_unknown_check(
    known_holding_ranking_world: Path,
) -> None:
    await seed_single_screen_run(600001)
    base = settings(known_holding_ranking_world)
    request = await valid_request()
    rules = _AvailableRules()
    async with session_scope() as session:
        receipt = await CandidateRankingAdmission(
            session, settings=base, rules=rules
        ).accept(request)
    await run_worker(int(receipt.run_ref.id), base)

    async with session_scope() as session:
        run = await session.get(PipelineRun, int(receipt.run_ref.id))
        assert run is not None
        frozen = FrozenAccountInputs.model_validate(run.request_json["frozen_inputs"])
        manifest = SourceManifest.model_validate(run.request_json["source_manifest"])
        assert next(
            source for source in frozen.listing_sources if source.listing_ref.id == 600000
        ).rule_unknown_reason == "not_applicable"
        assert next(
            source for source in frozen.listing_sources if source.listing_ref.id == 600001
        ).trusted_rule is not None
        artifact = await InvestmentAssessmentRepository(session).read_ranking(
            "local_owner", ObjectRef.model_validate(run.metadata_json["ranking_ref"])
        )
        assert artifact is not None
        assert artifact.entries[0].financial_status == "pass"
        assert not _ranking_result_has_unknown(artifact.entries)

    later = manifest.as_of + timedelta(seconds=60)
    status, reasons = await _ranking_current_state(
        base,
        "local_owner",
        frozen,
        manifest,
        request.source_policy,
        now=later,
        rules=rules,
        unknown_result=_ranking_result_has_unknown(artifact.entries),
    )
    assert status == "valid"
    assert "assessment_expired" not in reasons

    blocked_with_unknown = artifact.entries[0].model_copy(
        update={
            "financial_status": "blocked",
            "ranking_bucket": "blocked",
            "checks": (FitCheck(code="missing_dependency", state="unknown", scope="baseline"),),
        }
    )
    assert _ranking_result_has_unknown((blocked_with_unknown,))
    status, reasons = await _ranking_current_state(
        base,
        "local_owner",
        frozen,
        manifest,
        request.source_policy,
        now=later,
        rules=rules,
        unknown_result=_ranking_result_has_unknown((blocked_with_unknown,)),
    )
    assert status == "stale"
    assert "assessment_expired" in reasons
