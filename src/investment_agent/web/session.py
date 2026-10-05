"""Short-lived signed browser sessions.

The web session cookie never contains the API key: it is a signed,
self-expiring token (``v1.<expires>.<nonce>.<hmac>``) whose signing key is
derived server-side from the configured API key. Cookies are ``HttpOnly`` and
``SameSite=Strict``; the MVP binds locally, so the ``Secure`` flag is not set
by default (see ``web_routes``).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time

SESSION_COOKIE_NAME = "zcharlie_session"
_SESSION_VERSION = "v1"
_SECRET_PREFIX = "zcharlie-web-session:"

_session_secret_cache: dict[str, bytes] = {}


def _secret(api_key: str) -> bytes:
    cached = _session_secret_cache.get(api_key)
    if cached is None:
        cached = hashlib.sha256((_SECRET_PREFIX + api_key).encode("utf-8")).digest()
        if len(_session_secret_cache) > 8:
            _session_secret_cache.clear()
        _session_secret_cache[api_key] = cached
    return cached


def _sign(secret: bytes, payload: str) -> str:
    return hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()


def create_session_token(*, api_key: str, ttl_minutes: int, now: float | None = None) -> str:
    """Issue a signed session token valid for ``ttl_minutes``."""

    if ttl_minutes <= 0:
        raise ValueError("session ttl must be positive")
    expires = int((now if now is not None else time.time()) + ttl_minutes * 60)
    nonce = secrets.token_urlsafe(16)
    payload = f"{_SESSION_VERSION}.{expires}.{nonce}"
    return f"{payload}.{_sign(_secret(api_key), payload)}"


def verify_session_token(token: str, *, api_key: str, now: float | None = None) -> bool:
    """Return True only for untampered, unexpired tokens from this server."""

    parts = token.split(".")
    if len(parts) != 4 or parts[0] != _SESSION_VERSION:
        return False
    payload = ".".join(parts[:3])
    expected = _sign(_secret(api_key), payload)
    if not hmac.compare_digest(parts[3], expected):
        return False
    try:
        expires = int(parts[1])
    except ValueError:
        return False
    current = now if now is not None else time.time()
    return current < expires
