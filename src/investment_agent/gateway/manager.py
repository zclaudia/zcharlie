"""Run the gateway link from the runtime settings (``gateway_*`` keys).

The API process owns one :class:`GatewayManager`. It re-reads the effective
settings every few seconds and (re)builds the link when they change, so the
gateway can be enabled, repointed or have its credential rotated from the
settings page without a restart. A failure to read settings or to connect is
reported through :meth:`GatewayManager.status` and never stops the API.
"""

import asyncio
import contextlib
from dataclasses import dataclass, field
from typing import Any, Literal

import structlog
from zclaudia_gateway import GatewayBackend, asgi_http_handler

from investment_agent.config import Settings
from investment_agent.settings.loader import load_settings
from investment_agent.storage.database import session_scope

log = structlog.get_logger(__name__)

DEVICE_ID = "zcharlie"
DEFAULT_POLL_SECONDS = 10.0

GatewayState = Literal["disabled", "incomplete", "connecting", "connected", "error"]


@dataclass(frozen=True)
class GatewayConfig:
    url: str
    namespace: str
    instance_id: str
    credential: str = field(repr=False)


@dataclass(frozen=True)
class GatewayStatus:
    state: GatewayState
    detail: str | None = None
    url: str | None = None
    namespace: str | None = None
    backend_id: str | None = None
    epoch: int | None = None


_FIELD_LABELS = (
    ("gateway_url", "网关地址"),
    ("gateway_credential", "注册凭证"),
    ("gateway_namespace", "命名空间"),
)


def missing_fields(settings: Settings) -> list[str]:
    return [label for key, label in _FIELD_LABELS if not getattr(settings, key)]


def config_from_settings(settings: Settings) -> GatewayConfig | None:
    """The link configuration, or ``None`` when disabled or incomplete."""

    if not settings.gateway_enabled or missing_fields(settings):
        return None
    return GatewayConfig(
        url=settings.gateway_url,
        namespace=settings.gateway_namespace,
        instance_id=settings.gateway_instance_id,
        credential=settings.gateway_credential,
    )


class GatewayManager:
    def __init__(
        self,
        app: Any,
        *,
        base: Settings,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
        backend_factory: type[GatewayBackend] = GatewayBackend,
    ) -> None:
        self._app = app
        self._base = base
        self._poll_seconds = poll_seconds
        self._backend_factory = backend_factory
        self._task: asyncio.Task[None] | None = None
        self._config: GatewayConfig | None = None
        self._backend: GatewayBackend | None = None
        self._status = GatewayStatus("disabled")
        self._last_logged_error: str | None = None

    async def start(self) -> None:
        """Apply the current settings once, then keep following them in the background.

        The first pass runs inline so startup stays sequential (no database
        work racing the rest of the lifespan); a failure is reported through
        :meth:`status`, never raised.
        """

        if self._task is None:
            await self._reconcile_safely()
            self._task = asyncio.create_task(self._run(), name="gateway-manager")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self._teardown()

    def status(self) -> GatewayStatus:
        backend = self._backend
        if backend is None or self._config is None:
            return self._status
        if backend.state == "connected":
            return GatewayStatus(
                "connected",
                url=self._config.url,
                namespace=self._config.namespace,
                backend_id=backend.backend_id,
                epoch=backend.epoch,
            )
        return GatewayStatus(
            "connecting",
            url=self._config.url,
            namespace=self._config.namespace,
        )

    async def reconcile(self) -> None:
        """One pass: make the running link match the effective settings."""

        async with session_scope() as session:
            effective = await load_settings(session, base=self._base)
        config = config_from_settings(effective)
        if config == self._config:
            if config is None:
                self._status = self._idle_status(effective)
            return
        await self._teardown()
        self._config = config
        if config is None:
            self._status = self._idle_status(effective)
            log.info("gateway.link_disabled", detail=self._status.detail)
            return
        self._backend = await self._build_backend(config)
        log.info("gateway.link_configured", url=config.url, namespace=config.namespace)

    def _idle_status(self, settings: Settings) -> GatewayStatus:
        if not settings.gateway_enabled:
            return GatewayStatus("disabled")
        return GatewayStatus("incomplete", detail="未填写：" + "、".join(missing_fields(settings)))

    async def _build_backend(self, config: GatewayConfig) -> GatewayBackend:
        backend = self._backend_factory(
            url=config.url,
            credential=config.credential,
            namespace=config.namespace,
            identity={"device_id": DEVICE_ID, "instance_id": config.instance_id, "name": DEVICE_ID},
        )
        # The gateway forwards no Authorization / cookie, so the API key is
        # injected here: a phone authenticates to the gateway, never to us.
        backend.serve_http_stream(
            asgi_http_handler(self._app, extra_headers={"X-API-Key": self._base.api_key})
        )
        await backend.start()
        return backend

    async def _teardown(self) -> None:
        backend, self._backend = self._backend, None
        self._config = None
        if backend is not None:
            with contextlib.suppress(Exception):
                await backend.close()

    async def _reconcile_safely(self) -> None:
        try:
            await self.reconcile()
            self._last_logged_error = None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            self._status = GatewayStatus("error", detail=message)
            if message != self._last_logged_error:  # a stuck failure logs once, not every poll
                self._last_logged_error = message
                log.warning("gateway.reconcile_failed", error=message)

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._poll_seconds)
            await self._reconcile_safely()
