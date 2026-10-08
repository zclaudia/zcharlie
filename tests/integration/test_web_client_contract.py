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


def test_combobox_component_and_mounts():
    """combobox.js owns the search dropdown; the watchlist add form and the
    candidate nomination form attach it to their ticker inputs (plan 05 / D4)."""

    combobox = (WEB_STATIC / "combobox.js").read_text(encoding="utf-8")
    assert "export function attachCombobox" in combobox
    assert "/stocks/search?q=" in combobox
    for marker in ('role: "listbox"', 'role: "option"', '"ArrowDown"', '"ArrowUp"', '"Enter"', '"Escape"', "DEBOUNCE_MS = 200"):
        assert marker in combobox, marker
    for module in ("app.js", "candidates.js"):
        source = (WEB_STATIC / module).read_text(encoding="utf-8")
        assert 'from "./combobox.js"' in source, module
        assert "attachCombobox(" in source, module
    css = (WEB_STATIC / "app.css").read_text(encoding="utf-8")
    assert ".combo-list" in css and ".combo-list[hidden]" in css


def test_combobox_searches_renders_and_selects_with_the_keyboard():
    """Drive ``attachCombobox`` in node over a tiny fake DOM: debounce, listbox
    rendering, arrow keys, Enter selection (ticker + onSelect) and Escape."""

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available")
        return
    uri = (WEB_STATIC / "combobox.js").resolve().as_uri()
    script = f"const MODULE_URI = {json.dumps(uri)};\n" + """
    class Node {
      constructor(tag) { this.tag = tag; this.attrs = {}; this.children = []; this.listeners = {}; this.parentNode = null; this.hidden = false; this.value = ""; this.form = null; this.textContent = undefined; this.classes = new Set(); }
      get classList() { const c = this.classes; return { add: (x) => c.add(x), remove: (x) => c.delete(x), contains: (x) => c.has(x) }; }
      setAttribute(k, v) { this.attrs[k] = String(v); }
      getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
      removeAttribute(k) { delete this.attrs[k]; }
      appendChild(c) { c.parentNode = this; this.children.push(c); return c; }
      insertBefore(c, ref) { c.parentNode = this; const i = this.children.indexOf(ref); this.children.splice(i < 0 ? this.children.length : i, 0, c); return c; }
      replaceChildren() { this.children = []; }
      addEventListener(t, f) { (this.listeners[t] ||= []).push(f); }
      removeEventListener(t, f) { this.listeners[t] = (this.listeners[t] || []).filter((g) => g !== f); }
      dispatch(t, event = {}) { event.target ||= this; event.preventDefault ||= () => { event.prevented = true; }; for (const f of this.listeners[t] || []) f(event); return event; }
    }
    globalThis.document = { createElement: (tag) => new Node(tag) };
    const { attachCombobox } = await import(MODULE_URI);
    const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
    const form = new Node("form");
    const input = new Node("input"); input.name = "ticker"; input.form = form;
    form.appendChild(input);
    const queries = []; const selected = [];
    const fetch = async (q) => { queries.push(q); return q === "zzz" ? [] : [
      { ticker: "600519.SH", name: "贵州茅台", status: "listed", in_universe: true, on_watchlist: false },
      { ticker: "600518.SH", name: "ST康美", status: "delisted", in_universe: false, on_watchlist: true },
    ]; };
    attachCombobox(input, { fetch, onSelect: (item) => selected.push(item.ticker) });
    const host = form.children[0];
    const list = host.children[1];
    const out = { hostClass: host.attrs.class, inputRole: input.attrs.role, listRole: list.attrs.role, controls: input.attrs["aria-controls"] === list.attrs.id, hiddenAtStart: list.hidden };
    input.value = "g"; input.dispatch("input");
    input.value = "gz"; input.dispatch("input");
    await sleep(80);
    out.noQueryBeforeDebounce = queries.length;
    await sleep(220);
    out.queries = [...queries];
    out.options = list.children.map((li) => ({ role: li.attrs.role, index: li.attrs["data-index"], text: li.children.map((c) => `${c.attrs.class}:${c.textContent}`) }));
    out.expanded = input.attrs["aria-expanded"];
    const down = input.dispatch("keydown", { key: "ArrowDown" });
    out.activeAfterDown = [list.children[0].classes.has("is-active"), list.children[0].attrs["aria-selected"], input.attrs["aria-activedescendant"], down.prevented];
    input.dispatch("keydown", { key: "ArrowDown" });
    input.dispatch("keydown", { key: "ArrowUp" });
    const enter = input.dispatch("keydown", { key: "Enter" });
    out.afterEnter = { value: input.value, selected: [...selected], hidden: list.hidden, prevented: enter.prevented, expanded: input.attrs["aria-expanded"] };
    input.value = "zzz"; input.dispatch("input"); await sleep(300);
    out.empty = list.children.map((li) => [li.attrs.class, li.textContent]);
    const enterEmpty = input.dispatch("keydown", { key: "Enter" });
    out.enterOnEmptyPrevented = Boolean(enterEmpty.prevented);
    input.dispatch("keydown", { key: "Escape" });
    out.hiddenAfterEscape = list.hidden;
    input.value = ""; input.dispatch("input"); await sleep(300);
    out.queriesAfterBlank = queries.length;
    console.log(JSON.stringify(out));
    """
    result = subprocess.run(
        [node, "--input-type=module", "-e", script], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out["hostClass"] == "combo" and out["inputRole"] == "combobox" and out["listRole"] == "listbox"
    assert out["controls"] is True and out["hiddenAtStart"] is True
    assert out["noQueryBeforeDebounce"] == 0  # debounced: nothing fired at 80 ms
    assert out["queries"] == ["gz"]  # only the settled value was searched
    assert out["expanded"] == "true"
    assert [o["role"] for o in out["options"]] == ["option", "option"]
    assert out["options"][0]["text"] == ["combo-ticker:600519.SH", "combo-name:贵州茅台", "combo-tag:目标池"]
    assert out["options"][1]["text"] == ["combo-ticker:600518.SH", "combo-name:ST康美", "combo-tag combo-tag-warn:退市", "combo-tag:自选"]
    assert out["activeAfterDown"] == [True, "true", "ticker-1-listbox-0", True]
    assert out["afterEnter"] == {"value": "600519.SH", "selected": ["600519.SH"], "hidden": True, "prevented": True, "expanded": "false"}
    assert out["empty"] == [["combo-empty", "无匹配；可直接输入代码。"]]
    assert out["enterOnEmptyPrevented"] is False  # a plain Enter still submits the form
    assert out["hiddenAfterEscape"] is True
    assert out["queriesAfterBlank"] == 2  # "gz", "zzz"; a blank value never searches


@pytest.mark.parametrize(
    "module",
    ["api.js", "app.js", "dom.js", "format.js", "research_format.js", "ui.js", "candidates.js", "health.js", "settings.js", "combobox.js"],
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
