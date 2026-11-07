"""Read-only, bounded health aggregates; scanning is never delivery success."""

from datetime import datetime
from typing import Literal

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.investor.common import Contract, utc_timestamp
from investment_agent.research_events.registry import EventRegistry
from investment_agent.research_events.repository import _at
from investment_agent.storage.models.research_events import (
    ConsumerReceipt,
    ProjectionCursor,
    ResearchDomainEvent,
)


class ConsumerHealth(Contract):
    consumer: str
    status: Literal["not_installed", "not_started", "ok", "partial"]
    latest_sequence: int
    scanned_sequence: int
    unscanned: int
    retry: int
    blocked: int
    quarantined: int
    applied: int
    ignored: int
    last_success_at: datetime | None
    oldest_unsettled_at: datetime | None
    as_of: datetime


async def consumer_health(session: AsyncSession, *, registry: EventRegistry,
                          consumer: str, now: datetime) -> ConsumerHealth:
    at = utc_timestamp(now)
    latest = await session.scalar(select(func.max(ResearchDomainEvent.sequence))) or 0
    installed = consumer in registry.consumer_names
    cursor = await session.get(ProjectionCursor, consumer, populate_existing=True) if installed else None
    scanned = cursor.scanned_sequence if cursor is not None else 0
    counts: dict[str, int] = {state: count for state, count in (await session.execute(
        select(ConsumerReceipt.state, func.count()).where(ConsumerReceipt.consumer == consumer)
        .group_by(ConsumerReceipt.state))).all()}
    unscanned = await session.scalar(select(func.count()).select_from(ResearchDomainEvent).where(
        ResearchDomainEvent.sequence > scanned)) or 0
    oldest = await session.scalar(select(func.min(ResearchDomainEvent.recorded_at)).outerjoin(
        ConsumerReceipt, and_(ConsumerReceipt.consumer == consumer,
                              ConsumerReceipt.event_id == ResearchDomainEvent.event_id)).where(
            ConsumerReceipt.state.is_(None) | ConsumerReceipt.state.in_(("retry", "blocked_predecessor", "quarantined"))))
    unsettled = unscanned + counts.get("retry", 0) + counts.get("blocked_predecessor", 0) + counts.get("quarantined", 0)
    status: Literal["not_installed", "not_started", "ok", "partial"] = (
        "not_installed" if not installed else "not_started" if cursor is None else "partial" if unsettled else "ok")
    return ConsumerHealth(consumer=consumer, status=status, latest_sequence=latest,
        scanned_sequence=scanned, unscanned=unscanned, retry=counts.get("retry", 0),
        blocked=counts.get("blocked_predecessor", 0), quarantined=counts.get("quarantined", 0),
        applied=counts.get("applied", 0), ignored=counts.get("ignored", 0),
        last_success_at=_at(cursor.last_success_at) if cursor is not None else None,
        oldest_unsettled_at=_at(oldest), as_of=at)
