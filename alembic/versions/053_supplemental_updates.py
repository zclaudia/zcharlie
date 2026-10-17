"""Add the P4 immutable change journal and source/refresh CAS projections."""

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "053"
down_revision: str | Sequence[str] | None = "052"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "supplemental_change_receipt",
        sa.Column("sequence", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("id", sa.String(36), nullable=False, unique=True),
        sa.Column("owner_id", sa.String(32), nullable=False),
        sa.Column("change_key", sa.String(200), nullable=False),
        sa.Column("schema_version", sa.Integer, nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("operation_id", sa.String(36), nullable=True),
        sa.Column("request_hash", sa.String(64), nullable=True),
        sa.Column(
            "parent_sequence",
            sa.Integer,
            sa.ForeignKey("supplemental_change_receipt.sequence"),
            nullable=True,
        ),
        sa.Column("listing_id", sa.Integer, nullable=True),
        sa.Column("context", sa.String(32), nullable=True),
        sa.Column("origin_run_id", sa.Integer, nullable=True),
        sa.Column("source_manifest_hash", sa.String(64), nullable=True),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("available_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("committed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.CheckConstraint("sequence >= 1", name="ck_supplemental_change_sequence_positive"),
        sa.CheckConstraint("schema_version = 1", name="ck_supplemental_change_schema_version"),
        sa.CheckConstraint("length(payload_hash) = 64", name="ck_supplemental_change_payload_hash"),
        sa.CheckConstraint(
            "request_hash IS NULL OR length(request_hash) = 64",
            name="ck_supplemental_change_request_hash",
        ),
        sa.CheckConstraint(
            "source_manifest_hash IS NULL OR length(source_manifest_hash) = 64",
            name="ck_supplemental_change_manifest_hash",
        ),
        sa.CheckConstraint("json_valid(payload)", name="ck_supplemental_change_payload_json"),
        sa.CheckConstraint(
            "json_type(payload) = 'object'", name="ck_supplemental_change_payload_object"
        ),
        sa.CheckConstraint(
            "(kind = 'scope_member' AND parent_sequence IS NOT NULL AND listing_id IS NOT NULL AND context IS NOT NULL) "
            "OR kind <> 'scope_member'",
            name="ck_supplemental_change_member_shape",
        ),
        sa.UniqueConstraint("owner_id", "change_key", name="uq_supplemental_change_owner_key"),
    )
    op.create_index(
        "uq_supplemental_change_owner_operation",
        "supplemental_change_receipt",
        ["owner_id", "operation_id"],
        unique=True,
        sqlite_where=sa.text("operation_id IS NOT NULL"),
    )
    op.create_index(
        "uq_supplemental_change_parent_member",
        "supplemental_change_receipt",
        ["parent_sequence", "listing_id", "context"],
        unique=True,
        sqlite_where=sa.text("kind = 'scope_member'"),
    )
    op.create_index(
        "ix_supplemental_change_owner_sequence",
        "supplemental_change_receipt",
        ["owner_id", "sequence"],
    )
    op.create_index(
        "ix_supplemental_change_owner_kind_sequence",
        "supplemental_change_receipt",
        ["owner_id", "kind", "sequence"],
    )
    op.create_index(
        "ix_supplemental_change_owner_member_outcome_latest",
        "supplemental_change_receipt",
        ["owner_id", sa.text("json_extract(payload, '$.member_sequence')"), sa.text("sequence DESC")],
        unique=False,
        sqlite_where=sa.text("kind = 'scope_outcome'"),
    )
    op.create_index(
        "ix_supplemental_change_owner_scope_sequence",
        "supplemental_change_receipt",
        ["owner_id", "listing_id", "context", "sequence"],
    )
    op.create_index(
        "ix_supplemental_change_origin_run",
        "supplemental_change_receipt",
        ["owner_id", "origin_run_id"],
    )
    op.create_index(
        "ix_supplemental_change_evidence_proof",
        "supplemental_change_receipt",
        ["owner_id", "listing_id", "context", "committed_at", "sequence"],
        sqlite_where=sa.text("kind = 'evidence_revision'"),
    )
    for operation in ("UPDATE", "DELETE"):
        op.execute(
            f"CREATE TRIGGER trg_supplemental_change_immutable_{operation.lower()} "
            "BEFORE " + operation + " ON supplemental_change_receipt "
            "BEGIN SELECT RAISE(ABORT, 'supplemental change receipt is immutable'); END"
        )
    op.execute(
        "CREATE TRIGGER trg_supplemental_change_immutable_insert "
        "BEFORE INSERT ON supplemental_change_receipt WHEN EXISTS "
        "(SELECT 1 FROM supplemental_change_receipt WHERE owner_id=NEW.owner_id AND change_key=NEW.change_key) "
        "BEGIN SELECT RAISE(ABORT, 'supplemental change receipt identity already exists'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_supplemental_change_member_parent "
        "BEFORE INSERT ON supplemental_change_receipt WHEN NEW.kind='scope_member' AND NOT EXISTS "
        "(SELECT 1 FROM supplemental_change_receipt p WHERE p.sequence=NEW.parent_sequence "
        "AND p.owner_id=NEW.owner_id AND p.kind<>'scope_member') "
        "BEGIN SELECT RAISE(ABORT, 'scope member requires an owner-matched non-member parent'); END"
    )

    op.create_table(
        "supplemental_source_state",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("owner_id", sa.String(32), nullable=False),
        sa.Column("feed_id", sa.String(64), nullable=False),
        sa.Column("scope_key", sa.String(200), nullable=False),
        sa.Column("revision", sa.Integer, nullable=False),
        sa.Column("listing_id", sa.Integer, nullable=True),
        sa.Column("context", sa.String(32), nullable=True),
        sa.Column("config_hash", sa.String(64), nullable=False),
        sa.Column("contract_version", sa.String(32), nullable=False),
        sa.Column("scope_hash", sa.String(64), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("complete_watermark", sa.DateTime(timezone=True), nullable=True),
        sa.Column("coverage_state", sa.String(16), nullable=False),
        sa.Column("success_sequence", sa.Integer, nullable=False),
        sa.Column("success_receipt_hash", sa.String(64), nullable=True),
        sa.Column("source_refs", sa.JSON, nullable=False),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_code", sa.String(64), nullable=True),
        sa.Column("retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("health_limit_seconds", sa.Integer, nullable=False),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.CheckConstraint("revision >= 0", name="ck_supplemental_source_revision_nonnegative"),
        sa.CheckConstraint("length(config_hash) = 64", name="ck_supplemental_source_config_hash"),
        sa.CheckConstraint("length(scope_hash) = 64", name="ck_supplemental_source_scope_hash"),
        sa.CheckConstraint(
            "coverage_state IN ('complete','partial','unknown','failed')",
            name="ck_supplemental_source_coverage_state",
        ),
        sa.CheckConstraint("success_sequence >= 0", name="ck_supplemental_source_success_sequence"),
        sa.CheckConstraint("health_limit_seconds >= 0", name="ck_supplemental_source_health_limit"),
        sa.CheckConstraint(
            "success_receipt_hash IS NULL OR length(success_receipt_hash) = 64",
            name="ck_supplemental_source_success_receipt_hash",
        ),
        sa.CheckConstraint("json_valid(source_refs)", name="ck_supplemental_source_refs_json"),
        sa.CheckConstraint("json_valid(payload)", name="ck_supplemental_source_payload_json"),
        sa.CheckConstraint(
            "json_type(payload) = 'object'", name="ck_supplemental_source_payload_object"
        ),
        sa.UniqueConstraint(
            "owner_id", "feed_id", "scope_key", name="uq_supplemental_source_scope"
        ),
    )
    op.create_index(
        "ix_supplemental_source_health_scope",
        "supplemental_source_state",
        ["owner_id", "feed_id", "listing_id", "context"],
    )

    op.create_table(
        "supplemental_refresh_state",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("owner_id", sa.String(32), nullable=False),
        sa.Column("listing_id", sa.Integer, nullable=False),
        sa.Column("context", sa.String(32), nullable=False),
        sa.Column("revision", sa.Integer, nullable=False),
        sa.Column("confirmed_sequence", sa.Integer, nullable=False),
        sa.Column("requested_sequence", sa.Integer, nullable=False),
        sa.Column("active_run_id", sa.Integer, nullable=True),
        sa.Column("next_due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success", sa.DateTime(timezone=True), nullable=True),
        sa.Column("blocked_reason", sa.String(64), nullable=True),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.CheckConstraint("revision >= 0", name="ck_supplemental_refresh_revision_nonnegative"),
        sa.CheckConstraint(
            "confirmed_sequence >= 0", name="ck_supplemental_refresh_confirmed_nonnegative"
        ),
        sa.CheckConstraint(
            "requested_sequence >= confirmed_sequence",
            name="ck_supplemental_refresh_requested_ge_confirmed",
        ),
        sa.CheckConstraint("json_valid(payload)", name="ck_supplemental_refresh_payload_json"),
        sa.CheckConstraint(
            "json_type(payload) = 'object'", name="ck_supplemental_refresh_payload_object"
        ),
        sa.UniqueConstraint(
            "owner_id", "listing_id", "context", name="uq_supplemental_refresh_scope"
        ),
    )
    op.create_index(
        "ix_supplemental_refresh_owner_due",
        "supplemental_refresh_state",
        ["owner_id", "next_due_at"],
    )
    op.create_index(
        "ix_supplemental_refresh_active_run",
        "supplemental_refresh_state",
        ["owner_id", "active_run_id"],
    )


_P4_KEYS = {
    "supplemental_updates",
    "supplemental_update_source_binding",
    "update_source_binding",
    "source_bindings_v2",
    "p4_source_binding",
    "p4_update_state",
    "p4_enabled",
    "supplemental_update_sequence",
    "supplemental_update_state",
    "supplemental_update_batch_id",
    "supplemental_update_batch_hash",
    "p4_resume_revision",
}


def _has_p4_binding(value: object) -> bool:
    if isinstance(value, str):
        return value == "supplemental_rule.update_recovery.v1"
    if isinstance(value, dict):
        return any(
            key in _P4_KEYS
            or (isinstance(key, str) and key.startswith("p4_"))
            or _has_p4_binding(child)
            for key, child in value.items()
        )
    if isinstance(value, list):
        return any(_has_p4_binding(child) for child in value)
    return False


def _downgrade_has_legacy_payload() -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    candidates = (
        "investment_assessment",
        "pipeline_run",
        "agent_task",
        "agent_execution_grant",
    )
    for table in candidates:
        if table not in inspector.get_table_names():
            continue
        columns = {column["name"] for column in inspector.get_columns(table)}
        if (table == "pipeline_run" and "kind" in columns
            and bind.execute(sa.text(
                "SELECT 1 FROM pipeline_run WHERE kind='supplemental_update_process' LIMIT 1"
            )).first() is not None):
            return True
        payload_columns = [
            name
            for name in (
                "payload", "request", "request_json", "metadata_json", "checkpoint",
                "config_snapshot",
            )
            if name in columns
        ]
        for column in payload_columns:
            rows = bind.execute(sa.text(f"SELECT {column} FROM {table} WHERE {column} IS NOT NULL"))
            for (raw,) in rows:
                try:
                    if _has_p4_binding(json.loads(raw) if isinstance(raw, str) else raw):
                        return True
                except (TypeError, ValueError) as exc:
                    # Unknown legacy data cannot safely be discarded. A corrupt
                    # JSON blob may contain the only remaining P4 grant or
                    # source binding, so fail closed instead of guessing.
                    raise RuntimeError(
                        f"cannot verify legacy P4 payload in {table}.{column}"
                    ) from exc
    return False


def downgrade() -> None:
    bind = op.get_bind()
    for table in (
        "supplemental_change_receipt",
        "supplemental_source_state",
        "supplemental_refresh_state",
    ):
        if bind.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first() is not None:
            raise RuntimeError("cannot downgrade while supplemental update state exists")
    if _downgrade_has_legacy_payload():
        raise RuntimeError(
            "cannot downgrade while P4 source binding or state is stored in legacy payloads"
        )

    op.drop_index("ix_supplemental_refresh_active_run", table_name="supplemental_refresh_state")
    op.drop_index("ix_supplemental_refresh_owner_due", table_name="supplemental_refresh_state")
    op.drop_table("supplemental_refresh_state")
    op.drop_index("ix_supplemental_source_health_scope", table_name="supplemental_source_state")
    op.drop_table("supplemental_source_state")
    op.drop_index("ix_supplemental_change_origin_run", table_name="supplemental_change_receipt")
    op.drop_index("ix_supplemental_change_evidence_proof", table_name="supplemental_change_receipt")
    op.drop_index(
        "ix_supplemental_change_owner_scope_sequence", table_name="supplemental_change_receipt"
    )
    op.drop_index(
        "ix_supplemental_change_owner_member_outcome_latest",
        table_name="supplemental_change_receipt",
    )
    op.drop_index(
        "ix_supplemental_change_owner_kind_sequence", table_name="supplemental_change_receipt"
    )
    op.drop_index("ix_supplemental_change_owner_sequence", table_name="supplemental_change_receipt")
    op.drop_index("uq_supplemental_change_parent_member", table_name="supplemental_change_receipt")
    op.drop_index(
        "uq_supplemental_change_owner_operation", table_name="supplemental_change_receipt"
    )
    for trigger in (
        "trg_supplemental_change_immutable_insert",
        "trg_supplemental_change_immutable_update",
        "trg_supplemental_change_immutable_delete",
        "trg_supplemental_change_member_parent",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    op.drop_table("supplemental_change_receipt")
