"""Data-sources page (config-docs-plan §4.6, Tab A): shell, auth, nav and the
pure helpers of data-sources.js (grouping, derived provider options, the
document diff and the restricted YAML subset of the advanced editor)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from investment_agent.web.static_assets import static_url

STATIC = Path("src/investment_agent/web/static")
DISCLAIMER = "研究辅助，非投资建议"

DOCUMENT = {
    "version": "2026-10-30.1",
    "default_tolerance": 0.02,
    "fields": {
        "roe": {
            "table": "financial",
            "primary": "baostock",
            "definition": "ROE on average equity (fraction)",
            "key": True,
            "calibration": {
                "baostock": {"checked_on": "2026-10-06", "method": "live: query_profit_data"}
            },
        },
        "close": {
            "table": "market",
            "primary": "baostock",
            "fallback": ["tencent"],
            "tolerance": 0.001,
            "definition": "close, CNY/share",
        },
        "debt_ratio": {
            "table": "financial",
            "primary": "baostock",
            "fallback": ["akshare"],
            "definition": "liabilities / assets (fraction)",
            "sanity": {"baostock": {"min": 0.02, "reason": "scaled by 1/100 on some rows"}},
            "approx": {"akshare": True},
        },
        "eps_basic_fy": {
            "table": "derived",
            "primary": "derived",
            "formula": "net_profit_parent / total_shares",
            "definition": "approximates basic EPS CNY/share",
        },
        "report_overdue": {
            "table": "report_overdue",
            "primary": "baostock",
            "definition": "flag: 1 when overdue",
        },
    },
}


def _login(client: TestClient) -> None:
    response = client.post("/app/login", data={"api_key": "secret", "next": "/app"})
    assert response.status_code == 200


def _run_data_sources(script: str) -> Any:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available")
    uri = (STATIC / "data-sources.js").resolve().as_uri()
    source = f"const s = await import({json.dumps(uri)});\n{script}"
    result = subprocess.run(
        [node, "--input-type=module", "-e", source], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _run_node(script: str) -> Any:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available")
    result = subprocess.run(
        [node, "--input-type=module", "-e", script], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_data_sources_page_requires_session(api_client: TestClient) -> None:
    response = api_client.get("/app/data-sources", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/app/login"


def test_data_sources_page_shell(api_client: TestClient) -> None:
    _login(api_client)
    response = api_client.get("/app/data-sources")
    assert response.status_code == 200
    html = response.text
    for fragment in (
        'id="ds-form"',
        'data-endpoint="/config-docs/field_sources"',
        'id="ds-tabs"',
        "字段来源优先级",
        'id="ds-drift"',
        'id="ds-meta"',
        'id="ds-history"',
        'data-endpoint="/config-docs/field_sources/history?limit=50"',
        'id="ds-baseline-drawer"',
        'id="ds-baseline-diff"',
        'id="ds-baseline-import"',
        'id="ds-baseline-note"',
        'id="ds-advanced"',
        'id="ds-yaml"',
        'id="ds-savebar"',
        'id="ds-dirty-count"',
        'id="ds-save"',
        'id="ds-discard"',
        'id="ds-note"',
        'id="ds-save-result"',
        'href="/app/data-sources" class="active" aria-current="page"',
        'data-state="loading"',
        "<noscript>",
        DISCLAIMER,
        static_url("app.js"),
        static_url("pages/data-sources.css"),
    ):
        assert fragment in html, fragment
    # 变更说明必填（CD7：全部写操作带备注）。
    note_line = next(line for line in html.splitlines() if 'id="ds-note"' in line)
    assert "required" in note_line
    # CSP 基线：页面框架不写内联 style / <style>，脚本全部外链。
    assert " style=" not in html
    assert "<style" not in html
    for chunk in html.split("<script")[1:]:
        assert "src=" in chunk.split(">")[0]


def test_nav_links_data_sources_on_every_page(api_client: TestClient) -> None:
    _login(api_client)
    for path in ("/app", "/app/watchlist", "/app/runs", "/app/candidates", "/app/health", "/app/settings"):
        html = api_client.get(path).text
        assert 'href="/app/data-sources"' in html, path
        assert ">数据源<" in html, path


def test_app_boots_the_data_sources_module() -> None:
    source = (STATIC / "app.js").read_text(encoding="utf-8")
    assert 'from "./data-sources.js"' in source
    assert "bootDataSources()" in source and "DATA_SOURCE_REGIONS" in source
    module = (STATIC / "data-sources.js").read_text(encoding="utf-8")
    for endpoint in (
        "/config-docs/field_sources",
        "/config-docs/field_sources/history",
        "/config-docs/field_sources/baseline-import",
    ):
        assert endpoint in module, endpoint


def test_data_sources_grouping_and_provider_options() -> None:
    out = _run_data_sources(
        """
