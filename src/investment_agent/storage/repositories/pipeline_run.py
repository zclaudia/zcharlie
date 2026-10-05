"""Persistent pipeline-run queue and lifecycle helpers."""

from datetime import UTC, datetime

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError

from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.repositories.base import Repository


class IdempotencyPayloadMismatchError(ValueError):
    """Raised when one idempotency key is reused for different input."""


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
        now: datetime | None = None,
    ) -> PipelineRun | None:
        """Atomically claim the oldest available run for one worker."""

        claimed_at = now or datetime.now(UTC)
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
        if kind is not None:
            candidate = candidate.where(PipelineRun.kind == kind)

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
        now: datetime | None = None,
    ) -> tuple[list[int], list[int]]:
        """Requeue abandoned runs, or fail them after their retry budget is spent."""

        current_time = now or datetime.now(UTC)
        last_seen = func.coalesce(
            PipelineRun.heartbeat_at,
            PipelineRun.claimed_at,
            PipelineRun.started_at,
        )
        query = select(PipelineRun).where(
            PipelineRun.status == "running",
            last_seen < stale_before,
        )
        if kind is not None:
            query = query.where(PipelineRun.kind == kind)
        runs = list((await self.session.execute(query)).scalars().all())
        requeued: list[int] = []
        failed: list[int] = []
        for run in runs:
            run.worker_id = None
            run.claimed_at = None
            run.heartbeat_at = None
            if run.attempt_count >= run.max_attempts:
                run.status = "failed"
                run.stage = "failed"
                run.finished_at = current_time
                run.error_summary = "worker heartbeat expired after max attempts"
                failed.append(run.id)
            else:
                run.status = "queued"
                run.stage = "queued"
                run.available_at = current_time
                run.finished_at = None
                run.error_summary = "worker heartbeat expired; queued for retry"
                requeued.append(run.id)
        await self.session.flush()
        return requeued, failed

    async def mark_running(self, run_id: int, *, stage: str) -> None:
        run = await self._required(run_id)
        run.status = "running"
        run.stage = stage
        run.attempt_count += 1
        run.heartbeat_at = datetime.now(UTC)
        run.error_summary = None
        await self.session.flush()

    async def set_stage(self, run_id: int, stage: str) -> None:
        run = await self._required(run_id)
        run.stage = stage
        run.heartbeat_at = datetime.now(UTC)
        await self.session.flush()

    async def heartbeat(self, run_id: int) -> None:
        run = await self._required(run_id)
        run.heartbeat_at = datetime.now(UTC)
        await self.session.flush()

    async def mark_success(
        self, run_id: int, metadata: dict[str, object] | None = None
    ) -> None:
        run = await self._required(run_id)
        run.status = "success"
        run.stage = "completed"
        run.finished_at = datetime.now(UTC)
        if metadata:
            run.metadata_json = {**run.metadata_json, **metadata}
        await self.session.flush()

    async def mark_failed(self, run_id: int, error_summary: str) -> None:
        run = await self._required(run_id)
        run.status = "failed"
        run.stage = "failed"
        run.error_summary = error_summary
        run.finished_at = datetime.now(UTC)
        await self.session.flush()

    async def schedule_retry(
        self,
        run_id: int,
        *,
        error_summary: str,
        available_at: datetime,
    ) -> bool:
        run = await self._required(run_id)
        if run.attempt_count >= run.max_attempts:
            return False
        run.status = "queued"
        run.stage = "queued"
        run.available_at = available_at
        run.claimed_at = None
        run.worker_id = None
        run.heartbeat_at = None
        run.finished_at = None
        run.error_summary = error_summary
        await self.session.flush()
        return True

    async def mark_partial(
        self,
        run_id: int,
        error_summary: str,
        metadata: dict[str, object] | None = None,
    ) -> None:
        run = await self._required(run_id)
        run.status = "partial"
        run.stage = "partial"
        run.error_summary = error_summary
        run.finished_at = datetime.now(UTC)
        if metadata:
            run.metadata_json = {**run.metadata_json, **metadata}
        await self.session.flush()

    async def _required(self, run_id: int) -> PipelineRun:
        run = await self.get(run_id)
        if run is None:
            raise ValueError(f"PipelineRun {run_id} not found")
        return run
