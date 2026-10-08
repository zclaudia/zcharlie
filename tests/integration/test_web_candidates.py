"""P1-E1 candidates pages: shells, auth, JS guards and the API contract the JS uses."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests.unit._candidate_support import seed_promotable

KEY = {"X-API-Key": "secret"}
WEB = Path("src/investment_agent/web")
STATIC = WEB / "static"
TEMPLATES = WEB / "templates"
DISCLAIMER = "研究辅助，非投资建议"
TRADING_WORDS = ("买入", "卖出", "建仓", "加仓", "减仓", "清仓", "止损", "抄底", "满仓", "割肉")


def _login(client) -> None:
    response = client.post("/app/login", data={"api_key": "secret", "next": "/app"})
    assert response.status_code == 200


def _node() -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available")
    return node


def _run_format(script: str) -> Any:
    """Run ``script`` (an ES module importing ``./format.js`` as ``f``) and parse its JSON."""

    node = _node()
    source = f"const f = await import({json.dumps((STATIC / 'format.js').resolve().as_uri())});\n{script}"
    result = subprocess.run(
        [node, "--input-type=module", "-e", source], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


# ---- pages ---------------------------------------------------------------


def test_candidate_pages_require_session(api_client) -> None:
    for path in ("/app/candidates", "/app/candidates/1"):
        response = api_client.get(path, follow_redirects=False)
        assert response.status_code == 302, path
        assert response.headers["location"] == "/app/login"


def test_candidates_list_shell(api_client) -> None:
    _login(api_client)
    response = api_client.get("/app/candidates")
    assert response.status_code == 200
    html = response.text
    for fragment in (
        'id="candidates-list"',
        'data-endpoint="/candidates"',
        'id="candidates-status-filter"',
        'id="candidates-template-filter"',
        'id="candidate-nominate-form"',
        'name="template_id"',
        'aria-current="page"',
        "未校准",
        'data-state="loading"',
        "<noscript>",
        DISCLAIMER,
        "/app/static/app.js",
    ):
        assert fragment in html, fragment
    for status in ("open", "promoted", "dismissed", "expired"):
        assert f'value="{status}"' in html


def test_candidate_detail_shell(api_client) -> None:
    _login(api_client)
    html = api_client.get("/app/candidates/42").text
    for fragment in (
        'id="candidate-detail"',
        'data-candidate-id="42"',
        'data-endpoint="/candidates/42"',
        'id="candidate-reasons"',
        'id="candidate-inspector"',
        'id="candidate-rule"',
        'id="candidate-actions"',
        'id="candidate-verification"',
        'id="candidate-decisions"',
        "未校准",
        "<noscript>",
        DISCLAIMER,
    ):
        assert fragment in html, fragment


@pytest.mark.parametrize("bad", ["0", "abc", "1.5", "-3", "12345678901"])
def test_candidate_detail_rejects_invalid_id(api_client, bad: str) -> None:
    _login(api_client)
    response = api_client.get(f"/app/candidates/{bad}")
    assert response.status_code == 400
    assert "不是有效的候选编号" in response.text
    assert DISCLAIMER in response.text


def test_nav_links_candidates_on_every_page(api_client) -> None:
    _login(api_client)
    for path in (
        "/app", "/app/watchlist", "/app/runs", "/app/candidates", "/app/candidates/1",
        "/app/settings",
    ):
        assert 'href="/app/candidates"' in api_client.get(path).text, path


def test_pages_load_only_same_origin_module_scripts(api_client) -> None:
    _login(api_client)
    for path in (
        "/app", "/app/candidates", "/app/candidates/1", "/app/health", "/app/settings",
        "/app/login",
    ):
        html = api_client.get(path).text
        assert "<style" not in html and " style=" not in html, path
        sources = re.findall(r"<script\b([^>]*)>", html)
        assert sources, path
        for attrs in sources:
            match = re.search(r'src="([^"]+)"', attrs)
            assert match is not None, (path, attrs)
            assert match.group(1).startswith("/app/static/"), (path, match.group(1))
        for href in re.findall(r'<link\b[^>]*href="([^"]+)"', html):
            assert href.startswith("/app/static/"), (path, href)


# ---- static JS guards ----------------------------------------------------


def _js_sources() -> dict[str, str]:
    return {path.name: path.read_text(encoding="utf-8") for path in sorted(STATIC.glob("*.js"))}


def test_js_never_writes_html_strings() -> None:
    for name, source in _js_sources().items():
        for pattern in (r"\.innerHTML\b", r"\.outerHTML\b", r"insertAdjacentHTML", r"document\.write"):
            assert re.search(pattern, source) is None, (name, pattern)


def test_js_imports_are_relative_and_exist() -> None:
    for name, source in _js_sources().items():
        for spec in re.findall(r'from\s+"([^"]+)"', source):
            assert spec.startswith("./") and spec.endswith(".js"), (name, spec)
            assert (STATIC / spec[2:]).is_file(), (name, spec)
        assert "http://" not in source and "https://" not in source, name


def test_ui_has_no_trading_language_or_scores() -> None:
    texts = {**_js_sources()}
    texts |= {path.name: path.read_text(encoding="utf-8") for path in TEMPLATES.glob("*.html")}
    for name, text in texts.items():
        for word in TRADING_WORDS:
            assert word not in text, (name, word)
    for name in ("candidates.js", "format.js", "health.js"):
        source = texts[name]
        assert "score" not in source and "strength" not in source, name


def test_modules_link_without_a_dom() -> None:
    node = _node()
    uri = (STATIC / "candidates.js").resolve().as_uri()
    result = subprocess.run(
        [node, "--input-type=module", "-e", f"await import({json.dumps(uri)});"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_nominate_form_normalizes_tickers() -> None:
    node = _node()
    uri = (STATIC / "candidates.js").resolve().as_uri()
    script = (
        f"const m = await import({json.dumps(uri)});\n"
        "console.log(JSON.stringify(['600900', '000001', '300750', ' 830799 ', '600900.sh',"
        " '600900.SH', '600900.HK', '12345', '700000', ''].map(m.normalizeTicker)));"
    )
    result = subprocess.run(
        [node, "--input-type=module", "-e", script], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [
        "600900.SH", "000001.SZ", "300750.SZ", "830799.BJ", "600900.SH",
        "600900.SH", None, None, None, None,
    ]


# ---- formatter -------------------------------------------------------------


def test_reason_formatter_units_and_states() -> None:
    reasons = [
        {"factor": "roe", "op": ">=", "threshold": 0.08, "actual": 0.12, "unit": "fraction", "outcome": "met"},
        {"factor": "pe_ttm", "op": "<=", "threshold": 30, "actual": 12.34, "unit": "times", "outcome": "met"},
        {"factor": "market_cap", "op": ">=", "threshold": 2e10, "actual": 1.5e11, "unit": "CNY", "outcome": "met"},
        {"factor": "debt_ratio", "op": "<", "threshold": 0.6, "actual": None, "unit": "fraction", "outcome": "missing"},
        {"factor": "gross_margin", "op": ">=", "threshold": 0.3, "actual": None, "unit": "fraction", "outcome": "not_applicable"},
        {
            "factor": "pe_percentile_5y",
            "op": "between",
            "threshold": [0.1, 0.3],
            "actual": 0.2,
            "unit": "fraction (0,1]",
            "outcome": "met",
        },
        {"factor": "unknown_factor", "op": ">", "threshold": 1.5, "actual": 2, "outcome": "met"},
        {"detail": "industry_group_not_applicable"},
    ]
    out = _run_format(f"console.log(JSON.stringify({json.dumps(reasons)}.map(f.formatReason)));")
    assert out == [
        "ROE 12.0% ≥ 8%",
        "PE(TTM) 12.3 倍 ≤ 30 倍",
        "总市值 1500.0 亿元 ≥ 200 亿元",
        "资产负债率 缺失（条件 < 60%）",
        "毛利率 不适用（条件跳过：≥ 30%）",
        "PE 5 年分位 20.0%（介于 10%–30%）",
        "unknown_factor 2 > 1.5",
        "industry_group_not_applicable",
    ]


def test_condition_formatter_resolves_params_and_labels() -> None:
    out = _run_format(
        """
