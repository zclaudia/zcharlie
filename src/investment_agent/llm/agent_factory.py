"""Build pydantic-ai Agent instances configured against our LLM tier config."""

from typing import TypeVar

from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIModel
from pydantic_ai.providers.openai import OpenAIProvider

from investment_agent.config import LLMTierConfig

OutputT = TypeVar("OutputT", bound=BaseModel)


def build_agent(
    *,
    config: LLMTierConfig,
    system_prompt: str,
    output_type: type[OutputT] | None = None,
) -> Agent:  # type: ignore[type-arg]
    model = OpenAIModel(
        model_name=config.model,
        provider=OpenAIProvider(base_url=config.base_url, api_key=config.api_key),
    )
    kwargs: dict = {"model": model, "system_prompt": system_prompt}
    if output_type is not None:
        kwargs["output_type"] = output_type
    return Agent(**kwargs)
