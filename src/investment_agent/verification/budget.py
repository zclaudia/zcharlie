"""iFinD budget ledger (P1-A13): count every ifind-proxy call, cap a rolling window.

Every proxy call is one ``ifind_call_ledger`` row (time, endpoint, ticker
count, outcome ``ok`` / ``error`` / ``quota``, source):

* verification jobs call through :class:`BudgetedProxyClient`, which checks
  the budget *before* each call and records the call afterwards;
* the other iFinD paths - the watchlist A3 collectors (``source="snapshot"``)
  and the single-stock research chain (``source="research"``) - are counted
  through :func:`ledger_hook` (``IfindProxyClient(on_call=...)``) but never
  blocked; their calls still shrink the verification budget.

Budget algorithm. The window is the half-open interval ``(now - window, now]``
(default 5 hours, ``IFIND_BUDGET_WINDOW_HOURS``); ``used`` counts every
ledger row inside it (all sources and outcomes - a failed call may still
have reached the gateway), ``remaining = cap - used`` (``IFIND_BUDGET_PER_5H``,
default 150). ``n`` more calls fit once the ``used - (cap - n)`` oldest calls
of the window have expired: :meth:`BudgetStatus.available_at` returns the
moment the k-th oldest call leaves the window (its time + window).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.domain.time import as_utc as as_utc
from investment_agent.providers.errors import ProviderError
from investment_agent.research.ifind_chain import IfindProxyResult, ProxyCallHook
from investment_agent.research.retry_policy import is_quota_exhausted
from investment_agent.snapshots.collect import ProxyClient
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.verification import LEDGER_OUTCOMES, IfindCallLedger
from investment_agent.storage.repositories.pit import to_utc

log = structlog.get_logger(__name__)

DEFAULT_BUDGET = 150
DEFAULT_WINDOW = timedelta(hours=5)


def utc_now() -> datetime:
    return datetime.now(UTC)


def ticker_count(payload: dict[str, Any]) -> int:
    tickers = [item for item in str(payload.get("ticker", "")).split(",") if item.strip()]
    return len(tickers)


async def record_call(
    session: AsyncSession,
    *,
    at: datetime,
    endpoint: str,
    tickers: int,
    outcome: str,
    source: str,
    run_id: int | None = None,
    verification_request_id: int | None = None,
) -> IfindCallLedger:
    if outcome not in LEDGER_OUTCOMES:
        raise ValueError(f"unknown ledger outcome {outcome!r}")
    row = IfindCallLedger(
        at=to_utc(at),
        endpoint=endpoint[:128],
        ticker_count=tickers,
        outcome=outcome,
        source=source,
        run_id=run_id,
        verification_request_id=verification_request_id,
    )
    session.add(row)
    await session.flush()
    return row


@dataclass(frozen=True)
class BudgetStatus:
    cap: int
    window: timedelta
    now: datetime
    call_times: tuple[datetime, ...]
    """UTC times of the calls inside the window, oldest first."""

    @property
    def used(self) -> int:
        return len(self.call_times)

    @property
    def remaining(self) -> int:
        return max(self.cap - self.used, 0)

    def available_at(self, needed: int = 1) -> datetime:
        """When ``needed`` calls fit (``now`` if they already do)."""

        needed = min(max(needed, 1), self.cap)
        if self.remaining >= needed:
            return self.now
        expire = self.used - (self.cap - needed)  # oldest calls that must leave the window
        return self.call_times[expire - 1] + self.window

    def describe(self) -> dict[str, object]:
        return {
            "cap": self.cap,
            "window_hours": self.window.total_seconds() / 3600,
            "used": self.used,
            "remaining": self.remaining,
            "next_free_at": self.available_at(1).isoformat() if self.remaining == 0 else None,
        }


async def budget_status(
    session: AsyncSession,
    *,
    now: datetime,
    cap: int = DEFAULT_BUDGET,
    window: timedelta = DEFAULT_WINDOW,
) -> BudgetStatus:
    now = as_utc(now)
    stmt = (
        select(IfindCallLedger.at)
        .where(IfindCallLedger.at > now - window)
        .order_by(IfindCallLedger.at, IfindCallLedger.id)
    )
    times = tuple(
        stamp
        for stamp in (as_utc(value) for value in (await session.execute(stmt)).scalars())
        if stamp <= now
    )
    return BudgetStatus(cap=cap, window=window, now=now, call_times=times)


def ledger_hook(
    *,
    source: str,
    run_id: int | None = None,
    clock: Callable[[], datetime] = utc_now,
) -> ProxyCallHook:
    """``IfindProxyClient(on_call=...)`` hook that counts calls (best effort)."""

    async def hook(path: str, payload: dict[str, Any], outcome: str) -> None:
        try:
            async with session_scope() as session:
                await record_call(
                    session,
                    at=clock(),
                    endpoint=path,
                    tickers=ticker_count(payload),
                    outcome=outcome if outcome in LEDGER_OUTCOMES else "error",
                    source=source,
                    run_id=run_id,
                )
        except Exception as exc:  # never let bookkeeping break a collection
            log.warning("ifind.ledger.record_failed", source=source, path=path, error=str(exc))

    return hook


class BudgetExhaustedError(ProviderError):
    """The rolling-window budget has no call left (terminal for the current unit)."""

    def __init__(self, status: BudgetStatus, needed: int = 1) -> None:
        self.status = status
        self.available_at = status.available_at(needed)
        super().__init__(
            f"iFinD budget exhausted: {status.used}/{status.cap} calls in the last "
            f"{status.window}; next call fits at {self.available_at.isoformat()}",
            provider="ifind",
            error_code="budget_exhausted",
        )


class BudgetedProxyClient:
    """Proxy client wrapper for verification jobs: check budget, call, record.

    ``request_id`` attributes the next calls to one verification request in the
    ledger (set by the job before each unit).
    """

    def __init__(
        self,
        client: ProxyClient,
        *,
        cap: int = DEFAULT_BUDGET,
        window: timedelta = DEFAULT_WINDOW,
        clock: Callable[[], datetime] = utc_now,
        source: str = "verification",
        run_id: int | None = None,
    ) -> None:
        self._client = client
        self.cap = cap
        self.window = window
        self._clock = clock
        self.source = source
        self.run_id = run_id
        self.request_id: int | None = None
        self.calls = 0

    async def status(self) -> BudgetStatus:
        async with session_scope() as session:
            return await budget_status(
                session, now=self._clock(), cap=self.cap, window=self.window
            )

    async def post_rows(self, path: str, payload: dict[str, Any]) -> IfindProxyResult:
        status = await self.status()
        if status.remaining < 1:
            raise BudgetExhaustedError(status)
        outcome = "error"
        try:
            result = await self._client.post_rows(path, payload)
            outcome = "ok"
            return result
        except Exception as exc:
            outcome = "quota" if is_quota_exhausted(exc) else "error"
            raise
        finally:
            self.calls += 1
            async with session_scope() as session:
                await record_call(
                    session,
                    at=self._clock(),
                    endpoint=path,
                    tickers=ticker_count(payload),
                    outcome=outcome,
                    source=self.source,
                    run_id=self.run_id,
                    verification_request_id=self.request_id,
                )
