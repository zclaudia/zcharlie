"""Short-lived signed browser sessions.

The web session cookie never contains the API key: it is a signed,
self-expiring token (``v2.<issued_us>.<expires>.<nonce>.<hmac>``). The
signing key is derived server-side from ``WEB_SESSION_SECRET`` when set,
otherwise (backward compatible) from the configured API key. Cookies are
``HttpOnly`` and ``SameSite=Strict``; the ``Secure`` flag follows
``WEB_COOKIE_SECURE`` (required when served over TLS).

Server-side revocation: logging out records a "not-before" timestamp in
``<DATA_DIR>/web_session_not_before``; every token issued before it is
rejected. This is a single-user app, so logout signs out every browser
(tradeoff accepted for a stateless token + one tiny file, no DB table).
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import os
import secrets
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from investment_agent.config import Settings

SESSION_COOKIE_NAME = "zcharlie_session"
_SESSION_VERSION = "v2"
_SECRET_PREFIX = "zcharlie-web-session:"
REVOCATION_FILE_NAME = "web_session_not_before"

_session_secret_cache: dict[str, bytes] = {}


def signing_material(settings: Settings) -> str:
    """Secret material sessions are signed with (dedicated secret preferred)."""

    return settings.web_session_secret or settings.api_key


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
    """Issue a signed session token valid for ``ttl_minutes``.

    ``api_key`` is the signing material (see :func:`signing_material`).
    """

    if ttl_minutes <= 0:
        raise ValueError("session ttl must be positive")
    current = now if now is not None else time.time()
    issued_us = int(current * 1_000_000)
    expires = int(current + ttl_minutes * 60)
    nonce = secrets.token_urlsafe(16)
    payload = f"{_SESSION_VERSION}.{issued_us}.{expires}.{nonce}"
    return f"{payload}.{_sign(_secret(api_key), payload)}"


def verify_session_token(
    token: str,
    *,
    api_key: str,
    now: float | None = None,
    not_before_us: int | None = None,
) -> bool:
    """Return True only for untampered, unexpired, unrevoked tokens."""

    parts = token.split(".")
    if len(parts) != 5 or parts[0] != _SESSION_VERSION:
        return False
    payload = ".".join(parts[:4])
    expected = _sign(_secret(api_key), payload)
    # Bytes, like api_key_matches: a raw client can put non-ASCII in the
    # cookie and str comparison would raise TypeError (an unhandled 500 on
    # every request) instead of failing verification.
    if not hmac.compare_digest(parts[4].encode("utf-8"), expected.encode("utf-8")):
        return False
    try:
        issued_us = int(parts[1])
        expires = int(parts[2])
    except ValueError:
        return False
    if not_before_us is not None and issued_us < not_before_us:
        return False
    current = now if now is not None else time.time()
    return current < expires


def _revocation_path(data_dir: Path) -> Path:
    return data_dir / REVOCATION_FILE_NAME


def read_not_before_us(data_dir: Path) -> int | None:
    try:
        raw = _revocation_path(data_dir).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def revoke_sessions(data_dir: Path, *, now: float | None = None) -> int:
    """Invalidate every session issued up to ``now``; returns the cutoff (µs).

    The cutoff is one microsecond past ``now`` so a token minted in the same
    clock tick as the logout is revoked too.
    """

    cutoff = int((now if now is not None else time.time()) * 1_000_000) + 1
    data_dir.mkdir(parents=True, exist_ok=True)
    path = _revocation_path(data_dir)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(str(cutoff), encoding="utf-8")
    with contextlib.suppress(OSError):
        os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return cutoff


def session_is_valid(token: str, settings: Settings) -> bool:
    """Full check used by the app: signature, expiry and server revocation."""

    if not token:
        return False
    return verify_session_token(
        token,
        api_key=signing_material(settings),
        not_before_us=read_not_before_us(settings.data_dir),
    )
