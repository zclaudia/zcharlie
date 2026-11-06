"""Web contract for the multi-perspective review card and watchlist badge."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

WEB_STATIC = Path("src/investment_agent/web/static")


def _login(client):
    response = client.post("/app/login", data={"api_key": "secret", "next": "/app"})
    assert response.status_code == 200


def _run_node(script: str) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available")
    result = subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_stock_page_links_review_card_assets(api_client) -> None:
    _login(api_client)
    html = api_client.get("/app/stocks/600406.SH").text
    assert 'id="stock-detail"' in html
    assert 'data-listing-id=' in html
    assert 'data-ticker="600406.SH"' in html
    assert "pages/review_panel.css" in html
    assert 'href="#stock-review"' in html
    assert "多视角评审" in html


def test_watchlist_page_has_review_sort_control(api_client) -> None:
    _login(api_client)
    html = api_client.get("/app/watchlist").text
    assert 'id="watchlist-sort"' in html
    for value in ("default", "divergence", "reviewed"):
        assert f'value="{value}"' in html


def test_review_static_assets_are_served(api_client) -> None:
    script = api_client.get("/app/static/review_panel.js")
    assert script.status_code == 200
    source = script.text
    for marker in (
        "/review-panels",
        "/review-rounds/",
        "/review-rounds?limit=20",
        "POLL_INTERVAL_MS = 3000",
        "visibilitychange",
        "评审意见 · 未经发布校验 · 不是 Agent 正式意见",
    ):
        assert marker in source, marker
    # Model text is never written as HTML.
    assert "innerHTML" not in source
    css = api_client.get("/app/static/pages/review_panel.css")
    assert css.status_code == 200
    for marker in (".rp-matrix", ".rp-seat", ".rp-num-unverified", ".rp-ref-invalid"):
        assert marker in css.text, marker


def test_watchlist_renderer_consumes_latest_review_badge() -> None:
    source = (WEB_STATIC / "watchlist.js").read_text(encoding="utf-8")
    assert "latest_review" in source
    assert "divergence_level" in source
    assert "分歧" in source
    assert "#stock-review" in source
    assert 'view.sort === "divergence"' in source
    assert 'view.sort === "reviewed"' in source


def test_annotation_helpers_mark_unverified_numbers() -> None:
    uri = (WEB_STATIC / "review_panel.js").resolve().as_uri()
    result = _run_node(
        f"""
