"""``ifind_verify`` job (P1-A13): batching, write-back, view precedence, budget
deferral, quota stop, retries (offline, fake proxy)."""

from __future__ import annotations

import itertools
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from investment_agent.factors.view_db import load_snapshot_view
from investment_agent.notifiers.file_notifier import FileNotifier
from investment_agent.providers.errors import QuotaExhaustedError
from investment_agent.research.worker import ResearchWorker
from investment_agent.snapshots.throttle import AsyncRateLimiter
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.pit_snapshot import FinancialSnapshot, MarketSnapshot
from investment_agent.storage.models.verification import IfindCallLedger, VerificationRequest
from investment_agent.storage.repositories.financial_snapshot import FinancialSnapshotRepository
from investment_agent.storage.repositories.market_snapshot import MarketSnapshotRepository
from investment_agent.verification import enqueue_verification
from investment_agent.verification.budget import record_call
from investment_agent.verification.job import (
    IfindVerifyHandler,
    VerifyRuntime,
    enqueue_verify_run,
)
from investment_agent.verification.notifications import notify_verification_run
from investment_agent.verification.schedule import dispatch_verification
from tests.unit._snapshot_support import FakeProxy, NoSleep, seed_watchlist, settings_for

NOW = datetime(2026, 10, 6, 9, 0, tzinfo=UTC)  # 17:00 Asia/Shanghai
AS_OF = date(2026, 10, 6)
FY2025 = date(2025, 12, 31)
H1_2026 = date(2026, 6, 30)
BAR_DAY = date(2026, 9, 30)
MOUTAI = "600519.SH"
PROFITABILITY = "/financial/index:profitability"
CAPITAL = "/financial/index:capital_structure"
INCOME = "/financial/statements:income_statement"
CASH = "/financial/statements:cash_flow"
_slots = itertools.count(1)


async def seed_free(
    listing_id: int,
    *,
    period: date = FY2025,
    roe: float = 0.30,
    debt_ratio: float = 0.20,
    net_profit_parent: float = 8.6e10,
    close: float = 1500.0,
) -> None:
    """baostock + AkShare rows of the period and one baostock bar."""

    async with session_scope() as session:
        financial = FinancialSnapshotRepository(session)
        await financial.insert(
            listing_id=listing_id,
            report_period=period,
            period_kind="annual" if period.month == 12 else "semi",
            fields={
                "roe": roe,
                "debt_ratio": debt_ratio,
                "net_profit": 9.0e10,
                "revenue": 1.6e11,
                "total_shares": 1.256e9,
                "eps_ttm": 68.6,
                "cfo_to_np": 1.0,
            },
            provider="baostock",
            captured_at=datetime(2026, 3, 31, tzinfo=UTC),
            availability="actual",
            available_from=date(2026, 3, 30),
        )
        await financial.insert(
            listing_id=listing_id,
            report_period=period,
            period_kind="annual" if period.month == 12 else "semi",
            fields={
                "part": "summary",
                "net_profit_parent": net_profit_parent,
                "operating_cash_flow": 9.0e10,
            },
            provider="akshare",
            captured_at=datetime(2026, 3, 31, tzinfo=UTC),
            availability="actual",
            available_from=date(2026, 3, 30),
        )
        price = Decimal(str(close))
        await MarketSnapshotRepository(session).insert(
            listing_id=listing_id,
            trade_date=BAR_DAY,
            open=price,
            high=price,
            low=price,
            close=price,
            volume=Decimal(1000),
            provider="baostock",
            captured_at=datetime(2026, 9, 30, 12, tzinfo=UTC),
        )


