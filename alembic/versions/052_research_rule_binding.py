"""Add P3 rule-coverage storage and research rule bindings."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "052"
down_revision: str | Sequence[str] | None = "051"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_ASSESSMENT_COLUMNS = (
    "id, revision, owner_id, kind, account_id, listing_id, as_of, input_hash, "
    "entry_count, manifest_hash, universe_hash, series_key, application_sequence, application_mode, payload"
)


def _trigger_names() -> tuple[str, ...]:
    return (
        "trg_candidate_ranking_entry_parent_insert",
        "trg_candidate_ranking_entry_immutable_insert",
        "trg_investment_assessment_immutable_insert",
        "trg_candidate_ranking_entry_immutable_delete",
        "trg_candidate_ranking_entry_immutable_update",
        "trg_investment_assessment_immutable_delete",
        "trg_investment_assessment_immutable_update",
        "trg_research_rule_binding_immutable_insert",
        "trg_research_rule_binding_immutable_delete",
        "trg_research_rule_binding_immutable_update",
    )


def _drop_051_triggers() -> None:
    for trigger in _trigger_names():
        op.execute(f"DROP TRIGGER IF EXISTS {trigger}")


def _create_assessment_table(name: str, *, coverage: bool) -> None:
    bind = op.get_bind()
    columns = [
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("revision", sa.Integer, primary_key=True),
        sa.Column("owner_id", sa.String(32), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("account_id", sa.String(36), nullable=True),
        sa.Column("listing_id", sa.Integer, nullable=True),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("entry_count", sa.Integer, nullable=True),
        sa.Column("manifest_hash", sa.String(64), nullable=True),
        sa.Column("universe_hash", sa.String(64), nullable=True),
        sa.Column("series_key", sa.String(64), nullable=True),
        sa.Column("application_sequence", sa.Integer, nullable=True),
        sa.Column("application_mode", sa.String(24), nullable=True),
        sa.Column("payload", sa.JSON, nullable=False),
    ]
    constraints = [
        sa.CheckConstraint("revision = 1", name="ck_investment_assessment_revision_one"),
        sa.CheckConstraint(
            "kind IN ('account_fit', 'candidate_ranking', 'rule_application', 'rule_coverage')" if coverage
            else "kind IN ('account_fit', 'candidate_ranking', 'rule_application')",
            name="ck_investment_assessment_kind",
        ),
        sa.CheckConstraint(
            "(kind = 'account_fit' AND account_id IS NOT NULL AND listing_id IS NOT NULL AND entry_count IS NULL "
            "AND manifest_hash IS NULL AND universe_hash IS NULL) "
            "OR (kind = 'candidate_ranking' AND account_id IS NOT NULL AND listing_id IS NULL AND entry_count IS NOT NULL "
            "AND entry_count >= 0 AND manifest_hash IS NOT NULL AND universe_hash IS NOT NULL) "
            "OR (kind = 'rule_application' AND account_id IS NULL AND listing_id IS NOT NULL "
            "AND entry_count IS NULL AND manifest_hash IS NULL AND universe_hash IS NULL "
            "AND series_key IS NOT NULL AND application_sequence IS NOT NULL AND application_sequence >= 1 "
            "AND application_mode IS NOT NULL AND application_mode IN ('live','historical_replay'))"
            + (
                " OR (kind = 'rule_coverage' AND account_id IS NULL AND listing_id IS NOT NULL "
                "AND entry_count IS NOT NULL AND entry_count >= 0 AND manifest_hash IS NOT NULL AND universe_hash IS NULL "
                "AND series_key IS NULL AND application_sequence IS NULL AND application_mode IS NULL)"
                if coverage else ""
            ),
            name="ck_investment_assessment_shape",
        ),
        sa.CheckConstraint(
            "(kind = 'rule_application' AND series_key IS NOT NULL AND application_sequence IS NOT NULL "
            "AND application_mode IS NOT NULL) "
            "OR (kind = 'rule_coverage' AND series_key IS NULL AND application_sequence IS NULL "
            "AND application_mode IS NULL) "
            "OR (kind NOT IN ('rule_application', 'rule_coverage') AND series_key IS NULL "
            "AND application_sequence IS NULL AND application_mode IS NULL)" if coverage else
            "(kind = 'rule_application' AND series_key IS NOT NULL AND application_sequence IS NOT NULL "
            "AND application_mode IS NOT NULL) OR (kind <> 'rule_application' AND series_key IS NULL "
            "AND application_sequence IS NULL AND application_mode IS NULL)",
            name="ck_investment_assessment_series_shape",
        ),
        sa.CheckConstraint("json_valid(payload)", name="ck_investment_assessment_payload_json"),
        sa.CheckConstraint("json_type(payload) = 'object'", name="ck_investment_assessment_payload_object"),
        sa.UniqueConstraint(
            "owner_id", "listing_id", "series_key", "application_sequence",
            name="uq_investment_assessment_rule_application_sequence",
        ),
    ]
    table = sa.Table(name, sa.MetaData(), *columns, *constraints)
    table.create(bind)


def _create_assessment_indexes(*, coverage: bool) -> None:
    op.create_index("ix_investment_assessment_input", "investment_assessment", ["owner_id", "kind", "account_id", "input_hash"])
    op.create_index("ix_investment_assessment_owner_account_kind_asof", "investment_assessment", ["owner_id", "account_id", "kind", "as_of"])
    op.create_index("ix_investment_assessment_owner_listing_kind_asof", "investment_assessment", ["owner_id", "listing_id", "kind", "as_of"])
    op.create_index("ix_investment_assessment_rule_application_live", "investment_assessment", ["owner_id", "listing_id", "series_key", "application_mode", "application_sequence"])
    op.create_index(
        "ix_investment_assessment_rule_application_operation",
        "investment_assessment",
        ["owner_id", sa.text("json_extract(payload, '$.operation_id')")],
        sqlite_where=sa.text("kind = 'rule_application'"),
    )
    if coverage:
        op.create_index(
            "ix_investment_assessment_rule_coverage_task",
            "investment_assessment",
            ["owner_id", sa.text("json_extract(payload, '$.task_id')")],
            sqlite_where=sa.text("kind = 'rule_coverage'"),
        )


def _drop_assessment_indexes(*, coverage: bool) -> None:
    if coverage:
        op.drop_index("ix_investment_assessment_rule_coverage_task", table_name="investment_assessment")
    op.drop_index("ix_investment_assessment_rule_application_live", table_name="investment_assessment")
    op.drop_index("ix_investment_assessment_owner_listing_kind_asof", table_name="investment_assessment")
    op.drop_index("ix_investment_assessment_owner_account_kind_asof", table_name="investment_assessment")
    op.drop_index("ix_investment_assessment_input", table_name="investment_assessment")


def _create_candidate_entry() -> None:
    bind = op.get_bind()
    metadata = sa.MetaData()
    # The migration has already created the full parent table; register only
    # its referenced key columns locally so this frozen DDL can compile.
    sa.Table(
        "investment_assessment", metadata,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("revision", sa.Integer, primary_key=True),
    )
    table = sa.Table(
        "candidate_ranking_entry", metadata,
        sa.Column("ranking_id", sa.String(36), primary_key=True),
        sa.Column("revision", sa.Integer, primary_key=True),
        sa.Column("owner_id", sa.String(32), nullable=False),
        sa.Column("account_id", sa.String(36), nullable=False),
        sa.Column("listing_id", sa.Integer, primary_key=True),
        sa.Column("ordinal", sa.Integer, nullable=False),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.CheckConstraint("revision = 1", name="ck_candidate_ranking_entry_revision_one"),
        sa.CheckConstraint("ordinal >= 0", name="ck_candidate_ranking_entry_ordinal_nonnegative"),
        sa.CheckConstraint("json_valid(payload)", name="ck_candidate_ranking_entry_payload_json"),
        sa.CheckConstraint("json_type(payload) = 'object'", name="ck_candidate_ranking_entry_payload_object"),
        sa.ForeignKeyConstraint(
            ["ranking_id", "revision"], ["investment_assessment.id", "investment_assessment.revision"],
            name="fk_candidate_ranking_entry_assessment", ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("ranking_id", "revision", "listing_id", name="uq_candidate_ranking_entry_listing"),
        sa.UniqueConstraint("ranking_id", "revision", "ordinal", name="uq_candidate_ranking_entry_ordinal"),
    )
    sa.Index("ix_candidate_ranking_entry_page", table.c.ranking_id, table.c.revision, table.c.ordinal)
    table.create(bind)
    op.execute(
        "CREATE TRIGGER IF NOT EXISTS trg_candidate_ranking_entry_immutable_insert "
        "BEFORE INSERT ON candidate_ranking_entry WHEN EXISTS (SELECT 1 FROM candidate_ranking_entry WHERE "
        "(ranking_id = NEW.ranking_id AND revision = NEW.revision AND listing_id = NEW.listing_id) OR "
        "(ranking_id = NEW.ranking_id AND revision = NEW.revision AND ordinal = NEW.ordinal)) "
        "BEGIN SELECT RAISE(ABORT, 'candidate ranking entry is immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER IF NOT EXISTS trg_candidate_ranking_entry_parent_insert "
        "BEFORE INSERT ON candidate_ranking_entry WHEN NOT EXISTS (SELECT 1 FROM investment_assessment AS a "
        "WHERE a.id = NEW.ranking_id AND a.revision = NEW.revision AND a.kind = 'candidate_ranking' "
        "AND a.owner_id = NEW.owner_id AND a.account_id = NEW.account_id AND a.entry_count > 0 "
        "AND NEW.ordinal < a.entry_count AND (SELECT count(*) FROM candidate_ranking_entry AS e "
        "WHERE e.ranking_id = NEW.ranking_id AND e.revision = NEW.revision) < a.entry_count) "
        "BEGIN SELECT RAISE(ABORT, 'invalid or complete candidate ranking parent'); END"
    )
    for operation in ("UPDATE", "DELETE"):
        op.execute(
            f"CREATE TRIGGER IF NOT EXISTS trg_candidate_ranking_entry_immutable_{operation.lower()} "
            f"BEFORE {operation} ON candidate_ranking_entry "
            "BEGIN SELECT RAISE(ABORT, 'investment assessment is immutable'); END"
        )


def _create_assessment_guards() -> None:
    op.execute(
        "CREATE TRIGGER IF NOT EXISTS trg_investment_assessment_immutable_update "
        "BEFORE UPDATE ON investment_assessment BEGIN SELECT RAISE(ABORT, 'investment assessment is immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER IF NOT EXISTS trg_investment_assessment_immutable_delete "
        "BEFORE DELETE ON investment_assessment BEGIN SELECT RAISE(ABORT, 'investment assessment is immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER IF NOT EXISTS trg_investment_assessment_immutable_insert "
        "BEFORE INSERT ON investment_assessment WHEN EXISTS (SELECT 1 FROM investment_assessment WHERE id = NEW.id) "
        "BEGIN SELECT RAISE(ABORT, 'investment assessment is immutable'); END"
    )


def _save_candidate_entries(bind: sa.Connection, name: str) -> None:
    bind.execute(sa.text(f"CREATE TEMP TABLE {name} AS SELECT * FROM candidate_ranking_entry"))


def _restore_candidate_entries(bind: sa.Connection, name: str) -> None:
    bind.execute(sa.text(
        "INSERT INTO candidate_ranking_entry "
        "(ranking_id, revision, owner_id, account_id, listing_id, ordinal, payload) "
        f"SELECT ranking_id, revision, owner_id, account_id, listing_id, ordinal, payload "
        f"FROM {name} ORDER BY ranking_id, revision, ordinal"
    ))
    bind.execute(sa.text(f"DROP TABLE {name}"))


def _create_binding_table() -> None:
    bind = op.get_bind()
    metadata = sa.MetaData()
    table = sa.Table(
        "research_rule_binding", metadata,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("revision", sa.Integer, primary_key=True),
        sa.Column("owner_id", sa.String(32), nullable=False),
        sa.Column("task_id", sa.String(36), nullable=False),
        sa.Column("listing_id", sa.Integer, nullable=False),
        sa.Column("research_kind", sa.String(24), nullable=False),
        sa.Column("admission_as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("rules_generation", sa.Integer, nullable=False),
        sa.Column("events_generation", sa.Integer, nullable=False),
        sa.Column("rule_set_hash", sa.String(64), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.CheckConstraint("revision = 1", name="ck_research_rule_binding_revision_one"),
        sa.CheckConstraint("length(rule_set_hash) = 64", name="ck_research_rule_binding_rule_set_hash"),
        sa.CheckConstraint("length(input_hash) = 64", name="ck_research_rule_binding_input_hash"),
        sa.CheckConstraint("length(content_hash) = 64", name="ck_research_rule_binding_content_hash"),
        sa.CheckConstraint("json_valid(payload)", name="ck_research_rule_binding_payload_json"),
        sa.CheckConstraint("json_type(payload) = 'object'", name="ck_research_rule_binding_payload_object"),
        sa.UniqueConstraint("owner_id", "id", "revision", name="uq_research_rule_binding_owner_revision"),
        sa.UniqueConstraint("task_id", name="uq_research_rule_binding_task"),
    )
    sa.Index("ix_research_rule_binding_owner_listing", table.c.owner_id, table.c.listing_id)
    table.create(bind)
    for operation in ("UPDATE", "DELETE"):
        op.execute(
            f"CREATE TRIGGER IF NOT EXISTS trg_research_rule_binding_immutable_{operation.lower()} "
            f"BEFORE {operation} ON research_rule_binding "
            "BEGIN SELECT RAISE(ABORT, 'research rule binding is immutable'); END"
        )
    op.execute(
        "CREATE TRIGGER IF NOT EXISTS trg_research_rule_binding_immutable_insert "
        "BEFORE INSERT ON research_rule_binding "
        "WHEN EXISTS (SELECT 1 FROM research_rule_binding WHERE id = NEW.id) "
        "BEGIN SELECT RAISE(ABORT, 'research rule binding is immutable'); END"
    )


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        raise NotImplementedError("supplemental rule immutability requires SQLite")
    _drop_051_triggers()
    _save_candidate_entries(bind, "p3_saved_candidate_entries")
    _drop_assessment_indexes(coverage=False)
    op.drop_table("candidate_ranking_entry")

    _create_assessment_table("investment_assessment_new", coverage=True)
    bind.execute(sa.text(
        f"INSERT INTO investment_assessment_new ({_ASSESSMENT_COLUMNS}) "
        f"SELECT {_ASSESSMENT_COLUMNS} FROM investment_assessment"
    ))
    op.drop_table("investment_assessment")
    op.execute("ALTER TABLE investment_assessment_new RENAME TO investment_assessment")
    _create_assessment_indexes(coverage=True)
    _create_candidate_entry()
    _create_assessment_guards()
    _restore_candidate_entries(bind, "p3_saved_candidate_entries")
    _create_binding_table()


def _p3_business_data_exists() -> bool:
    bind = op.get_bind()
    checks = (
        "SELECT 1 FROM investment_assessment WHERE kind = 'rule_coverage' LIMIT 1",
        "SELECT 1 FROM research_rule_binding LIMIT 1",
        "SELECT 1 FROM investor_publication_dependency WHERE json_valid(payload) AND "
        "json_extract(payload, '$.ref.kind') = 'rule_coverage' LIMIT 1",
    )
    return any(bind.execute(sa.text(query)).first() is not None for query in checks)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        raise NotImplementedError("supplemental rule immutability requires SQLite")
    if _p3_business_data_exists():
        raise RuntimeError(
            "cannot downgrade 052 while rule coverage manifests, research rule bindings, "
            "or coverage publication dependencies exist"
        )
    _drop_051_triggers()
    op.drop_table("research_rule_binding")

    _save_candidate_entries(bind, "p3_saved_candidate_entries")
    _drop_assessment_indexes(coverage=True)
    op.drop_table("candidate_ranking_entry")

    _create_assessment_table("investment_assessment_old", coverage=False)
    bind.execute(sa.text(
        f"INSERT INTO investment_assessment_old ({_ASSESSMENT_COLUMNS}) "
        f"SELECT {_ASSESSMENT_COLUMNS} FROM investment_assessment "
        "WHERE kind IN ('account_fit','candidate_ranking','rule_application')"
    ))
    op.drop_table("investment_assessment")
    op.execute("ALTER TABLE investment_assessment_old RENAME TO investment_assessment")
    _create_assessment_indexes(coverage=False)
    _create_candidate_entry()
    _create_assessment_guards()
    _restore_candidate_entries(bind, "p3_saved_candidate_entries")
