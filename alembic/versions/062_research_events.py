"""Durable research events, consumer receipts and projection cursor.

Revision ID: 062
Revises: 061

DDL is frozen here; historical migrations never import live application models.
Existing event or consumer history makes a downgrade unsafe, so it is refused.
"""

import sqlalchemy as sa
from alembic import op

revision = "062"
down_revision = "061"
branch_labels = None
depends_on = None

_STATEMENTS = (
    "CREATE TABLE research_domain_event (\n\tsequence INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, \n\tevent_id VARCHAR(36) NOT NULL, \n\towner_id VARCHAR(32) NOT NULL, \n\tevent_type VARCHAR(64) NOT NULL, \n\tsource_identity VARCHAR(250) NOT NULL, \n\tsource_ref JSON NOT NULL, \n\tsource_revision INTEGER NOT NULL, \n\toccurred_at DATETIME NOT NULL, \n\trecorded_at DATETIME NOT NULL, \n\tschema_version INTEGER NOT NULL, \n\tpayload_hash VARCHAR(64) NOT NULL, \n\tpayload JSON NOT NULL, \n\tCONSTRAINT uq_research_domain_event_source UNIQUE (owner_id, event_type, source_identity, source_revision), \n\tCONSTRAINT ck_domain_event_sequence CHECK (sequence >= 1), \n\tCONSTRAINT ck_domain_event_source_revision CHECK (source_revision >= 1), \n\tCONSTRAINT ck_domain_event_schema_version CHECK (schema_version >= 1), \n\tCONSTRAINT ck_domain_event_payload_hash CHECK (length(payload_hash) = 64), \n\tCONSTRAINT ck_domain_event_source_object CHECK (json_valid(source_ref) AND json_type(source_ref) = 'object'), \n\tCONSTRAINT ck_domain_event_payload_object CHECK (json_valid(payload) AND json_type(payload) = 'object'), \n\tUNIQUE (event_id)\n)",
    'CREATE INDEX ix_research_domain_event_owner_sequence ON research_domain_event (owner_id, sequence)',
    'CREATE INDEX ix_research_domain_event_type_sequence ON research_domain_event (event_type, sequence)',
    "CREATE TABLE consumer_receipt (\n\tconsumer VARCHAR(80) NOT NULL, \n\tevent_id VARCHAR(36) NOT NULL, \n\tstate VARCHAR(24) NOT NULL, \n\tordering_key VARCHAR(250) NOT NULL, \n\teffect_manifest JSON, \n\teffect_manifest_hash VARCHAR(64), \n\teffect_cursor INTEGER, \n\tattempts INTEGER DEFAULT '0' NOT NULL, \n\tnext_retry_at DATETIME, \n\tlast_error_code VARCHAR(160), \n\terror_summary VARCHAR(500), \n\tprocessed_at DATETIME, \n\tprojector_version VARCHAR(80) NOT NULL, \n\teffect_refs JSON NOT NULL, \n\tPRIMARY KEY (consumer, event_id), \n\tCONSTRAINT ck_consumer_receipt_name CHECK (length(consumer) > 0), \n\tCONSTRAINT ck_consumer_receipt_state CHECK (state IN ('applied','ignored','retry','blocked_predecessor','quarantined')), \n\tCONSTRAINT ck_consumer_receipt_attempts CHECK (attempts >= 0), \n\tCONSTRAINT ck_consumer_receipt_effect_cursor CHECK (effect_cursor IS NULL OR effect_cursor >= 0), \n\tCONSTRAINT ck_consumer_receipt_manifest_fields CHECK ((effect_manifest IS NULL AND effect_manifest_hash IS NULL AND effect_cursor IS NULL) OR (effect_manifest IS NOT NULL AND effect_manifest_hash IS NOT NULL AND effect_cursor IS NOT NULL AND length(effect_manifest_hash) = 64)), \n\tCONSTRAINT ck_consumer_receipt_manifest_object CHECK (effect_manifest IS NULL OR (json_valid(effect_manifest) AND json_type(effect_manifest) = 'object')), \n\tFOREIGN KEY(event_id) REFERENCES research_domain_event (event_id)\n)",
    'CREATE INDEX ix_consumer_receipt_ordering ON consumer_receipt (consumer, ordering_key, state)',
    'CREATE INDEX ix_consumer_receipt_retry ON consumer_receipt (consumer, state, next_retry_at)',
    "CREATE TABLE projection_cursor (\n\tconsumer VARCHAR(80) NOT NULL, \n\tscanned_sequence INTEGER DEFAULT '0' NOT NULL, \n\trevision INTEGER DEFAULT '1' NOT NULL, \n\tupdated_at DATETIME NOT NULL, \n\tlast_success_at DATETIME, \n\tretry_count INTEGER DEFAULT '0' NOT NULL, \n\tquarantined_count INTEGER DEFAULT '0' NOT NULL, \n\tactive_generation VARCHAR(36), \n\trebuild_state JSON, \n\tPRIMARY KEY (consumer), \n\tCONSTRAINT ck_projection_cursor_name CHECK (length(consumer) > 0), \n\tCONSTRAINT ck_projection_cursor_sequence CHECK (scanned_sequence >= 0), \n\tCONSTRAINT ck_projection_cursor_revision CHECK (revision >= 1), \n\tCONSTRAINT ck_projection_cursor_counts CHECK (retry_count >= 0 AND quarantined_count >= 0), \n\tCONSTRAINT ck_projection_cursor_inbox_generation CHECK (consumer = 'research_inbox.v1' OR (active_generation IS NULL AND rebuild_state IS NULL)), \n\tCONSTRAINT ck_projection_cursor_rebuild_object CHECK (rebuild_state IS NULL OR (active_generation IS NOT NULL AND json_valid(rebuild_state) AND json_type(rebuild_state) = 'object'))\n)",
    "CREATE TRIGGER IF NOT EXISTS trg_research_domain_event_immutable_update BEFORE UPDATE ON research_domain_event BEGIN SELECT RAISE(ABORT, 'research domain events are immutable'); END",
    "CREATE TRIGGER IF NOT EXISTS trg_research_domain_event_immutable_delete BEFORE DELETE ON research_domain_event BEGIN SELECT RAISE(ABORT, 'research domain events are immutable'); END",
    "CREATE TRIGGER IF NOT EXISTS trg_research_domain_event_immutable_insert BEFORE INSERT ON research_domain_event WHEN EXISTS (SELECT 1 FROM research_domain_event WHERE sequence = NEW.sequence OR event_id = NEW.event_id OR (owner_id = NEW.owner_id AND event_type = NEW.event_type AND source_identity = NEW.source_identity AND source_revision = NEW.source_revision)) BEGIN SELECT RAISE(ABORT, 'research domain event identity already exists'); END",
)


def upgrade() -> None:
    for statement in _STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    connection = op.get_bind()
    for table in ("consumer_receipt", "projection_cursor", "research_domain_event"):
        if connection.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first() is not None:
            raise RuntimeError("062 downgrade refused: research event history must be retained")
    for table in ("consumer_receipt", "projection_cursor", "research_domain_event"):
        op.drop_table(table)
