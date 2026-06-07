"""AttributionReport repository — upsert + read helpers."""

from datetime import date
from decimal import Decimal

from sqlalchemy import select

from investment_agent.storage.models.analysis import AttributionReport
from investment_agent.storage.repositories.base import Repository


class AttributionReportRepository(Repository[AttributionReport]):
    model = AttributionReport

    async def upsert(
        self,
        *,
        pipeline_run_id: int,
        listing_id: int,
        analysis_date: date,
        price_change_pct: Decimal | float,
        beta_attribution: dict[str, object],
        triggering_events: list[object],
        narrative: str,
        valuation_delta: str | None = None,
        markdown_path: str | None = None,
    ) -> AttributionReport:
        existing = await self.session.get(AttributionReport, pipeline_run_id)
        change = (
            Decimal(str(price_change_pct))
            if isinstance(price_change_pct, float)
            else price_change_pct
        )
        if existing is None:
            existing = AttributionReport(
                pipeline_run_id=pipeline_run_id,
                listing_id=listing_id,
                analysis_date=analysis_date,
                price_change_pct=change,
                beta_attribution=beta_attribution,
                triggering_events=triggering_events,
                narrative=narrative,
                valuation_delta=valuation_delta,
                markdown_path=markdown_path,
            )
            self.session.add(existing)
        else:
            existing.listing_id = listing_id
            existing.analysis_date = analysis_date
            existing.price_change_pct = change
            existing.beta_attribution = beta_attribution
            existing.triggering_events = triggering_events
            existing.narrative = narrative
            existing.valuation_delta = valuation_delta
            if markdown_path is not None:
                existing.markdown_path = markdown_path
        await self.session.flush()
        return existing

    async def latest_for_listing(
        self,
        listing_id: int,
        limit: int = 5,
    ) -> list[AttributionReport]:
        stmt = (
            select(AttributionReport)
            .where(AttributionReport.listing_id == listing_id)
            .order_by(AttributionReport.analysis_date.desc())
            .limit(limit)
        )
        return list((await self.session.execute(stmt)).scalars().all())
