"""Cross-section aggregation and the sufficiency verdict (pure).

The report is built in two layers:

1. :func:`evaluate_observations` turns one ``as_of`` cross-section into per
   horizon score statistics (buckets, IC, monotonicity, hit rate) plus the
   explicitly separated review/coverage groups.
2. :func:`build_report` walks every ``as_of`` in the frozen config, adds the
   template-hit comparison and the benchmark comparison, and decides
   ``ok`` / ``partial`` / ``insufficient`` from the sample size only.

No function here changes ``calibration_status`` or applies any threshold that
was chosen from the observed results: thresholds come from
:class:`ValidationConfig` and are recorded in ``config_hash``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from investment_agent.research.forward_validation import stats
from investment_agent.research.forward_validation.contracts import (
    FORMULA_VERSION,
    GUARD_NOTE,
    HORIZON_LABELS,
    HORIZON_SESSIONS,
    SCORING_VERSION_EXPECTED,
    ScoredObservation,
    ValidationConfig,
)

#: Coverage bands, mutually exclusive and adding up to every scored member.
#: ``high`` is >= 0.9, ``mid`` is [min_coverage, 0.9) and ``low`` is below
#: ``min_coverage``; ``suppressed`` is a member with no published score.
#: The review flag is a *separate* axis so both questions stay answerable.
COVERAGE_GROUPS: dict[str, str] = {
    "high": "factor_coverage >= 0.9",
    "mid": "min_coverage <= factor_coverage < 0.9",
    "low": "factor_coverage < min_coverage",
    "suppressed": "investment_score is null (coverage floor or no factor data)",
}
HIGH_COVERAGE = 0.9


@dataclass(frozen=True)
class Benchmark:
    """Per-``as_of`` benchmark net return, keyed by horizon month.

    One benchmark window per cross-section (first tradable session after the
    signal session, the fixed session count to the exit) applies to every
    member, so excess returns stay internally consistent even when an
    individual entry is deferred by a suspension.
    """

    name: str
    by_horizon: Mapping[int, Mapping[date, float | None]]
    construction: str
    note: str

    def return_for(self, months: int, as_of: date) -> float | None:
        return self.by_horizon.get(months, {}).get(as_of)


def _valid_outcome(item: ScoredObservation, months: int) -> Any:
    outcome = item.outcome(months)
    if outcome is None or outcome.status != "ok" or outcome.net_return is None:
        return None
    return outcome


def _excess(value: float, benchmark: float | None) -> float | None:
    if benchmark is None:
        return None
    return value - benchmark


def _excess_series(
    items: Sequence[ScoredObservation],
    months: int,
    benchmark: Benchmark,
) -> list[tuple[ScoredObservation, float, float | None]]:
    rows: list[tuple[ScoredObservation, float, float | None]] = []
    for item in items:
        outcome = _valid_outcome(item, months)
        if outcome is None or item.investment_score is None:
            continue
        reference = benchmark.return_for(months, item.as_of)
        rows.append((item, float(outcome.net_return), _excess(float(outcome.net_return), reference)))
    return rows


def _excess_series_all(
    items: Sequence[ScoredObservation],
    months: int,
    benchmark: Benchmark,
) -> list[tuple[ScoredObservation, float, float | None]]:
    rows: list[tuple[ScoredObservation, float, float | None]] = []
    for item in items:
        outcome = _valid_outcome(item, months)
        if outcome is None:
            continue
        reference = benchmark.return_for(months, item.as_of)
        rows.append((item, float(outcome.net_return), _excess(float(outcome.net_return), reference)))
    return rows


def _group_summary(
    items: Sequence[ScoredObservation],
    months: int,
    benchmark: Benchmark,
    *,
    require_score: bool = True,
) -> dict[str, Any]:
    """Forward stats of a group; ``require_score=False`` keeps suppressed members.

    The coverage bands include members whose score is suppressed, so their
    forward returns stay visible (they cannot be part of an IC, but they can
    show whether "low coverage" was systematically overrated by the model that
    declined to score them).
    """

    rows = (
        _excess_series(items, months, benchmark)
        if require_score
        else _excess_series_all(items, months, benchmark)
    )
    excesses = [excess for _, _, excess in rows if excess is not None]
    returns = [value for _, value, _ in rows]
    return {
        "count": len(rows),
        "mean_net_excess": stats.clean_number(stats.safe_mean(excesses)),
        "median_net_excess": stats.clean_number(stats.safe_median(excesses)),
        "hit_rate": stats.clean_number(stats.hit_rate(excesses)),
        "mean_net_return": stats.clean_number(stats.safe_mean(returns)),
        "unpriced": len([item for item in items if item.outcome(months) is not None])
        - len(rows),
    }


def coverage_group(item: ScoredObservation, min_coverage: float) -> str:
    """Coverage band membership; ``needs_agent_review`` is not part of it."""

    if item.investment_score is None:
        return "suppressed"
    coverage = item.factor_coverage
    if coverage is None or coverage < min_coverage:
        return "low"
    if coverage >= HIGH_COVERAGE:
        return "high"
    return "mid"


def evaluate_cross_section(
    *,
    as_of: date,
    observations: Sequence[ScoredObservation],
    config: ValidationConfig,
    benchmark: Benchmark,
    diag: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Score statistics for one ``as_of`` cross-section."""

    scored = [item for item in observations if item.investment_score is not None]
    result: dict[str, Any] = {
        "as_of": as_of.isoformat(),
        "members": len(observations),
        "scored": len(scored),
        "failed": len([item for item in observations if item.score_status == "failed"]),
        "missing_listing": len(
            [item for item in observations if item.score_status == "missing_listing"]
        ),
        "sufficient_cross_section": len(scored) >= config.min_scored,
        "diagnostics": dict(diag or {}),
        "horizons": {},
        "coverage_groups": {},
        "review_groups": {},
        "industry_groups": {},
    }
    for months in config.horizons:
        rows = _excess_series(scored, months, benchmark)
        priced = len([item for item in scored if _valid_outcome(item, months) is not None])
        paired = [
            (float(item.investment_score), excess, value)
            for item, value, excess in rows
            if item.investment_score is not None and excess is not None
        ]
        scores = [score for score, _, _ in paired]
        excesses = [excess for _, excess, _ in paired]
        buckets, used = stats.score_buckets(paired, group_size=config.min_group_size)
        ic = stats.spearman(scores, excesses) if len(scores) >= 2 else None
        total = len(scored)
        pending = 0
        untradable = 0
        signal_gaps: list[float] = []
        for item in scored:
            outcome = item.outcome(months)
            if (
                outcome is not None
                and outcome.status == "ok"
                and outcome.signal_return is not None
                and outcome.net_return is not None
            ):
                signal_gaps.append(outcome.signal_return - outcome.net_return)
        resolved_statuses = {"ok", "pending_horizon", "entry_untradable", "exit_untradable"}
        unresolved_statuses: dict[str, int] = {}
        for item in scored:
            outcome = item.outcome(months)
            status = "no_outcome" if outcome is None else outcome.status
            if status in resolved_statuses:
                if status == "pending_horizon":
                    pending += 1
                elif status in {"entry_untradable", "exit_untradable"}:
                    untradable += 1
                continue
            unresolved_statuses[status] = unresolved_statuses.get(status, 0) + 1
        unresolved = sum(unresolved_statuses.values())
        if unresolved != total - priced - pending - untradable:
            raise RuntimeError(
                "forward-validation horizon accounting mismatch: "
                f"scored={total} priced={priced} pending={pending} "
                f"untradable={untradable} unresolved={unresolved}"
            )
        result["horizons"][HORIZON_LABELS[months]] = {
            "sessions": HORIZON_SESSIONS[months],
            "scored": total,
            "evaluated": used,
            "pending_horizon": pending,
            "untradable": untradable,
            "priced": priced,
            "missing_excess": priced - len(rows),
            "unresolved": unresolved,
            "unresolved_statuses": unresolved_statuses,
            "ic_spearman": stats.clean_number(ic),
            "buckets": [bucket.to_dict() for bucket in buckets],
            "monotonicity": stats.monotonicity(buckets),
            "top_minus_bottom": stats.clean_number(stats.top_minus_bottom(buckets)),
            "mean_net_excess": stats.clean_number(stats.safe_mean(excesses)),
            "hit_rate": stats.clean_number(stats.hit_rate(excesses)),
            # Signal (signal-session close -> exit) minus executable net return:
            # the part of a naive "signal return" that is not tradable.
            "mean_signal_minus_net": stats.clean_number(stats.safe_mean(signal_gaps)),
            "signal_net_observations": len(signal_gaps),
        }
        groups: dict[str, Any] = {}
        for name in COVERAGE_GROUPS:
            subset = [
                item for item in observations if coverage_group(item, config.min_coverage) == name
            ]
            groups[name] = _group_summary(
                subset, months, benchmark, require_score=False
            )
            groups[name]["min_group_size_met"] = groups[name]["count"] >= config.min_group_size
        result["coverage_groups"][HORIZON_LABELS[months]] = groups

        # Independent second axis: does the review flag itself predict a bias?
        reviews: dict[str, Any] = {}
        for name, subset in (
            (
                "needs_review",
                [item for item in observations if item.needs_agent_review],
            ),
            (
                "no_review",
                [item for item in observations if not item.needs_agent_review],
            ),
        ):
            reviews[name] = _group_summary(subset, months, benchmark, require_score=False)
            reviews[name]["min_group_size_met"] = (
                reviews[name]["count"] >= config.min_group_size
            )
        result["review_groups"][HORIZON_LABELS[months]] = reviews

        industries: dict[str, Any] = {}
        industry_names = sorted({item.industry_group for item in observations})
        for name in industry_names:
            subset = [item for item in observations if item.industry_group == name]
            summary = _group_summary(subset, months, benchmark, require_score=False)
            summary["min_group_size_met"] = summary["count"] >= config.min_group_size
            industries[name] = summary
        result["industry_groups"][HORIZON_LABELS[months]] = industries
    return result


