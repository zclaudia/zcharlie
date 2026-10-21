"""Same-origin web UI routes under ``/app``.

The web UI authenticates with a short-lived signed session cookie issued at
``/app/login`` (the submitted API key is verified server-side and never
stored in the cookie or rendered back). JSON APIs keep ``X-API-Key`` support;
only the same-origin UI uses the cookie. State-changing UI requests validate
the ``Origin`` header when present (``Origin: null`` is rejected). Failed
logins are throttled per client IP; logout revokes sessions server-side.

The stock page understands two kinds of display code. A-share codes take the
unchanged first branch (same validation, same 400 copy). A HK/US code is parsed
with the controlled registry and answered from the saved material of its
listing: the financial section is server-rendered from plan 09's read model, so
the page never fetches a supplier itself, and a code with no known listing is
answered with an explicit error instead of an invented view.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
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
from investment_agent.domain.a_share import parse_a_share_ticker
from investment_agent.domain.security import normalize_ticker
from investment_agent.materials.context import (
    DISPLAY_SUFFIXES,
    FinancialContextView,
    financial_context,
)
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.reference import Listing
from investment_agent.storage.repositories.listing import ListingRepository
from investment_agent.web.session import (
    SESSION_COOKIE_NAME,
    create_session_token,
    revoke_sessions,
    session_is_valid,
    signing_material,
)
from investment_agent.web.static_assets import (
    VERSIONED_PREFIX,
    VersionedStaticFiles,
    static_url,
)

WEB_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))


def _financial_timestamp(value: date | datetime | None) -> str:
    """Format financial event times to whole-second UTC; preserve review dates."""

    if value is None:
        return "未知"
    if isinstance(value, datetime):
        stamp = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        return stamp.strftime("%Y-%m-%d %H:%M:%S UTC")
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


templates.env.filters["financial_timestamp"] = _financial_timestamp

WEB_AUTH_API_KEY: Final = "api_key"


def _is_embedded(request: Request) -> bool:
    """True when the page was authenticated by ``X-API-Key`` (the app gateway)."""

    web_auth: object = request.scope.get("state", {}).get("web_auth")
    return web_auth == WEB_AUTH_API_KEY


templates.env.globals["is_embedded"] = _is_embedded
templates.env.globals["static_url"] = static_url

router = APIRouter(prefix="/app", tags=["web"], include_in_schema=False)

_DISCLAIMER = "研究辅助，非投资建议"

_HK_DISPLAY_CODE = re.compile(r"^\d{1,5}\.HK$")
"""HK display code of the page (``00002.HK``); the stored ticker is 5 digits."""

_US_DISPLAY_CODE = re.compile(r"^[A-Z]{1,6}(?:\.[A-Z])?$")
"""US display code of the page (``AAPL``); not every letter string is one.

