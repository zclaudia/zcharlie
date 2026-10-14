"""Persistent pipeline-run queue and lifecycle helpers."""

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import CursorResult, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.attributes import set_committed_value
from sqlalchemy.orm.util import identity_key

from investment_agent.domain.pipeline_status import TERMINAL_RUN_STATUSES
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.repositories.base import Repository

TERMINAL_STATUSES = TERMINAL_RUN_STATUSES
"""Public name kept for existing callers; the single home is the domain leaf."""


class IdempotencyPayloadMismatchError(ValueError):
    """Raised when one idempotency key is reused for different input."""


class LeaseLostError(RuntimeError):
    """Raised when a lifecycle update no longer owns the run it targets.

    Either another worker re-claimed the run after stale recovery, or the run
    already reached a different status. The caller must stop working on the run
    and must not overwrite its state.
    """

    def __init__(self, run_id: int, detail: str) -> None:
        super().__init__(f"PipelineRun {run_id} lease lost: {detail}")
        self.run_id = run_id
        self.detail = detail


@dataclass(frozen=True)
class RunLease:
    """Fencing token for one claimed attempt of a run."""

    run_id: int
    worker_id: str
    attempt: int


_active_lease: ContextVar[RunLease | None] = ContextVar(
    "pipeline_run_active_lease", default=None
)


@contextmanager
def lease_scope(lease: RunLease) -> Iterator[RunLease]:
    """Fence every lifecycle update of ``lease.run_id`` made in this context.

    Code that drives a claimed run (e.g. the research chain called by the
    worker) does not need to know about leases: repository lifecycle methods
    pick up the active lease for the matching run id automatically. Outside a
    lease scope (synchronous CLI runs) updates are only guarded by status.
    """

    token = _active_lease.set(lease)
    try:
        yield lease
    finally:
        _active_lease.reset(token)


def current_lease(run_id: int) -> RunLease | None:
    lease = _active_lease.get()
    return lease if lease is not None and lease.run_id == run_id else None


def _kind_criteria(kind: str | None, kinds: Sequence[str] | None) -> list[Any]:
    criteria: list[Any] = []
    if kind is not None:
        criteria.append(PipelineRun.kind == kind)
    if kinds is not None:
        criteria.append(PipelineRun.kind.in_(list(kinds)))
    return criteria


