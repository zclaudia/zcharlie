"""Rule templates: actor and note on every stored version (config-docs WP4, CD7)."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "059"
down_revision: str | Sequence[str] | None = "058"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "rule_template",
        sa.Column("created_by", sa.String(64), nullable=True),
    )
    op.add_column(
        "rule_template",
        sa.Column("note", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("rule_template", "note")
    op.drop_column("rule_template", "created_by")
