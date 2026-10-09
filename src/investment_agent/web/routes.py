"""Same-origin web UI routes under ``/app``.

The web UI authenticates with a short-lived signed session cookie issued at
``/app/login`` (the submitted API key is verified server-side and never
stored in the cookie or rendered back). JSON APIs keep ``X-API-Key`` support;
only the same-origin UI uses the cookie. State-changing UI requests validate
the ``Origin`` header when present (``Origin: null`` is rejected). Failed
logins are throttled per client IP; logout revokes sessions server-side.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from investment_agent.api.auth import (
    api_key_matches,
    client_key,
    get_auth_throttle,
    get_bootstrap_settings,
    is_same_origin,
)
from investment_agent.config import Settings
from investment_agent.web.session import (
    SESSION_COOKIE_NAME,
    create_session_token,
    revoke_sessions,
    session_is_valid,
    signing_material,
)

WEB_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))

router = APIRouter(prefix="/app", tags=["web"], include_in_schema=False)

_DISCLAIMER = "研究辅助，非投资建议"


def register_web(app: Any) -> None:
    """Attach the web router, session guard and static assets to the app."""

    app.add_middleware(WebSessionMiddleware)
    app.add_api_route("/", _root_redirect, methods=["GET"], include_in_schema=False)
    app.include_router(router)
    app.mount(
        "/app/static",
        StaticFiles(directory=str(WEB_DIR / "static")),
        name="web_static",
    )


async def _root_redirect() -> RedirectResponse:
    """Send bare-host visitors to the web UI (the session guard handles login)."""

    return RedirectResponse(url="/app", status_code=status.HTTP_302_FOUND)


async def require_same_origin(
    request: Request, settings: Annotated[Settings, Depends(get_bootstrap_settings)]
) -> None:
    """Reject cross-origin state-changing UI requests (CSRF defence in depth)."""

    if not is_same_origin(request, settings):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="cross-origin request")


_SESSION_EXEMPT_PATHS = frozenset({"/app/login", "/app/logout"})


class WebSessionMiddleware:
    """Guard /app pages with the signed session cookie.

    Middleware (instead of a route dependency) because a dependency listed in
    ``dependencies=[...]`` cannot short-circuit with its own redirect response.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        path = request.url.path
        needs_auth = (
            path.startswith("/app")
            and not path.startswith("/app/static/")
            and path not in _SESSION_EXEMPT_PATHS
        )
        if needs_auth:
            settings = get_bootstrap_settings()
            token = request.cookies.get(SESSION_COOKIE_NAME, "")
            if not session_is_valid(token, settings):
                response = _redirect_to_login()
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def _redirect_to_login() -> RedirectResponse:
    return RedirectResponse(url="/app/login", status_code=status.HTTP_302_FOUND)


def _render(
    template_name: str,
    request: Request,
    *,
    status_code: int = status.HTTP_200_OK,
    **context: Any,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name=template_name,
        context={"disclaimer": _DISCLAIMER, **context},
        status_code=status_code,
    )


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str | None = None) -> HTMLResponse:
    return _render("login.html", request, error=error)


@router.post(
    "/login",
    dependencies=[Depends(require_same_origin)],
)
async def login(
    request: Request,
    settings: Annotated[Settings, Depends(get_bootstrap_settings)],
    api_key: Annotated[str, Form(alias="api_key")],
    next_url: Annotated[str, Form(alias="next")] = "/app",
) -> Any:
    throttle = get_auth_throttle(request)
    client = client_key(request)
    retry_after = throttle.retry_after(client)
    if retry_after is not None:
        throttled = _render(
            "login.html",
            request,
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            error="失败次数过多，请稍后再试",
        )
        throttled.headers["Retry-After"] = str(retry_after)
        return throttled
    if not api_key_matches(api_key, settings.api_key):
        throttle.record_failure(client)
        return _render(
            "login.html",
            request,
            status_code=status.HTTP_401_UNAUTHORIZED,
            error="API Key 不正确",
        )
    throttle.reset(client)
    target = next_url if _is_safe_next(next_url) else "/app"
    response = RedirectResponse(url=target, status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        SESSION_COOKIE_NAME,
        create_session_token(
            api_key=signing_material(settings),
            ttl_minutes=settings.web_session_ttl_minutes,
        ),
        max_age=settings.web_session_ttl_minutes * 60,
        httponly=True,
        secure=settings.web_cookie_secure,
        samesite="strict",
        path="/",
    )
    return response