def ifind_answers(proxy: FakeProxy, ticker: str, period: date = FY2025, **overrides: str) -> None:
    key = period.strftime("%Y%m%d")
    rows = {
        PROFITABILITY: {
            "ths_roe_stock": "30.3",
            "ths_gross_selling_rate_stock": "91.5",
            "ths_net_sales_rate_stock": "52.0",
            "ths_eps_basic_stock": "68.64",
            "ths_nav_ps_stock": "205.3",
            "ths_revenue_ps_stock": "139.3",
        },
        CAPITAL: {"ths_asset_liab_ratio_stock": "25.0"},
        INCOME: {
            "ths_operating_total_revenue_stock": "175000000000",
            "ths_np_atoopc_stock": "86000000000",
        },
        CASH: {"ths_ncf_from_oa_stock": "90000000000", "ths_cash_paid_for_assets_stock": "5000000000"},
    }
    for endpoint, values in rows.items():
        values = {k: overrides.get(k, v) for k, v in values.items()}
        proxy.financial.setdefault((endpoint, key), {})[ticker] = values
    proxy.total_shares[ticker] = 1.256e9
    # close on BAR_DAY = base + (BAR_DAY - 2026-01-01).days
    proxy.base_close[ticker] = 1500.0 - (BAR_DAY - date(2026, 1, 1)).days


def runtime(tmp_path: Path, proxy: Any, *, clock: Any = None, **settings: Any) -> VerifyRuntime:
    return VerifyRuntime(
        settings=settings_for(tmp_path, **settings),
        proxy_factory=lambda: proxy,
        limiter=AsyncRateLimiter(6000, burst=1000),
        clock=clock or (lambda: NOW),
        sleep=NoSleep(),
        batch_backoff_seconds=0,
        notifier=FileNotifier(tmp_path / "notifications"),
    )


async def run_verify(rt: VerifyRuntime) -> PipelineRun:
    async with session_scope() as session:
        queued, _ = await enqueue_verify_run(
            session, slot=f"test-{next(_slots)}", triggered_by="test"
        )
        run_id = queued.id
    worker = ResearchWorker(
        settings=rt.settings, worker_id="w", handlers=[IfindVerifyHandler(rt.settings, runtime=rt)]
    )
    await worker.run_once(run_id=run_id)
    async with session_scope() as session:
        run = await session.get(PipelineRun, run_id)
        assert run is not None
        return run


async def requests() -> list[VerificationRequest]:
    async with session_scope() as session:
        stmt = select(VerificationRequest).order_by(VerificationRequest.id)
        return list((await session.execute(stmt)).scalars())


async def ifind_rows(listing_id: int) -> list[FinancialSnapshot]:
    async with session_scope() as session:
        stmt = (
            select(FinancialSnapshot)
            .where(FinancialSnapshot.listing_id == listing_id, FinancialSnapshot.provider == "ifind")
            .order_by(FinancialSnapshot.version)
        )
        return list((await session.execute(stmt)).scalars())


