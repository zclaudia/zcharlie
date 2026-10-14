"""Browser API client contract tests (work package B3).

The server-rendered pages declare data regions the ES-module client fills;
these tests pin that contract (region ids, endpoints, four-state hooks) and
keep the shipped JS modules parseable.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

WEB_STATIC = Path("src/investment_agent/web/static")

# 供 node 驱动测试使用的最小 DOM 替身：只覆盖被测模块用到的能力
# （createElement / 属性 / 子树 / 监听器 / dataset），绝不实现 innerHTML。
FAKE_DOM = """
class FakeNode {
  constructor(tag) {
    this.tag = tag; this.attrs = {}; this.children = []; this.listeners = {};
    this.parentNode = null; this.textContent = undefined; this.value = "";
    this.hidden = false; this.disabled = false; this.checked = false; this.id = "";
    this.dataset = {}; this.classes = new Set(); this._qs = {};
  }
  get className() { return [...this.classes].join(" "); }
  set className(v) { this.classes = new Set(String(v).split(/\\s+/).filter(Boolean)); }
  get classList() { const c = this.classes; return { add: (x) => c.add(x), remove: (x) => c.delete(x), contains: (x) => c.has(x) }; }
  setAttribute(k, v) { this.attrs[k] = String(v); if (k === "class") this.className = String(v); if (k === "id") this.id = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  hasAttribute(k) { return k in this.attrs; }
  removeAttribute(k) { delete this.attrs[k]; }
  appendChild(c) { c.parentNode = this; this.children.push(c); return c; }
  append(...nodes) { for (const n of nodes) { if (n instanceof FakeNode) this.appendChild(n); else this.children.push(String(n)); } }
  replaceChildren(...nodes) { this.children = []; for (const n of nodes) this.appendChild(n); }
  insertBefore(c, ref) { c.parentNode = this; const i = this.children.indexOf(ref); this.children.splice(i < 0 ? this.children.length : i, 0, c); return c; }
  remove() { if (this.parentNode) this.parentNode.children = this.parentNode.children.filter((c) => c !== this); }
  after(node) { if (this.parentNode) this.parentNode.children.splice(this.parentNode.children.indexOf(this) + 1, 0, node); }
  closest() { return null; }
  scrollIntoView() {}
  addEventListener(t, f) { (this.listeners[t] ||= []).push(f); }
  dispatch(t, event = {}) { event.target ||= this; event.preventDefault ||= () => {}; for (const f of this.listeners[t] || []) f(event); return event; }
  click() { if (this.disabled) return; this.dispatch("click"); }
  querySelector(sel) { if (!this._qs[sel]) { this._qs[sel] = new FakeNode(sel); this.appendChild(this._qs[sel]); } return this._qs[sel]; }
  querySelectorAll() { return []; }
  walk(fn) { for (const c of this.children) { if (c instanceof FakeNode) { fn(c); c.walk(fn); } } }
  text() { const out = []; this.walk((n) => { if (typeof n.textContent === "string") out.push(n.textContent); }); return out.join("\\n"); }
}
"""


def _run_node(script: str) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available")
    result = subprocess.run(
        [node, "--input-type=module", "-e", script], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


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
    year_input = next(line for line in html.splitlines() if 'name="report_year"' in line)
    assert 'value=' not in year_input
    assert 'placeholder="年报年度"' in year_input


def test_portfolio_page_declares_ledger_regions(api_client):
    _login(api_client)
    html = api_client.get("/app/portfolio").text
    assert 'id="portfolio-account"' in html
    assert 'id="portfolio-holdings"' in html
    assert 'id="portfolio-events"' in html
    assert 'id="portfolio-fact-form"' in html
    assert 'id="portfolio-create-form"' in html
    for option in ("opening_balance", "reconciliation", "trade", "cash_transfer", "dividend", "corporate_action"):
        assert f'value="{option}"' in html
    assert "卖出" in html  # the user's own recorded facts, never an order
    # WP5: the correction panel and the ratio-change fields are on the page.
    assert 'id="portfolio-correction-submit"' in html
    assert 'id="portfolio-correction-reason"' in html
    assert 'id="portfolio-fact-ratio"' in html
    # WP7: the display close-out regions (realized P&L, dividends, archive).
    assert 'id="portfolio-realized"' in html
    assert 'id="portfolio-archive"' in html
    script = api_client.get("/app/static/portfolio.js").text
    assert "/portfolio/corrections" in script
    assert "corporate_action" in script
    assert "selectedEvents" in script
    assert "orderByRecentActivity" in script
    assert "realizedTable" in script and "archiveList" in script


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
    app = (WEB_STATIC / "app.js").read_text(encoding="utf-8")
    watchlist = (WEB_STATIC / "watchlist.js").read_text(encoding="utf-8")
    # Dashboard and per-stock history endpoints come from the templates'
    # data-endpoint attributes (see page contract tests above); the renderers
    # themselves only reference these endpoints directly.
    assert "/research/runs" in app
    for endpoint in ("/watchlist", "/watchlist/refresh"):
        assert endpoint in watchlist, endpoint


def test_sparkline_component_and_mounts():
    """ui.js owns the SVG sparkline; watchlist.js and candidates.js feed it from /market/."""

    ui = (WEB_STATIC / "ui.js").read_text(encoding="utf-8")
    assert "export function sparkline" in ui
    assert "export async function fillSparklines" in ui
    for marker in ('viewBox: `0 0 ${SPARK_W} ${SPARK_H}`', 'preserveAspectRatio: "none"', "non-scaling-stroke", "spark-band", "spark-last", "spark-empty"):
        assert marker in ui, marker
    for module in ("watchlist.js", "candidates.js"):
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
    for module in ("watchlist.js", "candidates.js"):
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

    // IME Enter must accept Chinese text, without selecting a stock.
    input.value = "山东"; input.dispatch("compositionstart"); input.dispatch("input");
    await sleep(300);
    out.noSearchDuringComposition = queries.length === 2 && list.hidden;
    const imeEnter = input.dispatch("keydown", { key: "Enter", isComposing: true });
    out.imeEnter = { prevented: Boolean(imeEnter.prevented), value: input.value, selected: selected.length };
    input.dispatch("compositionend"); await sleep(300);
    out.searchedCommittedChinese = queries.at(-1) === "山东" && !list.hidden;
    input.dispatch("keydown", { key: "Escape" });
    input.dispatch("blur"); input.value = "山东黄"; input.dispatch("input"); await sleep(300);
    out.searchAfterQuickRefocus = queries.at(-1) === "山东黄" && !list.hidden;

    // A dismissed, superseded, or destroyed search cannot reopen its list.
    const raceInput = new Node("input"); raceInput.name = "race"; form.appendChild(raceInput);
    const pending = [];
    const raceCombo = attachCombobox(raceInput, { fetch: () => new Promise((resolve, reject) => pending.push({resolve, reject})) });
    const raceList = raceInput.parentNode.children[1];
    raceInput.value = "山东"; raceInput.dispatch("keydown", {key:"ArrowDown"});
    raceCombo.close(); pending.shift().resolve([{ticker:"600547.SH",name:"山东黄金"}]); await sleep(0);
    out.dismissedSearchStaysClosed = raceList.hidden;
    raceInput.dispatch("keydown", {key:"ArrowDown"});
    raceInput.value = "600"; raceInput.dispatch("input");
    pending.shift().reject(new Error("old search failed")); await sleep(0);
    out.oldErrorStaysClosed = raceList.hidden;
    raceCombo.close();
    raceInput.dispatch("keydown", {key:"ArrowDown"});
    raceCombo.destroy(); pending.shift().resolve([{ticker:"600547.SH",name:"山东黄金"}]); await sleep(0);
    out.destroyedSearchStaysClosed = raceList.hidden;
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
    assert out["noSearchDuringComposition"] is True
    assert out["imeEnter"] == {"prevented": False, "value": "山东", "selected": 1}
    assert out["searchedCommittedChinese"] is True
    assert out["searchAfterQuickRefocus"] is True
    assert out["dismissedSearchStaysClosed"] is True
    assert out["oldErrorStaysClosed"] is True
    assert out["destroyedSearchStaysClosed"] is True


def test_watchlist_metadata_autofill_ignores_superseded_requests():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available")
    source = (WEB_STATIC / "watchlist.js").read_text(encoding="utf-8")
    source = source[source.index("function wireWatchlistForm("):source.index("function wireRefreshAll(")]
    script = "import assert from 'node:assert/strict';\n" + """
    class Field {
      constructor() { this.value = ''; this.placeholder = ''; this.title = ''; this.disabled = false; this.listeners = {}; }
      addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
      fire(type, event = {}) { for (const fn of this.listeners[type] || []) fn(event); }
    }
    const fields = Object.fromEntries(['ticker','name','industry','report_year'].map(key => [key, new Field()]));
    const submit = new Field(), form = new Field();
    form.querySelector = selector => selector.startsWith('button') ? submit : fields[/name="(.*?)"/.exec(selector)[1]];
    let select;
    const requests = [];
    const attachCombobox = (input, opts) => { select = opts.onSelect; };
    const get = path => new Promise((resolve, reject) => requests.push({path, resolve, reject}));
    const document = {querySelector: () => null};
    """ + source + """
    wireWatchlistForm(form);
    const metadataSelect = select;
    select = item => metadataSelect({report_year:2025, ...item});
    const gold = {ticker:'600547.SH', name:'山东黄金', industry:'有色金属', report_year:2025, disclosed_on:'2026-03-30'};
    fields.ticker.value = gold.ticker; await select(gold);
    assert.equal(fields.name.value, gold.name);
    assert.equal(fields.industry.value, gold.industry);
    assert.equal(fields.report_year.value, '2025');
    assert.ok(fields.report_year.title.includes('2026-03-30'));
    assert.equal(requests.length, 0);
    fields.ticker.value = '600350.SH';
    let first = select({ticker:'600350.SH', name:'山东高速'});
    assert.equal(fields.industry.value, '');
    assert.equal(submit.disabled, true);
    form.fire('submit', {preventDefault() {}}); // Pending industry lookup blocks submission.
    fields.ticker.value = gold.ticker; await select(gold);
    requests.shift().resolve({industry:'道路运输业'}); await first;
    assert.equal(fields.industry.value, gold.industry); // Old answer cannot overwrite the new stock.
    assert.equal(submit.disabled, false);
    fields.ticker.value = '600350.SH'; first = select({ticker:'600350.SH', name:'山东高速'});
    fields.industry.value = '交通运输';
    requests.shift().resolve({industry:'道路运输业'}); await first;
    assert.equal(fields.industry.value, '交通运输'); // Keep manual edits made during lookup.
    first = select({ticker:'600350.SH', name:'山东高速'});
    form.fire('reset');
    requests.shift().resolve({industry:'道路运输业'}); await first;
    assert.equal(fields.industry.value, '');
    assert.equal(submit.disabled, false);
    first = select({ticker:'600350.SH', name:'山东高速'});
    fields.ticker.value = '山东'; fields.ticker.fire('input');
    requests.shift().reject(new Error('old lookup failed')); await first;
    assert.equal(fields.industry.value, '');
    assert.equal(submit.disabled, false);

