"""Add immutable account-fit assessments and complete ranking snapshots."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "050"
down_revision: str | Sequence[str] | None = "049"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "sqlite":
        raise NotImplementedError("investment assessment immutability requires SQLite")
    op.create_table(
        "investment_assessment",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("owner_id", sa.String(length=32), nullable=False),
        sa.Column("kind", sa.String(length=24), nullable=False),
        sa.Column("account_id", sa.String(length=36), nullable=False),
        sa.Column("listing_id", sa.Integer(), nullable=True),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("entry_count", sa.Integer(), nullable=True),
        sa.Column("manifest_hash", sa.String(length=64), nullable=True),
        sa.Column("universe_hash", sa.String(length=64), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id", "revision", name="pk_investment_assessment"),
        sa.CheckConstraint("revision = 1", name="ck_investment_assessment_revision_one"),
        sa.CheckConstraint(
            "kind IN ('account_fit', 'candidate_ranking')",
            name="ck_investment_assessment_kind",
        ),
        sa.CheckConstraint(
            "(kind = 'account_fit' AND listing_id IS NOT NULL AND entry_count IS NULL "
            "AND manifest_hash IS NULL AND universe_hash IS NULL) "
            "OR (kind = 'candidate_ranking' AND listing_id IS NULL AND entry_count IS NOT NULL "
            "AND entry_count >= 0 AND manifest_hash IS NOT NULL AND universe_hash IS NOT NULL)",
            name="ck_investment_assessment_shape",
        ),
        sa.CheckConstraint("json_valid(payload)", name="ck_investment_assessment_payload_json"),
        sa.CheckConstraint("json_type(payload) = 'object'", name="ck_investment_assessment_payload_object"),
    )
    op.create_index(
        "ix_investment_assessment_input", "investment_assessment",
        ["owner_id", "kind", "account_id", "input_hash"],
    )
    op.create_index(
        "ix_investment_assessment_owner_account_kind_asof",
        "investment_assessment", ["owner_id", "account_id", "kind", "as_of"],
    )
    op.create_index(
        "ix_investment_assessment_owner_listing_kind_asof",
        "investment_assessment", ["owner_id", "listing_id", "kind", "as_of"],
    )
    op.create_table(
        "candidate_ranking_entry",
        sa.Column("ranking_id", sa.String(length=36), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("owner_id", sa.String(length=32), nullable=False),
        sa.Column("account_id", sa.String(length=36), nullable=False),
        sa.Column("listing_id", sa.Integer(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint(
            "ranking_id", "revision", "listing_id", name="pk_candidate_ranking_entry",
        ),
        sa.ForeignKeyConstraint(
            ["ranking_id", "revision"],
            ["investment_assessment.id", "investment_assessment.revision"],
            name="fk_candidate_ranking_entry_assessment",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("revision = 1", name="ck_candidate_ranking_entry_revision_one"),
        sa.CheckConstraint("ordinal >= 0", name="ck_candidate_ranking_entry_ordinal_nonnegative"),
        sa.CheckConstraint("json_valid(payload)", name="ck_candidate_ranking_entry_payload_json"),
        sa.CheckConstraint("json_type(payload) = 'object'", name="ck_candidate_ranking_entry_payload_object"),
        sa.UniqueConstraint(
            "ranking_id", "revision", "listing_id", name="uq_candidate_ranking_entry_listing",
        ),
        sa.UniqueConstraint(
            "ranking_id", "revision", "ordinal", name="uq_candidate_ranking_entry_ordinal",
        ),
    )
    op.create_index(
        "ix_candidate_ranking_entry_page",
        "candidate_ranking_entry", ["ranking_id", "revision", "ordinal"],
    )

    for table in ("investment_assessment", "candidate_ranking_entry"):
        for operation in ("UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER IF NOT EXISTS trg_{table}_immutable_{operation.lower()} "
                f"BEFORE {operation} ON {table} "
                "BEGIN SELECT RAISE(ABORT, 'investment assessment is immutable'); END"
            )
    op.execute(
        "CREATE TRIGGER IF NOT EXISTS trg_investment_assessment_immutable_insert "
        "BEFORE INSERT ON investment_assessment "
        "WHEN EXISTS (SELECT 1 FROM investment_assessment WHERE id = NEW.id) "
        "BEGIN SELECT RAISE(ABORT, 'investment assessment is immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER IF NOT EXISTS trg_candidate_ranking_entry_immutable_insert "
        "BEFORE INSERT ON candidate_ranking_entry "
        "WHEN EXISTS (SELECT 1 FROM candidate_ranking_entry WHERE "
        "(ranking_id = NEW.ranking_id AND revision = NEW.revision AND listing_id = NEW.listing_id) "
        "OR (ranking_id = NEW.ranking_id AND revision = NEW.revision AND ordinal = NEW.ordinal)) "
        "BEGIN SELECT RAISE(ABORT, 'candidate ranking entry is immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER IF NOT EXISTS trg_candidate_ranking_entry_parent_insert "
        "BEFORE INSERT ON candidate_ranking_entry "
        "WHEN NOT EXISTS (SELECT 1 FROM investment_assessment AS a "
        "WHERE a.id = NEW.ranking_id AND a.revision = NEW.revision "
        "AND a.kind = 'candidate_ranking' AND a.owner_id = NEW.owner_id "
        "AND a.account_id = NEW.account_id AND a.entry_count > 0 "
        "AND NEW.ordinal < a.entry_count "
        "AND (SELECT count(*) FROM candidate_ranking_entry AS e "
        "WHERE e.ranking_id = NEW.ranking_id AND e.revision = NEW.revision) < a.entry_count) "
        "BEGIN SELECT RAISE(ABORT, 'invalid or complete candidate ranking parent'); END"
    )


def downgrade() -> None:
    for trigger in (
        "trg_candidate_ranking_entry_parent_insert",
        "trg_candidate_ranking_entry_immutable_insert",
        "trg_investment_assessment_immutable_insert",
        "trg_candidate_ranking_entry_immutable_delete",
        "trg_candidate_ranking_entry_immutable_update",
        "trg_investment_assessment_immutable_delete",
        "trg_investment_assessment_immutable_update",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    op.drop_index("ix_candidate_ranking_entry_page", table_name="candidate_ranking_entry")
    op.drop_table("candidate_ranking_entry")
    op.drop_index(
        "ix_investment_assessment_owner_listing_kind_asof", table_name="investment_assessment",
    )
    op.drop_index(
        "ix_investment_assessment_owner_account_kind_asof", table_name="investment_assessment",
    )
    op.drop_index("ix_investment_assessment_input", table_name="investment_assessment")
    op.drop_table("investment_assessment")