def _template_hits(
    observations: Sequence[ScoredObservation],
    template_refs: Sequence[str],
) -> dict[str, set[int]]:
    index: dict[str, set[int]] = {ref: set() for ref in template_refs}
    for position, item in enumerate(observations):
        for ref in item.template_hits:
            index.setdefault(ref, set()).add(position)
    return index


def evaluate_template_hits(
    *,
    observations: Sequence[ScoredObservation],
    template_refs: Sequence[str],
    months: int,
    config: ValidationConfig,
    benchmark: Benchmark,
) -> dict[str, Any]:
    """Hit vs. non-hit tradable net excess for each template.

    ``hit`` is the template's stored screen result at the signal session; the
    comparison uses entry-T+1 net returns, never the signal-session return.
    """

    hits = _template_hits(observations, template_refs)
    result: dict[str, Any] = {}
    for ref in template_refs:
        hit_positions = hits.get(ref, set())
        hit_items = [item for pos, item in enumerate(observations) if pos in hit_positions]
        other_items = [item for pos, item in enumerate(observations) if pos not in hit_positions]
        hit_summary = _group_summary(hit_items, months, benchmark)
        other_summary = _group_summary(other_items, months, benchmark)
        spread = None
        if hit_summary["mean_net_excess"] is not None and other_summary["mean_net_excess"] is not None:
            spread = hit_summary["mean_net_excess"] - other_summary["mean_net_excess"]
        result[ref] = {
            "hits": len(hit_items),
            "evaluated_hits": hit_summary["count"],
            "hit_mean_net_excess": hit_summary["mean_net_excess"],
            "hit_hit_rate": hit_summary["hit_rate"],
            "non_hit_count": other_summary["count"],
            "non_hit_mean_net_excess": other_summary["mean_net_excess"],
            "spread_vs_non_hits": stats.clean_number(spread),
            "sufficient": hit_summary["count"] >= config.min_template_hits,
            "min_template_hits": config.min_template_hits,
        }
    return result