    // An unknown year is looked up by stock and replaces any previous value.
    fields.ticker.value = gold.ticker;
    first = select({...gold, report_year:null});
    assert.equal(fields.report_year.value, '');
    assert.equal(submit.disabled, true);
    assert.equal(requests[0].path, '/stocks/600547.SH/report-year');
    requests.shift().resolve({report_year:2024, disclosed_on:'2025-03-29'}); await first;
    assert.equal(fields.report_year.value, '2024');
    assert.ok(fields.report_year.title.includes('2025-03-29'));
    assert.equal(submit.disabled, false);

    // A late annual-report response must not overwrite another selected stock.
    first = select({...gold, report_year:null});
    fields.ticker.value = '600350.SH';
    await select({ticker:'600350.SH', industry:'交通运输', report_year:2025});
    requests.shift().resolve({report_year:2024}); await first;
    assert.equal(fields.report_year.value, '2025');
    assert.equal(submit.disabled, false);

    // Keep an explicit year edit, and never guess when disclosure is unavailable.
    fields.ticker.value = gold.ticker;
    first = select({...gold, report_year:null});
    fields.report_year.value = '2023';
    requests.shift().resolve({report_year:2024}); await first;
    assert.equal(fields.report_year.value, '2023');
    first = select({...gold, report_year:null});
    requests.shift().resolve({report_year:null}); await first;
    assert.equal(fields.report_year.value, '');
    first = select({...gold, report_year:null});
    requests.shift().reject(new Error('disclosure unavailable')); await first;
    assert.equal(fields.report_year.value, '');
    assert.equal(submit.disabled, false);

