"""Cross-run provider circuit breaker (mrd-intake A1): backoff schedule, negative
cache (no upstream call while open), reset on success, terminal errors ignored,
client wiring (AkShare / baostock) and the collectors' ``partial`` path."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from freezegun import freeze_time

from investment_agent.providers.akshare_eastmoney import (
    AkshareEastmoneyClient,
    EastmoneyError,
    EastmoneyTransientError,
)
from investment_agent.providers.baostock_market import BaostockMarketClient
from investment_agent.providers.baostock_reference import BaostockTransientError
from investment_agent.providers.circuit import (
    ANY_ENDPOINT,
    DEFAULT_SCHEDULE,
    CircuitBreaker,
    CircuitEvent,
)
from investment_agent.providers.errors import (
    ProviderDataError,
    ProviderError,
    ProviderTransientError,
    ProviderUnavailableError,
)
from investment_agent.research.retry_policy import is_transient_error
from investment_agent.snapshots.breadth import collect_baostock_market
from investment_agent.snapshots.collect import ArtifactWriter
from investment_agent.snapshots.eastmoney import collect_akshare_financial
from investment_agent.snapshots.jobs import FinancialSnapshotPayload, MarketSnapshotPayload
from tests.unit._baostock_fake import FakeBaostockData
from tests.unit._eastmoney_fake import CAPTURED_AT, FakeEastmoneyBackend, NoSleep
from tests.unit._snapshot_support import seed_universe

T0 = "2026-10-09 10:00:00"
POOL = ["600030.SH", "600519.SH", "601318.SH", "601398.SH", "000001.SZ"]


def _now() -> datetime:
    return datetime.now(UTC)


def _transient() -> ProviderTransientError:
    return ProviderTransientError("HTTP 502", provider="akshare")


def _akshare_client(
    backend: FakeEastmoneyBackend, circuit: CircuitBreaker, *, max_attempts: int = 1
) -> AkshareEastmoneyClient:
    return AkshareEastmoneyClient(
        backend,
        clock=lambda: CAPTURED_AT,
        min_interval_seconds=0.0,
        sleep=NoSleep(),
        max_attempts=max_attempts,
        retry_backoff_seconds=0.0,
        circuit=circuit,
    )


# --------------------------------------------------------------------------- #
# Pure breaker
# --------------------------------------------------------------------------- #


def test_backoff_schedule_negative_cache_and_reset_on_success():
    events: list[CircuitEvent] = []
    with freeze_time(T0) as frozen:
        breaker = CircuitBreaker(_now, on_change=events.append)
        start = _now()
        breaker.check("akshare", "stock_lrb_em")  # closed: nothing happens
        # a transient failure on a closed circuit is the job breaker's business
        assert breaker.record_failure("akshare", "stock_lrb_em", _transient()) is False
        assert breaker.snapshot() == {}
        for count, delay in enumerate((5, 15, 45, 120, 300, 300), start=1):
            if count == 1:
                breaker.open_now("akshare", _transient(), endpoint="stock_lrb_em")  # job breaker
            else:  # the half-open probe of the next job failed again: escalate
                assert breaker.record_failure("akshare", "stock_lrb_em", _transient()) is True
            status = breaker.status("akshare", "stock_lrb_em")
            assert status.fail_count == count
            assert status.open_until == _now() + timedelta(seconds=delay)
            assert status.last_error == "ProviderTransientError: HTTP 502"
            with pytest.raises(ProviderUnavailableError) as caught:
                breaker.check("akshare", "stock_lrb_em")
            assert caught.value.open_until == status.open_until
            assert caught.value.fail_count == count
            assert caught.value.provider == "akshare" and caught.value.endpoint == "stock_lrb_em"
            assert is_transient_error(caught.value)
            # other endpoints / providers are untouched
            breaker.check("akshare", "stock_xjll_em")
            breaker.check("baostock", "stock_lrb_em")
            frozen.tick(timedelta(seconds=delay))
            breaker.check("akshare", "stock_lrb_em")  # window passed: half-open
        assert DEFAULT_SCHEDULE == (5, 15, 45, 120, 300)
        assert _now() - start == timedelta(seconds=5 + 15 + 45 + 120 + 300 + 300)
        breaker.record_success("akshare", "stock_lrb_em")
        assert breaker.status("akshare", "stock_lrb_em").fail_count == 0
        assert breaker.status("akshare", "stock_lrb_em").open_until is None
        assert breaker.snapshot() == {}
        # after the reset the next trip starts the schedule again
        breaker.open_now("akshare", _transient(), endpoint="stock_lrb_em")
        assert breaker.status("akshare", "stock_lrb_em").open_until == _now() + timedelta(seconds=5)
    assert [event.transition for event in events] == ["opened"] * 6 + ["closed", "opened"]
    assert events[0].status.key == "akshare:stock_lrb_em"
    # a success on a closed circuit is not a change
    events.clear()
    breaker.record_success("baostock", "login")
    assert events == []


def test_terminal_errors_never_open_or_escalate_the_circuit():
    with freeze_time(T0) as frozen:
        breaker = CircuitBreaker(_now)
        terminal = (
            ProviderDataError("columns changed", provider="akshare"),
            ProviderError("HTTP 404", provider="akshare"),
            EastmoneyError("HTTP 403", provider="akshare"),
            ValueError("bad ticker"),
        )
        for exc in terminal:
            assert breaker.record_failure("akshare", "stock_lrb_em", exc) is False
        assert breaker.snapshot() == {}
        breaker.check("akshare", "stock_lrb_em")
        # tripped by a job breaker: terminal errors still do not escalate
        breaker.open_now("akshare", _transient())
        frozen.tick(timedelta(seconds=5))
        for exc in terminal:
            assert breaker.record_failure("akshare", "stock_lrb_em", exc) is False
        assert breaker.status("akshare", ANY_ENDPOINT).fail_count == 1


def test_provider_wide_open_blocks_every_endpoint_until_a_success():
    with freeze_time(T0) as frozen:
        breaker = CircuitBreaker(_now)
        breaker.open_now("akshare", ProviderDataError("schema drift x3", provider="akshare"))
        wildcard = breaker.status("akshare", ANY_ENDPOINT)
        assert wildcard.fail_count == 1 and wildcard.open_until == _now() + timedelta(seconds=5)
        for endpoint in ("stock_lrb_em", "stock_cash_flow_sheet_by_report_em"):
            with pytest.raises(ProviderUnavailableError) as caught:
                breaker.check("akshare", endpoint)
            assert caught.value.endpoint == ANY_ENDPOINT
            assert "whole provider" in str(caught.value)
        breaker.check("baostock", "login")
        frozen.tick(timedelta(seconds=5))
        breaker.check("akshare", "stock_lrb_em")
        breaker.record_success("akshare", "stock_lrb_em")
        assert breaker.snapshot() == {}


def test_hook_failures_are_logged_not_raised():
    def boom(event: CircuitEvent) -> None:
        raise RuntimeError("ledger down")

    with freeze_time(T0):
        breaker = CircuitBreaker(_now, on_change=boom)
        breaker.open_now("baostock", _transient(), endpoint="login")
        assert breaker.status("baostock", "login").fail_count == 1


def test_clock_and_schedule_validation():
    with pytest.raises(ValueError):
        CircuitBreaker(_now, schedule=())
    with pytest.raises(ValueError):
        CircuitBreaker(lambda: datetime(2026, 10, 9)).check("akshare", "x")


# --------------------------------------------------------------------------- #
# Client wiring: no upstream call while open
# --------------------------------------------------------------------------- #


async def test_akshare_client_does_not_call_upstream_while_open():
    backend = FakeEastmoneyBackend(
        errors=[EastmoneyTransientError("HTTP 502", provider="akshare")]
    )
    with freeze_time(T0) as frozen:
        circuit = CircuitBreaker(_now)
        client = _akshare_client(backend, circuit)
        circuit.open_now("akshare", _transient())  # a job breaker tripped it
        with pytest.raises(ProviderUnavailableError):
            await client.fetch_cash_flow_statement("600519.SH")
        assert client.calls == 0 and backend.calls == []  # negative cache
        with pytest.raises(ProviderUnavailableError):
            await client.fetch_period_summaries(date(2024, 12, 31))
        assert client.calls == 0 and backend.calls == []

        frozen.tick(timedelta(seconds=5))  # half-open: the probe fails -> escalate to 15 s
        with pytest.raises(EastmoneyTransientError):
            await client.fetch_cash_flow_statement("600519.SH")
        assert client.calls == 1 and backend.calls == ["cash_flow:SH600519"]
        status = circuit.status("akshare", ANY_ENDPOINT)
        assert status.fail_count == 2 and status.open_until == _now() + timedelta(seconds=15)
        with pytest.raises(ProviderUnavailableError):
            await client.fetch_cash_flow_statement("600519.SH")
        assert client.calls == 1

        frozen.tick(timedelta(seconds=15))  # the probe succeeds -> closed
        statement = await client.fetch_cash_flow_statement("600519.SH")
        assert statement is not None and client.calls == 2
        assert circuit.snapshot() == {}


async def test_akshare_in_call_retries_are_counted_once():
    error = EastmoneyTransientError("HTTP 502", provider="akshare")
    backend = FakeEastmoneyBackend(errors=[error, error])
    with freeze_time(T0) as frozen:
        circuit = CircuitBreaker(_now)
        circuit.open_now("akshare", _transient())
        frozen.tick(timedelta(seconds=5))
        client = _akshare_client(backend, circuit, max_attempts=2)
        with pytest.raises(EastmoneyTransientError):
            await client.fetch_cash_flow_statement("600519.SH")
        assert client.calls == 2  # both in-call attempts ran: one logical failure
        assert circuit.status("akshare", ANY_ENDPOINT).fail_count == 2
        assert circuit.status("akshare", "stock_cash_flow_sheet_by_report_em").fail_count == 0


async def test_baostock_client_checks_and_records():
    backend = FakeBaostockData(fail=lambda method, params: OSError("connection reset"))
    with freeze_time(T0) as frozen:
        circuit = CircuitBreaker(_now)
        client = BaostockMarketClient(
            backend,  # type: ignore[arg-type]
            clock=_now,
            max_attempts=1,
            retry_backoff_seconds=0,
            circuit=circuit,
        )
        window = (date(2026, 9, 1), date(2026, 9, 30))
        # closed: a transient failure is reported to the job breaker, not the circuit
        with pytest.raises(BaostockTransientError):
            await client.fetch_daily_bars("600519.SH", *window, "none")
        assert len(backend.methods("query_history_k_data_plus")) == 1
        assert circuit.snapshot() == {}
        # tripped by a job breaker: even the login is refused
        circuit.open_now("baostock", OSError("connection reset"))
        with pytest.raises(ProviderUnavailableError):
            await client.fetch_daily_bars("600519.SH", *window, "none")
        assert backend.logins == 1 and len(backend.methods("query_history_k_data_plus")) == 1
        frozen.tick(timedelta(seconds=5))
        with pytest.raises(BaostockTransientError):  # probe fails -> 15 s
            await client.fetch_daily_bars("600519.SH", *window, "none")
        assert circuit.status("baostock", ANY_ENDPOINT).fail_count == 2
        frozen.tick(timedelta(seconds=15))
        backend.fail = None
        series = await client.fetch_daily_bars("600519.SH", *window, "none")
        assert series.bars and circuit.snapshot() == {}


# --------------------------------------------------------------------------- #
# Collectors: an open circuit ends the run ``partial`` without calling upstream
# --------------------------------------------------------------------------- #


def _cash_flow() -> FinancialSnapshotPayload:
    return FinancialSnapshotPayload(
        provider="akshare",
        part="cash_flow",
        report_period=date(2026, 6, 30),
        extra_periods=(date(2025, 12, 31), date(2025, 6, 30)),
        as_of=date(2026, 10, 7),
        universe_code="CSI300",
    )


def _summary() -> FinancialSnapshotPayload:
    return FinancialSnapshotPayload(
        provider="akshare",
        part="summary",
        report_period=date(2024, 12, 31),
        as_of=date(2026, 10, 7),
        universe_code="CSI300",
    )


async def _collect_akshare(payload, tmp_path, backend, circuit, **kwargs):
    return await collect_akshare_financial(
        payload,
        client=_akshare_client(backend, circuit, **kwargs),
        artifacts=ArtifactWriter(tmp_path, run_id=1, attempt=1),
        lease=None,
        now=lambda: CAPTURED_AT,
        circuit=circuit,
    )


async def test_failure_breaker_opens_the_circuit_and_the_next_run_is_partial_at_once(
    db_session, tmp_path
):
    del db_session
    await seed_universe(POOL)
    error = EastmoneyTransientError("HTTP 502", provider="akshare")
    with freeze_time(T0):
        circuit = CircuitBreaker(_now)
        # run 1: three consecutive failures trip the job breaker -> circuit opens
        backend = FakeEastmoneyBackend(errors=[error, error, error])
        report = await _collect_akshare(_cash_flow(), tmp_path, backend, circuit)
        assert report.status == "partial" and report.stop_reason == "provider_unavailable"
        assert len(backend.calls) == 3
        assert circuit.status("akshare", ANY_ENDPOINT).is_open(_now())
        # run 2 (same worker, seconds later): nothing is called, every ticker is ``stopped``
        backend = FakeEastmoneyBackend()
        report = await _collect_akshare(_cash_flow(), tmp_path, backend, circuit)
        assert report.status == "partial"
        assert report.stop_reason == "provider_unavailable"
        assert report.completeness.stopped == sorted(POOL)
        assert report.completeness.collected == set()
        assert backend.calls == []
        metadata = report.to_metadata()
        assert metadata["stop_reason"] == "provider_unavailable"
        assert "ProviderUnavailableError" in str(metadata["stop_error"])
        assert "unavailable until" in str(metadata["stop_error"])
        assert metadata["completeness"]["stopped_count"] == len(POOL)
        reason = report.completeness.missing[POOL[0]][1]
        assert reason.startswith("not collected: akshare unavailable after 1 consecutive")


async def test_summary_run_is_partial_when_the_circuit_is_open(db_session, tmp_path):
    del db_session
    await seed_universe(POOL)
    with freeze_time(T0):
        circuit = CircuitBreaker(_now)
        circuit.open_now("akshare", _transient(), endpoint="stock_lrb_em")
        backend = FakeEastmoneyBackend()
        report = await _collect_akshare(_summary(), tmp_path, backend, circuit)
        assert report.status == "partial" and report.stop_reason == "provider_unavailable"
        assert report.completeness.stopped == sorted(POOL)
        assert backend.calls == []
        assert report.metadata["akshare_calls"] == 0


async def test_baostock_market_run_is_partial_when_the_circuit_is_open(db_session, tmp_path):
    del db_session
    pool = ["000001.SZ", "600519.SH"]
    await seed_universe(pool)
    backend = FakeBaostockData()
    with freeze_time(T0):
        circuit = CircuitBreaker(_now)
        circuit.open_now("baostock", OSError("connection reset"))
        client = BaostockMarketClient(
            backend,  # type: ignore[arg-type]
            clock=lambda: CAPTURED_AT,
            max_attempts=1,
            retry_backoff_seconds=0,
            circuit=circuit,
        )
        report = await collect_baostock_market(
            MarketSnapshotPayload(trade_date=date(2026, 9, 30), universe_code="CSI300"),
            client=client,
            artifacts=ArtifactWriter(tmp_path, run_id=1, attempt=1),
            lease=None,
            now=lambda: CAPTURED_AT,
            circuit=circuit,
        )
    assert report.status == "partial" and report.stop_reason == "provider_unavailable"
    assert report.completeness.stopped == pool
    assert backend.calls == []  # the login itself was refused
    assert report.metadata["baostock_queries"] == 0
    assert report.metadata["aborted"] is None