def _sufficiency(
    cross_sections: Sequence[Mapping[str, Any]],
    config: ValidationConfig,
    *,
    has_templates: bool,
    failures: Sequence[Mapping[str, Any]],
    benchmark_gaps: int,
    scoring_mismatch: bool = False,
) -> tuple[str, list[str]]:
    """``(status, reasons)`` from sample size and coverage only.

    The verdict is deliberately independent of the observed sign or IC: a
    large negative result is still a usable, sufficient result.
    """

    reasons: list[str] = []
    sufficient = [section for section in cross_sections if section["sufficient_cross_section"]]
    if not cross_sections:
        return "insufficient", ["no cross-section could be built"]
    if not sufficient:
        reasons.append(
            f"no as_of date had >= {config.min_scored} scored members "
            f"(max {max(section['scored'] for section in cross_sections)})"
        )
    # Every requested horizon must be priced on its own: a 6-month horizon
    # inside a 3-month data window is not a (shorter) result, it is missing.
    shortfalls = [
        label
        for label, evaluated in (
            (label, horizon["evaluated"])
            for section in cross_sections
            for label, horizon in section["horizons"].items()
        )
        if evaluated < config.min_scored
    ]
    if shortfalls:
        unique = sorted(set(shortfalls))
        reasons.append(
            "horizons with < " + str(config.min_scored) + " priced observations: "
            + ", ".join(unique)
        )
    if len(cross_sections) < 2:
        reasons.append(
            "a single as_of cross-section is a snapshot, not a walk-forward series; "
            "at least 2 dates are required for an ok verdict"
        )
    if has_templates:
        template_sufficient = any(
            entry["sufficient"]
            for section in cross_sections
            for by_template in (section.get("templates") or {}).values()
            for entry in by_template.values()
        )
        if not template_sufficient:
            reasons.append(
                f"no template had >= {config.min_template_hits} evaluated hits"
            )
    if benchmark_gaps:
        reasons.append(
            f"{benchmark_gaps} observation/horizon rows have no benchmark reference"
        )
    if failures:
        reasons.append(f"{len(failures)} scoring failure(s) recorded")
    if scoring_mismatch:
        reasons.append(
            "the installed scoring version is not the expected frozen version; "
            "the result cannot be attributed to scoring v1.4"
        )
    if not sufficient or shortfalls or scoring_mismatch:
        return "insufficient", reasons
    return ("partial" if reasons else "ok"), reasons


