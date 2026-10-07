"""Async engine + session factory.

Lifecycle: every engine must be disposed on the event loop that opened its
connections. aiosqlite runs each connection on a worker thread that resolves
futures on the loop that issued the call; an engine dropped without
``dispose()`` leaves pooled connections whose garbage collection later posts to
a closed loop ("Event loop is closed" in a worker thread). Use
``engine_lifespan`` (async code) or ``run_with_engine`` (sync CLI entry points)
so the engine is disposed before the loop exits.
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
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
# Disposals scheduled by ``init_engine`` on a running loop; strong references
# keep the tasks alive until they finish.
_pending_disposals: set[asyncio.Task[None]] = set()

SQLITE_BUSY_TIMEOUT_MS = 15_000


def init_engine(database_url: str, *, echo: bool = False) -> AsyncEngine:
    """Create the process-wide engine, disposing any engine it replaces."""

    global _engine, _session_factory
    previous = _engine
    _engine, _session_factory = _create_engine(database_url, echo=echo)
    if previous is not None:
        _dispose_replaced(previous)
    return _engine


def _create_engine(
    database_url: str, *, echo: bool
) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(database_url, echo=echo, future=True)
    if make_url(database_url).get_backend_name() == "sqlite":
        configure_sqlite_engine(engine)
    return engine, async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


def _has_pooled_connections(engine: AsyncEngine) -> bool:
    pool: Any = engine.sync_engine.pool
    checkedin = getattr(pool, "checkedin", None)
    checkedout = getattr(pool, "checkedout", None)
    if callable(checkedin) and callable(checkedout):
        return bool(checkedin() + checkedout())
    return True  # e.g. StaticPool: cannot tell cheaply, dispose to be safe


def _dispose_replaced(engine: AsyncEngine) -> None:
    """Dispose a replaced engine without blocking a running loop.

    On a running loop the disposal is scheduled as a task (awaited by the next
    ``dispose_engine`` / ``wait_pending_disposals``). Without one nothing can be
    blocked, so it runs to completion on a private short-lived loop (the
    thread's current loop is left alone); aiosqlite connections are not bound
    to the loop that opened them.
    """

    if not _has_pooled_connections(engine):
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        private = asyncio.new_event_loop()
        try:
            private.run_until_complete(engine.dispose())
        finally:
            private.close()
        return
    task = loop.create_task(engine.dispose())
    _pending_disposals.add(task)
    task.add_done_callback(_pending_disposals.discard)


async def wait_pending_disposals() -> None:
    """Await disposals scheduled on the current loop by ``init_engine``."""

    loop = asyncio.get_running_loop()
    pending = [task for task in _pending_disposals if task.get_loop() is loop]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


async def dispose_engine() -> None:
    """Dispose the current engine (and pending replaced ones) and clear it.

    A later ``session_scope`` then fails loudly instead of silently reopening
    connections that nobody disposes.
    """

    global _engine, _session_factory
    engine, _engine, _session_factory = _engine, None, None
    if engine is not None:
        await engine.dispose()
    await wait_pending_disposals()


@asynccontextmanager
async def engine_lifespan(database_url: str, *, echo: bool = False) -> AsyncIterator[AsyncEngine]:
    """Own an engine for the duration of the block and dispose it on exit.

    The engine becomes the process-wide one inside the block; whatever engine
    was current before is restored afterwards (not disposed: its owner does
    that), so nested owners such as an app lifespan inside a test fixture or a
    CLI command invoked from a test do not tear down their caller's engine.
    """

    global _engine, _session_factory
    previous = (_engine, _session_factory)
    engine, factory = _create_engine(database_url, echo=echo)
    _engine, _session_factory = engine, factory
    try:
        yield engine
    finally:
        if _engine is engine:
            _engine, _session_factory = previous
        elif _engine is not None:
            # Someone called init_engine inside the block; that engine was
            # created for this scope too.
            await dispose_engine()
            _engine, _session_factory = previous
        await engine.dispose()
        await wait_pending_disposals()


def run_with_engine[T](database_url: str, main: Callable[[], Awaitable[T]]) -> T:
    """``asyncio.run(main())`` with the engine opened and disposed on that loop."""

    async def runner() -> T:
        async with engine_lifespan(database_url):
            return await main()

    return asyncio.run(runner())


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
