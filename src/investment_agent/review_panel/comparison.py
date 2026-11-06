"""Deterministic comparison of one review round's seats (design §6).

Pure functions over the structured :class:`SeatOpinion`: no DB, no model
calls, no semantic dedup. Everything is ordered (seat order as given,
dimensions in :class:`Dimension` enum order) so the same inputs always produce
the same JSON.

Versioned rule (``ALGORITHM_VERSION = "v1"``). A *divergent dimension* is one
where two different seats take opposite sides, using only ``assumptions``
(falsifiers are reported separately): one seat is ``positive`` or ``mixed``
and another is ``negative`` or ``mixed``. A single seat's ``mixed`` cell is
therefore never a divergence on its own. ``stance_range`` maps positive=+1,
neutral=0, negative=-1, ignores ``insufficient``, and is 0 with fewer than two
scored seats. ``divergence_score = stance_range * 2 + len(divergent_dimensions)``;
``divergence_level`` is ``high`` when ``stance_range == 2`` or at least three
dimensions diverge, ``medium`` when the score is at least 2, else ``low``.
Changing any of these rules must bump ``ALGORITHM_VERSION`` so a stored
``review_comparison`` row keeps the algorithm that produced it.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from investment_agent.review_panel.contracts import (
    CompanyStance,
    Dimension,
    Direction,
    SeatOpinion,
    TradeView,
)

ALGORITHM_VERSION = "v1"

_STANCE_SCORE: dict[CompanyStance, int] = {
    CompanyStance.POSITIVE: 1,
    CompanyStance.NEUTRAL: 0,
    CompanyStance.NEGATIVE: -1,
}
# Cell states are lens-neutral: only the assumption direction matters.
_POSITIVE_CELLS = frozenset({"positive", "mixed"})
_NEGATIVE_CELLS = frozenset({"negative", "mixed"})
_FLIP_STANCES = frozenset({CompanyStance.POSITIVE, CompanyStance.NEGATIVE})
_MATERIALS_OVERLAP_THRESHOLD = 0.3


@dataclass(frozen=True)
class SeatInput:
    """One seat's inputs to the comparison.

    ``opinion is None`` marks a failed seat: it is listed in
    ``failed_seats`` and excluded from every statistic. ``evidence_refs`` is
    the set the server recorded the seat actually read (design §4.3), not a
    model-reported list.
    """

    seat_id: str
    lens_id: str
    model_ref: str
    opinion: SeatOpinion | None
    evidence_refs: frozenset[str] = frozenset()


class DimensionCell(StrEnum):
    """One matrix cell: the seat's assumption direction(s) on a dimension."""

    POSITIVE = "positive"
    NEGATIVE = "negative"
    MIXED = "mixed"
    """The seat has both a positive and a negative assumption."""
    ABSENT = "absent"


class DivergenceLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EvidenceOverlap(_Record):
    """Jaccard overlap of one unordered seat pair's read evidence."""

    seat_a: str
    seat_b: str
    jaccard: float | None
    """``None`` when both seats read nothing: "no materials read"."""


class SeatChange(_Record):
    """One seat's stance / trade view relative to the previous round."""

    seat_id: str
    previous_stance: CompanyStance | None = None
    current_stance: CompanyStance | None = None
    previous_trade_view: TradeView | None = None
    current_trade_view: TradeView | None = None
    stance_flipped: bool = False
    """Both stances are positive/negative and differ."""


class FalsifierEntry(_Record):
    seat_id: str
    text: str


class FalsifierGroup(_Record):
    dimension: Dimension
    items: list[FalsifierEntry]


class ComparisonResult(_Record):
    """The stored comparison (``review_comparison.result_json``)."""

    algorithm_version: str = ALGORITHM_VERSION
    stance_distribution: dict[CompanyStance, int]
    trade_view_distribution: dict[TradeView, int]
    dimension_matrix: dict[Dimension, dict[str, DimensionCell]]
    """Dimension -> seat id -> cell; only dimensions any seat mentions."""
    divergent_dimensions: list[Dimension]
    divergent_materials: dict[Dimension, bool]
    """Per divergent dimension: did the opposing seats read different materials?"""
    stance_range: int
    divergence_score: int
    divergence_level: DivergenceLevel
    evidence_overlap: list[EvidenceOverlap]
    falsifiers_by_dimension: list[FalsifierGroup]
    seat_changes: list[SeatChange]
    notify_reasons: list[str]
    """``high_divergence`` and/or ``stance_flip`` (design §10.5)."""
    failed_seats: list[str]


