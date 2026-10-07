"""LLM reasoning toggle: sent only when configured, per tier and for rule drafting."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from investment_agent.config import LLMTierConfig, Settings
from investment_agent.llm.openai_compatible import OpenAICompatibleClient, thinking_extra_body


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {"api_key": "x" * 32, "llm_tier3_api_key": "k"}
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def _tier(thinking: str = "auto") -> LLMTierConfig:
    return LLMTierConfig(base_url="http://llm.test/v1", api_key="k", model="m", thinking=thinking)  # type: ignore[arg-type]


def test_extra_body_only_when_configured() -> None:
    assert thinking_extra_body(_tier()) is None
    assert thinking_extra_body(_tier("disabled")) == {"thinking": {"type": "disabled"}}
    assert thinking_extra_body(_tier("enabled")) == {"thinking": {"type": "enabled"}}


def test_rule_draft_override_inherits_or_replaces_the_tier_mode() -> None:
    assert _settings().llm_rule_draft.thinking == "auto"
    assert _settings(llm_tier3_thinking="disabled").llm_rule_draft.thinking == "disabled"
    settings = _settings(llm_tier3_thinking="enabled", llm_rule_draft_thinking="disabled")
    assert settings.llm_tier3.thinking == "enabled"
    assert settings.llm_rule_draft.thinking == "disabled"
    assert settings.llm_rule_draft.model == settings.llm_tier3.model


@pytest.mark.parametrize(("mode", "expected"), [("auto", None), ("disabled", {"type": "disabled"})])
async def test_client_sends_thinking_parameter(mode: str, expected: object, db_session) -> None:
    sent: list[dict[str, object]] = []

    def answer(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "x", "object": "chat.completion", "created": 0, "model": "m",
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    with respx.mock:
        respx.post("http://llm.test/v1/chat/completions").mock(side_effect=answer)
        client = OpenAICompatibleClient(tier_name="tier3", config=_tier(mode), session=db_session)
        await client.complete(system_prompt="s", user_message="u", purpose="test")
    assert sent[0].get("thinking") == expected
