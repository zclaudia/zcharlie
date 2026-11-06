"""Review lenses: config document kind ``review_lenses`` (design §3.1).

A lens is one analysis stance (value / growth / bear / risk in the baseline).
Its ``required_dimensions`` name the :class:`~investment_agent.review_panel.contracts.Dimension`
values a seat must cover in its assumptions, which is what the deterministic
comparison groups on.

The document is DB-managed like every other config document
(docs/product/config-docs-plan.md §3.1): ``config/review_lenses.yaml`` is the
first-import baseline, :func:`parse_review_lenses` is both parser and
validation (CD3), and :func:`load_review_lenses` returns the effective
document through the shared process cache.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from investment_agent.review_panel.contracts import Dimension

DEFAULT_REVIEW_LENSES_PATH = Path(__file__).resolve().parents[3] / "config" / "review_lenses.yaml"

_LENS_ID_PATTERN = r"^[a-z][a-z0-9_]{0,39}$"


class ReviewLensError(ValueError):
    """A review-lens document failed validation (API maps it to 422)."""


class Lens(BaseModel):
    """One versioned analysis stance."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    id: str = Field(pattern=_LENS_ID_PATTERN)
    version: int = Field(ge=1)
    name: str = Field(min_length=1)
    """Chinese display name (the seat card heading)."""
    focus: str = Field(min_length=1)
    """One-line Chinese statement of what the lens looks at."""
    prompt: str = Field(min_length=1)
    """Chinese prompt fragment appended to the shared review rules."""
    required_dimensions: tuple[Dimension, ...]
    """Dimensions the seat must cover in ``SeatOpinion.assumptions``."""

    @property
    def ref(self) -> str:
        """The versioned reference recorded on a round, e.g. ``value.v1``."""

        return f"{self.id}.v{self.version}"


class LensSet(BaseModel):
    """The ordered lens list plus id lookup."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lenses: tuple[Lens, ...]

    def get(self, lens_id: str) -> Lens | None:
        """The lens with ``lens_id``, else ``None``."""

        for lens in self.lenses:
            if lens.id == lens_id:
                return lens
        return None

    def require(self, lens_id: str) -> Lens:
        """The lens with ``lens_id``; ``KeyError`` when it is not configured."""

        lens = self.get(lens_id)
        if lens is None:
            raise KeyError(f"unknown review lens {lens_id!r}")
        return lens

    @property
    def ids(self) -> tuple[str, ...]:
        """Configured ids in document order."""

        return tuple(lens.id for lens in self.lenses)


def _entries(raw: Mapping[str, Any]) -> list[Any]:
    """The ``lenses`` section as a list.

    The baseline uses a list. A mapping keyed by id is also accepted (the id
    then defaults to the key) so an editor can reshape the document without
    tripping over the parser.
    """

    section = raw.get("lenses")
    if isinstance(section, Mapping):
        entries: list[Any] = []
        for lens_id, value in section.items():
            if not isinstance(value, Mapping):
                raise ReviewLensError(f"review_lenses.lenses.{lens_id}: expected a mapping")
            entry = dict(value)
            entry.setdefault("id", lens_id)
            entries.append(entry)
        return entries
    if isinstance(section, list):
        return section
    raise ReviewLensError("review_lenses.lenses: expected a list or a mapping")


def parse_review_lenses(raw: Any) -> LensSet:
    """Validate a whole review-lens document (config-docs CD3).

    Raises :class:`ReviewLensError` (a ``ValueError``) naming the offending
    lens for every structural problem: unknown fields, an invalid id pattern,
    a version below 1, empty name / focus / prompt, an invalid or duplicate
    dimension, or a duplicate lens id.
    """

    if not isinstance(raw, Mapping):
        raise ReviewLensError("review lenses config must be a mapping")
    entries = _entries(raw)
    if not entries:
        raise ReviewLensError("review lenses config must define at least one lens")

    lenses: list[Lens] = []
    seen_ids: set[str] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            raise ReviewLensError(f"review_lenses.lenses[{index}]: expected a mapping")
        try:
            lens = Lens.model_validate(entry)
        except ValidationError as exc:
            raise ReviewLensError(f"review_lenses.lenses[{index}]: {exc}") from exc
        if lens.id in seen_ids:
            raise ReviewLensError(f"duplicate review lens id {lens.id!r}")
        seen_ids.add(lens.id)
        if not lens.required_dimensions:
            raise ReviewLensError(f"review lens {lens.id!r}: required_dimensions must not be empty")
        if len(set(lens.required_dimensions)) != len(lens.required_dimensions):
            raise ReviewLensError(f"review lens {lens.id!r}: required_dimensions must be unique")
        lenses.append(lens)
    return LensSet(lenses=tuple(lenses))


def load_review_lenses(path: Path | str | None = None) -> LensSet:
    """The effective lens set: the DB document, else the baseline file.

    ``path`` reads a specific YAML file directly (tests / tooling). Without
    it the shared config-docs cache answers — primed at start-up, refreshed
    with the settings revision, and falling back to the baseline file on a
    cold cache (CLI, tests), exactly like the other kinds. The import is
    function-level so ``config_docs.registry`` can import this module without
    creating an import cycle.
    """

    if path is not None:
        document = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        return parse_review_lenses(document)
    from investment_agent.config_docs.loader import config_doc

    return cast(LensSet, config_doc("review_lenses"))


__all__ = [
    "DEFAULT_REVIEW_LENSES_PATH",
    "Lens",
    "LensSet",
    "ReviewLensError",
    "load_review_lenses",
    "parse_review_lenses",
]