async def test_full_verification_writes_back_and_view_prefers_verified(db_session, tmp_path):
    del db_session
    ids = await seed_watchlist([MOUTAI])
    lid = ids[MOUTAI]
    await seed_free(lid)
    proxy = FakeProxy()
    ifind_answers(proxy, MOUTAI)
    await enqueue_verification(listing_id=lid, ticker=MOUTAI, trigger="promote")

    run = await run_verify(runtime(tmp_path, proxy))
    assert run.status == "success", run.error_summary
    # one call per A3 financial endpoint + /stock/info + raw /stock/history
    assert sorted(path for path, _ in proxy.calls) == sorted(
        ["/financial/index", "/financial/index", "/financial/statements",
         "/financial/statements", "/stock/info", "/stock/history"]
    )
    (history,) = proxy.calls_to("/stock/history")
    assert (history["start_date"], history["end_date"], history["adjust"]) == (
        "2026-09-30", "2026-09-30", "none"
    )

    (request,) = await requests()
    assert request.status == "done" and request.run_id == run.id
    result = request.result
    assert result is not None
    assert result["report_period"] == "2025-12-31"
    assert result["conflicts"] == ["debt_ratio"]
    assert result["unavailable"] == ["eps_ttm"]
    assert set(result["verified"]) == {
        "roe", "net_profit_parent", "revenue_total", "operating_cash_flow",
        "capital_expenditure", "free_cash_flow", "total_shares", "close",
    }
    assert set(result["filled"]) == {"revenue_total", "capital_expenditure", "free_cash_flow"}
    debt = result["fields"]["debt_ratio"]
    assert debt["ifind"] == pytest.approx(0.25) and debt["free"] == pytest.approx(0.20)
    assert debt["free_provider"] == "baostock" and debt["tolerance"] == 0.02

    (row,) = await ifind_rows(lid)
    assert row.id == result["financial_snapshot_id"]
    assert row.fields["verification"] == {
        "capital_expenditure": "verified",
        "debt_ratio": "conflict",
        "free_cash_flow": "verified",
        "net_profit_parent": "verified",
        "operating_cash_flow": "verified",
        "revenue_total": "verified",
        "roe": "verified",
    }
    assert row.fields["verification_detail"]["request_id"] == request.id
    assert row.availability == "approx" and row.available_from == AS_OF

    async with session_scope() as session:
        market = (
            await session.execute(select(MarketSnapshot).where(MarketSnapshot.provider == "ifind"))
        ).scalar_one()
        ledger = list((await session.execute(select(IfindCallLedger))).scalars())
    assert market.trade_date == BAR_DAY and float(market.close) == 1500.0
    assert float(market.total_shares or 0) == 1.256e9
    assert len(ledger) == 6
    assert {(r.source, r.run_id, r.verification_request_id, r.outcome) for r in ledger} == {
        ("verification", run.id, request.id, "ok")
    }

    # The factor view now resolves verified fields to iFinD; on a conflict iFinD
    # wins too (D6 rev 2) and the free value stays on record.
    async with session_scope() as session:
        view = await load_snapshot_view(session, MOUTAI, AS_OF)
    period = view.latest_annual()
    assert period is not None and period.report_period == FY2025
    assert period.fields["roe"] == pytest.approx(0.303)
    assert period.source("roe").provider == "ifind"
    assert period.source("roe").verification == "verified"
    assert period.fields["revenue_total"] == pytest.approx(1.75e11)
    assert period.source("revenue_total").verification == "verified"
    assert period.fields["debt_ratio"] == pytest.approx(0.25)
    assert period.source("debt_ratio").provider == "ifind"
    assert period.source("debt_ratio").verification == "conflict"
    assert period.source("debt_ratio").conflicts[0]["value"] == pytest.approx(0.20)
    # Not visible before the verification happened (point in time).
    async with session_scope() as session:
        before = await load_snapshot_view(session, MOUTAI, date(2026, 10, 5))
    old = before.latest_annual()
    assert old is not None and old.fields["roe"] == pytest.approx(0.30)

    # Key-field conflict -> one action_required notification.
    notes = list((tmp_path / "notifications").rglob("*.md"))
    assert len(notes) == 1
    body = notes[0].read_text(encoding="utf-8")
    assert "debt_ratio（关键）" in body and MOUTAI in body
    assert run.metadata_json["key_field_conflicts"] == [MOUTAI]

    # A later conflict-only re-check keeps the earlier verifications of the period.
    proxy.calls.clear()
    ifind_answers(proxy, MOUTAI, ths_asset_liab_ratio_stock="20.1")
    await enqueue_verification(
        listing_id=lid, ticker=MOUTAI, trigger="conflict", fields=["debt_ratio"],
        report_period=FY2025,
    )
    rerun = await run_verify(runtime(tmp_path, proxy))
    assert rerun.status == "success"
    assert [path for path, _ in proxy.calls] == ["/financial/index"]
    latest = (await ifind_rows(lid))[-1]
    assert latest.fields["verification"]["debt_ratio"] == "verified"
    assert latest.fields["verification"]["roe"] == "verified"
    assert latest.fields["roe"] == pytest.approx(0.303)  # carried over
    async with session_scope() as session:
        view = await load_snapshot_view(session, MOUTAI, AS_OF)
    period = view.latest_annual()
    assert period is not None and period.fields["debt_ratio"] == pytest.approx(0.201)
    assert period.source("debt_ratio").provider == "ifind"


