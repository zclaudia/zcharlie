"""Read-only MCP server (mrd-intake D5): read-only engine, tool catalogue and
one call per tool against the seeded in-memory database."""

from __future__ import annotations

import sqlite3

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from typer.testing import CliRunner

from investment_agent.config import Settings
from investment_agent.mcp_server.readonly import readonly_engine, readonly_sessions, readonly_url

# ---- read-only engine ------------------------------------------------------


def _settings(url: str) -> Settings:
    return Settings(api_key="secret", database_url=url)


@pytest.mark.parametrize(
    "url",
    ["sqlite+aiosqlite:///./data/app.db", "sqlite+aiosqlite:////srv/zcharlie/app.db"],
)
def test_readonly_url_rewrites_sqlite_as_ro_uri(url: str) -> None:
    rewritten = readonly_url(url)
    assert rewritten.startswith("sqlite+aiosqlite:///file:/")
    assert rewritten.endswith("app.db?mode=ro&uri=true")


@pytest.mark.parametrize(
    "url",
    ["postgresql+asyncpg://user:pw@db/app", "mysql://db/app", "sqlite+aiosqlite:///:memory:"],
)
def test_readonly_url_refuses_other_backends(url: str) -> None:
    with pytest.raises(NotImplementedError) as info:
        readonly_url(url)
    assert "pw" not in str(info.value)


async def test_readonly_engine_reads_but_rejects_writes(tmp_path) -> None:
    path = tmp_path / "ro.db"
    seed = sqlite3.connect(path)
    seed.execute("CREATE TABLE note (body TEXT)")
    seed.execute("INSERT INTO note VALUES ('kept')")
    seed.commit()
    seed.close()

    engine = readonly_engine(_settings(f"sqlite+aiosqlite:///{path}"))
    try:
        sessions = readonly_sessions(engine)
        async with sessions() as session:
            rows = (await session.execute(text("SELECT body FROM note"))).scalars().all()
            assert rows == ["kept"]
        for statement in (
            "INSERT INTO note VALUES ('leak')",
            "UPDATE note SET body = 'leak'",
            "DELETE FROM note",
            "CREATE TABLE other (x INTEGER)",
        ):
            async with sessions() as session:
                with pytest.raises(OperationalError, match="readonly"):
                    await session.execute(text(statement))
    finally:
        await engine.dispose()

    check = sqlite3.connect(path)
    assert check.execute("SELECT body FROM note").fetchall() == [("kept",)]
    check.close()


async def test_readonly_engine_never_creates_the_file(tmp_path) -> None:
    missing = tmp_path / "absent.db"
    engine = readonly_engine(_settings(f"sqlite+aiosqlite:///{missing}"))
    try:
        async with readonly_sessions(engine)() as session:
            with pytest.raises(OperationalError):
                await session.execute(text("SELECT 1"))
    finally:
        await engine.dispose()
    assert not missing.exists()


# ---- CLI -------------------------------------------------------------------


def test_cli_mcp_help_lists_serve_and_tools() -> None:
    from investment_agent.cli.main import app

    result = CliRunner().invoke(app, ["mcp", "--help"])
    assert result.exit_code == 0
    assert "serve" in result.output
    assert "tools" in result.output
