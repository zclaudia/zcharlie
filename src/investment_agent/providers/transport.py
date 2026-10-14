"""Provider-call transport pipeline shared by the free-source clients (D3).

Five clients (``akshare_eastmoney``, ``eastmoney_annual``,
``baostock_reference``, ``tencent_quote``, ``akshare_shenwan``) each
hand-rolled the same logical-call pipeline around their own transport::

    circuit check -> [ throttle -> attempt -> classify ]... -> circuit record
                  -> exactly one ledger event -> backoff between attempts

This module owns that order once. It deliberately does **not** own:

* the **execution model** - the blocking akshare/baostock backends keep their
  ``asyncio.to_thread`` + ``asyncio.wait_for`` attempt callables (and the
  akshare client its per-client ``asyncio.Lock``), Tencent keeps native-async
  httpx. The pipeline only awaits the caller's attempt callable;
* the **error types and classification** - a client whose original retry loop
  caught a narrower class than :class:`ProviderTransientError` passes its own
  gate via :func:`classify_errors`; ``eastmoney_annual`` classifies **HTTP
  statuses** and raises its typed errors from the attempt (429 ``Retry-After``
  is handed back through :attr:`Attempt.retry_after_seconds`);
* the **client-specific side channels**: baostock's process-level session/IO
  locks and re-login, the annual collector's ``request_log``
  (``AnnualRequestRecord``) and response cache, every client's circuit key
  (``PROVIDER`` / endpoint) and public counters (``calls`` / ``requests``).

Behaviours shared here are byte-for-byte the ones the clients had:

* :meth:`Throttle.wait` is the ``_throttle`` body ``AkshareEastmoneyClient``
  and ``EastmoneyAnnualCollector`` carried twice; :meth:`Throttle.mark` is the
  extra ``finally: self._last_call = ...`` only the akshare client had
  (:attr:`ProviderCallPipeline.mark_after_attempt`);
* the circuit is checked **before the first attempt** and told the outcome
  **after the last one**; a call skipped because the circuit is open records
  no failure (``skipped_open``) and resets nothing;
* every logical call reports **exactly one** ledger event, in a ``finally``,
  with ``duration_ms=None`` for ``skipped_open``.

The ordering above is pinned by ``tests/unit/test_provider_call_ledger.py``
(one event per logical call, rollup) and ``tests/unit/test_provider_circuit.py``
(client-level retry/circuit counting).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from investment_agent.providers.circuit import CircuitBreaker
from investment_agent.providers.errors import ProviderTransientError, ProviderUnavailableError
from investment_agent.providers.ledger import CallOutcome, ProviderCallEvent, ProviderCallHook

FailureKind = Literal["transient", "terminal"]

Classify = Callable[[BaseException], FailureKind]
"""Retry gate: whether a failed attempt is worth another try."""


def classify_provider_error(exc: BaseException) -> FailureKind:
    """Default gate: retry typed transient errors, stop on everything else.

    Matches the ``except ProviderTransientError`` loops of the Tencent client
    (and the outcome classification all five clients apply in ``except
    BaseException``).
    """

    return "transient" if isinstance(exc, ProviderTransientError) else "terminal"


def classify_errors(*errors: type[BaseException]) -> Classify:
    """Gate mirroring a client's original ``except`` list (its own classes only).

    ``akshare_eastmoney``/``eastmoney_annual``/``baostock_reference``/
    ``akshare_shenwan`` retried a single provider-specific class, not every
    ``ProviderTransientError``: passing that class here keeps the retry
    decision exactly as narrow as it was.
    """

    return lambda exc: "transient" if isinstance(exc, errors) else "terminal"


@dataclass(frozen=True)
class RetryPolicy:
    """Retry budget of one logical call: ``max_attempts`` attempts, linear backoff.

    ``backoff_seconds * attempt`` (1-based) is what every original retry loop
    slept; ``max_retries=0`` maps to ``max_attempts=1``.
    """

    max_attempts: int = 1
    backoff_seconds: float = 0.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")

    def backoff(self, attempt: int) -> float:
        return self.backoff_seconds * attempt


class Throttle:
    """Minimum spacing between attempts of one client (``min_interval_seconds``).

    The pace is measured from the *start* of the previous attempt
    (:meth:`wait` stores "called now"); a client may additionally re-stamp it
    when an attempt finishes (:meth:`mark`, the akshare Eastmoney client's
    ``finally`` clause).
    """

    def __init__(
        self,
        *,
        min_interval_seconds: float,
        sleep: Callable[[float], Awaitable[None]],
        monotonic: Callable[[], float],
    ) -> None:
        if min_interval_seconds < 0:
            raise ValueError("min_interval_seconds must be >= 0")
        self.min_interval_seconds = min_interval_seconds
        self._sleep = sleep
        self._monotonic = monotonic
        self._last_call: float | None = None

    async def wait(self) -> None:
        """Sleep out the rest of the interval, then remember "called now".

        The body of the ``_throttle`` both eastmoney clients wrote separately
        (byte-identical); it must not gain a branch.
        """

        if self._last_call is not None and self.min_interval_seconds > 0:
            wait = self._last_call + self.min_interval_seconds - self._monotonic()
            if wait > 0:
                await self._sleep(wait)
        self.mark()

    def mark(self) -> None:
        """Re-stamp the last call time (an attempt just finished)."""

        self._last_call = self._monotonic()


@dataclass
class Attempt:
    """One attempt of a logical call, as seen by the caller's attempt callable.

    ``number`` is 1-based; ``last`` says the retry budget is spent, so a
    transient failure will be re-raised after this attempt. An attempt may set
    :attr:`retry_after_seconds` before failing to ask for that wait instead of
    the policy's backoff (the annual collector's HTTP 429 ``Retry-After``).
    """

    number: int
    last: bool
    retry_after_seconds: float | None = None


@dataclass(frozen=True)
class Reused[T]:
    """A pre-flight result that skips the provider call (see ``before`` in :meth:`run`).

    The wrapper keeps ``None`` a legitimate value: the hook returns ``None``
    when there is nothing to reuse, ``Reused(value)`` when the logical call is
    answered without touching the provider (the annual collector's saved
    response).
    """

    value: T



class ProviderCallPipeline:
    """Circuit / retry / ledger pipeline of one endpoint (one instance per client)."""

    def __init__(
        self,
        *,
        provider: str,
        retry: RetryPolicy | None = None,
        circuit: CircuitBreaker | None = None,
        on_call: ProviderCallHook | None = None,
        classify: Classify = classify_provider_error,
        throttle: Throttle | None = None,
        mark_after_attempt: bool = False,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._provider = provider
        self._retry = retry if retry is not None else RetryPolicy()
        self._circuit = circuit
        self._on_call = on_call
        self._classify = classify
        self._throttle = throttle
        self._mark_after_attempt = mark_after_attempt
        self._monotonic = monotonic
        self._sleep = sleep

    async def run[T](
        self,
        endpoint: str,
        attempt: Callable[[Attempt], Awaitable[T]],
        *,
        before: Callable[[], Reused[T] | None] | None = None,
        started: float | None = None,
        ticker_count: int = 0,
        on_retry: Callable[[Attempt, BaseException], None] | None = None,
        after_backoff: Callable[[Attempt, BaseException], Awaitable[None]] | None = None,
    ) -> T:
        """Run one logical call and return ``attempt``'s last value.

        The circuit is consulted once *before the first attempt*; when it is
        open the call raises :class:`ProviderUnavailableError`, no attempt
        runs, no failure is recorded and the ledger event is ``skipped_open``.
        The final outcome is classified once and, for anything except a
        skipped call, handed to the breaker after the last attempt. A
        transient failure retries with backoff while attempts remain; a
        terminal one stops the loop at once.

        ``before`` runs first, inside the same outcome/ledger region, and a
        :class:`Reused` result skips both the circuit and the attempts (a
        reused answer never touches the provider; its one ledger event keeps
        the label the annual collector has always given it - ``terminal``,
        because the reuse path returns before any outcome is classified).

        ``on_retry`` (sync: the original ``log.warning``) runs before the
        backoff sleep, ``after_backoff`` (e.g. baostock's re-login) after it -
        the order every client's loop had. ``started`` lets a caller that
        began timing before this call keep its ledger duration.
        """

        started_at = self._monotonic() if started is None else started
        outcome: CallOutcome = "terminal"
        try:
            if before is not None:
                reused = before()
                if reused is not None:
                    # Leaves the try via return, so the else-branch (outcome
                    # "ok", record_success) is skipped: only the finally runs.
                    return reused.value
            if self._circuit is not None:
                self._circuit.check(self._provider, endpoint)
            result = await self._run_attempts(
                attempt, on_retry=on_retry, after_backoff=after_backoff
            )
        except ProviderUnavailableError:
            outcome = "skipped_open"
            raise
        except BaseException as exc:
            # The label every client used; the breaker applies its own
            # is_transient_error() on top of it.
            outcome = "transient" if isinstance(exc, ProviderTransientError) else "terminal"
            if self._circuit is not None:
                self._circuit.record_failure(self._provider, endpoint, exc)
            raise
        else:
            outcome = "ok"
            if self._circuit is not None:
                self._circuit.record_success(self._provider, endpoint)
            return result
        finally:
            await self._report(endpoint, outcome, started_at, ticker_count)

    async def _report(
        self, endpoint: str, outcome: CallOutcome, started: float, ticker_count: int
    ) -> None:
        """The one ledger event of a logical call (``skipped_open``: no duration)."""

        if self._on_call is None:
            return
        duration_ms = (
            None if outcome == "skipped_open" else max(int((self._monotonic() - started) * 1000), 0)
        )
        await self._on_call(
            ProviderCallEvent(self._provider, endpoint, outcome, duration_ms, ticker_count)
        )

    async def _run_attempts[T](
        self,
        attempt: Callable[[Attempt], Awaitable[T]],
        *,
        on_retry: Callable[[Attempt, BaseException], None] | None,
        after_backoff: Callable[[Attempt, BaseException], Awaitable[None]] | None,
    ) -> T:
        number = 0
        while True:
            number += 1
            context = Attempt(number=number, last=number >= self._retry.max_attempts)
            if self._throttle is not None:
                await self._throttle.wait()
            try:
                result = await attempt(context)
            except BaseException as exc:
                if self._mark_after_attempt and self._throttle is not None:
                    self._throttle.mark()
                if self._classify(exc) == "terminal" or context.last:
                    raise
                if on_retry is not None:
                    on_retry(context, exc)
                wait = context.retry_after_seconds
                await self._sleep(self._retry.backoff(number) if wait is None else wait)
                if after_backoff is not None:
                    await after_backoff(context, exc)
            else:
                if self._mark_after_attempt and self._throttle is not None:
                    self._throttle.mark()
                return result


__all__ = [
    "Attempt",
    "Classify",
    "FailureKind",
    "ProviderCallPipeline",
    "RetryPolicy",
    "Reused",
    "Throttle",
    "classify_errors",
    "classify_provider_error",
]
