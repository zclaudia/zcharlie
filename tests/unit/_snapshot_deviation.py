"""Snapshot-mode vs iFinD-mode deviation report (P1-B5; informational, no thresholds).

Compares the canonical inputs and the kernel output of the two research
source modes for one listing: the iFinD side is a golden-corpus sample
(frozen ``pool`` / ``market`` / ``financial`` + kernel replay), the snapshot
side any snapshot bundle (fixture view in tests, the database in a sanity
run). Usable from tests and ad-hoc scripts.
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from investment_agent.research.ifind_chain import CanonicalBundle
from investment_agent.research.kernel import InProcessResearchKernel

FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "research_kernel"

COMPARED_FIELDS: tuple[tuple[str, str], ...] = (
    ("market", "latest_price"),
    ("market", "pe"),
    ("market", "pb"),
    ("market", "ps"),
    ("market", "dividend_yield"),
    ("market", "return_1y"),
    ("market", "volatility_1y"),
    ("market", "max_drawdown_1y"),
    ("market", "market_cap"),
    ("financial", "revenue"),
    ("financial", "net_profit"),
    ("financial", "gross_margin"),
    ("financial", "net_margin"),
    ("financial", "roe"),
    ("financial", "roe_stability"),
    ("financial", "debt_ratio"),
    ("financial", "operating_cash_flow"),
    ("financial", "free_cash_flow"),
    ("financial", "revenue_growth_3y"),
    ("financial", "net_profit_growth_3y"),
)
COMPARED_SCORE_FIELDS = (
    "strategy_type",
    "selected_model",
    "business_score",
    "research_confidence_score",
    "suggestion",
    "valuation_level",
    "risk_level",
    "needs_agent_review",
)


@dataclass(frozen=True)
class DeviationRow:
    ticker: str
    scope: str
    field: str
    ifind: Any
    snapshot: Any

    @property
    def relative(self) -> float | None:
        if not all(isinstance(v, int | float) and not isinstance(v, bool) for v in (self.ifind, self.snapshot)):
            return None
        if self.ifind == 0:
            return None
        return (float(self.snapshot) - float(self.ifind)) / abs(float(self.ifind))


def golden_bundle(ticker: str) -> CanonicalBundle:
    directory = FIXTURE_ROOT / ticker.split(".")[0]
    payloads = {
        name: json.loads((directory / f"{name}.json").read_text(encoding="utf-8"))
        for name in ("pool", "market", "financial")
    }
    return CanonicalBundle(raw={}, **payloads)


def score_bundle(bundle: CanonicalBundle, as_of: str) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        paths = {}
        for name in ("pool", "market", "financial"):
            paths[name] = root / f"{name}.json"
            paths[name].write_text(json.dumps(getattr(bundle, name)), encoding="utf-8")
        artifacts = InProcessResearchKernel().score_and_render(
            pool=paths["pool"],
            market=paths["market"],
            financial=paths["financial"],
            artifact_dir=root,
            as_of=as_of,
        )
        scores = json.loads(artifacts.scores_json.read_text(encoding="utf-8"))
    (item,) = scores["items"]
    return item


def _item(bundle: CanonicalBundle, scope: str) -> dict[str, Any]:
    (item,) = getattr(bundle, scope)["items"]
    return item


def deviation_rows(
    ticker: str,
    ifind: CanonicalBundle,
    snapshot: CanonicalBundle,
    *,
    as_of: str,
) -> list[DeviationRow]:
    rows = [
        DeviationRow(ticker, scope, field, _item(ifind, scope).get(field), _item(snapshot, scope).get(field))
        for scope, field in COMPARED_FIELDS
    ]
    ifind_score, snapshot_score = score_bundle(ifind, as_of), score_bundle(snapshot, as_of)
    rows.extend(
        DeviationRow(ticker, "score", field, ifind_score.get(field), snapshot_score.get(field))
        for field in COMPARED_SCORE_FIELDS
    )
    return rows


def _fmt(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.4g}" if abs(value) < 1e6 else f"{value:.4e}"
    return str(value)


def render_table(rows: list[DeviationRow]) -> str:
    lines = [
        "| ticker | scope | field | iFinD | snapshot | rel. diff |",
        "|---|---|---|---|---|---|",
    ]
    for row in rows:
        relative = row.relative
        lines.append(
            f"| {row.ticker} | {row.scope} | {row.field} | {_fmt(row.ifind)} | "
            f"{_fmt(row.snapshot)} | {'-' if relative is None else f'{relative:+.1%}'} |"
        )
    return "\n".join(lines) + "\n"
