"""Repository for multi-perspective review rounds, seats and comparisons.

The stock reference is ``listing_id`` throughout, matching ``watchlist_item``:
the watchlist list can fetch its latest-round badge with one query
(:meth:`ReviewPanelRepository.latest_rounds_for_stocks`).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from sqlalchemy import CursorResult, func, select, update
from sqlalchemy.orm.attributes import set_committed_value
from sqlalchemy.orm.util import identity_key

from investment_agent.review_panel.contracts import (
    TERMINAL_SEAT_STATUSES,
    RoundStatus,
    RoundTrigger,
    SeatStatus,
)
from investment_agent.storage.models.review_panel import (
    ReviewComparisonRecord,
    ReviewRoundRecord,
    ReviewSeatRunRecord,
)
from investment_agent.storage.repositories.base import Repository

_ACTIVE_ROUND_STATUSES = (RoundStatus.QUEUED.value, RoundStatus.RUNNING.value)
_TERMINAL_ROUND_STATUSES = (RoundStatus.COMPLETED.value, RoundStatus.FAILED.value)
_AUTO_TRIGGERS = (RoundTrigger.CHANGE.value, RoundTrigger.SCHEDULED.value)


@dataclass(frozen=True)
class SeatPlan:
    """One seat to create with a round; the server freezes its model snapshot."""

    seat_id: str
    lens_id: str
    lens_ref: str
    model_ref: str
    model_snapshot: dict[str, object] = field(default_factory=dict)
    pipeline_run_id: int | None = None


@dataclass(frozen=True)
class RoundDetail:
    """A round with its seat rows and the most recent comparison version."""

    round: ReviewRoundRecord
    seats: list[ReviewSeatRunRecord]
    comparison: ReviewComparisonRecord | None


class ReviewPanelRepository(Repository[ReviewRoundRecord]):
    model = ReviewRoundRecord

    async def create_round(
        self,
        *,
        listing_id: int,
        trigger: RoundTrigger,
        seats: Sequence[SeatPlan],
        panel_id: str,
        panel_snapshot: dict[str, object],
        as_of: date,
        evidence_cutoff: datetime,
        base_pack_hash: str,
        reserved_tokens: int,
        reserved_requests: int,
        base_pack: dict[str, object] | None = None,
        trigger_ref: str | None = None,
        status: RoundStatus = RoundStatus.QUEUED,
        settled_usage: dict[str, object] | None = None,
    ) -> ReviewRoundRecord:
        """Insert the round and all seat rows in one flush (one transaction)."""

        round_row = ReviewRoundRecord(
            listing_id=listing_id,
            trigger=trigger.value,
            trigger_ref=trigger_ref,
            panel_id=panel_id,
            panel_snapshot=panel_snapshot,
            as_of=as_of,
            evidence_cutoff=evidence_cutoff,
            base_pack=base_pack,
            base_pack_hash=base_pack_hash,
            status=status.value,
            reserved_tokens=reserved_tokens,
            reserved_requests=reserved_requests,
            settled_usage=settled_usage or {},
        )
        self.session.add(round_row)
        await self.session.flush()
        for seat in seats:
            self.session.add(
                ReviewSeatRunRecord(
                    round_id=round_row.id,
                    seat_id=seat.seat_id,
                    lens_id=seat.lens_id,
                    lens_ref=seat.lens_ref,
                    model_ref=seat.model_ref,
                    model_snapshot=seat.model_snapshot,
                    pipeline_run_id=seat.pipeline_run_id,
                    status=SeatStatus.QUEUED.value,
                    evidence_refs=[],
                )
            )
        await self.session.flush()
        return round_row

    async def get_round(self, round_id: int) -> RoundDetail | None:
        round_row = await self.session.get(ReviewRoundRecord, round_id)
        if round_row is None:
            return None
        seats = list(
            (
                await self.session.execute(
                    select(ReviewSeatRunRecord)
                    .where(ReviewSeatRunRecord.round_id == round_id)
                    .order_by(ReviewSeatRunRecord.id.asc())
                )
            )
            .scalars()
            .all()
        )
        comparison = (
            await self.session.execute(
                select(ReviewComparisonRecord)
                .where(ReviewComparisonRecord.round_id == round_id)
                .order_by(
                    ReviewComparisonRecord.created_at.desc(),
                    ReviewComparisonRecord.id.desc(),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        return RoundDetail(round=round_row, seats=seats, comparison=comparison)

    async def list_rounds(
        self, listing_id: int, limit: int = 20
    ) -> list[ReviewRoundRecord]:
        result = await self.session.execute(
            select(ReviewRoundRecord)
            .where(ReviewRoundRecord.listing_id == listing_id)
            .order_by(ReviewRoundRecord.created_at.desc(), ReviewRoundRecord.id.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def latest_round(
        self, listing_id: int, completed_only: bool = False
    ) -> ReviewRoundRecord | None:
        stmt = select(ReviewRoundRecord).where(
            ReviewRoundRecord.listing_id == listing_id
        )
        if completed_only:
            stmt = stmt.where(
                ReviewRoundRecord.status == RoundStatus.COMPLETED.value
            )
        stmt = stmt.order_by(
            ReviewRoundRecord.created_at.desc(), ReviewRoundRecord.id.desc()
        ).limit(1)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def latest_rounds_for_stocks(
        self, listing_ids: Sequence[int], *, completed_only: bool = False
    ) -> dict[int, ReviewRoundRecord]:
        """Latest round per listing in one query, e.g. for watchlist badges."""

        if not listing_ids:
            return {}
        inner = select(func.max(ReviewRoundRecord.id)).where(
            ReviewRoundRecord.listing_id.in_(list(listing_ids))
        )
        if completed_only:
            inner = inner.where(
                ReviewRoundRecord.status == RoundStatus.COMPLETED.value
            )
        latest_ids = inner.group_by(ReviewRoundRecord.listing_id).scalar_subquery()
        result = await self.session.execute(
            select(ReviewRoundRecord).where(ReviewRoundRecord.id.in_(latest_ids))
        )
        return {row.listing_id: row for row in result.scalars().all()}

    async def find_round_by_trigger(
        self,
        listing_id: int,
        trigger: RoundTrigger,
        trigger_ref: str,
    ) -> ReviewRoundRecord | None:
        result = await self.session.execute(
            select(ReviewRoundRecord)
            .where(
                ReviewRoundRecord.listing_id == listing_id,
                ReviewRoundRecord.trigger == trigger.value,
                ReviewRoundRecord.trigger_ref == trigger_ref,
            )
            .order_by(ReviewRoundRecord.id.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def last_auto_round_at(self, listing_id: int) -> datetime | None:
        """When an automatic (change/scheduled) round last started, for cooldown."""

        value = (
            await self.session.execute(
                select(func.max(ReviewRoundRecord.created_at)).where(
                    ReviewRoundRecord.listing_id == listing_id,
                    ReviewRoundRecord.trigger.in_(_AUTO_TRIGGERS),
                )
            )
        ).scalar()
        return value if isinstance(value, datetime) else None

    async def active_round_exists(self, listing_id: int) -> bool:
        result = await self.session.execute(
            select(ReviewRoundRecord.id)
            .where(
                ReviewRoundRecord.listing_id == listing_id,
                ReviewRoundRecord.status.in_(_ACTIVE_ROUND_STATUSES),
            )
            .limit(1)
        )
        return result.scalar_one_or_none() is not None

    async def mark_seat_running(self, seat_run_id: int) -> bool:
        seat = await self.session.get(ReviewSeatRunRecord, seat_run_id)
        if seat is None or seat.status != SeatStatus.QUEUED.value:
            return False
        seat.status = SeatStatus.RUNNING.value
        seat.started_at = datetime.now(UTC)
        await self.session.flush()
        return True

    async def reserve_seat_slot(self, seat_run_id: int, max_running: int) -> bool:
        """Atomically move one queued seat to ``running`` within a concurrency cap.

        The count and the state change are one UPDATE so two workers cannot
        both observe the last free slot. A seat already ``running`` returns
        ``True``: a retried attempt owns the slot it reserved.
        """

        if max_running < 1:
            return False
        seat = await self.session.get(ReviewSeatRunRecord, seat_run_id)
        if seat is None:
            return False
        if seat.status == SeatStatus.RUNNING.value:
            return True
        if seat.status != SeatStatus.QUEUED.value:
            return False
        running = (
            select(func.count(ReviewSeatRunRecord.id))
            .where(ReviewSeatRunRecord.status == SeatStatus.RUNNING.value)
            .scalar_subquery()
        )
        started_at = datetime.now(UTC)
        statement = (
            update(ReviewSeatRunRecord)
            .where(
                ReviewSeatRunRecord.id == seat_run_id,
                ReviewSeatRunRecord.status == SeatStatus.QUEUED.value,
                running < max_running,
            )
            .values(
                status=SeatStatus.RUNNING.value,
                started_at=started_at,
            )
            .execution_options(synchronize_session=False)
        )
        result = await self.session.execute(statement)
        assert isinstance(result, CursorResult)
        await self.session.flush()
        if result.rowcount == 0:
            return False
        set_committed_value(seat, "status", SeatStatus.RUNNING.value)
        set_committed_value(seat, "started_at", started_at)
        return True

    async def reset_seat_queued(self, seat_run_id: int) -> bool:
        """Put a running seat back to ``queued`` (a retried/deferred attempt)."""

        seat = await self.session.get(ReviewSeatRunRecord, seat_run_id)
        if seat is None:
            return False
        if seat.status == SeatStatus.QUEUED.value:
            return True
        if seat.status != SeatStatus.RUNNING.value:
            return False
        seat.status = SeatStatus.QUEUED.value
        seat.started_at = None
        await self.session.flush()
        return True

    async def complete_round_if_active(
        self,
        round_id: int,
        status: RoundStatus,
        *,
        settled_usage: dict[str, object],
    ) -> bool:
        """Elect one finalizer: terminal status only from queued/running.

        Returns ``False`` when another seat already finalized the round, so
        the comparison is written exactly once.
        """

        if status not in {RoundStatus.COMPLETED, RoundStatus.FAILED}:
            raise ValueError(f"round completion status must be terminal, got {status}")
        completed_at = datetime.now(UTC)
        statement = (
            update(ReviewRoundRecord)
            .where(
                ReviewRoundRecord.id == round_id,
                ReviewRoundRecord.status.in_(_ACTIVE_ROUND_STATUSES),
            )
            .values(
                status=status.value,
                completed_at=completed_at,
                settled_usage=settled_usage,
            )
            .execution_options(synchronize_session=False)
        )
        result = await self.session.execute(statement)
        assert isinstance(result, CursorResult)
        await self.session.flush()
        won = bool(result.rowcount == 1)
        if won:
            # Keep the identity-map instance in step: readers in the same
            # session (the finalizer's caller) must see the terminal status.
            cached = self.session.sync_session.identity_map.get(
                identity_key(ReviewRoundRecord, round_id)
            )
            if cached is not None:
                set_committed_value(cached, "status", status.value)
                set_committed_value(cached, "completed_at", completed_at)
                set_committed_value(cached, "settled_usage", settled_usage)
        return won

    async def previous_completed_round(
        self, listing_id: int, before_round_id: int
    ) -> ReviewRoundRecord | None:
        """The latest completed round of ``listing_id`` created before this one."""

        result = await self.session.execute(
            select(ReviewRoundRecord)
            .where(
                ReviewRoundRecord.listing_id == listing_id,
                ReviewRoundRecord.status == RoundStatus.COMPLETED.value,
                ReviewRoundRecord.id < before_round_id,
            )
            .order_by(
                ReviewRoundRecord.created_at.desc(), ReviewRoundRecord.id.desc()
            )
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def set_seat_result(
        self,
        seat_run_id: int,
        status: SeatStatus,
        *,
        opinion_json: dict[str, object] | None = None,
        annotations_json: dict[str, object] | None = None,
        evidence_refs: Sequence[str] | None = None,
        usage: dict[str, object] | None = None,
        error: str | None = None,
    ) -> bool:
        """Write a seat's terminal result once. Non-terminal statuses are rejected."""

        if status not in TERMINAL_SEAT_STATUSES:
            raise ValueError(f"seat result status must be terminal, got {status}")
        seat = await self.session.get(ReviewSeatRunRecord, seat_run_id)
        if seat is None:
            return False
        seat.status = status.value
        seat.opinion_json = opinion_json
        seat.annotations_json = annotations_json
        if evidence_refs is not None:
            seat.evidence_refs = list(evidence_refs)
        seat.usage = usage
        seat.error = error[:500] if error is not None else None
        seat.finished_at = datetime.now(UTC)
        await self.session.flush()
        return True

    async def set_round_status(
        self,
        round_id: int,
        status: RoundStatus,
        *,
        completed_at: datetime | None = None,
    ) -> bool:
        round_row = await self.session.get(ReviewRoundRecord, round_id)
        if round_row is None:
            return False
        round_row.status = status.value
        if status.value in _TERMINAL_ROUND_STATUSES:
            round_row.completed_at = completed_at or datetime.now(UTC)
        await self.session.flush()
        return True

    async def save_comparison(
        self,
        round_id: int,
        algorithm_version: str,
        result_json: dict[str, object],
    ) -> ReviewComparisonRecord:
        """Insert a comparison version, replacing only the same version's result."""

        existing = (
            await self.session.execute(
                select(ReviewComparisonRecord).where(
                    ReviewComparisonRecord.round_id == round_id,
                    ReviewComparisonRecord.algorithm_version == algorithm_version,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            existing.result_json = result_json
            await self.session.flush()
            return existing
        comparison = ReviewComparisonRecord(
            round_id=round_id,
            algorithm_version=algorithm_version,
            result_json=result_json,
        )
        self.session.add(comparison)
        await self.session.flush()
        return comparison

    async def count_rounds_since(
        self,
        since: datetime,
        triggers: Sequence[RoundTrigger] | None = None,
    ) -> int:
        stmt = select(func.count(ReviewRoundRecord.id)).where(
            ReviewRoundRecord.created_at >= since
        )
        if triggers:
            stmt = stmt.where(
                ReviewRoundRecord.trigger.in_([trigger.value for trigger in triggers])
            )
        return int((await self.session.execute(stmt)).scalar() or 0)

    async def sum_tokens_since(self, since: datetime) -> int:
        """Tokens committed by rounds created since ``since``.

        The whole round is reserved before any seat runs, so reservations are
        what the daily quota must count; settlement never exceeds them.
        """

        value = (
            await self.session.execute(
                select(func.coalesce(func.sum(ReviewRoundRecord.reserved_tokens), 0))
                .where(ReviewRoundRecord.created_at >= since)
            )
        ).scalar()
        return int(value or 0)

    async def count_running_seats(self) -> int:
        value = (
            await self.session.execute(
                select(func.count(ReviewSeatRunRecord.id)).where(
                    ReviewSeatRunRecord.status == SeatStatus.RUNNING.value
                )
            )
        ).scalar()
        return int(value or 0)
