"""WP10 stock-page industry-peer reference section.

Page and client contract, following the existing web test conventions:

* **Template contract** - the A-share stock page declares the independent
  ``#peer-context-section`` region (mount point, ticker, current vs research
  sub-areas, evidence panel) and links the section's own CSS file and the
  ``industry_peers.js`` module; both static assets are served (CSP forbids
  inline styles, so the styles ship as a dedicated file).
* **Label coverage** - the JS label maps cover every ``PeerContextStatus``,
  ``PeerMetricStatus`` value and every audit exclusion reason, so a new enum
  value cannot silently reach the page as raw internal text.
* **Display format** - the shipped formatter reproduces the design's golden
  presentation exactly (fraction -> percent with two decimals, CFO ratio with
  four decimals, position as percent) without ever going through float.
* **Boot behaviour** - driven in node over a minimal fake DOM: not_supported
  collapses the whole section to one "尚未覆盖" line, and a research snapshot
  without a saved binding shows "该次研究未保存同行参照" (never backfilled).
"""

from __future__ import annotations

import json
import shutil
import subprocess

from investment_agent.industry_peers.calculations import (
    FIELD_CONFLICT,
    MISSING_FIELD,
    NON_POSITIVE_DENOMINATOR,
)
from investment_agent.industry_peers.contracts import (
    PeerContextStatus,
    PeerMetricStatus,
)
from investment_agent.web.static_assets import static_url
from tests.support.peer_world import PEERS_JS, labels_object


def _login(client):
    response = client.post("/app/login", data={"api_key": "secret", "next": "/app"})
    assert response.status_code == 200


# ---- template contract ----------------------------------------------------------


def test_stock_page_declares_the_independent_peer_section(api_client):
    _login(api_client)
    html = api_client.get("/app/stocks/600519.SH").text
    assert 'id="peer-context-section"' in html
    assert 'data-ticker="600519.SH"' in html
    assert 'id="peer-context-current"' in html
    assert 'id="peer-context-bound"' in html
    assert 'id="peer-context-evidence"' in html
    # The section ships its own assets (CSP style-src 'self' forbids inline styles).
    assert static_url("industry_peers.css") in html
    assert f'<script type="module" src="{static_url("industry_peers.js")}"></script>' in html


def test_peer_section_is_not_in_the_financial_branch(api_client):
    """The peer section is A-share-only; the HK/US financial page never ships it."""

    _login(api_client)
    html = api_client.get("/app/stocks/00001.HK").text
    assert 'id="peer-context-section"' not in html


def test_peer_static_assets_are_served(api_client):
    _login(api_client)
    for path in ("/app/static/industry_peers.js", "/app/static/industry_peers.css"):
        response = api_client.get(path)
        assert response.status_code == 200, path


def test_peer_module_reads_only_the_two_readonly_endpoints():
    source = PEERS_JS.read_text(encoding="utf-8")
    assert "/industry-peer-context" in source
    assert "/industry-peer-context/companies" in source
    # 独立自加载：不 import app.js（评分区失败不影响本区域，反之亦然）。
    assert 'from "./app.js"' not in source
    assert 'from "./api.js"' in source
    # 历史不串新结果：绑定读取带 batch_run_id，且绝不回填。
    assert "batch_run_id" in source
    assert "未保存同行参照" in source


# ---- label maps cover the enums (no raw internal text on the page) ---------------


def test_context_and_metric_status_labels_cover_the_enums():
    source = PEERS_JS.read_text(encoding="utf-8")
    context = labels_object(source, "CONTEXT_STATUS_LABELS")
    assert set(context) == {status.value for status in PeerContextStatus}
    metric = labels_object(source, "METRIC_STATUS_LABELS")
    assert set(metric) == {status.value for status in PeerMetricStatus}


