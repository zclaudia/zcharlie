"""Tests for LLMCallRepository."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from investment_agent.storage.models.ops import LLMCall
from investment_agent.storage.repositories.llm_call import LLMCallRepository


def _make_call(
    *,
    purpose: str,
    cost: Decimal,
    started_at: datetime,
    pt: int = 100,
    ct: int = 50,
) -> LLMCall:
    return LLMCall(
        purpose=purpose,
        model="gpt-4o-mini",
        provider="openai",
        prompt_tokens=pt,
        completion_tokens=ct,
        cost_usd=cost,
        latency_ms=1234,
        started_at=started_at,
        success=True,
    )


@pytest.mark.asyncio
async def test_list_recent_limit_orders_desc(db_session):
    now = datetime.now(UTC)
    db_session.add_all(
        [
            _make_call(
                purpose="score",
                cost=Decimal("0.01"),
                started_at=now - timedelta(seconds=30),
            ),
            _make_call(
                purpose="deep",
                cost=Decimal("0.05"),
                started_at=now - timedelta(seconds=10),
            ),
            _make_call(purpose="attr", cost=Decimal("0.10"), started_at=now),
        ]
    )
    await db_session.flush()
    rows = await LLMCallRepository(db_session).list_recent(limit=2)
    assert len(rows) == 2
    assert rows[0].purpose == "attr"
    assert rows[1].purpose == "deep"


@pytest.mark.asyncio
async def test_cost_summary_aggregates(db_session):
    now = datetime.now(UTC)
    db_session.add_all(
        [
            _make_call(
                purpose="score",
                cost=Decimal("0.10"),
                started_at=now,
                pt=100,
                ct=50,
            ),
            _make_call(
                purpose="score",
                cost=Decimal("0.20"),
                started_at=now,
                pt=200,
                ct=100,
            ),
            _make_call(
                purpose="deep",
                cost=Decimal("0.50"),
                started_at=now,
                pt=500,
                ct=250,
            ),
        ]
    )
    await db_session.flush()
    month_start = date.today().replace(day=1)
    summary = await LLMCallRepository(db_session).cost_summary(
        month_start=month_start
    )
    assert summary["total_calls"] == 3
    assert summary["total_cost_usd"] == 0.80
    assert summary["total_prompt_tokens"] == 800
    assert summary["total_completion_tokens"] == 400
    by_purpose = summary["by_purpose"]
    assert isinstance(by_purpose, list)
    # Sorted by cost desc: deep (0.50), score (0.30)
    assert by_purpose[0]["purpose"] == "deep"
    assert by_purpose[0]["cost_usd"] == 0.50
    assert by_purpose[1]["purpose"] == "score"
    assert by_purpose[1]["cost_usd"] == 0.30
