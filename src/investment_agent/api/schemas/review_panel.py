"""Schemas for the multi-perspective review API (design §5 / §7).

The responses expose only what a client needs to render a round. The frozen
panel snapshot is never returned verbatim: it carries the lens prompt, which
is server-internal. Provider endpoints and credentials never appear in any
field either; ``model_ref`` is the bare ``provider/model`` id already shown in
settings, and seat ``error`` is redacted by the route.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any

from pydantic import BaseModel, Field

from investment_agent.review_panel.contracts import MAX_SEATS_PER_ROUND

_ID_PATTERN = r"^[a-z][a-z0-9_]{0,39}$"


class ReviewRoundCreateRequest(BaseModel):
    """Body of ``POST /watchlist/{listing_id}/review-rounds``."""

    panel_id: str | None = Field(default=None, min_length=1, max_length=40, pattern=_ID_PATTERN)
    seat_ids: list[Annotated[str, Field(min_length=1, max_length=64)]] | None = Field(
        default=None, max_length=MAX_SEATS_PER_ROUND
    )
    """Omitted means every seat of the chosen panel."""


class ReviewRoundSeatOut(BaseModel):
    seat_id: str
    lens_id: str
    lens_ref: str
    lens_name: str | None = None
    model_ref: str
    status: str
    error: str | None = None
    """A safe, redacted Chinese explanation for a failed/partial seat."""
    opinion: dict[str, Any] | None = None
    annotations: dict[str, Any] | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    usage: dict[str, Any] | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class ReviewComparisonOut(BaseModel):
    """The stored deterministic comparison (``review_comparison.result_json``)."""

    algorithm_version: str
    stance_distribution: dict[str, int] = Field(default_factory=dict)
    trade_view_distribution: dict[str, int] = Field(default_factory=dict)
    dimension_matrix: dict[str, dict[str, str]] = Field(default_factory=dict)
    divergent_dimensions: list[str] = Field(default_factory=list)
    divergent_materials: dict[str, bool] = Field(default_factory=dict)
    stance_range: int = 0
    divergence_score: int = 0
    divergence_level: str
    evidence_overlap: list[dict[str, Any]] = Field(default_factory=list)
    falsifiers_by_dimension: list[dict[str, Any]] = Field(default_factory=list)
    seat_changes: list[dict[str, Any]] = Field(default_factory=list)
    notify_reasons: list[str] = Field(default_factory=list)
    failed_seats: list[str] = Field(default_factory=list)


class ReviewComparisonSummaryOut(BaseModel):
    """The compact comparison carried by a round summary / watchlist badge."""

    algorithm_version: str
    stance_distribution: dict[str, int] = Field(default_factory=dict)
    divergence_level: str
    divergence_score: int
    divergent_dimension_count: int = 0
    failed_seats: list[str] = Field(default_factory=list)
    notify_reasons: list[str] = Field(default_factory=list)


class ReviewRoundSummaryOut(BaseModel):
    """One row of ``GET /watchlist/{listing_id}/review-rounds``."""

    id: int
    listing_id: int
    panel_id: str
    panel_name: str | None = None
    trigger: str
    status: str
    as_of: date
    evidence_cutoff: datetime
    created_at: datetime
    completed_at: datetime | None = None
    seat_count: int
    seat_terminal_count: int
    seat_failed_count: int
    comparison: ReviewComparisonSummaryOut | None = None


class ReviewRoundOut(BaseModel):
    """Full round detail returned by create and ``GET /review-rounds/{id}``."""

    id: int
    listing_id: int
    panel_id: str
    panel_name: str | None = None
    trigger: str
    status: str
    as_of: date
    evidence_cutoff: datetime
    base_pack: dict[str, Any] | None = None
    base_pack_hash: str
    created_at: datetime
    completed_at: datetime | None = None
    reserved_tokens: int
    reserved_requests: int
    settled_usage: dict[str, Any] = Field(default_factory=dict)
    seats: list[ReviewRoundSeatOut] = Field(default_factory=list)
    comparison: ReviewComparisonOut | None = None


class ReviewPanelSeatOut(BaseModel):
    seat_id: str
    lens_id: str
    lens_ref: str | None = None
    lens_name: str | None = None
    lens_focus: str | None = None
    model_ref: str


class ReviewPanelOut(BaseModel):
    id: str
    name: str
    default: bool
    seats: list[ReviewPanelSeatOut] = Field(default_factory=list)


class ReviewLensOut(BaseModel):
    id: str
    ref: str
    name: str
    focus: str
    required_dimensions: list[str] = Field(default_factory=list)


class ReviewPanelsOut(BaseModel):
    """Everything the launch dialog needs (design §7)."""

    enabled: bool
    default_panel: str | None = None
    panels: list[ReviewPanelOut] = Field(default_factory=list)
    lenses: list[ReviewLensOut] = Field(default_factory=list)
    configuration_error: str | None = None


class WatchlistReviewBadgeOut(BaseModel):
    """Latest completed round badge for one watchlist row."""

    round_id: int
    seat_count: int
    divergence_level: str
    divergence_score: int
    stance_distribution: dict[str, int] = Field(default_factory=dict)
    completed_at: datetime | None = None
