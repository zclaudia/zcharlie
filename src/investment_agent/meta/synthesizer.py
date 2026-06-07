"""Meta-Agent: pure-algorithmic synthesis of consensus / divergent / overlooked picks.

No LLM. Pulls all StockScore rows for a run, categorizes per listing.
"""

from collections import defaultdict
from statistics import mean
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.storage.models.analysis import DiscoveryReport, StockScore


async def synthesize_discovery_report(
    session: AsyncSession,
    run_id: int,
    *,
    consensus_min_score: int = 70,
    consensus_max_spread: int = 20,
    divergent_min_spread: int = 40,
    divergent_bear_max_score: int = 40,
    overlooked_champion_score: int = 80,
    overlooked_other_max: int = 50,
    top_per_bucket: int = 10,
) -> DiscoveryReport:
    """Categorize listings by cross-strategy score patterns. Upserts one DiscoveryReport."""
    stmt = select(StockScore).where(StockScore.pipeline_run_id == run_id)
    rows = list((await session.execute(stmt)).scalars().all())

    by_listing: dict[int, list[StockScore]] = defaultdict(list)
    for r in rows:
        by_listing[r.listing_id].append(r)

    consensus: list[dict[str, Any]] = []
    divergent: list[dict[str, Any]] = []
    overlooked: list[dict[str, Any]] = []

    for listing_id, listing_scores in by_listing.items():
        scores = [s.score for s in listing_scores]
        avg = mean(scores)
        spread = max(scores) - min(scores)
        supporting = [s for s in listing_scores if s.score >= consensus_min_score]
        opposing = [s for s in listing_scores if s.score <= divergent_bear_max_score]

        if (
            len(supporting) >= 2
            and avg >= consensus_min_score
            and spread < consensus_max_spread
        ):
            consensus.append(
                {
                    "listing_id": listing_id,
                    "avg_score": round(avg, 1),
                    "supporting": [s.strategy_code for s in supporting],
                }
            )
            continue

        if spread >= divergent_min_spread and supporting and opposing:
            divergent.append(
                {
                    "listing_id": listing_id,
                    "spread": spread,
                    "bull": [s.strategy_code for s in supporting],
                    "bear": [s.strategy_code for s in opposing],
                }
            )
            continue

        if len(supporting) == 1 and supporting[0].score >= overlooked_champion_score:
            others = [
                s
                for s in listing_scores
                if s.strategy_code != supporting[0].strategy_code
            ]
            if others and all(s.score < overlooked_other_max for s in others):
                overlooked.append(
                    {
                        "listing_id": listing_id,
                        "champion": supporting[0].strategy_code,
                        "champion_score": supporting[0].score,
                    }
                )

    consensus.sort(key=lambda x: x["avg_score"], reverse=True)
    divergent.sort(key=lambda x: x["spread"], reverse=True)
    overlooked.sort(key=lambda x: x["champion_score"], reverse=True)

    consensus_out: list[object] = list(consensus[:top_per_bucket])
    divergent_out: list[object] = list(divergent[:top_per_bucket])
    overlooked_out: list[object] = list(overlooked[:top_per_bucket])

    existing = await session.get(DiscoveryReport, run_id)
    if existing is None:
        report = DiscoveryReport(
            pipeline_run_id=run_id,
            consensus_picks=consensus_out,
            divergent_picks=divergent_out,
            overlooked_picks=overlooked_out,
        )
        session.add(report)
        existing = report
    else:
        existing.consensus_picks = consensus_out
        existing.divergent_picks = divergent_out
        existing.overlooked_picks = overlooked_out
    await session.flush()
    return existing