def build_report(
    *,
    config: ValidationConfig,
    observations: Sequence[ScoredObservation],
    benchmark: Benchmark,
    sessions: Sequence[date],
    market_data: Mapping[str, Any],
    scoring: Mapping[str, Any],
    templates: Mapping[str, Any],
    diagnostics: Mapping[str, Any],
    failures: Sequence[Mapping[str, Any]],
    code_version: Mapping[str, Any],
) -> dict[str, Any]:
    """The complete, reproducible report document."""

    by_as_of: dict[date, list[ScoredObservation]] = {}
    for item in observations:
        by_as_of.setdefault(item.as_of, []).append(item)
    template_refs = tuple(sorted(templates.get("template_refs", [])))
    benchmark_gaps = 0
    cross_sections: list[dict[str, Any]] = []
    for as_of in sorted(by_as_of):
        items = by_as_of[as_of]
        section = evaluate_cross_section(
            as_of=as_of,
            observations=items,
            config=config,
            benchmark=benchmark,
            diag=diagnostics.get(as_of.isoformat()),
        )
        section["templates"] = {
            HORIZON_LABELS[months]: evaluate_template_hits(
                observations=items,
                template_refs=template_refs,
                months=months,
                config=config,
                benchmark=benchmark,
            )
            for months in config.horizons
        }
        for horizon in section["horizons"].values():
            benchmark_gaps += int(horizon["missing_excess"])
        cross_sections.append(section)
    status, reasons = _sufficiency(
        cross_sections,
        config,
        has_templates=bool(template_refs),
        failures=list(failures),
        benchmark_gaps=benchmark_gaps,
        scoring_mismatch=(
            scoring.get("matches") is False
            and not config.allow_scoring_version_mismatch
        ),
    )
    lookahead_violations = sorted(
        {
            f"{item.as_of.isoformat()}:{item.ticker}"
            for item in observations
            if item.max_input_date is not None and item.max_input_date > item.as_of
        }
    )
    return {
        "schema_version": FORMULA_VERSION,
        "status": status,
        "insufficient_reasons": reasons,
        "sample": {
            "as_of_dates": [item.isoformat() for item in sorted(by_as_of)],
            "universe": config.universe,
            "members_per_as_of": {
                as_of.isoformat(): len(items) for as_of, items in sorted(by_as_of.items())
            },
            "scored_per_as_of": {
                as_of.isoformat(): len([item for item in items if item.investment_score is not None])
                for as_of, items in sorted(by_as_of.items())
            },
            "total_observations": len(observations),
            "total_scored": len([item for item in observations if item.investment_score is not None]),
            "total_failed": len(failures),
            "market_sessions": {
                "first": sessions[0].isoformat() if sessions else None,
                "last": sessions[-1].isoformat() if sessions else None,
                "count": len(sessions),
            },
            "horizon_sessions": {str(month): HORIZON_SESSIONS[month] for month in config.horizons},
        },
        "config": config.to_dict(),
        "config_hash": config.config_hash,
        "cost_model": config.cost.to_dict(),
        "benchmark": {
            "name": benchmark.name,
            "construction": benchmark.construction,
            "note": benchmark.note,
        },
        "scoring": dict(scoring),
        "templates": dict(templates),
        "market_data": dict(market_data),
        "code_version": dict(code_version),
        "diagnostics": dict(diagnostics),
        "failures": [dict(item) for item in failures],
        "lookahead_check": {
            "violations": lookahead_violations,
            "passed": not lookahead_violations,
            "rule": "every scoring input date must be <= its as_of (Asia/Shanghai)",
        },
        "cross_sections": cross_sections,
        "guard": {
            "calibration_status_changed": False,
            "note": GUARD_NOTE,
        },
    }


