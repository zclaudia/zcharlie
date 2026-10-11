"""Streaming HTTP-over-channel serving on top of the ``zclaudia-gateway`` SDK.

The SDK's ``serve_http`` buffers the whole request and the whole response,
which cannot carry an SSE stream. This module serves the same wire shape
(protocol v4 §7, channel kind ``http``) with a streaming handler instead:

The gateway sends ``http_request`` (method/path/headers), then the request
body as binary frames, then ``http_request_end``. The backend answers with
``http_response`` (status/headers), then body frames; closing the channel ends
the response. The handler runs as soon as the head arrives and may stream both
directions, and a client abort (the gateway closing the channel) is surfaced
through :attr:`HttpExchange.disconnected`.
"""

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import structlog
from websockets.exceptions import ConnectionClosed
from zclaudia_gateway import CHANNEL_KIND_HTTP, GatewayBackend, GatewayChannel

log = structlog.get_logger(__name__)

# How long a handler may take to wind down after the client went away.
_ABORT_GRACE_SECONDS = 5.0


class ChannelClosedError(Exception):
    """The channel's data socket is gone (client aborted, gateway tore it down)."""


async def _send(channel: GatewayChannel, data: str | bytes) -> None:
    if not channel.is_open:
        raise ChannelClosedError(channel.channel_id)
    try:
        await channel.send(data)
    except ConnectionClosed as exc:
        raise ChannelClosedError(channel.channel_id) from exc


@dataclass(frozen=True)
class HttpRequestHead:
    method: str
    path: str  # request target as the gateway forwards it: path plus query string
    headers: dict[str, str]  # gateway-allowlisted request headers only


class HttpExchange:
    """One proxied request/response pair.

    The request body queue is unbounded: the gateway only proxies API calls,
    whose bodies are small, and a bounded queue would stall disconnect
    detection behind a handler that never reads.
    """

    def __init__(self, head: HttpRequestHead, channel: GatewayChannel) -> None:
        self.head = head
        self.disconnected = asyncio.Event()
        self.started = False
        self._channel = channel
        self._body: asyncio.Queue[bytes | None] = asyncio.Queue()

    def _feed(self, chunk: bytes) -> None:
        self._body.put_nowait(chunk)

    def _end_body(self) -> None:
        self._body.put_nowait(None)

    def _abort(self) -> None:
        self.disconnected.set()
        self._body.put_nowait(None)  # wake a reader blocked on the next chunk

    async def read_chunk(self) -> bytes | None:
        """Next request body chunk, ``None`` once the body ended (or on abort).

        Safe to call from several tasks at once (ASGI servers poll ``receive``
        concurrently for disconnects): the end marker is sticky.
        """

        chunk = await self._body.get()
        if chunk is None:
            self._body.put_nowait(None)
        return chunk

    async def body(self) -> AsyncIterator[bytes]:
        """Request body chunks; ends at ``http_request_end`` or on abort."""

        while (chunk := await self.read_chunk()) is not None:
            yield chunk

    async def start(self, status: int, headers: dict[str, str] | None = None) -> None:
        if self.started:
            raise RuntimeError("response already started")
        self.started = True
        frame = {"type": "http_response", "status": status, "headers": headers or {}}
        await _send(self._channel, json.dumps(frame))

    async def write(self, chunk: bytes) -> None:
        if not self.started:
            raise RuntimeError("write before start")
        if chunk:
            await _send(self._channel, chunk)


HttpHandler = Callable[[HttpExchange], Awaitable[None]]


async def _invoke(handler: HttpHandler, exchange: HttpExchange, channel: GatewayChannel) -> None:
    try:
        await handler(exchange)
        if not exchange.started:
            await exchange.start(500)
    except ChannelClosedError:
        return  # client went away mid-response; nothing left to tell it
    except Exception:
        log.exception("gateway.http_handler_failed", path=exchange.head.path)
        if not exchange.started:
            with contextlib.suppress(ChannelClosedError):
                await exchange.start(500)
    with contextlib.suppress(Exception):
        await channel.close()  # closing the channel is what ends the response


async def serve_http_channel(channel: GatewayChannel, handler: HttpHandler) -> None:
    """Run one ``http`` channel to completion."""

    exchange: HttpExchange | None = None
    task: asyncio.Task[None] | None = None
    try:
        async for message in channel:
            if isinstance(message, bytes):
                if exchange is not None:
                    exchange._feed(message)
                continue
            try:
                frame = json.loads(message)
            except ValueError:
                continue
            frame_type = frame.get("type") if isinstance(frame, dict) else None
            if frame_type == "http_request" and exchange is None:
                head = HttpRequestHead(
                    method=str(frame.get("method", "GET")),
                    path=str(frame.get("path", "/")),
                    headers={str(k): str(v) for k, v in (frame.get("headers") or {}).items()},
                )
                exchange = HttpExchange(head, channel)
                task = asyncio.create_task(_invoke(handler, exchange, channel))
            elif frame_type == "http_request_end" and exchange is not None:
                exchange._end_body()
    finally:
        if exchange is not None:
            exchange._abort()
        if task is not None and not task.done():
            _done, pending = await asyncio.wait({task}, timeout=_ABORT_GRACE_SECONDS)
            for leftover in pending:
                leftover.cancel()
        with contextlib.suppress(Exception):
            await channel.close()


def serve_http_streaming(backend: GatewayBackend, handler: HttpHandler) -> None:
    """Answer the gateway's ``/api/proxy`` bridge with a streaming ``handler``.

    Use instead of ``backend.serve_http`` (buffered); both register the ``http``
    channel kind, so call only one.
    """

    async def on_channel(channel: GatewayChannel, _info: Any) -> None:
        await serve_http_channel(channel, handler)

    backend.on_channel(CHANNEL_KIND_HTTP, on_channel)
