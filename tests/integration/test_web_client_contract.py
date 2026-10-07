"""Browser API client contract tests (work package B3).

The server-rendered pages declare data regions the ES-module client fills;
these tests pin that contract (region ids, endpoints, four-state hooks) and
keep the shipped JS modules parseable.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

WEB_STATIC = Path("src/investment_agent/web/static")


def _login(client):
    response = client.post("/app/login", data={"api_key": "secret", "next": "/app"})
    assert response.status_code == 200


def test_overview_page_declares_dashboard_region(api_client):
    _login(api_client)
    html = api_client.get("/app").text
    assert 'id="dashboard-today"' in html
    assert 'data-endpoint="/dashboard/today"' in html
    assert "今日无待处理项" in html


def test_watchlist_page_declares_list_add_and_refresh_hooks(api_client):
    _login(api_client)
    html = api_client.get("/app/watchlist").text
    assert 'id="watchlist-list"' in html
    assert 'data-endpoint="/watchlist"' in html
    assert 'id="watchlist-add-form"' in html
    assert 'id="watchlist-refresh-all"' in html
    assert 'data-endpoint="/watchlist/refresh"' in html


def test_stock_page_declares_history_and_change_regions(api_client):
    _login(api_client)
    html = api_client.get("/app/stocks/600406.SH").text
    assert 'id="stock-history"' in html
    assert 'data-endpoint="/research/stocks/600406.SH/history"' in html


def test_runs_page_declares_status_filter_and_list_region(api_client):
    _login(api_client)
    html = api_client.get("/app/runs").text
    assert 'id="runs-list"' in html
    assert 'data-endpoint="/research/runs"' in html
    assert 'id="runs-status-filter"' in html
    for status in ("queued", "running", "success", "partial", "failed"):
        assert f'value="{status}"' in html


def test_every_page_carries_states_disclaimer_and_noscript(api_client):
    _login(api_client)
    pages = [
        "/app",
        "/app/watchlist",
        "/app/runs",
        "/app/stocks/600406.SH",
        "/app/candidates",
        "/app/candidates/1",
    ]
    for path in pages:
        html = api_client.get(path).text
        assert "<noscript>" in html, path
        assert "研究辅助，非投资建议" in html, path
        assert 'data-state="loading"' in html, path


def test_api_client_handles_states_idempotency_and_errors():
    source = (WEB_STATIC / "api.js").read_text(encoding="utf-8")
    for marker in (
        "Idempotency-Key",
        "renderRegion",
        '"loading"',
        '"empty"',
        '"error"',
        '"ready"',
        "401",
        "409",
        "422",
        "503",
        "AbortController",
        "credentials",
    ):
        assert marker in source, marker


def test_renderer_consumes_only_versioned_read_models():
    source = (WEB_STATIC / "app.js").read_text(encoding="utf-8")
    # Dashboard and per-stock history endpoints come from the templates'
    # data-endpoint attributes (see page contract tests above); the renderer
    # itself only references these endpoints directly.
    for endpoint in ("/watchlist", "/watchlist/refresh", "/research/runs"):
        assert endpoint in source, endpoint


@pytest.mark.parametrize("module", ["api.js", "app.js", "dom.js", "format.js", "candidates.js"])
def test_shipped_js_modules_parse(module: str):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available")
        return
    result = subprocess.run(
        [node, "--check", str(WEB_STATIC / module)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
