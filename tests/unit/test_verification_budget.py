"""iFinD budget ledger (P1-A13): rolling window, boundary, wrapper, call counting."""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from investment_agent.providers.errors import QuotaExhaustedError
from investment_agent.providers.ifind_budget import (
    BudgetedProxyClient,
    BudgetExhaustedError,
    BudgetStatus,
    budget_status,
    ledger_hook,
    record_call,
)
from investment_agent.research.ifind_chain import IfindProxyClient
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.verification import IfindCallLedger
from tests.unit._snapshot_support import FakeProxy

T0 = datetime(2026, 10, 7, 0, 0, tzinfo=UTC)
WINDOW = timedelta(hours=5)


async def _seed(*times: datetime, source: str = "snapshot") -> None:
    async with session_scope() as session:
        for at in times:
            await record_call(
                session, at=at, endpoint="/stock/info", tickers=3, outcome="ok", source=source
            )


async def _status(now: datetime, cap: int = 4) -> BudgetStatus:
    async with session_scope() as session:
        return await budget_status(session, now=now, cap=cap, window=WINDOW)


async def _ledger() -> list[IfindCallLedger]:
    async with session_scope() as session:
        stmt = select(IfindCallLedger).order_by(IfindCallLedger.id)
        return list((await session.execute(stmt)).scalars())


async def test_window_rolls_and_boundary_is_exclusive(db_session):
    del db_session
    await _seed(T0, T0 + timedelta(hours=1), T0 + timedelta(hours=2))
    status = await _status(T0 + timedelta(hours=2))
    assert (status.used, status.remaining) == (3, 1)

    # Exactly one window after the first call it has left the window.
    status = await _status(T0 + WINDOW)
    assert (status.used, status.remaining) == (2, 2)
    just_before = await _status(T0 + WINDOW - timedelta(microseconds=1))
    assert just_before.used == 3
    # Calls after ``now`` (another clock) do not count yet.
    early = await _status(T0 + timedelta(minutes=30))
    assert early.used == 1
    assert (await _status(T0 + timedelta(hours=8))).used == 0


async def test_available_at_is_when_enough_calls_expire(db_session):
    del db_session
    times = [T0 + timedelta(minutes=10 * i) for i in range(4)]
    await _seed(*times)
    now = T0 + timedelta(hours=1)
    status = await _status(now, cap=4)
    assert status.remaining == 0
    assert status.available_at(1) == times[0] + WINDOW
    assert status.available_at(3) == times[2] + WINDOW
    # More than the cap can never fit at once: clamp to the cap.
    assert status.available_at(10) == times[3] + WINDOW
    roomy = await _status(now, cap=10)
    assert roomy.available_at(6) == now
    assert roomy.available_at(7) == times[0] + WINDOW
    assert status.describe()["next_free_at"] == (times[0] + WINDOW).isoformat()


async def test_budgeted_client_refuses_without_budget_and_records_outcomes(db_session):
    del db_session
    clock = {"now": T0}
    proxy = FakeProxy(total_shares={"600519.SH": 1.0})
    client = BudgetedProxyClient(proxy, cap=2, window=WINDOW, clock=lambda: clock["now"], run_id=9)
    client.request_id = 42
    await client.post_rows("/stock/info", {"ticker": "600519.SH", "request_time": "2026-10-07"})

    def quota(path, payload):
        return QuotaExhaustedError("quota", provider="ifind", status_code=429)

    proxy.fail = quota
    with pytest.raises(QuotaExhaustedError):
        await client.post_rows("/stock/info", {"ticker": "600519.SH,000001.SZ"})
    with pytest.raises(BudgetExhaustedError) as exc_info:
        await client.post_rows("/stock/info", {"ticker": "600519.SH"})
    assert exc_info.value.available_at == T0 + WINDOW
    assert len(proxy.calls) == 2  # the refused call never reached the proxy

    rows = await _ledger()
    assert [(r.outcome, r.ticker_count, r.source, r.run_id, r.verification_request_id)
            for r in rows] == [
        ("ok", 1, "verification", 9, 42),
        ("quota", 2, "verification", 9, 42),
    ]
    clock["now"] = T0 + WINDOW
    proxy.fail = None
    await client.post_rows("/stock/info", {"ticker": "600519.SH"})
    assert client.calls == 3


async def test_proxy_client_hook_counts_every_call(db_session):
    del db_session

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/quota":
            return httpx.Response(429, json={"detail": "usage limit"})
        if request.url.path == "/broken":
            return httpx.Response(500, json={"detail": "boom"})
        return httpx.Response(200, json={"data": [{"thscode": "600519.SH", "x": "1"}]})

    client = IfindProxyClient(
        base_url="http://proxy.test",
        api_key="k",
        transport=httpx.MockTransport(handler),
        on_call=ledger_hook(source="research", run_id=5, clock=lambda: T0),
    )
    async with client:
        await client.post_rows("/ok", {"ticker": "600519.SH,000001.SZ,300750.SZ"})
        with pytest.raises(QuotaExhaustedError):
            await client.post_rows("/quota", {"ticker": "600519.SH"})
        with pytest.raises(httpx.HTTPStatusError):
            await client.post_rows("/broken", {"ticker": "600519.SH"})
    rows = await _ledger()
    assert [(r.endpoint, r.outcome, r.ticker_count, r.source, r.run_id) for r in rows] == [
        ("/ok", "ok", 3, "research", 5),
        ("/quota", "quota", 1, "research", 5),
        ("/broken", "error", 1, "research", 5),
    ]
    # Other sources shrink the verification budget too.
    status = await _status(T0, cap=150)
    assert status.used == 3 and status.remaining == 147


async def test_hook_never_breaks_a_call_when_the_ledger_is_unavailable(tmp_path):
    # No engine/schema for the ledger here: the hook logs and the call succeeds.
    from investment_agent.storage.database import init_engine

    engine = init_engine(f"sqlite+aiosqlite:///{tmp_path / 'empty.db'}")
    try:
        hook = ledger_hook(source="snapshot")
        await hook("/stock/info", {"ticker": "600519.SH"}, "ok")
    finally:
        await engine.dispose()


def test_verification_budget_shim_reexports_the_moved_surface():
    """``verification.budget`` stays importable and resolves to the moved objects (B2)."""
    import investment_agent.providers.ifind_budget as moved
    import investment_agent.verification.budget as shim

    for name in (
        "DEFAULT_BUDGET",
        "DEFAULT_WINDOW",
        "BudgetExhaustedError",
        "BudgetStatus",
        "BudgetedProxyClient",
        "as_utc",
        "budget_status",
        "ledger_hook",
        "record_call",
        "ticker_count",
        "utc_now",
    ):
        assert getattr(shim, name) is getattr(moved, name), name


def test_moved_module_imports_without_verification_or_snapshots():
    """The neutral module must not drag ``verification``/``snapshots``/``research.ifind_chain`` in.

    Checked in a fresh interpreter: the whole point of the move (B2) is that
    importing the ledger does not import the packages that used to depend on it.
    """
    code = (
        "import sys\n"
        "import investment_agent.providers.ifind_budget\n"
        "forbidden = ('investment_agent.verification', 'investment_agent.snapshots',\n"
        "             'investment_agent.research.ifind_chain')\n"
        "leaked = sorted(m for m in sys.modules if m.startswith(forbidden))\n"
        "assert not leaked, leaked\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
