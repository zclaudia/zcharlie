"""Offline baostock backend replaying ``tests/fixtures/baostock/{daily,financial}.json``.

* k-data: every recorded row for ``(code, adjustflag)`` is pooled and the
  requested ``[start_date, end_date]`` window is cut from the pool, so
  collectors may ask for any window inside the recorded range;
* quarterly tables: keyed by ``(method, code, year, quarter)``; unrecorded
  combinations answer like an undisclosed period (no rows);
* ``extra_rows`` / ``fail`` let tests add synthetic rows or inject errors.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "baostock"


class Status:
    def __init__(self, error_code: str = "0", error_msg: str = "success") -> None:
        self.error_code = error_code
        self.error_msg = error_msg


class ReplayResult:
    def __init__(
        self,
        fields: list[str],
        rows: list[list[str]],
        *,
        error_code: str = "0",
        error_msg: str = "success",
    ) -> None:
        self.error_code = error_code
        self.error_msg = error_msg
        self.fields = list(fields)
        self._rows = [list(row) for row in rows]
        self._index = 0

    def next(self) -> bool:
        return self._index < len(self._rows)

    def get_row_data(self) -> list[str]:
        row = self._rows[self._index]
        self._index += 1
        return row


def _load(name: str) -> list[dict[str, Any]]:
    payload = json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))
    return list(payload["requests"])


@dataclass
class FakeBaostockData:
    """Replay backend; ``calls`` records ``(method, params)`` in order."""

    fail: Callable[[str, dict[str, Any]], tuple[str, str] | Exception | None] | None = None
    extra_rows: dict[tuple[str, str], list[dict[str, str]]] = field(default_factory=dict)
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    logins: int = 0
    logged_in: bool = False

    def __post_init__(self) -> None:
        self.kdata_fields: list[str] = []
        self.kdata: dict[tuple[str, str], dict[str, list[str]]] = {}
        for record in _load("daily.json"):
            params = record["params"]
            self.kdata_fields = record["fields"]
            pool = self.kdata.setdefault((params["code"], params["adjustflag"]), {})
            for row in record["rows"]:
                pool[row[0]] = row
        self.quarterly: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        self.quarterly_fields: dict[str, list[str]] = {}
        for record in _load("financial.json"):
            params = record["params"]
            key = (record["method"], params["code"], params["year"], params["quarter"])
            self.quarterly[key] = record
            self.quarterly_fields[record["method"]] = record["fields"]

    # -- session ----------------------------------------------------------- #

    def login(self) -> Status:
        self.calls.append(("login", {}))
        self.logins += 1
        self.logged_in = True
        return Status()

    def logout(self) -> Status:
        self.calls.append(("logout", {}))
        self.logged_in = False
        return Status()

    def methods(self, name: str) -> list[dict[str, Any]]:
        return [params for method, params in self.calls if method == name]

    def _check(self, method: str, params: dict[str, Any]) -> ReplayResult | None:
        assert self.logged_in, f"{method} outside a login session"
        self.calls.append((method, params))
        if self.fail is not None:
            outcome = self.fail(method, params)
            if isinstance(outcome, Exception):
                raise outcome
            if outcome is not None:
                code, message = outcome
                return ReplayResult([], [], error_code=code, error_msg=message)
        return None

    # -- queries ----------------------------------------------------------- #

    def query_history_k_data_plus(
        self,
        code: str,
        fields: str,
        start_date: str | None = None,
        end_date: str | None = None,
        frequency: str = "d",
        adjustflag: str = "3",
    ) -> ReplayResult:
        params = {
            "code": code,
            "start_date": start_date,
            "end_date": end_date,
            "adjustflag": adjustflag,
        }
        failed = self._check("query_history_k_data_plus", params)
        if failed is not None:
            return failed
        names = fields.split(",")
        assert names == self.kdata_fields, "collector must request the recorded field list"
        pool = dict(self.kdata.get((code, adjustflag), {}))
        for extra in self.extra_rows.get((code, adjustflag), []):
            pool[extra["date"]] = [extra.get(name, "") for name in names]
        start = start_date or "0000-00-00"
        end = end_date or "9999-99-99"
        rows = [pool[day] for day in sorted(pool) if start <= day <= end]
        return ReplayResult(names, rows)

    def _quarterly(self, method: str, code: str, year: int | None, quarter: int | None) -> ReplayResult:
        params = {"code": code, "year": year, "quarter": quarter}
        failed = self._check(method, params)
        if failed is not None:
            return failed
        record = self.quarterly.get((method, code, str(year), str(quarter)))
        if record is None:
            return ReplayResult(self.quarterly_fields[method], [])
        return ReplayResult(record["fields"], record["rows"])

    def query_profit_data(
        self, code: str, year: int | None = None, quarter: int | None = None
    ) -> ReplayResult:
        return self._quarterly("query_profit_data", code, year, quarter)

    def query_balance_data(
        self, code: str, year: int | None = None, quarter: int | None = None
    ) -> ReplayResult:
        return self._quarterly("query_balance_data", code, year, quarter)

    def query_cash_flow_data(
        self, code: str, year: int | None = None, quarter: int | None = None
    ) -> ReplayResult:
        return self._quarterly("query_cash_flow_data", code, year, quarter)

    def query_growth_data(
        self, code: str, year: int | None = None, quarter: int | None = None
    ) -> ReplayResult:
        return self._quarterly("query_growth_data", code, year, quarter)
