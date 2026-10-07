"""Repository for immutable evidence-first research snapshots."""

from datetime import date
from typing import Any

from sqlalchemy import select

from investment_agent.research.change_tracking import build_research_diff
from investment_agent.storage.models.analysis import ResearchChangeSet, ResearchSnapshot
from investment_agent.storage.repositories.base import Repository


def score_item_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Return the single score item whether stored wrapped or bare.

    The research chain persists the score item itself, while tests and older
    fixtures may wrap it in ``{"items": [...]}``. Consumers must handle both.
    """

    items = payload.get("items")
    if isinstance(items, list) and items and isinstance(items[0], dict):
        return items[0]
    if "investment_score" in payload or "strategy_label" in payload:
        return payload
    return {}


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
        previous = await self.latest(symbol=symbol, market=market)
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
        diff = build_research_diff(
            current_run_id=pipeline_run_id,
            current_score=score_json,
            previous_run_id=(previous.pipeline_run_id if previous is not None else None),
            previous_score=(previous.score_json if previous is not None else None),
            current_scoring_version=scoring_version,
            previous_scoring_version=(
                previous.scoring_version if previous is not None else None
            ),
        )
        self.session.add(
            ResearchChangeSet(
                current_snapshot_id=snapshot.id,
                previous_snapshot_id=(previous.id if previous is not None else None),
                schema_version=diff.schema_version,
                baseline=diff.baseline,
                has_meaningful_changes=diff.has_meaningful_changes,
                change_count=diff.change_count,
                meaningful_change_count=diff.meaningful_change_count,
                diff_json=diff.as_dict(),
            )
        )
        await self.session.flush()
        return snapshot

    async def latest(self, *, symbol: str, market: str) -> ResearchSnapshot | None:
        """The most recently persisted snapshot of one listing."""

        return (
            await self.session.execute(
                select(ResearchSnapshot)
                .where(
                    ResearchSnapshot.symbol == symbol,
                    ResearchSnapshot.market == market,
                )
                .order_by(ResearchSnapshot.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

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
        offset: int = 0,
    ) -> list[ResearchSnapshot]:
        result = await self.session.execute(
            select(ResearchSnapshot)
            .where(
                ResearchSnapshot.symbol == symbol,
                ResearchSnapshot.market == market,
            )
            .order_by(ResearchSnapshot.as_of.desc(), ResearchSnapshot.id.desc())
            .offset(offset)
            .limit(limit)
        )
        return list(result.scalars().all())
