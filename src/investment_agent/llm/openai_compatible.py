"""LLMClient implementation backed by openai SDK (OpenAI-compatible)."""

import time
from datetime import UTC, datetime
from typing import Any

from openai import NOT_GIVEN, AsyncOpenAI
from openai.types.shared_params import ResponseFormatJSONObject
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.config import LLMTierConfig
from investment_agent.domain.protocols import CompletionResult
from investment_agent.llm.tracking import record_llm_call


class OpenAICompatibleClient:
    def __init__(
        self,
        *,
        tier_name: str,
        config: LLMTierConfig,
        session: AsyncSession,
    ) -> None:
        self.tier_name = tier_name
        self._config = config
        self._session = session
        self._client = AsyncOpenAI(
            base_url=config.base_url,
            api_key=config.api_key,
            timeout=config.timeout_seconds,
        )

    async def complete(
        self,
        *,
        system_prompt: str,
        user_message: str,
        output_model: type[BaseModel] | None = None,
        tools: list[Any] | None = None,
        purpose: str,
        pipeline_run_id: int | None = None,
    ) -> CompletionResult:
        started = datetime.now(UTC)
        t0 = time.perf_counter()
        success = False
        error_msg: str | None = None
        prompt_tokens = completion_tokens = 0
        text = ""
        parsed: Any | None = None

        try:
            resp = await self._client.chat.completions.create(
                model=self._config.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ],
                response_format=ResponseFormatJSONObject(type="json_object") if output_model else NOT_GIVEN,  # type: ignore[arg-type]
            )
            text = resp.choices[0].message.content or ""
            prompt_tokens = resp.usage.prompt_tokens if resp.usage else 0
            completion_tokens = resp.usage.completion_tokens if resp.usage else 0
            if output_model is not None:
                parsed = output_model.model_validate_json(text)
            success = True
        except Exception as e:
            error_msg = str(e)
            raise
        finally:
            latency_ms = int((time.perf_counter() - t0) * 1000)
            await record_llm_call(
                self._session,
                purpose=purpose,
                model=self._config.model,
                provider=_provider_name(self._config.base_url),
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                started_at=started,
                latency_ms=latency_ms,
                success=success,
                error_message=error_msg,
                pipeline_run_id=pipeline_run_id,
            )

        return CompletionResult(
            text=text,
            output=parsed,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=latency_ms,
            model=self._config.model,
            provider=_provider_name(self._config.base_url),
        )


def _provider_name(base_url: str) -> str:
    if "openai.com" in base_url:
        return "openai"
    if "deepseek" in base_url:
        return "deepseek"
    if "anthropic" in base_url:
        return "anthropic"
    if "siliconflow" in base_url:
        return "siliconflow"
    if "openrouter" in base_url:
        return "openrouter"
    return base_url
