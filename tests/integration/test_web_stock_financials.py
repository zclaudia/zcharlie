"""The stock page's HK/US financial section (plan 11).

The page renders plan 09's read model server-side, so these tests pin the real
HTML the browser receives: the separated quote/report currencies, the material's
*real* fiscal years (never a calendar-year assumption), the two coverages counted
separately, the "missing is never 0" rule, the business status wording, and the
client refresh contract (a queued task is never presented as updated data).

The material is plan 08's checked fixture, stored through plan 05's
``MaterialStore`` (the same path 09's API tests use); the fixture is read only
and never modified. A-share rendering is asserted unchanged.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from freezegun import freeze_time

from investment_agent.materials.contracts import DatePrecision
from investment_agent.storage.database import session_scope
from investment_agent.storage.repositories.listing import ListingRepository
from tests.integration.test_api_research_materials import (
    APPLE,
    HSBC,
    MISSING_REASON,
    PILOTS,
    fixture_facts,
    seed_listings,
    store_apple_material,
    store_hsbc_material,
    store_material,
)

CLP = "clp-holdings"
"""The plan's named HK sample (``00002.HK``); 09's module has no constant for it."""

TODAY = "2026-10-12"
"""The frozen current date of the date-sensitive assertions (real dates only)."""

STATIC = Path("src/investment_agent/web/static")

FORBIDDEN_UPSTREAM = ("eastmoney", "sec.gov", "cik", "accession")
TRADING_WORDS = (
    "买入",
    "卖出",
    "建仓",
    "加仓",
    "减仓",
    "清仓",
    "止损",
    "抄底",
    "满仓",
    "割肉",
    "目标价",
    "评分",
)


def _login(client) -> None:
    response = client.post("/app/login", data={"api_key": "secret", "next": "/app"})
    assert response.status_code == 200


def _page(client, code: str, *, frozen: str = TODAY) -> str:
    """GET one stock page with the caller's session cookie; assert 200."""

    with freeze_time(frozen):
        response = client.get(f"/app/stocks/{code}")
    assert response.status_code == 200, response.text
    return response.text


def _financial_section(html: str) -> str:
    """The rendered financial section, so copy guards do not read the shell."""

    start = html.index('id="stock-financials"')
    return html[start:]


def _fact_blocks(html: str, metric: str, period_end: str) -> list[str]:
    """Every fact block of one (metric, period end) cell (a cell may hold two)."""

    return re.findall(
        rf'<div class="financial-fact[^"]*"[^>]*data-metric="{re.escape(metric)}"'
        rf'[^>]*data-period-end="{period_end}"[^>]*>(.*?)</div>',
        html,
        re.S,
    )


def _data_attribute(html: str, element_id: str, name: str) -> str:
    match = re.search(rf'id="{element_id}"[^>]*data-{name}="([^"]*)"', html, re.S)
    assert match is not None, (element_id, name)
    return match.group(1)


def _amounts(html: str) -> list[str]:
    return re.findall(r'<span class="financial-amount">([^<]+)</span>', html)


# --- the covered HK listing with material --------------------------------------


async def test_plan_sample_page_00002_hk_renders_report_currency_and_year(
    api_client, tmp_path
) -> None:
    """The plan's named sample page: /app/stocks/00002.HK with CLP's material."""

    seeded = await seed_listings()
    years = tuple(year["label"] for year in PILOTS[CLP]["fiscal_years"])
    await store_material(tmp_path, listing_id=seeded["HKEX/00002"], company_key=CLP, labels=years)
    _login(api_client)
    html = _page(api_client, "00002.HK")

    assert "CLP Holdings Limited" in html
    assert "报表币种" in html
    assert '<dd id="financial-report-currency">HKD</dd>' in html
    assert '<dd id="financial-quote-currency">HKD</dd>' in html
    assert "2024-12-31" in html
    assert _data_attribute(html, "financial-material-coverage", "periods-covered") == "4"


