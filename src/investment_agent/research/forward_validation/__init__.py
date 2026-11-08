"""Offline, reproducible forward validation of scoring v1.4 and rule templates.

P1 item 7 of ``docs/review/valuation-methodology-review-2026-11-07.md``: score a
point-in-time cross-section (index constituents as of each ``as_of``, removed
names included), measure tradable T+1 entry returns at 1/3/6-month trading
session horizons against a benchmark after explicit costs, and report bucket
spreads, monotonicity, Spearman IC, hit rates and per-template hit spreads.

The tool never fits, tunes, writes or changes ``calibration_status``: a run
that cannot reach the configured sample size returns ``insufficient``.

See ``docs/product/forward-validation-v1.md`` for the full protocol.
"""

from investment_agent.research.forward_validation.contracts import (
    FORMULA_VERSION,
    HORIZON_SESSIONS,
    CostModel,
    ScoredObservation,
    ValidationConfig,
)
from investment_agent.research.forward_validation.dataset import load_dataset
from investment_agent.research.forward_validation.evaluation import Benchmark
from investment_agent.research.forward_validation.runner import (
    failed_report,
    run_forward_validation,
    run_forward_validation_sync,
    write_report,
)

__all__ = [
    "FORMULA_VERSION",
    "HORIZON_SESSIONS",
    "Benchmark",
    "CostModel",
    "ScoredObservation",
    "ValidationConfig",
    "failed_report",
    "load_dataset",
    "run_forward_validation",
    "run_forward_validation_sync",
    "write_report",
]
