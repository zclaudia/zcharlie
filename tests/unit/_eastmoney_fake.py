"""Offline AkShare Eastmoney backend replaying ``tests/fixtures/eastmoney/`` (P1-A11).

The fixtures are real answers captured on 2026-10-07 with akshare 1.19.1:
the period summaries (``stock_lrb_em`` / ``stock_xjll_em`` / ``stock_zcfz_em``)
of FY2024 and 2026H1 restricted to nine listings (incl. banks 601398 /
000001, insurer 601318, broker 600030), and the 12 latest periods of the
per-stock cash-flow statement of 600519 / 601318 / 601398 / 600030 (only the
columns the collector keeps; ``all_columns`` lists the full answer).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import Any

from investment_agent.providers.akshare_eastmoney import (
    AkshareEastmoneyClient,
    RawTable,
    SummaryEndpoint,
)

FIXTURE_DIR = Path(__file__).parents[1] / "fixtures" / "eastmoney"
CAPTURED_AT = datetime(2026, 10, 7, 8, 0, tzinfo=UTC)


@cache
def _load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def summary_table(endpoint: str, period: str) -> RawTable:
    payload = _load(f"{endpoint}_{period}.json")
    columns = tuple(payload["columns"])
    return RawTable(
        columns, tuple(dict(zip(columns, row, strict=True)) for row in payload["rows"])
    )


def cash_flow_table(symbol: str) -> RawTable:
    payload = _load(f"cash_flow_{symbol}.json")
    kept = payload["columns"]
    return RawTable(
        tuple(payload["all_columns"]),
        tuple(dict(zip(kept, row, strict=True)) for row in payload["rows"]),
    )


def cash_flow_symbols() -> list[str]:
    return sorted(path.stem.removeprefix("cash_flow_") for path in FIXTURE_DIR.glob("cash_flow_*"))


class FakeEastmoneyBackend:
    """Serves fixture tables; ``errors`` are raised (in order) before answering."""

    def __init__(
        self,
        *,
        periods: tuple[str, ...] = ("20241231", "20260630"),
        overrides: dict[tuple[str, str], RawTable | None] | None = None,
        errors: list[Exception | None] | None = None,
    ) -> None:
        self.periods = periods
        self.overrides = dict(overrides or {})
        self.errors = list(errors or [])
        self.calls: list[str] = []

    def _maybe_fail(self) -> None:
        if self.errors:
            error = self.errors.pop(0)
            if error is not None:
                raise error

    def period_table(self, endpoint: SummaryEndpoint, period: str) -> RawTable | None:
        self.calls.append(f"{endpoint}:{period}")
        self._maybe_fail()
        if (endpoint, period) in self.overrides:
            return self.overrides[(endpoint, period)]
        if period not in self.periods:
            return None
        return summary_table(endpoint, period)

    def cash_flow_table(self, symbol: str) -> RawTable | None:
        self.calls.append(f"cash_flow:{symbol}")
        self._maybe_fail()
        if ("cash_flow", symbol) in self.overrides:
            return self.overrides[("cash_flow", symbol)]
        if symbol not in cash_flow_symbols():
            return None
        return cash_flow_table(symbol)


class NoSleep:
    def __init__(self) -> None:
        self.slept: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.slept.append(seconds)


def fake_client(
    backend: FakeEastmoneyBackend | None = None,
    *,
    clock: datetime = CAPTURED_AT,
    min_interval_seconds: float = 0.0,
    sleep: NoSleep | None = None,
    max_attempts: int = 2,
) -> AkshareEastmoneyClient:
    return AkshareEastmoneyClient(
        backend or FakeEastmoneyBackend(),
        clock=lambda: clock,
        min_interval_seconds=min_interval_seconds,
        sleep=sleep or NoSleep(),
        max_attempts=max_attempts,
        retry_backoff_seconds=0.0,
    )
