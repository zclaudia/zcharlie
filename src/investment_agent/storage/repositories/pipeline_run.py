"""PipelineRun repository — lifecycle helpers (create / mark_success / mark_failed / mark_partial)."""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.repositories.base import Repository


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
        idempotency_key: str,
        triggered_by: str,
        universe_code: str | None = None,
        listing_id: int | None = None,
    ) -> tuple[PipelineRun, bool]:
        if not idempotency_key.strip() or len(idempotency_key) > 128:
            raise ValueError("idempotency_key must contain 1 to 128 characters")
        existing = await self.by_idempotency_key(
            kind=kind,
            idempotency_key=idempotency_key,
        )
        if existing is not None:
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
                )
        except IntegrityError:
            existing = await self.by_idempotency_key(
                kind=kind,
                idempotency_key=idempotency_key,
            )
            if existing is None:
                raise
            return existing, False
        return run, True

    async def mark_running(self, run_id: int, *, stage: str) -> None:
        run = await self._required(run_id)
        run.status = "running"
        run.stage = stage
        run.attempt_count += 1
        run.heartbeat_at = datetime.now(UTC)
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
