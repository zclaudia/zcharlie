"""Offline AkShare Shenwan backend replaying ``tests/fixtures/shenwan/`` (P1-A9).

The fixtures are real answers captured by ``tools/refresh_shenwan_directory.py
--fixtures --cross-check`` on 2026-10-06: history rows of the CSI 300 fixture
members plus edge cases, Shenwan's official code table, the three legulegu
index-info tables and the constituents of two level-3 indexes.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import Any

from investment_agent.providers.akshare_shenwan import (
    AkshareShenwanClient,
    InfoLevel,
    RawTable,
    ShenwanError,
)

FIXTURE_DIR = Path(__file__).parents[1] / "fixtures" / "shenwan"
CAPTURED_AT = datetime(2026, 10, 6, 8, 0, tzinfo=UTC)


@cache
def _load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def _table(payload: dict[str, Any]) -> RawTable:
    return RawTable(tuple(payload["columns"]), tuple(payload["rows"]))


def history_table() -> RawTable:
    return _table(_load("history.json"))


def class_code_table() -> RawTable:
    return _table(_load("class_code.json"))


def info_table(level: int) -> RawTable:
    return _table(_load("index_info.json")[f"level{level}"])


def constituents_table(index_code: str) -> RawTable:
    return _table(_load("constituents.json")["indexes"][index_code])


class FakeShenwanBackend:
    def __init__(self, history: RawTable | None = None, *, errors: list[Exception] | None = None):
        self.history = history or history_table()
        self.errors = list(errors or [])
        self.calls: list[str] = []

    def _maybe_fail(self) -> None:
        if self.errors:
            raise self.errors.pop(0)

    def industry_history(self) -> RawTable:
        self.calls.append("history")
        self._maybe_fail()
        return self.history

    def class_code_table(self) -> RawTable:
        self.calls.append("class_code")
        return class_code_table()

    def index_info(self, level: InfoLevel) -> RawTable:
        self.calls.append(f"info{level}")
        return info_table(level)

    def third_constituents(self, index_code: str) -> RawTable:
        self.calls.append(index_code)
        try:
            return constituents_table(index_code)
        except KeyError:
            return RawTable(("股票代码",), ())


class FailingShenwanClient:
    """A Shenwan client whose history call fails terminally."""

    async def fetch_industry_history(self) -> Any:
        raise ShenwanError("HTTP 404 from https://www.swsresearch.com/x", provider="akshare")


def fake_shenwan_client(
    backend: FakeShenwanBackend | None = None, *, captured_at: datetime = CAPTURED_AT
) -> AkshareShenwanClient:
    async def no_sleep(_: float) -> None:
        return None

    return AkshareShenwanClient(
        backend or FakeShenwanBackend(),
        clock=lambda: captured_at,
        retry_backoff_seconds=0,
        constituent_pause_seconds=0,
        sleep=no_sleep,
    )