The page only renders securities it can resolve to a local listing. A stricter
shape than the registry's own symbol rule keeps a malformed code on the
existing A-share 400 branch (``NOT-A-TICKER`` is not a page code) instead of
turning it into a lookup miss.
"""

_US_MARKET_CODES: Final[tuple[str, ...]] = ("NASDAQ", "NYSE")
"""Markets a bare US ticker is looked up in; the exchange is never assumed."""

MARKET_LABELS: Final[dict[str, str]] = {
    "HKEX": "香港交易所",
    "NASDAQ": "纳斯达克",
    "NYSE": "纽约证券交易所",
}
SECURITY_TYPE_LABELS: Final[dict[str, str]] = {
    "ordinary": "普通股",
    "ads": "ADS（美国存托凭证）",
    "unknown": "未标注",
}
VERIFICATION_LABELS: Final[dict[str, str]] = {
    "candidate": "候选（未核对原件）",
    "verified": "已核对原件",
}
CONFLICT_LABELS: Final[dict[str, str]] = {
    "unresolved": "来源冲突（未判定）",
    "resolved": "冲突已判定",
}
STATUS_LABELS: Final[dict[str, str]] = {
    "listing_not_covered": "当前未覆盖",
    "material_missing": "资料待采集",
    "partial_verification": "部分数据待核",
    "metric_not_applicable": "指标不适用",
    "source_conflict": "来源冲突",
    "material_stale": "资料待更新",
    "online_refresh_disabled": "在线刷新未开启",
    "raw_archive_missing": "原件归档不完整",
    "partial_coverage": "资料覆盖不完整",
    "awaiting_original_review": "原件待核对",
    "latestness_unknown": "最新状态未核实",
    "failed_refresh": "最近刷新失败",
}
"""Business wording of every read-model status code (no supplier/run identifier)."""

METRIC_STATUS_LABELS: Final[dict[str, str]] = {
    "ok": "已计算",
    "not_applicable": "不适用",
    "missing_inputs": "输入缺失",
    "missing_assumptions": "假设缺失",
    "stale_inputs": "输入过期",
    "outside_search_range": "超出检索范围",
    "calculation_error": "计算错误",
}
PERIOD_BASIS_LABELS: Final[dict[str, str]] = {
    "annual": "年度",
    "interim": "中期",
    "quarter": "季度",
    "ytd": "年初至今",
    "instant": "时点",
    "daily": "每日",
}
UNIT_LABELS: Final[dict[str, str]] = {"ratio": "比率"}
UNKNOWN_LABELS: Final[dict[str, str]] = {"unknown": "未标注"}
"""Display labels of the financial section; unknown codes fall back to themselves."""

FINANCIAL_LABELS: Final[dict[str, dict[str, str]]] = {
    "market": MARKET_LABELS,
    "security_type": SECURITY_TYPE_LABELS,
    "verification": VERIFICATION_LABELS,
    "conflict": CONFLICT_LABELS,
    "status": STATUS_LABELS,
    "metric_status": METRIC_STATUS_LABELS,
    "period_basis": PERIOD_BASIS_LABELS,
    "unit": UNIT_LABELS,
    "unknown": UNKNOWN_LABELS,
    "latestness": {
        "unknown": "未知",
        "checked_through": "已核对至记录日期",
        "stale": "有更新待核",
    },
    "metric_evidence": {
        "verified": "已核对证据可用",
        "candidate": "候选证据可用",
        "mixed": "候选与已核对并存",
        "missing": "证据不足",
        "not_applicable": "不适用",
        "unknown": "尚未评估",
    },
    "sources": {
        "hk_annual_reports": "港股报告来源",
        "us_issuer_facts": "美国发行人申报来源",
        "eastmoney.hk.native.v1": "港股年报来源",
        "sec.companyfacts.v1": "美国发行人申报来源",
    },
    "licensing": {
        "unknown": "未确认",
        "approved": "已确认",
        "denied": "未获许可",
        "scope_mismatch": "许可范围不匹配",
        "not_approved": "尚未核验",
    },
    "technical_acceptance": {
        "not_accepted": "未核验",
        "not_approved": "未核验",
        "scope_mismatch": "验收范围不匹配",
        "accepted": "已核验",
    },
    "source_reason": {
        "invalid_policy_schema": "来源策略记录格式无效",
        "unknown_source": "来源未列入受控策略",
        "identity_not_bound": "该证券未绑定此来源",
        "licensing_unknown": "使用许可尚未确认",
        "licensing_scope_mismatch": "许可范围不含该发行人",
        "licensing_citation_missing_or_invalid": "许可依据缺失或格式无效",
        "technical_acceptance_missing_or_mismatched": "技术验收尚未完成或版本不匹配",
        "technical_scope_mismatch": "技术验收范围不含该发行人",
        "technical_report_dates_invalid_or_insufficient": "技术验收记录日期无效或不足",
        "technical_report_citation_missing_or_invalid": "技术验收依据缺失或格式无效",
        "operationally_disabled": "运行开关已关闭",
        "global_refresh_disabled": "全局在线刷新未开启",
    },
    "refresh_status": {
        "queued": "排队中",
        "running": "执行中",
        "success": "已完成",
        "partial": "部分完成",
        "failed": "失败",
    },
    "refresh_reason": {
        "user_refresh": "用户点击刷新",
        "刷新运行失败": "刷新运行失败",
        "部分来源未完成": "部分来源未完成",
    },
}

INVALID_A_SHARE_COPY = "不是有效的 A 股代码：{ticker}"
"""Unchanged 400 copy of the A-share branch (behaviour stays exactly as before)."""


def _hk_us_code(ticker: str) -> tuple[tuple[str, ...], str] | None:
    """Candidate markets and stored ticker of a HK/US display code, or ``None``.

    ``ticker`` is the upper-cased display code. The canonical stored ticker comes
    from the controlled registry (:func:`normalize_ticker`), never from a string
    transform here; a US code without an exchange suffix is looked up in the US
    markets of the registry instead of one being assumed.
    """

    if _HK_DISPLAY_CODE.fullmatch(ticker) is not None:
        return ("HKEX",), normalize_ticker("HKEX", ticker)
    if _US_DISPLAY_CODE.fullmatch(ticker) is not None:
        return _US_MARKET_CODES, normalize_ticker("NASDAQ", ticker)
    return None


def _display_code(market_code: str, ticker: str) -> str:
    """The page code of a stored ticker (``00005`` -> ``00005.HK``)."""

    return f"{ticker}{DISPLAY_SUFFIXES.get(market_code, '')}"


def _financial_page(request: Request, view: FinancialContextView) -> HTMLResponse:
    """Render the stock page carrying one HK/US financial section."""

    return _render(
        "stock.html",
        request,
        page_title=f"单股研究 {view.display_code}",
        active_nav="watchlist",
        ticker=view.display_code,
        financial=view,
        listing_id=view.listing_id,
        labels=FINANCIAL_LABELS,
    )


def register_web(app: Any) -> None:
    """Attach the web router, session guard and static assets to the app."""

    app.add_middleware(WebSessionMiddleware)
    app.add_api_route("/", _root_redirect, methods=["GET"], include_in_schema=False)
    app.include_router(router)
    # Before the plain mount, which would otherwise swallow /app/static/v/….
    app.mount(VERSIONED_PREFIX, VersionedStaticFiles(), name="web_static_versioned")
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
    """Guard /app pages with the signed session cookie or ``X-API-Key``.

    Middleware (instead of a route dependency) because a dependency listed in
    ``dependencies=[...]`` cannot short-circuit with its own redirect response.

    The API key path serves the Android app, whose gateway strips cookies and
    injects ``X-API-Key``; such requests are marked ``state["web_auth"]`` so the
    templates render embedded mode. A wrong key gets 401 (429 once throttled)
    rather than the login redirect, since a header client cannot use the form.
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
        is_login_page = path == "/app/login" and request.method in {"GET", "HEAD"}
        if needs_auth or is_login_page:
            settings = get_bootstrap_settings()
            token = request.cookies.get(SESSION_COOKIE_NAME, "")
            if needs_auth and session_is_valid(token, settings):
                await self.app(scope, receive, send)
                return
            presented = request.headers.get("x-api-key")
            if presented is not None:
                rejection = _api_key_rejection(request, presented, settings)
                if rejection is not None:
                    await rejection(scope, receive, send)
                    return
                if is_login_page:
                    await _redirect_to_app()(scope, receive, send)
                    return
                scope.setdefault("state", {})["web_auth"] = WEB_AUTH_API_KEY
                await self.app(scope, receive, send)
                return
            if needs_auth:
                await _redirect_to_login()(scope, receive, send)
                return
        await self.app(scope, receive, send)