const doc = {
  version: "1",
  fields: {
    roe: {table: "financial", primary: "baostock"},
    close: {table: "market", primary: "baostock", fallback: ["tencent"]},
    eps: {table: "derived", primary: "derived"},
    overdue: {table: "report_overdue", primary: "baostock"},
    cash: {table: "dividend", primary: "baostock"},
    seg: {table: "earnings", primary: "akshare"},
    weird: {primary: "baostock"},
  },
};
console.log(JSON.stringify({
  groups: s.tableGroups(doc).map((g) => [g.table, g.label]),
  options: {
    financial: s.providerOptions(doc, "financial"),
    market: s.providerOptions(doc, "market"),
    derived: s.providerOptions(doc, "derived"),
    earnings: s.providerOptions(doc, "earnings"),
    empty: s.providerOptions(doc, "nope"),
  },
}));
"""
    )
    assert out["groups"] == [
        ["financial", "财务"],
        ["derived", "派生"],
        ["market", "行情"],
        ["report_overdue", "披露逾期"],
        ["dividend", "分红"],
        ["earnings", "业绩事件"],
        ["unknown", "unknown"],
    ]
    # 选项集从文档推导：market 自然是 baostock / tencent，不硬编码枚举。
    assert out["options"] == {
        "financial": ["baostock"],
        "market": ["baostock", "tencent"],
        "derived": ["derived"],
        "earnings": ["akshare"],
        "empty": [],
    }


def test_data_sources_document_diff() -> None:
    out = _run_data_sources(
        f"""
const doc = {json.dumps(DOCUMENT, ensure_ascii=False)};
const next = structuredClone(doc);
next.version = "2026-11-01.1";
next.fields.close.primary = "tencent";
next.fields.close.fallback = [];
delete next.fields.close.tolerance;
next.fields.roe.key = false;
delete next.fields.report_overdue;
next.fields.cash = {{table: "dividend", primary: "baostock", definition: "CNY/share"}};
console.log(JSON.stringify({{
  entries: s.diffDocuments(doc, next).map(s.diffEntryText),
  same: [s.sameDocument(doc, structuredClone(doc)),
         s.sameDocument({{a: 1, b: 2}}, {{b: 2, a: 1}}),
         s.sameDocument(doc, next)],
  empty: s.diffDocuments(doc, structuredClone(doc)),
}}));
"""
    )
    assert out["entries"] == [
        "版本：2026-10-30.1 → 2026-11-01.1",
        "移除字段 report_overdue（披露逾期）",
        "roe（财务）：关键字段 是 → 否",
        "close（行情）：主来源 baostock → tencent；备用顺序 tencent → （空）；容差 0.001 → （未设置）",
        "新增字段 cash（分红）：主来源 baostock",
    ]
    assert out["same"] == [True, True, False]
    assert out["empty"] == []


def test_yaml_round_trip_of_the_document_shape() -> None:
    out = _run_data_sources(
        f"""
const doc = {json.dumps(DOCUMENT, ensure_ascii=False)};
const text = s.yamlText(doc);
const edited = structuredClone(doc);
edited.fields.roe.calibration.baostock.method = "line one\\nline two: with colon";
edited.fields.close.fallback = [];
const editedText = s.yamlText(edited);
console.log(JSON.stringify({{
  roundTrip: s.parseYamlDocument(text),
  editedRoundTrip: s.parseYamlDocument(editedText),
  text, editedText,
}}));
"""
    )
    assert out["roundTrip"] == {"value": DOCUMENT}
    assert out["editedRoundTrip"] == {"value": {
        **DOCUMENT,
        "fields": {
            **DOCUMENT["fields"],
            "roe": {
                **DOCUMENT["fields"]["roe"],
                "calibration": {
                    "baostock": {
                        "checked_on": "2026-10-06",
                        "method": "line one\nline two: with colon",
                    }
                },
            },
            "close": {**DOCUMENT["fields"]["close"], "fallback": []},
        },
    }}
    text = out["text"]
    # 引号规则：含冒号的值加引号；标量列表逐项一行；数值 / 布尔原样。
    assert 'method: "live: query_profit_data"' in text
    assert "fallback:" in text and "- akshare" in text
    assert "tolerance: 0.001" in text
    assert "key: true" in text
    assert "default_tolerance: 0.02" in text
    # 多行字符串落块标量；空列表保持流式。
    assert "method: |-" in out["editedText"]
    assert "fallback: []" in out["editedText"]


def test_yaml_parses_hand_edits_and_rejects_broken_text() -> None:
    out = _run_data_sources(
        """
