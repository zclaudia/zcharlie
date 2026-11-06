"""Round creation service: rejections, quota reservation, atomic enqueue, base pack."""

from __future__ import annotations

import json
from datetime import date

import pytest
from sqlalchemy import func, select

from investment_agent.config import Settings
from investment_agent.review_panel.contracts import RoundStatus, RoundTrigger, SeatStatus
from investment_agent.review_panel.service import (
    BASE_AGENT_VIEW_REF,
    BASE_CHANGES_REF,
    BASE_SCORE_REF,
    BASE_SNAPSHOT_REF,
    RoundRejected,
    base_pack_hash,
    build_base_pack,
    create_round,
)
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.review_panel import (
    ReviewComparisonRecord,
    ReviewRoundRecord,
    ReviewSeatRunRecord,
)
from investment_agent.storage.repositories.listing import ListingRepository
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository
from investment_agent.storage.repositories.research_snapshot import (
    ResearchSnapshotRepository,
)
from investment_agent.storage.repositories.review_panel import ReviewPanelRepository
from investment_agent.storage.repositories.watchlist import WatchlistRepository
from tests.support.llm_catalog import catalog_values, slot_values

TICKER = "600036.SH"


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "api_key": "fixture",
        "llm_catalog": json.dumps(catalog_values()),
        "model_slots": json.dumps(slot_values()),
        "review_panel_enabled": True,
        "review_panels": _panels(),
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _panels(
    *,
    seats: list[dict[str, str]] | None = None,
    default_panel: str = "default",
) -> str:
    return json.dumps(
        {
            "default_panel": default_panel,
            "panels": {
                "default": {
                    "name": "默认评审组",
                    "seats": seats
                    if seats is not None
                    else [
                        {"seat_id": "value", "lens_id": "value", "model_ref": "primary/reason"},
                        {"seat_id": "bear", "lens_id": "bear", "model_ref": "primary/fast"},
                    ],
                }
            },
        }
    )


async def _listing(db_session, ticker: str = TICKER):
    return await ListingRepository(db_session).ensure_a_share_listing(
        ticker, company_name="招商银行"
    )


async def _snapshot(db_session, listing) -> None:
    run = await PipelineRunRepository(db_session).create(
        kind="ifind_research",
        listing_id=listing.id,
        status="success",
        stage="completed",
    )
    await ResearchSnapshotRepository(db_session).create(
        pipeline_run_id=run.id,
        symbol=listing.ticker,
        market="SH",
        company_name="招商银行",
        industry="银行",
        as_of=date(2026, 11, 6),
        report_year=2025,
        canonical_schema_version="v1",
        scoring_version="quality.v1",
        score_schema_version="v1",
        score_json={
            "investment_score": 72.5,
            "research_confidence_score": 81.0,
            "strategy_label": "quality",
            "needs_agent_review": True,
            "scoring_version": "quality.v1",
            "source_mode": "ifind",
        },
        explanation_json=None,
        artifact_manifest={},
    )


async def _watch(db_session, listing) -> None:
    await WatchlistRepository(db_session).add(listing.id)


async def _ready(db_session, *, ticker: str = TICKER):
    listing = await _listing(db_session, ticker)
    await _watch(db_session, listing)
    await _snapshot(db_session, listing)
    return listing


async def _reject(db_session, settings: Settings, listing_id: int, **kwargs: object):
    kwargs.setdefault("trigger", RoundTrigger.MANUAL)
    with pytest.raises(RoundRejected) as caught:
        await create_round(
            db_session,
            settings,
            listing_id=listing_id,
            **kwargs,
        )
    return caught.value


async def test_disabled(db_session) -> None:
    listing = await _ready(db_session)
    rejected = await _reject(
        db_session, _settings(review_panel_enabled=False), listing.id
    )
    assert rejected.code == "disabled"


async def test_not_on_watchlist(db_session) -> None:
    listing = await _listing(db_session)
    rejected = await _reject(db_session, _settings(), listing.id)
    assert rejected.code == "not_on_watchlist"


async def test_panel_not_configured(db_session) -> None:
    listing = await _ready(db_session)
    rejected = await _reject(db_session, _settings(review_panels=""), listing.id)
    assert rejected.code == "panel_not_configured"


async def test_unknown_panel(db_session) -> None:
    listing = await _ready(db_session)
    rejected = await _reject(
        db_session, _settings(), listing.id, panel_id="missing"
    )
    assert rejected.code == "unknown_panel"


async def test_unknown_seat(db_session) -> None:
    listing = await _ready(db_session)
    rejected = await _reject(
        db_session, _settings(), listing.id, seat_ids=["missing"]
    )
    assert rejected.code == "unknown_seat"


async def test_unknown_lens(db_session) -> None:
    listing = await _ready(db_session)
    rejected = await _reject(
        db_session,
        _settings(
            review_panels=_panels(
                seats=[
                    {"seat_id": "x", "lens_id": "missing", "model_ref": "primary/reason"}
                ]
            )
        ),
        listing.id,
    )
    assert rejected.code == "unknown_lens"


