"""Schemas of the ``/rule-templates`` endpoints (template page + authoring)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

FileStatus = Literal["synced", "baseline_changed", "file_missing", "file_invalid"]
"""CD6 drift semantics: how the baseline file compares with the *current*
stored version — ``synced`` - the file content is the current version;
``baseline_changed`` - the file differs (a DB-authored version is current,
or the file was edited; pull it in explicitly via 从基线导入);
``file_missing`` - no file defines the id any more; ``file_invalid`` - the
file does not load (the stored version stays in use)."""


class TemplateVersionOut(BaseModel):
    version: int
    hash: str
    calibration_status: str
    created_at: datetime
    created_by: str | None = None
    """``api`` / ``web`` for authored versions; ``None`` = the file baseline."""
    note: str | None = None


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
    baseline_version: int | None = None
    """The stored version whose content matches the baseline file, if any."""


class TemplateDetailOut(TemplateSummaryOut):
    latest_version: int
    definition: dict[str, Any]
    """Canonical stored definition of ``version`` (lifecycle fields excluded)."""
    versions: list[TemplateVersionOut]
    factor_units: dict[str, str] = Field(default_factory=dict)


class RuleIssueOut(BaseModel):
    """One validation problem, keyed by its path in the template document."""

    code: str
    path: str
    message: str


class TemplateValidateRequest(BaseModel):
    """Dry-run of ``TemplateVersionCreateRequest`` (the 校验 button)."""

    document: dict[str, Any]


class TemplateValidateOut(BaseModel):
    ok: bool
    issues: list[RuleIssueOut] = Field(default_factory=list)


class TemplateVersionCreateRequest(BaseModel):
    document: dict[str, Any]
    """The full template document (a file's YAML mapping; ``version`` ignored)."""
    note: str
    """Mandatory change note (CD7)."""


class TemplateActivateRequest(BaseModel):
    version: int = Field(ge=1)
    note: str
    """Mandatory change note (CD7)."""


class TemplateBaselineImportRequest(BaseModel):
    note: str
    """Mandatory change note (CD7)."""


class TemplateWriteOut(BaseModel):
    """The result of one authored write (new version / activate / import)."""

    action: Literal["created", "new_version", "unchanged"]
    version: int
    hash: str
    created_by: str | None = None
    note: str | None = None
    detail: TemplateDetailOut
