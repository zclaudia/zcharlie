"""Schema comparison for real Alembic upgrades against disposable SQLite."""

import os
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import patch

from alembic.config import Config
from sqlalchemy import Table, create_engine, inspect

ROOT = Path(__file__).resolve().parents[2]


@contextmanager
def migration_config(database: Path) -> Iterator[Config]:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    # The early offline plugin enforces the disposable SQLite path.
    with patch.dict(os.environ, {"DATABASE_URL": f"sqlite+aiosqlite:///{database}"}):
        yield config


def table_schema(database: Path, tables: Sequence[Table]) -> dict[str, Any]:
    engine = create_engine(f"sqlite:///{database}")
    try:
        inspector = inspect(engine)
        result: dict[str, Any] = {}
        for table in tables:
            result[table.name] = {
                "columns": [(c["name"], str(c["type"]), c["nullable"], c["default"])
                            for c in inspector.get_columns(table.name)],
                "pk": inspector.get_pk_constraint(table.name),
                "checks": sorted(inspector.get_check_constraints(table.name), key=lambda x: x["name"] or ""),
                "unique": sorted(inspector.get_unique_constraints(table.name), key=lambda x: str(x)),
                "indexes": inspector.get_indexes(table.name),
                "foreign_keys": inspector.get_foreign_keys(table.name),
            }
        return result
    finally:
        engine.dispose()
