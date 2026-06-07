from datetime import datetime, timezone
from decimal import Decimal

import pytest
import respx
from httpx import Response
from pydantic import BaseModel

from investment_agent.config import LLMTierConfig
from investment_agent.llm.openai_compatible import OpenAICompatibleClient


class _Score(BaseModel):
    score: int
    rationale: str


@pytest.mark.asyncio
@respx.mock
async def test_complete_returns_plain_text(db_session):
    """No output_model — direct string."""
    respx.post("https://api.test.example/v1/chat/completions").mock(
        return_value=Response(
            200,
            json={
                "id": "x",
                "object": "chat.completion",
                "created": 1,
                "model": "test-model",
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": "hi there"}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
            },
        )
    )
    cfg = LLMTierConfig(
        base_url="https://api.test.example/v1", api_key="sk-test", model="test-model"
    )
    client = OpenAICompatibleClient(tier_name="tier2", config=cfg, session=db_session)
    out = await client.complete(
        system_prompt="you are helpful",
        user_message="say hi",
        purpose="test_call",
    )
    assert out.text == "hi there"
    assert out.prompt_tokens == 5
    assert out.completion_tokens == 2


@pytest.mark.asyncio
@respx.mock
async def test_complete_with_output_model_parses_json(db_session):
    respx.post("https://api.test.example/v1/chat/completions").mock(
        return_value=Response(
            200,
            json={
                "id": "x",
                "object": "chat.completion",
                "created": 1,
                "model": "test-model",
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": '{"score": 87, "rationale": "solid fundamentals"}'}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 50, "completion_tokens": 20, "total_tokens": 70},
            },
        )
    )
    cfg = LLMTierConfig(
        base_url="https://api.test.example/v1", api_key="sk-test", model="test-model"
    )
    client = OpenAICompatibleClient(tier_name="tier2", config=cfg, session=db_session)
    out = await client.complete(
        system_prompt="score this",
        user_message="AAPL",
        output_model=_Score,
        purpose="strategy_score",
    )
    assert isinstance(out.output, _Score)
    assert out.output.score == 87
    assert out.output.rationale == "solid fundamentals"
