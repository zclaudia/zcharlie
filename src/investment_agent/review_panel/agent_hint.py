"""Compact summary of the latest completed review round for the Agent (design §10.3).

The investor Agent may read a stock's most recent completed multi-perspective
round while doing a ``review``. This payload is an *external lead*, never
evidence: every item is another model's opinion, useful only for finding
counter-arguments and open questions. It deliberately contains no evidence
refs, no source values and no citable identifiers, so nothing here can be
attached to ``evidence_refs``, claim support or publication inputs.

Callers render :data:`REVIEW_LEAD_SECTION_TITLE` and
:data:`REVIEW_LEAD_INSTRUCTION` around the JSON; the explicit markers
``kind`` / ``not_evidence`` travel with the payload itself as well.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.review_panel.comparison import ComparisonResult
from investment_agent.review_panel.contracts import (
    COMPANY_STANCE_LABELS,
    CONFIDENCE_LABELS,
    DIMENSION_LABELS,
    TRADE_VIEW_LABELS,
    SeatOpinion,
)
from investment_agent.storage.models.review_panel import ReviewRoundRecord
from investment_agent.storage.repositories.review_panel import ReviewPanelRepository

REVIEW_LEAD_KIND = "external_review_lead"
REVIEW_LEAD_SECTION_TITLE = "外部评审线索（非证据）"
REVIEW_LEAD_INSTRUCTION = (
    "这些观点来自其他模型的评审，只能用来发现反证和待核问题；"
    "不能作为证据引用，不能计入主张的支持材料，不改变发布校验。"
)

MAX_FALSIFIERS_PER_SEAT = 3
MAX_OPEN_QUESTIONS_PER_SEAT = 3
MAX_TEXT_CHARS = 200


def _iso(value: datetime | None) -> str | None:
    """UTC ISO text; SQLite drops the offset on read, stored values are UTC."""

    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _clip(text: str, limit: int = MAX_TEXT_CHARS) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _lens_names(round_row: ReviewRoundRecord) -> dict[str, str]:
    """Seat id -> frozen lens display name (``panel_snapshot`` is authoritative)."""

    snapshot = round_row.panel_snapshot
    seats = snapshot.get("seats") if isinstance(snapshot, dict) else None
    if not isinstance(seats, list):
        return {}
    names: dict[str, str] = {}
    for entry in seats:
        if not isinstance(entry, dict):
            continue
        seat_id = entry.get("seat_id")
        name = entry.get("lens_name")
        if isinstance(seat_id, str) and isinstance(name, str) and name.strip():
            names[seat_id] = name.strip()
    return names


def _seat_summary(
    seat_id: str, lens: str, opinion: SeatOpinion
) -> dict[str, Any]:
    return {
        "seat_id": seat_id,
        "lens": lens,
        "stance": COMPANY_STANCE_LABELS.get(
            opinion.company_stance, opinion.company_stance.value
        ),
        "trade_view": TRADE_VIEW_LABELS.get(
            opinion.trade_view, opinion.trade_view.value
        ),
        "confidence": CONFIDENCE_LABELS.get(
            opinion.confidence, opinion.confidence.value
        ),
        "falsifiers": [
            _clip(falsifier.text)
            for falsifier in opinion.falsifiers[:MAX_FALSIFIERS_PER_SEAT]
        ],
        "open_questions": [
            _clip(question)
            for question in opinion.open_questions[:MAX_OPEN_QUESTIONS_PER_SEAT]
        ],
    }


def _stance_distribution(comparison: ComparisonResult) -> dict[str, int]:
    counts: dict[str, int] = {}
    for stance, count in comparison.stance_distribution.items():
        if stance in COMPANY_STANCE_LABELS:
            counts[COMPANY_STANCE_LABELS[stance]] = int(count)
    return counts


def _falsifiers_by_dimension(comparison: ComparisonResult) -> list[dict[str, Any]]:
    return [
        {
            "dimension": DIMENSION_LABELS[group.dimension],
            "items": [
                {"seat_id": item.seat_id, "text": _clip(item.text)}
                for item in group.items
            ],
        }
        for group in comparison.falsifiers_by_dimension
    ]


async def review_hint(
    session: AsyncSession, listing_id: int
) -> dict[str, Any] | None:
    """The latest *completed* round as a compact, JSON-safe lead summary.

    ``None`` when the listing has no completed round, or when its stored
    comparison is missing/unreadable. In-progress and failed rounds never
    qualify.
    """

    repository = ReviewPanelRepository(session)
    round_row = await repository.latest_round(listing_id, completed_only=True)
    if round_row is None:
        return None
    detail = await repository.get_round(round_row.id)
    if detail is None or detail.comparison is None:
        return None
    try:
        comparison = ComparisonResult.model_validate(detail.comparison.result_json)
    except ValueError:
        return None

    lens_names = _lens_names(round_row)
    seats: list[dict[str, Any]] = []
    for seat in detail.seats:
        if seat.opinion_json is None:
            continue
        try:
            opinion = SeatOpinion.model_validate(seat.opinion_json)
        except ValueError:
            continue
        seats.append(
            _seat_summary(
                seat.seat_id,
                lens_names.get(seat.seat_id, seat.lens_ref),
                opinion,
            )
        )

    return {
        "kind": REVIEW_LEAD_KIND,
        "not_evidence": True,
        "listing_id": listing_id,
        "round_id": round_row.id,
        "as_of": round_row.as_of.isoformat(),
        "completed_at": _iso(round_row.completed_at),
        "evidence_cutoff": _iso(round_row.evidence_cutoff),
        "seat_count": len(detail.seats),
        "seats": seats,
        "stance_distribution": _stance_distribution(comparison),
        "divergence_level": comparison.divergence_level.value,
        "divergent_dimensions": [
            DIMENSION_LABELS[dimension]
            for dimension in comparison.divergent_dimensions
        ],
        "falsifiers_by_dimension": _falsifiers_by_dimension(comparison),
    }


__all__ = [
    "MAX_FALSIFIERS_PER_SEAT",
    "MAX_OPEN_QUESTIONS_PER_SEAT",
    "MAX_TEXT_CHARS",
    "REVIEW_LEAD_INSTRUCTION",
    "REVIEW_LEAD_KIND",
    "REVIEW_LEAD_SECTION_TITLE",
    "review_hint",
]