def _counts[E: StrEnum](values: Sequence[E], members: Iterable[E]) -> dict[E, int]:
    counts: dict[E, int] = {}
    for member in members:
        count = sum(1 for value in values if value == member)
        if count:
            counts[member] = count
    return counts


def _opinion(seat: SeatInput) -> SeatOpinion:
    assert seat.opinion is not None, "scorable seats only"
    return seat.opinion


def _dimension_cell(opinion: SeatOpinion, dimension: Dimension) -> DimensionCell:
    directions = {
        assumption.direction
        for assumption in opinion.assumptions
        if assumption.dimension == dimension
    }
    if not directions:
        return DimensionCell.ABSENT
    if Direction.POSITIVE in directions and Direction.NEGATIVE in directions:
        return DimensionCell.MIXED
    if Direction.POSITIVE in directions:
        return DimensionCell.POSITIVE
    return DimensionCell.NEGATIVE


def _matrix(seats: Sequence[SeatInput]) -> dict[Dimension, dict[str, DimensionCell]]:
    opinions = [(seat.seat_id, _opinion(seat)) for seat in seats]
    mentioned = {
        assumption.dimension for _seat_id, opinion in opinions for assumption in opinion.assumptions
    }
    matrix: dict[Dimension, dict[str, DimensionCell]] = {}
    for dimension in Dimension:
        if dimension not in mentioned:
            continue
        matrix[dimension] = {
            seat_id: _dimension_cell(opinion, dimension) for seat_id, opinion in opinions
        }
    return matrix


def _opposing(left: DimensionCell, right: DimensionCell) -> bool:
    return (left in _POSITIVE_CELLS and right in _NEGATIVE_CELLS) or (
        right in _POSITIVE_CELLS and left in _NEGATIVE_CELLS
    )


def _jaccard(left: frozenset[str], right: frozenset[str]) -> float | None:
    if not left and not right:
        return None
    union = left | right
    return len(left & right) / len(union)


def _evidence_by_seat(seats: Sequence[SeatInput]) -> dict[str, frozenset[str]]:
    return {seat.seat_id: seat.evidence_refs for seat in seats}


def _evidence_overlap(seats: Sequence[SeatInput]) -> list[EvidenceOverlap]:
    pairs: list[EvidenceOverlap] = []
    for index, left in enumerate(seats):
        for right in seats[index + 1 :]:
            pairs.append(
                EvidenceOverlap(
                    seat_a=left.seat_id,
                    seat_b=right.seat_id,
                    jaccard=_jaccard(left.evidence_refs, right.evidence_refs),
                )
            )
    return pairs


def _falsifiers(seats: Sequence[SeatInput]) -> list[FalsifierGroup]:
    grouped: dict[Dimension, list[FalsifierEntry]] = {}
    seen: dict[Dimension, set[str]] = {}
    for seat in seats:
        for falsifier in _opinion(seat).falsifiers:
            texts = seen.setdefault(falsifier.dimension, set())
            if falsifier.text in texts:
                continue
            texts.add(falsifier.text)
            grouped.setdefault(falsifier.dimension, []).append(
                FalsifierEntry(seat_id=seat.seat_id, text=falsifier.text)
            )
    return [
        FalsifierGroup(dimension=dimension, items=grouped[dimension])
        for dimension in Dimension
        if dimension in grouped
    ]