def scoring_metadata(scoring_version: str, *, declared: str = SCORING_VERSION_EXPECTED) -> dict[str, Any]:
    return {
        "scoring_version": scoring_version,
        "expected_scoring_version": declared,
        "matches": scoring_version == declared,
    }


def aggregate_horizon(
    cross_sections: Sequence[Mapping[str, Any]],
    label: str,
) -> dict[str, Any]:
    """Pooled descriptive summary across as_of dates (not a time-series IC)."""

    buckets: dict[str, dict[str, Any]] = {}
    ics: list[float] = []
    for section in cross_sections:
        horizon = section["horizons"].get(label)
        if horizon is None:
            continue
        if horizon["ic_spearman"] is not None:
            ics.append(horizon["ic_spearman"])
        for bucket in horizon["buckets"]:
            entry = buckets.setdefault(
                bucket["label"], {"label": bucket["label"], "excesses": [], "count": 0}
            )
            entry["count"] += bucket["count"]
            if bucket["mean_net_excess"] is not None:
                entry["excesses"].append(bucket["mean_net_excess"])
    pooled = [
        {
            "label": entry["label"],
            "observations": entry["count"],
            "mean_of_bucket_means": stats.clean_number(stats.safe_mean(entry["excesses"])),
        }
        for _, entry in sorted(buckets.items())
    ]
    return {
        "horizon": label,
        "ic_mean": stats.clean_number(stats.safe_mean(ics)),
        "ic_observations": len(ics),
        "ic_positive_share": stats.clean_number(
            sum(1 for value in ics if value > 0) / len(ics) if ics else None
        ),
        "buckets": pooled,
    }


__all__ = [
    "COVERAGE_GROUPS",
    "Benchmark",
    "aggregate_horizon",
    "build_report",
    "evaluate_cross_section",
    "evaluate_template_hits",
    "coverage_group",
    "scoring_metadata",
]