def test_exclusion_labels_cover_the_audit_reasons():
    from investment_agent.industry_peers import calculations

    source = PEERS_JS.read_text(encoding="utf-8")
    labels = labels_object(source, "EXCLUSION_LABELS")
    for constant in (
        "NOT_IN_COHORT",
        "DUPLICATE_COMPANY",
        "PERIOD_MISMATCH",
        "MISSING_FIELD",
        "FIELD_CONFLICT",
        "SOURCE_FIELD_MISMATCH",
        "NON_POSITIVE_DENOMINATOR",
        "NON_POSITIVE_EQUITY",
        "APPLICABILITY_INPUT_MISSING",
    ):
        assert getattr(calculations, constant) in labels, constant
    # The golden partial batch's exclusion reasons are covered too.
    assert NON_POSITIVE_DENOMINATOR in labels and MISSING_FIELD in labels and FIELD_CONFLICT in labels


# ---- golden display format (node-driven, exact decimal strings) ------------------


def test_metric_numbers_display_in_the_design_golden_format():
    node = shutil.which("node")
    if node is None:
        import pytest

        pytest.skip("node is not available")
        return
    uri = PEERS_JS.resolve().as_uri()
    script = (
        "globalThis.document = undefined;\n"
        f"const m = await import({json.dumps(uri)});\n"
        + """
const f = m.formatMetricNumber;
const out = {
  roeTarget: f("0.3253", "roe", "value"),
  roeMedian: f("0.0739", "roe", "value"),
  roePosition: f("94.44", "roe", "position"),
  contractTarget: f("0.0474", "contract_revenue", "value"),
  contractMedian: f("0.1630", "contract_revenue", "value"),
  contractPosition: f("16.67", "contract_revenue", "position"),
  cfoTarget: f("0.7474", "cfo_parent_profit", "value"),
  cfoMedian: f("0.5792", "cfo_parent_profit", "value"),
  cfoPosition: f("73.33", "cfo_parent_profit", "position"),
  rounding: f("0.999999", "roe", "value"),
  padded: f("0.5", "roe", "value"),
  unknown: f("not-a-number", "roe", "value"),
};
console.log(JSON.stringify(out));
"""
    )
    result = subprocess.run(
        [node, "--input-type=module", "-e", script], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out["roeTarget"] == "32.53%"
    assert out["roeMedian"] == "7.39%"
    assert out["roePosition"] == "94.44%"
    assert out["contractTarget"] == "4.74%"
    assert out["contractMedian"] == "16.30%"  # trailing zero kept (design golden)
    assert out["contractPosition"] == "16.67%"
    assert out["cfoTarget"] == "0.7474"
    assert out["cfoMedian"] == "0.5792"
    assert out["cfoPosition"] == "73.33%"
    assert out["rounding"] == "100.00%"  # half-up carry, exact
    assert out["padded"] == "50.00%"
    assert out["unknown"] is None


# ---- boot behaviour over a minimal fake DOM --------------------------------------


def _fake_dom() -> str:
    return """
class FakeNode {
  constructor(tag) {
    this.tag = tag; this.attrs = {}; this.children = []; this.listeners = {};
    this.parentNode = null; this.textContent = undefined; this.hidden = false;
    this.dataset = {}; this.classes = new Set();
  }
  setAttribute(k, v) { this.attrs[k] = String(v); if (k === "class") this.classes = new Set(String(v).split(/\\s+/).filter(Boolean)); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  appendChild(c) { c.parentNode = this; this.children.push(c); return c; }
  replaceChildren(...nodes) { this.children = []; for (const n of nodes) this.appendChild(n); }
  addEventListener(t, f) { (this.listeners[t] ||= []).push(f); }
  dispatch(t, event = {}) { for (const f of this.listeners[t] || []) f(event); }
  scrollIntoView() {}
  walk(fn) { for (const c of this.children) { if (c instanceof FakeNode) { fn(c); c.walk(fn); } } }
  text() { const out = []; this.walk((n) => { if (typeof n.textContent === "string") out.push(n.textContent); }); return out.join("\\n"); }
  querySelector(sel) { const id = sel.startsWith("#") ? sel.slice(1) : null; return id ? this.children.find((c) => c instanceof FakeNode && c.attrs.id === id) || null : null; }
}
const section = new FakeNode("section");
section.attrs["data-ticker"] = "600519.SH";
for (const id of ["#peer-context-current", "#peer-context-bound", "#peer-context-evidence"]) {
  const child = new FakeNode("div"); child.attrs.id = id.slice(1); section.appendChild(child);
}
const respond = (status, body) => ({
  status, ok: status >= 200 && status < 300,
  headers: { get: () => "application/json" },
  json: async () => body, text: async () => JSON.stringify(body),
});
"""


def test_not_supported_collapses_to_one_uncovered_line():
    node = shutil.which("node")
    if node is None:
        import pytest

        pytest.skip("node is not available")
        return
    uri = PEERS_JS.resolve().as_uri()
    script = _fake_dom() + f"""
const URI = {json.dumps(uri)};
globalThis.document = {{
  readyState: "complete",
  createElement: (tag) => new FakeNode(tag),
  querySelector: (sel) => (sel === "#peer-context-section" ? section : null),
  addEventListener: () => {{}},
}};
const calls = [];
globalThis.fetch = async (path) => {{
  calls.push(path);
  return respond(200, {{
    schema_version: 1, status: "not_supported", target: null,
    data_as_of: "2026-10-12T08:00:00+00:00",
    gaps: ["industry peer context is disabled; the feature does not cover this stock yet"],
  }});
}};
const m = await import(URI);
m.bootIndustryPeers();
await new Promise((r) => setTimeout(r, 20));
const text = section.text();
console.log(JSON.stringify({{calls, hidden: section.hidden, text}}));
"""
    result = subprocess.run(
        [node, "--input-type=module", "-e", script], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out["calls"][0] == "/stocks/600519.SH/industry-peer-context"
    assert out["hidden"] is False
    assert "尚未覆盖" in out["text"]
    # One uncovered notice: neither the research-binding sub-area nor the
    # not-supported reason is repeated twice (the parallel binding read is
    # suppressed once the section collapsed to the uncovered state).
    assert out["text"].count("行业与同行参照尚未覆盖该股票") == 1
    assert out["text"].count("industry peer context is disabled") == 1
    assert "该次研究" not in out["text"]


def test_research_snapshot_without_binding_shows_unsaved_copy():
    node = shutil.which("node")
    if node is None:
        import pytest

        pytest.skip("node is not available")
        return
    uri = PEERS_JS.resolve().as_uri()
    script = _fake_dom() + f"""
const URI = {json.dumps(uri)};
globalThis.document = {{
  readyState: "complete",
  createElement: (tag) => new FakeNode(tag),
  querySelector: (sel) => (sel === "#peer-context-section" ? section : null),
  addEventListener: () => {{}},
}};
const contextBody = {{
  schema_version: 1, status: "partial",
  target: {{ticker: "600519.SH", company_id: 1, listing_id: 1, name: "贵州茅台"}},
  report_period: "2025-12-31", current_report_period: "2025-12-31", old_reference: false,
  batch_run_id: "peer-offline-1", data_as_of: "2026-10-12T08:00:00+00:00",
  metrics: [], warnings: [], gaps: [], stale_reasons: [],
}};
const calls = [];
globalThis.fetch = async (path) => {{
  calls.push(path);
  if (path === "/stocks/600519.SH/industry-peer-context") return respond(200, contextBody);
  if (path === "/research/stocks/600519.SH/history?limit=1")
    return respond(200, [{{run_id: 7}}]);
  if (path === "/research/runs/7")
    return respond(200, {{run_id: 7, industry_peer_context: null}});
  throw new Error("unexpected fetch " + path);
}};
const m = await import(URI);
m.bootIndustryPeers();
await new Promise((r) => setTimeout(r, 20));
const current = section.children[0];
const bound = section.children[1];
console.log(JSON.stringify({{
  calls,
  currentText: current.text(),
  boundText: bound.text(),
  boundSplicedBatch: bound.text().includes("peer-offline-1"),
}}));
"""
    result = subprocess.run(
        [node, "--input-type=module", "-e", script], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    # 当前区读自己的端点；历史区只发 history + run detail，绝不带最新批次回填。
    assert "/stocks/600519.SH/industry-peer-context" in out["calls"][0]
    assert "/research/stocks/600519.SH/history?limit=1" in out["calls"]
    assert "/research/runs/7" in out["calls"]
    assert "该次研究（运行 #7）未保存同行参照" in out["boundText"]
    assert out["boundSplicedBatch"] is False  # null binding is never backfilled
    assert "当前参照" in out["currentText"]
