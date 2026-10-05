"""Repository for deterministic research change sets."""

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
