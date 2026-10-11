"""/portfolio/rule endpoint schemas (holding-scope rule review, WP8)."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from investment_agent.api.schemas.admin import (
    WatchlistRuleInstanceOut,
    ZoneEventsOut,
)


class HoldingRuleRunOut(BaseModel):
    """The holding draft run behind the current state."""

    id: int
    status: str
    template_ref: str | None = None
    error: str | None = None


class HoldingRuleDecisionOut(BaseModel):
    kind: str
    at: datetime
    reason: str | None = None
    rule_instance_id: int | None = None


class HoldingRuleOut(BaseModel):
    """One listing's holding rule slot as the portfolio page renders it."""

    listing_id: int
    scope: str = "holding"
    state: str
    """``needs_template`` / ``drafting`` / ``ready`` / ``failed`` / ``rejected`` / ``none``."""
    active: WatchlistRuleInstanceOut | None = None
    draft: WatchlistRuleInstanceOut | None = None
    run: HoldingRuleRunOut | None = None
    requested_template_ref: str | None = None
    last_decision: HoldingRuleDecisionOut | None = None
    zone_events: ZoneEventsOut | None = None


class HoldingRuleDraftRequest(BaseModel):
    listing_id: int = Field(ge=1)
    template_id: str | None = Field(default=None, min_length=1, max_length=64)


class HoldingRuleApproveRequest(BaseModel):
    listing_id: int = Field(ge=1)
    rule_instance_id: int = Field(ge=1)
    expected_active_rule_instance_id: int | None = Field(default=None, ge=1)
    reason: str | None = Field(default=None, max_length=1000)


class HoldingRuleEditRequest(BaseModel):
    listing_id: int = Field(ge=1)
    rule_instance_id: int = Field(ge=1)
    params: dict[str, float] = Field(default_factory=dict)
    reason: str | None = Field(default=None, max_length=1000)


class HoldingRuleRejectRequest(BaseModel):
    listing_id: int = Field(ge=1)
    rule_instance_id: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=1000)


class ZoneEventListOut(BaseModel):
    """Recent zone events (the review/history read)."""

    events: list[dict[str, Any]] = Field(default_factory=list)
