"""Security hardening: throttling, CSRF origin rules, session revocation,
cookie flags, security headers and prod-only docs disabling."""

from fastapi.testclient import TestClient

from investment_agent.api.throttle import AuthFailureThrottle
from investment_agent.web.session import SESSION_COOKIE_NAME

STRONG_KEY = "k" * 40


def _login(client, api_key="secret", **kwargs):
    return client.post(
        "/app/login",
        data={"api_key": api_key, "next": "/app"},
        follow_redirects=False,
        **kwargs,
    )


# ---- brute-force throttling ------------------------------------------------


def test_login_throttled_after_five_failures(api_client):
    for _ in range(5):
        assert _login(api_client, api_key="wrong").status_code == 401
    response = _login(api_client, api_key="wrong")
    assert response.status_code == 429
    assert int(response.headers["retry-after"]) > 0
    # Even the right key is refused while throttled.
    blocked = _login(api_client)
    assert blocked.status_code == 429
    assert not blocked.cookies.get(SESSION_COOKIE_NAME)


def test_login_throttle_resettable(api_client):
    for _ in range(5):
        _login(api_client, api_key="wrong")
    api_client.app.state.auth_throttle.reset()
    assert _login(api_client).status_code == 303


def test_api_key_failures_throttled(api_client):
    for _ in range(5):
        r = api_client.get("/admin/llm-calls", headers={"X-API-Key": "nope"})
        assert r.status_code == 401
    r = api_client.get("/admin/llm-calls", headers={"X-API-Key": "secret"})
    assert r.status_code == 429
    assert "retry-after" in r.headers
    # Session-or-key routes share the same counter.
    r = api_client.get("/watchlist", headers={"X-API-Key": "nope"})
    assert r.status_code == 429


def test_missing_api_key_is_not_counted(api_client):
    for _ in range(10):
        assert api_client.get("/admin/llm-calls").status_code == 401
    assert api_client.get("/admin/llm-calls", headers={"X-API-Key": "secret"}).status_code == 200


def test_throttle_window_expires():
    now = [0.0]
    throttle = AuthFailureThrottle(max_failures=2, window_seconds=10, clock=lambda: now[0])
    throttle.record_failure("ip")
    throttle.record_failure("ip")
    assert throttle.retry_after("ip") == 10
    assert throttle.retry_after("other") is None
    now[0] = 10.5
    assert throttle.retry_after("ip") is None


def test_non_ascii_api_key_rejected_not_crashing(api_client):
    r = api_client.get("/admin/llm-calls", headers={"X-API-Key": "密钥".encode()})
    assert r.status_code == 401
    assert _login(api_client, api_key="密钥").status_code == 401


def test_non_ascii_session_cookie_fails_verification_not_crashing(api_client):
    """P2-9: a raw client can put non-ASCII in the session cookie; verification
    must fail closed (401) on every request, never raise a 500."""

    from investment_agent.web.session import verify_session_token

    assert verify_session_token("v2.1.2.nønce.签名", api_key="secret") is False
    assert verify_session_token("v2.一.二.三.四", api_key="secret") is False
    # A real token still verifies, including through the byte comparison.
    token = api_client.post(  # nosec: straightforward login below
        "/app/login", data={"api_key": "secret", "next": "/app"}, follow_redirects=False
    )
    assert token.status_code == 303
    genuine = api_client.cookies[SESSION_COOKIE_NAME]
    assert verify_session_token(genuine, api_key="secret") is True
    api_client.cookies.clear()
    # Raw clients put raw bytes on the wire; ASGI decodes them as latin-1, so
    # the app sees a non-ASCII str cookie exactly like the forged vector.
    forged = f"{SESSION_COOKIE_NAME}=v2.1.2.nonce.中文伪造签名".encode()
    assert api_client.get("/dashboard/today", headers={"Cookie": forged}).status_code == 401
    genuine_bytes = f"{SESSION_COOKIE_NAME}={genuine}".encode()
    assert api_client.get("/dashboard/today", headers={"Cookie": genuine_bytes}).status_code == 200


# ---- CSRF origin rules -----------------------------------------------------


def _session_client(api_client):
    assert _login(api_client).status_code == 303
    return api_client


