"""Repository for immutable evidence-first research snapshots."""

from datetime import date

from sqlalchemy import select

from investment_agent.storage.models.analysis import ResearchSnapshot
from investment_agent.storage.repositories.base import Repository


class ResearchSnapshotRepository(Repository[ResearchSnapshot]):
    model = ResearchSnapshot

    async def create(
        self,
        *,
        pipeline_run_id: int,
        symbol: str,
        market: str,
        company_name: str,
        industry: str,
        as_of: date,
        report_year: int,
        canonical_schema_version: str,
        scoring_version: str,
        score_schema_version: str,
        score_json: dict[str, object],
        explanation_json: dict[str, object] | None,
        artifact_manifest: dict[str, object],
    ) -> ResearchSnapshot:
        snapshot = ResearchSnapshot(
            pipeline_run_id=pipeline_run_id,
            symbol=symbol,
            market=market,
            company_name=company_name,
            industry=industry,
            as_of=as_of,
            report_year=report_year,
            canonical_schema_version=canonical_schema_version,
            scoring_version=scoring_version,
            score_schema_version=score_schema_version,
            score_json=score_json,
            explanation_json=explanation_json,
            artifact_manifest=artifact_manifest,
        )
        self.session.add(snapshot)
        await self.session.flush()
        return snapshot

    async def by_run(self, pipeline_run_id: int) -> ResearchSnapshot | None:
        return (
            await self.session.execute(
                select(ResearchSnapshot).where(
                    ResearchSnapshot.pipeline_run_id == pipeline_run_id
                )
            )
        ).scalar_one_or_none()

    async def history(
        self,
        *,
        symbol: str,
        market: str,
        limit: int = 50,
    ) -> list[ResearchSnapshot]:
        result = await self.session.execute(
            select(ResearchSnapshot)
            .where(
                ResearchSnapshot.symbol == symbol,
                ResearchSnapshot.market == market,
            )
            .order_by(ResearchSnapshot.as_of.desc(), ResearchSnapshot.id.desc())
            .limit(limit)
        )
        return list(result.scalars().all())
