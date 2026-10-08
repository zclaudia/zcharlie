"""Local stock directory for name / pinyin search (mrd-intake D4, migration 023).

``stock_directory`` - one row per A-share listing ever seen in baostock's
``query_all_stock`` (stocks only: ``sh.6`` / ``sz.0`` / ``sz.3`` / ``bj.``
prefixes; indices and funds are filtered out). It is refreshed in full at the
end of every successful ``universe_sync`` (weekly), never deleted: a ticker
that disappears from the answer is marked ``delisted`` and keeps its name so
old research / watchlist rows still resolve.

* ``ticker``          - canonical ``600519.SH``;
* ``name``            - baostock ``code_name`` (``*ST`` / ``ST`` prefixes kept);
* ``pinyin_initials`` - lower-case first letters of the name with the ``*``
                        stripped (``贵州茅台`` -> ``gzmt``, ``*ST华仪`` -> ``sthy``);
* ``status``          - ``listed`` (tradeStatus 1) / ``suspended`` (0) / ``delisted``;
* ``synced_on``       - the trading day the directory answer describes;
* ``provider`` / ``captured_at`` - PIT provenance of the last write.

``GET /stocks/search`` reads it; the health page reports ``synced_on``.
"""

from datetime import date, datetime

from sqlalchemy import Date, DateTime, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from investment_agent.storage.models.base import Base

DIRECTORY_STATUSES = frozenset({"listed", "suspended", "delisted"})
STATUS_LISTED = "listed"
STATUS_SUSPENDED = "suspended"
STATUS_DELISTED = "delisted"


class StockDirectoryEntry(Base):
    __tablename__ = "stock_directory"
    __table_args__ = (Index("ix_stock_directory_pinyin", "pinyin_initials"),)

    ticker: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    pinyin_initials: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    synced_on: Mapped[date] = mapped_column(Date, nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