import assert from 'node:assert/strict';
const m = await import({json.dumps(uri)});
const segments = m.annotatedSegments('营收增长 12.5% 需要核实', 'summary', {{
  numbers: [{{ path: 'summary', value: '12.5', status: 'unverified' }}],
}});
assert.deepEqual(segments.map((s) => [s.text, s.kind]), [
  ['营收增长 ', 'plain'], ['12.5', 'unverified'], ['% 需要核实', 'plain'],
]);
const sourced = m.annotatedSegments('评分为 72.5 分', 'summary', {{
  numbers: [{{ path: 'summary', value: '72.5', status: 'sourced', source_ref: 'base:score' }}],
}});
assert.equal(sourced[1].kind, 'sourced');
assert.equal(sourced[1].title, '来源：base:score');
assert.equal(m.consensusText({{ positive: 2, negative: 1 }}), '2 偏正面 / 1 偏负面');
assert.equal(m.matrixCellLabel('mixed'), '分歧');
assert.equal(m.matrixCellLabel('absent'), '—');
console.log(JSON.stringify({{ ok: true }}));
"""
    )
    assert result == {"ok": True}


FAKE_DOM = """
class FakeNode {
  constructor(tag) {
    this.tag = tag; this.attrs = {}; this.children = []; this.listeners = {};
    this.parentNode = null; this.textContent = undefined; this.value = "";
    this.hidden = false; this.disabled = false; this.checked = false; this.id = "";
    this.dataset = {}; this.style = {}; this.isConnected = true;
    this.classes = new Set();
  }
  get className() { return [...this.classes].join(" "); }
  set className(v) { this.classes = new Set(String(v).split(/\\s+/).filter(Boolean)); }
  get classList() { const c = this.classes; return { add: (x) => c.add(x), remove: (x) => c.delete(x), contains: (x) => c.has(x) }; }
  setAttribute(k, v) { this.attrs[k] = String(v); if (k === "class") this.className = String(v); if (k === "id") this.id = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  hasAttribute(k) { return k in this.attrs; }
  removeAttribute(k) { delete this.attrs[k]; }
  appendChild(c) { if (c && typeof c === "object") c.parentNode = this; this.children.push(c); return c; }
  append(...nodes) { for (const n of nodes) this.appendChild(n); }
  replaceChildren(...nodes) { this.children = []; for (const n of nodes) this.appendChild(n); }
  remove() { if (this.parentNode) this.parentNode.children = this.parentNode.children.filter((c) => c !== this); }
  addEventListener(t, f) { (this.listeners[t] ||= []).push(f); }
  dispatch(t, event = {}) { event.target ||= this; event.preventDefault ||= () => {}; for (const f of this.listeners[t] || []) f(event); return event; }
  querySelector() { return null; }
  querySelectorAll() { return []; }
  walk(fn) { for (const c of this.children) { if (c instanceof FakeNode) { fn(c); c.walk(fn); } } }
}
globalThis.document = {
  createElement: (tag) => new FakeNode(tag),
  createTextNode: (text) => ({ textContent: text }),
  addEventListener() {}, removeEventListener() {}, hidden: false,
  body: new FakeNode("body"),
};
globalThis.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} });
globalThis.location = { href: "" };
"""


def _detail_payload() -> dict:
    return {
        "id": 1,
        "listing_id": 1,
        "panel_id": "default",
        "panel_name": "默认评审组",
        "trigger": "manual",
        "status": "completed",
        "as_of": "2026-11-06",
        "evidence_cutoff": "2026-11-06T08:00:00+00:00",
        "base_pack": {"items": []},
        "base_pack_hash": "a" * 64,
        "created_at": "2026-11-06T08:00:00+00:00",
        "completed_at": "2026-11-06T08:03:00+00:00",
        "reserved_tokens": 100,
        "reserved_requests": 8,
        "settled_usage": {},
        "seats": [
            {
                "seat_id": "value",
                "lens_id": "value",
                "lens_ref": "value.v1",
                "lens_name": "价值视角",
                "model_ref": "primary/reason",
                "status": "succeeded",
                "error": None,
                "opinion": {
                    "summary": "营收增长 12.5% 仍需核实。",
                    "company_stance": "positive",
                    "confidence": "high",
                    "trade_view": "hold_watch",
                    "assumptions": [
                        {
                            "dimension": "valuation",
                            "direction": "positive",
                            "text": "估值处于合理区间",
                            "evidence_refs": ["base:score", "evidence:not-read"],
                        }
                    ],
                    "bull_points": [{"text": "现金流稳健", "evidence_refs": ["base:score"]}],
                    "bear_points": [],
                    "falsifiers": [{"dimension": "cash_flow_quality", "text": "经营现金流转负"}],
                    "open_questions": ["营收增速能否持续？"],
                    "trade_conditions": [],
                },
                "annotations": {
                    "numbers": [
                        {"path": "summary", "value": "12.5", "status": "unverified"},
                    ],
                    "invalid_refs": [
                        {
                            "path": "bull_points[0].evidence_refs[0]",
                            "ref": "evidence:not-read",
                            "reason": "该引用不在本席位本轮实际读取的材料中",
                        }
                    ],
                    "missing_required_dimensions": [],
                },
                "evidence_refs": ["base:score"],
                "usage": {"total_tokens": 3200},
            },
            {
                "seat_id": "bear",
                "lens_id": "bear",
                "lens_ref": "bear.v1",
                "lens_name": "空头视角",
                "model_ref": "primary/fast",
                "status": "succeeded",
                "error": None,
                "opinion": {
                    "summary": "估值隐含增长过于乐观。",
                    "company_stance": "negative",
                    "confidence": "medium",
                    "trade_view": "avoid",
                    "assumptions": [
                        {
                            "dimension": "valuation",
                            "direction": "negative",
                            "text": "价格已透支增长",
                            "evidence_refs": ["base:score"],
                        }
                    ],
                    "bull_points": [],
                    "bear_points": [{"text": "应收增速高于营收", "evidence_refs": ["base:score"]}],
                    "falsifiers": [],
                    "open_questions": [],
                    "trade_conditions": [],
                },
                "annotations": {"numbers": [], "invalid_refs": [], "missing_required_dimensions": []},
                "evidence_refs": ["base:score"],
                "usage": {},
            },
        ],
        "comparison": {
            "algorithm_version": "v1",
            "stance_distribution": {"positive": 1, "negative": 1},
            "trade_view_distribution": {"hold_watch": 1, "avoid": 1},
            "dimension_matrix": {
                "valuation": {"value": "positive", "bear": "negative"},
            },
            "divergent_dimensions": ["valuation"],
            "divergent_materials": {"valuation": True},
            "stance_range": 2,
            "divergence_score": 5,
            "divergence_level": "high",
            "evidence_overlap": [],
            "falsifiers_by_dimension": [
                {
                    "dimension": "cash_flow_quality",
                    "items": [{"seat_id": "value", "text": "经营现金流转负"}],
                }
            ],
            "seat_changes": [],
            "notify_reasons": ["high_divergence"],
            "failed_seats": [],
        },
    }


def test_review_card_renders_consensus_matrix_and_seats() -> None:
    uri = (WEB_STATIC / "review_panel.js").resolve().as_uri()
    payload = {
        "panels": {
            "enabled": True,
            "default_panel": "default",
            "panels": [
                {
                    "id": "default",
                    "name": "默认评审组",
                    "default": True,
                    "seats": [
                        {
                            "seat_id": "value",
                            "lens_id": "value",
                            "lens_ref": "value.v1",
                            "lens_name": "价值视角",
                            "lens_focus": "经营价值",
                            "model_ref": "primary/reason",
                        }
                    ],
                }
            ],
            "lenses": [],
        },
        "rounds": [
            {
                "id": 1,
                "listing_id": 1,
                "panel_id": "default",
                "panel_name": "默认评审组",
                "trigger": "manual",
                "status": "completed",
                "as_of": "2026-11-06",
                "evidence_cutoff": "2026-11-06T08:00:00+00:00",
                "created_at": "2026-11-06T08:00:00+00:00",
                "completed_at": "2026-11-06T08:03:00+00:00",
                "seat_count": 2,
                "seat_terminal_count": 2,
                "seat_failed_count": 0,
                "comparison": {"divergence_level": "high"},
            }
        ],
        "detail": _detail_payload(),
    }
    script = (
        FAKE_DOM
        + f"""