async def test_batches_three_tickers_per_call_grouped_by_period(db_session, tmp_path):
    del db_session
    tickers = ["000001.SZ", "000002.SZ", "300750.SZ", "600519.SH"]
    ids = await seed_watchlist(tickers)
    proxy = FakeProxy()
    for ticker in tickers:
        ifind_answers(proxy, ticker)
        await enqueue_verification(
            listing_id=ids[ticker], ticker=ticker, trigger="missing", fields=["roe"],
            report_period=FY2025,
        )
    ifind_answers(proxy, MOUTAI, period=H1_2026)
    await enqueue_verification(
        listing_id=ids[MOUTAI], ticker=MOUTAI, trigger="conflict", fields=["roe"],
        report_period=H1_2026,
    )
    run = await run_verify(runtime(tmp_path, proxy))
    assert run.status == "success"
    calls = proxy.calls_to("/financial/index")
    assert [(c["ticker"], c["report_date"]) for c in calls] == [
        # the conflict request (higher priority) leads with its own period
        ("600519.SH", "20260630"),
        ("000001.SZ,000002.SZ,300750.SZ", "20251231"),
        ("600519.SH", "20251231"),
    ]
    assert all(r.status == "done" and r.result and r.result["verified"] == ["roe"]
               for r in await requests())
    assert run.metadata_json["units"] == 3 and run.metadata_json["proxy_calls"] == 3


async def test_requests_of_one_listing_and_period_fold_into_the_highest_priority(
    db_session, tmp_path
):
    del db_session
    ids = await seed_watchlist([MOUTAI])
    lid = ids[MOUTAI]
    await seed_free(lid)
    proxy = FakeProxy()
    ifind_answers(proxy, MOUTAI, ths_asset_liab_ratio_stock="20.0")
    await enqueue_verification(listing_id=lid, ticker=MOUTAI, trigger="calibration")
    await enqueue_verification(
        listing_id=lid, ticker=MOUTAI, trigger="conflict", fields=["debt_ratio"],
        report_period=FY2025,
    )
    run = await run_verify(runtime(tmp_path, proxy))
    assert run.status == "success"
    conflict, calibration = sorted(await requests(), key=lambda r: r.trigger != "conflict")
    assert conflict.status == "done"
    assert calibration.status == "superseded"
    assert calibration.result == {"superseded_by": conflict.id}
    assert conflict.fields is None  # absorbed the calibration's "all key fields"
    assert len(proxy.calls) == 6  # one full check, not two
    assert conflict.result is not None and conflict.result["conflicts"] == []
    assert run.metadata_json["superseded"] == 1
    assert not list((tmp_path / "notifications").rglob("*.md"))  # no conflict, no notice


async def test_budget_shortfall_defers_until_the_window_frees(db_session, tmp_path):
    del db_session
    ids = await seed_watchlist(["000001.SZ", MOUTAI])
    proxy = FakeProxy()
    ifind_answers(proxy, MOUTAI, period=H1_2026)
    ifind_answers(proxy, "000001.SZ")
    earlier = NOW - timedelta(hours=4)
    async with session_scope() as session:
        for _ in range(7):  # spent by another path (e.g. research) 4 hours ago
            await record_call(
                session, at=earlier, endpoint="/stock/history", tickers=1, outcome="ok",
                source="research",
            )
    await enqueue_verification(
        listing_id=ids[MOUTAI], ticker=MOUTAI, trigger="promote", fields=["roe"],
        report_period=H1_2026,
    )
    await enqueue_verification(
        listing_id=ids["000001.SZ"], ticker="000001.SZ", trigger="calibration", fields=["roe"],
        report_period=FY2025,
    )
    rt = runtime(tmp_path, proxy, ifind_budget_per_5h=8)
    run = await run_verify(rt)
    assert run.status == "success"  # deferral is not an error
    assert len(proxy.calls) == 1
    promote, calibration = await requests()
    assert promote.status == "done"
    assert calibration.status == "deferred"
    free_at = earlier + timedelta(hours=5)
    assert calibration.not_before is not None
    assert calibration.not_before.replace(tzinfo=UTC) == free_at
    assert run.metadata_json["stop_reason"] == "budget"
    assert run.metadata_json["deferred_until"] == free_at.isoformat()
    assert run.metadata_json["budget_after"]["remaining"] == 0

    # Nothing due before not_before; the first dispatch slot after it runs again.
    assert (await dispatch_verification(now=NOW + timedelta(minutes=30))).due == 0
    later = NOW + timedelta(hours=1)
    dispatched = await dispatch_verification(now=later)
    assert dispatched.due == 1 and dispatched.created
    async with session_scope() as session:
        queued = await session.get(PipelineRun, dispatched.run_id)
        assert queued is not None
        queued.status = "success"  # handled below by an explicit run instead
    rerun = await run_verify(runtime(tmp_path, proxy, clock=lambda: later, ifind_budget_per_5h=8))
    assert rerun.status == "success"
    assert (await requests())[1].status == "done"


