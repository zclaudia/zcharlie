"""The material collection job kind, its payload and its enqueueing (plan 06).

One ``material_collection`` run collects the research material of a single
security (a ``Listing``) from a bounded set of source adapters. The payload is
the whole request — which security, which sources, which period ends, which
refresh batch and which adapter versions the caller licensed — and the
idempotency key is derived from it, so re-enqueueing the same refresh returns
the run that already exists instead of collecting the material twice.

Three deliberate properties:

* the payload names no price / FX / corporate-action capability: those
  datasets are conditionally enabled and stay out of the first phase;
* ``periods`` carries period *end* dates only. The real period span belongs to
  the source (an issuer's 52/53-week year is not a calendar year), so the
  handler never back-computes a start date from a quarter or a year;
* ``adapter_versions`` binds each source to the adapter version the caller
  licensed; the handler refuses to fetch a source whose adapter moved instead
  of collecting under an unlicensed version.

The run carries no ``universe_code`` (material is not collected for a universe
slice) but does carry the ``listing_id`` it belongs to.
"""

from __future__ import annotations

import hashlib
from datetime import date

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.domain.security import (
    SOURCE_EASTMONEY_HK,
    SOURCE_SEC_COMPANYFACTS,
)
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository

MATERIAL_COLLECTION = "material_collection"
"""``pipeline_run.kind`` of one security's material collection run."""

MATERIAL_SOURCES: tuple[str, ...] = (SOURCE_EASTMONEY_HK, SOURCE_SEC_COMPANYFACTS)
"""The sources this job collects: the first phase's annual-report material.

The ids are task 01's checked source ids, not free strings: the pilot list, the
adapters and the stored datasets all use the same spelling.
"""

MAX_IDEMPOTENCY_KEY_LENGTH = 128
"""``pipeline_run.idempotency_key`` holds at most this many characters."""

DEFAULT_MAX_ATTEMPTS = 3
"""Retry budget of one collection run (transient network failures only)."""


class MaterialCollectionPayload(BaseModel):
    """One security's material collection request (frozen, no unknown fields).

    ``sources`` is normalised to the canonical order and de-duplicated, so two
    spellings of the same request are the same payload and the same idempotency
    key. ``periods`` holds canonical ISO period end dates (``2024-12-31``);
    empty means "the adapter's own default window". ``refresh_batch`` names the
    refresh the run belongs to (source + date, chosen by the caller) and takes
    part in the idempotency key, so a later batch is a separate run while a
    repeated enqueue of the same batch is not.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    listing_id: int = Field(ge=1)
    sources: tuple[str, ...] = MATERIAL_SOURCES
    periods: tuple[str, ...] = ()
    refresh_batch: str = Field(min_length=1, max_length=64)
    adapter_versions: dict[str, str] = {}
    """Source -> adapter version within the licensed scope (empty = no binding)."""
    reason: str = Field(default="user_refresh", min_length=1, max_length=64)

    @field_validator("sources")
    @classmethod
    def _known_sources(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("at least one material source is required")
        unknown = sorted(set(value) - set(MATERIAL_SOURCES))
        if unknown:
            raise ValueError(
                f"unknown material sources {unknown}; known sources are {list(MATERIAL_SOURCES)}"
            )
        return tuple(source for source in MATERIAL_SOURCES if source in value)

    @field_validator("periods")
    @classmethod
    def _canonical_period_ends(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for item in value:
            if not isinstance(item, str):
                raise ValueError(f"period end dates must be ISO date strings, got {item!r}")
            try:
                parsed = date.fromisoformat(item)
            except ValueError as exc:
                raise ValueError(f"period end {item!r} is not an ISO date") from exc
            if parsed.isoformat() != item:
                raise ValueError(f"period end {item!r} is not the canonical ISO form")
        return tuple(sorted(set(value)))

    @field_validator("adapter_versions")
    @classmethod
    def _known_adapter_versions(cls, value: dict[str, str]) -> dict[str, str]:
        unknown = sorted(set(value) - set(MATERIAL_SOURCES))
        if unknown:
            raise ValueError(
                f"adapter versions for unknown sources {unknown}; "
                f"known sources are {list(MATERIAL_SOURCES)}"
            )
        for source, version in value.items():
            if not isinstance(version, str) or not version.strip():
                raise ValueError(f"adapter version of {source!r} must be a non-empty string")
        return dict(sorted(value.items()))


def material_idempotency_key(payload: MaterialCollectionPayload) -> str:
    """``material_collection:<listing>:<sources>:<periods>:<refresh_batch>``.

    Every part is joined in the payload's canonical order, so the same request
    always yields the same key and a changed listing, source set, period set or
    batch always yields a different one. A key longer than
    :data:`MAX_IDEMPOTENCY_KEY_LENGTH` (many period ends) collapses to
    ``material_collection:<sha1 of the long key>``: still deterministic and
    still distinct per request.
    """

    listed = (
        f"{MATERIAL_COLLECTION}:{payload.listing_id}:"
        f"{'+'.join(payload.sources)}:{'+'.join(payload.periods) or '-'}:{payload.refresh_batch}"
    )
    if len(listed) <= MAX_IDEMPOTENCY_KEY_LENGTH:
        return listed
    digest = hashlib.sha1(listed.encode("utf-8"), usedforsecurity=False).hexdigest()
    return f"{MATERIAL_COLLECTION}:{digest}"


async def enqueue_material_job(
    session: AsyncSession,
    payload: MaterialCollectionPayload,
    *,
    triggered_by: str,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> tuple[PipelineRun, bool]:
    """Queue one collection run; an existing run with the same key is returned.

    No ``universe`` row is created: material belongs to one listing, and the
    request is the payload itself. The run is stamped with ``listing_id`` so
    the queue keeps the subject of the run (``ClaimedJob.listing_id``).
    """

    return await PipelineRunRepository(session).enqueue(
        kind=MATERIAL_COLLECTION,
        idempotency_key=material_idempotency_key(payload),
        triggered_by=triggered_by,
        listing_id=payload.listing_id,
        request_json=payload.model_dump(mode="json"),
        max_attempts=max_attempts,
    )
