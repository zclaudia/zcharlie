"""Same-origin web UI integration tests (session, shell, origin checks)."""

import time

from investment_agent.web.session import (
    SESSION_COOKIE_NAME,
    create_session_token,
)
from investment_agent.web.static_assets import static_url


def _login(client, api_key="secret", **kwargs):
    return client.post(
        "/app/login",
        data={"api_key": api_key, "next": "/app"},
        **kwargs,
    )


def test_app_root_redirects_unauthenticated_to_login(api_client):
    response = api_client.get("/app")
    assert response.status_code == 200
    assert response.url.path == "/app/login"


def test_bare_root_redirects_to_app(api_client):
    response = api_client.get("/", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/app"


def test_login_page_renders_disclaimer_and_password_form(api_client):
    response = api_client.get("/app/login")
    assert response.status_code == 200
    html = response.text
    assert 'type="password"' in html
    assert "研究辅助，非投资建议" in html
    assert "secret" not in html


def test_login_rejects_wrong_key_without_cookie(api_client):
    response = _login(api_client, api_key="wrong")
    assert response.status_code == 401
    assert "API Key 不正确" in response.text
    assert not response.cookies.get(SESSION_COOKIE_NAME)


def test_login_sets_httponly_strict_cookie_and_redirects(api_client):
    response = api_client.post(
        "/app/login",
        data={"api_key": "secret", "next": "/app"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/app"
    set_cookie = response.headers.get("set-cookie", "")
    assert SESSION_COOKIE_NAME in set_cookie
    assert "HttpOnly" in set_cookie
    assert "samesite=strict" in set_cookie.lower()
    cookie_value = response.cookies.get(SESSION_COOKIE_NAME, "")
    assert cookie_value and "secret" not in cookie_value


def test_authenticated_shell_renders_nav_and_disclaimer(api_client):
    _login(api_client)
    response = api_client.get("/app")
    assert response.status_code == 200
    html = response.text
    for fragment in (
        "/app/watchlist",
        "/app/runs",
        "今日概览",
        "研究辅助，非投资建议",
        static_url("app.css"),
        static_url("app.js"),
    ):
        assert fragment in html


def test_static_assets_served_from_app(api_client):
    assert api_client.get("/app/static/app.css").status_code == 200
    assert api_client.get("/app/static/api.js").status_code == 200
    assert api_client.get("/app/static/app.js").status_code == 200


def test_tampered_cookie_redirects_to_login(api_client):
    _login(api_client)
    cookie = api_client.cookies.get(SESSION_COOKIE_NAME)
    api_client.cookies.set(SESSION_COOKIE_NAME, cookie + "x")
    response = api_client.get("/app")
    assert response.url.path == "/app/login"


def test_expired_cookie_redirects_to_login(api_client):
    expired = create_session_token(api_key="secret", ttl_minutes=1, now=time.time() - 3600)
    api_client.cookies.set(SESSION_COOKIE_NAME, expired)
    response = api_client.get("/app")
    assert response.url.path == "/app/login"


def test_logout_clears_session(api_client):
    _login(api_client)
    response = api_client.post("/app/logout")
    assert response.status_code == 200
    response = api_client.get("/app", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/app/login"


def test_cross_origin_login_rejected(api_client):
    response = _login(
        api_client,
        headers={"Origin": "https://evil.example"},
        follow_redirects=False,
    )
    assert response.status_code == 403


def test_cross_origin_logout_rejected(api_client):
    _login(api_client)
    response = api_client.post(
        "/app/logout",
        headers={"Origin": "https://evil.example"},
    )
    assert response.status_code == 403


def test_stock_page_rejects_invalid_ticker(api_client):
    _login(api_client)
    response = api_client.get("/app/stocks/NOT-A-TICKER")
    assert response.status_code == 400
    assert "不是有效的 A 股代码" in response.text


def test_stock_page_renders_for_valid_ticker(api_client):
    _login(api_client)
    response = api_client.get("/app/stocks/600406.SH")
    assert response.status_code == 200
    assert "600406.SH" in response.text
    assert "研究辅助，非投资建议" in response.text


def test_api_key_never_appears_in_web_pages(api_client):
    _login(api_client)
    for path in (
        "/app",
        "/app/watchlist",
        "/app/runs",
        "/app/stocks/600406.SH",
        "/app/candidates",
        "/app/candidates/1",
        "/app/settings",
        "/app/login",
    ):
        response = api_client.get(path)
        assert "secret" not in response.text, path


# ---- Android embedded mode (X-API-Key) -------------------------------------


def _embedded(client, path="/app", key="secret"):
    return client.get(path, headers={"X-API-Key": key}, follow_redirects=False)


def test_embedded_api_key_renders_without_logout(api_client):
    response = _embedded(api_client)
    assert response.status_code == 200
    html = response.text
    assert 'data-embedded="android"' in html
    assert "/app/logout" not in html
    assert "设备与配对" in html
    assert "tauri.localhost/#pairing" in html


def test_cookie_session_stays_unembedded(api_client):
    _login(api_client)
    html = api_client.get("/app").text
    assert "data-embedded" not in html
    assert "/app/logout" in html


def test_embedded_login_page_redirects_to_app(api_client):
    response = _embedded(api_client, "/app/login")
    assert response.status_code == 302
    assert response.headers["location"] == "/app"


def test_embedded_wrong_key_is_401_not_login_redirect(api_client):
    response = _embedded(api_client, key="wrong")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "ApiKey"
    assert response.text == "Missing or invalid X-API-Key"


def test_embedded_key_failures_throttled(api_client):
    for _ in range(5):
        assert _embedded(api_client, key="wrong").status_code == 401
    blocked = _embedded(api_client)
    assert blocked.status_code == 429
    assert int(blocked.headers["retry-after"]) > 0
