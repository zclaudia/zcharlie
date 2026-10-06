"""iFinD quota exhaustion (P1-A8): classification, no retry, collectors stop and
keep data, one action_required notification, research runs fail fast."""

from __future__ import annotations

from datetime import date

import httpx
import pytest

from investment_agent.providers.errors import QuotaExhaustedError
from investment_agent.research import ifind_chain
from investment_agent.research.ifind_chain import IfindProxyClient, proxy_quota_error
from investment_agent.research.retry_policy import is_quota_exhausted, is_transient_error
from investment_agent.research.worker import ResearchWorker
from investment_agent.snapshots.collect import ArtifactWriter, ProxyBatchCaller
from investment_agent.snapshots.financial import collect_financial_snapshot
from investment_agent.snapshots.jobs import (
    FinancialSnapshotPayload,
    MarketSnapshotPayload,
    enqueue_snapshot_job,
)
from investment_agent.snapshots.market import collect_market_snapshot
from investment_agent.snapshots.notifications import notify_snapshot_run
from investment_agent.snapshots.throttle import AsyncRateLimiter
from investment_agent.storage.database import session_scope
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository
from tests.unit._snapshot_support import (
    CAPTURED_AT,
    FakeProxy,
    NoSleep,
    handlers_for,
    seed_watchlist,
    settings_for,
)

LIMIT = "You've reached your 5-hour usage limit"
WATCHLIST = ["000001.SZ", "000002.SZ", "000003.SZ", "600000.SH", "600519.SH", "601398.SH"]


def _response(status: int, body: object, path: str = "/stock/history") -> httpx.Response:
    request = httpx.Request("POST", f"http://proxy.test{path}")
    if isinstance(body, str):
        return httpx.Response(status, text=body, request=request)
    return httpx.Response(status, json=body, request=request)


def _quota(path: str = "/stock/history") -> QuotaExhaustedError:
    error = proxy_quota_error(_response(429, {"detail": LIMIT}, path), path)
    assert error is not None
    return error


# --- classification --------------------------------------------------------------


def test_proxy_answers_are_classified():
    error = _quota()
    assert error.status_code == 429 and LIMIT in error.detail
    assert "iFinD quota exhausted" in str(error) and "not retried" in str(error)
    legacy = proxy_quota_error(
        _response(502, {"detail": "upstream failed: [HTTP 403] forbidden"}), "/stock/info"
    )
    assert legacy is not None and legacy.status_code == 502
    worded = proxy_quota_error(_response(502, f"Bad gateway: {LIMIT}"), "/stock/info")
    assert worded is not None
    assert proxy_quota_error(_response(502, {"detail": "upstream timeout"}), "/x") is None
    assert proxy_quota_error(_response(500, {"detail": LIMIT}), "/x") is None
    assert proxy_quota_error(_response(200, {"data": []}), "/x") is None


def test_retry_policy_never_retries_quota():
    assert is_quota_exhausted(_quota()) and not is_transient_error(_quota())
    raw_429 = httpx.HTTPStatusError(
        "429", request=httpx.Request("POST", "http://p"), response=_response(429, {})
    )
    assert is_quota_exhausted(raw_429) and not is_transient_error(raw_429)
    legacy_502 = httpx.HTTPStatusError(
        "502",
        request=httpx.Request("POST", "http://p"),
        response=_response(502, {"detail": LIMIT}),
    )
    assert not is_transient_error(legacy_502)
    plain_502 = httpx.HTTPStatusError(
        "502",
        request=httpx.Request("POST", "http://p"),
        response=_response(502, {"detail": "bad gateway"}),
    )
    assert is_transient_error(plain_502) and not is_quota_exhausted(plain_502)


