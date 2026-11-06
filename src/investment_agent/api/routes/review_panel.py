"""Multi-perspective review rounds: create, list, detail and panel presets.

The routes mirror the watchlist/rule-template conventions: every endpoint is
authenticated by :func:`verify_api_key_or_session`, errors carry
``{"code", "message"}``, and the response models expose no server-internal
prompt, provider endpoint or credential. A round's frozen ``panel_snapshot``
is deliberately never serialized: it holds the lens prompt text. Only the
lens name / version and the seat's ``provider/model`` reference are returned.

``POST`` is the manual trigger (design §8). A :class:`RoundRejected` is mapped
to 409 for an already-running round, 404 when the listing is unknown or not in
the watchlist, and 422 for every other rejection (feature disabled, quota,
unknown panel/seat/lens, unavailable model, missing snapshot).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.api.auth import get_settings, verify_api_key_or_session
from investment_agent.api.schemas.review_panel import (
    ReviewComparisonOut,
    ReviewComparisonSummaryOut,
    ReviewLensOut,
    ReviewPanelOut,
    ReviewPanelSeatOut,
    ReviewPanelsOut,
    ReviewRoundCreateRequest,
    ReviewRoundOut,
    ReviewRoundSeatOut,
    ReviewRoundSummaryOut,
)
from investment_agent.config import Settings
from investment_agent.domain.time import as_utc
from investment_agent.review_panel.contracts import TERMINAL_SEAT_STATUSES, RoundTrigger
from investment_agent.review_panel.lenses import LensSet, load_review_lenses
from investment_agent.review_panel.panels import parse_review_panels
from investment_agent.review_panel.service import RoundRejected, create_round
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.review_panel import (
    ReviewComparisonRecord,
    ReviewRoundRecord,
    ReviewSeatRunRecord,
)
from investment_agent.storage.repositories.listing import ListingRepository
from investment_agent.storage.repositories.review_panel import (
    ReviewPanelRepository,
    RoundDetail,
)

router = APIRouter(
    tags=["review-panel"],
    dependencies=[Depends(verify_api_key_or_session)],
)

_ROUND_REJECTED_STATUS: dict[str, int] = {
    "active_round": 409,
    "duplicate_trigger": 409,
    "not_on_watchlist": 404,
}
_DEFAULT_LIMIT = 20
_MAX_LIMIT = 100

# Seat errors are written by the review job from safe exception types, but a
# provider SDK can still leak an endpoint or a key-shaped token into a message;
# redact defensively before the value crosses the HTTP boundary.
_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\b(?:api[_-]?key|authorization|bearer|token)\b\s*[:=]?\s*\S+"), "[已隐去]"),
    (re.compile(r"\bsk-[A-Za-z0-9._-]{6,}"), "[已隐去]"),
    (re.compile(r"https?://\S+"), "[已隐去]"),
)
_MAX_ERROR_CHARS = 300


def _rejected(exc: RoundRejected) -> HTTPException:
    return HTTPException(
        status_code=_ROUND_REJECTED_STATUS.get(exc.code, 422),
        detail={"code": exc.code, "message": exc.message},
    )


def _safe_error(value: str | None) -> str | None:
    if not value:
        return None
    text = str(value)[:_MAX_ERROR_CHARS]
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def _seat_lens_names(detail: RoundDetail) -> dict[str, str]:
    """``seat_id -> lens name`` from the round snapshot (never the prompt)."""

    names: dict[str, str] = {}
    seats = detail.round.panel_snapshot.get("seats") if detail.round.panel_snapshot else None
    if isinstance(seats, list):
        for entry in seats:
            if not isinstance(entry, dict):
                continue
            seat_id = entry.get("seat_id")
            name = entry.get("lens_name")
            if isinstance(seat_id, str) and isinstance(name, str):
                names[seat_id] = name
    return names


def _panel_name(round_row: ReviewRoundRecord) -> str | None:
    snapshot = round_row.panel_snapshot
    name = snapshot.get("name") if isinstance(snapshot, dict) else None
    return name if isinstance(name, str) else None


def _round_out(detail: RoundDetail) -> ReviewRoundOut:
    round_row = detail.round
    lens_names = _seat_lens_names(detail)
    seats = [
        ReviewRoundSeatOut(
            seat_id=seat.seat_id,
            lens_id=seat.lens_id,
            lens_ref=seat.lens_ref,
            lens_name=lens_names.get(seat.seat_id),
            model_ref=seat.model_ref,
            status=seat.status,
            error=_safe_error(seat.error),
            opinion=seat.opinion_json,
            annotations=seat.annotations_json,
            evidence_refs=list(seat.evidence_refs or ()),
            usage=seat.usage,
            started_at=as_utc(seat.started_at) if seat.started_at else None,
            finished_at=as_utc(seat.finished_at) if seat.finished_at else None,
        )
        for seat in detail.seats
    ]
    comparison = None
    if detail.comparison is not None:
        comparison = ReviewComparisonOut.model_validate(detail.comparison.result_json)
    return ReviewRoundOut(
        id=round_row.id,
        listing_id=round_row.listing_id,
        panel_id=round_row.panel_id,
        panel_name=_panel_name(round_row),
        trigger=round_row.trigger,
        status=round_row.status,
        as_of=round_row.as_of,
        evidence_cutoff=as_utc(round_row.evidence_cutoff),
        base_pack=round_row.base_pack,
        base_pack_hash=round_row.base_pack_hash,
        created_at=as_utc(round_row.created_at),
        completed_at=(
            as_utc(round_row.completed_at) if round_row.completed_at else None
        ),
        reserved_tokens=round_row.reserved_tokens,
        reserved_requests=round_row.reserved_requests,
        settled_usage=dict(round_row.settled_usage or {}),
        seats=seats,
        comparison=comparison,
    )


def comparison_summary(result: dict[str, Any]) -> ReviewComparisonSummaryOut:
    """Compact summary used by round lists and watchlist badges."""

    divergent = result.get("divergent_dimensions")
    failed = result.get("failed_seats")
    reasons = result.get("notify_reasons")
    return ReviewComparisonSummaryOut(
        algorithm_version=str(result.get("algorithm_version") or ""),
        stance_distribution=_integer_map(result.get("stance_distribution")),
        divergence_level=str(result.get("divergence_level") or "low"),
        divergence_score=int(result.get("divergence_score") or 0),
        divergent_dimension_count=len(divergent) if isinstance(divergent, list) else 0,
        failed_seats=[str(item) for item in failed] if isinstance(failed, list) else [],
        notify_reasons=[str(item) for item in reasons] if isinstance(reasons, list) else [],
    )


def _integer_map(value: object) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    return {
        str(key): int(count)
        for key, count in value.items()
        if isinstance(count, int) and not isinstance(count, bool)
    }


async def _comparisons_by_round(
    session: AsyncSession, round_ids: Sequence[int]
) -> dict[int, dict[str, Any]]:
    """Latest comparison result per round id (one query)."""

    if not round_ids:
        return {}
    rows = (
        await session.execute(
            select(ReviewComparisonRecord)
            .where(ReviewComparisonRecord.round_id.in_(list(round_ids)))
            .order_by(
                ReviewComparisonRecord.created_at.desc(),
                ReviewComparisonRecord.id.desc(),
            )
        )
    ).scalars()
    found: dict[int, dict[str, Any]] = {}
    for row in rows:
        found.setdefault(row.round_id, row.result_json)
    return found


async def _seat_counts(
    session: AsyncSession, round_ids: Sequence[int]
) -> dict[int, tuple[int, int, int]]:
    """``round_id -> (seat_count, terminal_count, failed_count)``."""

    if not round_ids:
        return {}
    rows = await session.execute(
        select(
            ReviewSeatRunRecord.round_id,
            ReviewSeatRunRecord.status,
            func.count(ReviewSeatRunRecord.id),
        )
        .where(ReviewSeatRunRecord.round_id.in_(list(round_ids)))
        .group_by(ReviewSeatRunRecord.round_id, ReviewSeatRunRecord.status)
    )
    terminal = {status.value for status in TERMINAL_SEAT_STATUSES}
    counts: dict[int, list[int]] = {}
    for round_id, status, count in rows:
        entry = counts.setdefault(round_id, [0, 0, 0])
        seat_count = int(count)
        entry[0] += seat_count
        if status in terminal:
            entry[1] += seat_count
        if status == "failed":
            entry[2] += seat_count
    return {round_id: (values[0], values[1], values[2]) for round_id, values in counts.items()}


@router.post(
    "/watchlist/{listing_id}/review-rounds",
    response_model=ReviewRoundOut,
    status_code=201,
)
async def create_review_round(
    listing_id: int,
    body: ReviewRoundCreateRequest,
    settings: Annotated[Settings, Depends(get_settings)],
) -> ReviewRoundOut:
    """Create one manual review round for a watchlist stock."""

    async with session_scope() as session:
        try:
            round_row = await create_round(
                session,
                settings,
                listing_id=listing_id,
                trigger=RoundTrigger.MANUAL,
                panel_id=body.panel_id,
                seat_ids=body.seat_ids,
            )
        except RoundRejected as exc:
            raise _rejected(exc) from exc
        detail = await ReviewPanelRepository(session).get_round(round_row.id)
        if detail is None:  # pragma: no cover - the row was just created
            raise HTTPException(status_code=500, detail={"code": "round_missing"})
        return _round_out(detail)


@router.get(
    "/watchlist/{listing_id}/review-rounds",
    response_model=list[ReviewRoundSummaryOut],
)
async def list_review_rounds(
    listing_id: int,
    limit: Annotated[int, Query(ge=1, le=_MAX_LIMIT)] = _DEFAULT_LIMIT,
) -> list[ReviewRoundSummaryOut]:
    """Round summaries (newest first) with their comparison summary."""

    async with session_scope() as session:
        if await ListingRepository(session).get(listing_id) is None:
            raise HTTPException(status_code=404, detail={"code": "not_found"})
        rounds = await ReviewPanelRepository(session).list_rounds(listing_id, limit)
        round_ids = [row.id for row in rounds]
        comparisons = await _comparisons_by_round(session, round_ids)
        counts = await _seat_counts(session, round_ids)
        result: list[ReviewRoundSummaryOut] = []
        for row in rounds:
            seat_count, terminal_count, failed_count = counts.get(row.id, (0, 0, 0))
            summary = None
            comparison_result = comparisons.get(row.id)
            if comparison_result is not None:
                summary = comparison_summary(comparison_result)
            result.append(
                ReviewRoundSummaryOut(
                    id=row.id,
                    listing_id=row.listing_id,
                    panel_id=row.panel_id,
                    panel_name=_panel_name(row),
                    trigger=row.trigger,
                    status=row.status,
                    as_of=row.as_of,
                    evidence_cutoff=as_utc(row.evidence_cutoff),
                    created_at=as_utc(row.created_at),
                    completed_at=as_utc(row.completed_at) if row.completed_at else None,
                    seat_count=seat_count,
                    seat_terminal_count=terminal_count,
                    seat_failed_count=failed_count,
                    comparison=summary,
                )
            )
        return result


@router.get("/review-rounds/{round_id}", response_model=ReviewRoundOut)
async def get_review_round(round_id: int) -> ReviewRoundOut:
    """Full round detail: seats, annotations, comparison and public base pack."""

    async with session_scope() as session:
        detail = await ReviewPanelRepository(session).get_round(round_id)
        if detail is None:
            raise HTTPException(status_code=404, detail={"code": "not_found"})
        return _round_out(detail)


def _lenses_for_panels(lens_set: LensSet) -> list[ReviewLensOut]:
    return [
        ReviewLensOut(
            id=lens.id,
            ref=lens.ref,
            name=lens.name,
            focus=lens.focus,
            required_dimensions=[dimension.value for dimension in lens.required_dimensions],
        )
        for lens in lens_set.lenses
    ]


@router.get("/review-panels", response_model=ReviewPanelsOut)
async def list_review_panels(
    settings: Annotated[Settings, Depends(get_settings)],
) -> ReviewPanelsOut:
    """Configured panels plus the lens catalogue for the launch dialog."""

    configuration_error: str | None = None
    try:
        panels = parse_review_panels(settings.review_panels)
    except ValueError as exc:
        panels = None
        configuration_error = str(exc)
    try:
        lens_set = load_review_lenses()
    except Exception as exc:  # noqa: BLE001 - a broken config must not 500 the page
        lens_set = None
        configuration_error = configuration_error or str(exc)

    if panels is None or lens_set is None:
        return ReviewPanelsOut(
            enabled=settings.review_panel_enabled,
            configuration_error=configuration_error or "评审组配置无效",
        )

    default_panel = panels.default_panel or None

    def panel_out(panel_id: str) -> ReviewPanelOut:
        panel = panels.panels[panel_id]
        seats: list[ReviewPanelSeatOut] = []
        for seat in panel.seats:
            lens = lens_set.get(seat.lens_id)
            seats.append(
                ReviewPanelSeatOut(
                    seat_id=seat.seat_id,
                    lens_id=seat.lens_id,
                    lens_ref=lens.ref if lens else None,
                    lens_name=lens.name if lens else None,
                    lens_focus=lens.focus if lens else None,
                    model_ref=seat.model_ref,
                )
            )
        return ReviewPanelOut(
            id=panel_id,
            name=panel.name,
            default=panel_id == default_panel,
            seats=seats,
        )

    configured = [panel_out(panel_id) for panel_id in panels.panels]
    return ReviewPanelsOut(
        enabled=settings.review_panel_enabled,
        default_panel=default_panel,
        panels=configured,
        lenses=_lenses_for_panels(lens_set),
        configuration_error=configuration_error,
    )


__all__ = ["comparison_summary", "router"]
