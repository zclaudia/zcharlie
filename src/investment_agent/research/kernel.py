"""Boundary and in-process implementation for deterministic A-share research."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol, cast

from investment_agent.research_kernel.reporting import (
    build_report_context,
    render_report_markdown,
)
from investment_agent.research_kernel.scoring import (
    DEFAULT_MAX_MARKET_AGE_TRADING_DAYS,
    TradingDayPredicate,
    score_pool,
)

# Versioned exchange calendar shipped with the repository. The kernel itself
# stays decoupled from the scheduler: it only receives an ``is_trading_day``
# predicate and falls back to weekday counting for years the calendar does
# not cover (or when the file is not available, e.g. in a wheel install).
DEFAULT_TRADING_CALENDAR_PATH = (
    Path(__file__).resolve().parents[3] / "config" / "calendars" / "a_share.yaml"
)


@lru_cache(maxsize=4)
def _load_trading_day_predicate(path: Path) -> TradingDayPredicate | None:
    if not path.is_file():
        return None
    from investment_agent.scheduler.trading_calendar import load_trading_calendar

    try:
        calendar = load_trading_calendar(path)
    except (OSError, ValueError):
        return None

    def is_trading_day(value: date) -> bool:
        return calendar.is_trading_day(value)

    return is_trading_day

CSV_FIELDS = (
    "symbol",
    "name",
    "strategy_label",
    "investment_score",
    "business_score",
    "research_confidence_score",
    "risk_level",
    "valuation_level",
    "suggestion_label",
    "data_quality_score",
    "needs_agent_review",
)


@dataclass(frozen=True)
class KernelArtifacts:
    scores_json: Path
    scores_csv: Path
    report_context_json: Path
    report_markdown: Path


class DeterministicResearchKernel(Protocol):
    """Scores canonical data and renders deterministic research context."""

    def score_and_render(
        self,
        *,
        pool: Path,
        market: Path,
        financial: Path,
        artifact_dir: Path,
        as_of: str | datetime | None = None,
    ) -> KernelArtifacts: ...


@dataclass(frozen=True)
class InProcessResearchKernel:
    """Repository-owned deterministic research kernel (scoring v1.3).

    Market freshness is measured in exchange trading sessions using the
    versioned A-share calendar when available.
    """

    max_market_age_trading_days: int = DEFAULT_MAX_MARKET_AGE_TRADING_DAYS
    max_financial_age_days: int = 180
    max_stock_age_days: int = 30
    trading_calendar_path: Path | None = DEFAULT_TRADING_CALENDAR_PATH

    @staticmethod
    def _read_payload(path: Path) -> dict[str, Any]:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"research kernel input must be a JSON object: {path}")
        return cast(dict[str, Any], payload)

    @staticmethod
    def _write_json(path: Path, payload: object) -> None:
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def _write_scores_csv(path: Path, rows: list[dict[str, Any]]) -> None:
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
            writer.writeheader()
            for row in rows:
                writer.writerow({field: row.get(field, "") for field in CSV_FIELDS})

    def score_and_render(
        self,
        *,
        pool: Path,
        market: Path,
        financial: Path,
        artifact_dir: Path,
        as_of: str | datetime | None = None,
    ) -> KernelArtifacts:
        artifacts = KernelArtifacts(
            scores_json=artifact_dir / "scores.json",
            scores_csv=artifact_dir / "scores.csv",
            report_context_json=artifact_dir / "report_context.json",
            report_markdown=artifact_dir / "deterministic_report.md",
        )
        scores = score_pool(
            self._read_payload(pool),
            self._read_payload(market),
            self._read_payload(financial),
            pool_name="ifind-single-stock",
            as_of=as_of,
            max_market_age_trading_days=self.max_market_age_trading_days,
            max_financial_age_days=self.max_financial_age_days,
            max_stock_age_days=self.max_stock_age_days,
            is_trading_day=(
                None
                if self.trading_calendar_path is None
                else _load_trading_day_predicate(self.trading_calendar_path)
            ),
        )
        context = build_report_context(scores, top=1)
        self._write_json(artifacts.scores_json, scores)
        self._write_scores_csv(artifacts.scores_csv, scores["items"])
        self._write_json(artifacts.report_context_json, context)
        artifacts.report_markdown.write_text(
            render_report_markdown(context),
            encoding="utf-8",
        )
        return artifacts
