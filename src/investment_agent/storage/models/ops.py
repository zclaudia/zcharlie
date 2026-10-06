"""LLMCall + DataFetchJob + DeepAnalysisCache.

Legacy tables (from the retired NASDAQ Discovery/Attribution product, preserved
at tag ``legacy-nasdaq-final``) keep their ORM classes so ``Base.metadata``
keeps matching the alembic-migrated schema; nothing at runtime uses them.
Legacy here: DataFetchJob, DeepAnalysisCache.
"""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from investment_agent.storage.models.base import Base, TimestampMixin


class LLMCall(Base, TimestampMixin):
    __tablename__ = "llm_call"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    pipeline_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("pipeline_run.id"), nullable=True, index=True
    )
    purpose: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(nullable=False)
    completion_tokens: Mapped[int] = mapped_column(nullable=False)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False)
    latency_ms: Mapped[int] = mapped_column(nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    success: Mapped[bool] = mapped_column(Boolean, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class DataFetchJob(Base, TimestampMixin):
    __tablename__ = "data_fetch_job"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    target_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    records_fetched: Mapped[int] = mapped_column(nullable=False, default=0)
    errors: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False, default=dict)


class DeepAnalysisCache(Base, TimestampMixin):
    __tablename__ = "deep_analysis_cache"
    __table_args__ = (
        UniqueConstraint(
            "listing_id",
            "latest_financial_report_period",
            "strategy_code",
            name="uq_deep_cache_key",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    listing_id: Mapped[int] = mapped_column(
        ForeignKey("listing.id"), nullable=False, index=True
    )
    latest_financial_report_period: Mapped[str] = mapped_column(
        String(16), nullable=False
    )
    strategy_code: Mapped[str] = mapped_column(
        ForeignKey("strategy.code"), nullable=False
    )
    analysis_json: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
