"""Async retry decorator + simple rate limiter."""

import asyncio
import functools
from collections.abc import Awaitable, Callable
from typing import ParamSpec, TypeVar

import structlog

P = ParamSpec("P")
R = TypeVar("R")

_log = structlog.get_logger(__name__)


def retry_async(
    *,
    attempts: int = 3,
    base_delay: float = 1.0,
    backoff: float = 2.0,
    retry_on: tuple[type[BaseException], ...] = (ConnectionError, TimeoutError, OSError),
) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
    def decorator(fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        @functools.wraps(fn)
        async def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
            last: BaseException | None = None
            for attempt in range(1, attempts + 1):
                try:
                    return await fn(*args, **kwargs)
                except retry_on as e:
                    last = e
                    if attempt == attempts:
                        break
                    delay = base_delay * (backoff ** (attempt - 1))
                    _log.warning(
                        "retry.scheduling",
                        attempt=attempt,
                        max_attempts=attempts,
                        delay=delay,
                        error=str(e),
                    )
                    await asyncio.sleep(delay)
            assert last is not None
            raise last
        return wrapped
    return decorator


class RateLimiter:
    def __init__(self, max_concurrent: int):
        self._sem = asyncio.Semaphore(max_concurrent)

    async def __aenter__(self) -> None:
        await self._sem.acquire()

    async def __aexit__(self, *exc: object) -> None:
        self._sem.release()
