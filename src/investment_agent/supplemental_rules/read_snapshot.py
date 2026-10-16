"""Local SQLite read snapshots for P2's multi-query read projections."""

from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession


async def ensure_read_snapshot(session: AsyncSession) -> None:
    """Start a read transaction only when the driver has not started one.

    SQLite's legacy driver does not emit BEGIN for SELECT. A P2 GET must keep
    its registry, source rows and application set in one snapshot. Mutation
    and worker transactions already hold their writer reservation, and must
    not be restarted or upgraded here. The caller owns commit/rollback.
    """
    connection = await session.connection()

    def begin_if_needed(sync_connection: Connection) -> None:
        if sync_connection.dialect.name != "sqlite":
            return
        driver = sync_connection.connection.driver_connection
        if not getattr(driver, "in_transaction", False):
            sync_connection.exec_driver_sql("BEGIN")

    await connection.run_sync(begin_if_needed)
