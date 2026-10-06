"""/admin endpoints -- LLM call audit + cost summary."""

from datetime import date, datetime
from typing import Annotated, cast
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query

from investment_agent.api.auth import verify_api_key
from investment_agent.api.schemas.admin import LLMCallOut, LLMCostSummary
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.ops import LLMCall
from investment_agent.storage.repositories.llm_call import LLMCallRepository

router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(verify_api_key)],
)


def _to_call_out(r: LLMCall) -> LLMCallOut:
    return LLMCallOut(
        id=r.id,
        pipeline_run_id=r.pipeline_run_id,
        purpose=r.purpose,
        model=r.model,
        provider=r.provider,
        prompt_tokens=r.prompt_tokens,
        completion_tokens=r.completion_tokens,
        cost_usd=float(r.cost_usd),
        latency_ms=r.latency_ms,
        started_at=r.started_at,
        success=r.success,
    )


@router.get("/llm-calls", response_model=list[LLMCallOut])
async def list_llm_calls(
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    since: Annotated[
        datetime | None,
        Query(description="ISO datetime; only returns calls at/after"),
    ] = None,
) -> list[LLMCallOut]:
    async with session_scope() as session:
        rows = await LLMCallRepository(session).list_recent(
            limit=limit,
            since=since,
        )
        return [_to_call_out(r) for r in rows]


@router.get("/llm-calls/cost", response_model=LLMCostSummary)
async def llm_cost_summary(
    month: Annotated[
        str | None,
        Query(description="YYYY-MM; defaults to current month"),
    ] = None,
) -> LLMCostSummary:
    if month is None:
        month_start = datetime.now(ZoneInfo("Asia/Shanghai")).date().replace(day=1)
    else:
        try:
            month_start = date.fromisoformat(f"{month}-01")
        except ValueError as e:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid month '{month}': expected YYYY-MM",
            ) from e
    async with session_scope() as session:
        summary = await LLMCallRepository(session).cost_summary(
            month_start=month_start,
        )
        return LLMCostSummary(
            month_start=cast(str, summary["month_start"]),
            total_calls=cast(int, summary["total_calls"]),
            total_cost_usd=cast(float, summary["total_cost_usd"]),
            total_prompt_tokens=cast(int, summary["total_prompt_tokens"]),
            total_completion_tokens=cast(int, summary["total_completion_tokens"]),
            by_purpose=cast(list[dict[str, object]], summary["by_purpose"]),
        )