    // Both lookups must finish before submission is enabled.
    first = select({...gold, industry:null, report_year:null});
    assert.equal(requests.length, 2);
    requests.shift().resolve({industry:'有色金属'});
    await Promise.resolve();
    assert.equal(submit.disabled, true);
    requests.shift().resolve({report_year:2025}); await first;
    assert.equal(submit.disabled, false);
    first = select({...gold, report_year:null});
    form.fire('reset');
    requests.shift().resolve({report_year:2025}); await first;
    assert.equal(fields.report_year.value, '');
    assert.equal(submit.disabled, false);
    first = select({...gold, report_year:null});
    fields.ticker.value = '山东'; fields.ticker.fire('input');
    requests.shift().resolve({report_year:2025}); await first;
    assert.equal(fields.report_year.value, '');
    """
    result = subprocess.run([node, "--input-type=module", "-e", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "module",
    ["api.js", "app.js", "dom.js", "financials.js", "format.js", "research_format.js", "ui.js", "candidates.js", "health.js", "settings.js", "combobox.js", "methods.js", "industry_peers.js"],
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


# ---- P1-8：422 校验拒绝必须渲染人话文案，而不是 DOM 节点 ---------------------


def test_portfolio_fact_error_renders_readable_text_not_a_dom_node():
    """422 时 renderRegion 的 error 分支只接受字符串；把 errorText() 的
    <details> 节点传进去会显示成 "[object HTMLDetailsElement]"。"""

    uri = (WEB_STATIC / "portfolio.js").resolve().as_uri()
    script = FAKE_DOM + f"""
const URI = {json.dumps(uri)};
import assert from 'node:assert/strict';
const hosts = new Map();
const host = (sel) => {{
  if (!hosts.has(sel)) hosts.set(sel, new FakeNode(sel));
  return hosts.get(sel);
}};
globalThis.document = {{
  createElement: (tag) => new FakeNode(tag),
  querySelector: (sel) => host(sel),
  getElementById: (id) => host('#' + id),
  querySelectorAll: () => [],
}};
const store = new Map();
globalThis.sessionStorage = {{
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => store.set(k, String(v)),
  removeItem: (k) => store.delete(k),
}};
const respond = (status, body) => ({{
  status, ok: status >= 200 && status < 300,
  headers: {{ get: () => "application/json" }},
  json: async () => body, text: async () => JSON.stringify(body),
}});
const calls = [];
globalThis.fetch = async (path, init = {{}}) => {{
  calls.push(({{method: init.method || "GET"}}).method + " " + path);
  if (path === "/portfolio/accounts") return respond(200, []);
  if (init.method === "POST" && path === "/portfolio/events")
    return respond(422, {{detail: {{
      message: "rule validation failed",
      issues: [{{code: "sell_quantity_insufficient", path: "holding.quantity", message: "确定数量不足，无法完成卖出"}}],
    }}}});
  throw new Error("unexpected fetch " + path);
}};
store.set("zcharlie.portfolio.pending.v1", JSON.stringify({{
  path: "/portfolio/events", method: "POST",
  request: {{account_id: "acc-1", idempotency_key: "k", fact: {{kind: "trade"}}}},
}}));
const m = await import(URI);
m.bootPortfolio();
await new Promise((r) => setTimeout(r, 20));
const retry = host("#portfolio-pending-retry");
assert.equal(retry.hidden, false);
retry.click();
await new Promise((r) => setTimeout(r, 20));
const factResult = host("#portfolio-fact-result");
const text = factResult.text();
console.log(JSON.stringify({{
  calls, state: factResult.dataset.state, text,
  hasObjectNode: text.includes("[object"),
}}));
"""
    out = _run_node(script)
    assert out["calls"] == ["GET /portfolio/accounts", "POST /portfolio/events"]
    assert out["state"] == "error"
    # 422 的规则校验明细必须可见，且不能出现 "[object ...]"。
    assert "确定数量不足，无法完成卖出" in out["text"]
    assert "holding.quantity" in out["text"]
    assert out["hasObjectNode"] is False


# ---- P2-13：近 24h 快速路径必须重挂行内规则列 -------------------------------


def test_portfolio_recent_activity_fast_path_rerenders_valuation():
    source = (WEB_STATIC / "portfolio.js").read_text(encoding="utf-8")
    # mountHoldingRules 只在 renderValuation 里调用：快速路径若绕过它，规则列永远"加载中…"。
    assert "renderValuation(holdingsRegion, lastValuation, lastListings)" in source
    assert 'renderRegion(holdingsRegion, "ready", holdingsTable(' not in source
    assert "mountHoldingRules(region, valuation)" in source


# ---- P2-14：观察池启用/停用与移除断网时必须渲染错误态 -----------------------


def test_watchlist_toggle_and_remove_render_error_when_offline():
    """两个操作改走 api.js 封装：请求被拒绝（断网）时 ApiError 落到错误态，
    而不是 unhandled rejection 无任何提示。"""

    uri = (WEB_STATIC / "watchlist.js").resolve().as_uri()
    script = FAKE_DOM + f"""
const URI = {json.dumps(uri)};
import assert from 'node:assert/strict';
const region = new FakeNode('#watchlist-list');
region.dataset.endpoint = '/watchlist';
globalThis.document = {{
  createElement: (tag) => new FakeNode(tag),
  querySelector: (sel) => (sel === '#watchlist-list' ? region : null),
}};
globalThis.prompt = () => '数据质量理由：已退市';
let offline = false;
const item = {{listing_id: 7, ticker: '600519.SH', name: '贵州茅台', enabled: true}};
const respond = (status, body) => ({{
  status, ok: status >= 200 && status < 300,
  headers: {{ get: () => 'application/json' }},
  json: async () => body, text: async () => JSON.stringify(body),
}});
globalThis.fetch = async (path, init = {{}}) => {{
  if (offline) throw new TypeError('network down');
  if (path === '/watchlist' && (init.method || 'GET') === 'GET') return respond(200, [item]);
  throw new Error('unexpected fetch ' + path);
}};
const m = await import(URI);
m.bootWatchlist();
await new Promise((r) => setTimeout(r, 20));
let toggle, remove;
region.walk((n) => {{
  if (n.tag === 'button' && n.textContent === '停用') toggle = n;
  if (n.tag === 'button' && n.textContent === '移除') remove = n;
}});
assert.ok(toggle && remove, '列表必须渲染启用/停用与移除按钮');
offline = true;
toggle.click();
await new Promise((r) => setTimeout(r, 20));
const afterToggle = {{state: region.dataset.state, text: region.text()}};
remove.click();
await new Promise((r) => setTimeout(r, 20));
const afterRemove = {{state: region.dataset.state, text: region.text()}};
console.log(JSON.stringify({{afterToggle, afterRemove}}));
"""
    out = _run_node(script)
    for phase in (out["afterToggle"], out["afterRemove"]):
        assert phase["state"] == "error"
        assert "网络请求失败或超时" in phase["text"]
        assert "[object" not in phase["text"]


# ---- P2-15：时间一律按北京时间渲染，不允许原样输出 UTC ISO 串 ----------------


@pytest.mark.parametrize(
    ("module", "present", "absent"),
    [
        ("agent.js", ["截止 ${formatDateTime(value.waiting.deadline)}"], ["截止 ${value.waiting.deadline}"]),
        ("agent_controls.js", ["formatDateTime(rec.created_at)"], ["${rec.created_at}"]),
        (
            "agent_portfolio.js",
            ["formatDateTime(snapshot.fact_as_of)", "formatDateTime(snapshot.valuation_at)", "formatDateTime(plan.price_basis_at)", "formatDateTime(plan.valid_until)"],
            ["${snapshot.fact_as_of}", "${snapshot.valuation_at}", "${plan.price_basis_at}", "${plan.valid_until}"],
        ),
        ("methods.js", ["formatDateTime(record.created_at)"], ["${record.created_at}"]),
        (
            "agent_views.js",
            ["dateLabel(decision.decided_at)", "dateLabel(entry.created_at)", "dateLabel(entry.as_of)", "dateLabel(episode.created_at)", "dateLabel(cycle.checked_at"],
            ["${decision.decided_at}", "发布于 ${entry.created_at}", "${cycle.checked_at || cycle.started_at ||", "· ${episode.created_at}"],
        ),
    ],
)
def test_utc_timestamps_render_through_beijing_formatters(module, present, absent):
    source = (WEB_STATIC / module).read_text(encoding="utf-8")
    for needle in present:
        assert needle in source, (module, needle)
    for needle in absent:
        assert needle not in source, (module, needle)


def test_agent_views_delegates_dates_to_format_js():
    """agent_views 不再自带 Intl 本地实现：dateLabel 统一来自 format.js。"""

    source = (WEB_STATIC / "agent_views.js").read_text(encoding="utf-8")
    assert "from './format.js'" in source
    assert "formatDateTime as dateLabel" in source
    assert "Intl.DateTimeFormat" not in source


# ---- A6：methods 页回归共享客户端；P3 健壮性断言保留 ------------------------


def test_methods_delegates_to_the_shared_api_client():
    """A6：methods.js 不再自带 HTTP 客户端，请求与错误分类统一由 api.js 承载。

