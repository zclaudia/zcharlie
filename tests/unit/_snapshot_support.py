"""Offline fakes shared by the snapshot collector tests."""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from investment_agent.config import Settings
from investment_agent.research.ifind_chain import IfindProxyResult, parse_proxy_rows
from investment_agent.snapshots.handlers import SnapshotRuntime, snapshot_job_handlers
from investment_agent.snapshots.throttle import AsyncRateLimiter
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.reference import UniverseMembership
from investment_agent.storage.repositories.listing import ListingRepository
from investment_agent.storage.repositories.universe import UniverseRepository
from investment_agent.storage.repositories.watchlist import WatchlistRepository

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "ifind_batch"
CAPTURED_AT = datetime(2026, 10, 6, 9, 0, tzinfo=UTC)  # 17:00 Asia/Shanghai


def settings_for(tmp_path: Path, **overrides: Any) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        api_key="test",
        llm_tier3_api_key="test",
        ifind_proxy_api_key="proxy-test",
        data_dir=tmp_path,
        **overrides,
    )


def csv_body(rows: list[dict[str, object]]) -> dict[str, Any]:
    if not rows:
        return {"format": "json", "data": {"data_preview": ""}}
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0]))
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return {"format": "json", "data": {"data_preview": buffer.getvalue()}}


def load_fixture(name: str) -> dict[str, Any]:
    body: dict[str, Any] = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    return body


def _ymd(value: date) -> str:
    return value.strftime("%Y%m%d")


@dataclass
class FakeProxy:
    """Synthesizes iFinD proxy answers; every call is recorded.

    * history: one bar per weekday in the window for each ticker in
      ``listed`` (close = ``base_close[ticker]`` + day offset; backward series
      multiplied by 10), unless the ticker is in ``suspended``;
    * info: ``total_shares[ticker]``;
    * financial: ``financial[(endpoint_key, period)][ticker]`` rows, blank
      rows otherwise.
    """

    base_close: dict[str, float] = field(default_factory=dict)
    suspended: set[str] = field(default_factory=set)
    total_shares: dict[str, float] = field(default_factory=dict)
    financial: dict[tuple[str, str], dict[str, dict[str, str]]] = field(default_factory=dict)
    fail: Callable[[str, dict[str, Any]], Exception | None] | None = None
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    on_call: Callable[[str, dict[str, Any]], Any] | None = None

    async def __aenter__(self) -> FakeProxy:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    def calls_to(self, path: str) -> list[dict[str, Any]]:
        return [payload for called, payload in self.calls if called == path]

    async def post_rows(self, path: str, payload: dict[str, Any]) -> IfindProxyResult:
        self.calls.append((path, dict(payload)))
        if self.on_call is not None:
            await self.on_call(path, payload)
        if self.fail is not None:
            error = self.fail(path, payload)
            if error is not None:
                raise error
        tickers = [t for t in str(payload["ticker"]).split(",") if t]
        assert len(tickers) <= 3, "iFinD accepts at most three tickers per call"
        if path == "/stock/history":
            body = csv_body(self._history(tickers, payload))
        elif path == "/stock/info":
            body = csv_body(
                [
                    {
                        "ths_stock_short_name_stock": ticker,
                        "ths_total_shares_stock": self.total_shares.get(ticker, ""),
                        "thscode": ticker,
                        "time": "",
                    }
                    for ticker in tickers
                ]
            )
        else:
            selector = payload.get("statement") or payload.get("category")
            key = f"{path}:{selector}"
            period = str(payload["report_date"])
            answers = self.financial.get((key, period), {})
            columns = sorted({c for row in answers.values() for c in row}) or ["value"]
            body = csv_body(
                [
                    {**dict.fromkeys(columns, ""), **answers.get(ticker, {}), "thscode": ticker,
                     "time": ""}
                    for ticker in tickers
                ]
            )
        return IfindProxyResult(path=path, body=body, rows=parse_proxy_rows(body))

    def _history(self, tickers: list[str], payload: dict[str, Any]) -> list[dict[str, object]]:
        start = date.fromisoformat(str(payload["start_date"]))
        end = date.fromisoformat(str(payload["end_date"]))
        factor = 10 if payload["adjust"] == "backward" else 1
        rows: list[dict[str, object]] = []
        for ticker in tickers:
            if ticker in self.suspended or ticker not in self.base_close:
                continue
            day = start
            while day <= end:
                if day.weekday() < 5:
                    close = (self.base_close[ticker] + (day - date(2026, 1, 1)).days) * factor
                    rows.append(
                        {
                            "open": close,
                            "high": close + 1,
                            "low": close - 1,
                            "close": close,
                            "volume": 1000,
                            "thscode": ticker,
                            "time": _ymd(day),
                        }
                    )
                day += timedelta(days=1)
        return rows