async def test_model_unavailable(db_session) -> None:
    listing = await _ready(db_session)
    rejected = await _reject(
        db_session,
        _settings(
            review_panels=_panels(
                seats=[
                    {"seat_id": "x", "lens_id": "value", "model_ref": "primary/missing"}
                ]
            )
        ),
        listing.id,
    )
    assert rejected.code == "model_unavailable"
    # A rejected round leaves no round/seat/run rows behind.
    assert await db_session.scalar(select(func.count()).select_from(ReviewRoundRecord)) == 0
    assert await db_session.scalar(select(func.count()).select_from(ReviewSeatRunRecord)) == 0
    runs = await db_session.scalar(
        select(func.count()).select_from(PipelineRun).where(PipelineRun.kind == "review_seat")
    )
    assert runs == 0


async def test_no_snapshot(db_session) -> None:
    listing = await _listing(db_session)
    await _watch(db_session, listing)
    rejected = await _reject(db_session, _settings(), listing.id)
    assert rejected.code == "no_snapshot"


async def test_active_round(db_session) -> None:
    listing = await _ready(db_session)
    settings = _settings()
    await create_round(db_session, settings, listing_id=listing.id, trigger=RoundTrigger.MANUAL)
    rejected = await _reject(db_session, settings, listing.id)
    assert rejected.code == "active_round"


async def test_duplicate_trigger(db_session) -> None:
    listing = await _ready(db_session)
    settings = _settings()
    first = await create_round(
        db_session,
        settings,
        listing_id=listing.id,
        trigger=RoundTrigger.CHANGE,
        trigger_ref="change-run-1",
    )
    await ReviewPanelRepository(db_session).set_round_status(
        first.id, RoundStatus.COMPLETED
    )
    rejected = await _reject(
        db_session,
        settings,
        listing.id,
        trigger=RoundTrigger.CHANGE,
        trigger_ref="change-run-1",
    )
    assert rejected.code == "duplicate_trigger"


async def test_daily_rounds_exhausted(db_session) -> None:
    listing = await _ready(db_session)
    first = await create_round(
        db_session, _settings(), listing_id=listing.id, trigger=RoundTrigger.MANUAL
    )
    await ReviewPanelRepository(db_session).set_round_status(
        first.id, RoundStatus.COMPLETED
    )
    rejected = await _reject(
        db_session, _settings(review_panel_daily_rounds=1), listing.id
    )
    assert rejected.code == "daily_rounds_exhausted"


async def test_daily_tokens_exhausted(db_session) -> None:
    listing = await _ready(db_session)
    rejected = await _reject(
        db_session,
        _settings(review_panel_daily_tokens=1, review_panel_seat_tokens=100),
        listing.id,
    )
    assert rejected.code == "daily_tokens_exhausted"


async def test_create_reserves_quota_and_enqueues_every_seat(db_session) -> None:
    listing = await _ready(db_session)
    settings = _settings(
        review_panel_seat_tokens=100,
        review_panel_seat_requests=3,
    )
    round_row = await create_round(
        db_session, settings, listing_id=listing.id, trigger=RoundTrigger.MANUAL
    )
    detail = await ReviewPanelRepository(db_session).get_round(round_row.id)
    assert detail is not None
    assert detail.round.reserved_tokens == 200
    assert detail.round.reserved_requests == 6
    assert [seat.seat_id for seat in detail.seats] == ["value", "bear"]
    assert detail.round.as_of == date(2026, 11, 6)
    assert detail.round.evidence_cutoff.tzinfo is not None

    runs = list(
        (
            await db_session.execute(
                select(PipelineRun)
                .where(PipelineRun.kind == "review_seat")
                .order_by(PipelineRun.id)
            )
        ).scalars()
    )
    assert len(runs) == 2
    assert {run.idempotency_key for run in runs} == {
        f"review_seat:{round_row.id}:value",
        f"review_seat:{round_row.id}:bear",
    }
    assert [seat.pipeline_run_id for seat in detail.seats] == [run.id for run in runs]
    assert {json.dumps(run.request_json, sort_keys=True) for run in runs} == {
        json.dumps(
            {"round_id": round_row.id, "seat_run_id": detail.seats[0].id},
            sort_keys=True,
        ),
        json.dumps(
            {"round_id": round_row.id, "seat_run_id": detail.seats[1].id},
            sort_keys=True,
        ),
    }
    panel = detail.round.panel_snapshot
    assert panel["panel_id"] == "default"
    seats = panel["seats"]
    assert [seat["lens_ref"] for seat in seats] == ["value.v1", "bear.v1"]
    assert all(seat["lens_prompt_hash"] for seat in seats)
    assert all("api_key" not in seat["model_snapshot"] for seat in seats)