async def test_hk_page_renders_material_currency_years_and_states(api_client, tmp_path) -> None:
    seeded = await seed_listings()
    await store_hsbc_material(tmp_path, seeded["HKEX/00005"])
    _login(api_client)
    html = _page(api_client, "00005.HK")

    # Identity: exchange, security type, quote currency and report currency are
    # four separate facts (HSBC quotes in HKD and reports in USD).
    assert "HSBC Holdings plc" in html
    assert '<dd id="financial-market">香港交易所</dd>' in html
    assert '<dd id="financial-security-type">普通股</dd>' in html
    assert '<dd id="financial-quote-currency">HKD</dd>' in html
    assert '<dd id="financial-report-currency">USD</dd>' in html
    assert "报表币种" in html and "报价币种" in html

    # Real fiscal years: the material's own ends, labels and spans.
    for fragment in ("2022-12-31", "2023-12-31", "2024-12-31", "FY2024", "365 天"):
        assert fragment in html, fragment

    # Two coverages, counted separately and exposed as separate numbers.
    assert _data_attribute(html, "financial-material-coverage", "periods-covered") == "3"
    assert _data_attribute(html, "financial-material-coverage", "facts-total") == "22"
    assert _data_attribute(html, "financial-verification-coverage", "facts-verified") == "7"
    assert _data_attribute(html, "financial-verification-coverage", "facts-candidate") == "15"
    assert "资料覆盖" in html and "核对覆盖" in html
    assert "不合成一个完整度" in html

    # Amounts are the source's own original-currency figures.
    assert "62440000000" in html
    assert "交易所年报原件" in html
    assert "已核对原件" in html and "候选（未核对原件）" in html
    assert "来源日期未标注" in html  # the fixture states no publication date

    # Business statuses, in the page's own wording.
    for code, label in (
        ("material_missing", "资料待采集"),
        ("partial_verification", "部分数据待核"),
        ("metric_not_applicable", "指标不适用"),
        ("latestness_unknown", "最新状态未核实"),
        ("online_refresh_disabled", "在线刷新未开启"),
    ):
        assert f'data-status-code="{code}"' in html, code
        assert label in html, label

    # The not-applicable bank metric names its reason.
    assert "平均权益 ROE" in html
    assert "bank: the ordinary-corporate profitability comparison is not applied" in html

    for forbidden in FORBIDDEN_UPSTREAM:
        assert forbidden not in html.lower(), forbidden


async def test_missing_amount_shows_reason_and_never_zero(api_client, tmp_path) -> None:
    seeded = await seed_listings()
    await store_hsbc_material(tmp_path, seeded["HKEX/00005"])
    _login(api_client)
    html = _page(api_client, "00005.HK")

    [cell] = _fact_blocks(html, "cost_of_revenue", "2024-12-31")
    assert "缺失" in cell
    assert MISSING_REASON in cell
    # No number at all is shown for the missing fact (missing is not 0).
    assert re.search(r"\d", cell) is None, cell
    # Every rendered amount on the page is a real non-zero figure.
    amounts = _amounts(html)
    assert amounts
    assert all(Decimal(amount) != 0 for amount in amounts)
    assert not re.search(r'financial-amount">0[.<]', html)


async def test_unresolved_source_conflict_is_shown_not_resolved(api_client, tmp_path) -> None:
    seeded = await seed_listings()
    base = next(
        fact for fact in fixture_facts(APPLE, "FY2024") if fact.metric == "revenue_operating"
    )
    conflict = replace(
        base,
        value=replace(base.value, raw_value="380000000000"),
        conflict="unresolved",
        conflict_note="两个来源的收入金额不同，未判定",
    )
    await store_material(
        tmp_path,
        listing_id=seeded["NASDAQ/AAPL"],
        company_key=APPLE,
        labels=("FY2021", "FY2022", "FY2023", "FY2024"),
        extra=(conflict,),
    )
    _login(api_client)
    html = _page(api_client, "AAPL")

    blocks = _fact_blocks(html, "revenue_operating", "2024-09-28")
    assert len(blocks) == 2  # both source values stay visible; neither is preferred
    assert "391035000000" in html and "380000000000" in html
    assert sum("来源冲突（未判定）" in block for block in blocks) == 1
    assert 'data-status-code="source_conflict"' in html
    assert "收入" in html  # the status names the affected metric


async def test_us_page_uses_the_real_52_53_week_fiscal_years(api_client, tmp_path) -> None:
    seeded = await seed_listings()
    await store_apple_material(tmp_path, seeded["NASDAQ/AAPL"])
    _login(api_client)
    html = _page(api_client, "AAPL")

    for fragment in ("2021-09-25", "2022-09-24", "2023-09-30", "2024-09-28", "370 天"):
        assert fragment in html, fragment
    assert "2024-12-31" not in html  # no calendar year is assumed
    assert '<dd id="financial-report-currency">USD</dd>' in html


async def test_source_date_and_locator_are_shown_per_fact(api_client, tmp_path) -> None:
    seeded = await seed_listings()
    base = next(
        fact for fact in fixture_facts(HSBC, "FY2024") if fact.metric == "revenue_net_credit"
    )
    dated = replace(
        base,
        provenance=replace(
            base.provenance,
            source_published_date=date(2025, 3, 18),
            source_published_precision=DatePrecision.DATE,
        ),
        original_locator="p.128 合并收益表",
    )
    await store_material(
        tmp_path,
        listing_id=seeded["HKEX/00005"],
        company_key=HSBC,
        labels=("FY2024",),
        extra=(dated,),
    )
    _login(api_client)
    html = _page(api_client, "00005.HK")

    blocks = _fact_blocks(html, "revenue_net_credit", "2024-12-31")
    assert any(
        "来源日期 2025-03-18" in block and "原件定位：p.128 合并收益表" in block for block in blocks
    ), blocks


