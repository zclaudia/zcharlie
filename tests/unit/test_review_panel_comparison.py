"""Deterministic round comparison (multi-model review design §6, algorithm v1)."""

from __future__ import annotations

import json
from collections.abc import Sequence

import pytest

from investment_agent.review_panel.comparison import (
    ALGORITHM_VERSION,
    ComparisonResult,
    DimensionCell,
    DivergenceLevel,
    SeatInput,
    compare_round,
)
from investment_agent.review_panel.contracts import (
    Assumption,
    CompanyStance,
    Confidence,
    Dimension,
    Direction,
    Falsifier,
    SeatOpinion,
    TradeView,
)


def _opinion(
    stance: CompanyStance,
    *,
    trade_view: TradeView = TradeView.HOLD_WATCH,
    assumptions: Sequence[Assumption] = (),
    falsifiers: Sequence[Falsifier] = (),
) -> SeatOpinion:
    return SeatOpinion(
        summary="一段话结论。",
        company_stance=stance,
        confidence=Confidence.MEDIUM,
        trade_view=trade_view,
        assumptions=list(assumptions),
        falsifiers=list(falsifiers),
    )


def _seat(
    seat_id: str,
    *,
    stance: CompanyStance = CompanyStance.NEUTRAL,
    trade_view: TradeView = TradeView.HOLD_WATCH,
    assumptions: Sequence[Assumption] = (),
    falsifiers: Sequence[Falsifier] = (),
    evidence: Sequence[str] = (),
    failed: bool = False,
) -> SeatInput:
    return SeatInput(
        seat_id=seat_id,
        lens_id="value",
        model_ref="provider/model",
        opinion=(
            None
            if failed
            else _opinion(
                stance,
                trade_view=trade_view,
                assumptions=assumptions,
                falsifiers=falsifiers,
            )
        ),
        evidence_refs=frozenset(evidence),
    )


def _assumption(dimension: Dimension, direction: Direction, text: str = "假设") -> Assumption:
    return Assumption(dimension=dimension, direction=direction, text=text)


def test_distributions_matrix_and_high_divergence() -> None:
    result = compare_round(
        [
            _seat(
                "value",
                stance=CompanyStance.POSITIVE,
                assumptions=[_assumption(Dimension.VALUATION, Direction.POSITIVE)],
            ),
            _seat(
                "bear",
                stance=CompanyStance.NEGATIVE,
                assumptions=[_assumption(Dimension.VALUATION, Direction.NEGATIVE)],
            ),
            _seat(
                "risk",
                stance=CompanyStance.NEUTRAL,
                assumptions=[_assumption(Dimension.MARGIN, Direction.NEGATIVE)],
            ),
        ]
    )

    assert result.algorithm_version == ALGORITHM_VERSION == "v1"
    assert result.stance_distribution == {
        CompanyStance.POSITIVE: 1,
        CompanyStance.NEUTRAL: 1,
        CompanyStance.NEGATIVE: 1,
    }
    assert result.trade_view_distribution == {TradeView.HOLD_WATCH: 3}
    assert list(result.dimension_matrix) == [Dimension.MARGIN, Dimension.VALUATION]
    assert result.dimension_matrix[Dimension.VALUATION] == {
        "value": DimensionCell.POSITIVE,
        "bear": DimensionCell.NEGATIVE,
        "risk": DimensionCell.ABSENT,
    }
    assert result.divergent_dimensions == [Dimension.VALUATION]
    assert result.stance_range == 2
    assert result.divergence_score == 2 * 2 + 1
    assert result.divergence_level is DivergenceLevel.HIGH
    assert result.notify_reasons == ["high_divergence"]
    assert result.failed_seats == []


def test_a_single_seat_mixed_cell_is_not_divergence() -> None:
    result = compare_round(
        [
            _seat(
                "only",
                assumptions=[
                    _assumption(Dimension.VALUATION, Direction.POSITIVE),
                    _assumption(Dimension.VALUATION, Direction.NEGATIVE),
                ],
            ),
            _seat("other"),
        ]
    )

    assert result.dimension_matrix[Dimension.VALUATION]["only"] is DimensionCell.MIXED
    assert Dimension.VALUATION not in result.divergent_dimensions
    assert result.divergent_dimensions == []
    assert result.divergence_score == 0
    assert result.divergence_level is DivergenceLevel.LOW


@pytest.mark.parametrize(
    ("stances", "expected_range", "expected_level"),
    [
        (
            [CompanyStance.POSITIVE, CompanyStance.INSUFFICIENT],
            0,
            DivergenceLevel.LOW,
        ),
        ([CompanyStance.POSITIVE], 0, DivergenceLevel.LOW),
        (
            [CompanyStance.POSITIVE, CompanyStance.NEUTRAL],
            1,
            DivergenceLevel.MEDIUM,
        ),
        (
            [CompanyStance.POSITIVE, CompanyStance.NEGATIVE],
            2,
            DivergenceLevel.HIGH,
        ),
    ],
)
def test_stance_range_ignores_insufficient_and_needs_two_scored_seats(
    stances: list[CompanyStance], expected_range: int, expected_level: DivergenceLevel
) -> None:
    result = compare_round(
        [_seat(f"seat-{index}", stance=stance) for index, stance in enumerate(stances)]
    )

    assert result.stance_range == expected_range
    assert result.divergence_level is expected_level


def test_failed_seats_are_listed_but_excluded_from_stats() -> None:
    result = compare_round(
        [
            _seat("value", stance=CompanyStance.POSITIVE, evidence=["x"]),
            _seat("bear", failed=True, evidence=["y"]),
        ]
    )

    assert result.failed_seats == ["bear"]
    assert result.stance_distribution == {CompanyStance.POSITIVE: 1}
    assert list(result.dimension_matrix) == [] or all(
        "bear" not in cells for cells in result.dimension_matrix.values()
    )
    assert result.stance_range == 0
    assert result.evidence_overlap == []


