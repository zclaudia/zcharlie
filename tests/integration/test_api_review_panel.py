"""Multi-perspective review API: auth, error mapping, shapes and leak checks."""

from __future__ import annotations

import json
from datetime import date

from investment_agent.api.auth import get_settings
from investment_agent.config import Settings
from investment_agent.review_panel.contracts import (
    Assumption,
    CompanyStance,
    Confidence,
    Dimension,
    Direction,
    SeatOpinion,
    SeatStatus,
    TradeView,
)
from investment_agent.review_panel.service import finalize_round
from investment_agent.storage.database import session_scope
from investment_agent.storage.repositories.listing import ListingRepository
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository
from investment_agent.storage.repositories.research_snapshot import (
    ResearchSnapshotRepository,
)
from investment_agent.storage.repositories.review_panel import ReviewPanelRepository
from investment_agent.storage.repositories.watchlist import WatchlistRepository
from tests.support.llm_catalog import catalog_values, slot_values

KEY = {"X-API-Key": "secret"}
TICKER = "600036.SH"


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "api_key": "secret",
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
                    },
                    "solo": {
                        "name": "单席",
                        "seats": [
                            {
                                "seat_id": "risk",
                                "lens_id": "risk",
                                "model_ref": "deepseek/chat",
                            }
                        ],
                    },
                },
            }
        ),
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def _use_settings(api_client, settings: Settings) -> None:
    api_client.app.dependency_overrides[get_settings] = lambda: settings


