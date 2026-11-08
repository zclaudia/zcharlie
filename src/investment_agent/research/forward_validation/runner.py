"""Read-only runner: open an existing SQLite file and produce the report.

The database is opened with SQLite's own ``mode=ro`` URI (the file is never
created) and ``PRAGMA query_only`` as a second guard. No migrations run and no
session commits. Failures before a single observation is built return a report
whose ``status`` is ``insufficient`` with the failure reason, never a crash
with a half-written file.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from urllib.parse import quote

from sqlalchemy import event, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from investment_agent.research.forward_validation.contracts import (
    FORMULA_VERSION,
    LookaheadError,
    ValidationConfig,
)
from investment_agent.research.forward_validation.dataset import load_dataset


def readonly_sqlite_url(path: Path) -> str:
    """``mode=ro`` SQLite URI for an existing database file.

    Raises ``FileNotFoundError`` instead of letting SQLite create an empty
    database, and rejects non-files so a typo cannot silently produce an
    ``insufficient`` report against nothing.
    """

    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"database not found: {resolved}")
    return f"sqlite+aiosqlite:///file:{quote(str(resolved))}?mode=ro&uri=true"


async def run_forward_validation(
    config: ValidationConfig,
    *,
    database: Path,
) -> dict[str, Any]:
    """Run the report against ``database``; returns the report document."""

    url = readonly_sqlite_url(database)
    engine = create_async_engine(url, future=True)

    @event.listens_for(engine.sync_engine, "connect")
    def _on_connect(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA query_only = 1")
        finally:
            cursor.close()

    try:
        async with engine.connect() as connection:
            await connection.exec_driver_sql("PRAGMA query_only = 1")
            async with AsyncSession(bind=connection) as session:
                schema_version = await _schema_version(session)
                try:
                    report = (await load_dataset(session, config)).report
                except SQLAlchemyError as exc:
                    return failed_report(
                        config,
                        reason=(
                            "the database schema is older than this code "
                            f"({type(exc).__name__}: {_first_line(str(exc))}); run "
                            "`ia migrate up` on a copy, or use --template-source files"
                        ),
                        database=database,
                        schema_version=schema_version,
                    )
                report["database"] = {
                    "path": str(database),
                    "read_only": True,
                    "schema_version": schema_version,
                }
                return report
    finally:
        await engine.dispose()


async def _schema_version(session: AsyncSession) -> str | None:
    try:
        result = await session.execute(text("select version_num from alembic_version"))
        value = result.scalar_one_or_none()
    except SQLAlchemyError:
        return None
    return str(value) if value is not None else None


def _first_line(message: str) -> str:
    line = message.strip().splitlines()[0] if message.strip() else message
    return line[:300]


def run_forward_validation_sync(config: ValidationConfig, *, database: Path) -> dict[str, Any]:
    return asyncio.run(run_forward_validation(config, database=database))


def failed_report(
    config: ValidationConfig,
    *,
    reason: str,
    database: Path | None,
    schema_version: str | None = None,
) -> dict[str, Any]:
    """An ``insufficient`` document for a run that could not start."""

    return {
        "schema_version": FORMULA_VERSION,
        "status": "insufficient",
        "insufficient_reasons": [reason],
        "database": {
            "path": str(database) if database is not None else None,
            "read_only": True,
            "schema_version": schema_version,
        },
        "config": config.to_dict(),
        "config_hash": config.config_hash,
        "guard": {"calibration_status_changed": False},
    }


def write_report(report: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )


__all__ = [
    "LookaheadError",
    "failed_report",
    "readonly_sqlite_url",
    "run_forward_validation",
    "run_forward_validation_sync",
    "write_report",
]
