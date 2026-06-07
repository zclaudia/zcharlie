"""PipelineRun repository — lifecycle helpers (create / mark_success / mark_failed / mark_partial)."""

from datetime import UTC, datetime

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
    ) -> PipelineRun:
        run = PipelineRun(
            kind=kind,
            universe_code=universe_code,
            listing_id=listing_id,
            triggered_by=triggered_by,
            started_at=datetime.now(UTC),
            status="running",
            metadata_json={},
        )
        self.session.add(run)
        await self.session.flush()
        return run

    async def mark_success(
        self, run_id: int, metadata: dict[str, object] | None = None
    ) -> None:
        run = await self.get(run_id)
        if run is None:
            raise ValueError(f"PipelineRun {run_id} not found")
        run.status = "success"
        run.finished_at = datetime.now(UTC)
        if metadata:
            run.metadata_json = {**run.metadata_json, **metadata}
        await self.session.flush()

    async def mark_failed(self, run_id: int, error_summary: str) -> None:
        run = await self.get(run_id)
        if run is None:
            raise ValueError(f"PipelineRun {run_id} not found")
        run.status = "failed"
        run.error_summary = error_summary
        run.finished_at = datetime.now(UTC)
        await self.session.flush()

    async def mark_partial(self, run_id: int, error_summary: str) -> None:
        run = await self.get(run_id)
        if run is None:
            raise ValueError(f"PipelineRun {run_id} not found")
        run.status = "partial"
        run.error_summary = error_summary
        run.finished_at = datetime.now(UTC)
        await self.session.flush()
