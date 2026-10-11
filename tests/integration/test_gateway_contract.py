"""GatewayBackend against the real zclaudia-gateway (Node reference implementation).

Opt-in: set ``ZCLAUDIA_GATEWAY_DIR`` to a built checkout of zclaudia-gateway
(``pnpm build`` done, so ``dist/index.js`` exists) and have ``node`` on PATH.
A throwaway gateway process is spawned per test on a free port with a temp
data dir, so nothing touches a deployed gateway.
"""

import asyncio
import json
import os
import shutil
import socket
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from websockets.asyncio.client import connect
from zclaudia_gateway import GatewayBackend

from investment_agent.gateway import asgi_http_handler, serve_http_streaming

GATEWAY_DIR = os.environ.get("ZCLAUDIA_GATEWAY_DIR", "")
ADMIN_TOKEN = "contract-test-admin"

pytestmark = pytest.mark.skipif(
    not (
        GATEWAY_DIR and (Path(GATEWAY_DIR) / "dist" / "index.js").exists() and shutil.which("node")
    ),
    reason="set ZCLAUDIA_GATEWAY_DIR to a built zclaudia-gateway checkout (and have node) to run",
)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest_asyncio.fixture
async def gateway_url(tmp_path: Path) -> AsyncIterator[str]:
    port = _free_port()
    env = {
        **os.environ,
        "GATEWAY_ADMIN_TOKEN": ADMIN_TOKEN,
        "GATEWAY_PORT": str(port),
        "ZCLAUDIA_DATA_DIR": str(tmp_path),
    }
    proc = await asyncio.create_subprocess_exec(
        "node",
        "dist/index.js",
        cwd=GATEWAY_DIR,
        env=env,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        async with httpx.AsyncClient() as client:
            for _ in range(100):
                try:
                    if (await client.get(f"{url}/health")).status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                await asyncio.sleep(0.1)
            else:
                pytest.fail("gateway did not start")
        yield url
    finally:
        proc.terminate()
        await proc.wait()


async def _issue(url: str, kind: str) -> str:
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{url}/api/admin/credentials",
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
            json={"type": kind, "namespace": "contract", "name": f"test-{kind}"},
        )
    response.raise_for_status()
    return str(response.json()["data"]["token"])


def _app() -> FastAPI:
    app = FastAPI()
    app.state.release = asyncio.Event()
    app.state.finished = asyncio.Event()

    @app.get("/ping")
    async def ping(request: Request) -> dict[str, object]:
        return {"key": request.headers.get("x-api-key"), "q": dict(request.query_params)}

    @app.post("/echo")
    async def echo(request: Request) -> dict[str, int]:
        return {"len": len(await request.body())}

    @app.get("/stream")
    async def stream() -> StreamingResponse:
        async def gen() -> AsyncIterator[bytes]:
            try:
                yield b"data: one\n\n"
                await app.state.release.wait()
                yield b"data: two\n\n"
            finally:
                app.state.finished.set()

        return StreamingResponse(gen(), media_type="text/event-stream")

    return app


@pytest_asyncio.fixture
async def proxied(
    gateway_url: str,
) -> AsyncIterator[tuple[str, dict[str, str], FastAPI, GatewayBackend]]:
    """(proxy base url, device auth headers, app, backend), enrolled with a zgb_ credential."""

    app = _app()
    backend = GatewayBackend(
        url=gateway_url,
        credential=await _issue(gateway_url, "backend"),  # exercises the zgb_ -> zga_ exchange
        namespace="contract",
        identity={"device_id": "host", "instance_id": "inst", "name": "contract"},
    )
    serve_http_streaming(
        backend, asgi_http_handler(app, extra_headers={"X-API-Key": "backend-secret"})
    )
    await asyncio.wait_for(backend.connect(), 10)
    device = await _issue(gateway_url, "device")
    try:
        yield (
            f"{gateway_url}/api/proxy/{backend.backend_id}",
            {"Authorization": f"Bearer {device}"},
            app,
            backend,
        )
    finally:
        await backend.close()


async def test_proxy_json_get_and_post(
    proxied: tuple[str, dict[str, str], FastAPI, GatewayBackend],
) -> None:
    base, auth, _app_, _backend = proxied
    async with httpx.AsyncClient() as client:
        ping = await client.get(f"{base}/ping", params={"a": "1"}, headers=auth)
        assert ping.status_code == 200
        assert ping.json() == {"key": "backend-secret", "q": {"a": "1"}}

        echo = await client.post(f"{base}/echo", content=b"x" * 200_000, headers=auth)
        assert echo.json() == {"len": 200_000}

        assert (await client.get(f"{base}/missing", headers=auth)).status_code == 404
        assert (await client.get(f"{base}/ping")).status_code in (401, 403)  # no credential


async def test_proxy_streams_and_propagates_client_abort(
    proxied: tuple[str, dict[str, str], FastAPI, GatewayBackend],
) -> None:
    base, auth, app, _backend = proxied
    async with (
        httpx.AsyncClient(timeout=10) as client,
        client.stream("GET", f"{base}/stream", headers=auth) as response,
    ):
        lines = response.aiter_lines()
        assert await anext(lines) == "data: one"  # arrives while the handler is still open
        assert not app.state.finished.is_set()
    # leaving the `with` closes the HTTP response early = client abort
    await asyncio.wait_for(app.state.finished.wait(), 10)

    app.state.finished.clear()
    app.state.release.set()
    async with httpx.AsyncClient(timeout=10) as client:
        body = (await client.get(f"{base}/stream", headers=auth)).text
    assert body == "data: one\n\ndata: two\n\n"


async def test_topic_retain_reaches_a_late_device_subscriber(
    gateway_url: str, proxied: tuple[str, dict[str, str], FastAPI, GatewayBackend]
) -> None:
    _base, auth, _app_, backend = proxied
    await backend.publish_topic("hints", {"n": 1}, retain=True)
    await asyncio.sleep(0.2)  # let the gateway store the retained payload

    device_token = auth["Authorization"].removeprefix("Bearer ")
    async with connect(f"ws{gateway_url.removeprefix('http')}/ws") as ws:
        await ws.send(
            json.dumps(
                {
                    "type": "peer_hello",
                    "protocolVersion": 4,
                    "namespace": "contract",
                    "clientProtocolVersion": 1,
                    "peerType": "client-only",
                    "gatewaySecret": device_token,
                    "identity": {"deviceId": "phone", "instanceId": "phone-1"},
                }
            )
        )
        ready = json.loads(await asyncio.wait_for(ws.recv(), 5))
        assert ready["type"] == "peer_ready"
        await ws.send(
            json.dumps(
                {"type": "topic_subscribe", "backendId": backend.backend_id, "topic": "hints"}
            )
        )
        seen: list[dict[str, object]] = []
        while not any(m.get("type") == "topic_message" for m in seen):
            seen.append(json.loads(await asyncio.wait_for(ws.recv(), 5)))
        message = next(m for m in seen if m.get("type") == "topic_message")
        assert message["payload"] == {"n": 1}
