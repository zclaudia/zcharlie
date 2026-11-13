from contextlib import asynccontextmanager
from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from investment_agent.config import Settings
from investment_agent.providers.wmshare_dividends import WmshareCapture
from investment_agent.scheduler.snapshot_jobs import snapshot_payloads
from investment_agent.snapshots.collect import ArtifactWriter
from investment_agent.snapshots.dividends import collect_wmshare_dividends
from investment_agent.snapshots.jobs import (
    DividendSnapshotPayload,
    FinancialSnapshotPayload,
    MarketSnapshotPayload,
    idempotency_key,
)

DAY = date(2026, 11, 13)


def test_scope_and_schedule_are_bounded():
    assert not Settings(_env_file=None, api_key="test").wmshare_dividends_enabled
    with pytest.raises(ValidationError):
        DividendSnapshotPayload(
            provider="wmshare", scope="universe", universe_code="CSI300", as_of=DAY
        )
    with pytest.raises(ValidationError):
        MarketSnapshotPayload(provider="wmshare", scope="watchlist", trade_date=DAY)
    with pytest.raises(ValidationError):
        FinancialSnapshotPayload(
            provider="wmshare", scope="watchlist", report_period=date(2026, 9, 30), as_of=DAY
        )
    all_watchlist = DividendSnapshotPayload(provider="wmshare", scope="watchlist", as_of=DAY)
    one = DividendSnapshotPayload(
        provider="wmshare", scope="watchlist", as_of=DAY, tickers=("600036.SH",)
    )
    assert idempotency_key(one) != idempotency_key(all_watchlist)
    payloads = snapshot_payloads("financial_weekly", today=DAY, calendar=None, universes=["CSI300"])
    assert not any(
        isinstance(p, DividendSnapshotPayload) and p.provider == "wmshare" for p in payloads
    )
    payloads = snapshot_payloads(
        "financial_weekly",
        today=DAY,
        calendar=None,
        universes=["CSI300"],
        wmshare_dividends_enabled=True,
    )
    assert {(p.provider, p.scope) for p in payloads if isinstance(p, DividendSnapshotPayload)} >= {
        ("wmshare", "watchlist"),
        ("baostock", "watchlist"),
    }


async def test_partial_capture_is_persisted_and_terminal_stops_following(tmp_path, monkeypatch):
    from investment_agent.dividends import service
    from investment_agent.snapshots import dividends

    @asynccontextmanager
    async def session_scope():
        yield object()

    async def members(*_):
        return {"600036.SH": 1, "600519.SH": 2, "601398.SH": 3}

    writes = []

    async def persist(_session, listing_id, capture, raw_ref=None):
        writes.append((listing_id, capture.ticker, raw_ref))
        if capture.ticker == "600036.SH":
            capture.history_error = "rejected during persistence"
        return {"events": len(capture.events)}

    async def fence(*_):
        pass

    class Fake:
        async def fetch(self, ticker, as_of):
            capture = WmshareCapture(ticker=ticker, as_of=as_of, captured_at=datetime.now(UTC))
            capture.history = []
            capture.snapshot = {"yield_ttm": 0.1}
            capture.ec = {"history": 3, "snapshot": None}
            if ticker == "600036.SH":
                capture.history_error = "history failed"
            else:
                capture.snapshot_error = "quota"
                capture.terminal = True
            return capture

    monkeypatch.setattr(dividends, "session_scope", session_scope)
    monkeypatch.setattr(dividends, "collection_members", members)
    monkeypatch.setattr(dividends, "fence_writes", fence)
    monkeypatch.setattr(service, "persist_wmshare_capture", persist)
    report = await collect_wmshare_dividends(
        DividendSnapshotPayload(provider="wmshare", scope="watchlist", as_of=DAY),
        client=Fake(),
        artifacts=ArtifactWriter(tmp_path, run_id=1, attempt=1),
        lease=None,
    )
    assert [w[1] for w in writes] == ["600036.SH", "600519.SH"]
    assert report.status == "partial"
    assert report.completeness.stopped == ["600519.SH", "601398.SH"]
    assert report.metadata["parts"]["600036.SH"]["history"] == "rejected during persistence"
    assert report.metadata["ec_known_total"] == 6
    assert report.metadata["ec_unknown_calls"] == 2


async def test_requested_ticker_must_be_enabled_watchlist(tmp_path, monkeypatch):
    from investment_agent.snapshots import dividends
    from investment_agent.snapshots.collect import SnapshotPreconditionError

    @asynccontextmanager
    async def session_scope():
        yield object()

    async def members(*_):
        return {"600036.SH": 1}

    monkeypatch.setattr(dividends, "session_scope", session_scope)
    monkeypatch.setattr(dividends, "collection_members", members)
    with pytest.raises(SnapshotPreconditionError):
        await collect_wmshare_dividends(
            DividendSnapshotPayload(
                provider="wmshare", scope="watchlist", as_of=DAY, tickers=("600519.SH",)
            ),
            client=object(),
            artifacts=ArtifactWriter(tmp_path, run_id=1, attempt=1),
            lease=None,
        )


async def test_baostock_exclusion_preserves_other_user_pools(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from investment_agent.snapshots import dividends

    @asynccontextmanager
    async def session_scope():
        yield object()

    async def members(*_):
        # Includes index members and the combined watchlist/candidate/holding pools.
        return {"600036.SH": 1, "600519.SH": 2, "601398.SH": 3, "000858.SZ": 4}

    async def pools(*_):
        return SimpleNamespace(watchlist={"600036.SH": 1})

    seen = {}

    async def collect(plan, **_):
        seen.update(plan)
        return dividends.DividendBatch(dividends.DividendStoreResult(), 0, {}, {}, 0, None)

    monkeypatch.setattr(dividends, "session_scope", session_scope)
    monkeypatch.setattr(dividends, "collection_members", members)
    monkeypatch.setattr(dividends, "user_pools", pools)
    monkeypatch.setattr(dividends, "collect_dividend_years", collect)
    await dividends.collect_baostock_dividends(
        DividendSnapshotPayload(
            provider="baostock",
            scope="universe",
            universe_code="CSI300",
            as_of=DAY,
            exclude_watchlist=True,
        ),
        client=object(),
        artifacts=ArtifactWriter(tmp_path, run_id=1, attempt=1),
        lease=None,
        now=lambda: datetime.now(UTC),
    )
    assert set(seen) == {"600519.SH", "601398.SH", "000858.SZ"}
