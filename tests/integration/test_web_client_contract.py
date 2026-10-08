"""Browser API client contract tests (work package B3).

The server-rendered pages declare data regions the ES-module client fills;
these tests pin that contract (region ids, endpoints, four-state hooks) and
keep the shipped JS modules parseable.
"""

import json
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
    # Sparkline host (plan 03 / B3): app.js fills it from /market/{ticker}/bars.
    assert 'id="stock-spark"' in html
    assert 'data-ticker="600406.SH"' in html


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
        "/app/health",
        "/app/settings",
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


def test_sparkline_component_and_mounts():
    """ui.js owns the SVG sparkline; app.js and candidates.js feed it from /market/."""

    ui = (WEB_STATIC / "ui.js").read_text(encoding="utf-8")
    assert "export function sparkline" in ui
    assert "export async function fillSparklines" in ui
    for marker in ('viewBox: `0 0 ${SPARK_W} ${SPARK_H}`', 'preserveAspectRatio: "none"', "non-scaling-stroke", "spark-band", "spark-last", "spark-empty"):
        assert marker in ui, marker
    for module in ("app.js", "candidates.js"):
        source = (WEB_STATIC / module).read_text(encoding="utf-8")
        assert "/market/" in source, module
        assert "fillSparklines" in source, module


def test_sparkline_builds_expected_svg_structure():
    """Render ``sparkline`` in node against a minimal fake DOM and inspect the tree."""

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available")
        return
    uri = (WEB_STATIC / "ui.js").resolve().as_uri()
    script = f"const MODULE_URI = {json.dumps(uri)};\n" + """
    class Node {
      constructor(tag) { this.tag = tag; this.attrs = {}; this.children = []; this.textContent = undefined; }
      setAttribute(k, v) { this.attrs[k] = v; }
      appendChild(c) { this.children.push(c); return c; }
    }
    globalThis.document = {
      createElement: (tag) => new Node(tag),
      createElementNS: (_ns, tag) => new Node(tag),
    };
    const { sparkline } = await import(MODULE_URI);
    const bars = [{trade_date: "2026-01-01", close: 10}, {trade_date: "2026-01-02", close: 12}, {trade_date: "2026-01-03", close: 11}];
    const up = sparkline(bars, { bands: [{ from: 10.5, to: 11.5, kind: "watch" }] });
    const down = sparkline([3, 2, 1], { width: 200, height: 80 });
    const empty = sparkline([{ close: 1 }]);
    console.log(JSON.stringify({ up, down, empty }));
    """
    result = subprocess.run(
        [node, "--input-type=module", "-e", script], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    up = out["up"]
    assert up["tag"] == "svg"
    assert up["attrs"]["class"] == "spark spark-up"
    assert up["attrs"]["viewBox"] == "0 0 100 30"
    assert up["attrs"]["preserveAspectRatio"] == "none"
    assert up["attrs"]["aria-hidden"] == "true"
    assert (up["attrs"]["width"], up["attrs"]["height"]) == ("120", "32")
    band, line, last = up["children"]
    assert band["tag"] == "rect" and band["attrs"]["class"] == "spark-band spark-band-watch"
    assert line["tag"] == "polyline"
    assert line["attrs"]["class"] == "spark-line"
    assert line["attrs"]["vector-effect"] == "non-scaling-stroke"
    points = line["attrs"]["points"].split(" ")
    assert len(points) == 3
    assert points[0] == "0.00,28.50"  # min close sits at the bottom (minus padding)
    assert points[1] == "50.00,1.50"  # max close at the top
    assert points[-1].startswith("100.00,")
    assert last["tag"] == "circle" and last["attrs"]["class"] == "spark-last"
    assert last["attrs"]["cx"] == "100.00" and last["attrs"]["cy"] == points[-1].split(",")[1]
    down = out["down"]
    assert down["attrs"]["class"] == "spark spark-down"
    assert (down["attrs"]["width"], down["attrs"]["height"]) == ("200", "80")
    assert [child["tag"] for child in down["children"]] == ["polyline", "circle"]
    empty = out["empty"]
    assert empty["tag"] == "span" and empty["attrs"]["class"] == "spark spark-empty"


@pytest.mark.parametrize(
    "module",
    ["api.js", "app.js", "dom.js", "format.js", "research_format.js", "ui.js", "candidates.js", "health.js", "settings.js"],
)
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
