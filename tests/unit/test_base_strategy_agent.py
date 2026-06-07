from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic_ai import Agent

from investment_agent.domain.strategy_types import ListingSnapshot
from investment_agent.strategies.base import BaseStrategyAgent, StrategyConfig
from investment_agent.strategies.schemas import ValueDeepAnalysis, ValueStockScore
from investment_agent.strategies.screening_rule import Condition, ScreeningRule


class _StubAgent:
    """Stub for Pydantic-AI Agent (used here only because BaseStrategyAgent's __init__ requires them)."""


def _make_agent(rule: ScreeningRule) -> BaseStrategyAgent:
    cfg = StrategyConfig(
        code="test",
        version="v1.0",
        name="t",
        description="t",
        screening_rule=rule,
        scoring_prompt_template="x/scoring",
        deep_prompt_template="x/deep",
        scoring_schema=ValueStockScore,
        deep_schema=ValueDeepAnalysis,
    )

    class _A(BaseStrategyAgent):
        pass

    _A.config = cfg
    return _A(
        prompts_dir=Path("."),
        tier2_agent=cast(Agent, _StubAgent()),
        tier3_agent=cast(Agent, _StubAgent()),
    )


def _snap(**kw: Any) -> ListingSnapshot:
    base: dict[str, Any] = dict(
        listing_id=1,
        company_id=1,
        ticker="AAPL",
        market_code="NASDAQ",
        company_name="Apple",
        as_of="2025-06-03",
    )
    base.update(kw)
    return ListingSnapshot.model_validate(base)


@pytest.mark.asyncio
async def test_screen_passes_when_required_met() -> None:
    rule = ScreeningRule(required=[Condition(metric="pe", op="<", value=30)])
    agent = _make_agent(rule)
    candidates = [_snap(pe=Decimal("25")), _snap(pe=Decimal("35"))]
    out = await agent.screen(candidates)
    assert len(out) == 1
    assert out[0].pe == Decimal("25")


@pytest.mark.asyncio
async def test_screen_returns_empty_when_none_pass() -> None:
    rule = ScreeningRule(required=[Condition(metric="pe", op="<", value=5)])
    agent = _make_agent(rule)
    out = await agent.screen([_snap(pe=Decimal("25"))])
    assert out == []
