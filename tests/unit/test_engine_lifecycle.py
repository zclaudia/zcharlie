"""Engines are disposed on the loop that opened their connections.

An engine dropped with pooled aiosqlite connections is closed by the garbage
collector from whatever loop is current then, which surfaces as
"Event loop is closed" in an aiosqlite worker thread (pytest's
PytestUnhandledThreadExceptionWarning).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from investment_agent.storage import database
from investment_agent.storage.database import (
    dispose_engine,
    engine_lifespan,
    get_engine,
    init_engine,
    run_with_engine,
    session_scope,
)


def _pooled(engine: AsyncEngine) -> int:
    pool = engine.sync_engine.pool
    return pool.checkedin() + pool.checkedout()  # type: ignore[attr-defined]


async def _touch() -> int:
    async with session_scope() as session:
        return int((await session.execute(text("SELECT 1"))).scalar_one())


def _url(tmp_path: Path, name: str) -> str:
    return f"sqlite+aiosqlite:///{tmp_path / name}"


@pytest.fixture(autouse=True)
def _restore_globals():
    saved = (database._engine, database._session_factory)
    database._engine, database._session_factory = None, None
    yield
    database._engine, database._session_factory = saved


def test_init_engine_without_loop_disposes_the_replaced_engine(tmp_path: Path) -> None:
    first = init_engine(_url(tmp_path, "a.db"))
    asyncio.run(_touch())
    assert _pooled(first) == 1

    second = init_engine(_url(tmp_path, "b.db"))

    assert _pooled(first) == 0
    assert get_engine() is second
    asyncio.run(dispose_engine())


async def test_init_engine_on_a_running_loop_schedules_the_disposal(tmp_path: Path) -> None:
    first = init_engine(_url(tmp_path, "a.db"))
    await _touch()
    assert _pooled(first) == 1

    init_engine(_url(tmp_path, "b.db"))
    await _touch()
    await dispose_engine()  # also awaits the scheduled disposal of ``first``

    assert _pooled(first) == 0
    assert not database._pending_disposals
    with pytest.raises(RuntimeError, match="not initialized"):
        get_engine()


async def test_engine_lifespan_disposes_its_engine_and_restores_the_caller(
    tmp_path: Path,
) -> None:
    outer = init_engine(_url(tmp_path, "outer.db"))
    await _touch()
    async with engine_lifespan(_url(tmp_path, "inner.db")) as inner:
        assert get_engine() is inner
        await _touch()
        assert _pooled(inner) == 1
    assert _pooled(inner) == 0
    assert get_engine() is outer
    assert await _touch() == 1  # the caller's engine is untouched and usable
    await dispose_engine()
    assert _pooled(outer) == 0


async def test_engine_lifespan_disposes_an_engine_initialised_inside_it(tmp_path: Path) -> None:
    async with engine_lifespan(_url(tmp_path, "a.db")) as owned:
        await _touch()
        nested = init_engine(_url(tmp_path, "b.db"))
        await _touch()
    assert _pooled(owned) == 0
    assert _pooled(nested) == 0
    with pytest.raises(RuntimeError, match="not initialized"):
        get_engine()


def test_run_with_engine_leaves_no_pooled_connection(tmp_path: Path) -> None:
    seen: list[AsyncEngine] = []

    async def main() -> int:
        seen.append(get_engine())
        return await _touch()

    assert run_with_engine(_url(tmp_path, "cli.db"), main) == 1
    assert _pooled(seen[0]) == 0
    with pytest.raises(RuntimeError, match="not initialized"):
        get_engine()


def test_api_lifespan_disposes_the_engine_on_shutdown(tmp_path: Path, monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from investment_agent.api.app import create_app

    monkeypatch.setenv("API_KEY", "secret")
    monkeypatch.setenv("LLM_TIER3_API_KEY", "x")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", _url(tmp_path, "api.db"))
    seen: list[AsyncEngine] = []
    with TestClient(create_app()) as client:
        seen.append(get_engine())
        assert client.get("/healthz").status_code == 200
        client.portal.call(_touch)  # type: ignore[union-attr]
        assert _pooled(seen[0]) == 1
    assert _pooled(seen[0]) == 0
    with pytest.raises(RuntimeError, match="not initialized"):
        get_engine()