const cases = {
  flowList: `fields:\\n  close:\\n    fallback: [tencent, "a, b", 'c']\\n`,
  comments: `# top\\nversion: "1"  # trailing\\nfields:\\n  roe:\\n    primary: baostock\\n`,
  blockScalar: `method: >-\\n  folded one\\n  folded two\\nversion: "1"\\n`,
  nullAndBool: `a:\\nb: true\\nc: FALSE\\nd: null\\ne: 1.5\\nf: 2026-10-06\\n`,
  badIndent: `version: "1"\\n fields:\\n`,
  dupKey: `a: 1\\na: 2\\n`,
  unclosedQuote: `a: "abc\\n`,
  empty: `\\n# only a comment\\n`,
  notMapping: `- a\\n- b\\n`,
};
const out = {};
for (const [name, text] of Object.entries(cases)) {
  const parsed = s.parseYamlDocument(text);
  out[name] = parsed.error ?? parsed.value;
}
console.log(JSON.stringify(out));
"""
    )
    assert out["flowList"]["fields"]["close"]["fallback"] == ["tencent", "a, b", "c"]
    assert out["comments"] == {"version": "1", "fields": {"roe": {"primary": "baostock"}}}
    assert out["blockScalar"] == {"method": "folded one folded two", "version": "1"}
    assert out["nullAndBool"] == {
        "a": None,
        "b": True,
        "c": False,
        "d": None,
        "e": 1.5,
        "f": "2026-10-06",
    }
    for name in ("badIndent", "dupKey", "unclosedQuote", "empty", "notMapping"):
        assert isinstance(out[name], str) and out[name], name


def test_data_sources_boot_flow_edits_save_and_advanced() -> None:
    """用假 DOM + 假 fetch 驱动真实 data-sources.js：加载 → 表单编辑（备用来源、
    必填备注）→ PATCH 全文档带 expected_revision → 高级编辑改版本号保存 → 非法
    YAML 阻止关闭。"""

    from tests.integration.test_web_client_contract import FAKE_DOM

    uri = (STATIC / "data-sources.js").resolve().as_uri()
    script = FAKE_DOM + f"""
