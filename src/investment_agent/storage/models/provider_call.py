"""Generic upstream call ledger (mrd-intake A2, migration 022).

``provider_call`` - one row per logical call of a free data provider
(``baostock`` / ``akshare`` / ``tencent`` ...; the iFinD budget keeps its own
``ifind_call_ledger``). Outcomes:

* ``ok``           - the call answered;
* ``transient``    - failed after the client's own retries with a transient
                     error (network / timeout / 5xx / 429);
* ``terminal``     - failed with a terminal error (schema drift, 4xx, data);
* ``skipped_open`` - **not** called: the circuit breaker was open. Rows with
                     ``endpoint = "circuit"`` are the breaker's own state
                     changes (``duration_ms`` = length of the open window), so
                     a process without the worker's in-memory breaker (the API)
                     can still show "熔断中 until ...".

``/health/data`` aggregates the last 24 hours per provider; ``ia admin
prune-provider-calls`` keeps 90 days.
"""

from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from investment_agent.storage.models.base import Base

PROVIDER_CALL_OUTCOMES = frozenset({"ok", "transient", "terminal", "skipped_open"})
FAILED_OUTCOMES = frozenset({"transient", "terminal"})
CIRCUIT_ENDPOINT = "circuit"


class ProviderCall(Base):
    __tablename__ = "provider_call"
    __table_args__ = (Index("ix_provider_call_provider_at", "provider", "at"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    endpoint: Mapped[str] = mapped_column(String(64), nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ticker_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
