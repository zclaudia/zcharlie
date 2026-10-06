"""baostock breadth collectors (target pool): fixture replay, incremental windows,
suspension / ST flags, pubDate availability, failure isolation (P1-A7)."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from investment_agent.providers.baostock_market import BaostockMarketClient
from investment_agent.research.worker import ResearchWorker
from investment_agent.snapshots.breadth import (
    MAX_CONSECUTIVE_TRANSIENT,
    collect_baostock_financial,
    collect_baostock_market,
)
from investment_agent.snapshots.collect import ArtifactWriter, SnapshotPreconditionError
from investment_agent.snapshots.jobs import (
    FinancialSnapshotPayload,
    MarketSnapshotPayload,
    enqueue_snapshot_job,
)
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.pit_snapshot import FinancialSnapshot, MarketSnapshot
from investment_agent.storage.repositories.financial_snapshot import FinancialSnapshotRepository
from investment_agent.storage.repositories.market_snapshot import MarketSnapshotRepository
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository
from tests.unit._baostock_fake import FakeBaostockData
from tests.unit._snapshot_support import (
    CAPTURED_AT,
    FakeProxy,
    handlers_for,
    seed_universe,
    seed_watchlist,
    settings_for,
)

TRADE_DATE = date(2026, 9, 30)
POOL = ["000001.SZ", "000016.SZ", "002813.SZ", "600519.SH", "601059.SH"]


def _client(backend: FakeBaostockData, **kwargs) -> BaostockMarketClient:
    kwargs.setdefault("retry_backoff_seconds", 0)
    return BaostockMarketClient(backend, clock=lambda: CAPTURED_AT, **kwargs)  # type: ignore[arg-type]


def _market(trade_date: date = TRADE_DATE, **kwargs) -> MarketSnapshotPayload:
    kwargs.setdefault("universe_code", "CSI300")
    return MarketSnapshotPayload(trade_date=trade_date, **kwargs)


def _financial(period: date, as_of: date = date(2026, 10, 6)) -> FinancialSnapshotPayload:
    return FinancialSnapshotPayload(universe_code="CSI300", report_period=period, as_of=as_of)


async def _collect_market(backend, tmp_path, payload=None, **client_kwargs):
    return await collect_baostock_market(
        payload or _market(),
        client=_client(backend, **client_kwargs),
        artifacts=ArtifactWriter(tmp_path, run_id=1, attempt=1),
        lease=None,
        now=lambda: CAPTURED_AT,
    )


async def _collect_financial(backend, tmp_path, period: date, **client_kwargs):
    return await collect_baostock_financial(
        _financial(period),
        client=_client(backend, **client_kwargs),
        artifacts=ArtifactWriter(tmp_path, run_id=2, attempt=1),
        lease=None,
        now=lambda: CAPTURED_AT,
    )


async def _market_rows(adjust: str | None = None) -> list[MarketSnapshot]:
    async with session_scope() as session:
        stmt = select(MarketSnapshot).order_by(MarketSnapshot.listing_id, MarketSnapshot.trade_date)
        if adjust is not None:
            stmt = stmt.where(MarketSnapshot.adjust == adjust)
        return list((await session.execute(stmt)).scalars().all())


async def _financial_rows() -> list[FinancialSnapshot]:
    async with session_scope() as session:
        stmt = select(FinancialSnapshot).order_by(FinancialSnapshot.listing_id)
        return list((await session.execute(stmt)).scalars().all())


async def _seed_bar(listing_id: int, day: date, provider: str) -> None:
    async with session_scope() as session:
        repository = MarketSnapshotRepository(session)
        for adjust in ("none", "backward"):
            await repository.insert(
                listing_id=listing_id, trade_date=day, open=Decimal(1), high=Decimal(1),
                low=Decimal(1), close=Decimal(1), volume=Decimal(1), adjust=adjust,
                provider=provider, captured_at=CAPTURED_AT - timedelta(days=10),
            )


# --- market ---------------------------------------------------------------------


async def test_market_fixture_replay_stores_both_series_with_valuation(db_session, tmp_path):
    del db_session
    ids = await seed_universe(POOL)
    backend = FakeBaostockData()

    report = await _collect_market(backend, tmp_path)

    assert backend.logins == 1  # one session for the whole run
    kdata = backend.methods("query_history_k_data_plus")
    assert len(kdata) == 2 * len(POOL)
    assert {p["adjustflag"] for p in kdata} == {"3", "1"}
    assert all(p["start_date"] == p["end_date"] == "2026-09-30" for p in kdata)

    raw = {r.listing_id: r for r in await _market_rows("none")}
    backward = {r.listing_id: r for r in await _market_rows("backward")}
    assert len(raw) == len(backward) == len(POOL)
    moutai = raw[ids["600519.SH"]]
    assert moutai.provider == "baostock"
    assert moutai.close == Decimal("1258.62")
    assert moutai.pe_ttm == Decimal("19.320898")
    assert moutai.pb_mrq == Decimal("6.26211")
    assert moutai.ps_ttm == Decimal("9.082149")
    assert moutai.turnover == Decimal("0.003066")
    assert moutai.trade_status == 1 and moutai.is_st is False
    assert moutai.total_shares is None  # market cap uses financial total_shares
    assert moutai.raw_ref is not None and (tmp_path / moutai.raw_ref).is_file()
    assert backward[ids["600519.SH"]].close == Decimal("9652.680245")  # Numeric(18, 6)

    suspended = raw[ids["601059.SH"]]
    assert suspended.trade_status == 0 and suspended.volume == 0 and suspended.turnover is None
    st = raw[ids["000016.SZ"]]
    assert st.is_st is True and st.trade_status == 0

    assert report.status == "success"
    assert report.completeness.ratio == 1.0
    flags = report.metadata["flags"]
    assert flags["suspended"] == ["000016.SZ", "002813.SZ", "601059.SH"]
    assert flags["st"] == ["000016.SZ"]
    assert report.metadata["rows_inserted"] == {"none": 5, "backward": 5}
    assert report.metadata["provider"] == "baostock"


async def test_market_is_incremental_per_provider(db_session, tmp_path):
    del db_session
    ids = await seed_universe(["600519.SH", "000001.SZ"])
    await _seed_bar(ids["600519.SH"], date(2026, 9, 25), "baostock")
    # iFinD rows never move the baostock cursor.
    await _seed_bar(ids["000001.SZ"], date(2026, 9, 30), "ifind")
    backend = FakeBaostockData()

    await _collect_market(backend, tmp_path)
    starts = {
        (p["code"], p["adjustflag"]): p["start_date"]
        for p in backend.methods("query_history_k_data_plus")
    }
    assert starts == {
        ("sh.600519", "3"): "2026-09-26",
        ("sh.600519", "1"): "2026-09-26",
        ("sz.000001", "3"): "2026-09-30",
        ("sz.000001", "1"): "2026-09-30",
    }
    moutai = [r for r in await _market_rows("none") if r.listing_id == ids["600519.SH"]]
    assert [r.trade_date for r in moutai] == [
        date(2026, 9, 25), date(2026, 9, 28), date(2026, 9, 29), date(2026, 9, 30)
    ]

    again = FakeBaostockData()
    report = await _collect_market(again, tmp_path)
    assert again.methods("query_history_k_data_plus") == []
    assert report.completeness.ratio == 1.0


async def test_listing_without_a_bar_is_no_data(db_session, tmp_path):
    del db_session
    await seed_universe(["001248.SZ", "600519.SH"])
    report = await _collect_market(FakeBaostockData(), tmp_path, _market(date(2026, 6, 30)))
    # 001248 listed on 2026-07-02; 600519 is outside the recorded window.
    assert report.completeness.tickers("no_data") == ["001248.SZ", "600519.SH"]
    assert report.status == "success"
    assert await _market_rows() == []


async def test_one_failing_listing_does_not_fail_the_job(db_session, tmp_path):
    del db_session
    await seed_universe(["600519.SH", "000001.SZ"])
    backend = FakeBaostockData(
        fail=lambda method, params: ("10004011", "bad code")
        if params.get("code") == "sz.000001"
        else None
    )
    report = await _collect_market(backend, tmp_path)
    assert report.completeness.failed == ["000001.SZ"]
    assert report.completeness.collected == {"600519.SH"}
    assert report.status == "partial"
    assert not report.all_failures_transient


async def test_consecutive_transient_failures_stop_calling_baostock(db_session, tmp_path):
    del db_session
    tickers = [f"60000{i}.SH" for i in range(8)]
    await seed_universe(tickers)
    backend = FakeBaostockData(fail=lambda method, params: OSError("connection reset"))
    report = await _collect_market(backend, tmp_path, max_attempts=1)
    assert len(backend.methods("query_history_k_data_plus")) == MAX_CONSECUTIVE_TRANSIENT
    assert report.completeness.failed == sorted(tickers)
    assert report.status == "failed"
    assert report.all_failures_transient
    assert "skipped: baostock unavailable" in report.completeness.missing[tickers[-1]][1]
    assert report.failure().provider == "baostock"


async def test_watchlist_scope_with_baostock(db_session, tmp_path):
    del db_session
    await seed_watchlist(["600519.SH"])
    report = await _collect_market(
        FakeBaostockData(), tmp_path, _market(scope="watchlist", universe_code=None)
    )
    assert report.completeness.collected == {"600519.SH"}


async def test_empty_universe_is_a_precondition_error(db_session, tmp_path):
    del db_session
    with pytest.raises(SnapshotPreconditionError):
        await _collect_market(FakeBaostockData(), tmp_path)


# --- financial --------------------------------------------------------------------


async def test_financial_fixture_replay_uses_pubdate(db_session, tmp_path):
    del db_session
    ids = await seed_universe(["600519.SH", "000001.SZ", "300750.SZ", "601398.SH"])
    backend = FakeBaostockData()

    report = await _collect_financial(backend, tmp_path, date(2026, 6, 30))

    rows = {row.listing_id: row for row in await _financial_rows()}
    assert set(rows) == {ids["600519.SH"], ids["000001.SZ"], ids["300750.SZ"]}
    moutai = rows[ids["600519.SH"]]
    assert moutai.provider == "baostock"
    assert moutai.availability == "actual"
    assert moutai.available_from == date(2026, 8, 15)
    assert moutai.period_kind == "semi"
    assert moutai.fields["roe"] == pytest.approx(0.179543)
    assert moutai.fields["total_shares"] == pytest.approx(1250081601.0)
    assert moutai.fields["source_values"]["profit.roeAvg"] == pytest.approx(0.179543)
    assert moutai.fields["pub_date"] == "2026-08-15"
    assert rows[ids["300750.SZ"]].available_from == date(2026, 7, 25)
    assert "gross_margin" in rows[ids["000001.SZ"]].fields["missing_fields"]
    assert moutai.raw_ref is not None and (tmp_path / moutai.raw_ref).is_file()
    # 601398 2026H1 is not in the recording -> answers like an undisclosed period
    assert report.completeness.pending == ["601398.SH"]
    assert report.status == "success" and report.completeness.ratio == 1.0
    assert report.metadata["pub_dates"]["600519.SH"] == "2026-08-15"

    again = FakeBaostockData()
    await _collect_financial(again, tmp_path, date(2026, 6, 30))
    assert [p["code"] for p in again.methods("query_profit_data")] == ["sh.601398"]


async def test_undisclosed_period_queries_only_the_profit_table(db_session, tmp_path):
    del db_session
    await seed_universe(["600519.SH"])
    backend = FakeBaostockData()
    report = await _collect_financial(backend, tmp_path, date(2026, 9, 30))
    assert report.completeness.pending == ["600519.SH"]
    assert [m for m, _ in backend.calls if m.startswith("query_")] == ["query_profit_data"]
    assert await _financial_rows() == []


async def test_ifind_rows_do_not_count_as_baostock_coverage(db_session, tmp_path):
    del db_session
    ids = await seed_universe(["600519.SH"])
    async with session_scope() as session:
        await FinancialSnapshotRepository(session).insert(
            listing_id=ids["600519.SH"], report_period=date(2026, 6, 30), period_kind="semi",
            fields={"roe": 0.18}, provider="ifind", captured_at=CAPTURED_AT,
            availability="approx", available_from=date(2026, 10, 6),
        )
    await _collect_financial(FakeBaostockData(), tmp_path, date(2026, 6, 30))
    rows = await _financial_rows()
    assert sorted((r.provider, r.version) for r in rows) == [("baostock", 2), ("ifind", 1)]


async def test_total_shares_follow_pubdate(db_session, tmp_path):
    del db_session
    ids = await seed_universe(["600519.SH"])
    await _collect_financial(FakeBaostockData(), tmp_path, date(2025, 12, 31))
    await _collect_financial(FakeBaostockData(), tmp_path, date(2026, 6, 30))
    listing_id = ids["600519.SH"]
    async with session_scope() as session:
        repository = FinancialSnapshotRepository(session)
        live = await repository.total_shares_as_of(listing_id, date(2026, 10, 6))
        # Live captures are unknown before their capture date (PIT knowledge).
        unknown = await repository.total_shares_as_of(listing_id, date(2026, 10, 5))
        # A backfilled history is gated by available_from (= pubDate) only.
        for period, kind, pub, shares in (
            (date(2025, 12, 31), "annual", date(2026, 4, 17), 1252270215.0),
            (date(2026, 6, 30), "semi", date(2026, 8, 15), 1250081601.0),
        ):
            await repository.insert(
                listing_id=listing_id, report_period=period, period_kind=kind,
                fields={"total_shares": shares}, provider="baostock", captured_at=CAPTURED_AT,
                availability="actual", available_from=pub, is_backfill=True,
            )
        before = await repository.total_shares_as_of(listing_id, date(2026, 8, 14))
        after = await repository.total_shares_as_of(listing_id, date(2026, 8, 15))
        nothing = await repository.total_shares_as_of(listing_id, date(2026, 4, 16))
        ifind_only = await repository.total_shares_as_of(
            listing_id, date(2026, 10, 6), provider="ifind"
        )
    assert live is not None and live[0] == pytest.approx(1250081601.0)
    assert unknown is None
    assert before is not None and before[0] == pytest.approx(1252270215.0)
    assert before[1].report_period == date(2025, 12, 31)
    assert after is not None and after[0] == pytest.approx(1250081601.0)
    assert nothing is None
    assert ifind_only is None


# --- through the worker -----------------------------------------------------------


async def test_jobs_route_to_baostock_without_touching_the_proxy(db_session, tmp_path):
    del db_session
    await seed_universe(["600519.SH", "000001.SZ"])
    settings = settings_for(tmp_path)
    proxy = FakeProxy()
    backend = FakeBaostockData()
    async with session_scope() as session:
        market, _ = await enqueue_snapshot_job(session, _market(), triggered_by="test")
        financial, _ = await enqueue_snapshot_job(
            session, _financial(date(2026, 6, 30)), triggered_by="test"
        )
        ids = (market.id, financial.id)
    worker = ResearchWorker(
        settings=settings,
        worker_id="w",
        handlers=handlers_for(settings, proxy, baostock=_client(backend)),
    )
    assert await worker.run_once() == ids[0]
    assert await worker.run_once() == ids[1]
    assert proxy.calls == []
    async with session_scope() as session:
        runs = [await PipelineRunRepository(session).get(run_id) for run_id in ids]
    assert [run.status for run in runs] == ["success", "success"]
    assert runs[0].metadata_json["provider"] == "baostock"
    assert runs[0].metadata_json["completeness"]["collected"] == 2
    assert runs[1].metadata_json["stored"] == 2
    assert list((tmp_path / "notifications").rglob("*.md")) == []
