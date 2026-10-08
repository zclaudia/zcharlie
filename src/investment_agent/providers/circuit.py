"""Cross-run circuit breaker with exponential backoff and a negative cache (A1).

One :class:`CircuitBreaker` lives in the worker process (decision D6: no
state table; every state change is reported through ``on_change`` so the A2
call ledger can record it). Keys are ``"<provider>:<endpoint>"``.

Semantics:

* :meth:`CircuitBreaker.check` raises
  :class:`~investment_agent.providers.errors.ProviderUnavailableError` while
  the endpoint (or the whole provider, ``provider:*``) is open
  (``now < open_until``): the upstream is **not** called - that is the
  negative cache. Once the window has passed the next call goes through
  (half-open); its outcome decides what happens next.
* :meth:`CircuitBreaker.open_now` trips the circuit: the collectors' own
  consecutive-failure breakers (``breadth.TransientBreaker``,
  ``eastmoney.FailureBreaker``) call it once a job has decided the provider
  is unavailable, regardless of the error class, so the next job of any kind
  fails fast with ``partial``. The n-th consecutive trip opens the circuit
  for ``schedule[min(n, len(schedule)) - 1]`` seconds: 5 s, 15 s, 45 s,
  2 min, then 5 min for every further one.
* :meth:`CircuitBreaker.record_failure` (called by the clients after a
  logical call failed for good) counts only transient failures
  (:func:`~investment_agent.research.retry_policy.is_transient_error`) and
  only escalates a circuit that is already tripped: the half-open probe of
  the next job failing again moves the backoff to the next step without
  waiting for that job's breaker. A transient failure on a closed circuit
  is left to the job's own breaker (one blip must not abort a 300-ticker
  run); terminal errors (schema drift, HTTP 4xx, lookahead data) never open
  the circuit.
* :meth:`CircuitBreaker.record_success` closes the endpoint and the
  provider-wide entry and resets the failure count.

The breaker is not thread-safe and does not need to be: every client calls
it from the event-loop side (the blocking provider call runs in
``asyncio.to_thread``, the bookkeeping happens after it returns).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Literal

import structlog

from investment_agent.providers.errors import ProviderUnavailableError
from investment_agent.research.retry_policy import is_transient_error

log = structlog.get_logger(__name__)

DEFAULT_SCHEDULE: tuple[float, ...] = (5, 15, 45, 120, 300)
"""Seconds the circuit stays open after the 1st, 2nd, ... consecutive failure."""

CIRCUIT_ENDPOINT = "circuit"
"""Ledger endpoint name under which state changes are recorded (A2)."""

ANY_ENDPOINT = "*"
"""Endpoint wildcard: an open ``provider:*`` entry blocks every endpoint of
the provider (used by the collectors' consecutive-failure breakers)."""

_MAX_ERROR_TEXT = 300

CircuitTransition = Literal["opened", "closed"]


def describe_error(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}"
    return text if len(text) <= _MAX_ERROR_TEXT else text[: _MAX_ERROR_TEXT - 3] + "..."


@dataclass(frozen=True)
class CircuitStatus:
    """Read-only view of one ``provider:endpoint`` entry (health page, tests)."""

    provider: str
    endpoint: str
    fail_count: int = 0
    open_until: datetime | None = None
    last_error: str | None = None
    last_failure_at: datetime | None = None

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.endpoint}"

    def is_open(self, now: datetime) -> bool:
        return self.open_until is not None and now < self.open_until


@dataclass(frozen=True)
class CircuitEvent:
    """One state change, handed to ``on_change`` (best effort, never awaited)."""

    transition: CircuitTransition
    status: CircuitStatus
    at: datetime


OnChange = Callable[[CircuitEvent], object]


