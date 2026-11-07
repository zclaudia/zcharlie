"""Rebuildable inbox projections and their immutable operation journal.

The EV cursor owns the active generation. Foreign keys document relationships;
repositories must verify them explicitly because SQLite FK enforcement is off.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    DDL,
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import Mapped, mapped_column

from investment_agent.storage.models.base import Base


class ResearchInboxEntry(Base):
    __tablename__ = "research_inbox_entry"
    __table_args__ = (
        UniqueConstraint("generation", "owner_id", "group_key", name="uq_inbox_entry_group"),
        CheckConstraint(
            "(subject_kind = 'listing' AND listing_id IS NOT NULL AND account_id IS NULL) OR "
            "(subject_kind = 'account' AND account_id IS NOT NULL AND listing_id IS NULL)",
            name="ck_inbox_entry_subject"),
        CheckConstraint(
            "(case_id IS NULL AND case_episode IS NULL) OR "
            "(subject_kind = 'listing' AND case_id IS NOT NULL AND "
            "case_episode IS NOT NULL AND case_episode >= 1)", name="ck_inbox_entry_case"),
        CheckConstraint(
            "0 <= read_content_revision AND read_content_revision <= content_revision AND "
            "1 <= content_revision AND content_revision <= revision", name="ck_inbox_entry_revisions"),
        CheckConstraint("state IN ('open','snoozed','in_review','resolved','withdrawn')", name="ck_inbox_entry_state"),
        CheckConstraint("priority_rank >= 0", name="ck_inbox_entry_priority"),
        CheckConstraint("length(group_key) > 0 AND length(control_status) > 0", name="ck_inbox_entry_labels"),
        CheckConstraint(
            "source_status IN ('current','source_unavailable','source_invalidated','superseded','historical')",
            name="ck_inbox_entry_source_status"),
        CheckConstraint("last_event_sequence >= 0 AND latest_journal_sequence >= 0", name="ck_inbox_entry_sequences"),
        CheckConstraint("json_valid(view) AND json_type(view) = 'object'", name="ck_inbox_entry_view"),
        Index("ix_inbox_entry_pending", "generation", "owner_id", "state", "priority_rank", "latest_journal_sequence", "id"),
        Index("ix_inbox_entry_listing", "owner_id", "listing_id"),
        Index("ix_inbox_entry_account", "owner_id", "account_id"),
        Index("ix_inbox_entry_case", "case_id", "case_episode"),
    )

    generation: Mapped[str] = mapped_column(String(36), primary_key=True)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(32))
    subject_kind: Mapped[str] = mapped_column(String(16))
    listing_id: Mapped[int | None] = mapped_column(ForeignKey("listing.id"))
    account_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("portfolio_account.id"))
    case_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("investor_case.id"))
    case_episode: Mapped[int | None]
    group_key: Mapped[str] = mapped_column(String(250))
    revision: Mapped[int] = mapped_column(default=1, server_default="1")
    content_revision: Mapped[int] = mapped_column(default=1, server_default="1")
    state: Mapped[str] = mapped_column(String(24))
    read_content_revision: Mapped[int] = mapped_column(default=0, server_default="0")
    priority_rank: Mapped[int]
    snoozed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    control_status: Mapped[str] = mapped_column(String(24))
    source_status: Mapped[str] = mapped_column(String(24))
    last_event_sequence: Mapped[int] = mapped_column(default=0, server_default="0")
    latest_journal_sequence: Mapped[int] = mapped_column(default=0, server_default="0")
    view: Mapped[dict[str, Any]] = mapped_column(JSON)


class ResearchInboxJournal(Base):
    __tablename__ = "research_inbox_journal"
    __table_args__ = (
        UniqueConstraint("generation", "domain_event_id", "effect_ordinal", name="uq_inbox_journal_effect"),
        UniqueConstraint("owner_id", "request_id", name="uq_inbox_journal_operation"),
        ForeignKeyConstraint(["generation", "entry_id"], ["research_inbox_entry.generation", "research_inbox_entry.id"]),
        CheckConstraint("sequence >= 1", name="ck_inbox_journal_sequence"),
        CheckConstraint("kind IN ('source','user','clock','control','repair')", name="ck_inbox_journal_kind"),
        CheckConstraint("effect_ordinal >= 0", name="ck_inbox_journal_ordinal"),
        CheckConstraint(
            "(request_id IS NULL AND request_hash IS NULL) OR "
            "(request_id IS NOT NULL AND request_hash IS NOT NULL AND length(request_hash) = 64)",
            name="ck_inbox_journal_request"),
        CheckConstraint(
            "(entry_id IS NULL AND entry_revision IS NULL AND entry_view IS NULL) OR "
            "(entry_id IS NOT NULL AND entry_revision IS NOT NULL AND entry_revision >= 1 AND "
            "entry_view IS NOT NULL AND json_valid(entry_view) AND json_type(entry_view) = 'object')",
            name="ck_inbox_journal_entry_view"),
        CheckConstraint("json_valid(result) AND json_type(result) = 'object'", name="ck_inbox_journal_result"),
        Index("ix_inbox_journal_entry", "generation", "entry_id", "sequence"),
        Index("ix_inbox_journal_owner", "owner_id", "sequence"),
        {"sqlite_autoincrement": True},
    )

    sequence: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    generation: Mapped[str] = mapped_column(String(36))
    entry_id: Mapped[str | None] = mapped_column(String(36))
    kind: Mapped[str] = mapped_column(String(24))
    domain_event_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("research_domain_event.event_id"))
    effect_ordinal: Mapped[int] = mapped_column(default=0, server_default="0")
    owner_id: Mapped[str] = mapped_column(String(32))
    request_id: Mapped[str | None] = mapped_column(String(36))
    request_hash: Mapped[str | None] = mapped_column(String(64))
    result: Mapped[dict[str, Any]] = mapped_column(JSON)
    entry_revision: Mapped[int | None]
    entry_view: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ResearchInboxEntrySource(Base):
    __tablename__ = "research_inbox_entry_source"
    __table_args__ = (
        ForeignKeyConstraint(["generation", "entry_id"], ["research_inbox_entry.generation", "research_inbox_entry.id"]),
        CheckConstraint("relation IN ('unlinked','support','opposition','context')", name="ck_inbox_source_relation"),
        CheckConstraint("group_member_ordinal >= 0", name="ck_inbox_source_ordinal"),
        CheckConstraint("json_valid(exact_ref) AND json_type(exact_ref) = 'object'", name="ck_inbox_source_ref"),
        CheckConstraint(
            "relation_review_ref IS NULL OR (json_valid(relation_review_ref) AND json_type(relation_review_ref) = 'object')",
            name="ck_inbox_source_relation_ref"),
        CheckConstraint("relation NOT IN ('support','opposition') OR relation_review_ref IS NOT NULL", name="ck_inbox_source_reviewed"),
        Index("ix_inbox_source_event", "event_id"),
        Index("ix_inbox_source_active", "generation", "entry_id", "active"),
    )

    generation: Mapped[str] = mapped_column(String(36), primary_key=True)
    entry_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    event_id: Mapped[str] = mapped_column(String(36), ForeignKey("research_domain_event.event_id"), primary_key=True)
    exact_ref: Mapped[dict[str, Any]] = mapped_column(JSON)
    relation: Mapped[str] = mapped_column(String(16))
    relation_review_ref: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True))
    active: Mapped[bool] = mapped_column(Boolean(create_constraint=True, name="ck_inbox_source_active"))
    invalidated_by: Mapped[str | None] = mapped_column(String(36), ForeignKey("research_domain_event.event_id"))
    group_member_ordinal: Mapped[int]


def _immutable(*args: object) -> None:
    raise ValueError("research inbox journal is immutable")


event.listen(ResearchInboxJournal, "before_update", _immutable)
event.listen(ResearchInboxJournal, "before_delete", _immutable)
INBOX_JOURNAL_IMMUTABILITY_DDL = (
    "CREATE TRIGGER IF NOT EXISTS trg_research_inbox_journal_immutable_update "
    "BEFORE UPDATE ON research_inbox_journal BEGIN "
    "SELECT RAISE(ABORT, 'research inbox journal is immutable'); END",
    "CREATE TRIGGER IF NOT EXISTS trg_research_inbox_journal_immutable_delete "
    "BEFORE DELETE ON research_inbox_journal BEGIN "
    "SELECT RAISE(ABORT, 'research inbox journal is immutable'); END",
    "CREATE TRIGGER IF NOT EXISTS trg_research_inbox_journal_immutable_insert "
    "BEFORE INSERT ON research_inbox_journal WHEN EXISTS "
    "(SELECT 1 FROM research_inbox_journal WHERE sequence = NEW.sequence OR "
    "(generation = NEW.generation AND domain_event_id = NEW.domain_event_id AND effect_ordinal = NEW.effect_ordinal) OR "
    "(owner_id = NEW.owner_id AND request_id = NEW.request_id)) "
    "BEGIN SELECT RAISE(ABORT, 'research inbox journal identity already exists'); END",
)
for _statement in INBOX_JOURNAL_IMMUTABILITY_DDL:
    event.listen(ResearchInboxJournal.__table__, "after_create", DDL(_statement).execute_if(dialect="sqlite"))  # type: ignore[no-untyped-call]
