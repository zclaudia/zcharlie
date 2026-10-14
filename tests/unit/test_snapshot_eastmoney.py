"""Eastmoney (AkShare) financial collector (P1-A11): storage, point in time,
restatements, cross-check, versioning, throttling / stop, jobs and CLI."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from investment_agent.factors.catalog import DEFAULT_REGISTRY
from investment_agent.factors.view_db import load_snapshot_view
from investment_agent.providers.akshare_eastmoney import EastmoneyTransientError
from investment_agent.research.worker import ResearchWorker
from investment_agent.snapshots.collect import ArtifactWriter
from investment_agent.snapshots.eastmoney import (
    MAX_CONSECUTIVE_FAILURES,
    akshare_backfill_payloads,
    collect_akshare_financial,
    plan_akshare_backfill,
)
from investment_agent.snapshots.jobs import (
    FinancialSnapshotPayload,
    enqueue_snapshot_job,
    idempotency_key,
    request_json,
)
from investment_agent.storage.database import init_engine, session_scope
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.base import Base
from investment_agent.storage.models.pit_snapshot import FinancialSnapshot
from investment_agent.storage.repositories.financial_snapshot import FinancialSnapshotRepository
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository
from tests.unit._eastmoney_fake import (
    CAPTURED_AT,
    FakeEastmoneyBackend,
    fake_client,
    summary_table,
)
from tests.unit._snapshot_support import (
    FakeProxy,
    handlers_for,
    seed_universe,
    seed_watchlist,
    settings_for,
)

FY2024 = date(2024, 12, 31)
FY2025 = date(2025, 12, 31)
H1_2025 = date(2025, 6, 30)
H1_2026 = date(2026, 6, 30)
AS_OF = date(2026, 10, 7)
POOL = ["600519.SH", "601398.SH", "601318.SH", "600030.SH", "000001.SZ"]


def _summary(period: date = FY2024, **kwargs) -> FinancialSnapshotPayload:
    kwargs.setdefault("universe_code", "CSI300")
    return FinancialSnapshotPayload(
        provider="akshare", part="summary", report_period=period, as_of=AS_OF, **kwargs
    )


def _cash_flow(**kwargs) -> FinancialSnapshotPayload:
    kwargs.setdefault("universe_code", "CSI300")
    return FinancialSnapshotPayload(
        provider="akshare",
        part="cash_flow",
        report_period=H1_2026,
        extra_periods=(FY2025, H1_2025),
        as_of=AS_OF,
        **kwargs,
    )


async def _collect(payload, tmp_path, backend=None, **client_kwargs):
    return await collect_akshare_financial(
        payload,
        client=fake_client(backend, **client_kwargs),
        artifacts=ArtifactWriter(tmp_path, run_id=1, attempt=1),
        lease=None,
        now=lambda: CAPTURED_AT,
    )


async def _seed_baostock(listing_id: int, period: date, pub: date, **fields) -> None:
    async with session_scope() as session:
        await FinancialSnapshotRepository(session).insert(
            listing_id=listing_id,
            report_period=period,
            period_kind="annual" if period.month == 12 else "semi",
            fields={"roe": 0.3, **fields},
            provider="baostock",
            captured_at=datetime(2026, 5, 1, tzinfo=UTC),
            availability="actual",
            available_from=pub,
            is_backfill=True,
        )


async def _akshare_rows(part: str | None = None) -> list[FinancialSnapshot]:
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(FinancialSnapshot)
                .where(FinancialSnapshot.provider == "akshare")
                .order_by(FinancialSnapshot.listing_id, FinancialSnapshot.report_period,
                          FinancialSnapshot.version)
            )
        ).scalars().all()
    return [row for row in rows if part is None or row.fields.get("part") == part]


# --------------------------------------------------------------------------- #
# Period summaries
# --------------------------------------------------------------------------- #


async def test_summary_stores_scope_with_pit_availability_and_restatements(db_session, tmp_path):
    del db_session
    ids = await seed_universe(POOL)
    watch = await seed_watchlist(["000333.SZ"])
    # baostock FY2024: 600519 carries the 1/100 debt-ratio defect, 601398 disagrees.
    await _seed_baostock(
        ids["600519.SH"], FY2024, date(2025, 4, 3),
        debt_ratio=0.0019, cfo_to_np=1.07, net_profit=86_228_146_421.62,
    )
    await _seed_baostock(ids["601398.SH"], FY2024, date(2025, 3, 29), debt_ratio=0.95)

    report = await _collect(_summary(), tmp_path)

    assert report.status == "success"
    metadata = report.to_metadata()
    assert metadata["stored"] == 6 and metadata["in_scope"] == 6  # 5 pool + watchlist
    rows = {row.listing_id: row for row in await _akshare_rows("summary")}
    assert set(rows) == {*ids.values(), *watch.values()}

    moutai = rows[ids["600519.SH"]]
    assert moutai.availability == "actual"
    assert moutai.available_from == date(2025, 4, 3)  # baostock pubDate, not 2026-04-17
    assert moutai.fields["availability_basis"] == "baostock_pub_date"
    assert moutai.fields["notice_date"] == "2026-04-17"
    assert moutai.fields["restated"] is True
    assert moutai.fields["restated_basis"] == "notice_after_baostock_pub_date"
    assert moutai.fields["debt_ratio"] == pytest.approx(0.190447556579)
    assert moutai.is_backfill is False and moutai.raw_ref

    insurer = rows[ids["601318.SH"]]
    assert insurer.availability == "approx"
    assert insurer.available_from == date(2026, 3, 27)  # Eastmoney notice (latest)
    assert insurer.fields["restated_basis"] == "notice_after_statutory_deadline"
    assert insurer.fields["restated"] is True

    bank = rows[ids["601398.SH"]]
    conflict = bank.fields["provider_conflicts"]["debt_ratio"]
    assert conflict["baostock"] == 0.95 and conflict["akshare"] == pytest.approx(0.91833012281)
    cross = metadata["provider_conflict"]
    assert cross["count"] == 1 and cross["by_field"] == {"debt_ratio": 1}
    assert cross["baostock_suspect_debt_ratio"] == {"600519.SH": 0.0019}
    # baostock CFOToNP x netProfit is within 2% of Eastmoney operating cash flow here
    assert "provider_conflicts" not in moutai.fields

    # The target-pool view now falls back to Eastmoney for the suspect debt ratio.
    async with session_scope() as session:
        view = await load_snapshot_view(session, "600519.SH", AS_OF)
    debt = DEFAULT_REGISTRY.get("debt_ratio").compute(view)
    assert debt.value == pytest.approx(0.190447556579)
    assert "provider_fallback" in debt.flags
    assert debt.inputs_ref["financial"][0]["sources"]["debt_ratio"]["provider"] == "akshare"
    async with session_scope() as session:
        bank_view = await load_snapshot_view(session, "601398.SH", AS_OF)
    assert bank_view.latest_annual().source("debt_ratio").verification == "conflict"


async def test_summary_capture_is_versioned_not_overwritten(db_session, tmp_path):
    del db_session
    await seed_universe(["600519.SH", "000001.SZ"])
    first = await _collect(_summary(), tmp_path)
    assert first.to_metadata()["stored"] == 2

    again = await _collect(_summary(), tmp_path)
    assert again.to_metadata()["stored"] == 0 and again.to_metadata()["unchanged"] == 2
    assert len(await _akshare_rows()) == 2

    table = summary_table("stock_lrb_em", "20241231")
    rows = tuple(
        {**row, "净利润": 1.0} if row["股票代码"] == "600519" else row for row in table.rows
    )
    backend = FakeEastmoneyBackend(
        overrides={("stock_lrb_em", "20241231"): replace(table, rows=rows)}
    )
    restated = await _collect(_summary(), tmp_path, backend)
    assert restated.to_metadata()["stored"] == 1
    stored = [row for row in await _akshare_rows() if row.fields["net_profit_parent"] in (1.0,)]
    assert len(stored) == 1 and stored[0].version == 2
    versions = sorted(
        (row.version, row.fields["net_profit_parent"]) for row in await _akshare_rows()
        if row.listing_id == stored[0].listing_id
    )
    assert versions[0][0] == 1 and versions[0][1] == pytest.approx(86_228_146_421.62)


async def test_undisclosed_period_is_pending(db_session, tmp_path):
    del db_session
    await seed_universe(POOL)
    report = await _collect(_summary(date(2026, 9, 30)), tmp_path)
    assert report.status == "success"
    assert report.completeness.pending == sorted(POOL)
    assert await _akshare_rows() == []


async def test_financial_stock_gap_is_missing_not_an_error(db_session, tmp_path):
    del db_session
    await seed_universe(["601318.SH"])
    table = summary_table("stock_lrb_em", "20241231")
    rows = tuple(
        {**row, "净利润": None} if row["股票代码"] == "601318" else row for row in table.rows
    )
    backend = FakeEastmoneyBackend(
        overrides={("stock_lrb_em", "20241231"): replace(table, rows=rows)}
    )
    report = await _collect(_summary(), tmp_path, backend)
    assert report.status == "success"
    (row,) = await _akshare_rows()
    assert row.fields["net_profit_parent"] is None
    assert row.fields["missing_fields"] == ["net_profit_parent"]
    assert report.to_metadata()["missing_key_fields"] == {"601318.SH": ["net_profit_parent"]}


async def test_summary_failure_fails_the_run(db_session, tmp_path):
    del db_session
    await seed_universe(["600519.SH"])
    backend = FakeEastmoneyBackend(errors=[EastmoneyTransientError("down", provider="akshare")])
    report = await _collect(_summary(), tmp_path, backend, max_attempts=1)
    assert report.status == "failed" and report.all_failures_transient


async def test_backfill_payload_uses_survivorship_set_and_marks_rows(db_session, tmp_path):
    del db_session
    await seed_universe(["600519.SH"])
    payloads = akshare_backfill_payloads(
        "CSI300", AS_OF, annual_years=2, quarters=1, with_cash_flow=False
    )
    assert [p.report_period for p in payloads] == [H1_2026, FY2025, FY2024]
    report = await _collect(payloads[-1], tmp_path)
    assert report.status == "success"
    (row,) = await _akshare_rows()
    assert row.is_backfill is True


# --------------------------------------------------------------------------- #
# Cash-flow statements
# --------------------------------------------------------------------------- #


async def test_cash_flow_statements_store_requested_periods(db_session, tmp_path):
    del db_session
    ids = await seed_universe(["600519.SH", "601318.SH", "000333.SZ"])
    await _seed_baostock(ids["600519.SH"], H1_2026, date(2026, 8, 15))

    report = await _collect(_cash_flow(), tmp_path)
    metadata = report.to_metadata()
    assert report.status == "success"  # 000333 has no statement: no_data, not an error
    assert report.completeness.tickers("no_data") == ["000333.SZ"]
    assert metadata["rows_stored"] == 6 and metadata["requested"] == 3

    rows = {(row.listing_id, row.report_period): row for row in await _akshare_rows("cash_flow")}
    latest = rows[(ids["600519.SH"], H1_2026)]
    assert latest.fields["capital_expenditure"] == pytest.approx(832_142_752.28)
    assert latest.fields["free_cash_flow"] == pytest.approx(70_690_750_119.06 - 832_142_752.28)
    assert latest.availability == "actual" and latest.available_from == date(2026, 8, 15)
    assert latest.fields["restated"] is False
    restated = rows[(ids["600519.SH"], H1_2025)]
    assert restated.fields["restated"] is True
    assert restated.availability == "approx" and restated.available_from == date(2025, 8, 13)
    assert restated.fields["update_date"] == "2026-08-15"
    insurer = rows[(ids["601318.SH"], FY2025)]
    assert insurer.fields["capital_expenditure"] == 8_355_000_000

    # Second run: every listing already has all periods -> no slow calls.
    client = fake_client()
    again = await collect_akshare_financial(
        _cash_flow(),
        client=client,
        artifacts=ArtifactWriter(tmp_path, run_id=2, attempt=1),
        lease=None,
        now=lambda: CAPTURED_AT,
    )
    assert client.calls == 1  # only 000333 (no statement) is retried
    assert again.to_metadata()["rows_stored"] == 0


async def test_three_consecutive_failures_stop_the_job_partial(db_session, tmp_path):
    del db_session
    # processed in ticker order: 600030 ok, then three failures, 601398 never called
    tickers = ["600030.SH", "600519.SH", "601318.SH", "601398.SH", "688981.SH", "000001.SZ"]
    await seed_universe(tickers[:-1])
    error = EastmoneyTransientError("HTTP 502", provider="akshare")
    backend = FakeEastmoneyBackend(errors=[None, error, error, error])
    report = await _collect(_cash_flow(), tmp_path, backend, max_attempts=1)
    assert MAX_CONSECUTIVE_FAILURES == 3
    assert report.status == "partial"
    assert report.stop_reason == "provider_unavailable"
    assert report.completeness.collected == {"600030.SH"}
    assert report.completeness.failed == ["600519.SH", "601318.SH", "601398.SH"]
    assert report.completeness.stopped == ["688981.SH"]
    assert "cash_flow:SH688981" not in backend.calls


async def test_worker_routes_akshare_and_notifies_a_stop_once(db_session, tmp_path):
    del db_session
    await seed_universe(["600519.SH", "601318.SH", "601398.SH", "600030.SH", "000001.SZ"])
    settings = settings_for(tmp_path)
    error = EastmoneyTransientError("HTTP 502", provider="akshare")
    failing = fake_client(FakeEastmoneyBackend(errors=[error] * 3), max_attempts=1)
    async with session_scope() as session:
        summary, _ = await enqueue_snapshot_job(session, _summary(), triggered_by="test")
        cash, _ = await enqueue_snapshot_job(session, _cash_flow(), triggered_by="test")
        ids = (summary.id, cash.id)
    proxy = FakeProxy()
    worker = ResearchWorker(
        settings=settings, worker_id="w", handlers=handlers_for(settings, proxy)
    )
    assert await worker.run_once() == ids[0]
    worker_failing = ResearchWorker(
        settings=settings, worker_id="w2", handlers=handlers_for(settings, proxy, eastmoney=failing)
    )
    assert await worker_failing.run_once() == ids[1]
    assert proxy.calls == []
    async with session_scope() as session:
        runs = [await PipelineRunRepository(session).get(run_id) for run_id in ids]
    assert runs[0].status == "success" and runs[0].metadata_json["provider"] == "akshare"
    assert runs[0].request_json["part"] == "summary"
    assert runs[1].status == "partial"
    assert runs[1].metadata_json["stop_reason"] == "provider_unavailable"
    notes = list((tmp_path / "notifications").rglob("*.md"))
    assert len(notes) == 1
    body = notes[0].read_text(encoding="utf-8")
    assert "action_required" in body and "数据源不可用" in body


# --------------------------------------------------------------------------- #
# Payloads, keys, backfill planning, CLI
# --------------------------------------------------------------------------- #


def test_payload_rules_and_keys():
    summary = _summary()
    assert idempotency_key(summary).endswith(":summary")
    assert request_json(summary)["part"] == "summary"
    baostock = FinancialSnapshotPayload(
        universe_code="CSI300", report_period=FY2024, as_of=AS_OF
    )
    assert "part" not in request_json(baostock)  # unchanged for other providers
    assert idempotency_key(baostock) == "financial_snapshot:baostock:CSI300:2024-12-31:2026-10-07"
    cash = _cash_flow(backfill=True)
    key = idempotency_key(cash)
    assert ":cash_flow:+2p" in key and key.endswith(":backfill")
    fewer = cash.model_copy(update={"extra_periods": (FY2025,)})
    assert key != idempotency_key(fewer)
    with pytest.raises(ValueError, match="AkShare options"):
        FinancialSnapshotPayload(
            universe_code="CSI300", report_period=FY2024, as_of=AS_OF, part="summary"
        )
    with pytest.raises(ValueError, match="extra_periods"):
        _summary(extra_periods=(FY2025,))
    from investment_agent.snapshots.jobs import MarketSnapshotPayload

    with pytest.raises(ValueError, match="financials only"):
        MarketSnapshotPayload(provider="akshare", universe_code="CSI300", trade_date=AS_OF)


async def test_backfill_plan_is_idempotent_and_retries_failures(db_session):
    payloads = akshare_backfill_payloads(
        "CSI300", AS_OF, annual_years=6, quarters=4, with_cash_flow=True
    )
    summaries = [p for p in payloads if p.part == "summary"]
    assert len(summaries) == 9 and payloads[-1].part == "cash_flow"
    assert all(p.backfill for p in payloads)
    first = await plan_akshare_backfill(db_session, payloads, triggered_by="test")
    assert all(action.created for action in first)
    again = await plan_akshare_backfill(db_session, payloads, triggered_by="test")
    assert [a.run_id for a in again] == [a.run_id for a in first]
    run = await db_session.get(PipelineRun, first[0].run_id)
    run.status = "failed"
    await db_session.flush()
    retried = await plan_akshare_backfill(db_session, payloads[:1], triggered_by="test")
    assert retried[0].created and retried[0].key.endswith(":r2")


@pytest.fixture
def cli_db(tmp_path, monkeypatch):
    db_path = tmp_path / "cli.db"
    url = f"sqlite+aiosqlite:///{db_path}"
    monkeypatch.setenv("API_KEY", "secret")
    monkeypatch.setenv("LLM_TIER3_API_KEY", "x")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", url)

    async def create() -> None:
        engine = init_engine(url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await engine.dispose()

    asyncio.run(create())
    return url


def _keys(url: str) -> list[str]:
    async def load() -> list[str]:
        engine = init_engine(url)
        async with session_scope() as session:
            keys = list(
                (
                    await session.execute(
                        select(PipelineRun.idempotency_key).order_by(PipelineRun.id)
                    )
                ).scalars()
            )
        await engine.dispose()
        return [str(key) for key in keys]

    return asyncio.run(load())


def test_cli_collect_financial_and_backfill_with_akshare(cli_db):
    from investment_agent.cli.main import app

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["snapshot", "collect-financial", "--provider", "akshare", "--part", "cash_flow",
         "--period", "2026-06-30", "--as-of", "2026-10-06"],
    )
    assert result.exit_code == 0, result.output
    result = runner.invoke(
        app,
        ["snapshot", "collect-financial", "--provider", "baostock", "--part", "summary",
         "--period", "2026-06-30"],
    )
    assert result.exit_code != 0 and "--part" in result.output
    result = runner.invoke(
        app, ["snapshot", "collect-market", "--provider", "akshare", "--date", "2026-09-30"]
    )
    assert result.exit_code != 0
    result = runner.invoke(
        app,
        ["snapshot", "backfill", "--provider", "akshare", "--as-of", "2026-10-06",
         "--annual", "1", "--quarters", "1"],
    )
    assert result.exit_code == 0, result.output
    assert "2 period(s)" in result.output
    result = runner.invoke(
        app, ["snapshot", "backfill", "--with-cash-flow", "--as-of", "2026-10-06"]
    )
    assert result.exit_code != 0
    keys = _keys(cli_db)
    assert keys == [
        "financial_snapshot:akshare:CSI300:2026-06-30:2026-10-06:cash_flow",
        "financial_snapshot:akshare:CSI300:2026-06-30:2026-10-06:summary:backfill",
        "financial_snapshot:akshare:CSI300:2025-12-31:2026-10-06:summary:backfill",
    ]
    status = runner.invoke(
        app,
        ["snapshot", "backfill", "--provider", "akshare", "--as-of", "2026-10-06",
         "--annual", "1", "--quarters", "1", "--status"],
    )
    assert status.exit_code == 0 and "queued" in status.output


async def test_only_live_conflicts_on_exact_values_ask_ifind(db_session, tmp_path):
    """Backfills never queue iFinD verification; neither do differences against
    baostock's approximate (ratio-derived) operating cash flow."""

    from investment_agent.storage.models.verification import VerificationRequest

    del db_session
    ids = await seed_universe(POOL)
    await _seed_baostock(ids["601398.SH"], FY2024, date(2025, 3, 29), debt_ratio=0.95)
    # Ratio-derived operating cash flow far from Eastmoney's figure.
    await _seed_baostock(
        ids["600519.SH"], FY2024, date(2025, 4, 3),
        debt_ratio=0.19, cfo_to_np=2.5, net_profit=86_228_146_421.62,
    )

    async def requests() -> list[VerificationRequest]:
        async with session_scope() as session:
            return list((await session.execute(select(VerificationRequest))).scalars())

    backfill = await _collect(_summary(backfill=True), tmp_path)
    assert backfill.status == "success"
    assert backfill.to_metadata()["provider_conflict"]["count"] == 2
    assert await requests() == []

    live = await _collect(_summary(), tmp_path)
    assert live.to_metadata()["provider_conflict"]["count"] == 2
    queued = await requests()
    assert [(r.ticker, r.trigger, r.fields) for r in queued] == [
        ("601398.SH", "conflict", ["debt_ratio"])
    ]
