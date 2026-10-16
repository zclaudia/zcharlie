"""Immutable account-fit results and complete account-ranking snapshots."""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    DDL,
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    event,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from investment_agent.storage.models.base import Base


class InvestmentAssessment(Base):
    """One immutable account-fit or account-ranking artifact (revision 1)."""

    __tablename__ = "investment_assessment"
    __table_args__ = (
        CheckConstraint("revision = 1", name="ck_investment_assessment_revision_one"),
        CheckConstraint(
            "kind IN ('account_fit', 'candidate_ranking', 'rule_application')",
            name="ck_investment_assessment_kind",
        ),
        CheckConstraint(
            "(kind = 'account_fit' AND account_id IS NOT NULL AND listing_id IS NOT NULL AND entry_count IS NULL "
            "AND manifest_hash IS NULL AND universe_hash IS NULL) "
            "OR (kind = 'candidate_ranking' AND account_id IS NOT NULL AND listing_id IS NULL AND entry_count IS NOT NULL "
            "AND entry_count >= 0 AND manifest_hash IS NOT NULL AND universe_hash IS NOT NULL) "
            "OR (kind = 'rule_application' AND account_id IS NULL AND listing_id IS NOT NULL "
            "AND entry_count IS NULL AND manifest_hash IS NULL AND universe_hash IS NULL "
            "AND series_key IS NOT NULL AND application_sequence IS NOT NULL AND application_sequence >= 1 "
            "AND application_mode IS NOT NULL AND application_mode IN ('live','historical_replay'))",
            name="ck_investment_assessment_shape",
        ),
        CheckConstraint(
            "(kind = 'rule_application' AND series_key IS NOT NULL AND application_sequence IS NOT NULL "
            "AND application_mode IS NOT NULL) OR (kind <> 'rule_application' AND series_key IS NULL "
            "AND application_sequence IS NULL AND application_mode IS NULL)",
            name="ck_investment_assessment_series_shape",
        ),
        UniqueConstraint(
            "owner_id", "listing_id", "series_key", "application_sequence",
            name="uq_investment_assessment_rule_application_sequence",
        ),
        CheckConstraint("json_valid(payload)", name="ck_investment_assessment_payload_json"),
        CheckConstraint("json_type(payload) = 'object'", name="ck_investment_assessment_payload_object"),
        Index(
            "ix_investment_assessment_input", "owner_id", "kind", "account_id", "input_hash",
        ),
        Index(
            "ix_investment_assessment_owner_account_kind_asof",
            "owner_id", "account_id", "kind", "as_of",
        ),
        Index(
            "ix_investment_assessment_owner_listing_kind_asof",
            "owner_id", "listing_id", "kind", "as_of",
        ),
        Index(
            "ix_investment_assessment_rule_application_live",
            "owner_id", "listing_id", "series_key", "application_mode", "application_sequence",
        ),
        Index(
            "ix_investment_assessment_rule_application_operation",
            "owner_id",
            text("json_extract(payload, '$.operation_id')"),
            sqlite_where=text("kind = 'rule_application'"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    owner_id: Mapped[str] = mapped_column(String(32), nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    account_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    listing_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    entry_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    manifest_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    universe_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    series_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    application_sequence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    application_mode: Mapped[str | None] = mapped_column(String(24), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class CandidateRankingEntry(Base):
    """One immutable member of a ranking snapshot's complete ordered set."""

    __tablename__ = "candidate_ranking_entry"
    __table_args__ = (
        CheckConstraint("revision = 1", name="ck_candidate_ranking_entry_revision_one"),
        CheckConstraint("ordinal >= 0", name="ck_candidate_ranking_entry_ordinal_nonnegative"),
        CheckConstraint("json_valid(payload)", name="ck_candidate_ranking_entry_payload_json"),
        CheckConstraint("json_type(payload) = 'object'", name="ck_candidate_ranking_entry_payload_object"),
        ForeignKeyConstraint(
            ["ranking_id", "revision"],
            ["investment_assessment.id", "investment_assessment.revision"],
            name="fk_candidate_ranking_entry_assessment",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "ranking_id", "revision", "listing_id",
            name="uq_candidate_ranking_entry_listing",
        ),
        UniqueConstraint(
            "ranking_id", "revision", "ordinal",
            name="uq_candidate_ranking_entry_ordinal",
        ),
        Index(
            "ix_candidate_ranking_entry_page",
            "ranking_id", "revision", "ordinal",
        ),
    )

    ranking_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(32), nullable=False)
    account_id: Mapped[str] = mapped_column(String(36), nullable=False)
    listing_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


def _immutable(*args: object) -> None:
    raise ValueError("investment assessments and ranking entries are immutable")


for _model in (InvestmentAssessment, CandidateRankingEntry):
    event.listen(_model, "before_update", _immutable)
    event.listen(_model, "before_delete", _immutable)

    for _operation in ("UPDATE", "DELETE"):
        event.listen(
            _model.__table__,
            "after_create",
            DDL(  # type: ignore[no-untyped-call]
                f"CREATE TRIGGER IF NOT EXISTS trg_{_model.__tablename__}_immutable_{_operation.lower()} "
                f"BEFORE {_operation} ON {_model.__tablename__} "
                "BEGIN SELECT RAISE(ABORT, 'investment assessment is immutable'); END"
            ).execute_if(dialect="sqlite"),
        )


event.listen(
    InvestmentAssessment.__table__,
    "after_create",
    DDL(  # type: ignore[no-untyped-call]
        "CREATE TRIGGER IF NOT EXISTS trg_investment_assessment_immutable_insert "
        "BEFORE INSERT ON investment_assessment "
        "WHEN EXISTS (SELECT 1 FROM investment_assessment WHERE id = NEW.id) "
        "BEGIN SELECT RAISE(ABORT, 'investment assessment is immutable'); END"
    ).execute_if(dialect="sqlite"),
)

event.listen(
    CandidateRankingEntry.__table__,
    "after_create",
    DDL(  # type: ignore[no-untyped-call]
        "CREATE TRIGGER IF NOT EXISTS trg_candidate_ranking_entry_immutable_insert "
        "BEFORE INSERT ON candidate_ranking_entry "
        "WHEN EXISTS (SELECT 1 FROM candidate_ranking_entry WHERE "
        "(ranking_id = NEW.ranking_id AND revision = NEW.revision AND listing_id = NEW.listing_id) "
        "OR (ranking_id = NEW.ranking_id AND revision = NEW.revision AND ordinal = NEW.ordinal)) "
        "BEGIN SELECT RAISE(ABORT, 'candidate ranking entry is immutable'); END"
    ).execute_if(dialect="sqlite"),
)

event.listen(
    CandidateRankingEntry.__table__,
    "after_create",
    DDL(  # type: ignore[no-untyped-call]
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
    ).execute_if(dialect="sqlite"),
)
