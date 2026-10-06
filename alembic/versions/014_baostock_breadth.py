"""baostock breadth columns and report_overdue (P1-A7, P1-A6)

* ``market_snapshot`` gains the per-day valuation / status columns baostock
  returns with every bar: ``pe_ttm``, ``pb_mrq``, ``ps_ttm``, ``turnover``
  (fraction), ``trade_status`` (1 trading, 0 suspended) and ``is_st``. All are
  nullable: existing iFinD rows keep ``NULL``.
* ``report_overdue``: a target-pool member without the report of a period
  after its statutory deadline; ``resolved_on`` is set (history kept) when the
  late report arrives.

Revision ID: 014
Revises: 013
Create Date: 2026-10-06

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "014"
down_revision: str | Sequence[str] | None = "013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("market_snapshot") as batch:
        batch.add_column(sa.Column("pe_ttm", sa.Numeric(precision=18, scale=6), nullable=True))
        batch.add_column(sa.Column("pb_mrq", sa.Numeric(precision=18, scale=6), nullable=True))
        batch.add_column(sa.Column("ps_ttm", sa.Numeric(precision=18, scale=6), nullable=True))
        batch.add_column(
            sa.Column("turnover", sa.Numeric(precision=18, scale=8), nullable=True)
        )
        batch.add_column(sa.Column("trade_status", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("is_st", sa.Boolean(), nullable=True))

    op.create_table(
        "report_overdue",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("listing_id", sa.Integer(), nullable=False),
        sa.Column("report_period", sa.Date(), nullable=False),
        sa.Column("deadline", sa.Date(), nullable=False),
        sa.Column("detected_on", sa.Date(), nullable=False),
        sa.Column("resolved_on", sa.Date(), nullable=True),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("detected_run_id", sa.Integer(), nullable=True),
        sa.Column("resolved_run_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["listing_id"], ["listing.id"]),
        sa.ForeignKeyConstraint(["detected_run_id"], ["pipeline_run.id"]),
        sa.ForeignKeyConstraint(["resolved_run_id"], ["pipeline_run.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("listing_id", "report_period", name="uq_report_overdue_period"),
    )
    op.create_index(
        "ix_report_overdue_period", "report_overdue", ["report_period"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_report_overdue_period", table_name="report_overdue")
    op.drop_table("report_overdue")
    with op.batch_alter_table("market_snapshot") as batch:
        batch.drop_column("is_st")
        batch.drop_column("trade_status")
        batch.drop_column("turnover")
        batch.drop_column("ps_ttm")
        batch.drop_column("pb_mrq")
        batch.drop_column("pe_ttm")
