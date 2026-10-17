"""Protect B poll receipts and V3 coverage from downgrade to schema 2 readers."""

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "054"
down_revision: str | Sequence[str] | None = "053"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_B_RECORD_TYPES = {
    "poll_final_v1", "poll_page_v1", "poll_document_leaf_v1",
    "poll_document_v1", "poll_root_v1", "poll_directory_v1",
    "poll_index_v1", "poll_http_response_v1", "http_attempt_v1",
    "poll_step_v1", "source_authorization_v1",
}

_B_AUTHORIZATION_FIELDS = {
    "authorization_id", "revision", "owner_id", "source", "feed_id",
    "contract_hash", "listing_refs", "doc_type_allowlist", "history_start",
    "history_end", "allow_overlap", "allow_deep_sweep",
    "min_poll_interval_seconds", "min_http_interval_seconds", "expires_at",
    "max_staleness_seconds", "per_poll_query_http", "per_poll_fetch",
    "per_poll_http", "per_poll_response_bytes", "grant_daily_http",
    "grant_daily_response_bytes", "storage_bytes_cap", "created_at", "actor",
    "status", "revoked_at", "revocation_reason", "authorization_hash",
}


def _has_b_payload(value: object) -> bool:
    if isinstance(value, dict):
        schema = value.get("schema_version")
        binding = value.get("source_coverage_binding")
        if type(schema) is int and schema == 3 and isinstance(binding, dict) and (
            {"listing_ref", "context", "scope_key", "sources", "source_policy_hash", "observed_at"}
            <= binding.keys()
        ):
            return True
        if value.get("proof_type") == "poll_window_v1":
            return True
        if value.get("record_type") in _B_RECORD_TYPES:
            return True
        authorization = value.get("authorization")
        if (
            isinstance(authorization, dict)
            and authorization.keys() >= _B_AUTHORIZATION_FIELDS
            and authorization.get("source") == "cninfo.v1"
            and authorization.get("feed_id") == "cninfo_szse_v1"
        ):
            return True
        return any(_has_b_payload(child) for child in value.values())
    if isinstance(value, list):
        return any(_has_b_payload(child) for child in value)
    return False


def _guarded_legacy_payload_exists() -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    candidates = {
        "investment_assessment": ("payload",),
        "pipeline_run": ("request_json", "metadata_json", "checkpoint", "config_snapshot"),
        "agent_task": ("payload", "checkpoint", "resolved", "consumed_events"),
        "agent_execution_grant": ("payload",),
        "disclosure_receipt": ("payload",),
    }
    for table, wanted in candidates.items():
        if table not in inspector.get_table_names():
            continue
        columns = {column["name"] for column in inspector.get_columns(table)}
        for column in wanted:
            if column not in columns:
                continue
            for (raw,) in bind.execute(sa.text(
                f"SELECT {column} FROM {table} WHERE {column} IS NOT NULL"
            )):
                try:
                    value = json.loads(raw) if isinstance(raw, str) else raw
                except (TypeError, ValueError) as exc:
                    # We cannot prove that malformed legacy payloads are free of
                    # poll/V3 state, so preserve the schema that understands them.
                    raise RuntimeError(
                        f"cannot verify B/V3 payload in {table}.{column}"
                    ) from exc
                if _has_b_payload(value):
                    return True
    return False


def upgrade() -> None:
    op.create_index(
        "ix_supplemental_change_poll_final_cutoff",
        "supplemental_change_receipt",
        ["owner_id", "listing_id", "context", "committed_at", "sequence"],
        unique=False,
        sqlite_where=sa.text(
            "kind = 'source_page' AND json_extract(payload, '$.record_type') = 'poll_final_v1'"
        ),
    )


def downgrade() -> None:
    for table in (
        "supplemental_change_receipt",
        "supplemental_source_state",
        "supplemental_refresh_state",
    ):
        if table in sa.inspect(op.get_bind()).get_table_names() and op.get_bind().execute(
            sa.text(f"SELECT 1 FROM {table} LIMIT 1")
        ).first() is not None:
            raise RuntimeError("cannot downgrade while P4 poll state exists")
    if _guarded_legacy_payload_exists():
        raise RuntimeError("cannot downgrade while B poll or V3 coverage payload exists")
    if "supplemental_change_receipt" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_index(
            "ix_supplemental_change_poll_final_cutoff",
            table_name="supplemental_change_receipt",
        )
