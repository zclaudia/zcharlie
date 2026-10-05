"""Repository for deterministic research change sets."""

from datetime import UTC, datetime

from sqlalchemy import select

from investment_agent.storage.models.analysis import ResearchChangeSet, ResearchSnapshot
from investment_agent.storage.repositories.base import Repository


class ResearchChangeRepository(Repository[ResearchChangeSet]):
    model = ResearchChangeSet

    async def by_run(self, pipeline_run_id: int) -> ResearchChangeSet | None:
        return (
            await self.session.execute(
                select(ResearchChangeSet)
                .join(
                    ResearchSnapshot,
                    ResearchSnapshot.id == ResearchChangeSet.current_snapshot_id,
                )
                .where(ResearchSnapshot.pipeline_run_id == pipeline_run_id)
            )
        ).scalar_one_or_none()

    async def mark_notified(
        self,
        pipeline_run_id: int,
        *,
        path: str,
        notified_at: datetime | None = None,
    ) -> ResearchChangeSet:
        change_set = await self.by_run(pipeline_run_id)
        if change_set is None:
            raise ValueError(f"Research change set for run {pipeline_run_id} not found")
        change_set.notification_path = path
        change_set.notified_at = notified_at or datetime.now(UTC)
        await self.session.flush()
        return change_set
