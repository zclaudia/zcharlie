"""Adapt an ASGI app (FastAPI) into a gateway ``http`` channel handler.

Requests are dispatched in-process (no loopback port) and stream both ways.
The gateway forwards only a small header allowlist (no ``Authorization``, no
cookies), so authentication toward the app is the backend's job:
``extra_headers`` (e.g. ``X-API-Key``) are injected on every request and
override anything the gateway passed. Callers must therefore only expose a
backend whose gateway credentials they trust.
"""

from collections.abc import Mapping
from typing import Any
from urllib.parse import unquote

from websockets.exceptions import ConnectionClosed

from investment_agent.gateway.streaming import ChannelClosedError, HttpExchange, HttpHandler


def asgi_http_handler(app: Any, *, extra_headers: Mapping[str, str] | None = None) -> HttpHandler:
    injected = {name.lower(): value for name, value in (extra_headers or {}).items()}

    async def handle(exchange: HttpExchange) -> None:
        head = exchange.head
        raw_path, _, query = head.path.partition("?")
        merged = {name.lower(): value for name, value in head.headers.items()}
        merged.setdefault("host", "localhost")
        merged.update(injected)
        scope: dict[str, Any] = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": head.method.upper(),
            "scheme": "http",
            "path": unquote(raw_path),
            "raw_path": raw_path.encode("latin-1", "replace"),
            "query_string": query.encode("latin-1", "replace"),
            "root_path": "",
            "headers": [(n.encode("latin-1"), v.encode("latin-1")) for n, v in merged.items()],
            "client": ("gateway", 0),
            "server": ("gateway", 0),
        }
        body_done = False

        async def receive() -> dict[str, Any]:
            # Servers poll receive() concurrently (body reader + disconnect
            # listener), so this must not hold per-call iterator state.
            nonlocal body_done
            if not body_done:
                chunk = await exchange.read_chunk()
                if chunk is not None:
                    return {"type": "http.request", "body": chunk, "more_body": True}
                if not body_done:
                    body_done = True
                    return {"type": "http.request", "body": b"", "more_body": False}
            await exchange.disconnected.wait()
            return {"type": "http.disconnect"}

        async def send(message: dict[str, Any]) -> None:
            if exchange.disconnected.is_set():
                return  # like a real server: output after a disconnect is dropped
            try:
                if message["type"] == "http.response.start":
                    headers: dict[str, str] = {}
                    for raw_name, raw_value in message.get("headers", []):
                        name = raw_name.decode("latin-1").lower()
                        value = raw_value.decode("latin-1")
                        headers[name] = f"{headers[name]}, {value}" if name in headers else value
                    await exchange.start(int(message["status"]), headers)
                elif message["type"] == "http.response.body":
                    await exchange.write(message.get("body", b""))
            except (ChannelClosedError, ConnectionClosed):
                exchange.disconnected.set()

        await app(scope, receive, send)

    return handle
