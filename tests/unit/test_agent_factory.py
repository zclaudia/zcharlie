from pydantic import BaseModel
from pydantic_ai import Agent

from investment_agent.config import LLMTierConfig
from investment_agent.llm.agent_factory import build_agent


class _Score(BaseModel):
    score: int


def test_build_agent_returns_pydantic_ai_agent():
    cfg = LLMTierConfig(
        base_url="https://api.openai.com/v1", api_key="sk-x", model="gpt-4o-mini"
    )
    agent = build_agent(
        config=cfg, system_prompt="you are a scorer", output_type=_Score
    )
    assert isinstance(agent, Agent)


def test_build_agent_without_output_type():
    cfg = LLMTierConfig(
        base_url="https://api.deepseek.com/v1", api_key="sk-x", model="deepseek-chat"
    )
    agent = build_agent(config=cfg, system_prompt="hello", output_type=None)
    assert isinstance(agent, Agent)


def test_build_agent_sets_output_retries_without_deprecation_and_timeout():
    import warnings

    cfg = LLMTierConfig(
        base_url="https://api.test.example/v1",
        api_key="sk-x",
        model="m",
        timeout_seconds=42.0,
    )
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        agent = build_agent(
            config=cfg, system_prompt="s", output_type=_Score, output_retries=4
        )
    assert agent._max_output_retries == 4
    assert agent.model.client.timeout == 42.0


def test_settings_propagate_llm_request_timeout(monkeypatch):
    from investment_agent.config import Settings
    from investment_agent.llm.openai_compatible import OpenAICompatibleClient

    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        api_key="t",
        llm_tier2_api_key="t",
        llm_tier3_api_key="t",
        llm_request_timeout_seconds=33.0,
    )
    assert settings.llm_tier2.timeout_seconds == 33.0
    assert settings.llm_tier3.timeout_seconds == 33.0
    client = OpenAICompatibleClient(
        tier_name="tier3", config=settings.llm_tier3, session=None  # type: ignore[arg-type]
    )
    assert client._client.timeout == 33.0
