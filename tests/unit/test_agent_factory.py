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
