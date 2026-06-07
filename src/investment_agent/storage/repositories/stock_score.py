"""StockScore repository — upsert by (run_id, listing_id, strategy_code) + by_run query."""

from decimal import Decimal

from sqlalchemy import select

from investment_agent.storage.models.analysis import StockScore
from investment_agent.storage.repositories.base import Repository


class StockScoreRepository(Repository[StockScore]):
    model = StockScore

    async def upsert(
        self,
        *,
        pipeline_run_id: int,
        listing_id: int,
        strategy_code: str,
        tier: int,
        score: int,
        confidence: float | Decimal,
        bull_thesis: str | None = None,
        bear_thesis: str | None = None,
        key_metrics: dict[str, object] | None = None,
        deep_analysis: str | None = None,
    ) -> StockScore:
        conf_dec = (
            confidence
            if isinstance(confidence, Decimal)
            else Decimal(str(confidence))
        )
        stmt = select(StockScore).where(
            StockScore.pipeline_run_id == pipeline_run_id,
            StockScore.listing_id == listing_id,
            StockScore.strategy_code == strategy_code,
        )
        existing = (await self.session.execute(stmt)).scalar_one_or_none()
        if existing is None:
            existing = StockScore(
                pipeline_run_id=pipeline_run_id,
                listing_id=listing_id,
                strategy_code=strategy_code,
                tier=tier,
                score=score,
                confidence=conf_dec,
                bull_thesis=bull_thesis,
                bear_thesis=bear_thesis,
                key_metrics=key_metrics or {},
                deep_analysis=deep_analysis,
            )
            self.session.add(existing)
        else:
            existing.tier = tier
            existing.score = score
            existing.confidence = conf_dec
            existing.bull_thesis = bull_thesis
            existing.bear_thesis = bear_thesis
            existing.key_metrics = key_metrics or {}
            if deep_analysis is not None:
                existing.deep_analysis = deep_analysis
        await self.session.flush()
        return existing

    async def by_run(self, run_id: int) -> list[StockScore]:
        stmt = select(StockScore).where(StockScore.pipeline_run_id == run_id)
        return list((await self.session.execute(stmt)).scalars().all())