const calls = [];
const payload = {json.dumps(payload)};
globalThis.fetch = async (path) => {{
  calls.push(path);
  const body = path === '/review-panels' ? payload.panels
    : path.startsWith('/watchlist/1/review-rounds') ? payload.rounds
    : path === '/review-rounds/1' ? payload.detail
    : null;
  return new Response(JSON.stringify(body), {{ status: 200, headers: {{ 'Content-Type': 'application/json' }} }});
}};
const {{ mountReviewCard }} = await import({json.dumps(uri)});
const container = new FakeNode('section');
const mounted = mountReviewCard(container, {{ listingId: 1, ticker: '600036.SH' }});
await new Promise((resolve) => setTimeout(resolve, 80));
const classes = [];
const texts = [];
const visit = (node) => {{
  if (!(node instanceof FakeNode)) return;
  classes.push(node.attrs.class || '');
  if (typeof node.textContent === 'string') texts.push(node.textContent);
  node.walk(visit);
}};
visit(mounted.root);
const unverified = [];
mounted.root.walk((node) => {{
  if ((node.attrs.class || '').includes('rp-num-unverified')) unverified.push(node.textContent);
}});
const invalid = [];
mounted.root.walk((node) => {{
  if ((node.attrs.class || '').includes('rp-ref-invalid')) invalid.push(node.textContent);
}});
console.log(JSON.stringify({{
  calls,
  hidden: mounted.root.hidden,
  classes,
  text: texts.join(' | '),
  unverified,
  invalid,
}}));
"""
    )
    out = _run_node(script)
    assert out["hidden"] is False
    assert out["calls"] == [
        "/review-panels",
        "/watchlist/1/review-rounds?limit=20",
        "/review-rounds/1",
    ]
    joined = " ".join(out["classes"])
    for marker in ("rp-consensus", "rp-matrix", "rp-divergent-row", "rp-seat", "rp-falsifiers", "rp-stamp"):
        assert marker in joined, marker
    assert "2 席" not in out["text"]  # badge text belongs to the watchlist row
    assert "1 偏正面 / 1 偏负面" in out["text"]
    assert "双方看过的材料不同" in out["text"]
    assert out["unverified"] == ["12.5"]
    assert out["invalid"] == ["base:score"]
    assert "评审意见 · 未经发布校验 · 不是 Agent 正式意见" in out["text"]


def test_review_card_hides_when_disabled_with_no_rounds() -> None:
    uri = (WEB_STATIC / "review_panel.js").resolve().as_uri()
    script = (
        FAKE_DOM
        + f"""
globalThis.fetch = async (path) => {{
  const body = path === '/review-panels'
    ? {{ enabled: false, default_panel: null, panels: [], lenses: [] }}
    : [];
  return new Response(JSON.stringify(body), {{ status: 200, headers: {{ 'Content-Type': 'application/json' }} }});
}};
const {{ mountReviewCard }} = await import({json.dumps(uri)});
const container = new FakeNode('section');
const mounted = mountReviewCard(container, {{ listingId: 7, ticker: '600036.SH' }});
await new Promise((resolve) => setTimeout(resolve, 80));
console.log(JSON.stringify({{ attached: container.children.length, hidden: mounted.root.hidden }}));
"""
    )
    out = _run_node(script)
    # Disabled and no history: the entry disappears entirely.
    assert out == {"attached": 0, "hidden": True}
