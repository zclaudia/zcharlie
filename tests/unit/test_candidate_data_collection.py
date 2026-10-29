"""Manual non-index stocks get initial data before drafting and stay in live collection."""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from investment_agent.candidates.lifecycle import CandidatePolicy, nominate_candidate
from investment_agent.providers.baostock_market import BaostockMarketClient
from investment_agent.research.worker import ResearchWorker
from investment_agent.rules.drafting import RuleDraftHandler
from investment_agent.snapshots.backfill import report_periods
from investment_agent.snapshots.bootstrap import collect_stock_bootstrap
from investment_agent.snapshots.breadth import collect_baostock_market
from investment_agent.snapshots.collect import ArtifactWriter
from investment_agent.snapshots.eastmoney import akshare_scope
from investment_agent.snapshots.handlers import snapshot_job_handlers
from investment_agent.snapshots.jobs import (
    STOCK_BOOTSTRAP,
    DividendSnapshotPayload,
    FinancialSnapshotPayload,
    MarketSnapshotPayload,
    StockBootstrapPayload,
)
from investment_agent.snapshots.scope import collection_members
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.candidates import CandidateRecord
from investment_agent.storage.models.pit_snapshot import FinancialSnapshot, MarketSnapshot
from investment_agent.storage.models.reference import UniverseMembership
from investment_agent.storage.models.user_domain import HoldingItem
from investment_agent.storage.repositories.listing import ListingRepository
from investment_agent.storage.repositories.watchlist import WatchlistRepository
from tests.unit._baostock_history_fake import FakeBaostockHistory
from tests.unit._candidate_support import TEMPLATE_ID, cand_db, seed_template  # noqa: F401
from tests.unit._snapshot_support import CAPTURED_AT, runtime_for, seed_universe, settings_for

pytestmark = pytest.mark.usefixtures("cand_db")
TICKER = "002772.SZ"
AS_OF = date(2026, 10, 6)
POLICY = CandidatePolicy(expiry_days=14, dismiss_cooldown_days=30)


def _runtime(tmp_path, backend):
    client = BaostockMarketClient(
        backend, clock=lambda: CAPTURED_AT, retry_backoff_seconds=0,
    )
    return dataclasses.replace(
        runtime_for(settings_for(tmp_path), None, reference=client),
        backfill_factory=lambda: client,
    )


def _backend():
    return FakeBaostockHistory(
        ipo={TICKER: date(2015, 6, 26)},
        industry={TICKER: [(date(2020, 1, 1), "A01农业")]},
        financials={
            (TICKER, p): p + timedelta(days=60)
            for p in report_periods(AS_OF, annual_years=6, quarters=4)
        },
    )


async def _nominate(template=True):
    async with session_scope() as session:
        if template:
            await seed_template(session)
        return await nominate_candidate(
            session, ticker=TICKER, policy=POLICY, as_of=AS_OF,
            template_id=TEMPLATE_ID if template else None,
        )


class InspectingLLM:
    def __init__(self):
        self.inputs = []

    async def complete(self, *, user_message, output_model, **kwargs):
        data = json.loads(user_message)
        # Use the actual DB-backed factor loader; the model must never receive
        # the original all-null context from a newly created manual listing.
        assert data["factors"]["roe"]["value"] is not None
        assert data["factors"]["pb_percentile_5y"]["value"] is not None
        assert data["stock"]["industry"]["csrc"]["code"] == "A01"
        self.inputs.append(data)
        return SimpleNamespace(
            model="offline", output=output_model.model_validate({
                "params": {}, "overall_rationale": "已采集历史资料，沿用模板默认参数，待后续核对。",
            }),
        )


async def test_manual_stock_collects_before_model_and_reuses_stored_history(tmp_path):
    nomination = await _nominate()
    backend = _backend()
    runtime = _runtime(tmp_path, backend)
    llm = InspectingLLM()
    worker = ResearchWorker(
        settings=runtime.settings, worker_id="test-bootstrap",
        handlers=[RuleDraftHandler(
            settings=runtime.settings, snapshot_runtime=runtime, client_factory=lambda _: llm,
        )],
    )
    await worker.run_once(run_id=nomination.draft_run_id)
    async with session_scope() as session:
        run = await session.get(PipelineRun, nomination.draft_run_id)
        assert run.status == "success", run.error_summary
        assert run.request_json["prepare_data"] is True
        assert set(run.metadata_json["data_preparation"]["parts"]) == {
            "industry", "prices", "financials", "dividends", "segments",
        }
        assert await session.scalar(select(func.count()).select_from(MarketSnapshot)) > 2000
        assert await session.scalar(select(func.count()).select_from(FinancialSnapshot)) >= 6
        assert await session.scalar(select(func.count()).select_from(UniverseMembership)) == 0
        candidate = await session.get(CandidateRecord, nomination.candidate.id)
        assert candidate.draft_state == "ready"
    assert len(llm.inputs) == 1
    assert all(p.get("code", "sz.002772") == "sz.002772" for _, p in backend.calls)
    assert not backend.methods("query_hs300_stocks")
    backend.calls.clear()
    await collect_stock_bootstrap(
        StockBootstrapPayload(ticker=TICKER, as_of=AS_OF), runtime=runtime,
        artifacts=ArtifactWriter(tmp_path, run_id=99, attempt=1), lease=None,
    )
    assert not backend.methods("query_history_k_data_plus")
    assert not backend.methods("query_profit_data")
    assert not backend.methods("query_dividend_data")


