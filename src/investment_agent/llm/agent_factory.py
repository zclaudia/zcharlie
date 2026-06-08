"""Build pydantic-ai Agent instances configured against our LLM tier config."""

from typing import Any

from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIModel
from pydantic_ai.output import PromptedOutput
from pydantic_ai.providers.openai import OpenAIProvider

from investment_agent.config import LLMTierConfig


def build_agent[OutputT: BaseModel](
    *,
    config: LLMTierConfig,
    system_prompt: str,
    output_type: type[OutputT] | None = None,
    output_retries: int = 3,
) -> Any:
    """Return a pydantic-ai Agent configured for the given tier.

    Uses PromptedOutput (schema injected into prompt, JSON parsed client-side
    with retry-on-validation-error) rather than the default tool_choice mode.
    Trade-off:
      - Tool mode: provider enforces schema strictly; modern OpenAI works,
        cheaper/thinking models often fail tool_choice='required' or omit
        required fields.
      - Prompted mode: client-side JSON parse + retry feedback to the model
        on validation failure. Works on any chat-completions model, costs
        slightly more tokens, but is dramatically more robust on cheap and
        thinking-mode models.

    Some providers (e.g. kimi-k2) also reject an empty system message at
    position 0; we only pass system_prompt when non-empty so pydantic-ai
    omits the role='system' message entirely otherwise.
    """
    model = OpenAIModel(
        model_name=config.model,
        provider=OpenAIProvider(base_url=config.base_url, api_key=config.api_key),
    )
    kwargs: dict[str, Any] = {"model": model}
    if system_prompt:
        kwargs["system_prompt"] = system_prompt
    if output_type is not None:
        kwargs["output_type"] = PromptedOutput(output_type)
        kwargs["output_retries"] = output_retries
    return Agent(**kwargs)
