"""P1-E2 data health page: shell, auth, nav, overview link and JS guards."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

STATIC = Path("src/investment_agent/web/static")
TEMPLATES = Path("src/investment_agent/web/templates")
DISCLAIMER = "研究辅助，非投资建议"
SECTIONS = (
    "warnings",
    "accumulation",
    "freshness",
    "collections",
    "universes",
    "industry",
    "backfill",
    "verification",
    "overdue",
)


def _login(client) -> None:
    response = client.post("/app/login", data={"api_key": "secret", "next": "/app"})
    assert response.status_code == 200


def _run_health(script: str) -> Any:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available")
    uri = (STATIC / "health.js").resolve().as_uri()
    source = f"const h = await import({json.dumps(uri)});\n{script}"
    result = subprocess.run(
        [node, "--input-type=module", "-e", source], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_health_page_requires_session(api_client) -> None:
    response = api_client.get("/app/health", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/app/login"


def test_health_page_shell(api_client) -> None:
    _login(api_client)
    response = api_client.get("/app/health")
    assert response.status_code == 200
    html = response.text
    for fragment in (
        'id="data-health"',
        'data-endpoint="/health/data"',
        'data-state="loading"',
        'href="/app/health" class="active" aria-current="page"',
        "数据健康",
        "<noscript>",
        DISCLAIMER,
        '<script type="module" src="/app/static/app.js">',
    ):
        assert fragment in html, fragment
    for section in SECTIONS:
        assert f'id="health-{section}"' in html, section
        assert f'data-health-section="{section}"' in html, section
    assert "<style" not in html and " style=" not in html
    assert "secret" not in html


def test_nav_links_health_on_every_page(api_client) -> None:
    _login(api_client)
    for path in ("/app", "/app/watchlist", "/app/runs", "/app/candidates", "/app/health"):
        assert 'href="/app/health"' in api_client.get(path).text, path


def test_static_health_module_is_served(api_client) -> None:
    response = api_client.get("/app/static/health.js")
    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"]


def test_health_js_renders_every_section_and_wires_the_overview() -> None:
    source = (STATIC / "health.js").read_text(encoding="utf-8")
    for section in SECTIONS:
        assert f"{section}:" in source or f"{section}: " in source, section
    for marker in ("<meter>", '"meter"', '"progress"', "textContent", '"/health/data"', '"details"'):
        assert marker in source, marker
    assert re.search(r"\.style\b", source) is None, "no inline styles from JS"
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "healthBucket" in app and "HEALTH_REGIONS" in app and "bootHealth()" in app
    assert "warning.link" in app


def test_health_formatters() -> None:
    out = _run_health(
        "console.log(JSON.stringify(["
        "h.percent(0.9967), h.percent(1), h.percent(null), h.percent(0.95, 0),"
        "h.localTime('2026-10-06T11:12:59Z'), h.localTime(null),"
        "h.missingSummary({quota: 2, failed: 1, pending: 0}),"
        "h.collectionTitle({kind: 'financial_snapshot', provider: 'akshare', part: 'summary', scope: 'CSI300'}),"
        "h.collectionTitle({kind: 'market_snapshot', provider: 'baostock', part: null, scope: 'CSI300'}),"
        "h.accumulationText({streak_weeks: 3, target_weeks: 8}),"
        "]))"
    )
    assert out == [
        "99.7%",
        "100%",
        "—",
        "95%",
        "2026-10-06 19:12",
        "—",
        "失败 1，额度耗尽 2",
        "财报快照（akshare · 按期汇总 · CSI300）",
        "日线快照（baostock · CSI300）",
        "快照积累 3 / 8 周",
    ]


def test_health_labels_cover_api_enums() -> None:
    from investment_agent.snapshots.collect import MissingKind
    from investment_agent.snapshots.health import COLLECTION_KINDS, WeekStatus

    labels = _run_health(
        "console.log(JSON.stringify({kinds: Object.keys(h.KIND_LABELS), missing: Object.keys(h.MISSING_KIND_LABELS),"
        " weeks: Object.keys(h.WEEK_STATUS_LABELS), stops: Object.keys(h.STOP_LABELS)}))"
    )
    assert set(labels["kinds"]) == set(COLLECTION_KINDS)
    assert set(labels["missing"]) == set(MissingKind.__args__)
    assert set(labels["weeks"]) == set(WeekStatus.__args__)
    assert set(labels["stops"]) == {"quota", "provider_unavailable"}
