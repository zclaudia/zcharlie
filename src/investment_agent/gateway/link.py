"""Keep a :class:`zclaudia_gateway.GatewayBackend` connected from application start.

The SDK's ``connect()`` raises when the first handshake fails and only
auto-recovers *after* a successful connect. An application must start (and keep
serving its own UI) while the gateway is unreachable, so this retries the first
connect in the background; once connected the SDK's own reconnect takes over.
"""

import asyncio

import structlog
from zclaudia_gateway import GatewayBackend, GatewayBackendState

log = structlog.get_logger(__name__)


class GatewayLink:
    def __init__(
        self, backend: GatewayBackend, *, retry_min: float = 1.0, retry_max: float = 30.0
    ) -> None:
        self.backend = backend
        self._retry_min = retry_min
        self._retry_max = retry_max
        self._task: asyncio.Task[None] | None = None
        self._connected = asyncio.Event()
        self.last_error: str | None = None
        """Why the most recent *initial* connect attempt failed (cleared on success)."""
        backend.on_state(self._on_state)

    def _on_state(self, state: GatewayBackendState) -> None:
        if state == "connected":
            self._connected.set()
        else:
            self._connected.clear()

    async def start(self) -> None:
        """Begin connecting in the background; never raises for an unreachable gateway."""

        if self._task is None:
            self._task = asyncio.create_task(self._connect_until_up(), name="gateway-link")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self.backend.close()

    async def wait_connected(self) -> None:
        await self._connected.wait()

    async def _connect_until_up(self) -> None:
        attempt = 0
        while True:
            try:
                backend_id, epoch = await self.backend.connect()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = str(exc) or type(exc).__name__
                delay = min(self._retry_min * 2**attempt, self._retry_max)
                attempt += 1
                log.warning("gateway.connect_failed", error=repr(exc), retry_in=delay)
                await asyncio.sleep(delay)
                continue
            self.last_error = None
            log.info("gateway.connected", backend_id=backend_id, epoch=epoch)
            return