class FixtureProxy:
    """Replays the recorded 2026-10-06 multi-ticker responses."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __aenter__(self) -> FixtureProxy:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def post_rows(self, path: str, payload: dict[str, Any]) -> IfindProxyResult:
        self.calls.append((path, dict(payload)))
        if path == "/stock/history":
            name = f"history_{payload['adjust']}"
        elif path == "/stock/info":
            name = "info"
        else:
            selector = payload.get("statement") or payload.get("category")
            prefix = {
                "income_statement": "income",
                "cash_flow": "cash",
                "profitability": "profitability",
                "capital_structure": "capital",
            }[str(selector)]
            name = f"{prefix}_{payload['report_date']}"
            if not (FIXTURES / f"{name}.json").exists():
                # Unrecorded combinations answer like an undisclosed period.
                name = name.replace(str(payload["report_date"]), "20260930")
                if not (FIXTURES / f"{name}.json").exists():
                    name = "profitability_20260930"
        body = load_fixture(name)
        return IfindProxyResult(path=path, body=body, rows=parse_proxy_rows(body))


async def seed_universe(
    tickers: list[str], *, universe: str = "CSI300", valid_from: date = date(2026, 1, 5)
) -> dict[str, int]:
    """Open memberships for ``tickers``; returns ticker -> listing id."""

    ids: dict[str, int] = {}
    async with session_scope() as session:
        await UniverseRepository(session).ensure_universe(universe)
        listings = ListingRepository(session)
        for ticker in tickers:
            listing = await listings.ensure_a_share_listing(ticker)
            ids[ticker] = listing.id
            session.add(
                UniverseMembership(
                    universe_code=universe,
                    listing_id=listing.id,
                    valid_from=valid_from,
                    valid_to=None,
                    source="test",
                    captured_at=CAPTURED_AT,
                )
            )
    return ids


async def seed_watchlist(tickers: list[str], *, disabled: tuple[str, ...] = ()) -> dict[str, int]:
    """Watchlist items for ``tickers``; returns ticker -> listing id."""

    ids: dict[str, int] = {}
    async with session_scope() as session:
        listings = ListingRepository(session)
        watchlist = WatchlistRepository(session)
        for ticker in tickers:
            listing = await listings.ensure_a_share_listing(ticker)
            ids[ticker] = listing.id
            await watchlist.add(listing.id, enabled=ticker not in disabled)
    return ids


class NoSleep:
    def __init__(self) -> None:
        self.slept: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.slept.append(seconds)


def runtime_for(
    settings: Settings,
    proxy: Any,
    *,
    reference: Any = None,
    baostock: Any = None,
    shenwan: Any = None,
    eastmoney: Any = None,
    dividend: Any = None,
    clock: Callable[[], datetime] = lambda: CAPTURED_AT,
) -> SnapshotRuntime:
    from tests.unit._eastmoney_fake import fake_client as fake_eastmoney_client
    from tests.unit._shenwan_fake import fake_shenwan_client

    return SnapshotRuntime(
        settings=settings,
        limiter=AsyncRateLimiter(6000, burst=1000),
        proxy_factory=lambda: proxy,
        reference_factory=(lambda: reference) if reference is not None else (lambda: None),  # type: ignore[arg-type,return-value]
        clock=clock,
        sleep=NoSleep(),
        batch_backoff_seconds=0,
        baostock_factory=(lambda: baostock) if baostock is not None else (lambda: None),  # type: ignore[arg-type,return-value]
        # offline: the Shenwan history replays tests/fixtures/shenwan
        shenwan_factory=(lambda: shenwan) if shenwan is not None else fake_shenwan_client,
        # offline: Eastmoney answers replay tests/fixtures/eastmoney
        eastmoney_factory=(lambda: eastmoney) if eastmoney is not None else fake_eastmoney_client,
        dividend_factory=(lambda: dividend or baostock),  # type: ignore[arg-type,return-value]
    )


def handlers_for(settings: Settings, proxy: Any, **kwargs: Any) -> list[Any]:
    return list(snapshot_job_handlers(settings, runtime_for(settings, proxy, **kwargs)))


def as_decimal(value: float) -> Decimal:
    return Decimal(str(value))