const cond = f.formatCondition(
  {factor: "pe_percentile_5y", op: "<=", value: "$pe_pct_max"},
  {pe_pct_max: 0.25},
  {pe_percentile_5y: "fraction (0,1]"},
);
const between = f.formatCondition(
  {factor: "roe", op: "between", value: ["$lo", 0.4]}, {lo: 0.1}, {roe: "fraction"},
);
console.log(JSON.stringify({
  cond, between,
  zones: ["buy_zone", "drop", "add", "trim", "exit_review"].map((z) => f.label(f.ZONE_LABELS, z)),
  drafts: ["none", "pending", "ready", "failed"].map((s) => f.label(f.DRAFT_STATE_LABELS, s)),
  issues: f.describeIssues({message: "rule validation failed",
    issues: [{code: "param_out_of_range", path: "params.pe_pct_max", message: "0.9 > 0.6"}]}),
  fastapi: f.describeIssues([{loc: ["body", "reason"], msg: "field required"}]),
  plain: f.parseRationale("valuation in the lower quarter"),
  json: f.parseRationale(JSON.stringify({params: {pe_pct_max: {choice: "adjusted", reason: "r",
    cited_factors: ["pe_percentile_5y"]}}, overall_rationale: "o", watch_points: ["w"]})),
}));
"""
    )
    assert out["cond"] == {"text": "PE 5 年分位 ≤ 25%", "params": ["pe_pct_max"]}
    assert out["between"]["text"] == "ROE 介于 10%–40%"
    assert out["zones"] == ["可研究区间", "移出候选", "持仓区间 add", "持仓区间 trim", "持仓区间 复核"]
    assert out["drafts"] == ["待起草", "起草中", "草稿就绪", "起草失败"]
    assert out["issues"] == ["params.pe_pct_max：0.9 > 0.6"]
    assert out["fastapi"] == ["body.reason field required"]
    assert out["plain"]["text"] == "valuation in the lower quarter"
    assert out["json"]["overall"] == "o" and out["json"]["watch"] == ["w"]
    assert out["json"]["params"]["pe_pct_max"]["cited_factors"] == ["pe_percentile_5y"]


# ---- API contract consumed by candidates.js --------------------------------


async def test_candidate_api_contract_for_the_page(api_client) -> None:
    ids = await seed_promotable()
    page = api_client.get("/candidates?status=open&limit=100", headers=KEY).json()
    [item] = page["items"]
    for key in (
        "id", "listing_id", "ticker", "name", "status", "opened_at", "expires_at",
        "template_refs", "calibration_status", "latest_screen", "draft_state", "sources",
    ):
        assert key in item, key
    reasons = item["latest_screen"]["reasons"]
    assert {reason["unit"] for reason in reasons} == {"fraction"}
    assert item["latest_screen"]["context"]["industry_group"] == "consumer"

    detail = api_client.get(f"/candidates/{ids['candidate_id']}", headers=KEY).json()
    rule = detail["draft_rule_detail"]
    assert set(rule["zones"]) == {"buy_zone", "drop"}
    assert rule["zones"]["buy_zone"] == {
        "all": [{"factor": "pe_percentile_5y", "op": "<=", "value": "$pe_pct_max"}]
    }
    assert rule["factor_units"] == {"pe_percentile_5y": "fraction (0,1]", "roe": "fraction"}
    assert rule["template_params"] == {"roe_min": 0.1, "pe_pct_max": 0.3}
    assert rule["effective_params"]["pe_pct_max"] == 0.25
    for key in ("param_ranges", "draft_rationale", "status", "version", "template_ref"):
        assert key in rule, key
    for key in ("kind", "actor", "reason_text", "at", "rule_ref"):
        assert key in detail["decisions"][0], key

    # The reasons of the real API response, formatted by the page's formatter.
    formatted = _run_format(
        f"console.log(JSON.stringify({json.dumps(reasons)}.map(f.formatReason)));"
    )
    assert formatted == ["ROE 31.0% ≥ 10%", "毛利率 91.0% ≥ 50%"]
    conditions = _run_format(
        f"""
const rule = {json.dumps(rule)};
console.log(JSON.stringify(Object.values(rule.zones).flatMap((g) => (g.all || g.any)
  .map((c) => f.formatCondition(c, rule.effective_params, rule.factor_units).text))));
"""
    )
    assert conditions == ["PE 5 年分位 ≤ 25%", "ROE < 5%"]


async def test_dashboard_candidates_bucket(api_client) -> None:
    empty = api_client.get("/dashboard/today", headers=KEY).json()["candidates"]
    assert empty == {
        "open": 0, "drafts_ready": 0, "drafting": 0, "draft_failed": 0, "verification_pending": 0,
    }
    await seed_promotable()
    counts = api_client.get("/dashboard/today", headers=KEY).json()["candidates"]
    assert counts["open"] == 1 and counts["drafts_ready"] == 1
    assert counts["verification_pending"] == 0
