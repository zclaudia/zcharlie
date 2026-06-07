"""DiscoveryPipeline: orchestrates 7 steps end-to-end.

Steps 1-4 ingest, screen, score (Tier 2), and run deep analysis (Tier 3).
Step 5 synthesizes the DiscoveryReport via the Meta-Agent. Step 6 renders +
dispatches the Markdown report. Step 7 reviews the prior run's hit-rate.
"""

from datetime import date
from pathlib import Path

import structlog
from sqlalchemy import select

from investment_agent.config import AppYamlConfig, Settings, load_app_yaml
from investment_agent.domain.protocols import DataSource
from investment_agent.pipelines import steps
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.reference import Listing
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository
from investment_agent.strategies.base import BaseStrategyAgent

log = structlog.get_logger(__name__)


class DiscoveryPipeline:
    """Orchestrate the full Discovery flow across multiple session_scope() transactions."""

    def __init__(
        self,
        *,
        data_source: DataSource,
        strategies: dict[str, BaseStrategyAgent],
        settings: Settings,
        app_config: AppYamlConfig | None = None,
        app_config_path: str = "config/app.yaml",
    ) -> None:
        self._data_source = data_source
        self._strategies = strategies
        self._settings = settings
        self._app_config = app_config or load_app_yaml(Path(app_config_path))

    async def run(self, universe_code: str, as_of: date) -> int:
        """Run full Discovery pipeline. Returns pipeline_run_id."""
        # Create the run row up-front.
        async with session_scope() as session:
            run = await PipelineRunRepository(session).create(
                kind="discovery",
                universe_code=universe_code,
                triggered_by="api",
            )
            run_id = run.id
        log.info(
            "pipeline.run.started",
            run_id=run_id,
            universe=universe_code,
            as_of=as_of.isoformat(),
        )

        try:
            # Step 1: Data ingestion.
            async with session_scope() as session:
                universe, _aux = await steps.step1_data_ingestion(
                    session,
                    universe_code=universe_code,
                    data_source=self._data_source,
                    as_of=as_of,
                    pipeline_run_id=run_id,
                    max_concurrent=self._settings.yfinance_max_concurrent,
                )
                universe_ids = [u.id for u in universe]

            # Step 2: Tier 1 screening.
            async with session_scope() as session:
                if universe_ids:
                    listings_in_session = list(
                        (
                            await session.execute(
                                select(Listing).where(Listing.id.in_(universe_ids))
                            )
                        )
                        .scalars()
                        .all()
                    )
                else:
                    listings_in_session = []
                candidates_by_strategy = await steps.step2_tier1_screening(
                    session,
                    listings=listings_in_session,
                    strategies=self._strategies,
                    as_of=as_of,
                )

            # Step 3: Tier 2 LLM scoring.
            async with session_scope() as session:
                await steps.step3_tier2_scoring(
                    session,
                    candidates_by_strategy=candidates_by_strategy,
                    strategies=self._strategies,
                    pipeline_run_id=run_id,
                    max_concurrent=self._settings.llm_max_concurrent,
                )

            # Step 4: Tier 3 deep analysis.
            async with session_scope() as session:
                await steps.step4_tier3_deep_analysis(
                    session,
                    pipeline_run_id=run_id,
                    strategies=self._strategies,
                    top_n=self._app_config.pipeline.tier3_top_n,
                    cache_days=self._app_config.pipeline.deep_analysis_cache_days,
                )

            # Step 5: Meta-Agent synthesis
            async with session_scope() as session:
                from investment_agent.meta.synthesizer import (
                    synthesize_discovery_report,
                )

                report = await synthesize_discovery_report(session, run_id)
                # Collect picks while session is open
                consensus = list(report.consensus_picks)
                divergent = list(report.divergent_picks)
                overlooked = list(report.overlooked_picks)

            # Step 6: Render + dispatch
            async with session_scope() as session:
                from investment_agent.notifiers.file_notifier import FileNotifier
                from investment_agent.renderers.markdown_renderer import (
                    MarkdownRenderer,
                )
                from investment_agent.storage.models.analysis import (
                    DiscoveryReport,
                    StockScore,
                )

                # Collect listing ids referenced by picks (for ticker enrichment)
                pick_listing_ids: set[int] = set()
                for bucket in (consensus, divergent, overlooked):
                    for p in bucket:
                        if isinstance(p, dict):
                            lid_val = p.get("listing_id")
                            if isinstance(lid_val, int):
                                pick_listing_ids.add(lid_val)

                score_stmt = select(StockScore).where(
                    StockScore.pipeline_run_id == run_id
                )
                scores = list(
                    (await session.execute(score_stmt)).scalars().all()
                )
                listing_ids = pick_listing_ids | {s.listing_id for s in scores}

                ticker_by_lid: dict[int, str] = {}
                if listing_ids:
                    listing_stmt = select(Listing).where(
                        Listing.id.in_(listing_ids)
                    )
                    for listing in (
                        await session.execute(listing_stmt)
                    ).scalars().all():
                        ticker_by_lid[listing.id] = listing.ticker

                def _enrich(
                    bucket: list[object],
                ) -> list[dict[str, object]]:
                    out: list[dict[str, object]] = []
                    for entry in bucket:
                        if not isinstance(entry, dict):
                            continue
                        lid_val = entry.get("listing_id")
                        ticker = (
                            ticker_by_lid.get(lid_val)
                            if isinstance(lid_val, int)
                            else None
                        )
                        enriched: dict[str, object] = {
                            **entry,
                            "ticker": ticker or str(lid_val),
                        }
                        out.append(enriched)
                    return out

                scores_by_listing: dict[int, list[dict[str, object]]] = {}
                for s in scores:
                    scores_by_listing.setdefault(s.listing_id, []).append(
                        {
                            "strategy_code": s.strategy_code,
                            "score": s.score,
                            "confidence": float(s.confidence),
                            "bull_thesis": s.bull_thesis,
                            "bear_thesis": s.bear_thesis,
                            "ticker": ticker_by_lid.get(s.listing_id),
                        }
                    )

                rendered = await MarkdownRenderer().render_discovery(
                    run_id=run_id,
                    consensus_picks=_enrich(consensus),
                    divergent_picks=_enrich(divergent),
                    overlooked_picks=_enrich(overlooked),
                    scores_by_listing=scores_by_listing,
                )

                base_dir = self._settings.data_dir / "reports"
                notif = await FileNotifier(base_dir).send(
                    rendered,
                    channel_metadata={
                        "kind": "discovery",
                        "run_id": run_id,
                        "date": as_of.isoformat(),
                    },
                )

                # Save markdown path on the DiscoveryReport row
                dr = await session.get(DiscoveryReport, run_id)
                if dr is not None:
                    dr.markdown_path = notif.target
                    await session.flush()

                log.info(
                    "pipeline.step6.complete",
                    run_id=run_id,
                    markdown_path=notif.target,
                )

            # Step 7: Hit-rate review (compare prior run's picks vs benchmark)
            async with session_scope() as session:
                await steps.step7_hit_rate_review(
                    session,
                    pipeline_run_id=run_id,
                    universe_code=universe_code,
                    as_of=as_of,
                )

            # Mark success.
            async with session_scope() as session:
                await PipelineRunRepository(session).mark_success(run_id)

        except Exception as e:
            log.exception("pipeline.failed", run_id=run_id)
            async with session_scope() as session:
                await PipelineRunRepository(session).mark_failed(run_id, str(e))
            raise

        log.info("pipeline.run.complete", run_id=run_id)
        return run_id