def _seat_changes(seats: Sequence[SeatInput], previous: Sequence[SeatInput]) -> list[SeatChange]:
    current_by_id = {seat.seat_id: seat for seat in seats}
    previous_by_id = {seat.seat_id: seat for seat in previous}
    ordered_ids = [seat.seat_id for seat in seats] + [
        seat.seat_id for seat in previous if seat.seat_id not in current_by_id
    ]
    changes: list[SeatChange] = []
    for seat_id in ordered_ids:
        current = current_by_id.get(seat_id)
        before = previous_by_id.get(seat_id)
        before_stance = before.opinion.company_stance if before and before.opinion else None
        current_stance = current.opinion.company_stance if current and current.opinion else None
        changes.append(
            SeatChange(
                seat_id=seat_id,
                previous_stance=before_stance,
                current_stance=current_stance,
                previous_trade_view=(
                    before.opinion.trade_view if before and before.opinion else None
                ),
                current_trade_view=(
                    current.opinion.trade_view if current and current.opinion else None
                ),
                stance_flipped=(
                    before_stance in _FLIP_STANCES
                    and current_stance in _FLIP_STANCES
                    and before_stance != current_stance
                ),
            )
        )
    return changes


def _validate_seats(seats: Sequence[SeatInput], label: str) -> None:
    seen: set[str] = set()
    for seat in seats:
        if seat.seat_id in seen:
            raise ValueError(f"{label}: duplicate seat_id {seat.seat_id!r}")
        seen.add(seat.seat_id)


def compare_round(
    seats: Sequence[SeatInput], previous: Sequence[SeatInput] | None = None
) -> ComparisonResult:
    """Compare one round's seats; see the module docstring for the rule.

    Failed seats (``opinion is None``) are excluded from every statistic and
    listed in ``failed_seats``. Passing ``previous`` (the same panel's prior
    round) adds per-seat stance / trade-view changes.
    """

    _validate_seats(seats, "seats")
    if previous is not None:
        _validate_seats(previous, "previous")

    scorable = [seat for seat in seats if seat.opinion is not None]
    failed_seats = [seat.seat_id for seat in seats if seat.opinion is None]

    stances = [_opinion(seat).company_stance for seat in scorable]
    trade_views = [_opinion(seat).trade_view for seat in scorable]

    matrix = _matrix(scorable)
    evidence = _evidence_by_seat(scorable)

    divergent: list[Dimension] = []
    materials_differ: dict[Dimension, bool] = {}
    for dimension, cells in matrix.items():
        opposing_pairs = [
            (left_id, right_id)
            for index, left_id in enumerate(cells)
            for right_id in list(cells)[index + 1 :]
            if _opposing(cells[left_id], cells[right_id])
        ]
        if not opposing_pairs:
            continue
        divergent.append(dimension)
        overlaps = [
            _jaccard(evidence[left_id], evidence[right_id]) for left_id, right_id in opposing_pairs
        ]
        materials_differ[dimension] = any(
            overlap is None or overlap < _MATERIALS_OVERLAP_THRESHOLD for overlap in overlaps
        )

    scores = [_STANCE_SCORE[stance] for stance in stances if stance in _STANCE_SCORE]
    stance_range = max(scores) - min(scores) if len(scores) >= 2 else 0
    divergence_score = stance_range * 2 + len(divergent)
    if stance_range == 2 or len(divergent) >= 3:
        divergence_level = DivergenceLevel.HIGH
    elif divergence_score >= 2:
        divergence_level = DivergenceLevel.MEDIUM
    else:
        divergence_level = DivergenceLevel.LOW

    changes = _seat_changes(seats, previous) if previous is not None else []
    notify_reasons: list[str] = []
    if divergence_level is DivergenceLevel.HIGH:
        notify_reasons.append("high_divergence")
    if any(change.stance_flipped for change in changes):
        notify_reasons.append("stance_flip")

    return ComparisonResult(
        stance_distribution=_counts(stances, tuple(CompanyStance)),
        trade_view_distribution=_counts(trade_views, tuple(TradeView)),
        dimension_matrix=matrix,
        divergent_dimensions=divergent,
        divergent_materials=materials_differ,
        stance_range=stance_range,
        divergence_score=divergence_score,
        divergence_level=divergence_level,
        evidence_overlap=_evidence_overlap(scorable),
        falsifiers_by_dimension=_falsifiers(scorable),
        seat_changes=changes,
        notify_reasons=notify_reasons,
        failed_seats=failed_seats,
    )


__all__ = [
    "ALGORITHM_VERSION",
    "ComparisonResult",
    "DimensionCell",
    "DivergenceLevel",
    "EvidenceOverlap",
    "FalsifierEntry",
    "FalsifierGroup",
    "SeatChange",
    "SeatInput",
    "compare_round",
]
