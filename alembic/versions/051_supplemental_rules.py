"""Add P2 supplemental rules and strict rule-application storage."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "051"
down_revision: str | Sequence[str] | None = "050"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_P1_COLUMNS = (
    "id, revision, owner_id, kind, account_id, listing_id, as_of, input_hash, "
    "entry_count, manifest_hash, universe_hash, payload"
)
_P2_EXTRA_COLUMNS = "series_key, application_sequence, application_mode"


def _trigger_names() -> tuple[str, ...]:
    return (
        "trg_candidate_ranking_entry_parent_insert",
        "trg_candidate_ranking_entry_immutable_insert",
        "trg_investment_assessment_immutable_insert",
        "trg_candidate_ranking_entry_immutable_delete",
        "trg_candidate_ranking_entry_immutable_update",
        "trg_investment_assessment_immutable_delete",
        "trg_investment_assessment_immutable_update",
    )


def _drop_050_triggers() -> None:
    for trigger in _trigger_names():
        op.execute(f"DROP TRIGGER IF EXISTS {trigger}")


def _create_assessment_table(name: str, *, p2: bool) -> None:
    bind = op.get_bind()
    columns = [
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("revision", sa.Integer, primary_key=True),
        sa.Column("owner_id", sa.String(32), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("account_id", sa.String(36), nullable=bool(p2)),
        sa.Column("listing_id", sa.Integer, nullable=True),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("entry_count", sa.Integer, nullable=True),
        sa.Column("manifest_hash", sa.String(64), nullable=True),
        sa.Column("universe_hash", sa.String(64), nullable=True),
    ]
    if p2:
        columns.extend([
            sa.Column("series_key", sa.String(64), nullable=True),
            sa.Column("application_sequence", sa.Integer, nullable=True),
            sa.Column("application_mode", sa.String(24), nullable=True),
        ])
    columns.append(sa.Column("payload", sa.JSON, nullable=False))
    constraints = [
        sa.CheckConstraint("revision = 1", name="ck_investment_assessment_revision_one"),
        sa.CheckConstraint(
            "kind IN ('account_fit', 'candidate_ranking', 'rule_application')" if p2
            else "kind IN ('account_fit', 'candidate_ranking')",
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
            "AND application_mode IS NOT NULL AND application_mode IN ('live','historical_replay'))" if p2 else
            "(kind = 'account_fit' AND account_id IS NOT NULL AND listing_id IS NOT NULL AND entry_count IS NULL "
            "AND manifest_hash IS NULL AND universe_hash IS NULL) "
            "OR (kind = 'candidate_ranking' AND account_id IS NOT NULL AND listing_id IS NULL AND entry_count IS NOT NULL "
            "AND entry_count >= 0 AND manifest_hash IS NOT NULL AND universe_hash IS NOT NULL)",
            name="ck_investment_assessment_shape",
        ),
        sa.CheckConstraint("json_valid(payload)", name="ck_investment_assessment_payload_json"),
        sa.CheckConstraint("json_type(payload) = 'object'", name="ck_investment_assessment_payload_object"),
    ]
    if p2:
        constraints.append(sa.CheckConstraint(
            "(kind = 'rule_application' AND series_key IS NOT NULL AND application_sequence IS NOT NULL "
            "AND application_mode IS NOT NULL) OR (kind <> 'rule_application' AND series_key IS NULL "
            "AND application_sequence IS NULL AND application_mode IS NULL)",
            name="ck_investment_assessment_series_shape",
        ))
    table = sa.Table(name, sa.MetaData(), *columns, *constraints)
    if p2:
        table.append_constraint(sa.UniqueConstraint(
            "owner_id", "listing_id", "series_key", "application_sequence",
            name="uq_investment_assessment_rule_application_sequence",
        ))
    table.create(bind)


def _create_assessment_indexes() -> None:
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


def _create_p2_tables() -> None:
    bind = op.get_bind()
    metadata = sa.MetaData()
    rule = sa.Table(
        "supplemental_rule_revision", metadata,
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("revision", sa.Integer, primary_key=True),
        sa.Column("owner_id", sa.String(32), nullable=False), sa.Column("schema_version", sa.Integer, nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False), sa.Column("semantic_hash", sa.String(64), nullable=False),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.CheckConstraint("revision >= 1", name="ck_supplemental_rule_revision_positive"),
        sa.CheckConstraint("schema_version = 1", name="ck_supplemental_rule_schema_version"),
        sa.CheckConstraint("length(content_hash) = 64", name="ck_supplemental_rule_content_hash"),
        sa.CheckConstraint("length(semantic_hash) = 64", name="ck_supplemental_rule_semantic_hash"),
        sa.CheckConstraint("json_valid(payload)", name="ck_supplemental_rule_payload_json"),
        sa.CheckConstraint("json_type(payload) = 'object'", name="ck_supplemental_rule_payload_object"),
        sa.UniqueConstraint("owner_id", "id", "revision", name="uq_supplemental_rule_owner_revision"),
    )
    sa.Index("ix_supplemental_rule_owner_id_revision", rule.c.owner_id, rule.c.id, rule.c.revision)
    head = sa.Table(
        "supplemental_rule_head", metadata,
        sa.Column("owner_id", sa.String(32), primary_key=True), sa.Column("rule_id", sa.String(36), primary_key=True),
        sa.Column("head_revision", sa.Integer, nullable=False), sa.Column("active_revision", sa.Integer),
        sa.Column("active_state", sa.String(16)), sa.Column("draft_revision", sa.Integer),
        sa.Column("draft_state", sa.String(16)), sa.Column("last_validation_revision", sa.Integer),
        sa.Column("last_validation_hash", sa.String(64)), sa.Column("last_validation_payload", sa.JSON),
        sa.CheckConstraint("head_revision >= 0", name="ck_supplemental_rule_head_revision_nonnegative"),
        sa.CheckConstraint(
            "(active_revision IS NULL AND active_state IS NULL) OR "
            "(active_revision IS NOT NULL AND active_revision >= 1 AND active_state IS NOT NULL "
            "AND active_state IN ('active','suspended','retired'))", name="ck_supplemental_rule_active_shape",
        ),
        sa.CheckConstraint(
            "(draft_revision IS NULL AND draft_state IS NULL) OR "
            "(draft_revision IS NOT NULL AND draft_revision >= 1 AND draft_state IS NOT NULL "
            "AND draft_state IN ('draft','validated'))", name="ck_supplemental_rule_draft_shape",
        ),
        sa.CheckConstraint(
            "(last_validation_revision IS NULL AND last_validation_hash IS NULL) OR "
            "(last_validation_revision IS NOT NULL AND last_validation_revision >= 1 "
            "AND last_validation_hash IS NOT NULL AND length(last_validation_hash) = 64)",
            name="ck_supplemental_rule_validation_shape",
        ),
    )
    rule_event = sa.Table(
        "supplemental_rule_event", metadata,
        sa.Column("owner_id", sa.String(32), primary_key=True), sa.Column("rule_id", sa.String(36), primary_key=True),
        sa.Column("sequence", sa.Integer, primary_key=True), sa.Column("rule_revision", sa.Integer),
        sa.Column("operation_id", sa.String(36), nullable=False), sa.Column("command", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("payload", sa.JSON, nullable=False),
        sa.CheckConstraint("sequence >= 1", name="ck_supplemental_rule_event_sequence_positive"),
        sa.CheckConstraint("json_valid(payload)", name="ck_supplemental_rule_event_payload_json"),
        sa.CheckConstraint("json_type(payload) = 'object'", name="ck_supplemental_rule_event_payload_object"),
    )
    sa.Index("ix_supplemental_rule_event_owner_rule_sequence", rule_event.c.owner_id, rule_event.c.rule_id, rule_event.c.sequence)
    sa.Index("ix_supplemental_rule_event_operation", rule_event.c.owner_id, rule_event.c.operation_id)
    registry = sa.Table(
        "supplemental_registry_head", metadata,
        sa.Column("owner_id", sa.String(32), primary_key=True), sa.Column("registry_revision", sa.Integer, nullable=False),
        sa.Column("rules_generation", sa.Integer, nullable=False), sa.Column("events_generation", sa.Integer, nullable=False),
        sa.Column("semantic_probe_hash", sa.String(64)), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("registry_revision >= 0", name="ck_supplemental_registry_revision_nonnegative"),
        sa.CheckConstraint("rules_generation >= 0", name="ck_supplemental_registry_rules_generation_nonnegative"),
        sa.CheckConstraint("events_generation >= 0", name="ck_supplemental_registry_events_generation_nonnegative"),
    )
    event_revision = sa.Table(
        "research_event_revision", metadata,
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("revision", sa.Integer, primary_key=True),
        sa.Column("owner_id", sa.String(32), nullable=False), sa.Column("previous_ref", sa.JSON),
        sa.Column("event_key", sa.String(200), nullable=False), sa.Column("event_aliases", sa.JSON, nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True)), sa.Column("effective_from", sa.DateTime(timezone=True)),
        sa.Column("effective_to", sa.DateTime(timezone=True)), sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("available_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False), sa.Column("payload", sa.JSON, nullable=False),
        sa.CheckConstraint("revision >= 1", name="ck_research_event_revision_positive"),
        sa.CheckConstraint("previous_ref IS NULL OR json_valid(previous_ref)", name="ck_research_event_previous_ref_json"),
        sa.CheckConstraint("json_valid(event_aliases)", name="ck_research_event_aliases_json"),
        sa.CheckConstraint("json_valid(payload)", name="ck_research_event_payload_json"),
        sa.CheckConstraint("json_type(payload) = 'object'", name="ck_research_event_payload_object"),
        sa.CheckConstraint("length(content_hash) = 64", name="ck_research_event_content_hash"),
        sa.UniqueConstraint("owner_id", "id", "revision", name="uq_research_event_owner_revision"),
    )
    sa.Index("ix_research_event_owner_key_revision", event_revision.c.owner_id, event_revision.c.event_key, event_revision.c.revision)
    sa.Index("ix_research_event_owner_available", event_revision.c.owner_id, event_revision.c.available_from)
    for table in (rule, head, rule_event, registry, event_revision):
        table.create(bind)

    # Freeze all guards into this migration; later ORM edits cannot rewrite 051.
    for table_name in ("supplemental_rule_revision", "research_event_revision"):
        for operation in ("UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER trg_{table_name}_immutable_{operation.lower()} "
                f"BEFORE {operation} ON {table_name} BEGIN SELECT RAISE(ABORT, 'supplemental revision is immutable'); END"
            )
        op.execute(
            f"CREATE TRIGGER trg_{table_name}_immutable_insert BEFORE INSERT ON {table_name} "
            "WHEN EXISTS (SELECT 1 FROM " + table_name + " WHERE id = NEW.id AND revision = NEW.revision) "
            "BEGIN SELECT RAISE(ABORT, 'supplemental revision is immutable'); END"
        )
    for operation in ("UPDATE", "DELETE"):
        op.execute(
            f"CREATE TRIGGER trg_supplemental_rule_event_immutable_{operation.lower()} "
            f"BEFORE {operation} ON supplemental_rule_event BEGIN SELECT RAISE(ABORT, 'supplemental rule event is immutable'); END"
        )
    op.execute(
        "CREATE TRIGGER trg_supplemental_rule_event_immutable_insert BEFORE INSERT ON supplemental_rule_event "
        "WHEN EXISTS (SELECT 1 FROM supplemental_rule_event WHERE owner_id=NEW.owner_id AND rule_id=NEW.rule_id AND sequence=NEW.sequence) "
        "BEGIN SELECT RAISE(ABORT, 'supplemental rule event is immutable'); END"
    )
    for op_name in ("insert", "update"):
        verb = "INSERT" if op_name == "insert" else "UPDATE"
        op.execute(
            f"CREATE TRIGGER trg_supplemental_rule_head_refs_{op_name} BEFORE {verb} ON supplemental_rule_head WHEN "
            "NOT EXISTS (SELECT 1 FROM supplemental_rule_revision r "
            "WHERE r.owner_id=NEW.owner_id AND r.id=NEW.rule_id) OR "
            "(NEW.active_revision IS NOT NULL AND NOT EXISTS (SELECT 1 FROM supplemental_rule_revision r "
            "WHERE r.owner_id=NEW.owner_id AND r.id=NEW.rule_id AND r.revision=NEW.active_revision)) OR "
            "(NEW.draft_revision IS NOT NULL AND NOT EXISTS (SELECT 1 FROM supplemental_rule_revision r "
            "WHERE r.owner_id=NEW.owner_id AND r.id=NEW.rule_id AND r.revision=NEW.draft_revision)) "
            "OR (NEW.last_validation_revision IS NOT NULL AND (NEW.last_validation_revision IS NOT NEW.draft_revision "
            "OR NOT EXISTS (SELECT 1 FROM supplemental_rule_revision r WHERE r.owner_id=NEW.owner_id "
            "AND r.id=NEW.rule_id AND r.revision=NEW.last_validation_revision))) "
            "BEGIN SELECT RAISE(ABORT, 'supplemental rule head reference owner mismatch'); END"
        )
    op.execute(
        "CREATE TRIGGER trg_supplemental_rule_event_parent_insert BEFORE INSERT ON supplemental_rule_event WHEN "
        "NOT EXISTS (SELECT 1 FROM supplemental_rule_head h WHERE h.owner_id=NEW.owner_id AND h.rule_id=NEW.rule_id) OR "
        "(NEW.rule_revision IS NOT NULL AND NOT EXISTS (SELECT 1 FROM supplemental_rule_revision r "
        "WHERE r.owner_id=NEW.owner_id AND r.id=NEW.rule_id AND r.revision=NEW.rule_revision)) "
        "BEGIN SELECT RAISE(ABORT, 'supplemental rule event parent mismatch'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_research_event_revision_parent_insert BEFORE INSERT ON research_event_revision WHEN "
        "(NEW.revision = 1 AND NEW.previous_ref IS NOT NULL) OR "
        "(NEW.revision > 1 AND (NEW.previous_ref IS NULL OR json_type(NEW.previous_ref) IS NOT 'object' OR "
        "json_type(NEW.previous_ref, '$.kind') IS NOT 'text' OR json_extract(NEW.previous_ref, '$.kind') IS NOT 'research_event' OR "
        "json_type(NEW.previous_ref, '$.id') IS NOT 'text' OR json_extract(NEW.previous_ref, '$.id') IS NOT NEW.id OR "
        "json_type(NEW.previous_ref, '$.revision') IS NOT 'integer' OR json_extract(NEW.previous_ref, '$.revision') IS NOT NEW.revision - 1 OR "
        "NOT EXISTS (SELECT 1 FROM research_event_revision p WHERE p.owner_id=NEW.owner_id AND p.id=NEW.id AND p.revision=NEW.revision-1))) "
        "BEGIN SELECT RAISE(ABORT, 'research event previous revision mismatch'); END"
    )


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        raise NotImplementedError("supplemental rule immutability requires SQLite")
    _drop_050_triggers()
    bind.execute(sa.text("CREATE TEMP TABLE p2_saved_candidate_entries AS SELECT * FROM candidate_ranking_entry"))
    op.drop_index("ix_investment_assessment_owner_listing_kind_asof", table_name="investment_assessment")
    op.drop_index("ix_investment_assessment_owner_account_kind_asof", table_name="investment_assessment")
    op.drop_index("ix_investment_assessment_input", table_name="investment_assessment")
    op.drop_table("candidate_ranking_entry")

    _create_assessment_table("investment_assessment_new", p2=True)
    bind.execute(sa.text(
        f"INSERT INTO investment_assessment_new ({_P1_COLUMNS}, {_P2_EXTRA_COLUMNS}) "
        f"SELECT {_P1_COLUMNS}, NULL, NULL, NULL FROM investment_assessment"
    ))
    op.drop_table("investment_assessment")
    op.execute("ALTER TABLE investment_assessment_new RENAME TO investment_assessment")
    _create_assessment_indexes()
    _create_candidate_entry()
    _create_assessment_guards()
    bind.execute(sa.text(
        "INSERT INTO candidate_ranking_entry "
        "(ranking_id, revision, owner_id, account_id, listing_id, ordinal, payload) "
        "SELECT ranking_id, revision, owner_id, account_id, listing_id, ordinal, payload "
        "FROM p2_saved_candidate_entries ORDER BY ranking_id, revision, ordinal"
    ))
    bind.execute(sa.text("DROP TABLE p2_saved_candidate_entries"))
    _create_p2_tables()


def _p2_business_data_exists() -> bool:
    bind = op.get_bind()
    checks = (
        "SELECT 1 FROM supplemental_rule_revision LIMIT 1",
        "SELECT 1 FROM supplemental_rule_head LIMIT 1",
        "SELECT 1 FROM supplemental_rule_event LIMIT 1",
        "SELECT 1 FROM research_event_revision LIMIT 1",
        "SELECT 1 FROM investment_assessment WHERE kind = 'rule_application' LIMIT 1",
        "SELECT 1 FROM pipeline_run WHERE lower(kind) LIKE '%supplemental_rule%' OR lower(kind) LIKE '%rule_application%' LIMIT 1",
        "SELECT 1 FROM investor_mutation WHERE json_valid(result) AND ("
        "json_extract(result, '$.p2_schema_version') = 1 OR "
        "json_extract(result, '$.receipt.result_kind') IN ('rule_mutation','event_mutation','evaluation_admission') OR "
        "json_extract(result, '$.result_kind') IN ('rule_mutation','event_mutation','evaluation_admission')) LIMIT 1",
    )
    return any(bind.execute(sa.text(query)).first() is not None for query in checks)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        raise NotImplementedError("supplemental rule immutability requires SQLite")
    if _p2_business_data_exists():
        raise RuntimeError("cannot downgrade 051 while P2 definitions, events, applications, runs, or receipts exist")
    registry_rows = bind.execute(sa.text(
        "SELECT count(*) FROM supplemental_registry_head "
        "WHERE registry_revision <> 0 OR rules_generation <> 0 OR events_generation <> 0"
    )).scalar_one()
    if registry_rows:
        raise RuntimeError("cannot downgrade 051 while supplemental registry history exists")
    _drop_050_triggers()
    for trigger in (
        "trg_supplemental_rule_head_refs_insert", "trg_supplemental_rule_head_refs_update",
        "trg_supplemental_rule_event_parent_insert", "trg_research_event_revision_parent_insert",
        "trg_supplemental_rule_revision_immutable_insert", "trg_supplemental_rule_revision_immutable_update",
        "trg_supplemental_rule_revision_immutable_delete", "trg_supplemental_rule_event_immutable_insert",
        "trg_supplemental_rule_event_immutable_update", "trg_supplemental_rule_event_immutable_delete",
        "trg_research_event_revision_immutable_insert", "trg_research_event_revision_immutable_update",
        "trg_research_event_revision_immutable_delete",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    for table in (
        "research_event_revision", "supplemental_registry_head", "supplemental_rule_event",
        "supplemental_rule_head", "supplemental_rule_revision",
    ):
        op.drop_table(table)

    bind.execute(sa.text("CREATE TEMP TABLE p2_saved_candidate_entries AS SELECT * FROM candidate_ranking_entry"))
    op.drop_index("ix_investment_assessment_rule_application_live", table_name="investment_assessment")
    op.drop_index("ix_investment_assessment_owner_listing_kind_asof", table_name="investment_assessment")
    op.drop_index("ix_investment_assessment_owner_account_kind_asof", table_name="investment_assessment")
    op.drop_index("ix_investment_assessment_input", table_name="investment_assessment")
    op.drop_table("candidate_ranking_entry")

    _create_assessment_table("investment_assessment_old", p2=False)
    bind.execute(sa.text(
        f"INSERT INTO investment_assessment_old ({_P1_COLUMNS}) "
        f"SELECT {_P1_COLUMNS} FROM investment_assessment WHERE kind IN ('account_fit','candidate_ranking')"
    ))
    op.drop_table("investment_assessment")
    op.execute("ALTER TABLE investment_assessment_old RENAME TO investment_assessment")
    op.create_index("ix_investment_assessment_input", "investment_assessment", ["owner_id", "kind", "account_id", "input_hash"])
    op.create_index("ix_investment_assessment_owner_account_kind_asof", "investment_assessment", ["owner_id", "account_id", "kind", "as_of"])
    op.create_index("ix_investment_assessment_owner_listing_kind_asof", "investment_assessment", ["owner_id", "listing_id", "kind", "as_of"])
    _create_candidate_entry()
    _create_assessment_guards()
    bind.execute(sa.text(
        "INSERT INTO candidate_ranking_entry "
        "(ranking_id, revision, owner_id, account_id, listing_id, ordinal, payload) "
        "SELECT ranking_id, revision, owner_id, account_id, listing_id, ordinal, payload "
        "FROM p2_saved_candidate_entries ORDER BY ranking_id, revision, ordinal"
    ))
    bind.execute(sa.text("DROP TABLE p2_saved_candidate_entries"))
