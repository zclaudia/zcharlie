"""ReviewPanelRepository: round creation, seat lifecycle, comparisons and quotas."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select

from investment_agent.review_panel.contracts import RoundStatus, RoundTrigger, SeatStatus
from investment_agent.storage.models.reference import Market
from investment_agent.storage.models.review_panel import ReviewComparisonRecord
from investment_agent.storage.repositories.listing import ListingRepository
from investment_agent.storage.repositories.review_panel import (
    ReviewPanelRepository,
    SeatPlan,
)

AS_OF = date(2026, 11, 6)


async def _listing(session, ticker: str = "AAPL"):
    if await session.get(Market, "NASDAQ") is None:
        session.add(
            Market(
                code="NASDAQ",
                name="NASDAQ",
                timezone="America/New_York",
                currency="USD",
            )
        )
        await session.flush()
    return await ListingRepository(session).upsert_listing(
        market_code="NASDAQ", ticker=ticker, company_name=f"{ticker} Inc"
    )


def _seat(seat_id: str = "value") -> SeatPlan:
    return SeatPlan(
        seat_id=seat_id,
        lens_id="value",
        lens_ref="value.v1",
        model_ref="primary/reason",
        model_snapshot={"ref": "primary/reason", "model": "reason-model"},
    )


async def _round(
    repo: ReviewPanelRepository,
    listing_id: int,
    *,
    trigger: RoundTrigger = RoundTrigger.MANUAL,
    trigger_ref: str | None = None,
    tokens: int = 100,
    requests: int = 8,
    seats: list[SeatPlan] | None = None,
):
    return await repo.create_round(
        listing_id=listing_id,
        trigger=trigger,
        seats=seats or [_seat()],
        panel_id="default",
        panel_snapshot={"name": "默认评审组", "seats": []},
        as_of=AS_OF,
        evidence_cutoff=datetime(2026, 11, 6, 15, 0, tzinfo=UTC),
        base_pack_hash="a" * 64,
        reserved_tokens=tokens,
        reserved_requests=requests,
        trigger_ref=trigger_ref,
    )


async def test_create_round_inserts_round_and_seats(db_session) -> None:
    listing = await _listing(db_session)
    repo = ReviewPanelRepository(db_session)
    round_row = await _round(repo, listing.id, seats=[_seat("value"), _seat("bear")])

    detail = await repo.get_round(round_row.id)
    assert detail is not None
    assert detail.round.listing_id == listing.id
    assert detail.round.trigger == "manual"
    assert detail.round.status == "queued"
    assert detail.round.as_of == AS_OF
    assert detail.round.reserved_tokens == 100
    assert [seat.seat_id for seat in detail.seats] == ["value", "bear"]
    assert detail.seats[0].status == "queued"
    assert detail.seats[0].lens_ref == "value.v1"
    assert detail.seats[0].model_snapshot["model"] == "reason-model"
    assert detail.comparison is None


async def test_seat_lifecycle_and_running_count(db_session) -> None:
    listing = await _listing(db_session)
    repo = ReviewPanelRepository(db_session)
    round_row = await _round(repo, listing.id)
    seat = (await repo.get_round(round_row.id)).seats[0]

    assert await repo.mark_seat_running(seat.id) is True
    assert await repo.mark_seat_running(seat.id) is False
    assert await repo.count_running_seats() == 1

    with pytest.raises(ValueError, match="terminal"):
        await repo.set_seat_result(seat.id, SeatStatus.RUNNING)

    assert await repo.set_seat_result(
        seat.id,
        SeatStatus.PARTIAL,
        opinion_json={"summary": "partial"},
        annotations_json={"invalid_refs": []},
        evidence_refs=["snapshot:1"],
        usage={"total_tokens": 900},
        error="token limit",
    ) is True
    assert await repo.count_running_seats() == 0

    detail = await repo.get_round(round_row.id)
    result = detail.seats[0]
    assert result.status == "partial"
    assert result.opinion_json == {"summary": "partial"}
    assert result.evidence_refs == ["snapshot:1"]
    assert result.usage == {"total_tokens": 900}
    assert result.error == "token limit"
    assert result.started_at is not None and result.finished_at is not None


async def test_round_status_latest_and_active_guard(db_session) -> None:
    listing = await _listing(db_session)
    repo = ReviewPanelRepository(db_session)
    round_row = await _round(repo, listing.id)

    assert await repo.active_round_exists(listing.id) is True
    assert await repo.latest_round(listing.id, completed_only=True) is None

    assert await repo.set_round_status(round_row.id, RoundStatus.RUNNING) is True
    assert await repo.active_round_exists(listing.id) is True

    completed_at = datetime(2026, 11, 6, 16, 0, tzinfo=UTC)
    assert await repo.set_round_status(
        round_row.id, RoundStatus.COMPLETED, completed_at=completed_at
    )
    detail = await repo.get_round(round_row.id)
    assert detail.round.status == "completed"
    assert detail.round.completed_at == completed_at
    assert await repo.active_round_exists(listing.id) is False
    latest = await repo.latest_round(listing.id, completed_only=True)
    assert latest is not None and latest.id == round_row.id


async def test_comparison_versions_keep_the_latest_and_replace_same_version(
    db_session,
) -> None:
    listing = await _listing(db_session)
    repo = ReviewPanelRepository(db_session)
    round_row = await _round(repo, listing.id)

    await repo.save_comparison(round_row.id, "cmp.v1", {"divergence": 1})
    await repo.save_comparison(round_row.id, "cmp.v2", {"divergence": 2})
    replaced = await repo.save_comparison(round_row.id, "cmp.v1", {"divergence": 3})
    assert replaced.result_json == {"divergence": 3}

    rows = (
        await db_session.execute(
            select(ReviewComparisonRecord).where(
                ReviewComparisonRecord.round_id == round_row.id
            )
        )
    ).scalars().all()
    assert sorted(row.algorithm_version for row in rows) == ["cmp.v1", "cmp.v2"]
    detail = await repo.get_round(round_row.id)
    assert detail.comparison is not None
    assert detail.comparison.algorithm_version == "cmp.v2"


async def test_list_latest_and_trigger_dedupe_lookups(db_session) -> None:
    aapl = await _listing(db_session, "AAPL")
    msft = await _listing(db_session, "MSFT")
    repo = ReviewPanelRepository(db_session)

    first = await _round(repo, aapl.id, trigger=RoundTrigger.CHANGE, trigger_ref="run-1")
    second = await _round(repo, aapl.id)
    third = await _round(repo, msft.id)

    assert [row.id for row in await repo.list_rounds(aapl.id)] == [second.id, first.id]
    assert [row.id for row in await repo.list_rounds(aapl.id, limit=1)] == [second.id]

    found = await repo.find_round_by_trigger(
        aapl.id, RoundTrigger.CHANGE, "run-1"
    )
    assert found is not None and found.id == first.id
    assert (
        await repo.find_round_by_trigger(aapl.id, RoundTrigger.CHANGE, "run-2")
        is None
    )

    badges = await repo.latest_rounds_for_stocks([aapl.id, msft.id])
    assert badges[aapl.id].id == second.id
    assert badges[msft.id].id == third.id
    assert await repo.latest_rounds_for_stocks([]) == {}


async def test_quota_counters_and_auto_cooldown(db_session) -> None:
    listing = await _listing(db_session)
    repo = ReviewPanelRepository(db_session)
    since = datetime.now(UTC) - timedelta(days=1)

    await _round(repo, listing.id, tokens=100)
    await _round(repo, listing.id, trigger=RoundTrigger.CHANGE, trigger_ref="run-1", tokens=200)
    await _round(repo, listing.id, trigger=RoundTrigger.SCHEDULED, tokens=300)

    assert await repo.count_rounds_since(since) == 3
    assert await repo.count_rounds_since(since, [RoundTrigger.MANUAL]) == 1
    assert await repo.sum_tokens_since(since) == 600
    assert await repo.last_auto_round_at(listing.id) is not None
    assert await repo.count_rounds_since(datetime.now(UTC) + timedelta(days=1)) == 0
