"""Async token-bucket rate limiter for iFinD proxy calls."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable


class AsyncRateLimiter:
    """Allow at most ``rate_per_minute`` acquisitions per minute.

    A token bucket with capacity ``burst`` refilled at ``rate_per_minute / 60``
    tokens per second. ``acquire`` waits until a token is available; callers
    are served in FIFO order. One limiter instance is shared by every
    collector in a worker process so concurrent jobs cannot exceed the budget.
    """

    def __init__(
        self,
        rate_per_minute: float,
        *,
        burst: int = 1,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if rate_per_minute <= 0:
            raise ValueError("rate_per_minute must be positive")
        if burst < 1:
            raise ValueError("burst must be >= 1")
        self.rate_per_minute = rate_per_minute
        self._interval = 60.0 / rate_per_minute
        self._capacity = float(burst)
        self._tokens = float(burst)
        self._clock = clock
        self._sleep = sleep
        self._updated = clock()
        self._lock: asyncio.Lock | None = None
        self.acquired = 0

    async def acquire(self) -> None:
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            while True:
                now = self._clock()
                elapsed = max(now - self._updated, 0.0)
                self._tokens = min(self._capacity, self._tokens + elapsed / self._interval)
                self._updated = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    self.acquired += 1
                    return
                await self._sleep((1.0 - self._tokens) * self._interval)
