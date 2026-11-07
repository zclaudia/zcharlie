"""Real SQLite constraints, migration parity and immutable fact behavior.

The application intentionally leaves SQLite foreign_keys disabled. These tests
prove uniqueness/check/trigger behavior, not cross-scope authorization; scoped
services must check identity before writing any child row.
"""

import importlib.util
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.autogenerate import produce_migrations
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.exc import IntegrityError

from investment_agent.storage.models import Base

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 11, 9, 10, tzinfo=UTC)
SIM_TABLES = [
    table for name, table in Base.metadata.tables.items() if name.startswith("simulation_")
]


@pytest.fixture(params=["metadata", "migration"])
def connection(request):
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        if request.param == "metadata":
            Base.metadata.create_all(conn, tables=SIM_TABLES)
        else:
            spec = importlib.util.spec_from_file_location(
                "simulation_migration", ROOT / "alembic/versions/061_simulation.py"
            )
            migration = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(migration)
            with Operations.context(MigrationContext.configure(conn)):
                migration.upgrade()
        yield conn
    engine.dispose()


def row(table, identity, **values):
    return {"id": identity, "payload": {}, **values}


def insert(conn, table, identity, **values):
    conn.execute(Base.metadata.tables[table].insert().values(**row(table, identity, **values)))


def experiment(conn):
    insert(
        conn,
        "simulation_experiment",
        "e",
        revision=0,
        status="draft",
        config_hash="a" * 64,
        config={"initial_cash": "1000000.000000000000000001"},
    )


def test_simulation_is_registered_and_isolated(connection):
    assert len(SIM_TABLES) == 13
    assert {table.name for table in SIM_TABLES} <= set(sa.inspect(connection).get_table_names())
    assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 0
    for table in SIM_TABLES:
        assert all(fk.column.table.name.startswith("simulation_") for fk in table.foreign_keys)
    experiment(connection)
    stored = connection.execute(
        sa.select(Base.metadata.tables["simulation_experiment"].c.config)
    ).scalar_one()
    assert stored["initial_cash"] == "1000000.000000000000000001"


def test_schema_matches_migration(connection):
    context = MigrationContext.configure(
        connection,
        opts={
            "include_object": lambda obj, name, type_, reflected, compare: (
                name.startswith("simulation_") if type_ == "table" else True
            ),
            "compare_type": True,
        },
    )
    assert produce_migrations(context, Base.metadata).upgrade_ops.is_empty()


def test_one_observation_round_and_nonnegative_revision(connection):
    experiment(connection)
    values = dict(
        experiment_id="e",
        observation_date=date(2026, 11, 9),
        execution_date=date(2026, 11, 10),
        cutoff=NOW,
        decision_deadline=NOW + timedelta(hours=1),
        execution_at=NOW + timedelta(days=1),
        manifest_hash="b" * 64,
        status="waiting_data",
        revision=0,
    )
    insert(connection, "simulation_round", "r", **values)
    with pytest.raises(IntegrityError, match="UNIQUE"):
        insert(connection, "simulation_round", "r-duplicate", **values)
    with pytest.raises(IntegrityError, match="CHECK"):
        insert(
            connection,
            "simulation_account",
            "a",
            experiment_id="e",
            participant_id="p",
            revision=-1,
        )


def test_participant_accounts_are_independent_and_unique(connection):
    values = dict(
        experiment_id="e",
        account_id="a",
        name="name",
        version=1,
        kind="agent",
        config_hash="a" * 64,
        config={},
    )
    insert(connection, "simulation_participant", "p", **values)
    with pytest.raises(IntegrityError, match="UNIQUE"):
        insert(connection, "simulation_participant", "p2", **(values | {"name": "other"}))
    with pytest.raises(IntegrityError, match="UNIQUE"):
        insert(connection, "simulation_participant", "p3", **(values | {"account_id": "other"}))


