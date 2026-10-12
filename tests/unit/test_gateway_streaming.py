"""GatewayManager serves authenticated streaming requests through the SDK."""

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest_asyncio
from cryptography.fernet import Fernet
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

from investment_agent.config import Settings
from investment_agent.gateway.manager import GatewayManager
from investment_agent.settings.store import SettingsStore
from investment_agent.storage.database import init_engine, session_scope
from investment_agent.storage.models.base import Base
from tests.unit._gateway_fake import FakeGateway, send_http_request

ENCRYPTION_KEY = Fernet.generate_key().decode()


@pytest_asyncio.fixture
async def database(tmp_path: Path) -> AsyncIterator[None]:
    engine = init_engine(f"sqlite+aiosqlite:///{tmp_path / 'streaming.db'}")
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


def build_app() -> FastAPI:
    app = FastAPI()
    app.state.release = asyncio.Event()
    app.state.finished = asyncio.Event()

    @app.get("/ping")
    async def ping(request: Request) -> dict[str, str | None]:
        return {"key": request.headers.get("x-api-key")}

    @app.get("/events")
    async def events() -> StreamingResponse:
        async def gen() -> AsyncIterator[bytes]:
            try:
                yield b"data: one\n\n"
                await app.state.release.wait()
                yield b"data: two\n\n"
            finally:
                app.state.finished.set()

        return StreamingResponse(gen(), media_type="text/event-stream")

    return app


async def test_manager_streams_sse_and_stops_on_client_abort(
    database: None, gateway: FakeGateway
) -> None:
    app = build_app()
    manager = GatewayManager(
        app,
        base=Settings(
            api_key="server-api-key",
            llm_tier3_api_key="x",
            settings_encryption_key=ENCRYPTION_KEY,
        ),
    )
    async with session_scope() as session:
        await SettingsStore(session, encryption_key=ENCRYPTION_KEY).apply(
            {
                "gateway_enabled": True,
                "gateway_url": gateway.url,
                "gateway_credential": "zga_test",
                "gateway_namespace": "test",
            },
            actor="test",
        )

    try:
        await manager.reconcile()
        assert manager._backend is not None
        await asyncio.wait_for(manager._backend.wait_connected(), 5)

        ping = await gateway.offer()
        await send_http_request(
            ping,
            "GET",
            "/ping",
            headers={"x-api-key": "client-supplied-key"},
        )
        ping_meta = json.loads(await asyncio.wait_for(ping.recv(), 5))
        assert ping_meta["status"] == 200
        assert json.loads(await asyncio.wait_for(ping.recv(), 5)) == {"key": "server-api-key"}

        stream = await gateway.offer()
        await send_http_request(stream, "GET", "/events")
        stream_meta = json.loads(await asyncio.wait_for(stream.recv(), 5))
        assert stream_meta["status"] == 200
        assert stream_meta["headers"]["content-type"].startswith("text/event-stream")
        assert await asyncio.wait_for(stream.recv(), 5) == b"data: one\n\n"

        await stream.close()
        await asyncio.wait_for(app.state.finished.wait(), 5)
    finally:
        await manager.stop()
