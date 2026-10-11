"""Bounded discovery and purpose classification in the existing cost ledger."""
import sqlalchemy as sa
from alembic import op

revision = "034"
down_revision = "033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agent_budget_entry", sa.Column("purpose", sa.String(16), nullable=False, server_default="interactive"))
    # Trusted queue kind identifies legacy reviews; global totals retain all rows.
    op.execute("UPDATE agent_budget_entry SET purpose='review' WHERE task_id IN (SELECT agent_task.id FROM agent_task JOIN pipeline_run ON pipeline_run.id=agent_task.run_id WHERE pipeline_run.kind='agent_review')")
    # Authenticated review messages used agent_turn, so also recover the saved
    # server-owned research_kind. Never infer purpose from user/model text.
    op.execute("UPDATE agent_budget_entry SET purpose='review' WHERE task_id IN (SELECT task_id FROM agent_execution_grant WHERE json_extract(payload, '$.research_kind')='review')")
    op.create_table("investor_discovery_cursor",
        sa.Column("listing_id", sa.Integer(), sa.ForeignKey("listing.id"), primary_key=True),
        sa.Column("payload", sa.JSON(), nullable=False))
    op.create_table("investor_discovery_batch",
        sa.Column("sequence", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("id", sa.String(36), nullable=False, unique=True),
        sa.Column("owner_id", sa.String(32), nullable=False),
        sa.Column("task_id", sa.String(36), sa.ForeignKey("agent_task.id"), nullable=True, unique=True),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False), sqlite_autoincrement=True)
    for field in ("owner_id", "state"):
        op.create_index(f"ix_investor_discovery_batch_{field}", "investor_discovery_batch", [field])


def downgrade() -> None:
    op.drop_table("investor_discovery_batch")
    op.drop_table("investor_discovery_cursor")
    with op.batch_alter_table("agent_budget_entry") as batch:
        batch.drop_column("purpose")
