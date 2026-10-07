"""Schemas of the read-only verification request endpoint (P1-A13 / P1-E1).

The candidate page shows whether an iFinD verification of a listing is
queued, deferred (with ``not_before``) or done (verified / conflicting
fields). ``result_summary`` keeps only those outcome keys; the raw iFinD
response bodies stay in the run artifacts.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, Field


class VerificationResultSummary(BaseModel):
    report_period: str | None = None
    verified: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    unavailable: list[str] = Field(default_factory=list)
    filled: list[str] = Field(default_factory=list)
    conflict_fields: dict[str, dict[str, Any]] = Field(default_factory=dict)
    """Per conflicting field: verdict, iFinD / free values, tolerance."""
    note: str | None = None
    error: str | None = None
    deferred: str | None = None
    """Why the request waits (e.g. the budget window is exhausted)."""
    superseded_by: int | None = None


class VerificationRequestOut(BaseModel):
    id: int
    listing_id: int
    ticker: str
    trigger: str
    status: str
    priority: int
    fields: list[str] | None
    """``None`` = every key field."""
    report_period: date | None
    requested_at: datetime
    not_before: datetime | None
    finished_at: datetime | None
    attempts: int
    run_id: int | None
    result_summary: VerificationResultSummary | None


class VerificationRequestPageOut(BaseModel):
    items: list[VerificationRequestOut]
    total: int
    limit: int
