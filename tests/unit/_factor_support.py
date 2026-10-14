"""Helpers building factor views from the captured baostock fixtures."""

from __future__ import annotations

import json
from datetime import date, timedelta
from functools import cache
from pathlib import Path
from typing import Any

from investment_agent.factors.field_resolution import factor_fields
from investment_agent.factors.snapshot_model import (
    TARGET_POOL,
    Bar,
    FinancialPeriod,
    SnapshotView,
)

FIXTURE_DIR = Path(__file__).parents[1] / "fixtures" / "factors"
# CSRC classification of the captured symbols (baostock, 2026-10-05).
INDUSTRY = {
    "600519": ("C15", "酒、饮料和精制茶制造业"),
    "601398": ("J66", "货币金融服务"),
}


@cache
def baostock_fixture(symbol: str) -> dict[str, Any]:
    return json.loads((FIXTURE_DIR / f"baostock_{symbol}.json").read_text(encoding="utf-8"))


def fixture_raw_bars(symbol: str) -> list[Bar]:
    payload = baostock_fixture(symbol)["bars_none"]
    bars = []
    for row in payload["rows"]:
        record = dict(zip(payload["columns"], row, strict=True))
        bars.append(
            Bar(
                trade_date=date.fromisoformat(record["date"]),
                close=record["close"],
                pe_ttm=record["pe_ttm"],
                pb_mrq=record["pb_mrq"],
                ps_ttm=record["ps_ttm"],
                turnover=record["turnover"],
                trade_status=record["trade_status"],
                is_st=record["is_st"],
                ref=f"fixture:none:{record['date']}",
            )
        )
    return bars


def fixture_backward_bars(symbol: str) -> list[Bar]:
    return [
        Bar(trade_date=date.fromisoformat(day), close=close, ref=f"fixture:backward:{day}")
        for day, close in baostock_fixture(symbol)["bars_backward"]["rows"]
    ]


def fixture_periods(symbol: str) -> list[FinancialPeriod]:
    return [
        FinancialPeriod(
            report_period=date.fromisoformat(period["report_period"]),
            period_kind=period["period_kind"],
            available_from=date.fromisoformat(period["available_from"]),
            availability="actual",
            fields=factor_fields("baostock", period["fields"]),
            ref=f"fixture:{period['report_period']}",
        )
        for period in baostock_fixture(symbol)["financials"]
    ]


def baostock_view(
    symbol: str,
    as_of: date,
    *,
    overdue: tuple[date, ...] = (),
    industry: tuple[str | None, str | None] | None = None,
) -> SnapshotView:
    code, name = industry if industry is not None else INDUSTRY[symbol]
    return SnapshotView.build(
        ticker=f"{symbol}.SH",
        as_of=as_of,
        policy=TARGET_POOL,
        industry_code=code,
        industry_name=name,
        raw_bars=fixture_raw_bars(symbol),
        adjusted_bars=fixture_backward_bars(symbol),
        periods=fixture_periods(symbol),
        overdue_periods=overdue,
    )


def synthetic_bars(
    end: date,
    sessions: int,
    *,
    close: float = 10.0,
    pe_ttm: float | None = 10.0,
    pb_mrq: float | None = 1.0,
    step_days: int = 1,
) -> list[Bar]:
    """``sessions`` bars ending on ``end``, one every ``step_days`` calendar days."""

    return [
        Bar(
            trade_date=end - timedelta(days=step_days * (sessions - 1 - index)),
            close=close,
            pe_ttm=pe_ttm,
            pb_mrq=pb_mrq,
            ps_ttm=1.0,
            turnover=0.01,
            trade_status=1,
            is_st=False,
        )
        for index in range(sessions)
    ]


def annual(year: int, available: date | None = None, **fields: float | None) -> FinancialPeriod:
    return FinancialPeriod(
        report_period=date(year, 12, 31),
        period_kind="annual",
        available_from=available or date(year + 1, 4, 30),
        availability="actual",
        fields=dict(fields),
        ref=f"synthetic:{year}",
    )