class PipelineRunRepository(Repository[PipelineRun]):
    model = PipelineRun

    async def create(
        self,
        *,
        kind: str,
        universe_code: str | None = None,
        listing_id: int | None = None,
        triggered_by: str = "cli",
        status: str = "running",
        stage: str = "created",
        idempotency_key: str | None = None,
        request_json: dict[str, object] | None = None,
        available_at: datetime | None = None,
        max_attempts: int = 3,
    ) -> PipelineRun:
        run = PipelineRun(
            kind=kind,
            universe_code=universe_code,
            listing_id=listing_id,
            triggered_by=triggered_by,
            started_at=datetime.now(UTC),
            status=status,
            stage=stage,
            idempotency_key=idempotency_key,
            attempt_count=0,
            max_attempts=max_attempts,
            request_json=request_json or {},
            available_at=available_at,
            metadata_json={},
        )
        self.session.add(run)
        await self.session.flush()
        return run

    async def by_idempotency_key(
        self,
        *,
        kind: str,
        idempotency_key: str,
    ) -> PipelineRun | None:
        return (
            await self.session.execute(
                select(PipelineRun).where(
                    PipelineRun.kind == kind,
                    PipelineRun.idempotency_key == idempotency_key,
                )
            )
        ).scalar_one_or_none()

    async def latest_by_kind(self, kind: str, *, limit: int = 5) -> list[PipelineRun]:
        """Most recently created runs of one kind, newest first."""

        stmt = (
            select(PipelineRun)
            .where(PipelineRun.kind == kind)
            .order_by(PipelineRun.id.desc())
            .limit(limit)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def enqueue(
        self,
        *,
        kind: str,
        idempotency_key: str | None,
        triggered_by: str,
        universe_code: str | None = None,
        listing_id: int | None = None,
        request_json: dict[str, object] | None = None,
        available_at: datetime | None = None,
        max_attempts: int = 3,
    ) -> tuple[PipelineRun, bool]:
        if idempotency_key is not None:
            if not idempotency_key.strip() or len(idempotency_key) > 128:
                raise ValueError("idempotency_key must contain 1 to 128 characters")
            existing = await self.by_idempotency_key(
                kind=kind,
                idempotency_key=idempotency_key,
            )
            if existing is not None:
                if request_json is not None and existing.request_json != request_json:
                    raise IdempotencyPayloadMismatchError(
                        "idempotency key is already associated with different input"
                    )
                return existing, False
        from investment_agent.agent.operations import AGENT_KINDS, admission
        if kind in AGENT_KINDS:
            queue_class = str((request_json or {}).get("queue_class") or {"agent_discovery": "exploration", "agent_review": "review"}.get(kind, "interactive"))
            if queue_class not in {"interactive", "review", "exploration"}:
                raise ValueError("invalid queue class")
            if not await admission(self.session, queue_class):
                from investment_agent.agent.tasks import TaskBoundaryError
                raise TaskBoundaryError("queue_capacity")
        try:
            async with self.session.begin_nested():
                run = await self.create(
                    kind=kind,
                    universe_code=universe_code,
                    listing_id=listing_id,
                    triggered_by=triggered_by,
                    status="queued",
                    stage="queued",
                    idempotency_key=idempotency_key,
                    request_json=request_json,
                    available_at=available_at or datetime.now(UTC),
                    max_attempts=max_attempts,
                )
        except IntegrityError as exc:
            if idempotency_key is None:
                raise
            existing = await self.by_idempotency_key(
                kind=kind,
                idempotency_key=idempotency_key,
            )
            if existing is None:
                raise
            if request_json is not None and existing.request_json != request_json:
                raise IdempotencyPayloadMismatchError(
                    "idempotency key is already associated with different input"
                ) from exc
            return existing, False
        return run, True

    async def claim_next(
        self,
        *,
        worker_id: str,
        kind: str | None = None,
        kinds: Sequence[str] | None = None,
        run_id: int | None = None,
        now: datetime | None = None,
    ) -> PipelineRun | None:
        """Atomically claim the oldest available run for one worker.

        ``kind`` / ``kinds`` restrict the claim to the job kinds this worker
        can execute; ``run_id`` claims one specific queued run (inline CLI
        execution) instead of the oldest one.
        """

        claimed_at = now or datetime.now(UTC)
        from investment_agent.agent.operations import AGENT_KINDS, choose
        selected_kinds = list(kinds) if kinds is not None else [kind] if kind else []
        if run_id is not None and not selected_kinds:
            selected_kind = await self.session.scalar(select(PipelineRun.kind).where(PipelineRun.id == run_id))
            if selected_kind in AGENT_KINDS:
                selected_kinds = [selected_kind]
        if any(k in AGENT_KINDS for k in selected_kinds):
            selected_id = await choose(self.session, kinds=selected_kinds, now=claimed_at, run_id=run_id)
            if selected_id is None:
                return None
            run_id = selected_id
        candidate = (
            select(PipelineRun.id)
            .where(
                PipelineRun.status == "queued",
                or_(
                    PipelineRun.available_at.is_(None),
                    PipelineRun.available_at <= claimed_at,
                ),
                PipelineRun.attempt_count < PipelineRun.max_attempts,
            )
            .order_by(PipelineRun.available_at.asc(), PipelineRun.id.asc())
            .limit(1)
        )
        candidate = candidate.where(*_kind_criteria(kind, kinds))
        if run_id is not None:
            candidate = candidate.where(PipelineRun.id == run_id)

        statement = (
            update(PipelineRun)
            .where(
                PipelineRun.id == candidate.scalar_subquery(),
                PipelineRun.status == "queued",
            )
            .values(
                status="running",
                stage="claimed",
                claimed_at=claimed_at,
                worker_id=worker_id,
                heartbeat_at=claimed_at,
                attempt_count=PipelineRun.attempt_count + 1,
                error_summary=None,
            )
            .returning(PipelineRun)
        )
        return (await self.session.execute(statement)).scalar_one_or_none()

    async def recover_stale(
        self,
        *,
        stale_before: datetime,
        kind: str | None = None,
        kinds: Sequence[str] | None = None,
        now: datetime | None = None,
    ) -> tuple[list[int], list[int]]:
        """Requeue abandoned worker claims, or fail them once retries are spent.

        Only worker-claimed runs (``worker_id`` set) are considered; synchronous
        CLI runs have no heartbeat owner and must not be stolen by a worker.
        Each transition is a conditional update so a run whose worker
        heartbeated in the meantime is left alone.
        """

        current_time = now or datetime.now(UTC)
        last_seen = func.coalesce(
            PipelineRun.heartbeat_at,
            PipelineRun.claimed_at,
            PipelineRun.started_at,
        )
        query = select(
            PipelineRun.id,
            PipelineRun.worker_id,
            PipelineRun.attempt_count,
            PipelineRun.max_attempts,
        ).where(
            PipelineRun.status == "running",
            PipelineRun.worker_id.is_not(None),
            last_seen < stale_before,
        )
        query = query.where(*_kind_criteria(kind, kinds))
        candidates = list((await self.session.execute(query)).tuples().all())
        requeued: list[int] = []
        failed: list[int] = []
        for run_id, worker_id, attempt_count, max_attempts in candidates:
            exhausted = attempt_count >= max_attempts
            values: dict[str, Any] = {
                "worker_id": None,
                "claimed_at": None,
                "heartbeat_at": None,
            }
            if exhausted:
                values.update(
                    status="failed",
                    stage="failed",
                    finished_at=current_time,
                    error_summary="worker heartbeat expired after max attempts",
                )
            else:
                values.update(
                    status="queued",
                    stage="queued",
                    available_at=current_time,
                    finished_at=None,
                    error_summary="worker heartbeat expired; queued for retry",
                )
            statement = (
                update(PipelineRun)
                .where(
                    PipelineRun.id == run_id,
                    PipelineRun.status == "running",
                    PipelineRun.worker_id == worker_id,
                    PipelineRun.attempt_count == attempt_count,
                    last_seen < stale_before,
                )
                .values(**values)
                .execution_options(synchronize_session=False)
            )
            if await self._apply(run_id, statement, values):
                (failed if exhausted else requeued).append(run_id)
        return requeued, failed

    async def mark_running(self, run_id: int, *, stage: str) -> None:
        run = await self._required(run_id)
        run.status = "running"
        run.stage = stage
        run.attempt_count += 1
        run.heartbeat_at = datetime.now(UTC)
        run.error_summary = None
        await self.session.flush()

    async def set_stage(
        self, run_id: int, stage: str, *, lease: RunLease | None = None
    ) -> None:
        await self._update_running(
            run_id,
            {"stage": stage, "heartbeat_at": datetime.now(UTC)},
            lease=lease,
        )

    async def heartbeat(self, run_id: int, *, lease: RunLease | None = None) -> None:
        await self._update_running(
            run_id, {"heartbeat_at": datetime.now(UTC)}, lease=lease
        )

    async def mark_success(
        self,
        run_id: int,
        metadata: dict[str, object] | None = None,
        *,
        lease: RunLease | None = None,
    ) -> None:
        values: dict[str, Any] = {
            "status": "success",
            "stage": "completed",
            "finished_at": datetime.now(UTC),
        }
        if metadata:
            values["metadata_json"] = await self._merged_metadata(run_id, metadata)
        await self._update_running(run_id, values, lease=lease)

    async def mark_failed(
        self,
        run_id: int,
        error_summary: str,
        *,
        lease: RunLease | None = None,
    ) -> bool:
        """Fail a run unless it already reached a terminal status.

        Returns ``False`` (without raising) when an unfenced call finds the run
        already terminal; raises :class:`LeaseLostError` when a fenced call no
        longer owns the run.
        """

        lease = lease or current_lease(run_id)
        values: dict[str, Any] = {
            "status": "failed",
            "stage": "failed",
            "error_summary": error_summary,
            "finished_at": datetime.now(UTC),
        }
        if lease is not None:
            await self._update_running(run_id, values, lease=lease)
            return True
        statement = (
            update(PipelineRun)
            .where(
                PipelineRun.id == run_id,
                PipelineRun.status.not_in(TERMINAL_STATUSES),
            )
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        if await self._apply(run_id, statement, values):
            return True
        await self._required(run_id)
        return False

    async def schedule_retry(
        self,
        run_id: int,
        *,
        error_summary: str,
        available_at: datetime,
        lease: RunLease | None = None,
    ) -> bool:
        """Put a running attempt back in the queue if retry budget remains.

        Returns ``False`` when the budget is exhausted (the caller should fail
        the run) and raises :class:`LeaseLostError` if this attempt no longer
        owns the run, so a live claim by another worker is never re-queued.
        """

        lease = lease or current_lease(run_id)
        values: dict[str, Any] = {
            "status": "queued",
            "stage": "queued",
            "available_at": available_at,
            "claimed_at": None,
            "worker_id": None,
            "heartbeat_at": None,
            "finished_at": None,
            "error_summary": error_summary,
        }
        statement = (
            update(PipelineRun)
            .where(
                *self._running_criteria(run_id, lease),
                PipelineRun.attempt_count < PipelineRun.max_attempts,
            )
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        if await self._apply(run_id, statement, values):
            return True
        # Distinguish "budget exhausted" from "we no longer own this run".
        await self._update_running(run_id, {}, lease=lease, check_only=True)
        return False

    async def record_metadata(
        self,
        run_id: int,
        metadata: dict[str, object],
        *,
        lease: RunLease | None = None,
    ) -> None:
        """Merge progress metadata into a running (leased) run."""

        values: dict[str, Any] = {
            "metadata_json": await self._merged_metadata(run_id, metadata),
            "heartbeat_at": datetime.now(UTC),
        }
        await self._update_running(run_id, values, lease=lease)

    async def mark_partial(
        self,
        run_id: int,
        error_summary: str,
        metadata: dict[str, object] | None = None,
        *,
        lease: RunLease | None = None,
    ) -> None:
        values: dict[str, Any] = {
            "status": "partial",
            "stage": "partial",
            "error_summary": error_summary,
            "finished_at": datetime.now(UTC),
        }
        if metadata:
            values["metadata_json"] = await self._merged_metadata(run_id, metadata)
        await self._update_running(run_id, values, lease=lease)

    @staticmethod
    def _running_criteria(run_id: int, lease: RunLease | None) -> list[Any]:
        criteria: list[Any] = [
            PipelineRun.id == run_id,
            PipelineRun.status == "running",
        ]
        if lease is not None:
            criteria.extend(
                [
                    PipelineRun.worker_id == lease.worker_id,
                    PipelineRun.attempt_count == lease.attempt,
                ]
            )
        return criteria

    async def _update_running(
        self,
        run_id: int,
        values: dict[str, Any],
        *,
        lease: RunLease | None,
        check_only: bool = False,
    ) -> None:
        """Apply ``values`` only while the run is running (and leased to us)."""

        lease = lease or current_lease(run_id)
        criteria = self._running_criteria(run_id, lease)
        if check_only:
            owned = (
                await self.session.execute(select(PipelineRun.id).where(*criteria))
            ).scalar_one_or_none()
            if owned is not None:
                return
        else:
            statement = (
                update(PipelineRun)
                .where(*criteria)
                .values(**values)
                .execution_options(synchronize_session=False)
            )
            if await self._apply(run_id, statement, values):
                return
        current = await self._required(run_id)
        detail = (
            f"status={current.status} worker_id={current.worker_id} "
            f"attempt={current.attempt_count}"
        )
        if lease is not None:
            detail += f"; expected worker_id={lease.worker_id} attempt={lease.attempt}"
        raise LeaseLostError(run_id, detail)

    async def _merged_metadata(
        self, run_id: int, metadata: dict[str, object]
    ) -> dict[str, object]:
        current = (
            await self.session.execute(
                select(PipelineRun.metadata_json).where(PipelineRun.id == run_id)
            )
        ).scalar_one_or_none()
        return {**(current or {}), **metadata}

    async def _apply(
        self, run_id: int, statement: Any, values: dict[str, Any]
    ) -> bool:
        """Execute a conditional UPDATE and mirror it onto any cached instance."""

        result = await self.session.execute(statement)
        assert isinstance(result, CursorResult)
        if result.rowcount == 0:
            return False
        cached = self.session.sync_session.identity_map.get(
            identity_key(PipelineRun, run_id)
        )
        if cached is not None:
            for key, value in values.items():
                set_committed_value(cached, key, value)
        return True

    async def _required(self, run_id: int) -> PipelineRun:
        run = await self.get(run_id)
        if run is None:
            raise ValueError(f"PipelineRun {run_id} not found")
        return run
