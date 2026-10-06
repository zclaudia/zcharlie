"""In-memory authentication failure throttle.

A deliberately small, process-local limiter for a single-user deployment:
after ``max_failures`` failed credential presentations from one client key
(normally the client IP) within ``window_seconds``, further attempts are
refused with HTTP 429 until the oldest failure ages out.

Tradeoffs: state lives in the API process only (restarts reset it, multiple
workers each keep their own counters), and behind a reverse proxy every
request shares the proxy's IP unless the proxy is configured to pass the real
client address (e.g. uvicorn ``--proxy-headers``).
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from collections.abc import Callable

DEFAULT_MAX_FAILURES = 5
DEFAULT_WINDOW_SECONDS = 300.0


class AuthFailureThrottle:
    def __init__(
        self,
        *,
        max_failures: int = DEFAULT_MAX_FAILURES,
        window_seconds: float = DEFAULT_WINDOW_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_failures <= 0 or window_seconds <= 0:
            raise ValueError("max_failures and window_seconds must be positive")
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self._clock = clock
        self._failures: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float) -> deque[float] | None:
        failures = self._failures.get(key)
        if failures is None:
            return None
        cutoff = now - self.window_seconds
        while failures and failures[0] <= cutoff:
            failures.popleft()
        if not failures:
            del self._failures[key]
            return None
        return failures

    def retry_after(self, key: str) -> int | None:
        """Seconds until ``key`` may try again, or None when not blocked."""

        with self._lock:
            now = self._clock()
            failures = self._prune(key, now)
            if failures is None or len(failures) < self.max_failures:
                return None
            wait = failures[0] + self.window_seconds - now
            return max(1, math.ceil(wait))

    def record_failure(self, key: str) -> None:
        with self._lock:
            now = self._clock()
            self._prune(key, now)
            failures = self._failures.setdefault(key, deque())
            failures.append(now)
            # Bound memory per key; only the newest max_failures matter.
            while len(failures) > self.max_failures:
                failures.popleft()
            # Bound the number of tracked keys against address spraying.
            if len(self._failures) > 10_000:
                for stale in list(self._failures)[:1_000]:
                    del self._failures[stale]

    def reset(self, key: str | None = None) -> None:
        with self._lock:
            if key is None:
                self._failures.clear()
            else:
                self._failures.pop(key, None)
