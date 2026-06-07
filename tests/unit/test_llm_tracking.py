from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from investment_agent.llm.tracking import record_llm_call
from investment_agent.storage.models.ops import LLMCall


@pytest.mark.asyncio
async def test_record_llm_call_writes_row(db_session):
    await record_llm_call(
        db_session,
        purpose="strategy_score",
        model="gpt-4o-mini",
        provider="openai",
        prompt_tokens=1000,
        completion_tokens=200,
        started_at=datetime(2025, 6, 3, tzinfo=UTC),
        latency_ms=1234,
        success=True,
        error_message=None,
        pipeline_run_id=None,
    )
    rows = (await db_session.execute(select(LLMCall))).scalars().all()
    assert len(rows) == 1
    assert rows[0].purpose == "strategy_score"
    assert rows[0].cost_usd == Decimal("0.00027")  # 0.15 + 0.12 per 1M


@pytest.mark.asyncio
async def test_record_llm_call_failure(db_session):
    await record_llm_call(
        db_session,
        purpose="deep_analysis",
        model="gpt-5",
        provider="openai",
        prompt_tokens=0,
        completion_tokens=0,
        started_at=datetime(2025, 6, 3, tzinfo=UTC),
        latency_ms=50,
        success=False,
        error_message="rate limited",
        pipeline_run_id=None,
    )
    rows = (await db_session.execute(select(LLMCall))).scalars().all()
    assert len(rows) == 1
    assert rows[0].success is False
    assert rows[0].error_message == "rate limited"
