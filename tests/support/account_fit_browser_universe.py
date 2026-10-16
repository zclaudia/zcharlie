"""Synthetic 105-member screen source for real account-fit browser pagination.

Call after the disposable browser DB contains its account inputs, portfolio
snapshot, valuation, mandate, and at least one ``RuleTemplateRecord``. Existing
listings are read only; missing ids receive an SSE/CNY listing and CSRC label.
No provider, candidate-intake, or rule-drafting code is invoked.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.rules.screen import (
    MemberResult,
    ScreenConfig,
    ScreenRun,
    TemplateResult,
)
from investment_agent.rules.screen_job import screen_summary
from investment_agent.storage.models.pit_snapshot import IndustryClassification
from investment_agent.storage.models.reference import Company, Listing, Market
from investment_agent.storage.models.rules import RuleTemplateRecord, ScreenResultRecord
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository

DEFAULT_RANKING_UNIVERSE_SIZE = 105


async def seed_ranking_universe(
    session: AsyncSession,
    listing_ids: list[int],
    as_of_date: date | None = None,
) -> tuple[int, str]:
    """Seed an exact complete 105-row screen batch and return ``(run_id, template_id)``."""

    if len(listing_ids) != DEFAULT_RANKING_UNIVERSE_SIZE:
        raise ValueError(f"browser ranking universe must contain exactly {DEFAULT_RANKING_UNIVERSE_SIZE} ids")
    if len(set(listing_ids)) != len(listing_ids) or any(item < 1 for item in listing_ids):
        raise ValueError("listing ids must be unique positive integers")

    now = datetime.now(UTC)
    screen_date = as_of_date or now.date()
    if screen_date > now.date():
        raise ValueError("screen as-of date cannot be in the future")

    market = await session.get(Market, "SSE")
    if market is None or market.currency != "CNY":
        raise ValueError("browser fixture requires an existing SSE market denominated in CNY")

    listings: list[Listing] = []
    for listing_id in listing_ids:
        listing = await session.get(Listing, listing_id)
        if listing is not None:
            existing_market = await session.get(Market, listing.market_code)
            if existing_market is None or existing_market.currency != "CNY":
                raise ValueError(f"existing listing {listing_id} is not on a CNY market")
            listings.append(listing)
            continue

        company = Company(canonical_name=f"Synthetic browser ranking listing {listing_id}")
        session.add(company)
        await session.flush()
        listing = Listing(
            id=listing_id,
            company_id=company.id,
            market_code="SSE",
            ticker=f"{listing_id:06d}.SH",
            is_primary=True,
        )
        session.add(listing)
        await session.flush()
        session.add(
            IndustryClassification(
                listing_id=listing_id,
                system="CSRC",
                code="C39",
                name="计算机、通信和其他电子设备制造业（合成验收数据）",
                valid_from=date(2020, 1, 1),
                valid_to=None,
                source="synthetic_fixture",
                captured_at=now,
            )
        )
        listings.append(listing)

    template = await session.scalar(
        select(RuleTemplateRecord)
        .order_by(
            RuleTemplateRecord.created_at.desc(),
            RuleTemplateRecord.id.asc(),
            RuleTemplateRecord.version.desc(),
        )
        .limit(1)
    )
    if template is None:
        raise ValueError("browser ranking fixture requires an existing RuleTemplateRecord")

    template_id = template.id
    template_version = template.version
    member_results = tuple(
        MemberResult(
            ticker=listing.ticker,
            listing_id=listing.id,
            outcome="hit" if rank <= 100 else "truncated",
            rank=rank,
            strength=round(1.0 - rank / 1000.0, 6),
            reasons=({"factor": "synthetic_fixture", "outcome": "met", "rank": rank},),
            industry_group="synthetic",
        )
        for rank, listing in enumerate(listings, start=1)
    )
    template_result = TemplateResult(
        template_id=template_id,
        template_version=template_version,
        calibration_status=template.calibration_status,
        members=member_results,
        misses_by_condition={},
        missing_by_factor={},
    )
    screen = ScreenRun(
        as_of=screen_date,
        config=ScreenConfig(hit_cap=100),
        member_count=DEFAULT_RANKING_UNIVERSE_SIZE,
        excluded={},
        member_context={
            listing.ticker: {
                "provider": "synthetic_fixture",
                "as_of": screen_date.isoformat(),
                "industry_group": "synthetic",
            }
            for listing in listings
        },
        templates=(template_result,),
    )
    metadata: dict[str, Any] = screen_summary(screen)
    metadata["candidates"] = {
        "created": [],
        "merged": [],
        "skipped_watchlist": [],
        "skipped_cooldown": [],
        "expired": [],
        "drafts_enqueued": 0,
    }
    metadata.update(
        {
            "universe_code": "CSI300",
            "template_sync": [],
            "load_errors": {},
            "result_rows": DEFAULT_RANKING_UNIVERSE_SIZE,
            "timing_seconds": {"factors": 0.0, "screen_and_persist": 0.0},
        }
    )

    run = await PipelineRunRepository(session).create(
        kind="screen_run",
        universe_code="CSI300",
        triggered_by="browser-fixture",
        status="success",
        stage="done",
        request_json={
            "universe_code": "CSI300",
            "as_of": screen_date.isoformat(),
            "sync_templates": False,
            "hit_cap": 100,
        },
    )
    run.finished_at = datetime.now(UTC)
    run.metadata_json = metadata
    for listing, result in zip(listings, member_results, strict=True):
        session.add(
            ScreenResultRecord(
                screen_run_id=run.id,
                as_of=screen_date,
                template_id=template_id,
                template_version=template_version,
                listing_id=listing.id,
                ticker=listing.ticker,
                outcome=result.outcome,
                hit=result.hit,
                rank=result.rank,
                strength=result.strength,
                reasons=[dict(reason) for reason in result.reasons],
                context={
                    "provider": "synthetic_fixture",
                    "as_of": screen_date.isoformat(),
                    "industry_group": "synthetic",
                },
            )
        )
    await session.flush()
    return run.id, template_id
