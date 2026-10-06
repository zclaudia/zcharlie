"""financial_snapshot collector: registry-driven fields, disclosure, idempotency."""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from investment_agent.research.ifind_mapping import IFIND_FIELD_MAPPINGS
from investment_agent.research.worker import ResearchWorker
from investment_agent.snapshots.collect import ArtifactWriter, ProxyBatchCaller
from investment_agent.snapshots.financial import (
    canonical_financial_fields,
    collect_financial_snapshot,
    financial_endpoints,
)
from investment_agent.snapshots.jobs import (
    FinancialSnapshotPayload,
    MarketSnapshotPayload,
    enqueue_snapshot_job,
    idempotency_key,
    period_kind,
)
from investment_agent.snapshots.throttle import AsyncRateLimiter
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.pit_snapshot import FinancialSnapshot
from investment_agent.storage.repositories.pipeline_run import (
    IdempotencyPayloadMismatchError,
    PipelineRunRepository,
)
from tests.unit._snapshot_support import (
    CAPTURED_AT,
    FakeProxy,
    FixtureProxy,
    NoSleep,
    handlers_for,
    seed_universe,
    settings_for,
)

TICKERS = ["000001.SZ", "300750.SZ", "600519.SH"]


async def _collect(proxy, tmp_path, period: date, *, as_of: date = date(2026, 10, 6)):
    return await collect_financial_snapshot(
        FinancialSnapshotPayload(universe_code="CSI300", report_period=period, as_of=as_of),
        caller=ProxyBatchCaller(proxy, AsyncRateLimiter(6000, burst=1000), sleep=NoSleep()),
        artifacts=ArtifactWriter(tmp_path, run_id=7, attempt=1),
        lease=None,
        now=lambda: CAPTURED_AT,
    )


async def _rows() -> list[FinancialSnapshot]:
    async with session_scope() as session:
        stmt = select(FinancialSnapshot).order_by(FinancialSnapshot.listing_id)
        return list((await session.execute(stmt)).scalars().all())


def test_endpoints_come_from_the_mapping_registry():
    keys = [endpoint.key for endpoint in financial_endpoints()]
    assert sorted(keys) == sorted(
        [
            "/financial/statements:income_statement",
            "/financial/statements:cash_flow",
            "/financial/index:profitability",
            "/financial/index:capital_structure",
        ]
    )
    cited = {
        part.strip()
        for mapping in IFIND_FIELD_MAPPINGS
        for part in mapping.endpoint.split("+")
        if part.strip().startswith("/financial/")
    }
    assert set(keys) == cited
    payload = financial_endpoints()[0].payload(["600519.SH"], date(2026, 6, 30))
    assert payload["report_date"] == "20260630"


def test_period_kind_from_month_day():
    assert period_kind(date(2026, 3, 31)) == "q1"
    assert period_kind(date(2026, 6, 30)) == "semi"
    assert period_kind(date(2026, 9, 30)) == "q3"
    assert period_kind(date(2025, 12, 31)) == "annual"
    with pytest.raises(ValueError):
        period_kind(date(2026, 6, 29))
    with pytest.raises(ValidationError):
        FinancialSnapshotPayload(
            universe_code="CSI300", report_period=date(2026, 5, 31), as_of=date(2026, 10, 6)
        )


def test_blank_rows_mean_not_disclosed():
    assert canonical_financial_fields({"/financial/index:profitability": {"ths_roe_stock": ""}}) is None


async def test_fixture_replay_stores_canonical_fields(db_session, tmp_path):
    del db_session
    ids = await seed_universe(TICKERS)
    proxy = FixtureProxy()

    report = await _collect(proxy, tmp_path, date(2026, 6, 30))

    assert len(proxy.calls) == 4  # four endpoints x one 3-ticker batch
    assert all(len(p["ticker"].split(",")) == 3 for _, p in proxy.calls)
    rows = {row.listing_id: row for row in await _rows()}
    assert set(rows) == set(ids.values())
    moutai = rows[ids["600519.SH"]]
    assert moutai.period_kind == "semi"
    assert moutai.report_period == date(2026, 6, 30)
    assert moutai.availability == "approx"
    assert moutai.available_from == date(2026, 10, 6)  # capture date in Asia/Shanghai
    assert moutai.is_backfill is False
    fields = moutai.fields
    assert fields["revenue"] == pytest.approx(92278072083.21)
    assert fields["net_profit"] == pytest.approx(44516880421.86)
    assert fields["gross_margin"] == pytest.approx(0.895552)
    assert fields["roe"] == pytest.approx(0.179543)
    assert fields["debt_ratio"] == pytest.approx(0.151931)
    assert fields["free_cash_flow"] == pytest.approx(70690750119.06 - 832142752.28)
    assert fields["source_values"]["ths_eps_basic_stock"] == pytest.approx(35.57)
    assert set(fields["multi_period_fields"]) == {
        "roe_stability",
        "revenue_growth_3y",
        "net_profit_growth_3y",
    }
    bank = rows[ids["000001.SZ"]].fields
    assert bank["gross_margin"] is None
    assert "gross_margin" in bank["missing_fields"]
    assert moutai.raw_ref is not None and (tmp_path / moutai.raw_ref).is_file()
    assert report.status == "success"
    assert report.completeness.ratio == 1.0