# --- covered without material, and listings outside the list --------------------


async def test_listed_without_material_asks_for_collection(api_client) -> None:
    await seed_listings()
    _login(api_client)
    html = _page(api_client, "AAPL")

    assert '<dd id="financial-security-type">普通股</dd>' in html
    assert '<dd id="financial-report-currency">USD</dd>' in html
    assert 'data-status-code="material_missing"' in html
    assert "资料待采集" in html
    # The refresh entry is visible; the copy says it only registers the task.
    assert 'id="financial-refresh-button"' in html
    assert "在线刷新未开启" in html
    assert "不会联网采集" in html
    assert _amounts(html) == []  # nothing is invented for a company without material


async def test_ads_security_type_and_uncovered_listing(api_client) -> None:
    await seed_listings()
    _login(api_client)

    ads = _page(api_client, "TSM")
    assert '<dd id="financial-security-type">ADS（美国存托凭证）</dd>' in ads
    assert '<dd id="financial-market">纽约证券交易所</dd>' in ads

    uncovered = _page(api_client, "00777.HK")
    assert 'data-status-code="listing_not_covered"' in uncovered
    assert "当前未覆盖" in uncovered
    assert '<dd id="financial-security-type">未标注</dd>' in uncovered
    assert '<dd id="financial-report-currency">未标注</dd>' in uncovered
    # Outside the checked list nothing is collected, so no refresh is offered.
    assert 'id="financial-refresh-button"' not in uncovered
    assert "不在受控身份名单内" in uncovered


async def test_unknown_or_malformed_code_keeps_the_existing_semantics(api_client) -> None:
    await seed_listings()
    _login(api_client)

    # The A-share branch's 400 copy is exactly the one it always had.
    for bad in ("NOT-A-TICKER", "600519", "00002.HK."):
        response = api_client.get(f"/app/stocks/{bad}")
        assert response.status_code == 400, bad
        assert f"不是有效的 A 股代码：{bad}" in response.text, bad

    # A well-formed HK/US code with no known listing is answered explicitly.
    for missing in ("00001.HK", "TSLA"):
        response = api_client.get(f"/app/stocks/{missing}")
        assert response.status_code == 404, missing
        assert "未收录该证券" in response.text, missing

    # A ticker registered on two US markets cannot be resolved without guessing.
    async with session_scope() as session:
        await ListingRepository(session).upsert_listing(
            market_code="NYSE", ticker="AAPL", company_name="Apple Inc. (other market)"
        )
    ambiguous = api_client.get("/app/stocks/AAPL")
    assert ambiguous.status_code == 400
    assert "多个市场登记" in ambiguous.text


# --- A-share regression and page guards ----------------------------------------


async def test_a_share_page_rendering_is_unchanged(api_client) -> None:
    _login(api_client)
    html = _page(api_client, "600519.SH")

    for fragment in (
        'id="stock-run-form"',
        'id="stock-run-result"',
        'id="stock-spark"',
        'data-ticker="600519.SH"',
        'id="stock-changes"',
        'id="stock-history"',
        'data-endpoint="/research/stocks/600519.SH/history"',
        "运行研究",
        "确定性评分",
        "研究辅助，非投资建议",
    ):
        assert fragment in html, fragment
    assert "financial" not in html
    assert "资料覆盖" not in html


async def test_hk_page_has_no_inline_style_and_only_static_scripts(api_client) -> None:
    await seed_listings()
    _login(api_client)
    html = _page(api_client, "00005.HK")

    assert 'id="supplemental-items-root"' not in html
    assert "<style" not in html
    assert " style=" not in html
    assert "onclick=" not in html
    scripts = re.findall(r"<script\b([^>]*)>", html)
    assert scripts
    for attrs in scripts:
        match = re.search(r'src="([^"]+)"', attrs)
        assert match is not None, attrs
        assert match.group(1).startswith("/app/static/"), match.group(1)
    section = _financial_section(html)
    for word in TRADING_WORDS:
        assert word not in section, word


async def test_hk_page_copy_carries_no_upstream_identifiers(api_client, tmp_path) -> None:
    seeded = await seed_listings()
    await store_hsbc_material(tmp_path, seeded["HKEX/00005"])
    _login(api_client)
    html = _page(api_client, "00005.HK")

    lowered = html.lower()
    for forbidden in ("eastmoney", "sec.gov", "cik", "accession", "material_collection"):
        assert forbidden not in lowered, forbidden