const URI = {json.dumps(uri)};
const DOC = {json.dumps(DOCUMENT, ensure_ascii=False)};
class FocusNode extends FakeNode {{ focus() {{}} }}
const hosts = new Map();
const host = (id) => {{ if (!hosts.has(id)) hosts.set(id, new FocusNode('#' + id)); return hosts.get(id); }};
globalThis.document = {{
  body: null,
  createElement: (tag) => new FocusNode(tag),
  createTextNode: (text) => Object.assign(new FakeNode('#text'), {{ textContent: text }}),
  getElementById: (id) => host(id),
  querySelector: () => null,
  querySelectorAll: () => [],
}};
let revision = 7;
const calls = [];
const respond = (status, body) => ({{
  status, ok: status >= 200 && status < 300,
  headers: {{ get: () => 'application/json' }},
  json: async () => body, text: async () => JSON.stringify(body),
}});
const detail = (value, drifted) => ({{
  kind: 'field_sources', title: '字段来源优先级', value, baseline_value: DOC,
  revision, updated_at: '2026-11-01T01:02:03Z', updated_by: 'web', imported_at: null, drifted,
}});
globalThis.fetch = async (path, init = {{}}) => {{
  const method = init.method || 'GET';
  calls.push({{ method, path, body: init.body }});
  if (method === 'GET' && path === '/config-docs/field_sources') return respond(200, detail(DOC, false));
  if (method === 'PATCH') {{
    const body = JSON.parse(init.body);
    if (body.expected_revision !== revision) return respond(409, {{detail: 'stale revision'}});
    revision += 1;
    return respond(200, detail(body.value, true));
  }}
  throw new Error('unexpected ' + method + ' ' + path);
}};
const m = await import(URI);
m.bootDataSources();
await new Promise((r) => setTimeout(r, 20));
const form = host('ds-form');
const walk = (node) => {{ const out = []; for (const c of node.children) if (c instanceof FakeNode) out.push(...walk(c), c); return out; }};
const all = () => walk(form);
const byLabel = (label) => all().find((n) => n.getAttribute('aria-label') === label);
const tick = () => new Promise((r) => setTimeout(r, 10));
const out = {{}};
out.groups = all().filter((n) => n.tag === 'h3').map((n) => n.textContent);
out.meta = host('ds-meta').textContent;
out.driftHidden = host('ds-drift').hidden;
out.dirtyBefore = host('ds-dirty-count').textContent;
// 1) 备用来源：为 roe 添加 akshare（选项 = financial 表出现过的 baostock / akshare）。
const add = byLabel('为 roe 添加备用来源');
add.value = 'akshare';
add.dispatch('change');
out.dirtyAfterAdd = host('ds-dirty-count').textContent;
out.roeFallbackShown = all().some((n) => n.textContent === 'akshare');
// 2) 未填备注 → 保存被拦截，不产生 PATCH。
host('ds-save').click();
await tick();
out.noteRequired = host('ds-save-result').text();
out.patchesBeforeNote = calls.filter((c) => c.method === 'PATCH').length;
// 3) 填备注保存：PATCH 全文档 + expected_revision，成功后按响应刷新。
host('ds-note').value = '调整 roe 备用';
host('ds-save').click();
await tick();
const patch = JSON.parse(calls.find((c) => c.method === 'PATCH').body);
out.patch = {{ note: patch.note, expected_revision: patch.expected_revision, fallback: patch.value.fields.roe.fallback }};
out.savedNotice = host('ds-save-result').text();
out.dirtyAfterSave = host('ds-dirty-count').textContent;
// 4) 高级编辑：打开即工作文档的 YAML；改版本号 → 未保存标记 → 保存解析后的文档。
const advanced = host('ds-advanced');
advanced.open = true;
advanced.dispatch('toggle');
await new Promise((r) => setTimeout(r, 0));
out.formHiddenWhileAdvanced = form.hidden;
out.yamlHasVersion = host('ds-yaml').value.includes('2026-10-30.1');
host('ds-yaml').value = host('ds-yaml').value.replace('2026-10-30.1', '2026-11-02.1');
host('ds-yaml').dispatch('input');
out.dirtyAdvanced = host('ds-dirty-count').textContent;
host('ds-note').value = '版本号推进';
host('ds-save').click();
await tick();
const patches = calls.filter((c) => c.method === 'PATCH');
out.advancedSavedVersion = JSON.parse(patches.at(-1).body).value.version;
out.advancedSavedFallback = JSON.parse(patches.at(-1).body).value.fields.roe.fallback;
// 5) 非法 YAML：编辑器提示错误，关闭被阻止（留在高级模式）。
host('ds-yaml').value = 'fields:\\n  a: "unclosed';
host('ds-yaml').dispatch('input');
advanced.open = false;
advanced.dispatch('toggle');
await new Promise((r) => setTimeout(r, 0));
out.blockedClose = [advanced.open, host('ds-yaml-issues').text()];
console.log(JSON.stringify(out));
"""
    out = _run_node(script)
    assert out["groups"] == ["财务 · 2 个字段", "派生 · 1 个字段", "行情 · 1 个字段", "披露逾期 · 1 个字段"]
    assert "版本 2026-10-30.1" in out["meta"] and "修订 7" in out["meta"]
    assert out["driftHidden"] is True
    assert out["dirtyBefore"] == "没有未保存的修改"
    assert out["dirtyAfterAdd"] == "有未保存的修改"
    assert out["roeFallbackShown"] is True
    assert "变更说明必填" in out["noteRequired"]
    assert out["patchesBeforeNote"] == 0
    assert out["patch"] == {"note": "调整 roe 备用", "expected_revision": 7, "fallback": ["akshare"]}
    assert "已保存" in out["savedNotice"] and "下一个任务" in out["savedNotice"]
    assert out["dirtyAfterSave"] == "没有未保存的修改"
    assert out["formHiddenWhileAdvanced"] is True
    assert out["yamlHasVersion"] is True
    assert out["dirtyAdvanced"] == "有未保存的修改"
    assert out["advancedSavedVersion"] == "2026-11-02.1"
    assert out["advancedSavedFallback"] == ["akshare"]
    assert out["blockedClose"][0] is True
    assert "无法解析" in out["blockedClose"][1]