    现在由 api.js 保证：15s 超时中断、X-Requested-With / Idempotency-Key、
    先按文本读取再安全解析 JSON（容忍网关 HTML/空体）、401/403/409/422 分类。
    本页只保证改用共享客户端，并把 ApiError 翻译回原有用户文案；
    请求形状与文案的回归由下面的行为测试驱动真实模块验证。
    """

    source = (WEB_STATIC / "methods.js").read_text(encoding="utf-8")
    assert re.search(
        r"import\s*\{[^}]*\bApiError\b[^}]*\bget\b[^}]*\bpost\b[^}]*\}\s*from\s*['\"]\./api\.js['\"]",
        source,
    ), "methods.js 必须从 ./api.js 引入共享客户端"
    # 本地不再有第二套客户端：无 fetch、超时控制器、响应手工解析或 401 分支。
    for marker in ("AbortController", "TIMEOUT_MS", "response.text()", "JSON.parse(text)", "fetch("):
        assert marker not in source, marker
    # 旧实现的用户文案在调用点翻译保留（断网/超时、401、对象 detail 的 message、降级）。
    for copy in ("'网络请求失败或超时'", "'登录已过期'", "detail.message", "`请求失败 ${error.status}`"):
        assert copy in source, copy


def test_methods_requests_go_through_the_shared_client_with_legacy_copy():
    """用假 DOM + 假 fetch 驱动真实 methods.js：URL/方法/请求头/凭据与
    409/422 分类、401 提示、断网文案逐项复现旧实现。"""

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available")
        return
    uri = (WEB_STATIC / "methods.js").resolve().as_uri()
    script = FAKE_DOM + f"""
const URI = {json.dumps(uri)};
const hosts = new Map();
const host = (id) => {{ if (!hosts.has(id)) hosts.set(id, new FakeNode('#' + id)); return hosts.get(id); }};
globalThis.document = {{
  createElement: (tag) => new FakeNode(tag),
  getElementById: (id) => host('#' + id),
  querySelector: () => null,
  querySelectorAll: () => [],
}};
const store = new Map();
globalThis.sessionStorage = {{
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => store.set(k, String(v)),
  removeItem: (k) => store.delete(k),
}};
const calls = [];
let scenario = 'ok';
const respond = (status, body) => ({{
  status, ok: status >= 200 && status < 300,
  headers: {{ get: () => 'application/json' }},
  json: async () => body, text: async () => JSON.stringify(body),
}});
const boot = {{proposals: [], heads: [], reviews: [], completed_tasks: [{{id: 'task-1', status: 'succeeded'}}],
  versions: [], baseline_method: {{}}, scope_names: {{}}, page: {{truncated: false, next_offset: null}}}};
globalThis.fetch = async (path, init = {{}}) => {{
  const method = init.method || 'GET';
  calls.push({{method, path, headers: init.headers || {{}}, body: init.body, credentials: init.credentials}});
  if (method === 'POST' && scenario === 'network') throw new TypeError('network down');
  if (method === 'POST' && scenario === 'unauthorized') return respond(401, {{detail: 'Authentication required'}});
  if (method === 'POST' && scenario === 'unprocessable') return respond(422, {{detail: {{message: '规则校验失败', issues: []}}}});
  if (method === 'POST') return respond(200, {{id: 'ok-1', status: 'created'}});
  return respond(200, boot);
}};
await import(URI);
await new Promise((r) => setTimeout(r, 20));
host('#method-task').value = 'task-1';
const click = async () => {{ host('#method-review').onclick(); await new Promise((r) => setTimeout(r, 30)); }};
const first = {{...calls[0]}};
scenario = 'unprocessable'; await click();
const after422 = {{status: host('#method-status').textContent, pending: store.has('investor.method.pending.v1'),
  gets: calls.filter((c) => c.method === 'GET').length}};
scenario = 'unauthorized'; await click();
const after401 = {{status: host('#method-status').textContent, pending: store.has('investor.method.pending.v1')}};
scenario = 'network'; await click();
const afterOffline = {{status: host('#method-status').textContent}};
scenario = 'ok'; await click();
const lastPost = calls.filter((c) => c.method === 'POST').at(-1);
console.log(JSON.stringify({{
  first, after422, after401, afterOffline, lastPost,
  postCount: calls.filter((c) => c.method === 'POST').length,
  finalStatus: host('#method-status').textContent,
  pendingAfterSuccess: store.has('investor.method.pending.v1'),
}}));
"""
    out = _run_node(script)
    # GET 经共享客户端：同一 URL、同一方法、同源凭据、fetch 标记头。
    assert out["first"]["method"] == "GET"
    assert out["first"]["path"] == "/investor/methods?offset=0"
    assert out["first"]["credentials"] == "same-origin"
    assert out["first"]["headers"]["X-Requested-With"] == "fetch"
    # POST：路径 / 方法 / body 与旧实现逐项一致，并带上共享客户端的幂等键。
    last = out["lastPost"]
    assert last["method"] == "POST"
    assert last["path"] == "/investor/methods/reviews/task-1"
    assert last["body"] == "{}"
    assert last["headers"]["Content-Type"] == "application/json"
    assert last["headers"]["X-Requested-With"] == "fetch"
    assert last["headers"]["Idempotency-Key"]
    # 422：对象 detail 的 message 仍直接展示；待确认内容清除并重载列表（旧行为）。
    assert out["after422"]["status"] == "规则校验失败。原操作内容已保留，可重试。"
    assert out["after422"]["pending"] is False
    assert out["after422"]["gets"] == 2  # 首次加载 + 422 后的重载
    # 401：文案与旧实现一致，待确认内容保留可重试。
    assert out["after401"]["status"] == "登录已过期。原操作内容已保留，可重试。"
    assert out["after401"]["pending"] is True
    # 断网：可读文案 + 保留待确认内容，不再是未处理拒绝。
    assert out["afterOffline"]["status"] == "网络请求失败或超时。原操作内容已保留，可重试。"
    # 成功：4 个 POST（422/401/断网/成功各一），保存后清除待确认内容并重载。
    assert out["postCount"] == 4
    assert out["finalStatus"] == "已保存。提案创建不会自动启用方法。"
    assert out["pendingAfterSuccess"] is False


# ---- B7：methods 页区分超时中断与真实网络失败 --------------------------------


def test_methods_distinguishes_timeout_abort_from_plain_network_failure():
    """B7：api.js 在 catch 里用 controller.signal.aborted 给 ApiError 附加
    aborted 标记（status/message/retryable/detail 均不变）；methods 页据此把
    超时中断翻译回旧文案『请求超时』，真实断网仍是『网络请求失败或超时』。
    同时断言 api.js 的 error.message 两种情况都保持通用文案——其它页面直接
    渲染 message，不能回退。"""

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available")
        return
    api_uri = (WEB_STATIC / "api.js").resolve().as_uri()
    methods_uri = (WEB_STATIC / "methods.js").resolve().as_uri()
    script = FAKE_DOM + f"""
const API_URI = {json.dumps(api_uri)};
const METHODS_URI = {json.dumps(methods_uri)};
// 只截获 api.js 的 15s 超时定时器（>=1s）：测试里手动触发以模拟真实超时中断；
// 测试自身的短延时定时器照常走真实实现。
const realSetTimeout = globalThis.setTimeout;
const realClearTimeout = globalThis.clearTimeout;
const longTimers = [];
globalThis.setTimeout = (fn, ms, ...rest) => {{
  if (typeof ms === 'number' && ms >= 1000) {{ const handle = {{fn, cleared: false}}; longTimers.push(handle); return handle; }}
  return realSetTimeout(fn, ms, ...rest);
}};
globalThis.clearTimeout = (handle) => {{
  if (handle && typeof handle === 'object' && 'cleared' in handle) {{ handle.cleared = true; return; }}
  realClearTimeout(handle);
}};
const fireTimeout = () => {{
  const handle = longTimers.filter((t) => !t.cleared).at(-1);
  if (!handle) throw new Error('no pending long timer');
  handle.fn();  // 15s 到期 → controller.abort()
}};
const sleep = (ms) => new Promise((resolve) => realSetTimeout(resolve, ms));
// 'timeout'：fetch 挂起，等 controller 中断后按 abort 语义拒绝（AbortError）；
// 'network'：直接抛 TypeError（真实断网）；其余返回 JSON。
let mode = 'ok';
globalThis.fetch = (path, init = {{}}) => {{
  if (mode === 'timeout') {{
    return new Promise((_resolve, reject) => {{
      init.signal.addEventListener('abort', () => reject(Object.assign(new Error('This operation was aborted'), {{name: 'AbortError'}})));
    }});
  }}
  if (mode === 'network') return Promise.reject(new TypeError('network down'));
  const boot = {{proposals: [], heads: [], reviews: [], completed_tasks: [{{id: 'task-1', status: 'succeeded'}}],
    versions: [], baseline_method: {{}}, scope_names: {{}}, page: {{truncated: false, next_offset: null}}}};
  return Promise.resolve({{status: 200, ok: true, headers: {{get: () => 'application/json'}},
    json: async () => boot, text: async () => JSON.stringify(boot)}});
}};
const snapshot = (error) => ({{name: error.name, status: error.status, retryable: error.retryable,
  aborted: error.aborted, message: error.message, isApiError: error instanceof api.ApiError}});

// 1) api.js 直接驱动：真实网络失败 vs 15s 定时器中断，message 必须都不变。
const api = await import(API_URI);
mode = 'network';
const apiNetwork = snapshot(await api.get('/probe').then(() => null, (error) => error));
mode = 'timeout';
const pendingTimeout = api.get('/probe').then(() => null, (error) => error);
fireTimeout();
const apiTimeout = snapshot(await pendingTimeout);

// 2) 真实 methods.js：点击评审按钮走 POST，同一页面下对比两种失败文案。
const hosts = new Map();
const host = (id) => {{ if (!hosts.has(id)) hosts.set(id, new FakeNode('#' + id)); return hosts.get(id); }};
globalThis.document = {{
  createElement: (tag) => new FakeNode(tag),
  getElementById: (id) => host('#' + id),
  querySelector: () => null,
  querySelectorAll: () => [],
}};
const store = new Map();
globalThis.sessionStorage = {{
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => store.set(k, String(v)),
  removeItem: (k) => store.delete(k),
}};
mode = 'ok';
await import(METHODS_URI);
await sleep(20);
host('#method-task').value = 'task-1';
mode = 'timeout';
host('#method-review').onclick();
await sleep(20);
fireTimeout();
await sleep(20);
const methodsTimeout = host('#method-status').textContent;
mode = 'network';
host('#method-review').onclick();
await sleep(30);
const methodsNetwork = host('#method-status').textContent;
console.log(JSON.stringify({{apiTimeout, apiNetwork, methodsTimeout, methodsNetwork}}));
"""
    out = _run_node(script)
    # api.js 自身：两种情况的 message/status/retryable 完全保持不变（其它页面直接渲染）。
    for case in ("apiTimeout", "apiNetwork"):
        probe = out[case]
        assert probe["isApiError"] is True
        assert probe["name"] == "ApiError"
        assert probe["status"] == 0
        assert probe["retryable"] is True
        assert probe["message"] == "网络请求失败或超时，请重试。"
    # 只有 abort（controller 被中断）才带上 aborted 标记。
    assert out["apiNetwork"]["aborted"] is False
    assert out["apiTimeout"]["aborted"] is True
    # methods 页文案：超时中断→旧文案『请求超时』；真实断网→『网络请求失败或超时』。
    assert out["methodsTimeout"] == "请求超时。原操作内容已保留，可重试。"
    assert out["methodsNetwork"] == "网络请求失败或超时。原操作内容已保留，可重试。"


def test_methods_guards_empty_evaluations_and_async_handlers():
    source = (WEB_STATIC / "methods.js").read_text(encoding="utf-8")
    # async onclick 包 catch 并更新 status()（详情/历史/加载三条路径，失败不静默）。
    assert source.count("catch (error) { status(") >= 3
    # evaluations / evaluation_ids 判空后再启用按钮，空时禁用并提示。
    assert "const latestEvaluationId = p.evaluation_ids.at(-1) || null;" in source
    assert "if (!latestEvaluationId) shadow.disabled = true;" in source
    assert "if (!latestEvaluationId) prospective.disabled = true;" in source


def test_methods_module_links_with_a_stub_document():
    """methods.js 是 ES module（IIFE 立即执行）：document 缺席时必须安静退出。"""

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available")
    script = (
        "globalThis.document = {getElementById: () => null};\n"
        f"await import({json.dumps((WEB_STATIC / 'methods.js').resolve().as_uri())});\n"
        "console.log('linked');\n"
    )
    result = subprocess.run([node, "--input-type=module", "-e", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "linked" in result.stdout


def test_agent_portfolio_refresh_has_no_dead_append():
    """summary 的"账本写入关闭"提示在 replaceChildren() 之后只 append 一次。"""

    source = (WEB_STATIC / "agent_portfolio.js").read_text(encoding="utf-8")
    assert source.count("账本写入关闭：请在设置页明确启用真实组合账本") == 1


# ---- P3：文档不再固定漂移事实 -----------------------------------------------


def test_docs_do_not_pin_drifting_facts():
    readme = Path("README.md").read_text(encoding="utf-8")
    assert "迁移随版本演进" in readme
    assert not re.search(r"Alembic（迁移 `\d+`）", readme)
    ag30 = (Path("docs/product") / "ag30-method-evolution.md").read_text(encoding="utf-8")
    # 代码口径是 2,000,000 字符（methods.py 的 2_000_000），不是 2 MiB。
    assert "大于 2,000,000 字符的原始产物拒绝内联" in ag30
    assert "2 MiB" not in ag30
