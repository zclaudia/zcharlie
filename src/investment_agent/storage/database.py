"""Async engine + session factory."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None

SQLITE_BUSY_TIMEOUT_MS = 15_000


def init_engine(database_url: str, *, echo: bool = False) -> AsyncEngine:
    global _engine, _session_factory
    _engine = create_async_engine(database_url, echo=echo, future=True)
    if make_url(database_url).get_backend_name() == "sqlite":
        configure_sqlite_engine(_engine)
    _session_factory = async_sessionmaker(
        _engine, class_=AsyncSession, expire_on_commit=False
    )
    return _engine


def configure_sqlite_engine(engine: AsyncEngine) -> None:
    """Make SQLite safe for concurrent api / worker / scheduler processes.

    * WAL lets readers proceed while one writer commits, and ``busy_timeout``
      makes contended writers wait instead of failing immediately.
    * pysqlite/aiosqlite only emit their implicit ``BEGIN`` before DML, never
      before ``SAVEPOINT``. A ``begin_nested()`` opened before the first write
      therefore ran outside any transaction and its ``RELEASE`` committed the
      work permanently, so a later outer rollback could not undo it. We emit
      ``BEGIN`` ourselves right before a savepoint whenever the driver has no
      transaction open, so savepoints always nest inside a real transaction.

    SQLAlchemy's full recipe (``isolation_level=None`` + ``BEGIN`` on every
    ``begin`` event) was evaluated and rejected: starting the transaction at the
    first SELECT makes read->write upgrades fail immediately with
    ``database is locked`` (stale WAL snapshot; the busy handler is not invoked)
    whenever another connection commits in between, which broke concurrent
    pipeline writers. Keeping the driver's lazy BEGIN preserves the existing
    lock behaviour while fixing nested transactions.
    """

    sync_engine = engine.sync_engine
    is_memory = sync_engine.url.database in (None, "", ":memory:")

    @event.listens_for(sync_engine, "connect")
    def _on_connect(dbapi_connection: Any, _connection_record: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
            if not is_memory:
                cursor.execute("PRAGMA journal_mode = WAL")
                cursor.execute("PRAGMA synchronous = NORMAL")
        finally:
            cursor.close()

    @event.listens_for(sync_engine, "savepoint")
    def _on_savepoint(connection: Connection, _name: str) -> None:
        if not _driver_in_transaction(connection):
            connection.exec_driver_sql("BEGIN")


def _driver_in_transaction(connection: Connection) -> bool:
    dbapi_connection: Any = connection.connection.dbapi_connection
    # aiosqlite's adapter wraps the aiosqlite connection in ``_connection``;
    # plain pysqlite exposes ``in_transaction`` directly.
    driver = getattr(dbapi_connection, "_connection", dbapi_connection)
    return bool(driver.in_transaction)


def get_engine() -> AsyncEngine:
    if _engine is None:
        raise RuntimeError("Engine not initialized. Call init_engine() first.")
    return _engine


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    if _session_factory is None:
        raise RuntimeError("Session factory not initialized. Call init_engine() first.")
    async with _session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
