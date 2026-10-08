"""API authentication dependencies.

JSON APIs accept ``X-API-Key``; the same-origin browser UI authenticates with
the signed session cookie instead. Cookie-authenticated state-changing
requests must prove they are same-origin: a matching ``Origin``/``Referer``
or, when neither is sent, the ``X-Requested-With: fetch`` header that the UI
fetch wrapper always adds (a cross-origin page cannot set it without a CORS
preflight, which this app never grants). ``Origin: null`` and unparseable
origins count as cross-origin.

Wrong API keys (header or login form) are throttled per client IP.

Two settings dependencies: ``get_bootstrap_settings`` (env only) for the auth
checks, which need just the bootstrap layer (``API_KEY``, ``WEB_*``) and must
not depend on the database; ``get_settings`` for route handlers, with the
runtime layer read from the DB per request (settings-plan §4.4).
"""

import secrets
from typing import Annotated, Literal

from fastapi import Depends, Header, HTTPException, Request, status
from starlette.datastructures import URL

from investment_agent.api.throttle import AuthFailureThrottle
from investment_agent.config import Settings
from investment_agent.settings.loader import load_settings
from investment_agent.web.session import SESSION_COOKIE_NAME, session_is_valid

STATE_CHANGING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
FETCH_MARKER_HEADER = "x-requested-with"
FETCH_MARKER_VALUE = "fetch"

_default_throttle = AuthFailureThrottle()


def get_bootstrap_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


async def get_settings() -> Settings:
    """Effective settings: env bootstrap + runtime values stored in the DB."""

    return await load_settings()


def api_key_matches(candidate: str | None, expected: str) -> bool:
    """Constant-time API key comparison shared by every auth entry point."""

    if candidate is None or not expected:
        return False
    return secrets.compare_digest(candidate.encode("utf-8"), expected.encode("utf-8"))


# ---- failure throttling -------------------------------------------------


def get_auth_throttle(request: Request) -> AuthFailureThrottle:
    throttle = getattr(request.app.state, "auth_throttle", None)
    if isinstance(throttle, AuthFailureThrottle):
        return throttle
    return _default_throttle


def client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _too_many_attempts(retry_after: int) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail="Too many failed authentication attempts; try again later",
        headers={"Retry-After": str(retry_after)},
    )


def enforce_not_throttled(request: Request) -> None:
    retry_after = get_auth_throttle(request).retry_after(client_key(request))
    if retry_after is not None:
        raise _too_many_attempts(retry_after)


def check_presented_api_key(request: Request, presented: str, expected: str) -> None:
    """Verify a presented key, throttling repeated failures (raises 429/401)."""

    enforce_not_throttled(request)
    if api_key_matches(presented, expected):
        return
    get_auth_throttle(request).record_failure(client_key(request))
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Missing or invalid X-API-Key",
        headers={"WWW-Authenticate": "ApiKey"},
    )


async def verify_api_key(
    request: Request,
    settings: Annotated[Settings, Depends(get_bootstrap_settings)],
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> None:
    if x_api_key is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid X-API-Key",
            headers={"WWW-Authenticate": "ApiKey"},
        )
    check_presented_api_key(request, x_api_key, settings.api_key)


# ---- origin checks ------------------------------------------------------

OriginStatus = Literal["same", "cross", "absent"]


def _netloc(value: str) -> str | None:
    """Host[:port] of an origin/referer value; None when null/unparseable."""

    if not value or value.strip().lower() == "null":
        return None
    try:
        url = URL(value)
    except Exception:
        return None
    if url.scheme not in {"http", "https"} or not url.netloc:
        return None
    return url.netloc.lower()


def origin_status(request: Request, settings: Settings | None = None) -> OriginStatus:
    """Classify the request's Origin (falling back to Referer)."""

    origin = request.headers.get("origin")
    if origin is None:
        origin = request.headers.get("referer")
        if origin is None:
            return "absent"
    netloc = _netloc(origin)
    if netloc is None:
        return "cross"
    allowed = set()
    host = request.headers.get("host")
    if host:
        allowed.add(host.lower())
    public_origin = settings.web_public_origin if settings is not None else ""
    if public_origin:
        public_netloc = _netloc(public_origin)
        if public_netloc:
            allowed.add(public_netloc)
    return "same" if netloc in allowed else "cross"


def is_same_origin(request: Request, settings: Settings | None = None) -> bool:
    """True unless Origin/Referer is present and cross-origin (or null)."""

    return origin_status(request, settings) != "cross"


def cookie_request_is_same_origin(request: Request, settings: Settings) -> bool:
    """Strict check for cookie-authenticated state-changing requests."""

    state = origin_status(request, settings)
    if state == "same":
        return True
    if state == "cross":
        return False
    marker = request.headers.get(FETCH_MARKER_HEADER, "")
    return marker.strip().lower() == FETCH_MARKER_VALUE


async def verify_api_key_or_session(
    request: Request,
    settings: Annotated[Settings, Depends(get_bootstrap_settings)],
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> None:
    """Accept the API key header or a valid same-origin browser session."""

    if x_api_key is not None:
        check_presented_api_key(request, x_api_key, settings.api_key)
        return

    token = request.cookies.get(SESSION_COOKIE_NAME, "")
    if token and session_is_valid(token, settings):
        if request.method in STATE_CHANGING_METHODS and not cookie_request_is_same_origin(
            request, settings
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="cross-origin request",
            )
        return

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required (X-API-Key or web session)",
        headers={"WWW-Authenticate": "ApiKey"},
    )
