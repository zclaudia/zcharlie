"""In-process fake of the gateway's backend-facing protocol (v4).

Plays only the gateway side that ``GatewayBackend`` talks to: control socket
(``/ws``: hello -> ready, heartbeats), channel offers and the per-channel data
sockets (``/channel/<id>?ticket=...``). The real gateway is covered by
``tests/integration/test_gateway_contract.py``.
"""

import asyncio
import json
import secrets
from typing import Any

from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import ConnectionClosed


class FakeGateway:
    def __init__(self) -> None:
        self.hellos: list[dict[str, Any]] = []
        self.messages: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.control: ServerConnection | None = None
        self.connected = asyncio.Event()
        self._hello_seen = asyncio.Event()
        self.handshake_error: tuple[str, str] | None = None
        self._pending: dict[str, tuple[str, asyncio.Future[ServerConnection]]] = {}
        self._server: Server | None = None
        self.port = 0

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def __aenter__(self) -> "FakeGateway":
        self._server = await serve(self._route, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *_exc: object) -> None:
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()

    async def _route(self, ws: ServerConnection) -> None:
        path = ws.request.path if ws.request else ""
        if path == "/ws":
            await self._serve_control(ws)
        elif path.startswith("/channel/"):
            await self._serve_data(ws, path)

    async def _serve_control(self, ws: ServerConnection) -> None:
        hello = json.loads(await ws.recv())
        self.hellos.append(hello)
        self._hello_seen.set()
        if self.handshake_error is not None:
            code, message = self.handshake_error
            await ws.send(json.dumps({"type": "gateway_error", "code": code, "message": message}))
            return
        epoch = len(self.hellos)
        await ws.send(
            json.dumps(
                {
                    "type": "peer_ready",
                    "protocolVersion": 4,
                    "peerSessionId": f"peer-{epoch}",
                    "recoveryToken": "r",
                    "backend": {"backendId": "backend-1", "epoch": epoch, "leaseTtlMs": 30000},
                    "registrySync": {"items": []},
                }
            )
        )
        self.control = ws
        self.connected.set()
        try:
            async for raw in ws:
                await self.messages.put(json.loads(raw))
        except ConnectionClosed:
            pass
        finally:
            if self.control is ws:
                self.control = None
                self.connected.clear()

    async def _serve_data(self, ws: ServerConnection, path: str) -> None:
        channel_id, _, query = path.removeprefix("/channel/").partition("?")
        pending = self._pending.pop(channel_id, None)
        if pending is None or query != f"ticket={pending[0]}":
            await ws.close(1008)
            return
        pending[1].set_result(ws)
        await ws.wait_closed()

    async def wait_for_hellos(self, count: int) -> None:
        while len(self.hellos) < count:
            self._hello_seen.clear()
            await asyncio.wait_for(self._hello_seen.wait(), 5)

    async def drop_control(self) -> None:
        assert self.control is not None
        await self.control.close()

    async def replace_control(self) -> None:
        """Close the control socket with the "backend replaced" code (SDK 0.4.0)."""

        assert self.control is not None
        await self.control.close(4001, "replaced by a newer connection")

    async def next_message(self, message_type: str) -> dict[str, Any]:
        while True:
            message = await asyncio.wait_for(self.messages.get(), 5)
            if message.get("type") == message_type:
                return message

    async def offer(self, kind: str | None = "http") -> ServerConnection:
        """Offer a channel to the backend and return the gateway end of its data socket."""

        assert self.control is not None
        channel_id = secrets.token_hex(8)
        ticket = secrets.token_urlsafe(12)
        data: asyncio.Future[ServerConnection] = asyncio.get_running_loop().create_future()
        self._pending[channel_id] = (ticket, data)
        offer: dict[str, Any] = {
            "type": "channel_offer",
            "channelId": channel_id,
            "ticket": ticket,
            "dataPath": f"/channel/{channel_id}",
        }
        if kind is not None:
            offer["kind"] = kind
        await self.control.send(json.dumps(offer))
        return await asyncio.wait_for(data, 5)


async def send_http_request(
    channel: ServerConnection,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    body: tuple[bytes, ...] = (),
) -> None:
    """Gateway side of the request direction (head, body frames, end)."""

    head = {"type": "http_request", "method": method, "path": path, "headers": headers or {}}
    await channel.send(json.dumps(head))
    for chunk in body:
        await channel.send(chunk)
    await channel.send(json.dumps({"type": "http_request_end"}))


async def read_http_response(channel: ServerConnection) -> tuple[dict[str, Any], bytes]:
    """Gateway side of the response direction: meta frame, then body until close."""

    meta = json.loads(await asyncio.wait_for(channel.recv(), 5))
    assert meta["type"] == "http_response"
    chunks: list[bytes] = []
    try:
        while True:
            frame = await asyncio.wait_for(channel.recv(), 5)
            assert isinstance(frame, bytes)
            chunks.append(frame)
    except ConnectionClosed:
        pass
    return meta, b"".join(chunks)
