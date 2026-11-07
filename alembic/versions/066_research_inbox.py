"""Frozen inbox projection/journal schema and explicit review-cycle event version.

Revision ID: 066
Revises: 065

Existing cycle payload/state/task identities remain byte-for-byte unchanged.
"""

import sqlalchemy as sa
from alembic import op

revision = "066"
down_revision = "065"
branch_labels = None
depends_on = None

_STATEMENTS = (
    "ALTER TABLE investor_review_cycle ADD COLUMN cycle_event_revision INTEGER DEFAULT '0' NOT NULL CONSTRAINT ck_review_cycle_event_revision CHECK (cycle_event_revision >= 0)",
    'CREATE TABLE research_inbox_entry (\n\tgeneration VARCHAR(36) NOT NULL, \n\tid VARCHAR(36) NOT NULL, \n\towner_id VARCHAR(32) NOT NULL, \n\tsubject_kind VARCHAR(16) NOT NULL, \n\tlisting_id INTEGER, \n\taccount_id VARCHAR(36), \n\tcase_id VARCHAR(36), \n\tcase_episode INTEGER, \n\tgroup_key VARCHAR(250) NOT NULL, \n\trevision INTEGER DEFAULT \'1\' NOT NULL, \n\tcontent_revision INTEGER DEFAULT \'1\' NOT NULL, \n\tstate VARCHAR(24) NOT NULL, \n\tread_content_revision INTEGER DEFAULT \'0\' NOT NULL, \n\tpriority_rank INTEGER NOT NULL, \n\tsnoozed_until DATETIME, \n\tcontrol_status VARCHAR(24) NOT NULL, \n\tsource_status VARCHAR(24) NOT NULL, \n\tlast_event_sequence INTEGER DEFAULT \'0\' NOT NULL, \n\tlatest_journal_sequence INTEGER DEFAULT \'0\' NOT NULL, \n\t"view" JSON NOT NULL, \n\tPRIMARY KEY (generation, id), \n\tCONSTRAINT uq_inbox_entry_group UNIQUE (generation, owner_id, group_key), \n\tCONSTRAINT ck_inbox_entry_subject CHECK ((subject_kind = \'listing\' AND listing_id IS NOT NULL AND account_id IS NULL) OR (subject_kind = \'account\' AND account_id IS NOT NULL AND listing_id IS NULL)), \n\tCONSTRAINT ck_inbox_entry_case CHECK ((case_id IS NULL AND case_episode IS NULL) OR (subject_kind = \'listing\' AND case_id IS NOT NULL AND case_episode IS NOT NULL AND case_episode >= 1)), \n\tCONSTRAINT ck_inbox_entry_revisions CHECK (0 <= read_content_revision AND read_content_revision <= content_revision AND 1 <= content_revision AND content_revision <= revision), \n\tCONSTRAINT ck_inbox_entry_state CHECK (state IN (\'open\',\'snoozed\',\'in_review\',\'resolved\',\'withdrawn\')), \n\tCONSTRAINT ck_inbox_entry_priority CHECK (priority_rank >= 0), \n\tCONSTRAINT ck_inbox_entry_labels CHECK (length(group_key) > 0 AND length(control_status) > 0), \n\tCONSTRAINT ck_inbox_entry_source_status CHECK (source_status IN (\'current\',\'source_unavailable\',\'source_invalidated\',\'superseded\',\'historical\')), \n\tCONSTRAINT ck_inbox_entry_sequences CHECK (last_event_sequence >= 0 AND latest_journal_sequence >= 0), \n\tCONSTRAINT ck_inbox_entry_view CHECK (json_valid(view) AND json_type(view) = \'object\'), \n\tFOREIGN KEY(listing_id) REFERENCES listing (id), \n\tFOREIGN KEY(account_id) REFERENCES portfolio_account (id), \n\tFOREIGN KEY(case_id) REFERENCES investor_case (id)\n)',
    'CREATE INDEX ix_inbox_entry_account ON research_inbox_entry (owner_id, account_id)',
    'CREATE INDEX ix_inbox_entry_case ON research_inbox_entry (case_id, case_episode)',
    'CREATE INDEX ix_inbox_entry_listing ON research_inbox_entry (owner_id, listing_id)',
    'CREATE INDEX ix_inbox_entry_pending ON research_inbox_entry (generation, owner_id, state, priority_rank, latest_journal_sequence, id)',
    "CREATE TABLE research_inbox_journal (\n\tsequence INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, \n\tgeneration VARCHAR(36) NOT NULL, \n\tentry_id VARCHAR(36), \n\tkind VARCHAR(24) NOT NULL, \n\tdomain_event_id VARCHAR(36), \n\teffect_ordinal INTEGER DEFAULT '0' NOT NULL, \n\towner_id VARCHAR(32) NOT NULL, \n\trequest_id VARCHAR(36), \n\trequest_hash VARCHAR(64), \n\tresult JSON NOT NULL, \n\tentry_revision INTEGER, \n\tentry_view JSON, \n\trecorded_at DATETIME NOT NULL, \n\tCONSTRAINT uq_inbox_journal_effect UNIQUE (generation, domain_event_id, effect_ordinal), \n\tCONSTRAINT uq_inbox_journal_operation UNIQUE (owner_id, request_id), \n\tFOREIGN KEY(generation, entry_id) REFERENCES research_inbox_entry (generation, id), \n\tCONSTRAINT ck_inbox_journal_sequence CHECK (sequence >= 1), \n\tCONSTRAINT ck_inbox_journal_kind CHECK (kind IN ('source','user','clock','control','repair')), \n\tCONSTRAINT ck_inbox_journal_ordinal CHECK (effect_ordinal >= 0), \n\tCONSTRAINT ck_inbox_journal_request CHECK ((request_id IS NULL AND request_hash IS NULL) OR (request_id IS NOT NULL AND request_hash IS NOT NULL AND length(request_hash) = 64)), \n\tCONSTRAINT ck_inbox_journal_entry_view CHECK ((entry_id IS NULL AND entry_revision IS NULL AND entry_view IS NULL) OR (entry_id IS NOT NULL AND entry_revision IS NOT NULL AND entry_revision >= 1 AND entry_view IS NOT NULL AND json_valid(entry_view) AND json_type(entry_view) = 'object')), \n\tCONSTRAINT ck_inbox_journal_result CHECK (json_valid(result) AND json_type(result) = 'object'), \n\tFOREIGN KEY(domain_event_id) REFERENCES research_domain_event (event_id)\n)",
    'CREATE INDEX ix_inbox_journal_entry ON research_inbox_journal (generation, entry_id, sequence)',
    'CREATE INDEX ix_inbox_journal_owner ON research_inbox_journal (owner_id, sequence)',
    "CREATE TABLE research_inbox_entry_source (\n\tgeneration VARCHAR(36) NOT NULL, \n\tentry_id VARCHAR(36) NOT NULL, \n\tevent_id VARCHAR(36) NOT NULL, \n\texact_ref JSON NOT NULL, \n\trelation VARCHAR(16) NOT NULL, \n\trelation_review_ref JSON, \n\tactive BOOLEAN NOT NULL, \n\tinvalidated_by VARCHAR(36), \n\tgroup_member_ordinal INTEGER NOT NULL, \n\tPRIMARY KEY (generation, entry_id, event_id), \n\tFOREIGN KEY(generation, entry_id) REFERENCES research_inbox_entry (generation, id), \n\tCONSTRAINT ck_inbox_source_relation CHECK (relation IN ('unlinked','support','opposition','context')), \n\tCONSTRAINT ck_inbox_source_ordinal CHECK (group_member_ordinal >= 0), \n\tCONSTRAINT ck_inbox_source_ref CHECK (json_valid(exact_ref) AND json_type(exact_ref) = 'object'), \n\tCONSTRAINT ck_inbox_source_relation_ref CHECK (relation_review_ref IS NULL OR (json_valid(relation_review_ref) AND json_type(relation_review_ref) = 'object')), \n\tCONSTRAINT ck_inbox_source_reviewed CHECK (relation NOT IN ('support','opposition') OR relation_review_ref IS NOT NULL), \n\tFOREIGN KEY(event_id) REFERENCES research_domain_event (event_id), \n\tCONSTRAINT ck_inbox_source_active CHECK (active IN (0, 1)), \n\tFOREIGN KEY(invalidated_by) REFERENCES research_domain_event (event_id)\n)",
    'CREATE INDEX ix_inbox_source_active ON research_inbox_entry_source (generation, entry_id, active)',
    'CREATE INDEX ix_inbox_source_event ON research_inbox_entry_source (event_id)',
    "CREATE TRIGGER IF NOT EXISTS trg_research_inbox_journal_immutable_update BEFORE UPDATE ON research_inbox_journal BEGIN SELECT RAISE(ABORT, 'research inbox journal is immutable'); END",
    "CREATE TRIGGER IF NOT EXISTS trg_research_inbox_journal_immutable_delete BEFORE DELETE ON research_inbox_journal BEGIN SELECT RAISE(ABORT, 'research inbox journal is immutable'); END",
    "CREATE TRIGGER IF NOT EXISTS trg_research_inbox_journal_immutable_insert BEFORE INSERT ON research_inbox_journal WHEN EXISTS (SELECT 1 FROM research_inbox_journal WHERE sequence = NEW.sequence OR (generation = NEW.generation AND domain_event_id = NEW.domain_event_id AND effect_ordinal = NEW.effect_ordinal) OR (owner_id = NEW.owner_id AND request_id = NEW.request_id)) BEGIN SELECT RAISE(ABORT, 'research inbox journal identity already exists'); END",
)

_TABLES = ('research_inbox_entry_source', 'research_inbox_journal', 'research_inbox_entry')


def upgrade() -> None:
    for statement in _STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    connection = op.get_bind()
    for table in _TABLES:
        if connection.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first() is not None:
            raise RuntimeError("066 downgrade refused: inbox history must be retained")
    if connection.execute(sa.text(
        "SELECT 1 FROM investor_review_cycle WHERE cycle_event_revision != 0 LIMIT 1"
    )).first() is not None:
        raise RuntimeError("066 downgrade refused: review-cycle event versions must be retained")
    for table in _TABLES:
        op.drop_table(table)
    op.execute("ALTER TABLE investor_review_cycle DROP COLUMN cycle_event_revision")
