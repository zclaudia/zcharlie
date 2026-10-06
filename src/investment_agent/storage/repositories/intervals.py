"""Rebuilding ``[valid_from, valid_to)`` interval history from ordered snapshots.

Live syncs build interval tables forward in time and reject older snapshots
(:class:`~investment_agent.storage.repositories.pit.OutOfOrderSnapshotError`).
The historical backfill (P1-A4) instead *recomputes* a window:

1. the stored rows are read back as a state timeline (``date -> state``);
2. inside the window ``[first snapshot, last snapshot]`` the snapshots are
   authoritative; before it and after it the stored timeline is kept (state
   changes recorded by live syncs after the window survive);
3. the merged timeline is turned back into intervals and diffed against the
   stored rows (:func:`plan_rows`): identical rows are left untouched,
   overlapping rows of the same value are reused (their ``source`` /
   ``captured_at`` provenance is kept, only their bounds move), the rest are
   deleted or inserted. Re-running a rebuild with the same snapshots
   therefore changes nothing.

States are compared with ``==``; ``None`` means "absent" (not a member / no
classification).
"""

from __future__ import annotations

from collections.abc import Callable, Hashable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class Span[V]:
    value: V
    valid_from: date
    valid_to: date | None

    def contains(self, day: date) -> bool:
        return self.valid_from <= day and (self.valid_to is None or day < self.valid_to)

    def overlaps(self, other: Span[V]) -> bool:
        end = self.valid_to or date.max
        other_end = other.valid_to or date.max
        return self.valid_from < other_end and other.valid_from < end


def state_at[V](spans: Iterable[Span[V]], day: date) -> V | None:
    for span in spans:
        if span.contains(day):
            return span.value
    return None


def boundaries[V](spans: Iterable[Span[V]]) -> list[date]:
    days: set[date] = set()
    for span in spans:
        days.add(span.valid_from)
        if span.valid_to is not None:
            days.add(span.valid_to)
    return sorted(days)


def compress[S](points: Iterable[tuple[date, S]]) -> list[tuple[date, S]]:
    """Drop points that repeat the previous state (input sorted by date)."""

    result: list[tuple[date, S]] = []
    for day, state in points:
        if result and result[-1][1] == state:
            continue
        result.append((day, state))
    return result


def merge_timeline[S](
    stored: Callable[[date], S],
    stored_days: Sequence[date],
    points: Sequence[tuple[date, S]],
) -> list[tuple[date, S]]:
    """Stored timeline outside ``[points[0], points[-1]]``, ``points`` inside.

    ``stored(day)`` is the stored state on ``day`` and ``stored_days`` the
    dates where it may change. ``points`` must be strictly increasing by date.
    The last point's state lasts until the next stored change after it.
    """

    if not points:
        raise ValueError("a rebuild needs at least one snapshot")
    days = [day for day, _ in points]
    if any(later <= earlier for earlier, later in zip(days, days[1:], strict=False)):
        raise ValueError("snapshots must be strictly ordered by effective date")
    first, last = days[0], days[-1]
    merged = [(day, stored(day)) for day in stored_days if day < first]
    merged.extend(points)
    merged.extend((day, stored(day)) for day in stored_days if day > last)
    return compress(merged)


def scalar_spans[V: Hashable](timeline: Sequence[tuple[date, V | None]]) -> list[Span[V]]:
    """Intervals of consecutive equal non-``None`` states (last one open)."""

    spans: list[Span[V]] = []
    for index, (day, state) in enumerate(timeline):
        if state is None:
            continue
        end = timeline[index + 1][0] if index + 1 < len(timeline) else None
        spans.append(Span(state, day, end))
    return spans


def set_spans[K](timeline: Sequence[tuple[date, frozenset[K]]]) -> dict[K, list[Span[bool]]]:
    """Per-key membership intervals of a set-valued timeline."""

    result: dict[K, list[Span[bool]]] = {}
    open_from: dict[K, date] = {}
    previous: frozenset[K] = frozenset()
    for day, members in timeline:
        for key in previous - members:
            result.setdefault(key, []).append(Span(True, open_from.pop(key), day))
        for key in members - previous:
            open_from[key] = day
        previous = members
    for key, start in open_from.items():
        result.setdefault(key, []).append(Span(True, start, None))
    return result


@dataclass(frozen=True)
class RowPlan[R, V]:
    """How to turn stored rows into the desired spans of one key."""

    keep: list[R]
    reuse: list[tuple[R, Span[V]]]
    """Stored row -> new bounds (provenance kept)."""
    delete: list[R]
    insert: list[Span[V]]

    @property
    def changed(self) -> bool:
        return bool(self.reuse or self.delete or self.insert)


def plan_rows[R, V](
    rows: Sequence[R], desired: Sequence[Span[V]], span_of: Callable[[R], Span[V]]
) -> RowPlan[R, V]:
    """Diff stored ``rows`` against ``desired`` spans (one key)."""

    remaining = list(desired)
    keep: list[R] = []
    leftovers: list[R] = []
    for row in rows:
        span = span_of(row)
        if span in remaining:
            remaining.remove(span)
            keep.append(row)
        else:
            leftovers.append(row)
    reuse: list[tuple[R, Span[V]]] = []
    delete: list[R] = []
    for row in leftovers:
        span = span_of(row)
        match = next(
            (
                target
                for target in remaining
                if target.value == span.value and target.overlaps(span)
            ),
            None,
        )
        if match is None:
            delete.append(row)
            continue
        remaining.remove(match)
        reuse.append((row, match))
    return RowPlan(keep=keep, reuse=reuse, delete=delete, insert=remaining)


def group_by[K, R](rows: Iterable[R], key: Callable[[R], K]) -> Mapping[K, list[R]]:
    grouped: dict[K, list[R]] = {}
    for row in rows:
        grouped.setdefault(key(row), []).append(row)
    return grouped
