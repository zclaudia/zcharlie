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


async def _create_probe_table(engine) -> None:
    from sqlalchemy import text

    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE probe (id INTEGER PRIMARY KEY, v TEXT)"))


@pytest.mark.asyncio
async def test_sqlite_file_engine_uses_wal_and_busy_timeout(tmp_path):
    from sqlalchemy import text

    engine = init_engine(f"sqlite+aiosqlite:///{tmp_path / 'wal.db'}")
    try:
        async with session_scope() as s:
            journal = (await s.execute(text("PRAGMA journal_mode"))).scalar()
            busy = (await s.execute(text("PRAGMA busy_timeout"))).scalar()
        assert str(journal).lower() == "wal"
        assert busy == 15000
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_outer_rollback_undoes_nested_savepoint_insert(tmp_path):
    """begin_nested() before any write must not commit the outer transaction."""

    from sqlalchemy import text

    engine = init_engine(f"sqlite+aiosqlite:///{tmp_path / 'nested.db'}")
    try:
        await _create_probe_table(engine)
        with pytest.raises(RuntimeError):
            async with session_scope() as s:
                # A read first, so the driver has not opened a transaction yet.
                await s.execute(text("SELECT count(*) FROM probe"))
                async with s.begin_nested():
                    await s.execute(text("INSERT INTO probe (v) VALUES ('nested')"))
                raise RuntimeError("abort outer transaction")
        async with session_scope() as s:
            count = (await s.execute(text("SELECT count(*) FROM probe"))).scalar()
        assert count == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_rolled_back_savepoint_keeps_outer_work(tmp_path):
    from sqlalchemy import text

    engine = init_engine(f"sqlite+aiosqlite:///{tmp_path / 'nested-keep.db'}")
    try:
        await _create_probe_table(engine)
        async with session_scope() as s:
            await s.execute(text("INSERT INTO probe (v) VALUES ('outer')"))
            with pytest.raises(RuntimeError):
                async with s.begin_nested():
                    await s.execute(text("INSERT INTO probe (v) VALUES ('inner')"))
                    raise RuntimeError("abort savepoint")
        async with session_scope() as s:
            rows = (await s.execute(text("SELECT v FROM probe"))).scalars().all()
        assert rows == ["outer"]
    finally:
        await engine.dispose()