class CircuitBreaker:
    """Per ``provider:endpoint`` failure counter with an open window."""

    def __init__(
        self,
        clock: Callable[[], datetime],
        *,
        schedule: tuple[float, ...] = DEFAULT_SCHEDULE,
        on_change: OnChange | None = None,
    ) -> None:
        if not schedule or any(step <= 0 for step in schedule):
            raise ValueError("schedule must be a non-empty tuple of positive seconds")
        self._clock = clock
        self._schedule = tuple(float(step) for step in schedule)
        self._on_change = on_change
        self._states: dict[str, CircuitStatus] = {}

    # -- queries ----------------------------------------------------------- #

    @property
    def schedule(self) -> tuple[float, ...]:
        return self._schedule

    def status(self, provider: str, endpoint: str) -> CircuitStatus:
        return self._states.get(f"{provider}:{endpoint}", CircuitStatus(provider, endpoint))

    def snapshot(self) -> Mapping[str, CircuitStatus]:
        """Every known entry keyed ``provider:endpoint`` (for the health page)."""

        return dict(self._states)

    def open_entries(self, now: datetime | None = None) -> list[CircuitStatus]:
        at = now if now is not None else self._now()
        return [status for status in self._states.values() if status.is_open(at)]

    # -- transitions ------------------------------------------------------- #

    def check(self, provider: str, endpoint: str) -> None:
        """Raise :class:`ProviderUnavailableError` while the endpoint is open."""

        now = self._now()
        status = self.status(provider, endpoint)
        if not status.is_open(now):
            status = self.status(provider, ANY_ENDPOINT)
            if endpoint == ANY_ENDPOINT or not status.is_open(now):
                return
        assert status.open_until is not None
        scope = endpoint if status.endpoint == endpoint else f"{endpoint} (whole provider)"
        raise ProviderUnavailableError(
            f"{provider} {scope} is unavailable until {status.open_until.isoformat()} "
            f"(circuit open after {status.fail_count} consecutive failures; "
            f"last error: {status.last_error or 'unknown'})",
            provider=provider,
            open_until=status.open_until,
            endpoint=status.endpoint,
            fail_count=status.fail_count,
        )

    def record_success(self, provider: str, endpoint: str) -> None:
        """Close ``provider:endpoint`` (and a provider-wide ``provider:*`` entry)."""

        for name in (endpoint, ANY_ENDPOINT) if endpoint != ANY_ENDPOINT else (endpoint,):
            status = self._states.pop(f"{provider}:{name}", None)
            if status is None:
                continue
            self._emit("closed", CircuitStatus(provider, name))

    def record_failure(self, provider: str, endpoint: str, exc: BaseException) -> bool:
        """Escalate a tripped circuit after a transient failure; ``True`` when it opened.

        A closed circuit stays closed (the job's own breaker decides when the
        provider is unavailable); terminal errors never count.
        """

        if not is_transient_error(exc):
            return False
        for name in (endpoint, ANY_ENDPOINT):
            if self.status(provider, name).fail_count:
                self._open(provider, name, exc)
                return True
        return False

    def open_now(self, provider: str, exc: BaseException, endpoint: str = ANY_ENDPOINT) -> None:
        """Open the circuit regardless of the error class (collector breakers).

        Without ``endpoint`` the whole provider (``provider:*``) opens, so the
        next job of any kind for that provider fails fast.
        """

        self._open(provider, endpoint, exc)

    # -- plumbing ---------------------------------------------------------- #

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None:
            raise ValueError("clock must return a timezone-aware datetime")
        return now

    def _open(self, provider: str, endpoint: str, exc: BaseException) -> None:
        key = f"{provider}:{endpoint}"
        previous = self.status(provider, endpoint)
        now = self._now()
        count = previous.fail_count + 1
        delay = self._schedule[min(count, len(self._schedule)) - 1]
        status = replace(
            previous,
            fail_count=count,
            open_until=now + timedelta(seconds=delay),
            last_error=describe_error(exc),
            last_failure_at=now,
        )
        self._states[key] = status
        log.warning(
            "provider.circuit_opened",
            provider=provider,
            endpoint=endpoint,
            fail_count=count,
            open_seconds=delay,
            error=status.last_error,
        )
        self._emit("opened", status)

    def _emit(self, transition: CircuitTransition, status: CircuitStatus) -> None:
        if self._on_change is None:
            return
        try:
            self._on_change(CircuitEvent(transition, status, self._now()))
        except Exception as exc:  # observation must never break a collection
            log.warning(
                "provider.circuit_hook_failed",
                provider=status.provider,
                endpoint=status.endpoint,
                error=str(exc),
            )


__all__ = [
    "ANY_ENDPOINT",
    "CIRCUIT_ENDPOINT",
    "DEFAULT_SCHEDULE",
    "CircuitBreaker",
    "CircuitEvent",
    "CircuitStatus",
    "describe_error",
]
