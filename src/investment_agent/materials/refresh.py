"""The licensed refresh entry of one listing's material (plan 09).

``POST /research/listings/{listing_id}/refresh`` adds exactly one thing to the
queue machinery: a *licensed request*. This module turns an HTTP-shaped request
into the frozen payload of task 06, refusing every structural problem before
anything is written, and binding to the payload what the caller licensed:

* the sources (only the checked source ids of :data:`MATERIAL_SOURCES`);
* the requested period ends (canonical ISO dates only, validated here rather
  than silently dropped); empty stays empty — "the adapter's own default
  window". An explicit HK period end currently reaches the adapter as an
  end-only period and is answered with its ``unsupported_period`` problem,
  which the run metadata reports; the request is not quietly rewritten;
* the adapter version of each requested source
  (:data:`ADAPTER_VERSIONS`), so a later adapter move cannot re-collect under an
  unlicensed version (the handler compares them);
* the refresh batch, ``<sources>|<real today>``: repeated clicks of the same
  sources on the same day reuse one batch and one run, a new day is a new batch.

Idempotency is task 06's: the payload (and therefore the idempotency key) is
built here, and :func:`~investment_agent.materials.jobs.enqueue_material_job`
returns the existing run with ``created=False`` instead of queueing a second
one, so two identical requests never collect the material twice.

No licence setting is invented: whether the run may actually call upstream is
:data:`~investment_agent.materials.handlers.ONLINE_REFRESH_ENABLED`, read by the
read model and reported in the refresh response — a queued run is not a promise
that data was fetched.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.domain.security import DEFAULT_PILOT_LIST_PATH, find_identity
from investment_agent.materials import hk_native, us_sec
from investment_agent.materials.jobs import (
    MATERIAL_SOURCES,
    MaterialCollectionPayload,
    enqueue_material_job,
    material_idempotency_key,
)
from investment_agent.storage.models.reference import Listing

DEFAULT_REASON: Final[str] = "user_refresh"
"""Reason recorded when the caller states none (task 06's payload default)."""

REFRESH_TRIGGER: Final[str] = "api:refresh"
"""``pipeline_run.triggered_by`` of a refresh enqueued through the API."""

MAX_REASON_LENGTH: Final[int] = 64
"""``reason`` bound of the frozen payload (a longer text is refused, not cut)."""

ERROR_NOT_FOUND: Final[str] = "not_found"
ERROR_LISTING_NOT_COVERED: Final[str] = "listing_not_covered"
ERROR_UNKNOWN_SOURCE: Final[str] = "unknown_source"
ERROR_INVALID_PERIOD: Final[str] = "invalid_period"
ERROR_INVALID_REASON: Final[str] = "invalid_reason"
"""Refusal codes; the endpoint maps them to 404/422 and never to a silent default."""

ADAPTER_VERSIONS: Final[dict[str, str]] = {
    hk_native.SOURCE: hk_native.ADAPTER_VERSION,
    us_sec.SOURCE_FACTS: us_sec.ADAPTER_VERSION,
}
"""Source id -> adapter version bound into the payload (traceable licensing).

Read from the adapters themselves, never hard-coded here: when an adapter
version moves, the next request binds the new version and the handler refuses a
run whose licensed version no longer matches instead of collecting under it.
"""


@dataclass(frozen=True)
class RefreshRequest:
    """One licensed refresh request; ``sources``/``periods`` empty mean defaults."""

    listing_id: int
    sources: tuple[str, ...] = ()
    periods: tuple[str, ...] = ()
    reason: str = DEFAULT_REASON


@dataclass(frozen=True)
class RefreshOutcome:
    """What one refresh request did: the run (new or existing) and its binding."""

    run_id: int
    created: bool
    idempotency_key: str
    adapter_versions: dict[str, str]


class RefreshError(ValueError):
    """A refresh request refused before anything was queued.

    ``code`` is one of the ``ERROR_*`` constants; the endpoint translates it to
    the HTTP status and returns the code with the explanation.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


async def request_refresh(session: AsyncSession, request: RefreshRequest) -> RefreshOutcome:
    """Validate, bind and enqueue one refresh run (deduplicated by task 06).

    Raises :class:`RefreshError` for an unknown listing, a listing outside the
    checked identity list, an unknown source, a malformed period end or a
    malformed reason — in every case without writing a run.
    """

    listing = await session.get(Listing, request.listing_id)
    if listing is None:
        raise RefreshError(ERROR_NOT_FOUND, f"listing {request.listing_id} does not exist")
    sources = _canonical_sources(request.sources)
    periods = _canonical_periods(request.periods)
    reason = _checked_reason(request.reason)
    identity = find_identity(
        market_code=listing.market_code,
        ticker=listing.ticker,
        path=DEFAULT_PILOT_LIST_PATH,
    )
    if identity is None:
        raise RefreshError(
            ERROR_LISTING_NOT_COVERED,
            f"listing {request.listing_id} is not in the checked HK/US identity list; "
            "material is not collected for an unchecked security",
        )
    payload = MaterialCollectionPayload(
        listing_id=listing.id,
        sources=sources,
        periods=periods,
        refresh_batch=_refresh_batch(sources),
        adapter_versions={
            source: ADAPTER_VERSIONS[source]
            for source in sources
            if source in ADAPTER_VERSIONS
        },
        reason=reason,
    )
    run, created = await enqueue_material_job(session, payload, triggered_by=REFRESH_TRIGGER)
    return RefreshOutcome(
        run_id=run.id,
        created=created,
        idempotency_key=material_idempotency_key(payload),
        adapter_versions=payload.adapter_versions,
    )


def _canonical_sources(sources: tuple[str, ...]) -> tuple[str, ...]:
    """The licensed sources: default set when empty, canonical order, no repeats."""

    wanted = tuple(sources) or MATERIAL_SOURCES
    unknown = sorted(set(wanted) - set(MATERIAL_SOURCES))
    if unknown:
        raise RefreshError(
            ERROR_UNKNOWN_SOURCE,
            f"unknown material source(s) {unknown}; known sources are {list(MATERIAL_SOURCES)}",
        )
    return tuple(source for source in MATERIAL_SOURCES if source in set(wanted))


def _canonical_periods(periods: tuple[str, ...]) -> tuple[str, ...]:
    """Canonical ISO period ends, deduplicated; never silently dropped.

    A caller that states a period gets that period in the payload — the adapter
    then answers honestly (an HK period end without a start is reported as
    ``unsupported_period`` in the run metadata). Dropping it here would hide a
    request the caller made.
    """

    canonical: list[str] = []
    for item in periods:
        try:
            parsed = date.fromisoformat(item)
        except (TypeError, ValueError) as exc:
            raise RefreshError(
                ERROR_INVALID_PERIOD, f"period end {item!r} is not an ISO date (YYYY-MM-DD)"
            ) from exc
        if parsed.isoformat() != item:
            raise RefreshError(
                ERROR_INVALID_PERIOD, f"period end {item!r} is not the canonical ISO date form"
            )
        canonical.append(item)
    return tuple(sorted(set(canonical)))


def _checked_reason(reason: str) -> str:
    """The payload's ``reason``: non-empty after stripping and at most 64 chars."""

    if not isinstance(reason, str) or not reason.strip():
        raise RefreshError(ERROR_INVALID_REASON, "reason must be a non-empty string")
    if len(reason) > MAX_REASON_LENGTH:
        raise RefreshError(
            ERROR_INVALID_REASON, f"reason must contain at most {MAX_REASON_LENGTH} characters"
        )
    return reason


def _refresh_batch(sources: tuple[str, ...]) -> str:
    """The refresh batch: the licensed sources and the real current date.

    Same sources + same day = same batch = same idempotency key = the run that
    already exists; another day is another batch. The date is today's real date
    (never derived from a period or a caller field).
    """

    return f"{'+'.join(sources)}|{date.today().isoformat()}"