def test_only_one_locked_decision_and_idempotent_revisions(connection):
    values = dict(
        experiment_id="e",
        participant_id="p",
        account_id="a",
        round_id="r",
        revision=0,
        account_revision=0,
        status="draft",
        idempotency_key="k0",
    )
    insert(connection, "simulation_decision", "d0", **values)
    insert(
        connection,
        "simulation_decision",
        "d1",
        **(values | {"revision": 1, "status": "locked", "idempotency_key": "k1"}),
    )
    with pytest.raises(IntegrityError, match="UNIQUE"):
        insert(
            connection,
            "simulation_decision",
            "d2",
            **(values | {"revision": 2, "status": "locked", "idempotency_key": "k2"}),
        )
    with pytest.raises(IntegrityError, match="UNIQUE"):
        insert(connection, "simulation_decision", "d3", **(values | {"revision": 3}))
    with pytest.raises(IntegrityError, match="UNIQUE"):
        insert(connection, "simulation_decision", "d4", **(values | {"idempotency_key": "k4"}))


def test_order_fill_and_ledger_deterministic_keys(connection):
    scope = dict(experiment_id="e", participant_id="p", account_id="a", round_id="r")
    order = scope | dict(
        decision_id="d",
        listing_id=1,
        sequence=1,
        account_revision=1,
        execution_date=date(2026, 11, 10),
        status="locked",
        idempotency_key="o-key",
    )
    insert(connection, "simulation_order", "o", **order)
    for patch in ({"idempotency_key": "new"}, {"sequence": 2}, {"listing_id": 2}):
        with pytest.raises(IntegrityError, match="UNIQUE"):
            insert(connection, "simulation_order", "o2", **(order | patch))
    fill = scope | dict(
        order_id="o", execution_key="o:open:1", sequence=1, economic_at=NOW, confirmed_at=NOW
    )
    insert(connection, "simulation_fill", "f", **fill)
    with pytest.raises(IntegrityError, match="UNIQUE"):
        insert(connection, "simulation_fill", "f2", **fill)
    ledger = {key: value for key, value in scope.items() if key != "round_id"} | dict(
        sequence=1,
        account_revision=1,
        idempotency_key="ledger-key",
        kind="fill",
        economic_at=NOW,
        confirmed_at=NOW,
    )
    insert(connection, "simulation_ledger_event", "l", **ledger)
    for patch in ({"idempotency_key": "new"}, {"sequence": 2}, {"account_revision": 2}):
        with pytest.raises(IntegrityError, match="UNIQUE"):
            insert(connection, "simulation_ledger_event", "l2", **(ledger | patch))


@pytest.mark.parametrize(
    "table,values",
    [
        (
            "simulation_ledger_event",
            dict(
                sequence=1,
                account_revision=1,
                idempotency_key="key",
                kind="initial_cash",
                economic_at=NOW,
                confirmed_at=NOW,
            ),
        ),
        (
            "simulation_fill",
            dict(
                round_id="r",
                order_id="o",
                execution_key="key",
                sequence=1,
                economic_at=NOW,
                confirmed_at=NOW,
            ),
        ),
        (
            "simulation_nav",
            dict(
                account_revision=1,
                valuation_date=date(2026, 11, 9),
                version=1,
                quality="complete",
                input_hash="a" * 64,
                valuation_version="v1",
                metrics_version="v1",
            ),
        ),
        (
            "simulation_execution_evidence",
            dict(
                listing_id=1,
                trade_date=date(2026, 11, 9),
                revision=1,
                source_kind="user",
                source_ref="official-source",
                captured_at=NOW,
                content_hash="a" * 64,
            ),
        ),
    ],
)
def test_facts_cannot_be_rewritten_or_deleted(connection, table, values):
    scope = dict(experiment_id="e")
    if table != "simulation_execution_evidence":
        scope |= dict(participant_id="p", account_id="a")
    insert(connection, table, "fact", **scope, **values)
    with pytest.raises(IntegrityError, match="append-only"):
        connection.exec_driver_sql(f"UPDATE {table} SET payload = '{{}}' WHERE id = 'fact'")
    with pytest.raises(IntegrityError, match="append-only"):
        connection.exec_driver_sql(f"DELETE FROM {table} WHERE id = 'fact'")