def _is_safe_next(next_url: str) -> bool:
    return next_url == "/app" or next_url.startswith("/app/")


@router.post("/logout", dependencies=[Depends(require_same_origin)])
async def logout(
    request: Request, settings: Annotated[Settings, Depends(get_bootstrap_settings)]
) -> RedirectResponse:
    """POST-only logout; revokes every outstanding session server-side.

    Only a request carrying a currently valid session can trigger revocation,
    so an anonymous POST cannot sign the user out.
    """

    token = request.cookies.get(SESSION_COOKIE_NAME, "")
    if session_is_valid(token, settings):
        revoke_sessions(settings.data_dir)
    response = RedirectResponse(url="/app/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(
        SESSION_COOKIE_NAME,
        path="/",
        secure=settings.web_cookie_secure,
        httponly=True,
        samesite="strict",
    )
    return response


@router.get("", response_class=HTMLResponse)
async def overview_page(request: Request) -> HTMLResponse:
    return _render("overview.html", request, page_title="今日概览", active_nav="overview")


@router.get("/watchlist", response_class=HTMLResponse)
async def watchlist_page(request: Request) -> HTMLResponse:
    return _render("watchlist.html", request, page_title="自选股", active_nav="watchlist")


@router.get("/stocks/{ticker}", response_class=HTMLResponse)
async def stock_page(request: Request, ticker: str) -> HTMLResponse:
    normalized = ticker.upper()
    if re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", normalized) is None:
        return _render(
            "error.html",
            request,
            status_code=status.HTTP_400_BAD_REQUEST,
            page_title="无效标的",
            message=f"不是有效的 A 股代码：{ticker}",
        )
    return _render(
        "stock.html",
        request,
        page_title=f"单股研究 {normalized}",
        active_nav="watchlist",
        ticker=normalized,
    )


@router.get("/runs", response_class=HTMLResponse)
async def runs_page(request: Request) -> HTMLResponse:
    return _render("runs.html", request, page_title="运行中心", active_nav="runs")


@router.get("/health", response_class=HTMLResponse)
async def health_page(request: Request) -> HTMLResponse:
    return _render("health.html", request, page_title="数据健康", active_nav="health")


@router.get("/candidates", response_class=HTMLResponse)
async def candidates_page(request: Request) -> HTMLResponse:
    return _render("candidates.html", request, page_title="本周候选", active_nav="candidates")


@router.get("/candidates/{candidate_id}", response_class=HTMLResponse)
async def candidate_page(request: Request, candidate_id: str) -> HTMLResponse:
    if re.fullmatch(r"[1-9]\d{0,9}", candidate_id) is None:
        return _render(
            "error.html",
            request,
            status_code=status.HTTP_400_BAD_REQUEST,
            page_title="无效候选",
            message=f"不是有效的候选编号：{candidate_id}",
        )
    return _render(
        "candidate.html",
        request,
        page_title=f"候选 #{candidate_id}",
        active_nav="candidates",
        candidate_id=int(candidate_id),
    )


@router.get("/templates", response_class=HTMLResponse)
async def templates_page(request: Request) -> HTMLResponse:
    return _render("templates.html", request, page_title="规则模板", active_nav="templates")


@router.get("/templates/{template_id}", response_class=HTMLResponse)
async def template_page(request: Request, template_id: str) -> HTMLResponse:
    if re.fullmatch(r"[a-z][a-z0-9_]{1,63}", template_id) is None:
        return _render(
            "error.html",
            request,
            status_code=status.HTTP_400_BAD_REQUEST,
            page_title="无效模板",
            message=f"不是有效的模板 id：{template_id}",
        )
    return _render(
        "template.html",
        request,
        page_title=f"模板 {template_id}",
        active_nav="templates",
        template_id=template_id,
    )


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request) -> HTMLResponse:
    return _render("settings.html", request, page_title="设置", active_nav="settings")
