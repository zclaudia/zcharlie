"""P1-D3 promote flow at the service level: one transaction, then best-effort
iFinD verification and single-stock research."""

from __future__ import annotations

import sys
import types
from datetime import date
from typing import Any

import pytest
from sqlalchemy import select

from investment_agent.candidates import promote as promote_module
from investment_agent.candidates.lifecycle import CandidateConflictError
from investment_agent.candidates.promote import (
    promote_candidate,
    run_promotion,
    watch_metadata,
)
from investment_agent.config import Settings
from investment_agent.rules.validation import RuleValidationError
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.candidates import CandidateRecord, DecisionRecord
from investment_agent.storage.models.rules import RuleInstanceRecord
from investment_agent.storage.models.user_domain import WatchlistItem
from investment_agent.storage.repositories.candidate import DecisionRepository
from tests.unit._candidate_support import (
    cand_db,  # noqa: F401 - fixture
    seed_classification,
    seed_listing,
    seed_promotable,
)

TODAY = date(2026, 10, 7)

pytestmark = pytest.mark.usefixtures("cand_db")


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "api_key": "x" * 32,
        "llm_tier3_api_key": "x",
        "ifind_proxy_api_key": "proxy-key",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[call-arg]


@pytest.fixture
def fake_verification(monkeypatch):
    calls: list[dict[str, Any]] = []
    module = types.ModuleType("investment_agent.verification")

    async def enqueue_verification(**kwargs: Any) -> None:
        calls.append(kwargs)

    module.enqueue_verification = enqueue_verification  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "investment_agent.verification", module)
    return calls


async def _rules() -> dict[int, RuleInstanceRecord]:
    async with session_scope() as session:
        return {
            row.id: row for row in (await session.execute(select(RuleInstanceRecord))).scalars()
        }


async def _decision_kinds(candidate_id: int) -> list[str]:
    async with session_scope() as session:
        return [row.kind for row in await DecisionRepository(session).for_candidate(candidate_id)]


async def test_promote_happy_path(fake_verification) -> None:
    ids = await seed_promotable()
    outcome = await run_promotion(
        ids["candidate_id"], reason="ROE 稳定，估值合理", settings=_settings(), today=TODAY
    )
    assert outcome.warnings == []
    assert outcome.rule.id == ids["draft_rule_id"]
    assert outcome.previous_active_id == ids["previous_rule_id"]
    rules = await _rules()
    assert rules[ids["draft_rule_id"]].status == "active"
    assert rules[ids["draft_rule_id"]].approved_by == "user"
    assert rules[ids["previous_rule_id"]].status == "retired"

    async with session_scope() as session:
        candidate = await session.get(CandidateRecord, ids["candidate_id"])
        assert candidate is not None and candidate.status == "promoted"
        assert candidate.draft_rule_instance_id == ids["draft_rule_id"]
        item = (await session.execute(select(WatchlistItem))).scalar_one()
        assert (item.industry, item.report_year, item.enabled, item.provider) == (
            "食品饮料",
            2025,
            True,
            "auto",
        )
        research = (
            await session.execute(select(PipelineRun).where(PipelineRun.kind == "ifind_research"))
        ).scalar_one()
        assert research.id == outcome.research_run_id
        assert research.listing_id == ids["listing_id"]
        assert research.idempotency_key == f"promote:{ids['candidate_id']}"
        assert research.request_json == {
            "ticker": "600519.SH",
            "industry": "食品饮料",
            "report_year": 2025,
            "as_of": "2026-10-07",
            "provider": "snapshot",
        }
        assert item.last_research_run_id == research.id and item.last_refresh_status == "queued"
        promote_decision = (
            await session.execute(select(DecisionRecord).where(DecisionRecord.kind == "promote"))
        ).scalar_one()
        assert promote_decision.reason_text == "ROE 稳定，估值合理"
        assert promote_decision.rule_instance_id == ids["draft_rule_id"]
        assert promote_decision.context["watchlist"]["industry_source"] == "sw2021_l1"
        assert promote_decision.context["screen_results"][0]["reasons"]

    assert await _decision_kinds(ids["candidate_id"]) == ["nominate", "approve_rule", "promote"]
    assert fake_verification == [
        {
            "listing_id": ids["listing_id"],
            "ticker": "600519.SH",
            "trigger": "promote",
            "fields": None,
            "report_period": None,
            "detail": {
                "candidate_id": ids["candidate_id"],
                "rule_instance_id": ids["draft_rule_id"],
                "rule_ref": "600519.SH/candidate/v2 (cand_test@1)",
                "as_of": "2026-10-07",
            },
            "run_id": None,
        }
    ]
    assert outcome.verification_requested