def test_cookie_post_without_origin_or_marker_rejected(api_client):
    client = _session_client(api_client)
    r = client.post("/watchlist", json={"ticker": "600519.SH"})
    assert r.status_code == 403


def test_cookie_post_with_null_origin_rejected(api_client):
    client = _session_client(api_client)
    r = client.post(
        "/watchlist",
        json={"ticker": "600519.SH"},
        headers={"Origin": "null", "X-Requested-With": "fetch"},
    )
    assert r.status_code == 403


def test_cookie_post_with_cross_origin_and_marker_rejected(api_client):
    client = _session_client(api_client)
    r = client.delete(
        "/watchlist/1",
        headers={"Origin": "https://evil.example", "X-Requested-With": "fetch"},
    )
    assert r.status_code == 403


def test_cookie_delete_with_fetch_marker_passes_csrf(api_client):
    client = _session_client(api_client)
    r = client.request(
        "DELETE",
        "/watchlist/999",
        json={"reason": "csrf probe"},
        headers={"X-Requested-With": "fetch"},
    )
    assert r.status_code == 404


def test_cookie_delete_with_matching_origin_passes_csrf(api_client):
    client = _session_client(api_client)
    r = client.request(
        "DELETE",
        "/watchlist/999",
        json={"reason": "csrf probe"},
        headers={"Origin": "http://testserver"},
    )
    assert r.status_code == 404


def test_public_origin_setting_allows_proxy_origin(api_client, monkeypatch):
    monkeypatch.setenv("WEB_PUBLIC_ORIGIN", "https://research.example.com")
    client = _session_client(api_client)
    r = client.request(
        "DELETE",
        "/watchlist/999",
        json={"reason": "csrf probe"},
        headers={"Origin": "https://research.example.com"},
    )
    assert r.status_code == 404
    r = client.delete("/watchlist/999", headers={"Origin": "https://other.example.com"})
    assert r.status_code == 403


def test_api_key_requests_skip_origin_check(api_client):
    r = api_client.request(
        "DELETE",
        "/watchlist/999",
        json={"reason": "csrf probe"},
        headers={"X-API-Key": "secret", "Origin": "https://evil.example"},
    )
    assert r.status_code == 404


def test_login_with_null_origin_rejected(api_client):
    r = _login(api_client, headers={"Origin": "null"})
    assert r.status_code == 403


# ---- sessions --------------------------------------------------------------


def test_logout_revokes_old_session_server_side(api_client):
    _login(api_client)
    stolen = api_client.cookies.get(SESSION_COOKIE_NAME)
    assert api_client.post("/app/logout", follow_redirects=False).status_code == 303
    api_client.cookies.set(SESSION_COOKIE_NAME, stolen)
    r = api_client.get("/app", follow_redirects=False)
    assert r.status_code == 302
    assert api_client.get("/dashboard/today").status_code == 401
    # A fresh login works again.
    api_client.cookies.clear()
    assert _login(api_client).status_code == 303
    assert api_client.get("/app", follow_redirects=False).status_code == 200


def test_logout_is_post_only(api_client):
    _login(api_client)
    assert api_client.get("/app/logout", follow_redirects=False).status_code == 405
    assert api_client.get("/app", follow_redirects=False).status_code == 200


def test_anonymous_logout_does_not_revoke(api_client, tmp_path):
    api_client.post("/app/logout")
    assert not (tmp_path / "web_session_not_before").exists()


def test_cookie_secure_flag_follows_setting(api_client, monkeypatch):
    insecure = _login(api_client).headers["set-cookie"].lower()
    assert "secure" not in insecure.replace("samesite", "")
    monkeypatch.setenv("WEB_COOKIE_SECURE", "true")
    secure = _login(api_client).headers["set-cookie"].lower()
    assert "; secure" in secure


