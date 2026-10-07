"""P1-C5 offline eval: LLM rule drafting against a scripted stub LLM.

The stub is the OpenAI-compatible HTTP endpoint mocked with respx, so the
real client (and its ``llm_call`` cost logging) runs; no real LLM is called.
Fixed inputs cover five stocks and all five shipped templates.

Asserted contract (P1-C5): valid drafts are stored and pass rule validation
100%; invalid drafts (out-of-range / unknown param, citation of a missing
factor, trading language, malformed JSON) are never stored; the retry path
is used exactly once; every call writes an ``llm_call`` row.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import socket
import sys
import types
from collections.abc import Callable
from datetime import date
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import func, select
from typer.testing import CliRunner

from investment_agent.config import Settings
from investment_agent.factors.catalog import DEFAULT_REGISTRY
from investment_agent.factors.registry import FactorValue
from investment_agent.research.worker import ResearchWorker, default_job_handlers
from investment_agent.rules import drafting
from investment_agent.rules.draft_policy import trading_terms
from investment_agent.rules.drafting import (
    RULE_DRAFT,
    RuleDraftHandler,
    RuleDraftInputError,
    RuleDraftPayload,
    StockContext,
    draft_rule,
    enqueue_rule_draft,
    notify_draft_result,
    rule_draft_idempotency_key,
)
from investment_agent.rules.schema import RuleTemplate
from investment_agent.rules.store import RuleStore, instance_from_record
from investment_agent.rules.templates import load_templates
from investment_agent.rules.validation import check_instance
from investment_agent.storage.database import get_engine, init_engine, session_scope
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.base import Base
from investment_agent.storage.models.ops import LLMCall
from investment_agent.storage.models.rules import RuleInstanceRecord
from investment_agent.storage.repositories.listing import ListingRepository

BASE_URL = "https://llm.stub.test/v1"
AS_OF = date(2026, 9, 30)

# Fixed inputs: one stock per template (factor values are fixed numbers, not
# live data; ``None`` -> quality missing).
STOCKS: dict[str, dict[str, Any]] = {
    "600900.SH": {
        "template": "dividend_blue_chip",
        "industry": {"group": "utility", "shenwan": {"code": "410101", "name": "水力发电"}},
        "factors": {
            "roe": 0.16, "roe_stability": 0.93, "net_profit_growth_3y": 0.07,
            "market_cap": 7.0e11, "volatility_1y": 0.18, "dividend_yield_ttm": 0.034,
            "pb_percentile_5y": 0.62, "pe_percentile_5y": 0.55, "debt_ratio": 0.56,
            "gross_margin": 0.58, "is_st": 0.0, "report_overdue": 0.0,
        },
    },
    "600519.SH": {
        "template": "quality_growth",
        "industry": {"group": "consumer", "shenwan": {"code": "340501", "name": "白酒Ⅲ"}},
        "factors": {
            "roe": 0.36, "roe_stability": 0.95, "revenue_growth_3y": 0.16,
            "net_profit_growth_3y": 0.15, "gross_margin": 0.92, "pe_percentile_5y": 0.08,
            "pb_percentile_5y": 0.05, "market_cap": 1.8e12, "is_st": 0.0,
        },
    },
    "600276.SH": {
        "template": "steady_quality_growth",
        "industry": {"group": "healthcare", "shenwan": {"code": "370101", "name": "化学制剂"}},
        "factors": {
            "roe": 0.14, "roe_stability": 0.82, "revenue_growth_3y": 0.09,
            "net_profit_growth_3y": 0.08, "net_margin": 0.22, "debt_ratio": 0.12,
            "volatility_1y": 0.30, "pe_percentile_5y": 0.41, "pb_percentile_5y": 0.37,
        },
    },
    "601899.SH": {
        "template": "cycle_reversal",
        "industry": {"group": "cyclical", "shenwan": {"code": "240301", "name": "铜"}},
        "factors": {
            "roe": 0.22, "max_drawdown_1y": 0.34, "pb_percentile_5y": 0.21,
            "pe_percentile_5y": 0.30, "market_cap": 4.5e11, "debt_ratio": 0.58,
        },
    },
    "688981.SH": {
        "template": "high_growth_venture",
        "industry": {"group": "venture", "shenwan": {"code": "270108", "name": "集成电路制造"}},
        "factors": {
            "revenue_growth_3y": 0.27, "gross_margin": 0.33, "market_cap": 9.0e10,
            "pb_percentile_5y": 0.48, "pe_percentile_5y": None, "roe": 0.04,
        },
    },
}


def _factor_values(values: dict[str, float | None]) -> dict[str, FactorValue]:
    return {
        factor.name: FactorValue(
            factor.name,
            factor.version,
            values.get(factor.name),
            "ok" if values.get(factor.name) is not None else "missing",
        )
        for factor in DEFAULT_REGISTRY
    }


async def stub_loader(session, ticker: str, as_of: date) -> StockContext:  # noqa: ANN001
    stock = STOCKS.get(ticker)
    if stock is None:
        raise LookupError(f"no listing for {ticker}")
    return StockContext(factors=_factor_values(stock["factors"]), industry=stock["industry"])


# --------------------------------------------------------------------------- #
# Stub LLM outputs
# --------------------------------------------------------------------------- #


def _buy_param(template: RuleTemplate) -> str:
    return next(name for name in template.params if name.startswith("buy_"))


def _cited(template: RuleTemplate) -> str:
    return "pe_percentile_5y" if "_pe_" in _buy_param(template) else "pb_percentile_5y"


def valid_output(template: RuleTemplate) -> dict[str, Any]:
    """Tighten the buy_zone valuation parameter halfway towards its minimum."""

    name = _buy_param(template)
    bounds = template.param_ranges[name]
    value = round((bounds.min + template.params[name]) / 2, 4)
    return {
        "params": {name: value},
        "rationale": [
            {
                "param": name,
                "choice": "adjusted",
                "reason": f"{_cited(template)} 显示估值处于自身历史区间的位置，收紧 buy_zone 参数，"
                "使候选区间只在估值更低时触发，属研究优先级假设。",
                "cited_factors": [_cited(template)],
            },
            {
                "param": next(iter(template.params)),
                "choice": "default",
                "reason": "模板未校准，暂无依据调整，沿用默认值。",
                "cited_factors": [],
            },
        ],
        "overall_rationale": "沿用模板全部条件，只收紧 buy_zone 的估值分位参数；模板未校准。",
        "watch_points": [f"{_cited(template)} 回升到 drop 条件附近时需要复核。"],
    }


def out_of_range(template: RuleTemplate) -> dict[str, Any]:
    data = valid_output(template)
    name = _buy_param(template)
    data["params"] = {name: template.param_ranges[name].max + 1.0}
    return data


def unknown_param(template: RuleTemplate) -> dict[str, Any]:
    data = valid_output(template)
    data["params"]["leverage_max"] = 2.0
    data["rationale"].append(
        {"param": "leverage_max", "choice": "adjusted", "reason": "x", "cited_factors": ["roe"]}
    )
    return data


def null_citation(template: RuleTemplate) -> dict[str, Any]:
    data = valid_output(template)
    data["rationale"][0]["cited_factors"] = ["free_cash_flow"]  # missing for every stock
    return data


def trading_language(template: RuleTemplate) -> dict[str, Any]:
    data = valid_output(template)
    data["overall_rationale"] = "估值低位，建议逢低买入并设置止损。"
    return data


MALFORMED = "这不是 JSON {params: 0.2"


class StubLLM:
    """Scripted chat-completions endpoint: one queued content per call."""

    def __init__(self, contents: list[str | dict[str, Any]]) -> None:
        self.contents = list(contents)
        self.requests: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        if not self.contents:
            raise AssertionError("stub LLM called more often than scripted")
        content = self.contents.pop(0)
        text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
        return httpx.Response(
            200,
            json={
                "id": "stub",
                "object": "chat.completion",
                "created": 1,
                "model": "stub-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": text},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1200, "completion_tokens": 300, "total_tokens": 1500},
            },
        )

    @property
    def user_messages(self) -> list[str]:
        return [request["messages"][1]["content"] for request in self.requests]


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def draft_db(tmp_path, monkeypatch) -> dict[str, RuleTemplate]:
    db_path = tmp_path / "draft.db"
    url = f"sqlite+aiosqlite:///{db_path}"
    for key, value in {
        "API_KEY": "secret",
        "LLM_TIER3_API_KEY": "stub-key",
        "LLM_TIER3_BASE_URL": BASE_URL,
        "LLM_TIER3_MODEL": "stub-model",
        "DATA_DIR": str(tmp_path),
        "DATABASE_URL": url,
        "COLUMNS": "200",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(drafting, "load_stock_context", stub_loader)

    async def create() -> dict[str, RuleTemplate]:
        engine = init_engine(url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with session_scope() as session:
            await RuleStore(session).sync_templates(load_templates())
            for ticker in STOCKS:
                await ListingRepository(session).ensure_a_share_listing(ticker)
        async with session_scope() as session:
            templates = {t.id: t for t in await RuleStore(session).latest_templates()}
        await engine.dispose()
        return templates

    return asyncio.run(create())


@pytest.fixture
def hook_calls(monkeypatch) -> list[dict[str, Any]]:
    """A fake candidate module exposing ``mark_draft_result``."""

    calls: list[dict[str, Any]] = []
    module = types.ModuleType("investment_agent.candidates.service")

    async def mark_draft_result(candidate_id: int, **kwargs: Any) -> None:
        calls.append({"candidate_id": candidate_id, **kwargs})

    module.mark_draft_result = mark_draft_result  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "investment_agent.candidates.service", module)
    monkeypatch.setattr(drafting, "_HOOK_MODULES", ("investment_agent.candidates.service",))
    return calls


def test_completion_seam_matches_candidate_lifecycle_signature() -> None:
    # The real hook must accept exactly what notify_draft_result passes.
    from investment_agent.candidates import lifecycle

    assert drafting._resolve_mark_draft_result() is lifecycle.mark_draft_result
    signature = inspect.signature(lifecycle.mark_draft_result)
    signature.bind(1, rule_instance_id=2, failed=False, error=None)


def _settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


def _payload(ticker: str, templates: dict[str, RuleTemplate], **kw: Any) -> RuleDraftPayload:
    template = templates[STOCKS[ticker]["template"]]
    data: dict[str, Any] = {
        "candidate_id": 11,
        "ticker": ticker,
        "template_id": template.id,
        "template_version": template.version,
        "as_of": AS_OF,
        "preference_notes": ["不喜欢高负债的公司"],
    }
    data.update(kw)
    return RuleDraftPayload(**data)


async def _llm_calls() -> list[LLMCall]:
    async with session_scope() as session:
        return list((await session.execute(select(LLMCall).order_by(LLMCall.id))).scalars())


async def _instance_count() -> int:
    async with session_scope() as session:
        return int(
            (await session.execute(select(func.count()).select_from(RuleInstanceRecord))).scalar_one()
        )


def _run(scenario: Callable[[], Any]) -> Any:
    async def wrapped() -> Any:
        try:
            return await scenario()
        finally:
            # Close pooled aiosqlite connections inside this event loop.
            await get_engine().dispose()

    return asyncio.run(wrapped())


# --------------------------------------------------------------------------- #
# Eval
# --------------------------------------------------------------------------- #


def test_valid_drafts_for_all_five_templates_pass_validation(draft_db) -> None:
    templates = draft_db
    assert {stock["template"] for stock in STOCKS.values()} == set(templates)

    async def scenario() -> None:
        for ticker, stock in STOCKS.items():
            template = templates[stock["template"]]
            stub = StubLLM([valid_output(template)])
            with respx.mock(assert_all_called=True) as router:
                router.post(f"{BASE_URL}/chat/completions").mock(side_effect=stub)
                result = await draft_rule(_payload(ticker, templates), settings=_settings())
            assert result.status == "drafted" and result.attempts == 1, result.issues
            # The prompt carries the template, every factor, industry and notes.
            sent = json.loads(stub.user_messages[0])
            assert sent["template"]["ref"] == template.ref
            assert set(sent["factors"]) == set(DEFAULT_REGISTRY.names())
            assert sent["stock"]["industry"]["group"] == stock["industry"]["group"]
            assert sent["preference_notes"] == ["不喜欢高负债的公司"]
            assert "不是投资建议" in stub.requests[0]["messages"][0]["content"]
            assert stub.requests[0]["response_format"] == {"type": "json_object"}

        async with session_scope() as session:
            records = await RuleStore(session).instances(status="draft")
            assert len(records) == len(STOCKS)
            for record in records:
                instance = instance_from_record(record)
                template = templates[record.template_id]
                assert check_instance(instance, template) == []  # 100% valid
                assert record.scope == "candidate" and record.drafted_by == "llm:stub-model"
                for name, value in instance.params.items():
                    bounds = template.param_ranges[name]
                    assert bounds.min <= value <= bounds.max
                rationale = json.loads(record.draft_rationale)
                assert rationale["schema"] == "rule_draft.v1"
                assert rationale["candidate_id"] == 11
                texts = [rationale["overall_rationale"], *rationale["watch_points"]]
                texts += [item["reason"] for item in rationale["params"].values()]
                assert not any(trading_terms(text) for text in texts)
        calls = await _llm_calls()
        assert len(calls) == len(STOCKS)
        assert {call.purpose for call in calls} == {"rule_draft"}
        assert all(call.success and call.cost_usd > 0 for call in calls)

    _run(scenario)


INVALID_SCRIPTS: dict[str, Callable[[RuleTemplate], dict[str, Any] | str]] = {
    "out_of_range": out_of_range,
    "unknown_param": unknown_param,
    "null_citation": null_citation,
    "trading_language": trading_language,
    "malformed_json": lambda template: MALFORMED,
}
EXPECTED_CODE = {
    "out_of_range": "param_out_of_range",
    "unknown_param": "unknown_param",
    "null_citation": "citation_null",
    "trading_language": "trading_language",
    "malformed_json": "malformed_output",
}


@pytest.mark.parametrize("case", sorted(INVALID_SCRIPTS))
def test_invalid_drafts_are_retried_once_then_never_stored(draft_db, case) -> None:
    templates = draft_db
    ticker = "600519.SH"
    template = templates[STOCKS[ticker]["template"]]
    bad = INVALID_SCRIPTS[case](template)

    async def scenario() -> None:
        stub = StubLLM([bad, bad])
        with respx.mock(assert_all_called=True) as router:
            router.post(f"{BASE_URL}/chat/completions").mock(side_effect=stub)
            result = await draft_rule(_payload(ticker, templates), settings=_settings(), run_id=None)
        assert result.status == "unavailable" and result.rule_instance_id is None
        assert result.attempts == 2 and len(stub.requests) == 2
        assert all(any(EXPECTED_CODE[case] in issue for issue in issues) for issues in result.issues)
        assert result.error is not None and result.error.startswith("draft_unavailable")
        # The retry carries the issues of the first attempt.
        assert "上一次输出未通过校验" not in stub.user_messages[0]
        assert "上一次输出未通过校验" in stub.user_messages[1]
        assert EXPECTED_CODE[case] in stub.user_messages[1]
        assert await _instance_count() == 0
        calls = await _llm_calls()
        assert [call.purpose for call in calls] == ["rule_draft", "rule_draft"]
        assert all(call.success == (case != "malformed_json") for call in calls)

    _run(scenario)


def test_retry_with_feedback_recovers(draft_db) -> None:
    templates = draft_db
    ticker = "601899.SH"
    template = templates["cycle_reversal"]

    async def scenario() -> None:
        stub = StubLLM([out_of_range(template), valid_output(template)])
        with respx.mock() as router:
            router.post(f"{BASE_URL}/chat/completions").mock(side_effect=stub)
            result = await draft_rule(_payload(ticker, templates), settings=_settings())
        assert result.status == "drafted" and result.attempts == 2
        assert len(result.issues) == 1 and "param_out_of_range" in result.issues[0][0]
        assert await _instance_count() == 1
        assert len(await _llm_calls()) == 2

    _run(scenario)


def test_rerun_reuses_an_equivalent_draft(draft_db) -> None:
    templates = draft_db
    ticker = "600900.SH"
    template = templates["dividend_blue_chip"]

    async def scenario() -> None:
        stub = StubLLM([valid_output(template), valid_output(template)])
        with respx.mock() as router:
            router.post(f"{BASE_URL}/chat/completions").mock(side_effect=stub)
            first = await draft_rule(_payload(ticker, templates), settings=_settings(), run_id=None)
            again = await draft_rule(_payload(ticker, templates), settings=_settings(), run_id=None)
        assert first.status == "drafted" and again.status == "reused"
        assert again.rule_instance_id == first.rule_instance_id
        assert await _instance_count() == 1

    _run(scenario)


def _worker(handler: RuleDraftHandler) -> ResearchWorker:
    return ResearchWorker(
        settings=_settings(),
        worker_id=f"test:{socket.gethostname()}:{os.getpid()}",
        handlers=[handler],
        retry_base_delay_seconds=0.0,
    )


async def _enqueue(payload: RuleDraftPayload) -> int:
    async with session_scope() as session:
        run, created = await enqueue_rule_draft(session, payload, triggered_by="test")
        assert created
        assert run.idempotency_key == rule_draft_idempotency_key(payload)
        return run.id


def test_worker_job_success_and_partial_notify_the_candidate(draft_db, hook_calls) -> None:
    templates = draft_db
    good = _payload("600276.SH", templates, candidate_id=21)
    bad = _payload("688981.SH", templates, candidate_id=22)

    async def scenario() -> None:
        good_id = await _enqueue(good)
        bad_id = await _enqueue(bad)
        stub = StubLLM(
            [
                valid_output(templates[good.template_id]),
                trading_language(templates[bad.template_id]),
                trading_language(templates[bad.template_id]),
            ]
        )
        handler = RuleDraftHandler(settings=_settings())
        with respx.mock() as router:
            router.post(f"{BASE_URL}/chat/completions").mock(side_effect=stub)
            await _worker(handler).run_once(run_id=good_id)
            await _worker(handler).run_once(run_id=bad_id)
        async with session_scope() as session:
            ok = await session.get(PipelineRun, good_id)
            partial = await session.get(PipelineRun, bad_id)
            assert ok is not None and ok.status == "success", ok.error_summary if ok else None
            assert ok.metadata_json["draft_status"] == "drafted"
            rule_id = ok.metadata_json["rule_instance_id"]
            assert isinstance(rule_id, int)
            record = await session.get(RuleInstanceRecord, rule_id)
            assert record is not None and json.loads(record.draft_rationale)["pipeline_run_id"] == good_id
            assert partial is not None and partial.status == "partial"
            assert (partial.error_summary or "").startswith("draft_unavailable after 2 attempt(s)")
            assert partial.metadata_json["llm_attempts"] == 2
            calls = (
                await session.execute(
                    select(LLMCall.pipeline_run_id, func.count()).group_by(LLMCall.pipeline_run_id)
                )
            ).all()
            assert dict(calls) == {good_id: 1, bad_id: 2}
        assert hook_calls == [
            {"candidate_id": 21, "rule_instance_id": rule_id, "failed": False, "error": None},
            {
                "candidate_id": 22,
                "rule_instance_id": None,
                "failed": True,
                "error": partial.error_summary,
            },
        ]

    _run(scenario)


def test_retried_run_reuses_its_stored_draft_without_llm(draft_db) -> None:
    templates = draft_db
    payload = _payload("600519.SH", templates)

    async def scenario() -> None:
        stub = StubLLM([valid_output(templates["quality_growth"])])
        with respx.mock() as router:
            router.post(f"{BASE_URL}/chat/completions").mock(side_effect=stub)
            first = await draft_rule(payload, settings=_settings(), run_id=None)
        assert first.ok
        # Simulate a crash after the draft was stored: same run id again.
        async with session_scope() as session:
            record = await session.get(RuleInstanceRecord, first.rule_instance_id)
            assert record is not None
            data = json.loads(record.draft_rationale)
            data["pipeline_run_id"] = 999
            record.draft_rationale = json.dumps(data)
        again = await draft_rule(payload, settings=_settings(), run_id=999)
        assert again.status == "reused" and again.rule_instance_id == first.rule_instance_id
        assert len(await _llm_calls()) == 1

    _run(scenario)


class _TransientClient:
    tier_name = "tier3"

    async def complete(self, **_: Any) -> Any:
        raise httpx.ConnectError("llm endpoint unreachable")


def test_transient_llm_error_requeues_the_run(draft_db, hook_calls) -> None:
    templates = draft_db
    payload = _payload("600519.SH", templates)

    async def scenario() -> None:
        run_id = await _enqueue(payload)
        handler = RuleDraftHandler(settings=_settings(), client_factory=lambda s: _TransientClient())
        await _worker(handler).run_once(run_id=run_id)
        async with session_scope() as session:
            run = await session.get(PipelineRun, run_id)
            assert run is not None and run.status == "queued"
            assert "ConnectError" in (run.error_summary or "")
        assert hook_calls == []  # not terminal yet
        assert await _instance_count() == 0

    _run(scenario)


def test_unknown_template_fails_and_notifies(draft_db, hook_calls) -> None:
    templates = draft_db
    payload = _payload("600519.SH", templates, template_version=99, candidate_id=31)

    async def scenario() -> None:
        with pytest.raises(RuleDraftInputError):
            await draft_rule(payload, settings=_settings())
        run_id = await _enqueue(payload)
        await _worker(RuleDraftHandler(settings=_settings())).run_once(run_id=run_id)
        async with session_scope() as session:
            run = await session.get(PipelineRun, run_id)
            assert run is not None and run.status == "failed"
        assert hook_calls and hook_calls[0]["candidate_id"] == 31 and hook_calls[0]["failed"]

    _run(scenario)


def test_screen_result_must_match_the_ticker(draft_db) -> None:
    templates = draft_db
    payload = _payload("600519.SH", templates, screen_result_id=12345)

    async def scenario() -> None:
        with pytest.raises(RuleDraftInputError, match="no screen_result"):
            await draft_rule(payload, settings=_settings())

    _run(scenario)


def test_notify_without_candidate_module_is_a_logged_noop(monkeypatch) -> None:
    monkeypatch.setattr(
        drafting, "_HOOK_MODULES", ("investment_agent._no_such_candidates_module",)
    )
    assert asyncio.run(notify_draft_result(5, rule_instance_id=1, failed=False, error=None)) is False
    assert asyncio.run(notify_draft_result(0, rule_instance_id=1, failed=False, error=None)) is False


def test_payload_contract() -> None:
    payload = RuleDraftPayload.model_validate(
        {
            "candidate_id": 3,
            "ticker": "600519.SH",
            "template_id": "quality_growth",
            "template_version": 2,
            "as_of": "2026-09-30",
            "screen_result_id": None,
            "preference_notes": ["估值太高"],
        }
    )
    assert rule_draft_idempotency_key(payload) == "rule_draft:3:quality_growth@2"
    with pytest.raises(ValueError):
        RuleDraftPayload.model_validate({**payload.model_dump(), "extra": 1})
    kinds = [handler.kind for handler in default_job_handlers(_settings_offline())]
    assert RULE_DRAFT in kinds


def _settings_offline() -> Settings:
    return Settings(api_key="x" * 32, llm_tier3_api_key="x")  # type: ignore[call-arg]


def test_cli_rules_draft_now(draft_db) -> None:
    from investment_agent.cli.main import app

    template = draft_db["quality_growth"]
    stub = StubLLM([valid_output(template)])
    runner = CliRunner()
    with respx.mock() as router:
        router.post(f"{BASE_URL}/chat/completions").mock(side_effect=stub)
        result = runner.invoke(
            app,
            [
                "rules", "draft", "--ticker", "600519.SH", "--template", "quality_growth",
                "--as-of", "2026-09-30", "--now", "--note", "偏好低估值",
            ],
        )
    assert result.exit_code == 0, result.output
    assert "finished: status=success" in result.output
    assert "drafted_by=llm:stub-model" in result.output
    assert f"template_ref: {template.ref}" in result.output
    again = runner.invoke(
        app, ["rules", "draft", "--ticker", "600519.SH", "--template", "quality_growth",
              "--as-of", "2026-09-30", "--note", "偏好低估值"]
    )
    assert again.exit_code == 0 and "Existing" in again.output
    changed = runner.invoke(
        app, ["rules", "draft", "--ticker", "600519.SH", "--template", "quality_growth",
              "--as-of", "2026-09-30"]
    )
    assert changed.exit_code == 1 and "--rerun" in changed.output
    missing = runner.invoke(app, ["rules", "draft", "--ticker", "600519.SH", "--template", "nope"])
    assert missing.exit_code == 1 and "Unknown template" in missing.output


def test_load_stock_context_from_a_snapshot_view(monkeypatch) -> None:
    from tests.unit._factor_support import baostock_view

    async def fake_view(session, ticker, as_of, **_: object):  # noqa: ANN001, ANN202
        return baostock_view("600519", as_of)

    monkeypatch.setattr(drafting, "load_snapshot_view", fake_view)
    stock = asyncio.run(drafting.load_stock_context(None, "600519.SH", AS_OF))  # type: ignore[arg-type]
    assert set(stock.factors) == set(DEFAULT_REGISTRY.names())
    assert stock.industry["group"] == "consumer"
    assert stock.industry["csrc"]["code"] == "C15"
    assert stock.factors["roe"].value is not None
    rendered = drafting.factor_input(stock.factors)
    assert rendered["roe"]["unit"] == "fraction" and rendered["roe"]["quality"] in {"ok", "approx"}
