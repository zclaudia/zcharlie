"""``close_quote`` job (mrd-intake B2): payload / idempotency key, trading-day gate,
scope union, writes and version append, baostock-first skipping, pending,
same-day conflicts in the factor view, circuit / breaker ``partial`` paths,
the worker handler with the respx-backed Tencent client, health freshness."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import respx
from freezegun import freeze_time
from pydantic import ValidationError
from sqlalchemy import select

from investment_agent.clock import SHANGHAI_TZ
from investment_agent.factors.catalog import DEFAULT_REGISTRY
from investment_agent.factors.snapshot_model import OBSERVATION, TARGET_POOL
from investment_agent.factors.view_db import load_snapshot_view
from investment_agent.providers.circuit import ANY_ENDPOINT, CircuitBreaker
from investment_agent.providers.errors import (
    ProviderDataError,
    ProviderTransientError,
    ProviderUnavailableError,
)
from investment_agent.providers.tencent_quote import (
    BASE_URL,
    ClosedQuote,
    QuoteBatch,
    TencentQuoteClient,
)
from investment_agent.research.worker import ResearchWorker
from investment_agent.scheduler.snapshot_jobs import snapshot_payloads
from investment_agent.scheduler.trading_calendar import load_trading_calendar
from investment_agent.snapshots.close_quote import (
    MAX_CONSECUTIVE_TRANSIENT,
    close_quote_scope,
    collect_close_quotes,
)
from investment_agent.snapshots.collect import ArtifactWriter, SnapshotPreconditionError
from investment_agent.snapshots.health import market_freshness
from investment_agent.snapshots.jobs import (
    CLOSE_QUOTE,
    CloseQuotePayload,
    FinancialSnapshotPayload,
    MarketSnapshotPayload,
    enqueue_snapshot_job,
    idempotency_key,
    payload_kind,
)
from investment_agent.snapshots.notifications import notification_level
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.candidates import CandidateRecord
from investment_agent.storage.models.pit_snapshot import MarketSnapshot
from investment_agent.storage.models.user_domain import HoldingItem
from investment_agent.storage.repositories.listing import ListingRepository
from investment_agent.storage.repositories.market_snapshot import (
    MARKET_PROVIDER_PRECEDENCE,
    MarketSnapshotRepository,
)
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository
from tests.unit._snapshot_support import (
    handlers_for,
    seed_universe,
    seed_watchlist,
    settings_for,
)

ROOT = Path(__file__).resolve().parents[2]
CALENDAR = load_trading_calendar(ROOT / "config" / "calendars" / "a_share.yaml")
FIXTURES = ROOT / "tests" / "fixtures" / "tencent"
SAMPLE = (FIXTURES / "quotes_sample.txt").read_text(encoding="utf-8")

TRADE_DATE = date(2026, 10, 8)
CAPTURED_AT = datetime(2026, 10, 8, 7, 10, tzinfo=UTC)  # 15:10 Asia/Shanghai
EVENING = datetime(2026, 10, 8, 10, 30, tzinfo=UTC)  # 18:30 Asia/Shanghai
POOL = ["000001.SZ", "000002.SZ", "600519.SH", "920748.BJ"]


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


def _quote(ticker: str, close: float, *, suspended: bool = False) -> ClosedQuote:
    value = Decimal(str(close))
    return ClosedQuote(
        ticker=ticker,
        trade_date=TRADE_DATE,
        name=ticker,
        open=value,
        high=value,
        low=value,
        close=value,
        prev_close=value,
        volume_shares=Decimal(0) if suspended else Decimal(2516800),
        amount_cny=Decimal(0) if suspended else Decimal(3145410000),
        turnover=Decimal("0.002"),
        pe=Decimal("19.28"),
        time=datetime(2026, 10, 8, 15, 0, 4, tzinfo=SHANGHAI_TZ),
        trade_status=0 if suspended else 1,
    )


@dataclass
class FakeQuoteClient:
    """Answers from ``closes`` (ticker -> close); ``pending`` / ``missing`` by name.

    ``errors`` are raised, one per call, before answering (``None`` = answer).
    """

    closes: dict[str, float] = field(default_factory=dict)
    pending: set[str] = field(default_factory=set)
    suspended: set[str] = field(default_factory=set)
    errors: list[Exception | None] = field(default_factory=list)
    chunk_size: int = 60
    calls: list[list[str]] = field(default_factory=list)
    captured_at: datetime = CAPTURED_AT

    async def fetch_close(self, tickers, trade_date):
        chunk = list(tickers)
        self.calls.append(chunk)
        if self.errors:
            error = self.errors.pop(0)
            if error is not None:
                raise error
        batch = QuoteBatch(trade_date=trade_date, captured_at=self.captured_at, requests=1)
        for ticker in chunk:
            if ticker in self.pending:
                batch.pending[ticker] = "session not closed: latest quote at 14:35:00"
            elif ticker in self.closes:
                batch.quotes[ticker] = _quote(
                    ticker, self.closes[ticker], suspended=ticker in self.suspended
                )
            else:
                batch.missing[ticker] = "not in the tencent answer (unknown or delisted code)"
        return QuoteBatch(
            trade_date=trade_date,
            captured_at=self.captured_at,
            quotes=batch.quotes,
            pending=batch.pending,
            missing=batch.missing,
            requests=1,
            raw=("v_fake=\"\";",),
        )


def _payload(**kwargs) -> CloseQuotePayload:
    kwargs.setdefault("universe_code", "CSI300")
    kwargs.setdefault("trade_date", TRADE_DATE)
    return CloseQuotePayload(**kwargs)


async def _collect(client, tmp_path, payload=None, *, circuit=None, run_id: int = 1):
    return await collect_close_quotes(
        payload or _payload(),
        client=client,
        artifacts=ArtifactWriter(tmp_path, run_id=run_id, attempt=1),
        lease=None,
        now=lambda: CAPTURED_AT,
        circuit=circuit,
    )


async def _rows(provider: str | None = None) -> list[MarketSnapshot]:
    async with session_scope() as session:
        stmt = select(MarketSnapshot).order_by(MarketSnapshot.listing_id, MarketSnapshot.version)
        if provider is not None:
            stmt = stmt.where(MarketSnapshot.provider == provider)
        return list((await session.execute(stmt)).scalars().all())


async def _seed_bar(
    listing_id: int,
    day: date,
    *,
    provider: str,
    close: float = 1.0,
    captured_at: datetime = EVENING,
    adjust: str = "none",
) -> MarketSnapshot:
    async with session_scope() as session:
        value = Decimal(str(close))
        return await MarketSnapshotRepository(session).insert(
            listing_id=listing_id, trade_date=day, open=value, high=value, low=value,
            close=value, volume=Decimal(1), adjust=adjust, provider=provider,
            captured_at=captured_at,
        )


# --------------------------------------------------------------------------- #
# Payload and schedule
# --------------------------------------------------------------------------- #


def test_payload_defaults_keys_and_validation():
    payload = _payload()
    assert payload.provider == "tencent" and payload.scope == "universe"
    assert payload.slot == "close" and payload_kind(payload) == CLOSE_QUOTE
    assert idempotency_key(payload) == "close_quote:tencent:CSI300:2026-10-08"
    retry = _payload(slot="retry")
    assert idempotency_key(retry) == "close_quote:tencent:CSI300:2026-10-08:retry"
    watchlist = CloseQuotePayload(scope="watchlist", trade_date=TRADE_DATE)
    assert idempotency_key(watchlist) == "close_quote:tencent:watchlist:2026-10-08"
    assert CloseQuotePayload.model_validate(payload.model_dump(mode="json")) == payload
    with pytest.raises(ValidationError, match="tencent only"):
        _payload(provider="baostock")
    with pytest.raises(ValidationError):
        _payload(slot="later")
    with pytest.raises(ValidationError, match="close_quote job only"):
        MarketSnapshotPayload(provider="tencent", universe_code="CSI300", trade_date=TRADE_DATE)
    with pytest.raises(ValidationError, match="close_quote job only"):
        FinancialSnapshotPayload(
            provider="tencent", universe_code="CSI300",
            report_period=date(2026, 6, 30), as_of=TRADE_DATE,
        )


def test_schedule_gates_on_trading_days_for_both_slots():
    trading = date(2026, 9, 30)
    holiday = date(2026, 10, 1)  # National Day
    close = snapshot_payloads("close_quote", today=trading, calendar=CALENDAR, universes=["CSI300"])
    assert close == [_payload(trade_date=trading)]
    retry = snapshot_payloads(
        "close_quote_retry", today=trading, calendar=CALENDAR, universes=["CSI300", "SSE50"]
    )
    assert [p.slot for p in retry] == ["retry", "retry"]  # type: ignore[union-attr]
    assert [p.universe_code for p in retry] == ["CSI300", "SSE50"]
    assert idempotency_key(close[0]) != idempotency_key(retry[0])
    assert snapshot_payloads("close_quote", today=holiday, calendar=CALENDAR, universes=["CSI300"]) == []
    assert (
        snapshot_payloads(
            "close_quote_retry", today=holiday, calendar=CALENDAR, universes=["CSI300"]
        )
        == []
    )
    with pytest.raises(ValueError, match="needs the trading calendar"):
        snapshot_payloads("close_quote", today=trading, calendar=None, universes=["CSI300"])


# --------------------------------------------------------------------------- #
# Scope
# --------------------------------------------------------------------------- #


async def test_scope_unions_pools_and_deduplicates(db_session):
    del db_session
    ids = await seed_universe(["600519.SH", "000001.SZ"])
    watch = await seed_watchlist(["000001.SZ", "300750.SZ", "002594.SZ"], disabled=("002594.SZ",))
    async with session_scope() as session:
        listings = ListingRepository(session)
        candidate = await listings.ensure_a_share_listing("601318.SH")
        holding = await listings.ensure_a_share_listing("920748.BJ")
        session.add(
            CandidateRecord(
                listing_id=candidate.id, ticker="601318.SH", status="open",
                opened_at=CAPTURED_AT, expires_at=CAPTURED_AT + timedelta(days=14), sources=[],
            )
        )
        session.add(
            CandidateRecord(
                listing_id=watch["300750.SZ"], ticker="300750.SZ", status="dismissed",
                opened_at=CAPTURED_AT, expires_at=CAPTURED_AT, closed_at=CAPTURED_AT, sources=[],
            )
        )
        session.add(
            HoldingItem(
                listing_id=holding.id, cost_basis=Decimal(30), shares=Decimal(100),
                opened_at=TRADE_DATE,
            )
        )
    async with session_scope() as session:
        scope = await close_quote_scope(session, _payload(), TRADE_DATE)
    assert list(scope.listing_by_ticker) == [
        "000001.SZ", "300750.SZ", "600519.SH", "601318.SH", "920748.BJ",
    ]
    assert scope.listing_by_ticker["600519.SH"] == ids["600519.SH"]
    assert (scope.universe, scope.watchlist, scope.candidates, scope.holdings) == (2, 2, 1, 1)
    async with session_scope() as session:
        only_user = await close_quote_scope(
            session, CloseQuotePayload(scope="watchlist", trade_date=TRADE_DATE), TRADE_DATE
        )
    assert list(only_user.listing_by_ticker) == ["000001.SZ", "300750.SZ", "601318.SH", "920748.BJ"]
    assert only_user.universe == 0


async def test_empty_target_pool_is_a_precondition_error(db_session, tmp_path):
    del db_session
    with pytest.raises(SnapshotPreconditionError, match="universe_sync"):
        await _collect(FakeQuoteClient(), tmp_path)
    # The user's pools may be empty: a watchlist-only run just has nothing to do.
    async with session_scope() as session:
        scope = await close_quote_scope(
            session, CloseQuotePayload(scope="watchlist", trade_date=TRADE_DATE), TRADE_DATE
        )
    assert scope.listing_by_ticker == {}


# --------------------------------------------------------------------------- #
# Collection
# --------------------------------------------------------------------------- #


async def test_writes_tencent_rows_with_units_and_artifact(db_session, tmp_path):
    del db_session
    ids = await seed_universe(POOL)
    client = FakeQuoteClient(
        closes={"600519.SH": 1255.79, "000001.SZ": 11.78, "000002.SZ": 4.06, "920748.BJ": 32.47},
        suspended={"920748.BJ"},
        chunk_size=3,
    )
    report = await _collect(client, tmp_path)

    assert report.status == "success" and report.provider == "tencent"
    assert client.calls == [["000001.SZ", "000002.SZ", "600519.SH"], ["920748.BJ"]]
    rows = {row.listing_id: row for row in await _rows("tencent")}
    assert set(rows) == set(ids.values())
    moutai = rows[ids["600519.SH"]]
    assert (moutai.trade_date, moutai.adjust, moutai.version) == (TRADE_DATE, "none", 1)
    assert moutai.close == Decimal("1255.79")
    assert moutai.volume == Decimal(2516800) and moutai.amount == Decimal(3145410000)
    assert moutai.turnover == Decimal("0.002") and moutai.pe_ttm == Decimal("19.28")
    assert moutai.pb_mrq is None and moutai.ps_ttm is None and moutai.total_shares is None
    assert moutai.trade_status == 1 and moutai.is_st is False
    assert moutai.captured_at.replace(tzinfo=UTC) == CAPTURED_AT
    assert moutai.raw_ref == "snapshot-runs/1/attempt-1/close-quote-2026-10-08-000.json"
    assert (tmp_path / moutai.raw_ref).exists()
    halted = rows[ids["920748.BJ"]]
    assert halted.trade_status == 0 and halted.volume == 0
    assert halted.raw_ref.endswith("close-quote-2026-10-08-001.json")
    metadata = report.to_metadata()
    assert metadata["completeness"]["collected"] == 4 and metadata["completeness"]["ratio"] == 1.0
    assert metadata["members"] == {
        "members": 4, "universe": 4, "watchlist": 0, "candidates": 0, "holdings": 0,
    }
    assert metadata["slot"] == "close" and metadata["tencent_requests"] == 2
    assert metadata["rows_inserted"] == 4 and metadata["flags"]["suspended"] == ["920748.BJ"]
    assert metadata["aborted"] is None


async def test_skips_listings_baostock_or_a_previous_slot_already_stored(db_session, tmp_path):
    del db_session
    ids = await seed_universe(POOL)
    await _seed_bar(ids["600519.SH"], TRADE_DATE, provider="baostock")
    await _seed_bar(ids["000002.SZ"], TRADE_DATE - timedelta(days=1), provider="baostock")
    await _seed_bar(ids["000001.SZ"], TRADE_DATE, provider="tencent", captured_at=CAPTURED_AT)
    # A backward series row does not count: only the raw series is compared.
    await _seed_bar(ids["920748.BJ"], TRADE_DATE, provider="baostock", adjust="backward")
    client = FakeQuoteClient(closes={"000002.SZ": 4.06, "920748.BJ": 32.47})

    report = await _collect(client, tmp_path, _payload(slot="retry"))

    assert client.calls == [["000002.SZ", "920748.BJ"]]
    assert report.status == "success"
    assert report.completeness.collected == set(POOL)
    assert report.metadata["baostock_present"] == 1 and report.metadata["already_present"] == 1
    assert report.metadata["requested"] == 2 and report.metadata["slot"] == "retry"
    assert len(await _rows("tencent")) == 3


async def test_pending_is_excluded_from_the_ratio_and_version_appends(db_session, tmp_path):
    del db_session
    ids = await seed_universe(POOL)
    # A watchlist iFinD capture of the same key: the Tencent row becomes version 2.
    await _seed_bar(ids["600519.SH"], TRADE_DATE, provider="ifind", captured_at=CAPTURED_AT)
    client = FakeQuoteClient(
        closes={"600519.SH": 1255.79, "000002.SZ": 4.06}, pending={"000001.SZ"}
    )
    report = await _collect(client, tmp_path)

    assert report.status == "success"
    assert report.completeness.pending == ["000001.SZ"]
    assert report.completeness.tickers("no_data") == ["920748.BJ"]
    assert report.completeness.ratio == round(2 / 3, 4)
    assert report.metadata["pending"] == ["000001.SZ"]
    moutai = [row for row in await _rows() if row.listing_id == ids["600519.SH"]]
    assert [(row.provider, row.version) for row in moutai] == [("ifind", 1), ("tencent", 2)]
    assert "session not closed" in report.summary() or "pending 1" in report.summary()


async def test_terminal_failure_marks_the_chunk_failed_without_tripping(db_session, tmp_path):
    del db_session
    await seed_universe(POOL)
    circuit = CircuitBreaker(lambda: datetime.now(UTC))
    client = FakeQuoteClient(
        closes={"600519.SH": 1255.79, "920748.BJ": 32.47},
        errors=[ProviderDataError("tencent quote HTTP 403", provider="tencent"), None],
        chunk_size=2,
    )
    report = await _collect(client, tmp_path, circuit=circuit)
    assert report.status == "partial"
    assert report.completeness.failed == ["000001.SZ", "000002.SZ"]
    assert report.completeness.collected == {"600519.SH", "920748.BJ"}
    assert report.all_failures_transient is False
    assert circuit.snapshot() == {}  # terminal errors never open the circuit
    assert report.metadata["aborted"] is None


async def test_breaker_trips_the_circuit_and_the_next_run_is_partial_at_once(
    db_session, tmp_path
):
    del db_session
    pool = [*POOL, "600000.SH"]
    await seed_universe(pool)
    await seed_universe(["601318.SH", "601398.SH"], universe="SSE50")
    error = ProviderTransientError("tencent quote HTTP 502", provider="tencent")
    with freeze_time("2026-10-08 07:10:00"):
        circuit = CircuitBreaker(lambda: datetime.now(UTC))
        client = FakeQuoteClient(
            closes=dict.fromkeys(pool, 10.0),
            errors=[None, *[error] * MAX_CONSECUTIVE_TRANSIENT],
            chunk_size=1,
        )
        report = await _collect(client, tmp_path, circuit=circuit)
        assert report.status == "partial" and report.stop_reason is None
        assert report.all_failures_transient
        # one chunk stored, three consecutive failures, the fifth chunk is not tried
        assert len(client.calls) == 1 + MAX_CONSECUTIVE_TRANSIENT
        assert client.calls[-1] == ["600519.SH"]
        assert report.completeness.collected == {"000001.SZ"}
        assert report.completeness.failed == ["000002.SZ", "600000.SH", "600519.SH", "920748.BJ"]
        assert report.metadata["aborted"].startswith(
            f"skipped: tencent unavailable after {MAX_CONSECUTIVE_TRANSIENT} consecutive"
        )
        reason = report.completeness.missing["920748.BJ"][1]
        assert reason.startswith("skipped: tencent unavailable")
        assert circuit.status("tencent", ANY_ENDPOINT).is_open(datetime.now(UTC))
        assert len(await _rows()) == 1

        # Seconds later (the retry slot or a CLI run): nothing is called.
        client = FakeQuoteClient(closes=dict.fromkeys(pool, 10.0))
        payload = _payload(slot="retry")
        report = await _collect(
            _CircuitGuarded(client, circuit), tmp_path, payload, circuit=circuit, run_id=2
        )
        assert report.status == "partial" and report.stop_reason == "provider_unavailable"
        assert report.completeness.collected == {"000001.SZ"}  # stored by the first slot
        assert report.completeness.stopped == sorted(set(pool) - {"000001.SZ"})
        assert client.calls == []
        assert "unavailable until" in str(report.to_metadata()["stop_error"])
        assert report.metadata["tencent_requests"] == 0

    # Nothing collected at all is ``failed`` (the worker retries into the open
    # circuit and lands on ``partial``), like the baostock collector.
    client = FakeQuoteClient(errors=[error] * MAX_CONSECUTIVE_TRANSIENT, chunk_size=1)
    whole = await _collect(client, tmp_path, _payload(universe_code="SSE50"), run_id=3)
    assert whole.status == "failed"
    assert isinstance(whole.failure(), ProviderTransientError)


@dataclass
class _CircuitGuarded:
    """What the real client does before a request: consult the breaker."""

    inner: FakeQuoteClient
    circuit: CircuitBreaker

    @property
    def chunk_size(self) -> int:
        return self.inner.chunk_size

    async def fetch_close(self, tickers, trade_date):
        self.circuit.check("tencent", "q")
        return await self.inner.fetch_close(tickers, trade_date)


async def test_open_circuit_mid_run_keeps_what_was_stored(db_session, tmp_path):
    del db_session
    await seed_universe(POOL)
    circuit = CircuitBreaker(lambda: datetime.now(UTC))
    client = FakeQuoteClient(closes=dict.fromkeys(POOL, 10.0), chunk_size=2)
    client.errors = [
        None,
        ProviderUnavailableError(
            "tencent q is unavailable until 2026-10-08T07:15:00+00:00",
            provider="tencent", open_until=datetime.now(UTC) + timedelta(minutes=5),
            endpoint="*", fail_count=2,
        ),
    ]
    report = await _collect(client, tmp_path, circuit=circuit)
    assert report.status == "partial" and report.stop_reason == "provider_unavailable"
    assert report.completeness.collected == {"000001.SZ", "000002.SZ"}
    assert report.completeness.stopped == ["600519.SH", "920748.BJ"]
    assert report.summary().endswith("stopped: tencent unavailable, 2 not collected")
    assert len(await _rows("tencent")) == 2
    assert notification_level(_run_like(report), threshold=0.95) == "action_required"


def _run_like(report):
    class _Run:
        kind = CLOSE_QUOTE
        status = report.status
        metadata_json = report.to_metadata()

    return _Run()


# --------------------------------------------------------------------------- #
# Read precedence and conflicts
# --------------------------------------------------------------------------- #


async def test_series_as_of_prefers_baostock_per_trade_date(db_session, tmp_path):
    del db_session
    ids = await seed_universe(["600519.SH"])
    listing_id = ids["600519.SH"]
    yesterday = TRADE_DATE - timedelta(days=1)
    earlier = EVENING - timedelta(days=1)
    await _seed_bar(listing_id, yesterday, provider="baostock", close=1258.62, captured_at=earlier)
    await _seed_bar(listing_id, yesterday, provider="tencent", close=1258.60, captured_at=earlier)
    await _seed_bar(listing_id, TRADE_DATE, provider="tencent", close=1255.79, captured_at=CAPTURED_AT)
    async with session_scope() as session:
        repo = MarketSnapshotRepository(session)
        afternoon = await repo.series_as_of(listing_id, yesterday, TRADE_DATE, CAPTURED_AT)
        assert [(r.trade_date, r.provider, float(r.close)) for r in afternoon] == [
            (yesterday, "baostock", 1258.62),
            (TRADE_DATE, "tencent", 1255.79),
        ]
        # The evening baostock bar for the trade date takes over (D2).
        await repo.insert(
            listing_id=listing_id, trade_date=TRADE_DATE, open=Decimal(1), high=Decimal(1),
            low=Decimal(1), close=Decimal("1255.80"), volume=Decimal(1), provider="baostock",
            captured_at=EVENING,
        )
        evening = await repo.series_as_of(listing_id, yesterday, TRADE_DATE, EVENING)
        assert [(r.provider, float(r.close)) for r in evening] == [
            ("baostock", 1258.62), ("baostock", 1255.80),
        ]
        still_afternoon = await repo.series_as_of(listing_id, yesterday, TRADE_DATE, CAPTURED_AT)
        assert [r.provider for r in still_afternoon] == ["baostock", "tencent"]
        # Explicit precedence lists restrict the sources; a name pins one.
        only_tencent = await repo.series_as_of(
            listing_id, yesterday, TRADE_DATE, EVENING, provider=("tencent",)
        )
        assert [r.provider for r in only_tencent] == ["tencent", "tencent"]
        pinned = await repo.series_as_of(listing_id, yesterday, TRADE_DATE, EVENING, provider="baostock")
        assert [float(r.close) for r in pinned] == [1258.62, 1255.80]
        by_provider = await repo.series_by_provider(listing_id, yesterday, TRADE_DATE, EVENING)
        assert {day: sorted(rows) for day, rows in by_provider.items()} == {
            yesterday: ["baostock", "tencent"], TRADE_DATE: ["baostock", "tencent"],
        }
    assert MARKET_PROVIDER_PRECEDENCE == ("baostock", "tencent")


async def test_factor_view_marks_same_day_close_conflicts(db_session, tmp_path):
    del db_session
    ids = await seed_universe(["600519.SH"])
    listing_id = ids["600519.SH"]
    history = [TRADE_DATE - timedelta(days=offset) for offset in (3, 2, 1)]
    for day in history:
        await _seed_bar(listing_id, day, provider="baostock", close=1250.0, captured_at=EVENING - timedelta(days=1))
        await _seed_bar(listing_id, day, provider="baostock", close=12500.0, captured_at=EVENING - timedelta(days=1), adjust="backward")
    # 15:10: only the Tencent close exists for the trade date.
    await _seed_bar(listing_id, TRADE_DATE, provider="tencent", close=1255.79, captured_at=CAPTURED_AT)
    async with session_scope() as session:
        view = await load_snapshot_view(session, "600519.SH", TRADE_DATE, policy=TARGET_POOL)
    latest = view.latest_raw_bar()
    assert latest is not None and latest.provider == "tencent" and latest.close == 1255.79
    assert latest.conflicts == ()
    assert view.raw_bars[0].provider == "baostock" and len(view.raw_bars) == 4
    pe = DEFAULT_REGISTRY.get("pe_ttm").compute(view)
    assert pe.inputs_ref["market"]["provider"] == "tencent"
    assert "conflicts" not in pe.inputs_ref["market"]

    # 18:30: the baostock bar arrives 0.5 % away -> baostock wins, conflict recorded.
    await _seed_bar(listing_id, TRADE_DATE, provider="baostock", close=1262.00, captured_at=EVENING)
    async with session_scope() as session:
        view = await load_snapshot_view(session, "600519.SH", TRADE_DATE, policy=TARGET_POOL)
    latest = view.latest_raw_bar()
    assert latest is not None and latest.provider == "baostock" and latest.close == 1262.0
    (conflict,) = latest.conflicts
    assert conflict["provider"] == "tencent" and conflict["value"] == 1255.79
    assert conflict["field"] == "close" and conflict["tolerance"] == 0.001
    assert conflict["relative_difference"] == pytest.approx(abs(1262 - 1255.79) / 1262, abs=1e-6)
    pe = DEFAULT_REGISTRY.get("pe_ttm").compute(view)
    assert pe.inputs_ref["market"]["verification"] == "conflict"
    assert pe.inputs_ref["market"]["conflicts"] == [conflict]

    # Within 0.1 % there is no conflict (a later baostock version supersedes).
    await _seed_bar(listing_id, TRADE_DATE, provider="baostock", close=1255.80, captured_at=EVENING)
    async with session_scope() as session:
        view = await load_snapshot_view(session, "600519.SH", TRADE_DATE, policy=TARGET_POOL)
        observation = await load_snapshot_view(session, "600519.SH", TRADE_DATE, policy=OBSERVATION)
    latest = view.latest_raw_bar()
    assert latest is not None and latest.close == 1255.8 and latest.conflicts == ()
    assert observation.raw_bars == ()  # the observation policy still reads iFinD only


# --------------------------------------------------------------------------- #
# Worker handler with the real client (respx) and health freshness
# --------------------------------------------------------------------------- #


def _fixture_answer(request: httpx.Request) -> httpx.Response:
    lines = {line.split("=", 1)[0]: line for line in SAMPLE.splitlines() if line.startswith("v_")}
    path = httpx.URL(str(request.url)).path
    codes = path.removeprefix("/q=").split(",")
    body = "\n".join(lines[f"v_{code}"] for code in codes if f"v_{code}" in lines)
    return httpx.Response(200, content=body.encode("gbk"))


@respx.mock
async def test_worker_runs_the_handler_end_to_end(db_session, tmp_path):
    del db_session
    ids = await seed_universe([*POOL, "600000.SH"])
    route = respx.get(url__regex=rf"{BASE_URL}/q=.+").mock(side_effect=_fixture_answer)
    settings = settings_for(tmp_path)
    circuit = CircuitBreaker(lambda: datetime.now(UTC))
    # 16:20 Asia/Shanghai on the trade date; the fixture quotes are dated 2026-10-08 16:14.
    captured = datetime(2026, 10, 8, 8, 20, tzinfo=UTC)
    client = TencentQuoteClient(lambda: captured, circuit=circuit, retry_backoff_seconds=0)
    handlers = handlers_for(settings, None, close_quote=client, clock=lambda: captured)
    async with session_scope() as session:
        run, created = await enqueue_snapshot_job(session, _payload(), triggered_by="test")
        run_id = run.id
    assert created
    worker = ResearchWorker(settings=settings, worker_id="w", handlers=handlers)
    await worker.run_once(run_id=run_id)

    async with session_scope() as session:
        finished = await PipelineRunRepository(session).get(run_id)
    assert finished is not None and finished.status == "success", finished.error_summary
    assert route.call_count == 1
    completeness = finished.metadata_json["completeness"]
    assert completeness["collected"] == 4 and completeness["expected"] == 5
    assert completeness["missing"]["600000.SH"]["kind"] == "no_data"
    rows = {row.listing_id: row for row in await _rows("tencent")}
    assert rows[ids["600519.SH"]].close == Decimal("1255.79")
    assert rows[ids["920748.BJ"]].amount == Decimal("138670100")
    artifact = tmp_path / rows[ids["600519.SH"]].raw_ref
    assert '贵州茅台' in artifact.read_text(encoding="utf-8")
    # Same key, same day: the second enqueue finds the finished run.
    async with session_scope() as session:
        again, created = await enqueue_snapshot_job(session, _payload(), triggered_by="test")
    assert not created and again.id == run_id


async def test_health_freshness_shows_the_close_quote_date(db_session):
    ids = await seed_universe(POOL)
    member_ids = list(ids.values())
    await _seed_bar(ids["600519.SH"], TRADE_DATE - timedelta(days=1), provider="baostock")
    await _seed_bar(ids["600519.SH"], TRADE_DATE, provider="tencent", captured_at=CAPTURED_AT)
    freshness = await market_freshness(
        db_session, "CSI300", member_ids, today=TRADE_DATE, calendar=CALENDAR
    )
    assert freshness.latest_trade_date == TRADE_DATE - timedelta(days=1)  # baostock only
    assert freshness.latest_close_quote_date == TRADE_DATE
    assert freshness.lag_trading_days == 0  # 10-07 is a holiday: baostock is not stale
    empty = await market_freshness(db_session, "CSI300", [], today=TRADE_DATE, calendar=CALENDAR)
    assert empty.latest_close_quote_date is None
