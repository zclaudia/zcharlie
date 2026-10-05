"""Boundary and in-process implementation for deterministic A-share research."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, cast

from investment_agent.research_kernel.reporting import (
    build_report_context,
    render_report_markdown,
)
from investment_agent.research_kernel.scoring import score_pool

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
    """Repository-owned port of deterministic ``my-stocks`` scoring v1.2."""

    max_market_age_days: int = 7
    max_financial_age_days: int = 180
    max_stock_age_days: int = 30

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
            max_market_age_days=self.max_market_age_days,
            max_financial_age_days=self.max_financial_age_days,
            max_stock_age_days=self.max_stock_age_days,
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
