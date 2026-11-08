"""Pure statistics for the forward-validation tool (no database, no clock).

Everything here is deterministic given its arguments: the report reproduces
byte-for-byte from the same stored data plus :class:`ValidationConfig`.

Conventions
-----------
* Ties in ranked correlation use average ranks (the textbook Spearman).
* A decile/tercile bucket exists only when it has at least
  ``min_group_size`` observations; otherwise it is reported as ``insufficient``
  and never silently dropped from the denominator.
* IC is computed only within one ``as_of`` cross-section. A pooled number is
  never presented as if it were a time-series IC.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from investment_agent.research.forward_validation.contracts import (
    HORIZON_LABELS,
    HORIZON_SESSIONS,
)


def average_ranks(values: Sequence[float]) -> list[float]:
    """1-based ranks with tied values sharing their mean rank."""

    order = sorted(range(len(values)), key=lambda index: values[index])
    ranks = [0.0] * len(values)
    position = 0
    while position < len(order):
        end = position
        while end + 1 < len(order) and values[order[end + 1]] == values[order[position]]:
            end += 1
        rank = (position + end) / 2.0 + 1.0
        for index in range(position, end + 1):
            ranks[order[index]] = rank
        position = end + 1
    return ranks


def pearson(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    """Pearson correlation; ``None`` for fewer than 2 points or zero variance."""

    if len(xs) != len(ys) or len(xs) < 2:
        return None
    mean_x = statistics.fmean(xs)
    mean_y = statistics.fmean(ys)
    dx = [value - mean_x for value in xs]
    dy = [value - mean_y for value in ys]
    sx = math.sqrt(sum(value * value for value in dx))
    sy = math.sqrt(sum(value * value for value in dy))
    if sx == 0.0 or sy == 0.0:
        return None
    return sum(a * b for a, b in zip(dx, dy, strict=True)) / (sx * sy)


def spearman(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    """Spearman rank correlation (average ranks for ties)."""

    if len(xs) != len(ys) or len(xs) < 2:
        return None
    return pearson(average_ranks(xs), average_ranks(ys))


def safe_mean(values: Iterable[float]) -> float | None:
    items = [value for value in values if value is not None and math.isfinite(value)]
    return statistics.fmean(items) if items else None


def safe_median(values: Iterable[float]) -> float | None:
    items = [value for value in values if value is not None and math.isfinite(value)]
    return statistics.median(items) if items else None


def safe_stdev(values: Iterable[float]) -> float | None:
    items = [value for value in values if value is not None and math.isfinite(value)]
    return statistics.pstdev(items) if len(items) >= 2 else None


def hit_rate(values: Iterable[float]) -> float | None:
    items = [value for value in values if value is not None and math.isfinite(value)]
    if not items:
        return None
    return sum(1 for value in items if value > 0) / len(items)


@dataclass(frozen=True)
class Bucket:
    """One score bucket of one cross-section (or pooled descriptive bucket)."""

    label: str
    lower_rank: int
    """1-based rank bounds, ``lower_rank`` inclusive and ``upper_rank`` inclusive."""
    upper_rank: int
    count: int
    mean_net_excess: float | None
    median_net_excess: float | None
    hit_rate: float | None
    mean_net_return: float | None
    below_min_group_size: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "count": self.count,
            "below_min_group_size": self.below_min_group_size,
            "mean_net_excess": self.mean_net_excess,
            "median_net_excess": self.median_net_excess,
            "hit_rate": self.hit_rate,
            "mean_net_return": self.mean_net_return,
        }


def bucket_count(group_size: int, total: int) -> int:
    """Largest affordable bucket count, preferring deciles then terciles.

    ``total`` observations split into ``n`` roughly equal buckets, each with at
    least ``group_size`` members. Returns 0 when no split is affordable, which
    the caller reports as ``insufficient`` rather than inventing groups.
    """

    for candidate in (10, 5, 3):
        if total // candidate >= group_size:
            return candidate
    return 0


def score_buckets(
    pairs: Sequence[tuple[float, float | None, float]],
    *,
    group_size: int,
    buckets: int | None = None,
) -> tuple[list[Bucket], int]:
    """Rank ``pairs`` by score and split into ``buckets`` equal-count groups.

    ``pairs`` is ``(score, net_excess, net_return)``; rows with a missing excess
    are ignored for the statistics but remain part of the input count. Returns
    ``(buckets, used)`` where ``used`` is the number of rows with a usable
    excess. The top group is bucket ``1`` (highest scores).
    """

    usable = [
        (score, excess, ret)
        for score, excess, ret in pairs
        if excess is not None and math.isfinite(excess) and math.isfinite(score)
    ]
    if not usable:
        return [], 0
    usable.sort(key=lambda item: (-item[0], item[1], item[2]))
    count = buckets if buckets is not None else bucket_count(group_size, len(usable))
    if count <= 0:
        return [], len(usable)
    size, remainder = divmod(len(usable), count)
    result: list[Bucket] = []
    start = 0
    for index in range(count):
        length = size + (1 if index < remainder else 0)
        chunk = usable[start : start + length]
        lower = start + 1
        upper = start + length
        excesses = [item[1] for item in chunk]
        returns = [item[2] for item in chunk]
        result.append(
            Bucket(
                label=f"b{index + 1}",
                lower_rank=lower,
                upper_rank=upper,
                count=len(chunk),
                mean_net_excess=safe_mean(excesses),
                median_net_excess=safe_median(excesses),
                hit_rate=hit_rate(excesses),
                mean_net_return=safe_mean(returns),
                below_min_group_size=len(chunk) < group_size,
            )
        )
        start += length
    return result, len(usable)


def top_minus_bottom(buckets: Sequence[Bucket]) -> float | None:
    """Mean net excess of the highest-score bucket minus the lowest."""

    if len(buckets) < 2:
        return None
    top, bottom = buckets[0].mean_net_excess, buckets[-1].mean_net_excess
    if top is None or bottom is None:
        return None
    return top - bottom


def monotonicity(buckets: Sequence[Bucket]) -> dict[str, Any]:
    """Whether bucket mean net excess increases from the bottom to the top bucket.

    ``spearman`` compares bucket order (1 = lowest score) with the bucket mean;
    ``strict`` additionally requires every step to be non-negative. Buckets
    below ``min_group_size`` make the verdict ``insufficient``.
    """

    ordered = list(buckets)
    if not ordered:
        return {"status": "insufficient", "spearman": None, "strict": False, "steps": []}
    if any(bucket.below_min_group_size for bucket in ordered):
        return {"status": "insufficient", "spearman": None, "strict": False, "steps": []}
    means = [bucket.mean_net_excess for bucket in ordered]
    if any(value is None for value in means):
        return {"status": "insufficient", "spearman": None, "strict": False, "steps": []}
    values = [float(value) for value in means if value is not None]
    # Bucket 1 is the top score; reverse so ascending score order maps to ascending index.
    ascending = list(reversed(values))
    order = [float(index) for index in range(len(ascending))]
    steps = [ascending[index + 1] - ascending[index] for index in range(len(ascending) - 1)]
    return {
        "status": "ok",
        "spearman": spearman(order, ascending),
        "strict": all(step >= 0 for step in steps),
        "steps": [round(step, 8) for step in steps],
    }


def clean_number(value: float | None) -> float | None:
    """Round for a stable report; ``None`` stays ``None``."""

    if value is None or not math.isfinite(value):
        return None
    return round(value, 8)


def horizon_key(months: int) -> str:
    return HORIZON_LABELS[months]


__all__ = [
    "Bucket",
    "HORIZON_SESSIONS",
    "average_ranks",
    "bucket_count",
    "clean_number",
    "hit_rate",
    "horizon_key",
    "monotonicity",
    "pearson",
    "safe_mean",
    "safe_median",
    "safe_stdev",
    "score_buckets",
    "spearman",
    "top_minus_bottom",
]