async def test_post_insert_quota_recheck_rolls_back_every_row(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    listing = await _ready(db_session)
    settings = _settings(
        review_panel_seat_tokens=100, review_panel_daily_tokens=1000
    )
    calls = {"count": 0}
    original = ReviewPanelRepository.sum_tokens_since

    async def fake_tokens(self, since):  # noqa: ANN001, ANN202
        calls["count"] += 1
        if calls["count"] >= 2:
            return 1_000_000
        return await original(self, since)

    monkeypatch.setattr(ReviewPanelRepository, "sum_tokens_since", fake_tokens)
    with pytest.raises(RoundRejected) as caught:
        await create_round(
            db_session, settings, listing_id=listing.id, trigger=RoundTrigger.MANUAL
        )
    assert caught.value.code == "daily_tokens_exhausted"
    assert await db_session.scalar(select(func.count()).select_from(ReviewRoundRecord)) == 0
    assert await db_session.scalar(select(func.count()).select_from(ReviewSeatRunRecord)) == 0
    runs = await db_session.scalar(
        select(func.count())
        .select_from(PipelineRun)
        .where(PipelineRun.kind == "review_seat")
    )
    assert runs == 0


async def test_quota_counts_this_round_after_creation(db_session) -> None:
    listing = await _ready(db_session)
    settings = _settings(review_panel_seat_tokens=100)
    await create_round(db_session, settings, listing_id=listing.id, trigger=RoundTrigger.MANUAL)
    repository = ReviewPanelRepository(db_session)
    from investment_agent.review_panel.service import _shanghai_day_start

    day_start = _shanghai_day_start()
    assert await repository.count_rounds_since(day_start) == 1
    assert await repository.sum_tokens_since(day_start) == 200


async def test_base_pack_refs_and_stable_hash(db_session) -> None:
    listing = await _ready(db_session)
    snapshot = await ResearchSnapshotRepository(db_session).latest(
        symbol=listing.ticker, market="SH"
    )
    assert snapshot is not None
    from investment_agent.storage.repositories.research_change import (
        ResearchChangeRepository,
    )

    change_set = await ResearchChangeRepository(db_session).by_snapshot(snapshot.id)
    pack = build_base_pack(
        listing_id=listing.id,
        ticker=TICKER,
        snapshot=snapshot,
        change_set=change_set,
        agent_view=None,
    )
    refs = [item["ref"] for item in pack["items"]]
    assert refs == [BASE_SCORE_REF, BASE_SNAPSHOT_REF, BASE_CHANGES_REF]
    assert BASE_AGENT_VIEW_REF not in refs
    assert base_pack_hash(pack) == base_pack_hash(dict(reversed(list(pack.items()))))

    round_row = await create_round(
        db_session, _settings(), listing_id=listing.id, trigger=RoundTrigger.MANUAL
    )
    detail = await ReviewPanelRepository(db_session).get_round(round_row.id)
    assert detail is not None
    assert detail.round.base_pack is not None
    assert detail.round.base_pack_hash == base_pack_hash(detail.round.base_pack)
    assert detail.round.base_pack["items"][0]["ref"] == BASE_SCORE_REF


async def test_daily_rounds_zero_means_unlimited(db_session) -> None:
    listing = await _ready(db_session)
    first = await create_round(
        db_session, _settings(), listing_id=listing.id, trigger=RoundTrigger.MANUAL
    )
    await ReviewPanelRepository(db_session).set_round_status(
        first.id, RoundStatus.COMPLETED
    )
    second = await create_round(
        db_session,
        _settings(review_panel_daily_rounds=0),
        listing_id=listing.id,
        trigger=RoundTrigger.MANUAL,
    )
    assert second.id != first.id


async def test_finalize_round_is_idempotent(db_session) -> None:
    listing = await _ready(db_session)
    round_row = await create_round(
        db_session, _settings(), listing_id=listing.id, trigger=RoundTrigger.MANUAL
    )
    repository = ReviewPanelRepository(db_session)
    detail = await repository.get_round(round_row.id)
    assert detail is not None
    opinion = {
        "summary": "中性。",
        "company_stance": "neutral",
        "confidence": "medium",
        "trade_view": "hold_watch",
        "trade_conditions": [],
        "assumptions": [
            {"dimension": "valuation", "direction": "positive", "text": "估值合理", "evidence_refs": []}
        ],
        "bull_points": [],
        "bear_points": [],
        "falsifiers": [],
        "open_questions": [],
    }
    for seat in detail.seats:
        await repository.set_seat_result(
            seat.id,
            SeatStatus.SUCCEEDED,
            opinion_json=opinion,
            annotations_json={"invalid_refs": [], "numbers": [], "missing_required_dimensions": []},
            evidence_refs=["base:score"],
            usage={"requests": 2, "tool_calls": 1, "input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        )

    from investment_agent.review_panel.service import finalize_round

    assert await finalize_round(db_session, round_row.id) is True
    assert await finalize_round(db_session, round_row.id) is False
    detail = await repository.get_round(round_row.id)
    assert detail is not None
    assert detail.round.status == "completed"
    assert detail.round.settled_usage["requests"] == 4
    assert detail.round.settled_usage["total_tokens"] == 30
    comparisons = (
        await db_session.execute(
            select(ReviewComparisonRecord).where(
                ReviewComparisonRecord.round_id == round_row.id
            )
        )
    ).scalars().all()
    assert len(comparisons) == 1
