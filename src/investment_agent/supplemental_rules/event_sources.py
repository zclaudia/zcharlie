"""Cutoff-safe reads for saved P2 events and existing candidate event facts.

Existing earnings forecasts, disclosure schedules and segment rows are useful
saved facts, but their adapter marks them candidate. This module never promotes
them to a verified policy event or a company impact conclusion.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.clock import SHANGHAI_TZ
from investment_agent.evidence.contracts import DatePrecision
from investment_agent.evidence.events import EVENT_MODELS, EventRow, event_material
from investment_agent.investor.common import ObjectRef, OwnerId, utc_timestamp
from investment_agent.storage.models.earnings_events import EarningsForecastRecord
from investment_agent.supplemental_rules.contracts import ResearchEvent


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _after_date(value: date) -> datetime:
    return datetime.combine(value + timedelta(days=1), time.min, SHANGHAI_TZ).astimezone(UTC)


async def load_research_event(
    session: AsyncSession,
    owner_id: OwnerId,
    ref: ObjectRef,
    cutoff: datetime,
    *,
    visible_at: datetime | None = None,
) -> ResearchEvent | None:
    """Resolve an exact owner-scoped revision, preserving publication precision."""
    cutoff = utc_timestamp(cutoff)
    visible = utc_timestamp(visible_at or cutoff)
    if ref.kind != "research_event" or ref.revision < 1:
        raise ValueError("an exact research_event reference is required")
    # Import lazily so this source adapter stays independent of write services.
    from investment_agent.storage.repositories.supplemental_rules import SupplementalRulesRepository

    repository = SupplementalRulesRepository(session)
    row = await repository.get_event_row(owner_id, ref)
    if row is None or _utc(row.created_at) > visible:
        return None
    event = await repository.get_event_revision(owner_id, ref)
    if event is None:
        return None
    if str(event.id) != str(ref.id) or event.revision != ref.revision:
        raise ValueError("saved research event identity disagrees with its storage row")
    if event.content_hash != row.content_hash:
        raise ValueError("saved research event content hash disagrees with its row")
    if max(event.acquired_at, event.available_from) > cutoff:
        return None
    if event.published_at is not None and event.published_at > cutoff:
        # A record may be known as a future event, but callers must preserve
        # that it is not effective. It remains visible if acquired already.
        pass
    return event


async def load_candidate_event_facts(
    session: AsyncSession,
    listing_id: int,
    cutoff: datetime,
    *,
    limit: int = 2_000,
) -> tuple[tuple[dict[str, Any], ...], tuple[str, ...]]:
    """Read existing saved event rows for this listing as candidate facts only."""
    cutoff = utc_timestamp(cutoff)
    output: list[dict[str, Any]] = []
    diagnostics: list[str] = []
    total = 0
    for kind, model in EVENT_MODELS.items():
        stmt = (select(model)
                .where(model.listing_id == listing_id, model.captured_at <= cutoff)
                .order_by(model.captured_at, model.id)
                .limit(limit + 1))
        rows = list((await session.execute(stmt)).scalars().all())
        if len(rows) > limit:
            diagnostics.append(f"{kind}_scan_truncated:count_at_least_limit_plus_one")
            rows = rows[:limit]
        for row in rows:
            if not isinstance(row, EventRow):
                raise TypeError(f"unexpected saved event row type: {type(row).__name__}")
            created_at = getattr(row, "created_at", None)
            updated_at = getattr(row, "updated_at", None)
            if created_at is not None and _utc(created_at) > cutoff:
                diagnostics.append(f"{kind}_row_persisted_after_cutoff:{row.id}")
                continue
            if updated_at is not None and _utc(updated_at) > cutoff:
                diagnostics.append(f"{kind}_row_mutated_after_cutoff:{row.id}")
                continue
            if total >= limit:
                diagnostics.append("candidate_event_scan_truncated:count_at_least_limit_plus_one")
                break
            material = event_material(row, "text")
            precision = "unknown"
            if isinstance(row, EarningsForecastRecord):
                # The row carries only a notice date. Safely make it visible
                # after that entire Shanghai date has ended; the legacy event
                # adapter's day-start timestamp is too early for strict P2.
                available_at = _after_date(row.notice_date)
                published_at = datetime.combine(row.notice_date, time.min, UTC)
                precision = "date"
                material = material.model_copy(update={
                    "published_at": published_at,
                    "available_from": available_at,
                    "date_precision": DatePrecision.DATE,
                })
            else:
                available_at = _utc(row.captured_at)
                material = material.model_copy(update={
                    "published_at": available_at,
                    "available_from": available_at,
                    "date_precision": DatePrecision.UNKNOWN,
                })
            if material.acquired_at > cutoff or available_at > cutoff:
                continue
            output.append({
                "kind": kind,
                "id": row.id,
                "ref": material.source_ref.model_dump(mode="json"),
                "subject_ref": material.subject_ref.model_dump(mode="json"),
                "captured_at": _utc(row.captured_at).isoformat(),
                "published_at": material.published_at.isoformat(),
                "available_from": material.available_from.isoformat(),
                "date_precision": precision,
                "verification": material.verification,
                "conflict": material.conflict,
                "content_hash": material.content_hash,
                "fact": material.model_dump(mode="json"),
                "semantic_role": "candidate_fact",
            })
            total += 1
    return tuple(output), tuple(dict.fromkeys(diagnostics))
