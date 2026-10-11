"""Streaming HTTP bridge and connection keeper over the zclaudia-gateway SDK.

The SDK itself (handshake, heartbeats, reconnect) is tested in its own
repository; here a real ``GatewayBackend`` talks to an in-process fake gateway
to cover what zcharlie adds: SSE-capable HTTP serving and ``GatewayLink``.
"""

import asyncio
import json
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from websockets.exceptions import ConnectionClosed
from zclaudia_gateway import GatewayBackend

from investment_agent.gateway import GatewayLink, asgi_http_handler, serve_http_streaming
from tests.unit._gateway_fake import FakeGateway, read_http_response, send_http_request


@pytest_asyncio.fixture
async def gateway() -> AsyncIterator[FakeGateway]:
    async with FakeGateway() as gw:
        yield gw


def make_backend(gw: FakeGateway) -> GatewayBackend:
    return GatewayBackend(
        url=gw.url,
        credential="zga_test",
        namespace="ns",
        identity={"device_id": "host-1", "instance_id": "inst-1"},
        reconnect_min_ms=10,
        reconnect_max_ms=50,
    )


def build_app() -> FastAPI:
    app = FastAPI()
    app.state.release = asyncio.Event()
    app.state.finished = asyncio.Event()

    @app.get("/ping")
    async def ping(request: Request) -> dict[str, object]:
        return {"key": request.headers.get("x-api-key"), "q": dict(request.query_params)}

    @app.post("/echo")
    async def echo(request: Request) -> dict[str, object]:
        return {"body": (await request.body()).decode(), "ct": request.headers.get("content-type")}

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("boom")

    @app.get("/stream")
    async def stream() -> StreamingResponse:
        async def gen() -> AsyncIterator[bytes]:
            try:
                yield b"one\n"
                await app.state.release.wait()
                yield b"two\n"
            finally:
                app.state.finished.set()

        return StreamingResponse(gen(), media_type="text/event-stream")

    return app


@pytest_asyncio.fixture
async def serving(gateway: FakeGateway) -> AsyncIterator[tuple[FastAPI, GatewayBackend]]:
    app = build_app()
    backend = make_backend(gateway)
    serve_http_streaming(backend, asgi_http_handler(app, extra_headers={"X-API-Key": "secret"}))
    await backend.connect()
    yield app, backend
    await backend.close()


async def test_json_round_trip_injects_credentials(
    gateway: FakeGateway, serving: tuple[FastAPI, GatewayBackend]
) -> None:
    channel = await gateway.offer()
    await send_http_request(
        channel, "GET", "/ping?a=1&b=x%20y", headers={"x-api-key": "client-supplied"}
    )
    meta, body = await read_http_response(channel)
    assert meta["status"] == 200
    assert meta["headers"]["content-type"] == "application/json"
    # The gateway-supplied value must not win over the backend's own credential.
    assert json.loads(body) == {"key": "secret", "q": {"a": "1", "b": "x y"}}


async def test_request_body_is_streamed_in(
    gateway: FakeGateway, serving: tuple[FastAPI, GatewayBackend]
) -> None:
    channel = await gateway.offer()
    await send_http_request(
        channel,
        "POST",
        "/echo",
        headers={"content-type": "text/plain"},
        body=(b"hel", b"lo ", b"world"),
    )
    meta, body = await read_http_response(channel)
    assert meta["status"] == 200
    assert json.loads(body) == {"body": "hello world", "ct": "text/plain"}


async def test_unknown_route_and_handler_error(
    gateway: FakeGateway, serving: tuple[FastAPI, GatewayBackend]
) -> None:
    missing = await gateway.offer()
    await send_http_request(missing, "GET", "/nope")
    assert (await read_http_response(missing))[0]["status"] == 404

    broken = await gateway.offer()
    await send_http_request(broken, "GET", "/boom")
    assert (await read_http_response(broken))[0]["status"] == 500


async def test_response_streams_before_the_handler_finishes(
    gateway: FakeGateway, serving: tuple[FastAPI, GatewayBackend]
) -> None:
    app, _backend = serving
    channel = await gateway.offer()
    await send_http_request(channel, "GET", "/stream")

    meta = json.loads(await asyncio.wait_for(channel.recv(), 5))
    assert meta["status"] == 200
    assert meta["headers"]["content-type"].startswith("text/event-stream")
    assert await asyncio.wait_for(channel.recv(), 5) == b"one\n"
    assert not app.state.finished.is_set()  # first chunk arrived while the stream is still open

    app.state.release.set()
    assert await asyncio.wait_for(channel.recv(), 5) == b"two\n"
    with pytest.raises(ConnectionClosed):  # closing the channel is what ends the response
        await asyncio.wait_for(channel.recv(), 5)


async def test_client_abort_stops_the_handler(
    gateway: FakeGateway, serving: tuple[FastAPI, GatewayBackend]
) -> None:
    app, _backend = serving
    channel = await gateway.offer()
    await send_http_request(channel, "GET", "/stream")
    await asyncio.wait_for(channel.recv(), 5)  # meta
    await asyncio.wait_for(channel.recv(), 5)  # first chunk; the app now blocks on `release`

    await channel.close()  # what the gateway does when the HTTP client goes away
    await asyncio.wait_for(app.state.finished.wait(), 5)


async def test_link_retries_the_first_connect_until_the_gateway_accepts(
    gateway: FakeGateway,
) -> None:
    gateway.handshake_error = ("AUTH_FAILED", "not yet")
    link = GatewayLink(make_backend(gateway), retry_min=0.01, retry_max=0.05)
    await link.start()  # must not raise although the gateway refuses
    try:
        await gateway.wait_for_hellos(3)
        assert link.backend.state != "connected"

        gateway.handshake_error = None
        await asyncio.wait_for(link.wait_connected(), 5)
        assert link.backend.backend_id == "backend-1"
    finally:
        await link.stop()
    assert link.backend.state == "closed"
