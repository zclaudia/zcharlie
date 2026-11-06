"""Schemas of the config-document endpoints (config-docs-plan §4.5).

Values are the raw documents (the parsed-YAML mappings the UI edits and the
baseline file holds); the server never reshapes them — validation is the
kind's own parser at write time (CD3). The note on every write is mandatory
(CD7); an empty / missing / blank one is rejected with the same 422 shape as
a parse failure.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class ConfigDocSummaryOut(BaseModel):
    """One row of the kind list: edit metadata plus baseline drift."""

    kind: str
    title: str
    revision: int
    updated_at: datetime | None = None
    """``None`` while the kind has never been imported or written."""

    updated_by: str | None = None
    imported: bool
    drifted: bool
    """The stored document differs from the baseline file (CD5)."""


class ConfigDocListOut(BaseModel):
    items: list[ConfigDocSummaryOut]


class ConfigDocOut(BaseModel):
    """The effective document plus the baseline file's raw document.

    The UI renders the baseline diff client-side from ``value`` vs
    ``baseline_value`` (``None`` when the baseline file cannot be read).
    """

    kind: str
    title: str
    value: dict[str, Any]
    baseline_value: dict[str, Any] | None = None
    revision: int
    updated_at: datetime | None = None
    updated_by: str | None = None
    imported_at: datetime | None = None
    """When the baseline was (last) imported; ``None`` for a direct write."""

    drifted: bool


class ConfigDocPatchRequest(BaseModel):
    value: dict[str, Any]
    note: str | None = Field(default=None, max_length=500)
    """Mandatory; empty / blank reaches the store and is a 422."""

    expected_revision: int | None = Field(default=None, ge=0)


class ConfigDocBaselineImportRequest(BaseModel):
    note: str | None = Field(default=None, max_length=500)
    """Mandatory; empty / blank reaches the store and is a 422."""


class ConfigDocChangeOut(BaseModel):
    id: int
    old_value: Any = None
    """``None`` on the row that first created the document."""

    new_value: Any = None
    changed_at: datetime
    actor: str
    note: str


class ConfigDocHistoryOut(BaseModel):
    items: list[ConfigDocChangeOut]


class ConfigDocCoverageCheckRequest(BaseModel):
    """Body of the industry-groups coverage check (config-docs-plan §4.5, WP3).

    ``value`` is optional: with it the *draft* document in the editor is
    checked (parse-validated first, a 422 on a bad draft); without it the
    effective stored document.
    """

    value: dict[str, Any] | None = None


class CoverageGapOut(BaseModel):
    """One uncovered code (UI highlight + quick-assign target)."""

    code: str
    name: str | None = None
    tickers: list[str] = []


class IndustryGroupsCoverageOut(BaseModel):
    """The industry-groups coverage result (§4.5 / §6), read-only, no writes."""

    ok: bool
    unmapped_csrc: list[CoverageGapOut]
    missing_level1_defaults: list[CoverageGapOut]
    invalid_overrides: list[CoverageGapOut]
    member_count: int
    csrc_code_count: int
    summary: str
    """One Chinese line for the UI."""

    as_of: str
    """The date the universe membership / classifications were read at."""