def _jaccard(left: Sequence[str], right: Sequence[str]) -> float | None:
    result = compare_round(
        [
            _seat("a", evidence=left),
            _seat("b", evidence=right),
        ]
    )
    [pair] = result.evidence_overlap
    assert pair.seat_a == "a" and pair.seat_b == "b"
    return pair.jaccard


def test_evidence_jaccard_none_when_both_empty() -> None:
    assert _jaccard([], []) is None
    assert _jaccard(["a"], []) == 0.0
    assert _jaccard(["a", "b"], ["b", "c"]) == pytest.approx(1 / 3)


@pytest.mark.parametrize(
    ("left", "right", "expected_differ"),
    [
        (["a", "b"], ["a"], False),  # 0.5: shared materials
        (["a", "b", "c", "d", "e", "f", "g", "h"], ["a"], True),  # 0.125
        ([], [], True),  # None: no materials read at all
    ],
)
def test_materials_differ_for_a_divergent_dimension(
    left: list[str], right: list[str], expected_differ: bool
) -> None:
    result = compare_round(
        [
            _seat(
                "value",
                stance=CompanyStance.POSITIVE,
                assumptions=[_assumption(Dimension.VALUATION, Direction.POSITIVE)],
                evidence=left,
            ),
            _seat(
                "bear",
                stance=CompanyStance.NEGATIVE,
                assumptions=[_assumption(Dimension.VALUATION, Direction.NEGATIVE)],
                evidence=right,
            ),
        ]
    )

    assert result.divergent_dimensions == [Dimension.VALUATION]
    assert result.divergent_materials == {Dimension.VALUATION: expected_differ}


def test_falsifiers_are_grouped_by_dimension_and_deduplicated() -> None:
    result = compare_round(
        [
            _seat(
                "value",
                falsifiers=[
                    Falsifier(dimension=Dimension.VALUATION, text="营收断崖"),
                    Falsifier(dimension=Dimension.MARGIN, text="毛利下滑"),
                ],
            ),
            _seat(
                "bear",
                falsifiers=[
                    Falsifier(dimension=Dimension.VALUATION, text="营收断崖"),
                    Falsifier(dimension=Dimension.VALUATION, text="客户流失"),
                ],
            ),
        ]
    )

    assert [group.dimension for group in result.falsifiers_by_dimension] == [
        Dimension.MARGIN,
        Dimension.VALUATION,
    ]
    valuation = result.falsifiers_by_dimension[1]
    assert [(item.seat_id, item.text) for item in valuation.items] == [
        ("value", "营收断崖"),
        ("bear", "客户流失"),
    ]


def test_stance_flip_vs_previous_round_and_notify_reason() -> None:
    previous = [
        _seat(
            "bear",
            stance=CompanyStance.POSITIVE,
            trade_view=TradeView.WATCH_BUY,
        ),
        _seat("value", stance=CompanyStance.NEUTRAL),
    ]
    current = [
        _seat(
            "bear",
            stance=CompanyStance.NEGATIVE,
            trade_view=TradeView.AVOID,
        ),
        _seat("value", stance=CompanyStance.NEUTRAL),
    ]

    result = compare_round(current, previous)

    changes = {change.seat_id: change for change in result.seat_changes}
    assert changes["bear"].previous_stance is CompanyStance.POSITIVE
    assert changes["bear"].current_stance is CompanyStance.NEGATIVE
    assert changes["bear"].previous_trade_view is TradeView.WATCH_BUY
    assert changes["bear"].current_trade_view is TradeView.AVOID
    assert changes["bear"].stance_flipped is True
    assert changes["value"].stance_flipped is False
    assert result.notify_reasons == ["stance_flip"]


def test_high_divergence_and_stance_flip_notify_together() -> None:
    previous = [_seat("bear", stance=CompanyStance.POSITIVE)]
    current = [
        _seat(
            "value",
            stance=CompanyStance.POSITIVE,
            assumptions=[_assumption(Dimension.VALUATION, Direction.POSITIVE)],
        ),
        _seat(
            "bear",
            stance=CompanyStance.NEGATIVE,
            assumptions=[_assumption(Dimension.VALUATION, Direction.NEGATIVE)],
        ),
    ]

    result = compare_round(current, previous)

    assert result.divergence_level is DivergenceLevel.HIGH
    assert result.notify_reasons == ["high_divergence", "stance_flip"]


def test_comparison_is_deterministic_and_json_serialisable() -> None:
    seats = [
        _seat(
            "value",
            stance=CompanyStance.POSITIVE,
            assumptions=[_assumption(Dimension.VALUATION, Direction.POSITIVE)],
            evidence=["a", "b"],
        ),
        _seat(
            "bear",
            stance=CompanyStance.NEGATIVE,
            assumptions=[_assumption(Dimension.VALUATION, Direction.NEGATIVE)],
            evidence=["b"],
        ),
        _seat("growth", stance=CompanyStance.NEUTRAL),
    ]

    first = compare_round(seats)
    second = compare_round(seats)

    assert first == second
    payload = first.model_dump(mode="json")
    assert json.loads(json.dumps(payload)) == payload
    assert payload["divergent_dimensions"] == ["valuation"]
    assert list(first.dimension_matrix) == [Dimension.VALUATION]
    assert list(first.dimension_matrix[Dimension.VALUATION]) == [
        "value",
        "bear",
        "growth",
    ]


def test_duplicate_seat_ids_are_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate seat_id"):
        compare_round([_seat("same"), _seat("same")])


def test_result_model_is_a_comparison_result() -> None:
    assert isinstance(compare_round([]), ComparisonResult)