async def test_verification_missing_and_research_failure_do_not_roll_back(
    monkeypatch
) -> None:
    monkeypatch.setitem(sys.modules, "investment_agent.verification", None)

    async def broken(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("queue down")

    monkeypatch.setattr(promote_module, "enqueue_single_stock_research", broken)
    ids = await seed_promotable()
    outcome = await run_promotion(ids["candidate_id"], settings=_settings(), today=TODAY)
    assert not outcome.verification_requested and outcome.research_run_id is None
    assert any("verification" in warning for warning in outcome.warnings)
    assert any("queue down" in warning for warning in outcome.warnings)
    async with session_scope() as session:
        candidate = await session.get(CandidateRecord, ids["candidate_id"])
        assert candidate is not None and candidate.status == "promoted"


async def test_verification_error_is_a_warning(monkeypatch) -> None:
    module = types.ModuleType("investment_agent.verification")

    def enqueue_verification(**kwargs: Any) -> None:
        raise RuntimeError("ledger locked")

    module.enqueue_verification = enqueue_verification  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "investment_agent.verification", module)
    ids = await seed_promotable()
    outcome = await run_promotion(
        ids["candidate_id"], settings=_settings(ifind_proxy_api_key=""), today=TODAY
    )
    assert any("ledger locked" in warning for warning in outcome.warnings)
    # The research run reads the free-source snapshot store: no iFinD key needed.
    assert not any("IFIND_PROXY_API_KEY" in warning for warning in outcome.warnings)
    assert outcome.research_run_id is not None


async def test_promote_without_rule_is_conflict() -> None:
    ids = await seed_promotable(previous_active=False, draft=False)
    async with session_scope() as session:
        with pytest.raises(CandidateConflictError, match="drafting not ready"):
            await promote_candidate(session, ids["candidate_id"], today=TODAY)


async def test_promote_falls_back_to_active_rule(fake_verification) -> None:
    ids = await seed_promotable(previous_active=True, draft=False)
    outcome = await run_promotion(ids["candidate_id"], settings=_settings(), today=TODAY)
    assert outcome.rule.id == ids["previous_rule_id"] and outcome.previous_active_id is None
    assert await _decision_kinds(ids["candidate_id"]) == ["nominate", "promote"]


async def test_params_override(fake_verification) -> None:
    ids = await seed_promotable()
    async with session_scope() as session:
        with pytest.raises(RuleValidationError) as error:
            await promote_candidate(
                session, ids["candidate_id"], params_override={"pe_pct_max": 0.95}, today=TODAY
            )
        assert "param_out_of_range" in error.value.codes
    outcome = await run_promotion(
        ids["candidate_id"], params_override={"pe_pct_max": 0.2}, settings=_settings(), today=TODAY
    )
    rules = await _rules()
    new = rules[outcome.rule.id]
    assert new.version == 3 and new.status == "active" and new.drafted_by == "user"
    assert new.definition["params"] == {"pe_pct_max": 0.2}
    assert rules[ids["draft_rule_id"]].status == "retired"
    assert rules[ids["previous_rule_id"]].status == "retired"
    assert await _decision_kinds(ids["candidate_id"]) == [
        "nominate",
        "edit_rule",
        "approve_rule",
        "promote",
    ]


async def test_promote_is_atomic(monkeypatch) -> None:
    ids = await seed_promotable()
    original = DecisionRepository.append

    async def fail_on_promote(self, **kwargs: Any):  # noqa: ANN001, ANN202
        if kwargs["kind"] == "promote":
            raise RuntimeError("journal down")
        return await original(self, **kwargs)

    monkeypatch.setattr(DecisionRepository, "append", fail_on_promote)
    with pytest.raises(RuntimeError, match="journal down"):
        await run_promotion(ids["candidate_id"], settings=_settings(), today=TODAY)
    rules = await _rules()
    assert rules[ids["draft_rule_id"]].status == "draft"
    assert rules[ids["previous_rule_id"]].status == "active"
    async with session_scope() as session:
        candidate = await session.get(CandidateRecord, ids["candidate_id"])
        assert candidate is not None and candidate.status == "open"
        assert (await session.execute(select(WatchlistItem))).first() is None
        assert (
            await session.execute(select(PipelineRun).where(PipelineRun.kind == "ifind_research"))
        ).first() is None
    assert await _decision_kinds(ids["candidate_id"]) == ["nominate"]


async def test_watch_metadata_fallbacks() -> None:
    async with session_scope() as session:
        listing = await seed_listing(session, "600900.SH", "长江电力")
        await seed_classification(session, listing.id, shenwan=False)
        meta = await watch_metadata(session, listing.id, date(2026, 3, 1))
    assert meta.industry == "酒、饮料和精制茶制造业" and meta.industry_source == "csrc"
    # No annual snapshot: before 04-30 the year before last, after it last year.
    assert (meta.report_year, meta.report_year_source) == (2024, "deadline_fallback")
    async with session_scope() as session:
        meta = await watch_metadata(session, listing.id, date(2026, 5, 2))
    assert meta.report_year == 2025
    async with session_scope() as session:
        bare = await seed_listing(session, "601398.SH", "工商银行")
        meta = await watch_metadata(session, bare.id, date(2026, 5, 2))
    assert meta.industry is None and meta.industry_source is None


async def test_promote_queues_a_real_ifind_verification_request() -> None:
    # Integration with P1-A13: no fake module, the real queue is used.
    from investment_agent.storage.models.verification import VerificationRequest

    ids = await seed_promotable()
    outcome = await run_promotion(ids["candidate_id"], settings=_settings(), today=TODAY)
    assert outcome.verification_requested
    async with session_scope() as session:
        request = (await session.execute(select(VerificationRequest))).scalar_one()
    assert (request.listing_id, request.ticker, request.trigger, request.status) == (
        ids["listing_id"],
        "600519.SH",
        "promote",
        "pending",
    )
    assert request.detail["candidate_id"] == ids["candidate_id"]
