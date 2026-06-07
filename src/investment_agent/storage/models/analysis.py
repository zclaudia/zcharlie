"""Strategy + PipelineRun + StockScore + DiscoveryReport + AttributionReport."""

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


class Strategy(Base, TimestampMixin):
    __tablename__ = "strategy"

    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[str] = mapped_column(String(16), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    config_json: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class PipelineRun(Base, TimestampMixin):
    __tablename__ = "pipeline_run"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    universe_code: Mapped[str | None] = mapped_column(
        ForeignKey("universe.code"), nullable=True
    )
    listing_id: Mapped[int | None] = mapped_column(
        ForeignKey("listing.id"), nullable=True
    )
    triggered_by: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False, default=dict)


class StockScore(Base, TimestampMixin):
    __tablename__ = "stock_score"
    __table_args__ = (
        UniqueConstraint(
            "pipeline_run_id", "listing_id", "strategy_code", name="uq_stock_score"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    pipeline_run_id: Mapped[int] = mapped_column(
        ForeignKey("pipeline_run.id"), nullable=False, index=True
    )
    listing_id: Mapped[int] = mapped_column(
        ForeignKey("listing.id"), nullable=False, index=True
    )
    strategy_code: Mapped[str] = mapped_column(
        ForeignKey("strategy.code"), nullable=False
    )
    tier: Mapped[int] = mapped_column(nullable=False)
    score: Mapped[int] = mapped_column(nullable=False)
    confidence: Mapped[Decimal] = mapped_column(Numeric(4, 3), nullable=False)
    bull_thesis: Mapped[str | None] = mapped_column(Text, nullable=True)
    bear_thesis: Mapped[str | None] = mapped_column(Text, nullable=True)
    key_metrics: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False, default=dict)
    deep_analysis: Mapped[str | None] = mapped_column(Text, nullable=True)


class DiscoveryReport(Base, TimestampMixin):
    __tablename__ = "discovery_report"

    pipeline_run_id: Mapped[int] = mapped_column(
        ForeignKey("pipeline_run.id"), primary_key=True
    )
    consensus_picks: Mapped[list[object]] = mapped_column(JSON, nullable=False, default=list)
    divergent_picks: Mapped[list[object]] = mapped_column(JSON, nullable=False, default=list)
    overlooked_picks: Mapped[list[object]] = mapped_column(JSON, nullable=False, default=list)
    markdown_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)


class AttributionReport(Base, TimestampMixin):
    __tablename__ = "attribution_report"

    pipeline_run_id: Mapped[int] = mapped_column(
        ForeignKey("pipeline_run.id"), primary_key=True
    )
    listing_id: Mapped[int] = mapped_column(
        ForeignKey("listing.id"), nullable=False
    )
    analysis_date: Mapped[date] = mapped_column(Date, nullable=False)
    price_change_pct: Mapped[Decimal] = mapped_column(Numeric(8, 4), nullable=False)
    beta_attribution: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False, default=dict)
    triggering_events: Mapped[list[object]] = mapped_column(JSON, nullable=False, default=list)
    narrative: Mapped[str] = mapped_column(Text, nullable=False)
    valuation_delta: Mapped[str | None] = mapped_column(Text, nullable=True)
    markdown_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
