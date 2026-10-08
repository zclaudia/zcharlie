"""Generic provider call ledger (mrd-intake A2): repository, best-effort hooks,
client ``on_call`` wiring, run attribution, ``/health/data`` providers block,
and the ``ia admin prune-provider-calls`` retention command."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from investment_agent.providers.akshare_eastmoney import (
    AkshareEastmoneyClient,
    EastmoneyError,
    EastmoneyTransientError,
)
from investment_agent.providers.baostock_market import BaostockMarketClient
from investment_agent.providers.baostock_reference import BaostockTransientError
from investment_agent.providers.circuit import CircuitBreaker, CircuitStatus
from investment_agent.providers.errors import ProviderUnavailableError
from investment_agent.providers.ledger import (
    CURRENT_RUN_ID,
    ProviderCallEvent,
    circuit_ledger_hook,
    provider_ledger_hook,
)
from investment_agent.research.worker import ResearchWorker
from investment_agent.snapshots.health import build_data_health, derive_warnings
from investment_agent.snapshots.jobs import FinancialSnapshotPayload, enqueue_snapshot_job
from investment_agent.storage.database import init_engine, session_scope
from investment_agent.storage.models.base import Base
from investment_agent.storage.models.provider_call import ProviderCall
from investment_agent.storage.repositories.provider_call import ProviderCallRepository
from tests.unit._baostock_fake import FakeBaostockData
from tests.unit._eastmoney_fake import CAPTURED_AT, FakeEastmoneyBackend, NoSleep
from tests.unit._health_support import health_settings, seed_members
from tests.unit._snapshot_support import FakeProxy, handlers_for, seed_universe, settings_for

T0 = datetime(2026, 10, 9, 2, 0, tzinfo=UTC)  # 10:00 Asia/Shanghai
NOW = datetime(2026, 10, 15, 4, 0, tzinfo=UTC)
TODAY = date(2026, 10, 15)


async def _rows() -> list[ProviderCall]:
    async with session_scope() as session:
        return list((await session.execute(select(ProviderCall).order_by(ProviderCall.id))).scalars())


async def _record(**kwargs) -> None:
    kwargs.setdefault("at", T0)
    kwargs.setdefault("provider", "akshare")
    kwargs.setdefault("endpoint", "stock_lrb_em")
    kwargs.setdefault("outcome", "ok")
    async with session_scope() as session:
        await ProviderCallRepository(session).record(**kwargs)


# --------------------------------------------------------------------------- #
# Repository
# --------------------------------------------------------------------------- #


async def test_record_and_summary_aggregate_per_provider(db_session):
    del db_session
    await _record(at=T0 - timedelta(hours=30), outcome="transient")  # outside the window
    await _record(at=T0 - timedelta(hours=3), duration_ms=6400, ticker_count=0)
    await _record(at=T0 - timedelta(hours=2), endpoint="stock_cash_flow_sheet_by_report_em",
                  outcome="transient", duration_ms=180_000, ticker_count=1, run_id=7)
    await _record(at=T0 - timedelta(hours=1), endpoint="stock_cash_flow_sheet_by_report_em",
                  outcome="terminal", ticker_count=1, run_id=7)
    await _record(at=T0 - timedelta(minutes=30), endpoint="circuit", outcome="skipped_open",
                  duration_ms=15_000)
    await _record(at=T0 - timedelta(minutes=20), endpoint="stock_lrb_em", outcome="skipped_open")
    await _record(at=T0 - timedelta(minutes=10), provider="baostock",
                  endpoint="query_history_k_data_plus", duration_ms=210, ticker_count=1)
    async with session_scope() as session:
        stats = await ProviderCallRepository(session).summary(T0 - timedelta(hours=24))
    assert [stat.provider for stat in stats] == ["akshare", "baostock"]
    akshare, baostock = stats
    assert (akshare.calls, akshare.failed, akshare.skipped) == (3, 2, 2)
    assert akshare.last_error_at == T0 - timedelta(hours=1)
    assert akshare.last_error == "stock_cash_flow_sheet_by_report_em: terminal"
    assert akshare.last_open_at == T0 - timedelta(minutes=30)
    assert akshare.last_open_until == T0 - timedelta(minutes=30) + timedelta(seconds=15)
    assert (baostock.calls, baostock.failed, baostock.skipped) == (1, 0, 0)
    assert baostock.last_error is None and baostock.last_open_at is None
    rows = await _rows()
    assert rows[2].run_id == 7 and rows[2].duration_ms == 180_000 and rows[2].ticker_count == 1
    assert rows[0].at.replace(tzinfo=UTC) == T0 - timedelta(hours=30)


async def test_record_validates_outcome_and_counts(db_session):
    repo = ProviderCallRepository(db_session)
    with pytest.raises(ValueError, match="outcome"):
        await repo.record(at=T0, provider="akshare", endpoint="x", outcome="error")
    with pytest.raises(ValueError, match="ticker_count"):
        await repo.record(at=T0, provider="akshare", endpoint="x", outcome="ok", ticker_count=-1)
    with pytest.raises(ValueError, match="timezone"):
        await repo.record(at=T0.replace(tzinfo=None), provider="akshare", endpoint="x", outcome="ok")
    row = await repo.record(
        at=T0, provider="p" * 40, endpoint="e" * 80, outcome="ok", duration_ms=-5
    )
    assert len(row.provider) == 32 and len(row.endpoint) == 64 and row.duration_ms == 0


async def test_prune_keeps_recent_rows(db_session):
    del db_session
    for days in (100, 91, 89, 1):
        await _record(at=T0 - timedelta(days=days))
    async with session_scope() as session:
        removed = await ProviderCallRepository(session).prune(before=T0 - timedelta(days=90))
    assert removed == 2
    assert len(await _rows()) == 2


# --------------------------------------------------------------------------- #
# Hooks (best effort)
# --------------------------------------------------------------------------- #


async def test_provider_hook_writes_rows_and_reads_the_run_id_context(db_session):
    del db_session
    hook = provider_ledger_hook(source="snapshot", clock=lambda: T0)
    await hook(ProviderCallEvent("akshare", "stock_lrb_em", "ok", 6400, 0))
    token = CURRENT_RUN_ID.set(42)
    try:
        await hook(ProviderCallEvent("baostock", "login", "transient", 1000, 0))
    finally:
        CURRENT_RUN_ID.reset(token)
    explicit = provider_ledger_hook(source="cli", run_id=9, clock=lambda: T0)
    await explicit(ProviderCallEvent("akshare", "stock_xjll_em", "skipped_open", None, 0))
    rows = await _rows()
    assert [(r.provider, r.endpoint, r.outcome, r.duration_ms, r.run_id) for r in rows] == [
        ("akshare", "stock_lrb_em", "ok", 6400, None),
        ("baostock", "login", "transient", 1000, 42),
        ("akshare", "stock_xjll_em", "skipped_open", None, 9),
    ]


async def test_hooks_never_raise_when_the_ledger_is_unavailable(tmp_path):
    # No schema here: both hooks log and return; the client call is unaffected.
    engine = init_engine(f"sqlite+aiosqlite:///{tmp_path / 'empty.db'}")
    try:
        await provider_ledger_hook(source="snapshot")(
            ProviderCallEvent("akshare", "stock_lrb_em", "ok", 1, 0)
        )
        circuit_hook = circuit_ledger_hook()
        breaker = CircuitBreaker(lambda: T0, on_change=circuit_hook)
        breaker.open_now("akshare", EastmoneyTransientError("boom", provider="akshare"))
        await circuit_hook.drain()
    finally:
        await engine.dispose()


async def test_circuit_hook_records_every_open_with_its_window(db_session):
    del db_session
    circuit_hook = circuit_ledger_hook(clock=lambda: T0)
    breaker = CircuitBreaker(lambda: T0, on_change=circuit_hook)
    error = EastmoneyTransientError("HTTP 502", provider="akshare")
    breaker.open_now("akshare", error)  # 5 s
    breaker.record_failure("akshare", "stock_lrb_em", error)  # escalates the wildcard: 15 s
    breaker.record_success("akshare", "stock_lrb_em")  # closed: not a ledger row
    await circuit_hook.drain()
    rows = await _rows()
    assert [(r.provider, r.endpoint, r.outcome, r.duration_ms, r.ticker_count) for r in rows] == [
        ("akshare", "circuit", "skipped_open", 5000, 0),
        ("akshare", "circuit", "skipped_open", 15000, 0),
    ]


def test_circuit_hook_without_a_running_loop_only_logs():
    circuit_hook = circuit_ledger_hook(clock=lambda: T0)
    breaker = CircuitBreaker(lambda: T0, on_change=circuit_hook)
    breaker.open_now("baostock", BaostockTransientError("boom", provider="baostock"))
    assert breaker.status("baostock", "*").fail_count == 1


# --------------------------------------------------------------------------- #
# Client wiring
# --------------------------------------------------------------------------- #


class _Capture:
    def __init__(self) -> None:
        self.events: list[ProviderCallEvent] = []

    async def __call__(self, event: ProviderCallEvent) -> None:
        self.events.append(event)


async def test_akshare_client_reports_one_event_per_logical_call():
    capture = _Capture()
    circuit = CircuitBreaker(lambda: T0)
    transient = EastmoneyTransientError("HTTP 502", provider="akshare")
    backend = FakeEastmoneyBackend(
        errors=[None, None, None, transient, transient, EastmoneyError("HTTP 404", provider="akshare")]
    )
    client = AkshareEastmoneyClient(
        backend,
        clock=lambda: CAPTURED_AT,
        min_interval_seconds=0.0,
        sleep=NoSleep(),
        max_attempts=2,
        retry_backoff_seconds=0.0,
        circuit=circuit,
        on_call=capture,
    )
    await client.fetch_period_summaries(date(2024, 12, 31))
    with pytest.raises(EastmoneyTransientError):  # two attempts, one event
        await client.fetch_cash_flow_statement("600519.SH")
    with pytest.raises(EastmoneyError):
        await client.fetch_cash_flow_statement("600519.SH")
    circuit.open_now("akshare", transient)
    with pytest.raises(ProviderUnavailableError):
        await client.fetch_cash_flow_statement("600519.SH")
    assert [(e.provider, e.endpoint, e.outcome, e.ticker_count) for e in capture.events] == [
        ("akshare", "stock_lrb_em", "ok", 0),
        ("akshare", "stock_xjll_em", "ok", 0),
        ("akshare", "stock_zcfz_em", "ok", 0),
        ("akshare", "stock_cash_flow_sheet_by_report_em", "transient", 1),
        ("akshare", "stock_cash_flow_sheet_by_report_em", "terminal", 1),
        ("akshare", "stock_cash_flow_sheet_by_report_em", "skipped_open", 1),
    ]
    assert all(e.duration_ms is not None and e.duration_ms >= 0 for e in capture.events[:5])
    assert capture.events[5].duration_ms is None
    assert client.calls == 6  # the skipped call never reached the backend


async def test_baostock_client_reports_login_and_queries():
    capture = _Capture()
    backend = FakeBaostockData()
    client = BaostockMarketClient(
        backend,  # type: ignore[arg-type]
        clock=lambda: CAPTURED_AT,
        max_attempts=1,
        retry_backoff_seconds=0,
        on_call=capture,
    )
    async with client.session():
        await client.fetch_daily_bars("600519.SH", date(2026, 9, 1), date(2026, 9, 30), "none")
        backend.fail = lambda method, params: OSError("connection reset")
        with pytest.raises(BaostockTransientError):
            await client.fetch_daily_bars("600519.SH", date(2026, 9, 1), date(2026, 9, 30), "none")
    assert [(e.provider, e.endpoint, e.outcome, e.ticker_count) for e in capture.events] == [
        ("baostock", "login", "ok", 0),
        ("baostock", "query_history_k_data_plus", "ok", 1),
        ("baostock", "query_history_k_data_plus", "transient", 1),
    ]


async def test_worker_jobs_attribute_ledger_rows_to_their_run(db_session, tmp_path):
    del db_session
    await seed_universe(["600519.SH", "601318.SH"])
    settings = settings_for(tmp_path)
    client = AkshareEastmoneyClient(
        FakeEastmoneyBackend(),
        clock=lambda: CAPTURED_AT,
        min_interval_seconds=0.0,
        sleep=NoSleep(),
        max_attempts=1,
        retry_backoff_seconds=0.0,
        on_call=provider_ledger_hook(source="snapshot", clock=lambda: T0),
    )
    payload = FinancialSnapshotPayload(
        provider="akshare", part="summary", report_period=date(2024, 12, 31),
        as_of=date(2026, 10, 7), universe_code="CSI300",
    )
    async with session_scope() as session:
        run, _ = await enqueue_snapshot_job(session, payload, triggered_by="test")
        run_id = run.id
    worker = ResearchWorker(
        settings=settings, worker_id="w", handlers=handlers_for(settings, FakeProxy(), eastmoney=client)
    )
    assert await worker.run_once() == run_id
    rows = await _rows()
    assert [(r.endpoint, r.outcome, r.run_id) for r in rows] == [
        ("stock_lrb_em", "ok", run_id),
        ("stock_xjll_em", "ok", run_id),
        ("stock_zcfz_em", "ok", run_id),
    ]
    assert CURRENT_RUN_ID.get() is None


# --------------------------------------------------------------------------- #
# /health/data providers block
# --------------------------------------------------------------------------- #


async def test_health_providers_from_ledger_and_breaker_state(db_session):
    await seed_members(db_session, 2)
    repo = ProviderCallRepository(db_session)
    await repo.record(at=NOW - timedelta(hours=2), provider="akshare", endpoint="stock_lrb_em",
                      outcome="ok", duration_ms=7000)
    await repo.record(at=NOW - timedelta(hours=1), provider="akshare",
                      endpoint="stock_cash_flow_sheet_by_report_em", outcome="transient")
    await repo.record(at=NOW - timedelta(minutes=2), provider="akshare", endpoint="circuit",
                      outcome="skipped_open", duration_ms=300_000)
    await repo.record(at=NOW - timedelta(minutes=1), provider="akshare", endpoint="stock_lrb_em",
                      outcome="skipped_open")
    await repo.record(at=NOW - timedelta(hours=20), provider="baostock", endpoint="circuit",
                      outcome="skipped_open", duration_ms=5000)
    await repo.record(at=NOW - timedelta(hours=5), provider="baostock",
                      endpoint="query_history_k_data_plus", outcome="ok", ticker_count=1)

    # API process: no breaker in memory -> the ledger's circuit rows decide
    health = await build_data_health(db_session, health_settings(), now=NOW, today=TODAY)
    by_name = {item.provider: item for item in health.providers}
    assert set(by_name) == {"akshare", "baostock"}
    akshare = by_name["akshare"]
    assert (akshare.calls_24h, akshare.failed_24h, akshare.skipped_24h) == (2, 1, 2)
    assert akshare.open_until == NOW + timedelta(minutes=3)
    assert akshare.last_open_at == NOW - timedelta(minutes=2)
    assert akshare.last_error == "stock_cash_flow_sheet_by_report_em: transient"
    baostock = by_name["baostock"]
    assert baostock.open_until is None  # that window expired long ago
    assert baostock.last_open_at == NOW - timedelta(hours=20)
    codes = {w.code: w for w in health.warnings}
    assert "provider_circuit_open" in codes
    assert codes["provider_circuit_open"].section == "providers"
    assert "akshare 熔断中" in codes["provider_circuit_open"].message
    assert health.model_dump(mode="json")["providers"][0]["provider"] == "akshare"

    # Worker-side: the breaker snapshot wins over the ledger
    states = (
        CircuitStatus("baostock", "*", fail_count=2, open_until=NOW + timedelta(seconds=15),
                      last_error="BaostockTransientError: socket", last_failure_at=NOW),
        CircuitStatus("tencent", "quote", fail_count=1, open_until=NOW - timedelta(seconds=1),
                      last_error="x", last_failure_at=NOW - timedelta(seconds=6)),
    )
    health = await build_data_health(
        db_session, replace(health_settings(), circuit_states=states), now=NOW, today=TODAY
    )
    by_name = {item.provider: item for item in health.providers}
    assert set(by_name) == {"akshare", "baostock", "tencent"}
    assert by_name["baostock"].open_until == NOW + timedelta(seconds=15)
    assert by_name["baostock"].last_error == "*: BaostockTransientError: socket"
    assert by_name["tencent"].open_until is None and by_name["tencent"].calls_24h == 0
    assert [w.message.split(" ")[0] for w in health.warnings if w.code == "provider_circuit_open"] == [
        "akshare", "baostock",
    ]


async def test_health_without_ledger_rows_has_no_providers(db_session):
    await seed_members(db_session, 1)
    health = await build_data_health(db_session, health_settings(), now=NOW, today=TODAY)
    assert health.providers == []
    assert "provider_circuit_open" not in {w.code for w in health.warnings}


def test_derive_warnings_accepts_providers_keyword():
    # Older call sites pass no providers: the rule is simply inactive.
    from investment_agent.snapshots.health import ProviderHealth

    item = ProviderHealth(
        provider="akshare", calls_24h=1, failed_24h=1, skipped_24h=0,
        open_until=NOW + timedelta(seconds=30), last_error_at=NOW, last_error="x: transient",
        last_open_at=NOW,
    )
    assert derive_warnings.__kwdefaults__["providers"] == ()
    assert item.open_until is not None


# --------------------------------------------------------------------------- #
# CLI retention
# --------------------------------------------------------------------------- #


def test_admin_prune_provider_calls(tmp_path, monkeypatch):
    from investment_agent.cli.main import app

    url = f"sqlite+aiosqlite:///{tmp_path / 'cli.db'}"
    monkeypatch.setenv("API_KEY", "secret")
    monkeypatch.setenv("LLM_TIER3_API_KEY", "x")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", url)
    now = datetime.now(UTC)

    async def seed() -> None:
        engine = init_engine(url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with session_scope() as session:
            repo = ProviderCallRepository(session)
            for days in (120, 95, 10):
                await repo.record(at=now - timedelta(days=days), provider="akshare",
                                  endpoint="stock_lrb_em", outcome="ok")
        await engine.dispose()

    async def count() -> int:
        engine = init_engine(url)
        async with session_scope() as session:
            rows = list((await session.execute(select(ProviderCall))).scalars())
        await engine.dispose()
        return len(rows)

    asyncio.run(seed())
    runner = CliRunner()
    assert "days" in runner.invoke(app, ["admin", "prune-provider-calls", "--help"]).output
    result = runner.invoke(app, ["admin", "prune-provider-calls"])
    assert result.exit_code == 0, result.output
    assert "Removed 2 provider_call rows" in result.output
    assert asyncio.run(count()) == 1
    result = runner.invoke(app, ["admin", "prune-provider-calls", "--days", "5"])
    assert result.exit_code == 0 and "Removed 1" in result.output
    assert asyncio.run(count()) == 0
