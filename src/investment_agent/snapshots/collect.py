"""Shared plumbing for batched iFinD collection: batching, retries, artifacts,
completeness accounting.
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

    Transient failures (network, timeouts, 429/5xx) are retried up to
    ``retries`` times with linear backoff; anything else is raised at once.
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


# Missing tickers fall into three buckets:
# * ``failed``  - a request or parse error; degrades the run to ``partial``;
# * ``no_data`` - the provider answered but had nothing (e.g. suspended stock
#   without a bar); counts against completeness but is not an error;
# * ``pending`` - financial report not yet disclosed; excluded from the
#   completeness denominator.
MissingKind = Literal["failed", "no_data", "pending"]


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

    def status(self) -> Literal["success", "partial", "failed"]:
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

    def record_failure(self, exc: BaseException) -> None:
        if not is_transient_error(exc):
            self.all_failures_transient = False

    @property
    def status(self) -> Literal["success", "partial", "failed"]:
        return self.completeness.status()

    def to_metadata(self) -> dict[str, object]:
        return {"completeness": self.completeness.to_metadata(), **self.metadata}

    def summary(self) -> str:
        c = self.completeness
        return (
            f"collected {len(c.collected)}/{len(c.expected)}; "
            f"failed {len(c.failed)}, no data {len(c.tickers('no_data'))}, "
            f"pending {len(c.pending)}"
        )

    def failure(self) -> ProviderError:
        """The error raised when nothing could be collected."""

        message = f"snapshot collection failed: {self.summary()}"
        if self.all_failures_transient:
            return ProviderTransientError(message, provider=IFIND_PROVIDER)
        return ProviderError(message, provider=IFIND_PROVIDER)


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
