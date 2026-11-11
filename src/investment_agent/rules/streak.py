"""Consecutive-period gate for trim, falsified-sell and re-review conditions.

A condition with ``min_periods`` N counts only after it has held on N
consecutive evaluated periods. One period that misses resets the count. The
same ``as_of`` is idempotent: a re-run starts from the counts that entered
that date, so a replay does not add an extra period. A later date continues
from the previous date's counts. An older date does not rewind stored state.

A period is one zone-detection ``as_of``. For a holding rule that date is the
valuation date, so a daily valuation is one period. The count is a noise
filter. Template defaults are uncalibrated starting values, not researched
A-share parameters. The screen does not use this gate.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from investment_agent.rules.schema import Condition
from investment_agent.rules.validation import resolve_scalar


def resolve_periods(value: int | str | None, params: Mapping[str, float]) -> int:
    """``None`` and ``1`` mean the comparison counts immediately.

    A non-integer or a value below 1 does not count (the caller treats the
    leaf as not met) so a bad override cannot quietly disable the filter.
    """

    if value is None:
        return 1
    resolved: object = resolve_scalar(value, params) if isinstance(value, str) else value
    if isinstance(resolved, bool) or not isinstance(resolved, int | float):
        return 0
    number = float(resolved)
    if not math.isfinite(number):
        return 0
    nearest = round(number)
    if number < 1 or abs(number - nearest) > 1e-6:
        return 0
    return int(nearest)


@dataclass
class StreakMemory:
    """Counts entering and leaving one ``as_of`` for one zone."""

    as_of: date | None = None
    before: dict[str, int] = field(default_factory=dict)
    after: dict[str, int] = field(default_factory=dict)

    def base_counts(self, as_of: date) -> dict[str, int]:
        if self.as_of == as_of:
            return dict(self.before)
        if self.as_of is None or as_of > self.as_of:
            return dict(self.after)
        return {}

    def finish(
        self, as_of: date, before: Mapping[str, int], after: Mapping[str, int]
    ) -> StreakMemory:
        if self.as_of is not None and as_of < self.as_of:
            return self
        return StreakMemory(as_of=as_of, before=dict(before), after=dict(after))

    def to_json(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of.isoformat() if self.as_of is not None else None,
            "before": dict(self.before),
            "after": dict(self.after),
        }

    @classmethod
    def from_json(cls, raw: object) -> StreakMemory | None:
        if not isinstance(raw, Mapping):
            return None
        text = raw.get("as_of")
        parsed = date.fromisoformat(text) if isinstance(text, str) and text else None
        return cls(
            as_of=parsed,
            before=_counts(raw.get("before")),
            after=_counts(raw.get("after")),
        )


def _counts(raw: object) -> dict[str, int]:
    if not isinstance(raw, Mapping):
        return {}
    counts: dict[str, int] = {}
    for key, value in raw.items():
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            continue
        counts[str(key)] = value
    return counts


@dataclass
class StreakBook:
    """Mutable gate for one zone on one ``as_of``."""

    prior: StreakMemory
    as_of: date
    params: Mapping[str, float]
    before: dict[str, int] = field(init=False)
    counts: dict[str, int] = field(init=False)
    touched: bool = False

    def __post_init__(self) -> None:
        self.before = self.prior.base_counts(self.as_of)
        self.counts = dict(self.before)

    def gate(
        self, path: str, condition: Condition, result: bool | None
    ) -> tuple[bool | None, int | None, int | None]:
        """Return ``(effective, streak, required)``.

        ``streak`` / ``required`` are ``None`` when the condition has no
        multi-period gate. ``result is None`` (not applicable) neither
        increments nor resets.
        """

        required = resolve_periods(condition.min_periods, self.params)
        if required <= 1 or result is None:
            if required <= 0 and result is True:
                return False, None, None
            return result, None, None
        self.touched = True
        count = self.before.get(path, 0) + 1 if result else 0
        self.counts[path] = count
        return count >= required, count, required

    def snapshot(self) -> dict[str, Any] | None:
        if not self.touched:
            return None
        return self.prior.finish(self.as_of, self.before, self.counts).to_json()


class StreakSession:
    """One evaluation's books, keyed by zone name."""

    def __init__(
        self, prior: Mapping[str, StreakMemory], as_of: date, params: Mapping[str, float]
    ) -> None:
        self.prior = prior
        self.as_of = as_of
        self.params = params
        self.books: dict[str, StreakBook] = {}

    def book_for(self, zone: str) -> StreakBook:
        book = self.books.get(zone)
        if book is None:
            book = StreakBook(self.prior.get(zone, StreakMemory()), self.as_of, self.params)
            self.books[zone] = book
        return book

    def snapshots(self) -> dict[str, dict[str, Any]]:
        found: dict[str, dict[str, Any]] = {}
        for zone, book in self.books.items():
            snap = book.snapshot()
            if snap is not None:
                found[zone] = snap
        return found


__all__ = [
    "StreakBook",
    "StreakMemory",
    "StreakSession",
    "resolve_periods",
]
