"""Process liveness file for container health checks.

The worker and the scheduler have no HTTP port, so their compose health checks
read a file instead: when ``LIVENESS_FILE`` is set, a background task rewrites
that file every few seconds for as long as the event loop is responsive. A
crashed process or a wedged loop stops refreshing it and the health check
(``now - mtime`` over a threshold) turns the container unhealthy.

Unset (local runs, tests) means no file and no task.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from collections.abc import AsyncIterator
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)

LIVENESS_ENV = "LIVENESS_FILE"
DEFAULT_INTERVAL_SECONDS = 15.0


def touch(path: Path) -> None:
    """Write the current epoch seconds; the mtime is what health checks read."""

    path.write_text(f"{int(time.time())}\n", encoding="ascii")


def _remove(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.unlink()


async def _beat(path: Path, interval: float) -> None:
    failing = False
    while True:
        try:
            touch(path)
            failing = False
        except OSError as exc:
            if not failing:
                log.warning("liveness.write_failed", path=str(path), error=str(exc))
            failing = True
        await asyncio.sleep(interval)


@contextlib.asynccontextmanager
async def liveness(
    path: str | None = None, *, interval: float = DEFAULT_INTERVAL_SECONDS
) -> AsyncIterator[None]:
    """Keep ``path`` (default: ``$LIVENESS_FILE``) fresh while the body runs."""

    raw = path if path is not None else os.environ.get(LIVENESS_ENV, "")
    if not raw:
        yield
        return
    target = Path(raw)
    task = asyncio.create_task(_beat(target, interval))
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        _remove(target)