async def _seed_listing(*, watched: bool = True, ticker: str = TICKER) -> int:
    async with session_scope() as session:
        listing = await ListingRepository(session).ensure_a_share_listing(
            ticker, company_name="招商银行"
        )
        if watched:
            await WatchlistRepository(session).add(listing.id)
        run = await PipelineRunRepository(session).create(
            kind="ifind_research",
            listing_id=listing.id,
            status="success",
            stage="completed",
        )
        symbol, market = ticker.split(".")
        await ResearchSnapshotRepository(session).create(
            pipeline_run_id=run.id,
            symbol=symbol,
            market=market,
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
        return listing.id


def _opinion(*, stance: CompanyStance, direction: Direction) -> dict[str, object]:
    return SeatOpinion(
        summary="投资评分 72.5 较高，但营收增长 12.5% 仍需核实。",
        company_stance=stance,
        confidence=Confidence.MEDIUM,
        trade_view=TradeView.HOLD_WATCH,
        assumptions=[
            Assumption(
                dimension=Dimension.VALUATION,
                direction=direction,
                text="估值与增长假设存在分歧",
                evidence_refs=["base:score"],
            )
        ],
        bull_points=[],
        bear_points=[],
        falsifiers=[],
        open_questions=["营收增速能否持续？"],
    ).model_dump(mode="json")


async def _complete_round(round_id: int) -> None:
    async with session_scope() as session:
        repository = ReviewPanelRepository(session)
        detail = await repository.get_round(round_id)
        assert detail is not None
        for index, seat in enumerate(detail.seats):
            await repository.set_seat_result(
                seat.id,
                SeatStatus.SUCCEEDED,
                opinion_json=_opinion(
                    stance=CompanyStance.POSITIVE if index == 0 else CompanyStance.NEGATIVE,
                    direction=Direction.POSITIVE if index == 0 else Direction.NEGATIVE,
                ),
                annotations_json={
                    "invalid_refs": [
                        {
                            "path": "assumptions[0].evidence_refs[0]",
                            "ref": "evidence:not-read",
                            "reason": "该引用不在本席位本轮实际读取的材料中",
                        }
                    ],
                    "numbers": [
                        {"path": "summary", "value": "12.5", "status": "unverified"}
                    ],
                    "missing_required_dimensions": [],
                },
                evidence_refs=["base:score"],
                usage={"requests": 2, "tool_calls": 1, "total_tokens": 3200},
            )
        await finalize_round(session, round_id)


def _create(api_client, listing_id: int, body: dict[str, object] | None = None):
    return api_client.post(
        f"/watchlist/{listing_id}/review-rounds", json=body or {}, headers=KEY
    )


def test_endpoints_require_auth(api_client) -> None:
    assert api_client.get("/review-panels").status_code == 401
    assert api_client.get("/watchlist/1/review-rounds").status_code == 401
    assert api_client.get("/review-rounds/1").status_code == 401
    assert api_client.post("/watchlist/1/review-rounds", json={}).status_code == 401


def test_panels_payload(api_client) -> None:
    _use_settings(api_client, _settings())
    body = api_client.get("/review-panels", headers=KEY).json()
    assert body["enabled"] is True
    assert body["default_panel"] == "default"
    assert body["configuration_error"] is None
    assert [panel["id"] for panel in body["panels"]] == ["default", "solo"]
    default = body["panels"][0]
    assert default["default"] is True
    assert default["seats"][0]["lens_name"] == "价值视角"
    assert default["seats"][0]["model_ref"] == "primary/reason"
    assert default["seats"][0]["lens_focus"]
    assert {lens["id"] for lens in body["lenses"]} >= {"value", "growth", "bear", "risk"}
    # No prompt text crosses the API boundary.
    assert "prompt" not in json.dumps(body)


def test_panels_invalid_config_is_a_readable_payload(api_client) -> None:
    _use_settings(api_client, _settings(review_panels="{"))
    body = api_client.get("/review-panels", headers=KEY).json()
    assert body["panels"] == []
    assert body["configuration_error"]


async def test_create_round_returns_detail_without_prompts_or_credentials(
    api_client,
) -> None:
    listing_id = await _seed_listing()
    _use_settings(api_client, _settings())

    response = _create(api_client, listing_id)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["listing_id"] == listing_id
    assert body["trigger"] == "manual"
    assert body["status"] == "queued"
    assert body["panel_id"] == "default"
    assert body["panel_name"] == "默认评审组"
    assert [seat["seat_id"] for seat in body["seats"]] == ["value", "bear"]
    assert body["seats"][0]["lens_name"] == "价值视角"
    assert body["seats"][0]["lens_ref"] == "value.v1"
    assert body["seats"][0]["model_ref"] == "primary/reason"
    assert body["base_pack"]["schema_version"] == "review-base-pack.v1"
    assert body["comparison"] is None

    raw = json.dumps(body)
    for forbidden in ("prompt", "api_key", "base_url", "sk-", "reason-model"):
        assert forbidden not in raw


async def test_create_round_rejects_active_round_with_409(api_client) -> None:
    listing_id = await _seed_listing()
    _use_settings(api_client, _settings())
    assert _create(api_client, listing_id).status_code == 201
    conflict = _create(api_client, listing_id)
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "active_round"


async def test_error_mapping(api_client) -> None:
    listing_id = await _seed_listing()
    unwatched = await _seed_listing(watched=False, ticker="600000.SH")
    _use_settings(api_client, _settings())
    assert _create(api_client, 99999).status_code == 404
    not_watched = _create(api_client, unwatched)
    assert not_watched.status_code == 404
    assert not_watched.json()["detail"]["code"] == "not_on_watchlist"
    unknown_panel = _create(api_client, listing_id, {"panel_id": "missing"})
    assert unknown_panel.status_code == 422
    assert unknown_panel.json()["detail"]["code"] == "unknown_panel"
    unknown_seat = _create(api_client, listing_id, {"seat_ids": ["missing"]})
    assert unknown_seat.status_code == 422
    assert unknown_seat.json()["detail"]["code"] == "unknown_seat"

    _use_settings(api_client, _settings(review_panel_enabled=False))
    disabled = _create(api_client, listing_id)
    assert disabled.status_code == 422
    assert disabled.json()["detail"]["code"] == "disabled"


async def test_unknown_lens_and_unavailable_model_are_422(api_client) -> None:
    listing_id = await _seed_listing()
    _use_settings(
        api_client,
        _settings(
            review_panels=json.dumps(
                {
                    "default_panel": "default",
                    "panels": {
                        "default": {
                            "name": "坏配置",
                            "seats": [
                                {
                                    "seat_id": "value",
                                    "lens_id": "missing",
                                    "model_ref": "primary/reason",
                                }
                            ],
                        }
                    },
                }
            )
        ),
    )
    assert _create(api_client, listing_id).json()["detail"]["code"] == "unknown_lens"

    _use_settings(
        api_client,
        _settings(
            review_panels=json.dumps(
                {
                    "default_panel": "default",
                    "panels": {
                        "default": {
                            "name": "坏模型",
                            "seats": [
                                {
                                    "seat_id": "value",
                                    "lens_id": "value",
                                    "model_ref": "missing/model",
                                }
                            ],
                        }
                    },
                }
            )
        ),
    )
    assert _create(api_client, listing_id).json()["detail"]["code"] == "model_unavailable"


async def test_list_and_detail_include_comparison_and_annotations(api_client) -> None:
    listing_id = await _seed_listing()
    _use_settings(api_client, _settings())
    round_id = _create(api_client, listing_id).json()["id"]
    await _complete_round(round_id)

    rounds = api_client.get(
        f"/watchlist/{listing_id}/review-rounds", headers=KEY
    ).json()
    assert len(rounds) == 1
    summary = rounds[0]
    assert summary["id"] == round_id
    assert summary["seat_count"] == 2
    assert summary["seat_terminal_count"] == 2
    assert summary["seat_failed_count"] == 0
    assert summary["comparison"]["divergence_level"] == "high"
    assert summary["comparison"]["stance_distribution"] == {
        "positive": 1,
        "negative": 1,
    }
    assert summary["comparison"]["divergent_dimension_count"] == 1

    detail = api_client.get(f"/review-rounds/{round_id}", headers=KEY).json()
    assert detail["status"] == "completed"
    assert detail["completed_at"]
    assert len(detail["seats"]) == 2
    seat = detail["seats"][0]
    assert seat["status"] == "succeeded"
    assert seat["opinion"]["summary"].startswith("投资评分")
    assert seat["annotations"]["numbers"][0]["status"] == "unverified"
    assert seat["evidence_refs"] == ["base:score"]
    assert seat["usage"]["total_tokens"] == 3200
    assert detail["comparison"]["dimension_matrix"]["valuation"]["value"] == "positive"
    assert detail["comparison"]["dimension_matrix"]["valuation"]["bear"] == "negative"
    assert detail["comparison"]["divergent_dimensions"] == ["valuation"]

    raw = json.dumps(detail)
    for forbidden in ("lens_prompt", "prompt", "api_key", "base_url", "sk-"):
        assert forbidden not in raw


async def test_detail_redacts_provider_shaped_seat_errors(api_client) -> None:
    listing_id = await _seed_listing()
    _use_settings(api_client, _settings())
    round_id = _create(api_client, listing_id).json()["id"]
    async with session_scope() as session:
        repository = ReviewPanelRepository(session)
        detail = await repository.get_round(round_id)
        assert detail is not None
        await repository.set_seat_result(
            detail.seats[0].id,
            SeatStatus.FAILED,
            error="provider https://api.example.invalid/v1 api_key=sk-live-secret rejected",
        )
        await repository.set_seat_result(
            detail.seats[1].id,
            SeatStatus.FAILED,
            error="评审席位运行失败",
        )
        await finalize_round(session, round_id)

    body = api_client.get(f"/review-rounds/{round_id}", headers=KEY).json()
    error = body["seats"][0]["error"]
    assert "[已隐去]" in error
    assert "sk-live-secret" not in error
    assert "api.example.invalid" not in error
    assert body["seats"][1]["error"] == "评审席位运行失败"


async def test_watchlist_badge_is_additive_and_has_no_opinions(api_client) -> None:
    listing_id = await _seed_listing()
    _use_settings(api_client, _settings())
    before = api_client.get("/watchlist", headers=KEY).json()
    assert before[0]["latest_review"] is None

    round_id = _create(api_client, listing_id).json()["id"]
    await _complete_round(round_id)
    after = api_client.get("/watchlist", headers=KEY).json()
    badge = after[0]["latest_review"]
    assert badge["round_id"] == round_id
    assert badge["seat_count"] == 2
    assert badge["divergence_level"] == "high"
    assert badge["stance_distribution"] == {"positive": 1, "negative": 1}
    assert "seats" not in badge and "opinion" not in json.dumps(badge)
