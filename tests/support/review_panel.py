"""Small builders for multi-perspective review tests."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.review_panel.contracts import (
    Assumption,
    CompanyStance,
    Confidence,
    Dimension,
    Direction,
    Falsifier,
    RoundTrigger,
    SeatOpinion,
    SeatStatus,
    TradeView,
)
from investment_agent.review_panel.service import finalize_round
from investment_agent.storage.repositories.review_panel import (
    ReviewPanelRepository,
    SeatPlan,
)

_FIXED_CUTOFF = datetime(2026, 11, 6, 8, tzinfo=UTC)


def opinion(
    *,
    stance: CompanyStance = CompanyStance.NEUTRAL,
    assumptions: tuple[Assumption, ...] = (),
    falsifiers: tuple[Falsifier, ...] = (),
    open_questions: tuple[str, ...] = (),
    trade_view: TradeView = TradeView.HOLD_WATCH,
    confidence: Confidence = Confidence.MEDIUM,
) -> SeatOpinion:
    return SeatOpinion(
        summary="测试意见。",
        company_stance=stance,
        confidence=confidence,
        trade_view=trade_view,
        assumptions=list(assumptions),
        falsifiers=list(falsifiers),
        open_questions=list(open_questions),
    )


def base_pack(*, ticker: str = "600036.SH", company_name: str = "招商银行") -> dict[str, object]:
    return {
        "schema_version": "review-base-pack.v1",
        "ticker": ticker,
        "listing_id": 1,
        "as_of": "2026-11-06",
        "items": [
            {
                "ref": "base:snapshot",
                "label": "研究快照摘要",
                "data": {"company_name": company_name, "ticker": ticker},
            }
        ],
    }


async def create_round_with_opinions(
    session: AsyncSession,
    listing_id: int,
    opinions: Mapping[str, SeatOpinion],
    *,
    lens_names: Mapping[str, str] | None = None,
    ticker: str = "600036.SH",
    company_name: str = "招商银行",
) -> int:
    """Create one round and write terminal opinions; it is not finalised."""

    lens_names = dict(lens_names or {})
    seat_ids = list(opinions)
    repository = ReviewPanelRepository(session)
    round_row = await repository.create_round(
        listing_id=listing_id,
        trigger=RoundTrigger.MANUAL,
        seats=[
            SeatPlan(
                seat_id=seat_id,
                lens_id=seat_id,
                lens_ref=f"{seat_id}.v1",
                model_ref="primary/reason",
            )
            for seat_id in seat_ids
        ],
        panel_id="default",
        panel_snapshot={
            "schema_version": "review-panel-snapshot.v1",
            "panel_id": "default",
            "name": "测试评审组",
            "seats": [
                {
                    "seat_id": seat_id,
                    "lens_id": seat_id,
                    "lens_ref": f"{seat_id}.v1",
                    "lens_name": lens_names.get(seat_id, seat_id),
                }
                for seat_id in seat_ids
            ],
        },
        as_of=date(2026, 11, 6),
        evidence_cutoff=_FIXED_CUTOFF,
        base_pack=base_pack(ticker=ticker, company_name=company_name),
        base_pack_hash="0" * 64,
        reserved_tokens=0,
        reserved_requests=0,
    )
    detail = await repository.get_round(round_row.id)
    assert detail is not None
    for seat in detail.seats:
        await repository.set_seat_result(
            seat.id,
            SeatStatus.SUCCEEDED,
            opinion_json=opinions[seat.seat_id].model_dump(mode="json"),
            evidence_refs=[],
        )
    return round_row.id


async def add_completed_round(
    session: AsyncSession,
    listing_id: int,
    opinions: Mapping[str, SeatOpinion],
    *,
    lens_names: Mapping[str, str] | None = None,
    ticker: str = "600036.SH",
    company_name: str = "招商银行",
) -> int:
    """Create one round, write opinions and finalise it (status ``completed``)."""

    round_id = await create_round_with_opinions(
        session,
        listing_id,
        opinions,
        lens_names=lens_names,
        ticker=ticker,
        company_name=company_name,
    )
    assert await finalize_round(session, round_id) is True
    return round_id


def divergent_opinions() -> dict[str, SeatOpinion]:
    """Two seats that disagree on valuation (high divergence)."""

    return {
        "value": opinion(
            stance=CompanyStance.POSITIVE,
            assumptions=(
                Assumption(
                    dimension=Dimension.VALUATION,
                    direction=Direction.POSITIVE,
                    text="估值处于合理区间",
                ),
            ),
        ),
        "bear": opinion(
            stance=CompanyStance.NEGATIVE,
            assumptions=(
                Assumption(
                    dimension=Dimension.VALUATION,
                    direction=Direction.NEGATIVE,
                    text="估值已经透支增长",
                ),
            ),
            falsifiers=(
                Falsifier(
                    dimension=Dimension.CASH_FLOW_QUALITY,
                    text="经营现金流转负",
                ),
            ),
            open_questions=("应收增速是否高于营收？",),
        ),
    }
