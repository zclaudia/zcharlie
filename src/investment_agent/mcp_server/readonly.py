"""Read-only database access for the MCP server (mrd-intake D5).

The server never writes: SQLite is opened through a ``mode=ro`` URI (the
file is never created and every write statement fails with
``attempt to write a readonly database``), ``PRAGMA query_only`` is set on
every connection as a second guard, and sessions never commit. Only the
local SQLite file is supported; the WAL / savepoint setup of
:mod:`investment_agent.storage.database` is deliberately not applied
(``PRAGMA journal_mode = WAL`` is itself a write on a non-WAL file).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote

from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from investment_agent.config import Settings
from investment_agent.storage.database import SQLITE_BUSY_TIMEOUT_MS

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]
"""Opens one read-only session per tool call (``async with sessions() as s``)."""


def readonly_url(database_url: str) -> str:
    """``database_url`` rewritten as a ``mode=ro`` SQLite URI.

    Raises ``NotImplementedError`` for every other backend (the MCP server
    only serves the local SQLite file) and for in-memory databases (nothing
    to read).
    """

    url = make_url(database_url)
    if url.get_backend_name() != "sqlite":
        raise NotImplementedError(
            "the MCP server only supports the local SQLite database "
            f"(DATABASE_URL backend {url.get_backend_name()!r} is not supported)"
        )
    database = url.database or ""
    if database.startswith("file:"):
        database = database[len("file:") :]
    if database in ("", ":memory:"):
        raise NotImplementedError("the MCP server needs an on-disk SQLite database")
    path = Path(database).expanduser().resolve()
    # ``quote`` keeps ``/`` and encodes ``?`` / ``#`` so the path cannot smuggle options.
    return f"sqlite+aiosqlite:///file:{quote(str(path))}?mode=ro&uri=true"


def _on_connect(dbapi_connection: Any, _record: Any) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
        cursor.execute("PRAGMA query_only = 1")
    finally:
        cursor.close()


def readonly_engine(settings: Settings) -> AsyncEngine:
    """A private read-only engine for ``settings.database_url``.

    Not registered as the process-wide engine (``session_scope`` stays
    unusable, so no writing code path can run by accident); the caller
    disposes it on the loop that used it.
    """

    engine = create_async_engine(readonly_url(settings.database_url), future=True)
    event.listens_for(engine.sync_engine, "connect")(_on_connect)
    return engine


def readonly_sessions(engine: AsyncEngine) -> SessionFactory:
    """Session factory over ``engine``; sessions are closed, never committed."""

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    @asynccontextmanager
    async def open_session() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    return open_session


__all__ = ["SessionFactory", "readonly_engine", "readonly_sessions", "readonly_url"]
