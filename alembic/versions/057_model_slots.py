"""Move model settings onto providers, models, and consumer slot refs.

Legacy ``agent_*`` model keys and ``llm_tier3_*`` keys become catalog rows plus
``model_slots``. The catalog no longer stores which consumer uses a model.
An empty settings table is unchanged. Downgrade does not restore the legacy
keys: the new rows remain readable only by the new code.

Revision ID: 057
Revises: 056
"""

from collections.abc import Sequence

from alembic import op

revision: str = "057"
down_revision: str | Sequence[str] | None = "056"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    from investment_agent.settings.model_migration import apply_model_migration

    apply_model_migration(op.get_bind())


def downgrade() -> None:
    # The legacy keys are not reconstructed. Slot refs and the catalog stay.
    return
