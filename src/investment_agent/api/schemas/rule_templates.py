"""Schemas of the read-only ``/rule-templates`` endpoints (template page)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

FileStatus = Literal["synced", "pending_sync", "file_missing", "file_invalid"]
"""How the template file compares with the latest stored version:
``pending_sync`` - the file changed and the next sync stores a new version;
``file_missing`` - no file defines the id any more; ``file_invalid`` - the
file does not load (the stored version stays in use)."""


class TemplateVersionOut(BaseModel):
    version: int
    hash: str
    calibration_status: str
    created_at: datetime


class TemplateSummaryOut(BaseModel):
    id: str
    version: int
    ref: str
    name: str
    description: str | None = None
    thesis: str | None = None
    calibration_status: str
    applicable_groups: list[str] = Field(default_factory=list)
    screen_condition_count: int
    param_count: int
    version_count: int
    synced_at: datetime
    open_candidates: int
    file_status: FileStatus


class TemplateDetailOut(TemplateSummaryOut):
    latest_version: int
    definition: dict[str, Any]
    """Canonical stored definition of ``version`` (lifecycle fields excluded)."""
    versions: list[TemplateVersionOut]
    factor_units: dict[str, str] = Field(default_factory=dict)
