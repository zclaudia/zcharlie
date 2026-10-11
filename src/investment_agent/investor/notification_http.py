"""Small HTTPS webhook sender. User endpoints, pinned public DNS/TLS, no redirects.

A timeout after POST may have delivered: caller records unknown, never claims
physical exactly once. Fixture injection happens outside this production adapter.
"""

import asyncio
import http.client
import ipaddress
import re
import threading
from urllib.parse import urlsplit

from investment_agent.evidence.disclosure_http import PinnedHTTPS, public_targets


def validate_endpoint(url: str) -> tuple[str, str]:
    if not url or len(url) > 2048 or any(ord(c) < 33 or ord(c) > 126 for c in url):
        raise ValueError("invalid_webhook_url")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("invalid_webhook_authority") from exc
    host = parsed.hostname
    if (
        parsed.scheme != "https"
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.fragment
        or parsed.query
        or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9.-]{0,252}", host)
    ):
        raise ValueError("invalid_webhook_authority")
    if host.lower() in {"localhost", "localhost.localdomain"}:
        raise ValueError("non_public_webhook_host")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and (
        not address.is_global or address.is_reserved or address.is_multicast
    ):
        raise ValueError("non_public_webhook_host")
    return host, parsed.path or "/"


class HTTPSWebhook:
    async def send(self, *, endpoint: str, token: str, delivery_id: str, body: bytes) -> str:
        host, path = validate_endpoint(endpoint)
        if (
            len(body) > 16384
            or len(token) > 2048
            or any(ord(c) < 33 or ord(c) > 126 for c in token)
        ):
            raise ValueError("invalid_webhook_payload_or_token")
        # DNS failure before starting POST is definitely unsent. All exceptions
        # after socket creation are treated conservatively as unknown by caller.
        targets = await asyncio.wait_for(asyncio.to_thread(public_targets, host), 5)
        return await asyncio.to_thread(self._send, host, path, targets[0], token, delivery_id, body)

    @staticmethod
    def _send(host: str, path: str, address: str, token: str, delivery_id: str, body: bytes) -> str:
        connection = PinnedHTTPS(host, address, 10)
        timer = threading.Timer(10, connection.abort)
        timer.daemon = True
        timer.start()
        try:
            headers = {
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Accept-Encoding": "identity",
                "Idempotency-Key": delivery_id,
                "User-Agent": "InvestmentAgent-notifications/1",
            }
            if token:
                headers["Authorization"] = "Bearer " + token
            connection.request("POST", path, body=body, headers=headers)
            response = connection.getresponse()
            # Redirects are never followed or retried. Bounded response reading
            # includes header trickle deadline via the active abort timer.
            length = response.getheader("Content-Length")
            if (
                response.getheader("Content-Encoding", "identity") != "identity"
                or length is not None
                and (not length.isdigit() or int(length) > 4096)
            ):
                return "unknown"
            received = 0
            while chunk := response.read1(min(1024, 4097 - received)):
                received += len(chunk)
                if received > 4096:
                    return "unknown"
            if length is not None and received != int(length):
                return "unknown"
            if 200 <= response.status < 300:
                return "delivered"
            if 300 <= response.status < 400 or response.status in {400, 401, 403, 404, 410, 422}:
                return "failed"
            return "unknown"
        except (OSError, http.client.HTTPException):
            return "unknown"
        finally:
            timer.cancel()
            connection.close()
