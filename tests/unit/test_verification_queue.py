"""Verification request queue (P1-A13): priority, dedup / merge, deferral, hooks."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select

from investment_agent.snapshots import eastmoney
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.verification import VerificationRequest
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository
from investment_agent.verification import TRIGGER_PRIORITY, enqueue_verification
from investment_agent.verification.queue import (
    count_due,
    due_requests,
    queue_summary,
    release_stale_running,
)
from tests.unit._snapshot_support import seed_watchlist

NOW = datetime(2026, 10, 7, 1, 0, tzinfo=UTC)
FY2025 = date(2025, 12, 31)


async def _requests() -> list[VerificationRequest]:
    async with session_scope() as session:
        stmt = select(VerificationRequest).order_by(VerificationRequest.id)
        return list((await session.execute(stmt)).scalars())


def test_priorities_follow_the_trigger_order():
    assert sorted(TRIGGER_PRIORITY, key=TRIGGER_PRIORITY.__getitem__, reverse=True) == [
        "promote",
        "conflict",
        "missing",
        "exit_review",
        "calibration",
    ]


async def test_dedup_merges_fields_detail_and_keeps_queue_place(db_session):
    del db_session
    ids = await seed_watchlist(["600519.SH"])
    lid = ids["600519.SH"]
    assert await enqueue_verification(
        listing_id=lid, ticker="600519.SH", trigger="conflict", fields=["debt_ratio"],
        report_period=FY2025, detail={"debt_ratio": {"akshare": 0.2}}, run_id=7, now=NOW,
    )
    assert not await enqueue_verification(
        listing_id=lid, ticker="600519.SH", trigger="conflict",
        fields=["operating_cash_flow", "debt_ratio"], report_period=FY2025,
        detail={"operating_cash_flow": {"akshare": 1.0}}, now=NOW + timedelta(hours=1),
    )
    # A different period or trigger is a separate request.
    assert await enqueue_verification(
        listing_id=lid, ticker="600519.SH", trigger="conflict", fields=["roe"],
        report_period=date(2026, 6, 30), now=NOW,
    )
    assert await enqueue_verification(
        listing_id=lid, ticker="600519.SH", trigger="promote", now=NOW
    )
    # ``None`` (all key fields) absorbs any list.
    assert not await enqueue_verification(
        listing_id=lid, ticker="600519.SH", trigger="promote", fields=["roe"], now=NOW
    )
    rows = await _requests()
    assert len(rows) == 3
    conflict = rows[0]
    assert conflict.fields == ["debt_ratio", "operating_cash_flow"]
    assert set(conflict.detail) == {"debt_ratio", "operating_cash_flow"}
    assert conflict.source_run_id == 7
    assert conflict.priority == TRIGGER_PRIORITY["conflict"]
    assert conflict.requested_at.replace(tzinfo=UTC) == NOW
    assert rows[2].trigger == "promote" and rows[2].fields is None

    # Once a request is done, a new one opens again.
    async with session_scope() as session:
        done = await session.get(VerificationRequest, conflict.id)
        assert done is not None
        done.status = "done"
    assert await enqueue_verification(
        listing_id=lid, ticker="600519.SH", trigger="conflict", fields=["debt_ratio"],
        report_period=FY2025, now=NOW,
    )


async def test_enqueue_inside_the_callers_transaction(db_session):
    del db_session
    ids = await seed_watchlist(["600519.SH"])

    class PromoteFailedError(RuntimeError):
        pass

    with pytest.raises(PromoteFailedError):
        async with session_scope() as session:
            assert await enqueue_verification(
                listing_id=ids["600519.SH"], ticker="600519.SH", trigger="promote",
                session=session,
            )
            raise PromoteFailedError  # the promote flow failed: the request rolls back with it
    assert await _requests() == []

    async with session_scope() as session:
        await enqueue_verification(
            listing_id=ids["600519.SH"], ticker="600519.SH", trigger="promote", session=session
        )
    assert len(await _requests()) == 1


async def test_invalid_input_is_rejected(db_session):
    del db_session
    with pytest.raises(ValueError):
        await enqueue_verification(listing_id=1, ticker="600519.SH", trigger="whim")
    with pytest.raises(ValueError):
        await enqueue_verification(listing_id=1, ticker="600519.SH", trigger="promote", fields=[])


async def test_due_selection_priority_and_not_before(db_session):
    del db_session
    ids = await seed_watchlist(["600519.SH", "000001.SZ", "300750.SZ"])
    await enqueue_verification(
        listing_id=ids["600519.SH"], ticker="600519.SH", trigger="calibration", now=NOW
    )
    await enqueue_verification(
        listing_id=ids["000001.SZ"], ticker="000001.SZ", trigger="missing",
        fields=["net_profit_parent"], report_period=FY2025, now=NOW + timedelta(minutes=5),
    )
    await enqueue_verification(
        listing_id=ids["300750.SZ"], ticker="300750.SZ", trigger="promote",
        now=NOW + timedelta(minutes=10),
    )
    await enqueue_verification(
        listing_id=ids["600519.SH"], ticker="600519.SH", trigger="missing",
        fields=["revenue_total"], report_period=FY2025, now=NOW + timedelta(minutes=1),
    )
    async with session_scope() as session:
        due = await due_requests(session, now=NOW + timedelta(hours=1))
        assert [(r.ticker, r.trigger) for r in due] == [
            ("300750.SZ", "promote"),
            ("600519.SH", "missing"),  # same priority: earlier request first
            ("000001.SZ", "missing"),
            ("600519.SH", "calibration"),
        ]
        due[0].status = "deferred"
        due[0].not_before = NOW + timedelta(hours=3)
    async with session_scope() as session:
        assert await count_due(session, now=NOW + timedelta(hours=1)) == 3
        assert await count_due(session, now=NOW + timedelta(hours=3)) == 4
        summary = await queue_summary(session)
    assert summary["deferred"] == {"promote": 1}
    assert summary["pending"] == {"calibration": 1, "missing": 2}


async def test_stale_running_requests_return_to_the_queue(db_session):
    del db_session
    ids = await seed_watchlist(["600519.SH", "000001.SZ"])
    for ticker in ids:
        await enqueue_verification(listing_id=ids[ticker], ticker=ticker, trigger="promote")
    async with session_scope() as session:
        run, _ = await PipelineRunRepository(session).enqueue(
            kind="ifind_verify", idempotency_key="x", triggered_by="test"
        )
        await PipelineRunRepository(session).mark_failed(run.id, "crashed")
        for row in (await session.execute(select(VerificationRequest))).scalars():
            row.status = "running"
            row.run_id = run.id
    # Meanwhile a new promote request for 000001.SZ was queued.
    await enqueue_verification(
        listing_id=ids["000001.SZ"], ticker="000001.SZ", trigger="promote", fields=["roe"]
    )
    async with session_scope() as session:
        assert await release_stale_running(session, now=NOW) == 2
    rows = await _requests()
    assert [(r.ticker, r.status) for r in rows] == [
        ("600519.SH", "pending"),
        ("000001.SZ", "superseded"),
        ("000001.SZ", "pending"),
    ]
    assert rows[2].fields is None  # absorbed the stale request's "all fields"


async def test_eastmoney_hook_delegates_to_the_queue(db_session):
    del db_session
    ids = await seed_watchlist(["601398.SH"])
    created = await eastmoney.enqueue_verification(
        listing_id=ids["601398.SH"],
        ticker="601398.SH",
        report_period=FY2025,
        fields=["debt_ratio"],
        trigger="conflict",
        detail={"debt_ratio": {"akshare": 0.918, "baostock": 0.95}},
        run_id=3,
    )
    again = await eastmoney.enqueue_verification(
        listing_id=ids["601398.SH"], ticker="601398.SH", report_period=FY2025,
        fields=["revenue_total"], trigger="missing", run_id=3,
    )
    assert created and again
    rows = await _requests()
    assert [(r.trigger, r.fields, r.report_period, r.source_run_id) for r in rows] == [
        ("conflict", ["debt_ratio"], FY2025, 3),
        ("missing", ["revenue_total"], FY2025, 3),
    ]
    assert rows[0].detail["debt_ratio"]["baostock"] == 0.95
    # A failing enqueue never breaks the collector.
    assert not await eastmoney.enqueue_verification(
        listing_id=ids["601398.SH"], ticker="601398.SH", report_period=FY2025,
        fields=[], trigger="missing",
    )
