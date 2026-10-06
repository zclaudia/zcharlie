"""Disclosure deadlines and report_overdue lifecycle (P1-A6)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from investment_agent.providers.baostock_market import BaostockMarketClient
from investment_agent.research.worker import ResearchWorker
from investment_agent.scheduler.snapshot_jobs import snapshot_payloads
from investment_agent.scheduler.trading_calendar import load_trading_calendar
from investment_agent.snapshots.breadth import collect_baostock_financial
from investment_agent.snapshots.collect import ArtifactWriter
from investment_agent.snapshots.disclosure import is_past_deadline, statutory_deadline
from investment_agent.snapshots.jobs import FinancialSnapshotPayload, enqueue_snapshot_job
from investment_agent.storage.database import session_scope
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository
from investment_agent.storage.repositories.report_overdue import ReportOverdueRepository
from tests.unit._baostock_fake import FakeBaostockData
from tests.unit._snapshot_support import (
    CAPTURED_AT,
    FakeProxy,
    handlers_for,
    seed_universe,
    seed_watchlist,
    settings_for,
)

H1 = date(2026, 6, 30)
POOL = ["600519.SH", "601398.SH"]  # 601398 2026H1 is absent from the recording


@pytest.mark.parametrize(
    ("period", "deadline"),
    [
        (date(2026, 3, 31), date(2026, 4, 30)),
        (date(2026, 6, 30), date(2026, 8, 31)),
        (date(2026, 9, 30), date(2026, 10, 31)),
        (date(2025, 12, 31), date(2026, 4, 30)),  # annual: next year
        (date(2026, 12, 31), date(2027, 4, 30)),
    ],
)
def test_statutory_deadline(period, deadline):
    assert statutory_deadline(period) == deadline
    assert not is_past_deadline(period, deadline)  # the deadline day itself is allowed
    assert is_past_deadline(period, date.fromordinal(deadline.toordinal() + 1))


def test_statutory_deadline_rejects_non_quarter_end():
    with pytest.raises(ValueError):
        statutory_deadline(date(2026, 5, 31))


def _client(backend: FakeBaostockData) -> BaostockMarketClient:
    return BaostockMarketClient(  # type: ignore[arg-type]
        backend, clock=lambda: CAPTURED_AT, retry_backoff_seconds=0
    )


async def _collect(backend, tmp_path, as_of: date, *, scope: str = "universe", run_id=None):
    payload = FinancialSnapshotPayload(
        scope=scope,
        universe_code="CSI300" if scope == "universe" else None,
        report_period=H1,
        as_of=as_of,
    )
    return await collect_baostock_financial(
        payload,
        client=_client(backend),
        artifacts=ArtifactWriter(tmp_path, run_id=run_id or 1, attempt=1),
        lease=None,
        now=lambda: CAPTURED_AT,
        run_id=run_id,
    )


def _publish_late(backend: FakeBaostockData, pub_date: str) -> None:
    """601398 publishes its H1 report late (synthesized from the 600519 rows)."""

    for method in ("query_profit_data", "query_balance_data", "query_cash_flow_data",
                   "query_growth_data"):
        record = backend.quarterly[(method, "sh.600519", "2026", "2")]
        row = ["sh.601398", pub_date, *record["rows"][0][2:]]
        backend.quarterly[(method, "sh.601398", "2026", "2")] = {
            "fields": record["fields"], "rows": [row]
        }


async def _overdue(listing_id: int):
    async with session_scope() as session:
        return await ReportOverdueRepository(session).for_period(H1, [listing_id])


async def test_no_detection_until_the_deadline_has_passed(db_session, tmp_path):
    del db_session
    ids = await seed_universe(POOL)
    report = await _collect(FakeBaostockData(), tmp_path, date(2026, 8, 31))
    assert report.completeness.pending == ["601398.SH"]
    assert report.metadata["report_overdue"]["checked"] is False
    assert await _overdue(ids["601398.SH"]) == []


async def test_overdue_lifecycle_detect_once_then_resolve(db_session, tmp_path):
    del db_session
    ids = await seed_universe(POOL)
    bank = ids["601398.SH"]

    first = await _collect(FakeBaostockData(), tmp_path, date(2026, 9, 1))
    overdue = first.metadata["report_overdue"]
    assert overdue == {
        "deadline": "2026-08-31",
        "checked": True,
        "detected": ["601398.SH"],
        "open": ["601398.SH"],
        "resolved": [],
    }
    (row,) = await _overdue(bank)
    assert (row.detected_on, row.resolved_on, row.deadline) == (
        date(2026, 9, 1), None, date(2026, 8, 31)
    )

    # A later scan does not detect (or notify) the same period again.
    again = await _collect(FakeBaostockData(), tmp_path, date(2026, 9, 5))
    assert again.metadata["report_overdue"]["detected"] == []
    assert again.metadata["report_overdue"]["open"] == ["601398.SH"]

    late = FakeBaostockData()
    _publish_late(late, "2026-09-10")
    resolved = await _collect(late, tmp_path, date(2026, 9, 12))
    assert resolved.metadata["report_overdue"]["resolved"] == ["601398.SH"]
    assert resolved.metadata["report_overdue"]["open"] == []
    (row,) = await _overdue(bank)
    assert row.detected_on == date(2026, 9, 1)  # history kept
    assert row.resolved_on == date(2026, 9, 10)  # the actual pubDate

    async with session_scope() as session:
        repository = ReportOverdueRepository(session)
        assert await repository.open_as_of(bank, date(2026, 8, 31)) == []  # not yet known
        assert [r.report_period for r in await repository.open_as_of(bank, date(2026, 9, 9))] == [H1]
        assert await repository.open_as_of(bank, date(2026, 9, 10)) == []
        # A resolved record is never reopened by a later scan.
        assert not await repository.record(
            listing_id=bank, report_period=H1, deadline=date(2026, 8, 31),
            detected_on=date(2026, 9, 20), provider="baostock",
        )


async def test_failed_members_and_watchlist_scope_are_not_flagged(db_session, tmp_path):
    del db_session
    ids = await seed_universe(POOL)
    failing = FakeBaostockData(
        fail=lambda method, params: ("10004011", "bad") if params["code"] == "sh.601398" else None
    )
    report = await _collect(failing, tmp_path, date(2026, 9, 1))
    assert report.completeness.failed == ["601398.SH"]
    assert report.metadata["report_overdue"]["detected"] == []
    assert await _overdue(ids["601398.SH"]) == []

    await seed_watchlist(["601398.SH"])
    watch = await _collect(FakeBaostockData(), tmp_path, date(2026, 9, 1), scope="watchlist")
    assert watch.metadata["report_overdue"]["checked"] is False


async def test_overdue_run_notifies_once(db_session, tmp_path):
    del db_session
    await seed_universe(POOL)
    settings = settings_for(tmp_path)
    worker = ResearchWorker(
        settings=settings,
        worker_id="w",
        handlers=handlers_for(settings, FakeProxy(), baostock=_client(FakeBaostockData())),
    )
    run_ids = []
    for as_of in (date(2026, 9, 1), date(2026, 9, 5)):
        async with session_scope() as session:
            run, _ = await enqueue_snapshot_job(
                session,
                FinancialSnapshotPayload(universe_code="CSI300", report_period=H1, as_of=as_of),
                triggered_by="scheduler",
            )
            run_ids.append(run.id)
        assert await worker.run_once() == run.id
    async with session_scope() as session:
        first = await PipelineRunRepository(session).get(run_ids[0])
        (row,) = await ReportOverdueRepository(session).for_period(H1)
    assert first.status == "success"
    assert row.detected_run_id == run_ids[0]
    notes = list((tmp_path / "notifications").rglob("*.md"))
    assert len(notes) == 1  # the second scan found nothing new
    text = notes[0].read_text()
    assert "action_required" in text and "逾期未披露" in text and "601398.SH" in text
    assert "2026-08-31" in text


def test_deadline_trigger_schedules_the_scan():
    calendar = load_trading_calendar(
        Path(__file__).resolve().parents[2] / "config" / "calendars" / "a_share.yaml"
    )
    payloads = snapshot_payloads(
        "financial_deadline", today=date(2026, 9, 1), calendar=calendar, universes=["CSI300"]
    )
    assert [(p.provider, p.scope, p.report_period, p.as_of) for p in payloads] == [  # type: ignore[union-attr]
        ("baostock", "universe", H1, date(2026, 9, 1))
    ]
    assert is_past_deadline(H1, date(2026, 9, 1))
