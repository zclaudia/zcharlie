"""Candidate and decision-journal repositories (P1-D1 / P1-D2).

:class:`DecisionRepository` is append-only by construction: it offers
:meth:`~DecisionRepository.append` and reads, nothing that updates or
deletes (the ORM listeners on :class:`DecisionRecord` refuse both anyway).
Callers write a decision in the same session / transaction as the state
change it records, so both commit or roll back together.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select

from investment_agent.domain.time import as_utc as as_utc
from investment_agent.storage.models.candidates import (
    CANDIDATE_STATUSES,
    DECISION_ACTORS,
    DECISION_KINDS,
    CandidateRecord,
    DecisionRecord,
)
from investment_agent.storage.repositories.base import Repository

REASON_REQUIRED_KINDS = frozenset({"dismiss", "reject_rule", "remove_watch"})
PREFERENCE_KINDS = ("reject_rule", "dismiss")


class ReasonRequiredError(ValueError):
    """``dismiss`` / ``reject_rule`` decisions need a non-empty reason."""


class CandidateRepository(Repository[CandidateRecord]):
    model = CandidateRecord

    async def open_for_listing(self, listing_id: int) -> CandidateRecord | None:
        stmt = select(CandidateRecord).where(
            CandidateRecord.listing_id == listing_id, CandidateRecord.status == "open"
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def open_by_listing(self, listing_ids: Sequence[int]) -> dict[int, CandidateRecord]:
        if not listing_ids:
            return {}
        stmt = select(CandidateRecord).where(
            CandidateRecord.listing_id.in_(list(listing_ids)), CandidateRecord.status == "open"
        )
        return {row.listing_id: row for row in (await self.session.execute(stmt)).scalars()}

    async def dismissed_since(
        self, listing_ids: Sequence[int], since: datetime
    ) -> dict[int, CandidateRecord]:
        """Latest candidate per listing dismissed at / after ``since``."""

        if not listing_ids:
            return {}
        stmt = (
            select(CandidateRecord)
            .where(
                CandidateRecord.listing_id.in_(list(listing_ids)),
                CandidateRecord.status == "dismissed",
                CandidateRecord.closed_at >= since,
            )
            .order_by(CandidateRecord.closed_at.asc())
        )
        return {row.listing_id: row for row in (await self.session.execute(stmt)).scalars()}

    async def due_for_expiry(self, now: datetime) -> list[CandidateRecord]:
        stmt = (
            select(CandidateRecord)
            .where(CandidateRecord.status == "open", CandidateRecord.expires_at <= now)
            .order_by(CandidateRecord.id)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def page(
        self, *, status: str | None, limit: int, offset: int
    ) -> tuple[list[CandidateRecord], int]:
        if status is not None and status not in CANDIDATE_STATUSES:
            raise ValueError(f"unknown candidate status {status!r}")
        stmt = select(CandidateRecord)
        count = select(func.count()).select_from(CandidateRecord)
        if status is not None:
            stmt = stmt.where(CandidateRecord.status == status)
            count = count.where(CandidateRecord.status == status)
        stmt = (
            stmt.order_by(CandidateRecord.opened_at.desc(), CandidateRecord.id.desc())
            .offset(offset)
            .limit(limit)
        )
        rows = list((await self.session.execute(stmt)).scalars())
        total = int((await self.session.execute(count)).scalar_one())
        return rows, total


class DecisionRepository(Repository[DecisionRecord]):
    model = DecisionRecord

    async def append(
        self,
        *,
        listing_id: int,
        ticker: str,
        kind: str,
        actor: str,
        reason_text: str | None = None,
        rule_instance_id: int | None = None,
        rule_ref: str | None = None,
        candidate_id: int | None = None,
        context: dict[str, Any] | None = None,
        at: datetime | None = None,
    ) -> DecisionRecord:
        if kind not in DECISION_KINDS:
            raise ValueError(f"unknown decision kind {kind!r}")
        if actor not in DECISION_ACTORS:
            raise ValueError(f"unknown decision actor {actor!r}")
        reason = reason_text.strip() if reason_text is not None else None
        if kind in REASON_REQUIRED_KINDS and not reason:
            raise ReasonRequiredError(f"a {kind} decision requires a reason")
        record = DecisionRecord(
            listing_id=listing_id,
            ticker=ticker,
            kind=kind,
            reason_text=reason or None,
            rule_instance_id=rule_instance_id,
            rule_ref=rule_ref,
            candidate_id=candidate_id,
            context=context or {},
            actor=actor,
            at=at or datetime.now(UTC),
        )
        self.session.add(record)
        await self.session.flush()
        return record

    async def for_candidate(self, candidate_id: int) -> list[DecisionRecord]:
        stmt = (
            select(DecisionRecord)
            .where(DecisionRecord.candidate_id == candidate_id)
            .order_by(DecisionRecord.at, DecisionRecord.id)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def for_listing(self, listing_id: int, *, limit: int = 100) -> list[DecisionRecord]:
        stmt = (
            select(DecisionRecord)
            .where(DecisionRecord.listing_id == listing_id)
            .order_by(DecisionRecord.at.desc(), DecisionRecord.id.desc())
            .limit(limit)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def recent(
        self, kinds: Sequence[str] = PREFERENCE_KINDS, *, limit: int = 10
    ) -> list[DecisionRecord]:
        """Newest first: the user's latest decisions of ``kinds``."""

        stmt = (
            select(DecisionRecord)
            .where(DecisionRecord.kind.in_(list(kinds)), DecisionRecord.actor == "user")
            .order_by(DecisionRecord.at.desc(), DecisionRecord.id.desc())
            .limit(limit)
        )
        return list((await self.session.execute(stmt)).scalars())