def _api_key_rejection(
    request: Request, presented: str, settings: Settings
) -> PlainTextResponse | None:
    """None when ``presented`` is the API key; else a 429/401, recording the failure."""

    throttle = get_auth_throttle(request)
    client = client_key(request)
    retry_after = throttle.retry_after(client)
    if retry_after is not None:
        return PlainTextResponse(
            "Too many failed authentication attempts; try again later",
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            headers={"Retry-After": str(retry_after)},
        )
    if api_key_matches(presented, settings.api_key):
        return None
    throttle.record_failure(client)
    return PlainTextResponse(
        "Missing or invalid X-API-Key",
        status_code=status.HTTP_401_UNAUTHORIZED,
        headers={"WWW-Authenticate": "ApiKey"},
    )


def _redirect_to_login() -> RedirectResponse:
    return RedirectResponse(url="/app/login", status_code=status.HTTP_302_FOUND)


def _redirect_to_app() -> RedirectResponse:
    return RedirectResponse(url="/app", status_code=status.HTTP_302_FOUND)


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
    return _render("watchlist.html", request, page_title="观察池", active_nav="watchlist")


@router.get("/portfolio", response_class=HTMLResponse)
async def portfolio_page(request: Request) -> HTMLResponse:
    return _render("portfolio.html", request, page_title="持仓池", active_nav="portfolio")


