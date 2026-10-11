"""Conversation activity time, editable title revision and subject projection."""

import json
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision = "041"
down_revision = "040"
branch_labels = None
depends_on = None

# Object kinds that resolve to a listing through a direct primary-key relation
# at this revision. Anything else keeps an empty projection: the identity stays
# unknown instead of being guessed.
_DIRECT_LISTING_TABLES = {
    "candidate": "candidate",
    "watchlist_item": "watchlist_item",
    "financial_snapshot": "financial_snapshot",
    "market_snapshot": "market_snapshot",
    "earnings_forecast": "earnings_forecast",
    "disclosure_schedule": "disclosure_schedule",
    "business_segment": "business_segment",
    "case": "investor_case",
}


def _payload_time(value: object) -> datetime:
    """Parse a contract timestamp; naive values are invalid, never assumed UTC."""
    if not isinstance(value, str):
        raise ValueError("payload timestamps must be ISO strings")
    at = datetime.fromisoformat(value)
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("payload timestamps must include a timezone")
    return at


def _storage_string(at: datetime) -> str:
    # Same six-digit microsecond UTC format the ORM DateTime column writes on
    # SQLite; comparisons and keyset pagination rely on this exact shape.
    return at.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")


def _resolve_listing(bind, ref: dict) -> int | None:
    kind = ref.get("kind")
    identifier = ref.get("id")
    if kind == "listing":
        return identifier if isinstance(identifier, int) else None
    table = _DIRECT_LISTING_TABLES.get(str(kind))
    if table is not None:
        if not isinstance(identifier, str) and not isinstance(identifier, int):
            return None
        found = bind.execute(sa.text(f"SELECT listing_id FROM {table} WHERE id = :id"),
            {"id": identifier}).scalar()
        return int(found) if found is not None else None
    if kind in {"thesis", "recommendation"}:
        case_id = bind.execute(sa.text(
            "SELECT case_id FROM investor_publication_artifact WHERE id = :id AND revision = :revision"),
            {"id": ref.get("id"), "revision": ref.get("revision")}).scalar()
        if case_id is None:
            return None
        found = bind.execute(sa.text("SELECT listing_id FROM investor_case WHERE id = :id"),
            {"id": case_id}).scalar()
        return int(found) if found is not None else None
    return None


def upgrade() -> None:
    bind = op.get_bind()
    # Validate and compute every backfill value BEFORE any DDL: the pysqlite
    # driver autocommits DDL, so a late failure could not roll the columns back.
    # A missing message falls back to the conversation creation time; an invalid
    # payload time fails the whole migration instead of being replaced by now().
    activities: list[tuple[str, str, str]] = []
    for row in bind.execute(sa.text("SELECT id, payload FROM agent_conversation")).all():
        payload = json.loads(row.payload) if isinstance(row.payload, str) else row.payload
        times = [_payload_time(payload["created_at"])]
        for (message_payload,) in bind.execute(
            sa.text("SELECT payload FROM agent_message WHERE conversation_id = :id"), {"id": row.id}
        ).all():
            message = json.loads(message_payload) if isinstance(message_payload, str) else message_payload
            times.append(_payload_time(message["created_at"]))
        subjects: list[dict] = []
        seen: set[int] = set()
        for ref in payload.get("context_refs", ()) or ():
            listing_id = _resolve_listing(bind, ref) if isinstance(ref, dict) else None
            if listing_id is not None and listing_id not in seen:
                seen.add(listing_id)
                subjects.append({"kind": "listing", "id": listing_id, "revision": 1})
        activities.append((row.id, _storage_string(max(times)), json.dumps(subjects)))
    op.add_column("agent_conversation", sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("agent_conversation", sa.Column("title_revision", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("agent_conversation", sa.Column("subject_refs", sa.JSON(), nullable=False, server_default="[]"))
    op.create_index("ix_agent_conversation_activity", "agent_conversation", ["owner_id", "last_activity_at", "id"])
    for conversation_id, activity, refs in activities:
        bind.execute(sa.text(
            "UPDATE agent_conversation SET last_activity_at = :at, subject_refs = :refs WHERE id = :id"),
            {"at": activity, "refs": refs, "id": conversation_id})


def downgrade() -> None:
    with op.batch_alter_table("agent_conversation") as batch:
        batch.drop_index("ix_agent_conversation_activity")
        batch.drop_column("subject_refs")
        batch.drop_column("title_revision")
        batch.drop_column("last_activity_at")