async def test_undisclosed_period_is_pending_not_an_error(db_session, tmp_path):
    del db_session
    await seed_universe(TICKERS)
    report = await _collect(FixtureProxy(), tmp_path, date(2026, 9, 30))
    assert await _rows() == []
    assert report.completeness.pending == TICKERS
    assert report.completeness.failed == []
    assert report.status == "success"
    assert report.to_metadata()["completeness"]["missing"]["600519.SH"]["kind"] == "pending"


async def test_only_members_without_the_period_are_requested(db_session, tmp_path):
    del db_session
    await seed_universe(TICKERS)
    await _collect(FixtureProxy(), tmp_path, date(2026, 6, 30))
    proxy = FixtureProxy()
    report = await _collect(proxy, tmp_path, date(2026, 6, 30))
    assert proxy.calls == []
    assert report.completeness.ratio == 1.0
    assert report.metadata["already_present"] == 3
    assert len(await _rows()) == 3


async def test_failed_batch_marks_its_tickers_missing(db_session, tmp_path):
    del db_session
    tickers = ["000001.SZ", "000002.SZ", "000003.SZ", "600000.SH"]
    await seed_universe(tickers)
    profit = {t: {"ths_roe_stock": "10", "ths_gross_selling_rate_stock": "30"} for t in tickers}

    def fail(path, payload):
        if "600000.SH" in payload["ticker"] and payload.get("statement") == "cash_flow":
            return ValueError("bad csv")
        return None

    proxy = FakeProxy(
        financial={("/financial/index:profitability", "20260630"): profit}, fail=fail
    )
    report = await _collect(proxy, tmp_path, date(2026, 6, 30))
    assert report.completeness.failed == ["600000.SH"]
    assert len(report.completeness.collected) == 3
    assert report.status == "partial"
    assert len(await _rows()) == 3


async def test_financial_job_runs_through_worker(db_session, tmp_path):
    del db_session
    await seed_universe(TICKERS)
    settings = settings_for(tmp_path)
    async with session_scope() as session:
        run, created = await enqueue_snapshot_job(
            session,
            FinancialSnapshotPayload(
                universe_code="CSI300", report_period=date(2026, 6, 30), as_of=date(2026, 10, 6)
            ),
            triggered_by="test",
        )
        run_id = run.id
    assert created
    worker = ResearchWorker(
        settings=settings, worker_id="w", handlers=handlers_for(settings, FixtureProxy())
    )
    assert await worker.run_once() == run_id
    async with session_scope() as session:
        run = await PipelineRunRepository(session).get(run_id)
    assert run.status == "success"
    assert run.universe_code == "CSI300"
    assert run.metadata_json["completeness"]["collected"] == 3
    assert run.metadata_json["stored"] == 3


async def test_enqueue_is_idempotent_per_kind_universe_date(db_session):
    del db_session
    payload = MarketSnapshotPayload(universe_code="CSI300", trade_date=date(2026, 9, 30))
    assert idempotency_key(payload) == "market_snapshot:CSI300:2026-09-30"
    financial = FinancialSnapshotPayload(
        universe_code="CSI300", report_period=date(2026, 6, 30), as_of=date(2026, 10, 10)
    )
    assert idempotency_key(financial) == "financial_snapshot:CSI300:2026-06-30:2026-10-10"
    async with session_scope() as session:
        first, created_first = await enqueue_snapshot_job(session, payload, triggered_by="a")
        second, created_second = await enqueue_snapshot_job(session, payload, triggered_by="b")
        assert (created_first, created_second) == (True, False)
        assert first.id == second.id
        with pytest.raises(IdempotencyPayloadMismatchError):
            await enqueue_snapshot_job(
                session,
                MarketSnapshotPayload(universe_code="CSI300", trade_date=date(2026, 10, 1)),
                triggered_by="c",
                key="market_snapshot:CSI300:2026-09-30",
            )