def test_prod_https_public_origin_forces_secure_cookie(monkeypatch):
    """P3g: prod behind an https WEB_PUBLIC_ORIGIN cannot emit non-Secure
    session cookies — the setting is forced (with a warning), local dev and
    non-https deployments keep their explicit configuration."""

    from investment_agent.config import Settings

    forced = Settings(
        app_env="prod",
        api_key=STRONG_KEY,
        web_public_origin="https://research.example.com",
        web_cookie_secure=False,
    )
    assert forced.web_cookie_secure is True
    # An explicit True is kept, and dev / non-https deployments are untouched.
    assert Settings(
        app_env="prod", api_key=STRONG_KEY, web_public_origin="https://research.example.com",
        web_cookie_secure=True,
    ).web_cookie_secure is True
    assert Settings(app_env="prod", api_key=STRONG_KEY, web_cookie_secure=False).web_cookie_secure is False
    assert Settings(
        api_key="secret", web_cookie_secure=False, web_public_origin="https://research.example.com"
    ).web_cookie_secure is False


def test_prod_https_public_origin_sets_secure_cookie_on_login(monkeypatch):
    from fastapi.testclient import TestClient

    from investment_agent.api.app import create_app

    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("API_KEY", STRONG_KEY)
    monkeypatch.setenv("WEB_PUBLIC_ORIGIN", "https://research.example.com")
    client = TestClient(create_app())
    response = _login(client, api_key=STRONG_KEY)
    assert response.status_code == 303
    assert "; secure" in response.headers["set-cookie"].lower()


def test_web_session_secret_changes_signing_key(api_client, monkeypatch):
    _login(api_client)
    assert api_client.get("/app", follow_redirects=False).status_code == 200
    monkeypatch.setenv("WEB_SESSION_SECRET", "a-dedicated-session-signing-secret")
    # Sessions signed with the API-key-derived secret are no longer valid.
    assert api_client.get("/app", follow_redirects=False).status_code == 302
    _login(api_client)
    assert api_client.get("/app", follow_redirects=False).status_code == 200


def test_request_body_limit_answers_413_with_security_headers(api_client):
    """P3h: bodies declared over the global cap are refused before any handler
    reads them (no reverse-proxy memory amplification); the guard runs inside
    the security-headers middleware, and legitimate requests are untouched."""

    from investment_agent.api.app import MAX_REQUEST_BODY_BYTES

    key = {"X-API-Key": "secret"}
    oversized = api_client.post(
        "/agent/conversations",
        headers={**key, "content-type": "application/json"},
        content=b"0" * (MAX_REQUEST_BODY_BYTES + 1),
    )
    assert oversized.status_code == 413
    assert oversized.json() == {"detail": "request body too large"}
    assert oversized.headers["x-content-type-options"] == "nosniff"
    # Just under the cap the request reaches the normal (malformed JSON) path.
    under = api_client.post(
        "/agent/conversations",
        headers={**key, "content-type": "application/json"},
        content=b"[" * (MAX_REQUEST_BODY_BYTES - 1),
    )
    assert under.status_code in (400, 422)


# ---- headers / docs --------------------------------------------------------


def test_web_pages_send_security_headers(api_client):
    r = api_client.get("/app/login")
    csp = r.headers["content-security-policy"]
    assert "default-src 'self'" in csp
    assert "script-src 'self'" in csp
    assert "style-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "same-origin"


def test_static_ui_files_are_revalidated_not_heuristically_cached(api_client):
    # Unversioned module URLs: without this an upgrade keeps running stale JS.
    r = api_client.get("/app/static/app.js")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-cache"
    assert r.headers.get("etag")
    again = api_client.get("/app/static/app.js", headers={"If-None-Match": r.headers["etag"]})
    assert again.status_code == 304
    assert "cache-control" not in api_client.get("/healthz").headers


def test_templates_have_no_inline_script_or_style(api_client):
    _login(api_client)
    for path in (
        "/app",
        "/app/watchlist",
        "/app/runs",
        "/app/stocks/600519.SH",
        "/app/candidates",
        "/app/candidates/1",
        "/app/settings",
        "/app/login",
    ):
        html = api_client.get(path).text
        assert "<style" not in html, path
        assert " style=" not in html, path
        for chunk in html.split("<script")[1:]:
            assert "src=" in chunk.split(">")[0], path


def test_docs_available_in_dev(api_client):
    assert api_client.get("/openapi.json").status_code == 200
    assert api_client.get("/docs").status_code == 200


def test_docs_disabled_in_prod(api_client, monkeypatch):
    from investment_agent.api.app import create_app

    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("API_KEY", STRONG_KEY)
    client = TestClient(create_app())
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404, path
    assert client.get("/healthz").status_code == 200