async def test_collection_failure_retries_before_any_model_call(tmp_path):
    nomination = await _nominate()
    backend = _backend()
    backend.fail = lambda method, _: (
        ("10002001", "network unavailable") if method == "query_history_k_data_plus" else None
    )
    runtime = _runtime(tmp_path, backend)
    llm = InspectingLLM()
    worker = ResearchWorker(
        settings=runtime.settings, worker_id="test-bootstrap-retry", retry_base_delay_seconds=0,
        handlers=[RuleDraftHandler(
            settings=runtime.settings, snapshot_runtime=runtime, client_factory=lambda _: llm,
        )],
    )
    await worker.run_once(run_id=nomination.draft_run_id)
    async with session_scope() as session:
        run = await session.get(PipelineRun, nomination.draft_run_id)
        assert run.status == "queued", run.error_summary
        assert run.metadata_json["data_preparation"]["parts"]["prices"]["completeness"]["failed_count"] == 1
    assert llm.inputs == []
    backend.fail = None
    await worker.run_once(run_id=nomination.draft_run_id)
    async with session_scope() as session:
        run = await session.get(PipelineRun, nomination.draft_run_id)
        assert run.status == "success", run.error_summary
    assert len(llm.inputs) == 1


async def test_nomination_without_template_queues_idempotent_data_job(tmp_path):
    nomination = await _nominate(template=False)
    await _nominate(template=False)
    assert nomination.draft_run_id is None
    async with session_scope() as session:
        runs = list((await session.execute(select(PipelineRun))).scalars())
        assert len(runs) == 1 and runs[0].kind == STOCK_BOOTSTRAP
        run_id = runs[0].id
    runtime = _runtime(tmp_path, _backend())
    worker = ResearchWorker(
        settings=runtime.settings, worker_id="test-bootstrap-no-template",
        handlers=snapshot_job_handlers(runtime.settings, runtime),
    )
    await worker.run_once(run_id=run_id)
    async with session_scope() as session:
        run = await session.get(PipelineRun, run_id)
        assert run.status == "success", run.error_summary
        assert await session.scalar(select(func.count()).select_from(FinancialSnapshot)) >= 6


async def test_free_live_collection_includes_pools_without_changing_index(tmp_path):
    await seed_universe(["600519.SH"])
    nomination = await _nominate(template=False)
    async with session_scope() as session:
        listings = ListingRepository(session)
        watched = await listings.ensure_a_share_listing("000001.SZ")
        disabled = await listings.ensure_a_share_listing("000002.SZ")
        held = await listings.ensure_a_share_listing("300750.SZ")
        await WatchlistRepository(session).add(watched.id)
        await WatchlistRepository(session).add(disabled.id, enabled=False)
        session.add(HoldingItem(
            listing_id=held.id, shares=Decimal(100), cost_basis=Decimal(10), opened_at=AS_OF,
        ))
    expected = {"600519.SH", TICKER, "000001.SZ", "300750.SZ"}
    async with session_scope() as session:
        for payload in [
            MarketSnapshotPayload(universe_code="CSI300", trade_date=AS_OF),
            FinancialSnapshotPayload(universe_code="CSI300", report_period=date(2025, 12, 31), as_of=AS_OF),
            DividendSnapshotPayload(universe_code="CSI300", as_of=AS_OF),
        ]:
            assert set(await collection_members(session, payload, AS_OF)) == expected
        financial = FinancialSnapshotPayload(
            provider="akshare", universe_code="CSI300", report_period=date(2025, 12, 31), as_of=AS_OF,
        )
        assert set(await akshare_scope(session, financial)) == expected
        limited = MarketSnapshotPayload(provider="ifind", scope="watchlist", trade_date=AS_OF)
        assert set(await collection_members(session, limited, AS_OF)) == {"000001.SZ"}
        assert await session.scalar(select(func.count()).select_from(UniverseMembership)) == 1
        candidate = await session.get(CandidateRecord, nomination.candidate.id)
        candidate.status = "dismissed"
        candidate.closed_at = datetime.now(UTC)
    async with session_scope() as session:
        assert TICKER not in await collection_members(session, payload, AS_OF)


async def test_scheduled_market_actually_fetches_non_index_candidate(tmp_path):
    await seed_universe(["600519.SH"])
    await _nominate(template=False)
    backend = _backend()
    runtime = _runtime(tmp_path, backend)
    report = await collect_baostock_market(
        MarketSnapshotPayload(universe_code="CSI300", trade_date=date(2026, 9, 30)),
        client=runtime.backfill_factory(), artifacts=ArtifactWriter(tmp_path, run_id=1, attempt=1),
        lease=None, now=lambda: CAPTURED_AT,
    )
    assert report.status == "success"
    assert TICKER in report.completeness.collected
    assert any(p["code"] == "sz.002772" for p in backend.methods("query_history_k_data_plus"))
