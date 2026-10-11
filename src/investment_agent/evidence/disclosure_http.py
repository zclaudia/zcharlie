"""Small direct HTTPS reader: validate every hop and pin its public socket address.

No ambient proxies, cookies, credentials, content decompression or browser engine.
Production does not accept a client transport/resolver; test injection is explicit.
"""

import asyncio
import http.client
import ipaddress
import re
import socket
import ssl
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit

DIRECTORY = "https://www.cninfo.com.cn/new/data/szse_stock.json"
QUERY = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
MAX_BYTES = 16 * 1024 * 1024


class DisclosureError(ValueError):
    pass


def validate_url(url: str, purpose: str) -> tuple[str, str]:
    if len(url) > 2048 or any(ord(c) < 33 or ord(c) > 126 for c in url):
        raise DisclosureError("invalid_url")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise DisclosureError("invalid_url_authority") from exc
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.fragment
    ):
        raise DisclosureError("invalid_url_authority")
    host, path = parsed.hostname, parsed.path
    allowed = (
        (purpose == "directory" and url == DIRECTORY)
        or (purpose == "query" and url == QUERY)
        or (
            purpose == "original"
            and host == "static.cninfo.com.cn"
            and not parsed.query
            and re.fullmatch(
                r"/finalpage/\d{4}-\d{2}-\d{2}/[0-9A-Za-z_-]{1,100}\.(?:PDF|pdf|html|HTML)", path
            )
            is not None
        )
    )
    if not allowed:
        raise DisclosureError("unsupported_source_url")
    assert host is not None
    return host, path


def public_targets(
    host: str, resolver: Callable[..., list[tuple[Any, ...]]] = socket.getaddrinfo
) -> tuple[str, ...]:
    addresses = tuple(
        dict.fromkeys(item[4][0] for item in resolver(host, 443, type=socket.SOCK_STREAM))
    )
    if not addresses or any(
        not ipaddress.ip_address(ip).is_global
        or ipaddress.ip_address(ip).is_reserved
        or ipaddress.ip_address(ip).is_multicast
        or getattr(ipaddress.ip_address(ip), "ipv4_mapped", None) is not None
        for ip in addresses
    ):
        raise DisclosureError("non_public_or_mixed_dns")
    return addresses


class PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host: str, target: str, timeout: float) -> None:
        self.tls_context = ssl.create_default_context()
        super().__init__(host, timeout=timeout, context=self.tls_context)
        self.target = target
        self.deadline = time.monotonic() + timeout
        self.active_socket: socket.socket | None = None

    def remaining(self) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("official request deadline")
        return remaining

    def abort(self) -> None:
        if self.active_socket is not None:
            with suppress(OSError):
                self.active_socket.shutdown(socket.SHUT_RDWR)
            self.active_socket.close()

    def send(self, data: object) -> None:
        if self.sock is not None:
            self.sock.settimeout(self.remaining())
        super().send(data)  # type: ignore[arg-type]

    def connect(self) -> None:
        # Numeric sockaddr only. TLS hostname verification/SNI retain the host.
        family = socket.AF_INET6 if ":" in self.target else socket.AF_INET
        raw = socket.socket(family, socket.SOCK_STREAM)
        self.sock = raw
        self.active_socket = raw
        raw.settimeout(self.remaining())
        try:
            raw.connect((self.target, 443))
            raw.settimeout(self.remaining())
            tls = self.tls_context.wrap_socket(
                raw, server_hostname=self.host, do_handshake_on_connect=False
            )
            self.sock = tls
            self.active_socket = tls
            tls.settimeout(self.remaining())
            tls.do_handshake()
        except BaseException:
            self.abort()
            raw.close()
            raise


@dataclass(frozen=True)
class SourceResponse:
    status: int
    headers: dict[str, str]
    body: bytes
    url: str


class OfficialHTTPS:
    async def request(
        self,
        url: str,
        purpose: str,
        *,
        body: bytes | None = None,
        max_bytes: int = MAX_BYTES,
        timeout_seconds: float = 15,
    ) -> SourceResponse:
        if not 1 <= max_bytes <= MAX_BYTES or not 0 < timeout_seconds <= 30:
            raise DisclosureError("invalid_http_bound")
        deadline = time.monotonic() + timeout_seconds
        try:
            for hop in range(3):
                host, path = validate_url(url, purpose)
                targets = await asyncio.wait_for(
                    asyncio.to_thread(public_targets, host), deadline - time.monotonic()
                )
                response = await asyncio.wait_for(
                    asyncio.to_thread(
                        self._read, host, path, targets[0], url, body, max_bytes, deadline
                    ),
                    deadline - time.monotonic(),
                )
                if response.status in (301, 302, 303, 307, 308):
                    if hop == 2 or (body is not None and response.status not in (307, 308)):
                        raise DisclosureError("redirect_limit_or_method_change")
                    url = urljoin(url, response.headers.get("location", ""))
                    validate_url(url, purpose)
                    continue
                if response.status != 200:
                    raise DisclosureError(f"http_status_{response.status}")
                return response
        except TimeoutError as exc:
            raise DisclosureError("http_timeout") from exc
        except (OSError, http.client.HTTPException, ssl.SSLError) as exc:
            raise DisclosureError(
                "http_timeout" if time.monotonic() >= deadline else "http_connection_failure"
            ) from exc
        raise DisclosureError("redirect_limit")

    @staticmethod
    def _read(
        host: str,
        path: str,
        target: str,
        url: str,
        body: bytes | None,
        max_bytes: int,
        deadline: float,
    ) -> SourceResponse:
        connection = PinnedHTTPS(host, target, max(0.001, deadline - time.monotonic()))
        # Active abort also bounds a slow trickle during http.client's header
        # readline loops, which otherwise reuse the socket timeout repeatedly.
        deadline_timer = threading.Timer(max(0.001, deadline - time.monotonic()), connection.abort)
        deadline_timer.daemon = True
        deadline_timer.start()
        try:
            headers = {
                "Accept-Encoding": "identity",
                "Accept": "application/json,application/pdf,text/html",
                "User-Agent": "InvestmentAgent-disclosure/1",
            }
            if body is not None:
                headers["Content-Type"] = "application/x-www-form-urlencoded"
            connection.request(
                "POST" if body is not None else "GET", path, body=body, headers=headers
            )
            if connection.sock is not None:
                connection.sock.settimeout(connection.remaining())
            response = connection.getresponse()
            received = {k.lower(): v for k, v in response.getheaders()}
            if received.get("content-encoding", "identity").lower() != "identity":
                raise DisclosureError("unsupported_content_encoding")
            length = received.get("content-length")
            if length is not None and (not length.isdigit() or int(length) > max_bytes):
                raise DisclosureError("response_too_large")
            chunks, total = [], 0
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DisclosureError("http_timeout")
                if connection.active_socket is not None:
                    connection.active_socket.settimeout(remaining)
                chunk = response.read1(min(65536, max_bytes + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > max_bytes:
                    raise DisclosureError("response_too_large")
            if length is not None and total != int(length):
                raise DisclosureError("incomplete_response")
            return SourceResponse(response.status, received, b"".join(chunks), url)
        finally:
            deadline_timer.cancel()
            connection.close()
