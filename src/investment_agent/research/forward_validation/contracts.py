"""Frozen contracts for the offline forward-validation tool (P1 item 7).

The tool evaluates an already frozen scoring version and the stored rule
templates against a point-in-time cross-section. It never fits, tunes or
writes: every threshold below is a fixed default, recorded in
``ValidationConfig.config_hash`` so a report can be reproduced.

Vocabulary used throughout the package (see ``docs/product/forward-validation-v1.md``):

* **signal session** - the last market session on or before ``as_of``; the
  score sees data public by ``as_of`` only.
* **entry** - the first tradable session after the signal session (T+1),
  deferred while the stock is suspended or opens at the limit-up price.
* **horizon** - a fixed number of *market sessions* after entry
  (1/3/6 months = 21/63/126 sessions). Sessions come from the stored market
  calendar, never from the stock's own bars, so a suspension does not shrink
  the window.
* **gross return** - backward-adjusted price return from the entry open to
  the exit close.
* **net return** - gross return after the explicit :class:`CostModel`;
  this is the only return used for "executable" metrics.
* **signal return** - signal-session close to exit close, gross, reported
  only to show how much of a signal is *not* executable.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

FORMULA_VERSION = "forward-validation.v1"
#: The frozen scoring version this tool is allowed to evaluate.
SCORING_VERSION_EXPECTED = "v1.4"
#: Horizon month -> market sessions after entry.
HORIZON_SESSIONS: dict[int, int] = {1: 21, 3: 63, 6: 126}
HORIZON_LABELS: dict[int, str] = {1: "1m", 3: "3m", 6: "6m"}
BENCHMARKS: tuple[str, ...] = ("equal_weight", "csi300")
TEMPLATE_SOURCES: tuple[str, ...] = ("db", "files")

ReportStatus = Literal["ok", "partial", "insufficient"]
TemplateSource = Literal["db", "files"]

#: Price rows are final once their session closes. Forward evaluation reads
#: stored bars through this fixed sentinel so a report does not depend on the
#: wall clock of the run (see ``dataset.py``).
PRICE_KNOWLEDGE_SENTINEL = "2100-01-01T00:00:00+00:00"

GUARD_NOTE = (
    "calibration_status is never changed by this tool; the report only says "
    "whether the observed sample is sufficient to consider a future, separately "
    "reviewed calibration change"
)


@dataclass(frozen=True)
class CostModel:
    """Explicit round-trip cost assumption (basis points).

    These are *assumptions*, not measured costs. They are applied to both the
    candidate observation and the equal-weight benchmark so the excess return
    is cost-neutral. ``stamp_duty_bps`` is charged on the sell side only.
    """

    commission_bps: float = 3.0
    stamp_duty_bps: float = 5.0
    slippage_bps: float = 10.0

    def __post_init__(self) -> None:
        for name in ("commission_bps", "stamp_duty_bps", "slippage_bps"):
            value = getattr(self, name)
            if not isinstance(value, float | int) or not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be a finite non-negative number")

    @property
    def buy_factor(self) -> float:
        return 1.0 + (self.commission_bps + self.slippage_bps) / 10_000.0

    @property
    def sell_factor(self) -> float:
        return 1.0 - (
            self.commission_bps + self.slippage_bps + self.stamp_duty_bps
        ) / 10_000.0

    @property
    def round_trip_bps(self) -> float:
        return 2.0 * (self.commission_bps + self.slippage_bps) + self.stamp_duty_bps

    def to_dict(self) -> dict[str, Any]:
        return {
            "commission_bps": self.commission_bps,
            "stamp_duty_bps": self.stamp_duty_bps,
            "slippage_bps": self.slippage_bps,
            "round_trip_bps": round(self.round_trip_bps, 4),
            "applied_to": "candidate_and_equal_weight_benchmark",
            "note": "assumption, not a measured cost",
        }


@dataclass(frozen=True)
class ValidationConfig:
    """Frozen run parameters. There is deliberately no per-date override."""

    as_of_dates: tuple[date, ...]
    universe: str = "CSI300"
    horizons: tuple[int, ...] = (1, 3, 6)
    benchmark: str = "equal_weight"
    cost: CostModel = field(default_factory=CostModel)
    min_scored: int = 20
    min_group_size: int = 5
    min_coverage: float = 0.5
    min_template_hits: int = 5
    max_entry_deferral: int = 5
    max_exit_deferral: int = 5
    template_source: TemplateSource = "db"
    template_refs: tuple[str, ...] | None = None
    scoring_version_expected: str = SCORING_VERSION_EXPECTED
    allow_scoring_version_mismatch: bool = False
    include_backfill: bool = True

    def __post_init__(self) -> None:
        if not self.as_of_dates:
            raise ValueError("at least one as_of date is required")
        if len(set(self.as_of_dates)) != len(self.as_of_dates):
            raise ValueError("as_of dates must be unique")
        if not self.horizons:
            raise ValueError("at least one horizon is required")
        unknown = sorted(set(self.horizons) - set(HORIZON_SESSIONS))
        if unknown:
            raise ValueError(f"unsupported horizons {unknown}; choose from {sorted(HORIZON_SESSIONS)}")
        if self.benchmark not in BENCHMARKS:
            raise ValueError(f"benchmark must be one of {BENCHMARKS}")
        if self.template_source not in TEMPLATE_SOURCES:
            raise ValueError(f"template_source must be one of {TEMPLATE_SOURCES}")
        if self.min_scored < 2:
            raise ValueError("min_scored must be >= 2")
        if self.min_group_size < 1:
            raise ValueError("min_group_size must be >= 1")
        if not 0 < self.min_coverage <= 1:
            raise ValueError("min_coverage must be in (0, 1]")
        if self.min_template_hits < 1:
            raise ValueError("min_template_hits must be >= 1")
        if self.max_entry_deferral < 0 or self.max_exit_deferral < 0:
            raise ValueError("deferral limits must be >= 0")
        if not self.scoring_version_expected:
            raise ValueError("scoring_version_expected must not be empty")

    def to_dict(self) -> dict[str, Any]:
        return {
            "as_of_dates": [item.isoformat() for item in self.as_of_dates],
            "universe": self.universe,
            "horizons": list(self.horizons),
            "horizon_sessions": {str(month): HORIZON_SESSIONS[month] for month in self.horizons},
            "benchmark": self.benchmark,
            "cost": self.cost.to_dict(),
            "min_scored": self.min_scored,
            "min_group_size": self.min_group_size,
            "min_coverage": self.min_coverage,
            "min_template_hits": self.min_template_hits,
            "max_entry_deferral": self.max_entry_deferral,
            "max_exit_deferral": self.max_exit_deferral,
            "template_source": self.template_source,
            "template_refs": list(self.template_refs) if self.template_refs else None,
            "scoring_version_expected": self.scoring_version_expected,
            "allow_scoring_version_mismatch": self.allow_scoring_version_mismatch,
            "include_backfill": self.include_backfill,
        }

    @property
    def config_hash(self) -> str:
        payload = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()


@dataclass(frozen=True)
class UniverseEntry:
    listing_id: int
    ticker: str
    valid_from: date
    valid_to: date | None


@dataclass(frozen=True)
class Tradability:
    tradable: bool
    reason: str | None
    """Blocking reason (``suspended`` / ``limit_up`` / ``limit_down``)."""
    flags: tuple[str, ...] = ()
    """Non-blocking diagnostics (``limit_unknown`` / ``missing_bar``)."""


@dataclass(frozen=True)
class PriceSeries:
    """Raw and backward-adjusted bars of one listing, ascending by session."""

    ticker: str
    raw_open: dict[date, float]
    raw_close: dict[date, float]
    adjusted_open: dict[date, float]
    adjusted_close: dict[date, float]
    volume: dict[date, float]
    trade_status: dict[date, int | None]
    is_st: dict[date, bool | None]
    adjusted_source: str = "backward"
    """``backward`` or ``raw_fallback`` (no adjusted series stored)."""


@dataclass(frozen=True)
class HorizonOutcome:
    months: int
    status: str
    """``ok`` / ``pending_horizon`` / ``entry_untradable`` / ``exit_untradable``
    / ``no_price`` / ``no_signal_session`` / ``stale_forward_data``."""
    reason: str | None = None
    entry_date: date | None = None
    exit_date: date | None = None
    entry_deferral_sessions: int = 0
    exit_deferral_sessions: int = 0
    gross_return: float | None = None
    net_return: float | None = None
    signal_return: float | None = None
    flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class ScoredObservation:
    """One PIT member at one ``as_of``: score inputs plus forward outcomes."""

    ticker: str
    listing_id: int
    as_of: date
    signal_session: date
    industry_group: str
    investment_score: float | None
    factor_coverage: float | None
    needs_agent_review: bool
    score_suppression_reason: str | None
    selected_model: str | None
    quality_signals: tuple[str, ...]
    score_status: str
    """``scored`` / ``suppressed`` / ``failed`` / ``missing_listing``."""
    score_failure: str | None
    outcomes: dict[int, HorizonOutcome]
    template_hits: tuple[str, ...] = ()
    template_strength: dict[str, float] = field(default_factory=dict)
    max_input_date: date | None = None
    """Latest date any scoring input was public: must not exceed ``as_of``."""

    def outcome(self, months: int) -> HorizonOutcome | None:
        return self.outcomes.get(months)


class LookaheadError(RuntimeError):
    """A scoring input dated after ``as_of``; the run must stop, not guess."""