# --- the refresh entry the page's JS calls -------------------------------------


async def test_refresh_from_the_page_session_is_deduplicated(api_client, tmp_path) -> None:
    seeded = await seed_listings()
    await store_hsbc_material(tmp_path, seeded["HKEX/00005"])
    _login(api_client)
    html = _page(api_client, "00005.HK")
    listing_id = re.search(r'id="financial-refresh-button" data-listing-id="(\d+)"', html)
    assert listing_id is not None
    url = f"/research/listings/{listing_id.group(1)}/refresh"
    # Same headers the page's api.js sends (session cookie + fetch marker).
    headers = {"X-Requested-With": "fetch"}
    with freeze_time(TODAY):
        first = api_client.post(url, json={}, headers=headers)
        second = api_client.post(url, json={}, headers=headers)
    assert first.status_code == 202 and second.status_code == 202
    assert first.json()["created"] is True
    assert second.json()["created"] is False
    assert second.json()["idempotency_key"] == first.json()["idempotency_key"]
    assert first.json()["online_refresh_enabled"] is False


def test_refresh_client_posts_and_never_claims_an_update() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available")
        return
    uri = (STATIC / "financials.js").resolve().as_uri()
    script = (
        f"const MODULE_URI = {json.dumps(uri)};\n"
        + """
    class Node {
      constructor(tag) { this.tag = tag; this.attrs = {}; this.children = []; this.listeners = {}; this.textContent = undefined; this.dataset = {}; this.disabled = false; }
      setAttribute(k, v) { this.attrs[k] = String(v); }
      getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
      appendChild(c) { this.children.push(c); return c; }
      replaceChildren() { this.children = []; }
      addEventListener(t, f) { (this.listeners[t] ||= []).push(f); }
    }
    const button = new Node("button"); button.attrs["data-listing-id"] = "7";
    const region = new Node("div");
    const bySelector = { "#financial-refresh-button": button, "#financial-refresh-result": region };
    globalThis.document = { createElement: (tag) => new Node(tag), querySelector: (sel) => bySelector[sel] || null };
    const payloads = [
      { run_id: 42, created: false, idempotency_key: "material_collection:7:eastmoney.hk.native.v1+sec.companyfacts.v1:-|today",
        adapter_versions: { "eastmoney.hk.native.v1": "1", "sec.companyfacts.v1": "1" }, online_refresh_enabled: false },
      { run_id: 43, created: true, idempotency_key: "material_collection:7:...",
        adapter_versions: { "eastmoney.hk.native.v1": "1", "sec.companyfacts.v1": "1" }, online_refresh_enabled: true },
    ];
    const calls = [];
    globalThis.fetch = async (path, options) => {
      calls.push({ path, method: options.method, body: options.body, marker: options.headers["X-Requested-With"] });
      if (calls.length === 3) return { status: 503, ok: false, headers: { get: () => "application/json" }, json: async () => ({}) };
      return { status: 202, ok: true, headers: { get: () => "application/json" }, json: async () => payloads[calls.length - 1] };
    };
    const m = await import(MODULE_URI);
    m.bootFinancials();
    const text = (node) => (node.textContent || "") + node.children.map((c) => " " + text(c)).join("");
    const snapshots = [];
    for (let i = 0; i < 3; i += 1) {
      await button.listeners.click[0]();
      snapshots.push({ state: region.dataset.state, text: text(region), enabled: button.disabled === false });
    }
    console.log(JSON.stringify({ calls, snapshots }));
    """
    )
    result = subprocess.run(
        [node, "--input-type=module", "-e", script], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    for call in out["calls"]:
        assert call["path"] == "/research/listings/7/refresh"
        assert call["method"] == "POST"
        assert call["body"] == "{}"
        assert call["marker"] == "fetch"
    dedup, created, failed = out["snapshots"]
    assert dedup["state"] == "ready" and "未重复登记" in dedup["text"]
    assert "在线刷新未开启" in dedup["text"]
    assert created["state"] == "ready" and "已登记采集任务（新建）" in created["text"]
    assert "在线刷新已开启" in created["text"]
    # A queued batch is never presented as fetched data.
    for snapshot in out["snapshots"]:
        assert "已更新" not in snapshot["text"]
        for forbidden in (
            "42",
            "43",
            "eastmoney",
            "sec.companyfacts",
            "material_collection",
            "CIK",
        ):
            assert forbidden not in snapshot["text"], (forbidden, snapshot["text"])
    assert failed["state"] == "error" and "请求失败" in failed["text"]
    assert all(snapshot["enabled"] for snapshot in out["snapshots"])
