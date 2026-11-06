"""``review_seat`` job: tool whitelist, annotations, usage limits and finalisation."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime

import pytest
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RunUsage
from sqlalchemy import select

from investment_agent.config import Settings
from investment_agent.jobs.base import ClaimedJob
from investment_agent.review_panel.contracts import (
    Assumption,
    CompanyStance,
    Confidence,
    Dimension,
    Direction,
    Falsifier,
    RoundStatus,
    RoundTrigger,
    SeatOpinion,
    SeatStatus,
    TradeCondition,
    TradeView,
)
from investment_agent.review_panel.job import (
    REVIEW_TOOL_NAMES,
    ReadRecorder,
    ReviewSeatHandler,
    annotate_opinion,
    build_review_toolset,
)
from investment_agent.review_panel.service import create_round
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.review_panel import ReviewComparisonRecord
from investment_agent.storage.repositories.listing import ListingRepository
from investment_agent.storage.repositories.pipeline_run import (
    PipelineRunRepository,
    RunLease,
)
from investment_agent.storage.repositories.research_snapshot import (
    ResearchSnapshotRepository,
)
from investment_agent.storage.repositories.review_panel import ReviewPanelRepository
from investment_agent.storage.repositories.watchlist import WatchlistRepository
from tests.support.llm_catalog import catalog_values, slot_values

TICKER = "600036.SH"
CUTOFF_TEXT = "2026-11-06T08:00:00+00:00"


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "api_key": "fixture",
        "llm_catalog": json.dumps(catalog_values()),
        "model_slots": json.dumps(slot_values()),
        "review_panel_enabled": True,
        "review_panels": json.dumps(
            {
                "default_panel": "default",
                "panels": {
                    "default": {
                        "name": "默认评审组",
                        "seats": [
                            {
                                "seat_id": "value",
                                "lens_id": "value",
                                "model_ref": "primary/reason",
                            },
                            {
                                "seat_id": "bear",
                                "lens_id": "bear",
                                "model_ref": "primary/fast",
                            },
                        ],
                    }
                },
            }
        ),
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _opinion() -> SeatOpinion:
    return SeatOpinion(
        summary="投资评分 72.5 较高，但营收增长 12.5% 仍需核实。",
        company_stance=CompanyStance.NEUTRAL,
        confidence=Confidence.MEDIUM,
        trade_view=TradeView.HOLD_WATCH,
        trade_conditions=[
            TradeCondition(
                text="若价格低于 10 元则关注买入",
                threshold=10.0,
                basis_ref="base:score",
            )
        ],
        assumptions=[
            Assumption(
                dimension=Dimension.VALUATION,
                direction=Direction.POSITIVE,
                text="估值处于合理区间",
                evidence_refs=["evidence:00000000-0000-0000-0000-000000000000"],
            )
        ],
        bull_points=[],
        bear_points=[],
        falsifiers=[
            Falsifier(dimension=Dimension.CASH_FLOW_QUALITY, text="经营现金流转负")
        ],
        open_questions=["应收增速是否高于营收？"],
    )


def _session_factory(session):
    @asynccontextmanager
    async def factory():
        yield session

    return factory


class _Context:
    worker_id = "test-worker"

    async def run_with_heartbeat(self, lease, work):  # noqa: ANN001, ANN201
        return await work()


async def _ready_round(db_session, settings: Settings):
    listing = await ListingRepository(db_session).ensure_a_share_listing(
        TICKER, company_name="招商银行"
    )
    await WatchlistRepository(db_session).add(listing.id)
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
        score_json={"investment_score": 72.5, "strategy_label": "quality"},
        explanation_json=None,
        artifact_manifest={},
    )
    round_row = await create_round(
        db_session, settings, listing_id=listing.id, trigger=RoundTrigger.MANUAL
    )
    detail = await ReviewPanelRepository(db_session).get_round(round_row.id)
    assert detail is not None
    return listing, round_row, detail


async def _job_for(db_session, seat) -> ClaimedJob:
    run = await db_session.get(PipelineRun, seat.pipeline_run_id)
    assert run is not None
    run.status = "running"
    run.stage = "claimed"
    run.attempt_count = 1
    run.worker_id = "test-worker"
    await db_session.flush()
    return ClaimedJob(
        run_id=run.id,
        kind="review_seat",
        lease=RunLease(run.id, "test-worker", 1),
        listing_id=run.listing_id,
        request_json=dict(run.request_json),
    )


def _tool_then_opinion(messages, info):  # noqa: ANN001, ANN202
    del info
    returned = [
        part
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, ToolReturnPart)
    ]
    if not returned:
        return ModelResponse(
            [
                ToolCallPart(
                    "get_snapshot",
                    {
                        "business": {
                            "ticker": TICKER,
                            "field": "close",
                            "as_of": CUTOFF_TEXT,
                        }
                    },
                )
            ],
            usage=RunUsage(input_tokens=10, output_tokens=5, requests=1, tool_calls=1),
        )
    return ModelResponse(
        [TextPart(_opinion().model_dump_json())],
        usage=RunUsage(input_tokens=10, output_tokens=5, requests=1),
    )


async def _run_seat(db_session, handler, job) -> str:
    await handler.on_claimed(db_session, job)
    return await handler.execute(job, _Context())


async def test_toolset_is_exactly_the_read_only_allowlist(db_session) -> None:
    recorder = ReadRecorder()
    toolset = build_review_toolset(
        sessions=_session_factory(db_session),
        cutoff=datetime.now(UTC),
        recorder=recorder,
        peer_enabled=False,
    )
    assert set(toolset.tools) == set(REVIEW_TOOL_NAMES)
    assert "request_data_refresh" not in toolset.tools
    assert "fetch_disclosure_original" not in toolset.tools
    assert "prepare_company_view" not in toolset.tools


def test_annotations_mark_invalid_refs_and_untraceable_numbers() -> None:
    recorder = ReadRecorder()
    recorder.seed("base:score", {"investment_score": 72.5})
    opinion = _opinion()
    annotations = annotate_opinion(
        opinion,
        lens_required_dimensions=[Dimension.VALUATION, Dimension.CASH_FLOW_QUALITY],
        recorder=recorder,
    )
    assert annotations.invalid_refs
    assert annotations.invalid_refs[0].ref.startswith("evidence:")
    statuses = {(number.path, number.status) for number in annotations.numbers}
    assert ("trade_conditions[0].threshold", "sourced") in statuses
    assert any(
        number.value.rstrip("%") == "12.5" and number.status == "unverified"
        for number in annotations.numbers
    )
    assert any(
        number.value.rstrip("%") == "72.5"
        and number.status == "sourced"
        and number.source_ref == "base:score"
        for number in annotations.numbers
    )
    assert annotations.missing_required_dimensions == [Dimension.CASH_FLOW_QUALITY]


@pytest.mark.asyncio
async def test_seat_happy_path_records_tool_read_and_annotations(db_session) -> None:
    settings = _settings()
    listing, round_row, detail = await _ready_round(db_session, settings)
    seat = detail.seats[0]
    job = await _job_for(db_session, seat)

    factory_calls: list[str] = []

    def factory(resolved, trace):  # noqa: ANN001, ANN202
        factory_calls.append(resolved.ref)
        return FunctionModel(_tool_then_opinion)

    handler = ReviewSeatHandler(
        settings=settings,
        sessions=_session_factory(db_session),
        model_factory=factory,
    )
    outcome = await _run_seat(db_session, handler, job)
    assert outcome == "completed"
    assert factory_calls == ["primary/reason"]

    repository = ReviewPanelRepository(db_session)
    finished = await repository.get_round(round_row.id)
    assert finished is not None
    assert finished.seats[0].status == SeatStatus.SUCCEEDED.value
    assert finished.seats[0].opinion_json is not None
    assert finished.seats[0].evidence_refs == [
        "base:changes",
        "base:score",
        "base:snapshot",
    ]
    annotations = finished.seats[0].annotations_json
    assert annotations is not None
    assert annotations["invalid_refs"]
    assert any(
        number["value"].rstrip("%") == "12.5" and number["status"] == "unverified"
        for number in annotations["numbers"]
    )
    assert any(
        number["path"] == "trade_conditions[0].threshold"
        and number["status"] == "sourced"
        and number["source_ref"] == "base:score"
        for number in annotations["numbers"]
    )
    assert annotations["missing_required_dimensions"] == ["cash_flow_quality"]
    assert finished.seats[0].usage["requests"] >= 2
    run = await db_session.get(PipelineRun, job.run_id)
    assert run is not None and run.status == "success"


@pytest.mark.asyncio
async def test_two_seats_finalise_comparison_exactly_once(db_session) -> None:
    settings = _settings()
    _, round_row, detail = await _ready_round(db_session, settings)

    def factory(resolved, trace):  # noqa: ANN001, ANN202
        del resolved, trace
        return FunctionModel(_tool_then_opinion)

    handler = ReviewSeatHandler(
        settings=settings,
        sessions=_session_factory(db_session),
        model_factory=factory,
    )
    for seat in detail.seats:
        job = await _job_for(db_session, seat)
        await _run_seat(db_session, handler, job)

    from investment_agent.review_panel.service import finalize_round

    assert await finalize_round(db_session, round_row.id) is False
    finished = await ReviewPanelRepository(db_session).get_round(round_row.id)
    assert finished is not None
    assert finished.round.status == RoundStatus.COMPLETED.value
    assert finished.comparison is not None
    assert finished.comparison.algorithm_version == "v1"
    comparisons = (
        await db_session.execute(
            select(ReviewComparisonRecord).where(
                ReviewComparisonRecord.round_id == round_row.id
            )
        )
    ).scalars().all()
    assert len(comparisons) == 1
    assert finished.round.settled_usage["requests"] >= 4


@pytest.mark.asyncio
async def test_usage_limit_marks_partial(db_session) -> None:
    settings = _settings(review_panel_seat_tool_calls=1)
    _, round_row, detail = await _ready_round(db_session, settings)

    def endless_tool(messages, info):  # noqa: ANN001, ANN202
        del messages, info
        return ModelResponse(
            [
                ToolCallPart(
                    "get_snapshot",
                    {
                        "business": {
                            "ticker": TICKER,
                            "field": "close",
                            "as_of": CUTOFF_TEXT,
                        }
                    },
                )
            ],
            usage=RunUsage(input_tokens=5, output_tokens=1, requests=1, tool_calls=1),
        )

    handler = ReviewSeatHandler(
        settings=settings,
        sessions=_session_factory(db_session),
        model_factory=lambda resolved, trace: FunctionModel(endless_tool),
    )
    job = await _job_for(db_session, detail.seats[0])
    outcome = await _run_seat(db_session, handler, job)
    assert outcome == "partial"
    finished = await ReviewPanelRepository(db_session).get_round(round_row.id)
    assert finished is not None
    assert finished.seats[0].status == SeatStatus.PARTIAL.value
    assert finished.seats[0].opinion_json is None
    assert finished.seats[0].error is not None
    assert "用量上限" in finished.seats[0].error
    run = await db_session.get(PipelineRun, job.run_id)
    assert run is not None and run.status == "partial"


@pytest.mark.asyncio
async def test_model_becoming_unavailable_fails_the_seat(db_session) -> None:
    settings = _settings()
    _, round_row, detail = await _ready_round(db_session, settings)
    repository = ReviewPanelRepository(db_session)
    for other in detail.seats[1:]:
        await repository.set_seat_result(
            other.id, SeatStatus.FAILED, error="fixture: other seat skipped"
        )
    disabled = catalog_values()
    disabled["providers"][0]["models"][1]["enabled"] = False
    broken = settings.model_copy(
        update={"llm_catalog": json.dumps(disabled)}
    )
    handler = ReviewSeatHandler(
        settings=broken,
        sessions=_session_factory(db_session),
        model_factory=lambda resolved, trace: pytest.fail("model must not be built"),
    )
    job = await _job_for(db_session, detail.seats[0])
    outcome = await _run_seat(db_session, handler, job)
    assert outcome == "completed"
    finished = await ReviewPanelRepository(db_session).get_round(round_row.id)
    assert finished is not None
    assert finished.seats[0].status == SeatStatus.FAILED.value
    assert finished.seats[0].error is not None
    assert "模型不可用" in finished.seats[0].error
    assert finished.round.status == RoundStatus.FAILED.value
    run = await db_session.get(PipelineRun, job.run_id)
    assert run is not None and run.status == "success"
