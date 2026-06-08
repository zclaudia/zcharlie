"""Build pydantic-ai Agent instances configured against our LLM tier config."""

from typing import Any

from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIModel
from pydantic_ai.providers.openai import OpenAIProvider

from investment_agent.config import LLMTierConfig


def build_agent[OutputT: BaseModel](
    *,
    config: LLMTierConfig,
    system_prompt: str,
    output_type: type[OutputT] | None = None,
) -> Any:
    """Return a pydantic-ai Agent configured for the given tier."""
    model = OpenAIModel(
        model_name=config.model,
        provider=OpenAIProvider(base_url=config.base_url, api_key=config.api_key),
    )
    kwargs: dict[str, Any] = {"model": model}
    if system_prompt:
        kwargs["system_prompt"] = system_prompt
    if output_type is not None:
        kwargs["output_type"] = output_type
    return Agent(**kwargs)
