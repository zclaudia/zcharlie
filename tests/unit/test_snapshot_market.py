"""iFinD market_snapshot collector (watchlist scope): batching, incremental
windows, both series, total shares."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select

from investment_agent.research.worker import ResearchWorker
from investment_agent.snapshots.collect import (
    ArtifactWriter,
    ProxyBatchCaller,
    SnapshotPreconditionError,
    chunked,
)
from investment_agent.snapshots.jobs import MarketSnapshotPayload, enqueue_snapshot_job
from investment_agent.snapshots.market import (
    HistoryRequest,
    collect_market_snapshot,
    history_windows,
    plan_history_requests,
)
from investment_agent.snapshots.throttle import AsyncRateLimiter
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.pit_snapshot import MarketSnapshot
from investment_agent.storage.repositories.market_snapshot import MarketSnapshotRepository
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository
from tests.unit._snapshot_support import (
    CAPTURED_AT,
    FakeProxy,
    FixtureProxy,
    NoSleep,
    handlers_for,
    seed_watchlist,
    settings_for,
)

TICKERS = ["000001.SZ", "300750.SZ", "600519.SH"]


def _caller(proxy, *, retries: int = 2) -> ProxyBatchCaller:
    return ProxyBatchCaller(
        proxy, AsyncRateLimiter(6000, burst=1000), retries=retries, sleep=NoSleep()
    )


def _payload(trade_date: date) -> MarketSnapshotPayload:
    return MarketSnapshotPayload(provider="ifind", scope="watchlist", trade_date=trade_date)


async def _collect(proxy, tmp_path, trade_date: date, **kwargs):
    return await collect_market_snapshot(
        _payload(trade_date),
        caller=_caller(proxy, **kwargs),
        artifacts=ArtifactWriter(tmp_path, run_id=1, attempt=1),
        lease=None,
        now=lambda: CAPTURED_AT,
    )


async def _rows(adjust: str | None = None) -> list[MarketSnapshot]:
    async with session_scope() as session:
        stmt = select(MarketSnapshot).order_by(
            MarketSnapshot.listing_id, MarketSnapshot.trade_date, MarketSnapshot.adjust
        )
        if adjust is not None:
            stmt = stmt.where(MarketSnapshot.adjust == adjust)
        return list((await session.execute(stmt)).scalars().all())


async def _seed_bar(listing_id: int, day: date, *, total_shares=None, raw_ref=None) -> None:
    """One iFinD bar per series (baostock rows would not move the iFinD cursor)."""

    async with session_scope() as session:
        repository = MarketSnapshotRepository(session)
        for adjust in ("none", "backward"):
            await repository.insert(
                listing_id=listing_id,
                trade_date=day,
                open=Decimal(1),
                high=Decimal(1),
                low=Decimal(1),
                close=Decimal(1),
                volume=Decimal(1),
                adjust=adjust,
                provider="ifind",
                captured_at=CAPTURED_AT - timedelta(days=30),
                total_shares=total_shares if adjust == "none" else None,
                raw_ref=raw_ref,
            )


# --- pure planning -------------------------------------------------------------


def test_chunked_never_exceeds_three():
    assert [len(batch) for batch in chunked(list(range(7)))] == [3, 3, 1]


def test_history_windows_cap_three_years():
    windows = history_windows(date(2020, 1, 1), date(2026, 9, 30))
    assert windows[0] == (date(2020, 1, 1), date(2022, 12, 31))
    assert windows[1] == (date(2023, 1, 1), date(2025, 12, 31))
    assert windows[2] == (date(2026, 1, 1), date(2026, 9, 30))


def test_plan_groups_tickers_by_start_date_and_batches_by_three():
    end = date(2026, 9, 30)
    starts = {
        "none": {
            "000001.SZ": date(2026, 9, 29),
            "000002.SZ": date(2026, 9, 29),
            "000003.SZ": date(2026, 9, 29),
            "000004.SZ": date(2026, 9, 29),
            "600000.SH": date(2026, 9, 30),
        },
        "backward": {"000001.SZ": date(2026, 9, 30)},
    }
    plan = plan_history_requests(starts, end)
    assert plan == [
        HistoryRequest("none", date(2026, 9, 29), end, ("000001.SZ", "000002.SZ", "000003.SZ")),
        HistoryRequest("none", date(2026, 9, 29), end, ("000004.SZ",)),
        HistoryRequest("none", date(2026, 9, 30), end, ("600000.SH",)),
        HistoryRequest("backward", date(2026, 9, 30), end, ("000001.SZ",)),
    ]
    assert all(len(request.tickers) <= 3 for request in plan)


# --- recorded fixtures -----------------------------------------------------------


async def test_fixture_replay_stores_raw_and_backward_series(db_session, tmp_path):
    del db_session
    ids = await seed_watchlist(TICKERS)
    for listing_id in ids.values():
        await _seed_bar(listing_id, date(2026, 9, 21))
    proxy = FixtureProxy()

    report = await _collect(proxy, tmp_path, date(2026, 9, 30))

    history_calls = [p for path, p in proxy.calls if path == "/stock/history"]
    assert {p["adjust"] for p in history_calls} == {"none", "backward"}
    assert all(p["start_date"] == "2026-09-22" for p in history_calls)
    assert all(len(p["ticker"].split(",")) == 3 for p in history_calls)
    assert len(history_calls) == 2
    info_calls = [p for path, p in proxy.calls if path == "/stock/info"]
    assert len(info_calls) == 1  # one 3-ticker call for the listings needing bars

    raw = [r for r in await _rows("none") if r.trade_date > date(2026, 9, 21)]
    backward = [r for r in await _rows("backward") if r.trade_date > date(2026, 9, 21)]
    assert len(raw) == len(backward) == 18
    moutai_raw = {r.trade_date: r for r in raw if r.listing_id == ids["600519.SH"]}
    moutai_bwd = {r.trade_date: r for r in backward if r.listing_id == ids["600519.SH"]}
    assert moutai_raw[date(2026, 9, 30)].close == Decimal("1258.62")
    assert moutai_bwd[date(2026, 9, 30)].close == Decimal("10882.41")
    # Shares fetched for the trade date land on that day's raw row only.
    assert moutai_raw[date(2026, 9, 30)].total_shares == Decimal("1250081601.0")
    assert moutai_raw[date(2026, 9, 29)].total_shares is None
    assert all(r.total_shares is None for r in backward)
    # raw_ref is the plain history response (no total-shares suffix any more).
    ref = moutai_raw[date(2026, 9, 30)].raw_ref
    assert ref is not None and "#" not in ref and (tmp_path / ref).is_file()
    assert all(r.provider == "ifind" and r.pe_ttm is None for r in raw)
    assert report.status == "success"
    assert report.completeness.ratio == 1.0
    assert "proxy-test" not in "".join(p.read_text() for p in tmp_path.rglob("*.json"))


# --- incremental + total shares ---------------------------------------------------


async def test_second_run_fetches_only_new_days(db_session, tmp_path):
    del db_session
    ids = await seed_watchlist(TICKERS)
    for listing_id in ids.values():
        await _seed_bar(listing_id, date(2026, 9, 25))
    proxy = FakeProxy(
        base_close={t: 10.0 for t in TICKERS}, total_shares={t: 1e9 for t in TICKERS}
    )
    first = await _collect(proxy, tmp_path, date(2026, 9, 29))
    assert first.status == "success"
    assert {p["start_date"] for p in proxy.calls_to("/stock/history")} == {"2026-09-26"}
    before = len(await _rows())

    proxy.calls.clear()
    second = await _collect(proxy, tmp_path, date(2026, 9, 30))
    history = proxy.calls_to("/stock/history")
    assert {(p["start_date"], p["end_date"]) for p in history} == {("2026-09-30", "2026-09-30")}
    assert len(await _rows()) == before + 2 * len(TICKERS)
    # Shares are fetched for every run that stores a new raw bar.
    assert len(proxy.calls_to("/stock/info")) == 1
    raw_today = [r for r in await _rows("none") if r.trade_date == date(2026, 9, 30)]
    assert all(r.total_shares == Decimal("1000000000") for r in raw_today)
    assert second.status == "success"

    proxy.calls.clear()
    third = await _collect(proxy, tmp_path, date(2026, 9, 30))
    assert proxy.calls == []  # already up to date
    assert third.completeness.ratio == 1.0


async def test_total_shares_land_on_trade_date_row_only(db_session, tmp_path):
    del db_session
    ids = await seed_watchlist(["600519.SH"])
    await _seed_bar(ids["600519.SH"], date(2026, 9, 25))
    proxy = FakeProxy(base_close={"600519.SH": 10.0}, total_shares={"600519.SH": 9.0})

    report = await _collect(proxy, tmp_path, date(2026, 9, 29))
    assert len(proxy.calls_to("/stock/info")) == 1
    raw = {r.trade_date: r for r in await _rows("none") if r.trade_date > date(2026, 9, 25)}
    assert raw[date(2026, 9, 29)].total_shares == Decimal("9.0")
    assert raw[date(2026, 9, 28)].total_shares is None
    assert report.metadata["total_shares"]["fetched"] == ["600519.SH"]


async def test_baostock_rows_do_not_move_the_ifind_cursor(db_session, tmp_path):
    del db_session
    ids = await seed_watchlist(["600519.SH"])
    async with session_scope() as session:
        repository = MarketSnapshotRepository(session)
        for adjust in ("none", "backward"):
            await repository.insert(
                listing_id=ids["600519.SH"], trade_date=date(2026, 9, 30), open=Decimal(1),
                high=Decimal(1), low=Decimal(1), close=Decimal(1), volume=Decimal(1),
                adjust=adjust, provider="baostock", captured_at=CAPTURED_AT,
            )
    proxy = FakeProxy(base_close={"600519.SH": 10.0}, total_shares={"600519.SH": 9.0})
    await _collect(proxy, tmp_path, date(2026, 9, 30))
    assert {p["start_date"] for p in proxy.calls_to("/stock/history")} == {"2026-09-30"}


async def test_disabled_watchlist_items_are_not_collected(db_session, tmp_path):
    del db_session
    await seed_watchlist(["600519.SH", "000001.SZ"], disabled=("000001.SZ",))
    proxy = FakeProxy(base_close={"600519.SH": 10.0}, total_shares={"600519.SH": 9.0})
    report = await _collect(proxy, tmp_path, date(2026, 9, 30))
    assert report.completeness.expected == ["600519.SH"]


async def test_listing_without_history_starts_at_trade_date(db_session, tmp_path):
    del db_session
    await seed_watchlist(["600519.SH"])
    proxy = FakeProxy(base_close={"600519.SH": 10.0}, total_shares={"600519.SH": 9.0})
    await _collect(proxy, tmp_path, date(2026, 9, 30))
    assert {p["start_date"] for p in proxy.calls_to("/stock/history")} == {"2026-09-30"}


# --- isolation, completeness, failures ---------------------------------------------


async def test_failed_batch_does_not_fail_the_job(db_session, tmp_path):
    del db_session
    tickers = ["000001.SZ", "000002.SZ", "000003.SZ", "600000.SH", "600001.SH"]
    await seed_watchlist(tickers)

    def fail(path, payload):
        if path == "/stock/history" and "600000.SH" in payload["ticker"]:
            return ValueError("malformed CSV")
        return None

    proxy = FakeProxy(
        base_close={t: 10.0 for t in tickers},
        total_shares={t: 1.0 for t in tickers},
        suspended={"000003.SZ"},
        fail=fail,
    )
    report = await _collect(proxy, tmp_path, date(2026, 9, 30))
    completeness = report.completeness
    assert completeness.collected == {"000001.SZ", "000002.SZ"}
    assert completeness.failed == ["600000.SH", "600001.SH"]
    assert completeness.tickers("no_data") == ["000003.SZ"]
    assert report.status == "partial"
    metadata = report.to_metadata()["completeness"]
    assert metadata["expected"] == 5
    assert metadata["collected"] == 2
    assert metadata["missing"]["600000.SH"]["reason"].startswith("none: ValueError")
    # Non-transient errors are not retried; the backward series of the failed
    # batch is still attempted independently.
    failing = [p for p in proxy.calls_to("/stock/history") if "600000.SH" in p["ticker"]]
    assert len(failing) == 2


async def test_transient_batch_error_is_retried(db_session, tmp_path):
    del db_session
    await seed_watchlist(["600519.SH"])
    attempts = {"n": 0}

    def flaky(path, payload):
        if path == "/stock/history" and attempts["n"] < 2:
            attempts["n"] += 1
            return httpx.ConnectError("reset", request=httpx.Request("POST", "http://x"))
        return None

    proxy = FakeProxy(base_close={"600519.SH": 1.0}, total_shares={"600519.SH": 1.0}, fail=flaky)
    report = await _collect(proxy, tmp_path, date(2026, 9, 30), retries=2)
    assert report.status == "success"
    assert len(proxy.calls_to("/stock/history")) == 4  # 2 failures + none + backward


async def test_empty_watchlist_is_a_precondition_error(db_session, tmp_path):
    del db_session
    with pytest.raises(SnapshotPreconditionError):
        await _collect(FakeProxy(), tmp_path, date(2026, 9, 30))


async def test_rate_limiter_paces_calls():
    now = {"t": 0.0}
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        now["t"] += seconds

    limiter = AsyncRateLimiter(60, clock=lambda: now["t"], sleep=sleep)
    for _ in range(4):
        await limiter.acquire()
    assert limiter.acquired == 4
    assert sum(slept) == pytest.approx(3.0)  # first is free, then one per second


async def test_caller_throttles_every_call(db_session, tmp_path):
    del db_session
    await seed_watchlist(TICKERS)
    limiter = AsyncRateLimiter(6000, burst=1000)
    proxy = FakeProxy(base_close={t: 1.0 for t in TICKERS}, total_shares={t: 1.0 for t in TICKERS})
    caller = ProxyBatchCaller(proxy, limiter, sleep=NoSleep())
    await collect_market_snapshot(
        _payload(date(2026, 9, 30)),
        caller=caller,
        artifacts=ArtifactWriter(tmp_path, run_id=1, attempt=1),
        lease=None,
        now=lambda: CAPTURED_AT,
    )
    assert limiter.acquired == len(proxy.calls) == caller.calls == 3


# --- through the worker -------------------------------------------------------------


async def _enqueue_market(trade_date: date) -> int:
    async with session_scope() as session:
        run, _ = await enqueue_snapshot_job(session, _payload(trade_date), triggered_by="test")
        return run.id


async def _run(run_id: int):
    async with session_scope() as session:
        return await PipelineRunRepository(session).get(run_id)


async def test_worker_persists_completeness_and_partial_status(db_session, tmp_path):
    del db_session
    tickers = ["000001.SZ", "000002.SZ", "000003.SZ", "600000.SH"]
    await seed_watchlist(tickers)
    settings = settings_for(tmp_path)

    def fail(path, payload):
        if "600000.SH" in payload["ticker"] and path == "/stock/history":
            return ValueError("bad rows")
        return None

    proxy = FakeProxy(
        base_close=dict.fromkeys(tickers, 1.0),
        total_shares=dict.fromkeys(tickers, 1.0),
        fail=fail,
    )
    run_id = await _enqueue_market(date(2026, 9, 30))
    worker = ResearchWorker(
        settings=settings, worker_id="w", handlers=handlers_for(settings, proxy)
    )
    assert await worker.run_once() == run_id
    run = await _run(run_id)
    assert run.status == "partial"
    completeness = run.metadata_json["completeness"]
    assert completeness["expected"] == 4
    assert completeness["collected"] == 3
    assert completeness["missing"]["600000.SH"]["kind"] == "failed"
    assert run.metadata_json["rows_inserted"] == {"none": 3, "backward": 3}
    # 75% < 95%: an action_required notification is written once.
    notes = list((tmp_path / "notifications").rglob("snapshot_market_snapshot_*.md"))
    assert len(notes) == 1
    assert "action_required" in notes[0].read_text()


async def test_worker_retries_when_every_batch_failed_transiently(db_session, tmp_path):
    del db_session
    await seed_watchlist(["600519.SH"])
    settings = settings_for(tmp_path, ifind_collect_batch_retries=0)
    proxy = FakeProxy(
        fail=lambda path, payload: httpx.ReadTimeout("slow")
        if path == "/stock/history"
        else None,
        total_shares={"600519.SH": 1.0},
    )
    run_id = await _enqueue_market(date(2026, 9, 30))
    worker = ResearchWorker(
        settings=settings,
        worker_id="w",
        handlers=handlers_for(settings, proxy),
        retry_base_delay_seconds=0,
    )
    await worker.run_once()
    run = await _run(run_id)
    assert run.status == "queued"
    assert "ProviderTransientError" in (run.error_summary or "")
    assert run.metadata_json["completeness"]["failed_count"] == 1


async def test_terminal_total_failure_fails_the_run(db_session, tmp_path):
    del db_session
    await seed_watchlist(["600519.SH"])
    settings = settings_for(tmp_path)
    proxy = FakeProxy(fail=lambda path, payload: ValueError("schema changed"))
    run_id = await _enqueue_market(date(2026, 9, 30))
    worker = ResearchWorker(
        settings=settings, worker_id="w", handlers=handlers_for(settings, proxy)
    )
    await worker.run_once()
    run = await _run(run_id)
    assert run.status == "failed"
    # Persisted summaries carry the exception type only (no provider text).
    assert run.error_summary == "ProviderError"
    notes = list((tmp_path / "notifications").rglob("*.md"))
    assert len(notes) == 1 and "action_required" in notes[0].read_text()


async def test_lease_lost_mid_collection_writes_nothing(db_session, tmp_path):
    del db_session
    await seed_watchlist(["600519.SH"])
    settings = settings_for(tmp_path)
    run_id = await _enqueue_market(date(2026, 9, 30))

    async def steal(path, payload):
        # Stale recovery hands the run to another worker while we collect.
        async with session_scope() as session:
            run = await PipelineRunRepository(session).get(run_id)
            run.worker_id = "thief"
            run.attempt_count += 1

    proxy = FakeProxy(
        base_close={"600519.SH": 1.0}, total_shares={"600519.SH": 1.0}, on_call=steal
    )
    worker = ResearchWorker(
        settings=settings, worker_id="w", handlers=handlers_for(settings, proxy)
    )
    assert await worker.run_once() == run_id
    run = await _run(run_id)
    assert run.status == "running"
    assert run.worker_id == "thief"
    assert await _rows() == []
