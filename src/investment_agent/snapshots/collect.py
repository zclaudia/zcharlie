"""Shared plumbing for snapshot collection: iFinD batching and retries, raw
response artifacts, completeness accounting (iFinD and baostock collectors).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import Awaitable, Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.providers.errors import ProviderError, ProviderTransientError
from investment_agent.research.ifind_chain import IfindProxyResult
from investment_agent.research.retry_policy import is_transient_error
from investment_agent.snapshots.throttle import AsyncRateLimiter
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository, RunLease

log = structlog.get_logger(__name__)

# Every iFinD endpoint accepts at most three tickers per call; responses label
# each row with ``thscode`` (verified 2026-10-06 for /stock/history,
# /stock/info, /financial/statements and /financial/index).
MAX_TICKERS_PER_CALL = 3
TICKER_COLUMN = "thscode"
IFIND_PROVIDER = "ifind"

_SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]+")


class ProxyClient(Protocol):
    async def post_rows(self, path: str, payload: dict[str, Any]) -> IfindProxyResult: ...


def chunked[T](items: Sequence[T], size: int = MAX_TICKERS_PER_CALL) -> Iterator[list[T]]:
    if size < 1:
        raise ValueError("size must be >= 1")
    for index in range(0, len(items), size):
        yield list(items[index : index + size])


def rows_by_ticker(rows: Sequence[dict[str, str]]) -> dict[str, list[dict[str, str]]]:
    grouped: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        ticker = row.get(TICKER_COLUMN, "").strip().upper()
        if ticker:
            grouped.setdefault(ticker, []).append(row)
    return grouped


class ProxyBatchCaller:
    """Throttled proxy calls with a small per-batch retry budget.

    Transient failures (network, timeouts, 5xx) are retried up to ``retries``
    times with linear backoff; anything else - including iFinD quota
    exhaustion - is raised at once.
    The caller decides what a failed batch means; one batch never aborts the
    whole job.
    """

    def __init__(
        self,
        client: ProxyClient,
        limiter: AsyncRateLimiter,
        *,
        retries: int = 2,
        backoff_seconds: float = 2.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._client = client
        self._limiter = limiter
        self._retries = max(retries, 0)
        self._backoff_seconds = backoff_seconds
        self._sleep = sleep
        self.calls = 0
        self.failed_calls = 0

    async def call(self, path: str, payload: dict[str, Any]) -> IfindProxyResult:
        attempt = 0
        while True:
            attempt += 1
            await self._limiter.acquire()
            self.calls += 1
            try:
                return await self._client.post_rows(path, payload)
            except Exception as exc:
                self.failed_calls += 1
                if not is_transient_error(exc) or attempt > self._retries:
                    raise
                log.warning(
                    "snapshot.proxy.retry",
                    path=path,
                    tickers=payload.get("ticker"),
                    attempt=attempt,
                    error=f"{type(exc).__name__}: {exc}",
                )
                await self._sleep(self._backoff_seconds * attempt)


class ArtifactWriter:
    """Writes raw provider responses under ``<data_dir>/snapshot-runs/<run>/attempt-<n>/``.

    Returned references are relative to ``data_dir`` and stored in ``raw_ref``.
    Only request bodies and response bodies are written; the proxy API key is
    an HTTP header and never reaches these files.
    """

    def __init__(self, data_dir: Path, *, run_id: int, attempt: int) -> None:
        self._data_dir = Path(data_dir)
        self.relative_dir = Path("snapshot-runs") / str(run_id) / f"attempt-{attempt}"

    def write(self, name: str, payload: Any) -> str:
        safe = _SAFE_NAME.sub("_", name).strip("_") or "response"
        relative = self.relative_dir / f"{safe}.json"
        target = self._data_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, target)
        return relative.as_posix()


def error_reason(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}"
    return text if len(text) <= 300 else text[:297] + "..."


# Missing tickers fall into four buckets:
# * ``failed``  - a request or parse error; degrades the run to ``partial``;
# * ``no_data`` - the provider answered but had nothing (e.g. suspended stock
#   without a bar); counts against completeness but is not an error;
# * ``pending`` - financial report not yet disclosed; excluded from the
#   completeness denominator;
# * ``quota``   - not attempted (or interrupted) because the iFinD quota ran
#   out; the run finishes ``partial`` and the next run resumes them.
# * ``stopped`` - not attempted because the provider failed repeatedly
#   (AkShare: 3 consecutive failures); the run finishes ``partial`` with
#   ``stop_reason="provider_unavailable"`` and the next run resumes them.
MissingKind = Literal["failed", "no_data", "pending", "quota", "stopped"]
QUOTA_STOP_REASON = "quota"
PROVIDER_STOP_REASON = "provider_unavailable"


@dataclass
class Completeness:
    expected: list[str]
    collected: set[str] = field(default_factory=set)
    missing: dict[str, tuple[MissingKind, str]] = field(default_factory=dict)

    def mark_collected(self, ticker: str) -> None:
        self.missing.pop(ticker, None)
        self.collected.add(ticker)

    def mark_missing(self, ticker: str, kind: MissingKind, reason: str) -> None:
        if ticker in self.collected:
            return
        # A hard failure wins over softer explanations for the same ticker.
        current = self.missing.get(ticker)
        if current is not None and current[0] == "failed":
            if kind == "failed" and reason not in current[1]:
                self.missing[ticker] = ("failed", f"{current[1]}; {reason}")
            return
        self.missing[ticker] = (kind, reason)

    def tickers(self, kind: MissingKind) -> list[str]:
        return sorted(ticker for ticker, (k, _) in self.missing.items() if k == kind)

    @property
    def failed(self) -> list[str]:
        return self.tickers("failed")

    @property
    def pending(self) -> list[str]:
        return self.tickers("pending")

    @property
    def ratio(self) -> float:
        denominator = len(self.expected) - len(self.pending)
        if denominator <= 0:
            return 1.0
        return round(len(self.collected) / denominator, 4)

    @property
    def quota(self) -> list[str]:
        return self.tickers("quota")

    @property
    def stopped(self) -> list[str]:
        return self.tickers("stopped")

    def status(self) -> Literal["success", "partial", "failed"]:
        if self.quota or self.stopped:
            # Quota exhaustion / a provider stop keeps what was stored and
            # never fails the run.
            return "partial"
        if self.failed and not self.collected:
            return "failed"
        return "partial" if self.failed else "success"

    def to_metadata(self) -> dict[str, object]:
        return {
            "expected": len(self.expected),
            "collected": len(self.collected),
            "missing_count": len(self.missing),
            "failed_count": len(self.failed),
            "pending_count": len(self.pending),
            "quota_count": len(self.quota),
            "stopped_count": len(self.stopped),
            "ratio": self.ratio,
            "missing": {
                ticker: {"kind": kind, "reason": reason}
                for ticker, (kind, reason) in sorted(self.missing.items())
            },
        }


@dataclass
class CollectionReport:
    """Outcome of one collector run, persisted as run metadata."""

    completeness: Completeness
    metadata: dict[str, object] = field(default_factory=dict)
    all_failures_transient: bool = True
    provider: str = IFIND_PROVIDER
    stop_reason: str | None = None
    stop_error: str | None = None

    def record_failure(self, exc: BaseException) -> None:
        if not is_transient_error(exc):
            self.all_failures_transient = False

    def stop_for_quota(self, exc: BaseException, tickers: Sequence[str]) -> None:
        """Record quota exhaustion and tag every not-yet-collected ticker ``quota``."""

        self.stop_reason = QUOTA_STOP_REASON
        self.stop_error = error_reason(exc)
        for ticker in tickers:
            self.completeness.mark_missing(
                ticker, "quota", f"not collected: iFinD quota exhausted ({self.stop_error})"
            )
        log.warning(
            "snapshot.quota_exhausted",
            provider=self.provider,
            remaining=len(tickers),
            error=self.stop_error,
        )

    def stop_for_provider(self, exc: BaseException, tickers: Sequence[str], *, failures: int) -> None:
        """Stop after ``failures`` consecutive provider failures; tag the rest ``stopped``."""

        self.stop_reason = PROVIDER_STOP_REASON
        self.stop_error = error_reason(exc)
        for ticker in tickers:
            self.completeness.mark_missing(
                ticker,
                "stopped",
                f"not collected: {self.provider} unavailable after {failures} consecutive "
                f"failures ({self.stop_error})",
            )
        log.warning(
            "snapshot.provider_unavailable",
            provider=self.provider,
            remaining=len(tickers),
            error=self.stop_error,
        )

    def stop_for_quota_ticker(self, ticker: str) -> None:
        """Tag one more ticker as not collected because of the recorded quota stop."""

        self.completeness.mark_missing(
            ticker, "quota", f"not collected: iFinD quota exhausted ({self.stop_error})"
        )

    @property
    def status(self) -> Literal["success", "partial", "failed"]:
        return self.completeness.status()

    def to_metadata(self) -> dict[str, object]:
        metadata: dict[str, object] = {
            "completeness": self.completeness.to_metadata(),
            **self.metadata,
        }
        if self.stop_reason is not None:
            metadata["stop_reason"] = self.stop_reason
            metadata["stop_error"] = self.stop_error
        return metadata

    def summary(self) -> str:
        c = self.completeness
        text = (
            f"collected {len(c.collected)}/{len(c.expected)}; "
            f"failed {len(c.failed)}, no data {len(c.tickers('no_data'))}, "
            f"pending {len(c.pending)}"
        )
        if self.stop_reason == QUOTA_STOP_REASON:
            text += f"; stopped: iFinD quota exhausted, {len(c.quota)} not collected"
        if self.stop_reason == PROVIDER_STOP_REASON:
            text += f"; stopped: {self.provider} unavailable, {len(c.stopped)} not collected"
        return text

    def failure(self) -> ProviderError:
        """The error raised when nothing could be collected."""

        message = f"snapshot collection failed: {self.summary()}"
        if self.all_failures_transient:
            return ProviderTransientError(message, provider=self.provider)
        return ProviderError(message, provider=self.provider)


class SnapshotPreconditionError(ProviderError):
    """The job cannot run with the data currently stored (terminal)."""

    def __init__(self, message: str) -> None:
        super().__init__(message, provider="local")


async def fence_writes(session: AsyncSession, lease: RunLease | None) -> None:
    """Check (and refresh) the claim inside the transaction that writes snapshots.

    A worker whose lease was lost raises :class:`LeaseLostError` here, so the
    surrounding transaction rolls back and nothing is written.
    """

    if lease is not None:
        await PipelineRunRepository(session).heartbeat(lease.run_id, lease=lease)
