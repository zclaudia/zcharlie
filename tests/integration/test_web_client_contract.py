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
