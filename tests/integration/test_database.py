import pytest

from investment_agent.storage.database import init_engine, session_scope


@pytest.mark.asyncio
async def test_session_scope_commits():
    from sqlalchemy import text

    init_engine("sqlite+aiosqlite:///:memory:")
    async with session_scope() as s:
        result = await s.execute(text("SELECT 1 AS one"))
        assert result.scalar() == 1


@pytest.mark.asyncio
async def test_session_scope_rolls_back_on_error():
    from sqlalchemy import text

    init_engine("sqlite+aiosqlite:///:memory:")
    with pytest.raises(ValueError):
        async with session_scope() as s:
            await s.execute(text("SELECT 1"))
            raise ValueError("boom")
