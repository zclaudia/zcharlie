"""API authentication dependencies.

JSON APIs accept ``X-API-Key``; the same-origin browser UI authenticates with
the signed session cookie instead. Cookie-authenticated state-changing
requests must be same-origin (Origin/Referer check) because they bypass the
explicit API key presentation.
"""

import secrets
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status
from starlette.datastructures import URL

from investment_agent.config import Settings
from investment_agent.web.session import SESSION_COOKIE_NAME, verify_session_token


def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


async def verify_api_key(
    settings: Annotated[Settings, Depends(get_settings)],
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> None:
    if x_api_key is None or x_api_key != settings.api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid X-API-Key",
            headers={"WWW-Authenticate": "ApiKey"},
        )


def is_same_origin(request: Request) -> bool:
    """True when Origin/Referer matches Host, or when neither is present."""

    origin = request.headers.get("origin")
    if origin is None:
        referer = request.headers.get("referer")
        if referer is None:
            return True
        origin = referer
    try:
        origin_host = URL(origin).netloc
    except Exception:
        return False
    if not origin_host:
        return True
    return bool(origin_host == request.headers.get("host"))


async def verify_api_key_or_session(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> None:
    """Accept the API key header or a valid same-origin browser session."""

    if x_api_key is not None:
        if secrets.compare_digest(x_api_key, settings.api_key):
            return
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid X-API-Key",
            headers={"WWW-Authenticate": "ApiKey"},
        )

    token = request.cookies.get(SESSION_COOKIE_NAME, "")
    if token and verify_session_token(token, api_key=settings.api_key):
        if request.method in {"POST", "PUT", "PATCH", "DELETE"} and not is_same_origin(request):
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
