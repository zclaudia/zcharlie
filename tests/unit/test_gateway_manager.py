"""GatewayManager: the gateway link follows the ``gateway_*`` runtime settings."""

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from fastapi import FastAPI, Request

from investment_agent.config import Settings
from investment_agent.gateway.manager import GatewayManager, config_from_settings
from investment_agent.settings.store import SettingsStore
from investment_agent.storage.database import init_engine, session_scope
from investment_agent.storage.models.base import Base
from tests.unit._gateway_fake import FakeGateway, read_http_response, send_http_request

ENCRYPTION_KEY = Fernet.generate_key().decode()


@pytest_asyncio.fixture
async def database(tmp_path: Path) -> AsyncIterator[None]:
    engine = init_engine(f"sqlite+aiosqlite:///{tmp_path / 'manager.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with session_scope() as session:
        assert await SettingsStore(session).claim_import()
    yield
    await engine.dispose()


@pytest_asyncio.fixture
async def gateway() -> AsyncIterator[FakeGateway]:
    async with FakeGateway() as gw:
        yield gw


def base_settings() -> Settings:
    return Settings(
        api_key="the-api-key", llm_tier3_api_key="x", settings_encryption_key=ENCRYPTION_KEY
    )


async def save(values: dict[str, object]) -> None:
    async with session_scope() as session:
        await SettingsStore(session, encryption_key=ENCRYPTION_KEY).apply(values, actor="web")


def build_app() -> FastAPI:
    app = FastAPI()

    @app.get("/ping")
    async def ping(request: Request) -> dict[str, str | None]:
        return {"key": request.headers.get("x-api-key")}

    return app


@pytest_asyncio.fixture
async def manager(database: None) -> AsyncIterator[GatewayManager]:
    mgr = GatewayManager(build_app(), base=base_settings())
    yield mgr
    await mgr.stop()


def connection_settings(gw: FakeGateway, **overrides: object) -> dict[str, object]:
    return {
        "gateway_enabled": True,
        "gateway_url": gw.url,
        "gateway_credential": "zga_first",
        "gateway_namespace": "ns",
        **overrides,
    }


async def test_disabled_by_default(manager: GatewayManager) -> None:
    await manager.reconcile()
    assert manager.status().state == "disabled"


async def test_enabled_but_incomplete_names_the_missing_fields(manager: GatewayManager) -> None:
    await save({"gateway_enabled": True, "gateway_url": "https://gw.example.com"})
    await manager.reconcile()
    status = manager.status()
    assert status.state == "incomplete"
    assert status.detail is not None
    assert "注册凭证" in status.detail
    assert "命名空间" in status.detail
    assert "网关地址" not in status.detail


async def test_connects_serves_the_app_and_follows_setting_changes(
    manager: GatewayManager, gateway: FakeGateway
) -> None:
    await save(connection_settings(gateway))
    await manager.reconcile()
    assert manager._backend is not None
    await asyncio.wait_for(manager._backend.wait_connected(), 5)

    status = manager.status()
    assert (status.state, status.backend_id, status.namespace) == ("connected", "backend-1", "ns")
    assert gateway.hellos[0]["gatewaySecret"] == "zga_first"
    assert gateway.hellos[0]["namespace"] == "ns"
    assert gateway.hellos[0]["identity"]["instanceId"] == "zcharlie"

    # The phone's request reaches the app, which sees the bootstrap API key.
    channel = await gateway.offer()
    await send_http_request(channel, "GET", "/ping")
    meta, body = await read_http_response(channel)
    assert meta["status"] == 200
    assert json.loads(body) == {"key": "the-api-key"}

    # Unchanged settings keep the same connection.
    await manager.reconcile()
    assert len(gateway.hellos) == 1

    # A rotated credential reconnects with the new one.
    await save({"gateway_credential": "zga_second"})
    await manager.reconcile()
    assert manager._backend is not None
    await asyncio.wait_for(manager._backend.wait_connected(), 5)
    assert gateway.hellos[-1]["gatewaySecret"] == "zga_second"

    # Disabling disconnects.
    await save({"gateway_enabled": False})
    await manager.reconcile()
    assert manager.status().state == "disabled"
    assert manager._backend is None


async def test_unreachable_gateway_reports_connecting_with_last_error(
    manager: GatewayManager, gateway: FakeGateway
) -> None:
    # The gateway refuses the credential with its wire code; the SDK classifies
    # UNAUTHORIZED as the client-side AUTH_FAILED backend error.
    gateway.handshake_error = ("UNAUTHORIZED", "credential revoked")
    await save(connection_settings(gateway))
    await manager.reconcile()
    await gateway.wait_for_hellos(1)
    await asyncio.sleep(0.05)
    status = manager.status()
    assert status.state == "connecting"
    assert status.detail is not None
    assert status.detail.startswith("AUTH_FAILED:")


async def test_a_replaced_backend_reports_replaced_and_stays_down(
    manager: GatewayManager, gateway: FakeGateway
) -> None:
    # SDK >= 0.4.0: a newer connection with the same credential takes over and
    # the displaced backend stops for good; the manager stands down with it
    # instead of fighting the new connection back.
    await save(connection_settings(gateway))
    await manager.reconcile()
    assert manager._backend is not None
    await asyncio.wait_for(manager._backend.wait_connected(), 5)
    await gateway.replace_control()
    await asyncio.sleep(0.05)
    status = manager.status()
    assert status.state == "replaced"
    assert status.detail is not None
    assert "停止重连" in status.detail

    # The poll loop re-runs reconcile on unchanged settings without reconnecting.
    await manager.reconcile()
    assert len(gateway.hellos) == 1
    assert manager.status().state == "replaced"

    # A settings change takes the identity back: disable, then enable again.
    await save({"gateway_enabled": False})
    await manager.reconcile()
    assert manager.status().state == "disabled"
    await manager.reconcile()  # a further poll keeps the link off
    await save({"gateway_enabled": True})
    await manager.reconcile()
    assert manager._backend is not None
    await asyncio.wait_for(manager._backend.wait_connected(), 5)
    assert len(gateway.hellos) == 2


async def test_a_failing_settings_read_is_reported_and_retried(
    manager: GatewayManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    async def flaky() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("settings unavailable")
        manager._status = manager._idle_status(base_settings())

    monkeypatch.setattr(manager, "reconcile", flaky)
    manager._poll_seconds = 0.01
    await manager.start()
    assert manager.status().state == "error"  # reported at startup, not raised
    assert manager.status().detail == "RuntimeError: settings unavailable"
    for _ in range(100):
        if calls >= 2:
            break
        await asyncio.sleep(0.02)
    assert calls >= 2  # the loop survived the failure
    assert manager.status().state == "disabled"


def test_config_requires_enabled_and_all_fields() -> None:
    complete = base_settings().model_copy(
        update={
            "gateway_enabled": True,
            "gateway_url": "https://gw",
            "gateway_credential": "zgb_x",
            "gateway_namespace": "ns",
        }
    )
    config = config_from_settings(complete)
    assert config is not None
    assert "zgb_x" not in repr(config)  # the credential never reaches logs
    assert config_from_settings(complete.model_copy(update={"gateway_enabled": False})) is None
    assert config_from_settings(complete.model_copy(update={"gateway_namespace": ""})) is None