@router.get("/industries", response_class=HTMLResponse)
async def industries_page(request: Request) -> HTMLResponse:
    return _render("industries.html", request, page_title="行业年度研究", active_nav="industries")


@router.get("/agent", response_class=HTMLResponse)
async def agent_page(request: Request) -> HTMLResponse:
    return _render("agent.html", request, page_title="AI", active_nav="agent")


@router.get("/stocks/{ticker}", response_class=HTMLResponse)
async def stock_page(request: Request, ticker: str) -> HTMLResponse:
    normalized = ticker.upper()
    if parse_a_share_ticker(normalized) is not None:
        async with session_scope() as session:
            listing = await ListingRepository(session).get_a_share_listing(normalized)
            listing_id = listing.id if listing is not None else None
        return _render(
            "stock.html",
            request,
            page_title=f"单股研究 {normalized}",
            active_nav="watchlist",
            ticker=normalized,
            financial=None,
            listing_id=listing_id,
        )
    code = _hk_us_code(normalized)
    if code is None:
        return _render(
            "error.html",
            request,
            status_code=status.HTTP_400_BAD_REQUEST,
            page_title="无效标的",
            message=INVALID_A_SHARE_COPY.format(ticker=ticker),
        )
    markets, stored = code
    async with session_scope() as session:
        repository = ListingRepository(session)
        matches: list[Listing] = []
        for market_code in markets:
            listing = await repository.get_by_ticker(market_code, stored)
            if listing is not None:
                matches.append(listing)
        if not matches:
            return _render(
                "error.html",
                request,
                status_code=status.HTTP_404_NOT_FOUND,
                page_title="未收录标的",
                message=(
                    f"未收录该证券：{_display_code(markets[0], stored)}。"
                    "名单外证券的资料不采信；请先完成身份登记与资料采集。"
                ),
            )
        if len(matches) > 1:
            # Two markets carry the same ticker: the code alone cannot say which
            # security is meant, so no listing is silently preferred.
            return _render(
                "error.html",
                request,
                status_code=status.HTTP_400_BAD_REQUEST,
                page_title="代码不唯一",
                message=f"该代码在多个市场登记：{normalized}；请在名单中核对证券身份。",
            )
        # Same session as the lookup above, so the row cannot disappear here.
        view = await financial_context(session, matches[0].id)
    return _financial_page(request, view)


@router.get("/runs", response_class=HTMLResponse)
async def runs_page(request: Request) -> HTMLResponse:
    return _render("runs.html", request, page_title="运行中心", active_nav="runs")


@router.get("/health", response_class=HTMLResponse)
async def health_page(request: Request) -> HTMLResponse:
    return _render("health.html", request, page_title="数据健康", active_nav="health")


@router.get("/candidates", response_class=HTMLResponse)
async def candidates_page(request: Request) -> HTMLResponse:
    return _render("candidates.html", request, page_title="候选池", active_nav="candidates")


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


@router.get("/methods", response_class=HTMLResponse)
async def methods_page(request: Request) -> HTMLResponse:
    return _render("methods.html", request, active_nav="methods")


@router.get("/supplemental-rules", response_class=HTMLResponse)
async def supplemental_rules_page(request: Request) -> HTMLResponse:
    """Management UI for user-authored supplemental rules and event reports."""
    return _render(
        "supplemental_rules.html", request,
        page_title="补充事项", active_nav="supplemental_rules",
    )
