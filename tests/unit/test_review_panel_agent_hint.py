"""Agent review hint: compact completed-round summary, never evidence."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

import pytest

from investment_agent.review_panel.agent_hint import (
    MAX_FALSIFIERS_PER_SEAT,
    MAX_OPEN_QUESTIONS_PER_SEAT,
    MAX_TEXT_CHARS,
    REVIEW_LEAD_KIND,
    review_hint,
)
from investment_agent.review_panel.contracts import (
    CompanyStance,
    Dimension,
    Falsifier,
    RoundStatus,
    RoundTrigger,
)
from investment_agent.storage.repositories.listing import ListingRepository
from investment_agent.storage.repositories.review_panel import (
    ReviewPanelRepository,
    SeatPlan,
)
from tests.support.review_panel import (
    add_completed_round,
    divergent_opinions,
    opinion,
)


async def _listing(db_session):
    return await ListingRepository(db_session).ensure_a_share_listing(
        "600036.SH", company_name="招商银行"
    )


async def test_hint_has_compact_shape_and_chinese_labels(db_session) -> None:
    listing = await _listing(db_session)
    round_id = await add_completed_round(
        db_session,
        listing.id,
        divergent_opinions(),
        lens_names={"value": "价值", "bear": "空头"},
    )

    hint = await review_hint(db_session, listing.id)

    assert hint is not None
    assert hint["kind"] == REVIEW_LEAD_KIND == "external_review_lead"
    assert hint["not_evidence"] is True
    assert hint["round_id"] == round_id
    assert hint["listing_id"] == listing.id
    assert hint["seat_count"] == 2
    assert hint["completed_at"] is not None
    assert hint["evidence_cutoff"] is not None
    assert hint["divergence_level"] == "high"
    assert hint["divergent_dimensions"] == ["估值水平"]
    assert hint["stance_distribution"] == {"偏正面": 1, "偏负面": 1}
    assert [seat["lens"] for seat in hint["seats"]] == ["价值", "空头"]
    value_seat, bear_seat = hint["seats"]
    assert value_seat["stance"] == "偏正面"
    assert value_seat["trade_view"] == "持有观察"
    assert value_seat["confidence"] == "中"
    assert bear_seat["stance"] == "偏负面"
    assert bear_seat["falsifiers"] == ["经营现金流转负"]
    assert bear_seat["open_questions"] == ["应收增速是否高于营收？"]
    assert hint["falsifiers_by_dimension"] == [
        {
            "dimension": "现金流质量",
            "items": [{"seat_id": "bear", "text": "经营现金流转负"}],
        }
    ]
    # JSON-safe: the payload round-trips and carries no citable source refs.
    encoded = json.dumps(hint, ensure_ascii=False)
    assert "evidence:" not in encoded
    assert "base:" not in encoded


async def test_hint_caps_falsifiers_questions_and_text(db_session) -> None:
    listing = await _listing(db_session)
    long_text = "很长的反证说明" * 40
    opinions = {
        "bear": opinion(
            stance=CompanyStance.NEGATIVE,
            falsifiers=tuple(
                Falsifier(dimension=Dimension.OTHER, text=f"{long_text}{index}")
                for index in range(5)
            ),
            open_questions=tuple(f"{long_text}{index}" for index in range(5)),
        )
    }
    await add_completed_round(db_session, listing.id, opinions)

    hint = await review_hint(db_session, listing.id)

    assert hint is not None
    seat = hint["seats"][0]
    assert len(seat["falsifiers"]) == MAX_FALSIFIERS_PER_SEAT
    assert len(seat["open_questions"]) == MAX_OPEN_QUESTIONS_PER_SEAT
    assert all(len(text) <= MAX_TEXT_CHARS for text in seat["falsifiers"])
    assert seat["falsifiers"][0].endswith("…")


async def test_hint_is_none_without_a_completed_round(db_session) -> None:
    listing = await _listing(db_session)
    assert await review_hint(db_session, listing.id) is None

    # A queued/running round does not qualify.
    await ReviewPanelRepository(db_session).create_round(
        listing_id=listing.id,
        trigger=RoundTrigger.MANUAL,
        seats=[SeatPlan(seat_id="value", lens_id="value", lens_ref="value.v1", model_ref="m")],
        panel_id="default",
        panel_snapshot={"seats": []},
        as_of=date(2026, 11, 6),
        evidence_cutoff=datetime(2026, 11, 6, tzinfo=UTC),
        base_pack_hash="0" * 64,
        reserved_tokens=0,
        reserved_requests=0,
    )
    assert await review_hint(db_session, listing.id) is None


async def test_hint_ignores_a_newer_incomplete_round(db_session) -> None:
    listing = await _listing(db_session)
    completed_id = await add_completed_round(
        db_session, listing.id, divergent_opinions()
    )
    repository = ReviewPanelRepository(db_session)
    queued = await repository.create_round(
        listing_id=listing.id,
        trigger=RoundTrigger.MANUAL,
        seats=[SeatPlan(seat_id="value", lens_id="value", lens_ref="value.v1", model_ref="m")],
        panel_id="default",
        panel_snapshot={"seats": []},
        as_of=date(2026, 11, 6),
        evidence_cutoff=datetime(2026, 11, 6, tzinfo=UTC),
        base_pack_hash="0" * 64,
        reserved_tokens=0,
        reserved_requests=0,
    )
    assert queued.id > completed_id
    assert queued.status == RoundStatus.QUEUED.value

    hint = await review_hint(db_session, listing.id)

    assert hint is not None
    assert hint["round_id"] == completed_id


@pytest.mark.parametrize("completed", [False, True])
async def test_hint_requires_the_stored_comparison(db_session, completed: bool) -> None:
    listing = await _listing(db_session)
    await add_completed_round(db_session, listing.id, divergent_opinions())
    if completed:
        # A completed round always has a comparison; the guard is defensive.
        from sqlalchemy import delete

        from investment_agent.storage.models.review_panel import ReviewComparisonRecord

        await db_session.execute(delete(ReviewComparisonRecord))
        await db_session.flush()
        assert await review_hint(db_session, listing.id) is None
    else:
        assert await review_hint(db_session, listing.id) is not None
