"""Config document kinds: what is DB-managed, where its baseline lives, how to parse.

A *config document* is one whole YAML config file promoted into the DB and
edited in the Web UI (config-docs-plan §3.1). Each kind reuses the file's
existing loader as both parser and validator (CD3: parse-as-validation, no
second validation model), keeps the repo file as the first-import baseline
(CD5) and names its apply mode for the UI. Registered kinds: ``field_sources``
(WP1), ``source_policy`` (WP2), ``industry_groups`` (WP3), ``review_lenses``.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal

import yaml

from investment_agent.factors.field_sources import (
    DEFAULT_FIELD_SOURCES_PATH,
    parse_field_sources,
)
from investment_agent.factors.industry_groups import (
    DEFAULT_INDUSTRY_GROUPS_PATH,
    parse_industry_groups,
)
from investment_agent.materials.source_policy import (
    DEFAULT_SOURCE_POLICY_PATH,
    parse_source_policy,
)
from investment_agent.review_panel.lenses import (
    DEFAULT_REVIEW_LENSES_PATH,
    parse_review_lenses,
)

ApplyMode = Literal["live", "next_job"]
"""``live`` = the next read sees it; ``next_job`` = the worker's next job."""


@dataclass(frozen=True)
class ConfigDocSpec:
    """One DB-managed config document kind.

    ``parse`` takes the raw document (the parsed-YAML dict a UI PATCH sends or
    the baseline file holds) and returns the validated in-memory object
    consumers use; a ``ValueError`` subclass is the validation error the API
    maps to 422. ``baseline`` is the repo file imported once on first start;
    afterwards the DB row is the source of truth and the file only serves the
    drift check and explicit re-import.
    """

    kind: str
    title: str
    """Chinese UI label of the document (tab / card heading)."""
    baseline: Path
    parse: Callable[[Any], Any]
    apply: ApplyMode


KINDS: tuple[ConfigDocSpec, ...] = (
    ConfigDocSpec(
        kind="field_sources",
        title="字段来源优先级",
        baseline=DEFAULT_FIELD_SOURCES_PATH,
        parse=parse_field_sources,
        apply="next_job",
    ),
    ConfigDocSpec(
        kind="source_policy",
        title="来源信任策略",
        baseline=DEFAULT_SOURCE_POLICY_PATH,
        parse=parse_source_policy,
        apply="next_job",
    ),
    ConfigDocSpec(
        kind="industry_groups",
        title="行业分组映射",
        baseline=DEFAULT_INDUSTRY_GROUPS_PATH,
        parse=parse_industry_groups,
        apply="live",
    ),
    ConfigDocSpec(
        kind="review_lenses",
        title="评审视角",
        baseline=DEFAULT_REVIEW_LENSES_PATH,
        parse=parse_review_lenses,
        apply="next_job",
    ),
)
"""The registered kinds: field_sources (WP1), source_policy (WP2),
industry_groups (WP3), review_lenses (multi-model review)."""

KINDS_BY_KIND: dict[str, ConfigDocSpec] = {spec.kind: spec for spec in KINDS}


def spec_for(kind: str) -> ConfigDocSpec:
    """The spec of ``kind``; ``KeyError`` for an unregistered kind."""

    try:
        return KINDS_BY_KIND[kind]
    except KeyError:
        raise KeyError(f"unknown config document kind {kind!r}") from None


def jsonable(value: Any) -> Any:
    """Recursively replace YAML date / datetime objects with ISO strings.

    ``yaml.safe_load`` turns the baseline files' unquoted dates
    (``checked_on: 2026-10-06``) into ``datetime.date`` objects, which neither
    the JSON columns nor the canonical-JSON drift comparison can serialize —
    while every kind parser accepts ISO *strings* (``date.fromisoformat``),
    which is also the only shape a JSON transport (UI PATCH, API) can deliver.
    Normalizing here keeps every stored, compared and hashed document
    JSON-shaped without touching the parsers (CD3).
    """

    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, dict):
        return {jsonable(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [jsonable(item) for item in value]
    return value


def baseline_document(spec: ConfigDocSpec) -> dict[str, Any]:
    """The baseline file's raw document, date objects normalized (see ``jsonable``).

    Must be a mapping, like every stored value.
    """

    document = yaml.safe_load(spec.baseline.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError(f"{spec.baseline}: expected a mapping document")
    normalized = jsonable(document)
    assert isinstance(normalized, dict)  # jsonable preserves the node types
    return normalized


def parse_baseline(spec: ConfigDocSpec) -> Any:
    """The baseline file parsed / validated by the kind's own loader."""

    return spec.parse(baseline_document(spec))