async def test_quota_stops_defers_everything_and_notifies_once(db_session, tmp_path):
    del db_session
    ids = await seed_watchlist(["000001.SZ", MOUTAI])
    proxy = FakeProxy()

    def quota(path: str, payload: dict[str, Any]) -> Exception:
        return QuotaExhaustedError(
            "iFinD quota exhausted (HTTP 429)", provider="ifind", status_code=429,
            detail="usage limit",
        )

    proxy.fail = quota
    for ticker in ids:
        await enqueue_verification(listing_id=ids[ticker], ticker=ticker, trigger="promote")
    rt = runtime(tmp_path, proxy)
    run = await run_verify(rt)
    assert run.status == "partial"
    assert len(proxy.calls) == 1  # no retry, no further batch
    for request in await requests():
        assert request.status == "deferred"
        assert request.not_before is not None
        assert request.not_before.replace(tzinfo=UTC) == NOW + timedelta(hours=5)
    assert run.metadata_json["stop_reason"] == "quota"
    notes = list((tmp_path / "notifications").rglob("*.md"))
    assert len(notes) == 1 and "额度耗尽" in notes[0].read_text(encoding="utf-8")
    await notify_verification_run(run.id, notifier=rt.file_notifier())
    assert len(list((tmp_path / "notifications").rglob("*.md"))) == 1
    async with session_scope() as session:
        (entry,) = (await session.execute(select(IfindCallLedger))).scalars()
    assert entry.outcome == "quota"


async def test_unit_failures_retry_then_fail(db_session, tmp_path):
    del db_session
    ids = await seed_watchlist([MOUTAI])
    proxy = FakeProxy()
    proxy.fail = lambda path, payload: ValueError("unexpected proxy payload")
    await enqueue_verification(
        listing_id=ids[MOUTAI], ticker=MOUTAI, trigger="missing", fields=["roe"],
        report_period=FY2025,
    )
    clock = {"now": NOW}
    rt = runtime(tmp_path, proxy, clock=lambda: clock["now"])
    run = await run_verify(rt)
    assert run.status == "partial"
    (request,) = await requests()
    assert (request.status, request.attempts) == ("deferred", 1)
    assert request.not_before is not None
    assert request.not_before.replace(tzinfo=UTC) == NOW + timedelta(minutes=30)
    for step in (1, 2):
        clock["now"] = NOW + timedelta(hours=step)
        await run_verify(rt)
    (request,) = await requests()
    assert (request.status, request.attempts) == ("failed", 3)
    assert "unexpected proxy payload" in str(request.result)


async def test_no_ifind_data_and_unverifiable_fields_are_unavailable(db_session, tmp_path):
    del db_session
    ids = await seed_watchlist([MOUTAI])
    proxy = FakeProxy()  # answers blank rows: period not disclosed
    await enqueue_verification(
        listing_id=ids[MOUTAI], ticker=MOUTAI, trigger="missing",
        fields=["roe", "eps_ttm", "close"], report_period=H1_2026,
    )
    run = await run_verify(runtime(tmp_path, proxy))
    assert run.status == "success"
    (request,) = await requests()
    assert request.status == "done" and request.result is not None
    assert request.result["unavailable"] == ["close", "eps_ttm", "roe"]
    assert request.result["fields"]["close"]["reason"] == "no baostock bar to compare with"
    assert await ifind_rows(ids[MOUTAI]) == []
    assert [path for path, _ in proxy.calls] == ["/financial/index"]


async def test_empty_queue_is_a_quiet_success(db_session, tmp_path):
    del db_session
    run = await run_verify(runtime(tmp_path, FakeProxy()))
    assert run.status == "success" and run.metadata_json["picked"] == 0
