"""DB-backed runtime settings (docs/product/settings-plan.md)

* ``app_setting`` - overridden runtime keys; secrets only as Fernet
  ciphertext + last-4 hint.
* ``app_setting_change`` - append-only change history.
* ``app_setting_meta`` - single row ``id = 1``: ``revision`` (bumped per
  write) and ``imported_at`` (one-time env / app.yaml import).

The meta row is seeded with revision 0 and no import, so the first process
to start imports the existing env / app.yaml values.

Revision ID: 021
Revises: 020
Create Date: 2026-10-08

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "021"
down_revision: str | Sequence[str] | None = "020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "app_setting",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value_json", sa.JSON(), nullable=True),
        sa.Column("ciphertext", sa.Text(), nullable=True),
        sa.Column("secret_hint", sa.String(length=8), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_by", sa.String(length=64), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )
    op.create_table(
        "app_setting_change",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("old_value_json", sa.JSON(), nullable=True),
        sa.Column("new_value_json", sa.JSON(), nullable=True),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor", sa.String(length=64), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_app_setting_change_key", "app_setting_change", ["key"])
    op.create_table(
        "app_setting_meta",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute("INSERT INTO app_setting_meta (id, revision, imported_at) VALUES (1, 0, NULL)")


def downgrade() -> None:
    op.drop_table("app_setting_meta")
    op.drop_index("ix_app_setting_change_key", table_name="app_setting_change")
    op.drop_table("app_setting_change")
    op.drop_table("app_setting")