def test_execution_evidence_versions_and_request_keys(connection):
    values = dict(
        experiment_id="e",
        listing_id=1,
        trade_date=date(2026, 11, 9),
        revision=1,
        source_kind="provider",
        source_ref="official-source",
        captured_at=NOW,
        content_hash="a" * 64,
    )
    insert(connection, "simulation_execution_evidence", "x", **values)
    with pytest.raises(IntegrityError, match="UNIQUE"):
        insert(
            connection,
            "simulation_execution_evidence",
            "x2",
            **(values | {"content_hash": "b" * 64}),
        )
    with pytest.raises(IntegrityError, match="UNIQUE"):
        insert(connection, "simulation_execution_evidence", "x3", **(values | {"revision": 2}))
    requests = Base.metadata.tables["simulation_request"]
    request = dict(
        scope="experiment:e:start",
        idempotency_key="k",
        input_hash="a" * 64,
        result_id="e",
        payload={},
    )
    connection.execute(requests.insert().values(**request))
    with pytest.raises(IntegrityError, match="UNIQUE"):
        connection.execute(requests.insert().values(**request))


def test_upgrade_from_060_and_downgrade_preserves_real_tables(tmp_path, monkeypatch):
    path = tmp_path / "migration.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{path}")
    config = Config(str(ROOT / "alembic.ini"))
    command.upgrade(config, "060")
    engine = sa.create_engine(f"sqlite:///{path}")
    with engine.connect() as conn:
        before = set(sa.inspect(conn).get_table_names())
    command.upgrade(config, "061")
    with engine.connect() as conn:
        assert set(sa.inspect(conn).get_table_names()) == before | {
            table.name for table in SIM_TABLES
        }
    command.downgrade(config, "060")
    with engine.connect() as conn:
        assert set(sa.inspect(conn).get_table_names()) == before
        assert "portfolio_account" in before
    engine.dispose()


@pytest.mark.parametrize(
    "table,values",
    [
        (
            "simulation_participant",
            dict(
                experiment_id="e",
                account_id="a",
                name="p",
                kind="agent",
                version=0,
                config_hash="a" * 64,
                config={},
            ),
        ),
        (
            "simulation_review",
            dict(
                experiment_id="e",
                participant_id="p",
                account_id="a",
                decision_id="d",
                version=0,
                status="observing",
                available_at=NOW,
            ),
        ),
        (
            "simulation_fill",
            dict(
                experiment_id="e",
                participant_id="p",
                account_id="a",
                round_id="r",
                order_id="o",
                execution_key="key",
                sequence=1,
                economic_at=NOW,
                confirmed_at=NOW - timedelta(seconds=1),
            ),
        ),
        (
            "simulation_ledger_event",
            dict(
                experiment_id="e",
                participant_id="p",
                account_id="a",
                sequence=1,
                account_revision=1,
                idempotency_key="key",
                kind="fill",
                economic_at=NOW,
                confirmed_at=NOW - timedelta(seconds=1),
            ),
        ),
        (
            "simulation_round",
            dict(
                experiment_id="e",
                observation_date=NOW.date(),
                execution_date=NOW.date() + timedelta(days=1),
                cutoff=NOW,
                decision_deadline=NOW,
                execution_at=NOW + timedelta(days=1),
                manifest_hash="a" * 64,
                status="waiting_data",
                revision=0,
            ),
        ),
    ],
)
def test_storage_rejects_invalid_versions_and_clocks(connection, table, values):
    with pytest.raises(IntegrityError, match="CHECK"):
        insert(connection, table, "invalid", **values)