async def test_proxy_client_raises_quota_error():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/stock/info":
            return httpx.Response(502, json={"detail": "[HTTP 403] " + LIMIT})
        if request.url.path == "/stock/history":
            return httpx.Response(429, json={"detail": LIMIT})
        return httpx.Response(503, json={"detail": "busy"})

    async with IfindProxyClient(
        base_url="http://proxy.test", api_key="k", transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(QuotaExhaustedError):
            await client.post_rows("/stock/history", {"ticker": "600519.SH"})
        with pytest.raises(QuotaExhaustedError):
            await client.post_rows("/stock/info", {"ticker": "600519.SH"})
        with pytest.raises(httpx.HTTPStatusError):
            await client.post_rows("/financial/index", {"ticker": "600519.SH"})


async def test_batch_caller_does_not_retry_quota():
    calls = {"n": 0}

    def fail(path, payload):
        calls["n"] += 1
        return _quota(path)

    caller = ProxyBatchCaller(
        FakeProxy(fail=fail), AsyncRateLimiter(6000, burst=1000), retries=3, sleep=NoSleep()
    )
    with pytest.raises(QuotaExhaustedError):
        await caller.call("/stock/history", {"ticker": "600519.SH"})
    assert calls["n"] == 1


# --- collectors --------------------------------------------------------------------


def _market() -> MarketSnapshotPayload:
    return MarketSnapshotPayload(provider="ifind", scope="watchlist", trade_date=date(2026, 9, 30))


def _quota_after(path: str, allowed: int):
    seen = {"n": 0}

    def fail(called: str, payload):
        if called != path:
            return None
        seen["n"] += 1
        return _quota(called) if seen["n"] > allowed else None

    return fail


async def test_market_stops_on_quota_and_keeps_stored_bars(db_session, tmp_path):
    del db_session
    await seed_watchlist(WATCHLIST)
    proxy = FakeProxy(
        base_close=dict.fromkeys(WATCHLIST, 10.0),
        total_shares=dict.fromkeys(WATCHLIST, 1.0),
        fail=_quota_after("/stock/history", allowed=1),
    )
    report = await collect_market_snapshot(
        _market(),
        caller=ProxyBatchCaller(
            proxy, AsyncRateLimiter(6000, burst=1000), retries=3, sleep=NoSleep()
        ),
        artifacts=ArtifactWriter(tmp_path, run_id=1, attempt=1),
        lease=None,
        now=lambda: CAPTURED_AT,
    )
    # info (2 batches) + history none batch 1 ok + history none batch 2 quota -> stop
    assert len(proxy.calls_to("/stock/history")) == 2
    assert report.status == "partial"
    assert report.stop_reason == "quota"
    completeness = report.completeness
    assert completeness.quota == sorted(WATCHLIST)  # backward series never ran
    assert completeness.failed == []
    metadata = report.to_metadata()
    assert metadata["stop_reason"] == "quota"
    assert metadata["completeness"]["quota_count"] == 6
    assert metadata["rows_inserted"] == {"none": 3, "backward": 0}  # kept
    assert "quota exhausted" in report.summary()


async def test_quota_on_total_shares_stops_before_any_history(db_session, tmp_path):
    del db_session
    await seed_watchlist(["600519.SH"])
    proxy = FakeProxy(fail=_quota_after("/stock/info", allowed=0))
    report = await collect_market_snapshot(
        _market(),
        caller=ProxyBatchCaller(proxy, AsyncRateLimiter(6000, burst=1000), sleep=NoSleep()),
        artifacts=ArtifactWriter(tmp_path, run_id=1, attempt=1),
        lease=None,
        now=lambda: CAPTURED_AT,
    )
    assert proxy.calls_to("/stock/history") == []
    assert report.status == "partial" and report.completeness.quota == ["600519.SH"]


async def test_financial_stops_on_quota(db_session, tmp_path):
    del db_session
    await seed_watchlist(WATCHLIST)
    profit = {t: {"ths_roe_stock": "10"} for t in WATCHLIST}
    seen = {"n": 0}

    def fail(path, payload):
        seen["n"] += 1
        return _quota(path) if seen["n"] > 4 else None  # first batch = 4 endpoint calls

    proxy = FakeProxy(financial={("/financial/index:profitability", "20260630"): profit}, fail=fail)
    report = await collect_financial_snapshot(
        FinancialSnapshotPayload(
            provider="ifind", scope="watchlist", report_period=date(2026, 6, 30),
            as_of=date(2026, 10, 6),
        ),
        caller=ProxyBatchCaller(proxy, AsyncRateLimiter(6000, burst=1000), sleep=NoSleep()),
        artifacts=ArtifactWriter(tmp_path, run_id=2, attempt=1),
        lease=None,
        now=lambda: CAPTURED_AT,
    )
    assert len(proxy.calls) == 5
    assert report.completeness.collected == set(WATCHLIST[:3])
    assert report.completeness.quota == WATCHLIST[3:]
    assert report.status == "partial"
    assert report.metadata["stored"] == 3


async def test_quota_run_is_partial_with_one_notification(db_session, tmp_path):
    del db_session
    await seed_watchlist(WATCHLIST)
    settings = settings_for(tmp_path)
    proxy = FakeProxy(
        base_close=dict.fromkeys(WATCHLIST, 10.0),
        total_shares=dict.fromkeys(WATCHLIST, 1.0),
        fail=_quota_after("/stock/history", allowed=0),
    )
    async with session_scope() as session:
        run, _ = await enqueue_snapshot_job(session, _market(), triggered_by="test")
        run_id = run.id
    worker = ResearchWorker(
        settings=settings,
        worker_id="w",
        handlers=handlers_for(settings, proxy),
        retry_base_delay_seconds=0,
    )
    assert await worker.run_once() == run_id
    async with session_scope() as session:
        finished = await PipelineRunRepository(session).get(run_id)
    assert finished.status == "partial"
    assert finished.attempt_count == 1
    assert finished.metadata_json["stop_reason"] == "quota"
    assert "quota exhausted" in (finished.error_summary or "")
    notes = list((tmp_path / "notifications").rglob("*.md"))
    assert len(notes) == 1
    text = notes[0].read_text()
    assert "action_required" in text and "iFinD 额度耗尽" in text
    # Finalizing again (e.g. a crash after writing) never sends a second one.
    await notify_snapshot_run(
        run_id,
        notifier=handlers_for(settings, proxy)[0].runtime.file_notifier(),
        threshold=settings.snapshot_completeness_threshold,
    )
    assert len(list((tmp_path / "notifications").rglob("*.md"))) == 1
    assert await worker.run_once() is None  # nothing re-queued


# --- single-stock research chain ----------------------------------------------------


async def test_research_run_fails_without_retry_on_quota(db_session, tmp_path, monkeypatch):
    del db_session
    settings = settings_for(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"detail": LIMIT})

    class QuotaClient(IfindProxyClient):
        def __init__(self, **kwargs):
            super().__init__(**kwargs, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(ifind_chain, "IfindProxyClient", QuotaClient)
    async with session_scope() as session:
        run, _ = await PipelineRunRepository(session).enqueue(
            kind="ifind_research",
            idempotency_key="quota-research",
            triggered_by="test",
            request_json={
                "ticker": "600406.SH",
                "industry": "电力设备",
                "report_year": 2025,
                "as_of": "2026-10-05",
            },
            max_attempts=3,
        )
        run_id = run.id
    worker = ResearchWorker(settings=settings, worker_id="w", retry_base_delay_seconds=0)
    assert await worker.run_once() == run_id
    async with session_scope() as session:
        finished = await PipelineRunRepository(session).get(run_id)
    assert finished.status == "failed"
    assert finished.attempt_count == 1
    assert "iFinD quota exhausted" in (finished.error_summary or "")
    assert "retry scheduled" not in (finished.error_summary or "")
