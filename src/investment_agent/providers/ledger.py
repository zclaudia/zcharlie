"""Best-effort hooks that write the ``provider_call`` ledger (A2).

Mirrors :func:`investment_agent.verification.budget.ledger_hook`: a client
calls ``on_call`` after every logical call (``ok`` / ``transient`` /
``terminal`` / ``skipped_open``); the hook writes one row in its own session
and only logs when the database is unavailable - bookkeeping never breaks a
collection. :func:`circuit_ledger_hook` records the circuit breaker's
``opened`` transitions (``endpoint = "circuit"``, ``outcome = skipped_open``,
``duration_ms`` = open window) so the API process, which has no breaker in
memory, can still show "熔断中 until ...".

``run_id``: the snapshot handlers set :data:`CURRENT_RUN_ID` for the duration
of a job (a :class:`contextvars.ContextVar`, so concurrent jobs in one worker
never mix); a hook built without an explicit ``run_id`` reads it at record
time.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

import structlog

from investment_agent.providers.circuit import CircuitEvent
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.provider_call import CIRCUIT_ENDPOINT
from investment_agent.storage.repositories.provider_call import ProviderCallRepository

log = structlog.get_logger(__name__)

CURRENT_RUN_ID: ContextVar[int | None] = ContextVar("provider_call_run_id", default=None)

CallOutcome = Literal["ok", "transient", "terminal", "skipped_open"]


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class ProviderCallEvent:
    """What a client reports after one logical call (retries included)."""

    provider: str
    endpoint: str
    outcome: CallOutcome
    duration_ms: int | None = None
    ticker_count: int = 0


ProviderCallHook = Callable[[ProviderCallEvent], Awaitable[None]]


def provider_ledger_hook(
    *,
    source: str,
    run_id: int | None = None,
    clock: Callable[[], datetime] = utc_now,
) -> ProviderCallHook:
    """``on_call`` hook for the provider clients (best effort).

    ``source`` (``snapshot`` / ``cli`` / ``research``) only labels the log
    line - the table attributes rows by ``run_id``.
    """

    async def hook(event: ProviderCallEvent) -> None:
        try:
            async with session_scope() as session:
                await ProviderCallRepository(session).record(
                    at=clock(),
                    provider=event.provider,
                    endpoint=event.endpoint,
                    outcome=event.outcome,
                    duration_ms=event.duration_ms,
                    ticker_count=event.ticker_count,
                    run_id=run_id if run_id is not None else CURRENT_RUN_ID.get(),
                )
        except Exception as exc:  # never let bookkeeping break a collection
            log.warning(
                "provider.ledger.record_failed",
                source=source,
                provider=event.provider,
                endpoint=event.endpoint,
                outcome=event.outcome,
                error=str(exc),
            )

    return hook


class CircuitLedgerHook:
    """``CircuitBreaker(on_change=...)`` hook: one ``skipped_open`` row per open.

    The breaker calls it synchronously from the event-loop side, so the row
    is written by a task scheduled on the running loop (kept referenced until
    done). Without a running loop the event is only logged.
    """

    def __init__(
        self,
        *,
        source: str = "circuit",
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._source = source
        self._clock = clock
        self._pending: set[asyncio.Task[None]] = set()

    def __call__(self, event: CircuitEvent) -> None:
        if event.transition != "opened":
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            log.warning(
                "provider.ledger.circuit_no_loop",
                provider=event.status.provider,
                endpoint=event.status.endpoint,
            )
            return
        task = loop.create_task(self._write(event))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def drain(self) -> None:
        """Wait for scheduled writes (tests, graceful shutdown)."""

        if self._pending:
            await asyncio.gather(*list(self._pending), return_exceptions=True)

    async def _write(self, event: CircuitEvent) -> None:
        status = event.status
        window_ms: int | None = None
        if status.open_until is not None:
            window_ms = max(int((status.open_until - event.at).total_seconds() * 1000), 0)
        try:
            async with session_scope() as session:
                await ProviderCallRepository(session).record(
                    at=self._clock(),
                    provider=status.provider,
                    endpoint=CIRCUIT_ENDPOINT,
                    outcome="skipped_open",
                    duration_ms=window_ms,
                    ticker_count=0,
                    run_id=CURRENT_RUN_ID.get(),
                )
        except Exception as exc:
            log.warning(
                "provider.ledger.circuit_record_failed",
                source=self._source,
                provider=status.provider,
                endpoint=status.endpoint,
                error=str(exc),
            )


def circuit_ledger_hook(
    *, source: str = "circuit", clock: Callable[[], datetime] = utc_now
) -> CircuitLedgerHook:
    return CircuitLedgerHook(source=source, clock=clock)


__all__ = [
    "CURRENT_RUN_ID",
    "CallOutcome",
    "CircuitLedgerHook",
    "ProviderCallEvent",
    "ProviderCallHook",
    "circuit_ledger_hook",
    "provider_ledger_hook",
]
