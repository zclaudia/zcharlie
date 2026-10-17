"""The audit-registration entry for one listing's material (plan 09).

``POST /research/listings/{listing_id}/refresh`` registers a deduplicated audit
task. This module turns the request into the frozen payload of task 06, refusing
structural problems before writing and recording requested scope plus current
source decisions:

* requested sources (only checked source ids of :data:`MATERIAL_SOURCES`);
* requested period ends (canonical ISO dates); an explicit HK period is refused
  before enqueue because that adapter only supports its default period window;
* the adapter version and policy decision for each requested source, so the
  worker can re-evaluate every fetch gate;
* a batch derived from the real date and effective source decisions, so
  identical same-day requests deduplicate while a gate change creates a new run.

Idempotency is task 06's: the payload (and therefore the idempotency key) is
built here, and :func:`~investment_agent.materials.jobs.enqueue_material_job`
returns the existing run with ``created=False`` instead of queueing a second
one, so two identical requests never collect the material twice.

The queue is not a grant to fetch. The worker must re-evaluate identity
binding, licensing, technical acceptance, runtime source settings and the global
safety gate before making any request.
"""

from __future__ import annotations

import hashlib
import json
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
from investment_agent.storage.models.reference import Company, Listing

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
ERROR_UNSUPPORTED_PERIOD: Final[str] = "unsupported_period"
"""Refusal codes; the endpoint maps them to 404/422 and never to a silent default."""

ADAPTER_VERSIONS: Final[dict[str, str]] = {
    hk_native.SOURCE: hk_native.ADAPTER_VERSION,
    us_sec.SOURCE_FACTS: us_sec.ADAPTER_VERSION,
}
"""Source id -> adapter version recorded with the requested audit scope.

Read from the adapters themselves, never hard-coded here: when an adapter
version moves, the next request records the new version and the handler refuses
a run whose version no longer matches.
"""


@dataclass(frozen=True)
class RefreshRequest:
    """One audit registration; empty ``sources``/``periods`` mean defaults."""

    listing_id: int
    sources: tuple[str, ...] = ()
    periods: tuple[str, ...] = ()
    reason: str = DEFAULT_REASON


@dataclass(frozen=True)
class RefreshOutcome:
    """What one refresh registration did and the decisions it recorded."""

    run_id: int
    created: bool
    idempotency_key: str
    adapter_versions: dict[str, str]
    source_decisions: dict[str, dict[str, object]]
    online_refresh_enabled: bool


class RefreshError(ValueError):
    """A refresh request refused before anything was queued.

    ``code`` is one of the ``ERROR_*`` constants; the endpoint translates it to
    the HTTP status and returns the code with the explanation.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


async def request_refresh(session: AsyncSession, request: RefreshRequest) -> RefreshOutcome:
    """Validate and enqueue one audit run (deduplicated by task 06).

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
    company = await session.get(Company, listing.company_id)
    if identity is None or company is None or company.canonical_name != identity.company_name:
        raise RefreshError(
            ERROR_LISTING_NOT_COVERED,
            f"listing {request.listing_id} is not in the checked HK/US identity list; "
            "material is not collected for an unchecked security",
        )
    if periods and hk_native.SOURCE in sources:
        raise RefreshError(
            ERROR_UNSUPPORTED_PERIOD,
            "香港年报来源只支持其默认期间范围；显式期间请求未入队。",
        )

    from investment_agent.materials.handlers import ONLINE_REFRESH_ENABLED
    from investment_agent.materials.source_policy import evaluate_source_policy
    from investment_agent.settings.loader import load_settings

    settings = await load_settings(session)
    decisions = {
        source: evaluate_source_policy(
            source,
            identity,
            settings=settings,
            adapter_version=ADAPTER_VERSIONS.get(source),
            global_enabled=ONLINE_REFRESH_ENABLED,
        )
        for source in sources
    }
    decision_records = {
        source: {**decision.as_dict(), "accepted_for_fetch": decision.enabled}
        for source, decision in decisions.items()
    }
    adapter_versions = {
        source: ADAPTER_VERSIONS[source] for source in sources if source in ADAPTER_VERSIONS
    }
    payload = MaterialCollectionPayload(
        listing_id=listing.id,
        sources=sources,
        periods=periods,
        refresh_batch=_refresh_batch(sources, adapter_versions, decision_records),
        adapter_versions=adapter_versions,
        reason=reason,
    )
    run, created = await enqueue_material_job(session, payload, triggered_by=REFRESH_TRIGGER)
    return RefreshOutcome(
        run_id=run.id,
        created=created,
        idempotency_key=material_idempotency_key(payload),
        adapter_versions=payload.adapter_versions,
        source_decisions=decision_records,
        online_refresh_enabled=any(decision.enabled for decision in decisions.values()),
    )


def _canonical_sources(sources: tuple[str, ...]) -> tuple[str, ...]:
    """The requested sources: default set when empty, canonical order, no repeats."""

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
    then answers honestly. Explicit HK periods are refused before enqueue
    because that source only supports its default period window. Dropping a
    caller's stated period would hide a request they made.
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


def _refresh_batch(
    sources: tuple[str, ...],
    adapter_versions: dict[str, str],
    decisions: dict[str, dict[str, object]],
) -> str:
    """The refresh batch: the real current date and effective decision digest.

    Same sources + same day + same effective gates = same batch and idempotency
    key. A gate change makes a new audit run even on the same day. The date is
    today's real date (never derived from a period or a caller field).
    """

    decision_digest = hashlib.sha256(
        json.dumps(
            {"adapter_versions": adapter_versions, "source_decisions": decisions},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:16]
    # Sources are already included in the idempotency payload. Keep the batch
    # below its 64-character contract while changing it when a gate changes.
    return f"{date.today().isoformat()}|{decision_digest}"
