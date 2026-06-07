"""Insert LLMCall audit rows."""

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.llm.pricing import calculate_cost
from investment_agent.storage.models.ops import LLMCall


async def record_llm_call(
    session: AsyncSession,
    *,
    purpose: str,
    model: str,
    provider: str,
    prompt_tokens: int,
    completion_tokens: int,
    started_at: datetime,
    latency_ms: int,
    success: bool,
    error_message: str | None = None,
    pipeline_run_id: int | None = None,
) -> None:
    cost = calculate_cost(
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )
    row = LLMCall(
        pipeline_run_id=pipeline_run_id,
        purpose=purpose,
        model=model,
        provider=provider,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost_usd=cost,
        latency_ms=latency_ms,
        started_at=started_at,
        success=success,
        error_message=error_message,
    )
    session.add(row)
    await session.flush()
