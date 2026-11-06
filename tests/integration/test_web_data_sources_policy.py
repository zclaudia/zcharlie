"""Data-sources page Tab B (config-docs-plan §4.6): 来源信任策略 — shell, the
kind-aware diff of data-sources.js, and the boot flow (cards → approve a
licensing row → mandatory note → full-document PATCH with expected_revision →
drift banner, shared history / baseline drawers)."""

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

POLICY = {
    "schema_version": 1,
    "revision": "test-policy-1",
    "sources": {
        "eastmoney.hk.native.v1": {
            "licensing": {"status": "unknown", "company_keys": [], "citation": ""},
            "technical_acceptance": {
                "status": "not_accepted",
                "company_keys": [],
                "adapter_version": "",
                "reports": [],
            },
        },
        "sec.companyfacts.v1": {
            "licensing": {"status": "unknown", "company_keys": [], "citation": ""},
            "technical_acceptance": {
                "status": "not_accepted",
                "company_keys": ["nasdaq-aapl"],
                "adapter_version": "1",
                "reports": [{"observed_on": "2026-10-01", "citation": "https://example.com/r1"}],
            },
        },
    },
}

FIELDS = {
    "version": "test-1",
    "default_tolerance": 0.02,
    "fields": {
        "close": {"table": "market", "primary": "baostock", "definition": "close, CNY/share"},
        "roe": {"table": "financial", "primary": "baostock", "definition": "ROE"},
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


def test_data_sources_page_declares_the_policy_tab(api_client: TestClient) -> None:
    _login(api_client)
    html = api_client.get("/app/data-sources").text
    for fragment in (
        'id="ds-tab-policy"',
        "来源信任策略",
        'id="ds-panel-policy"',
        'id="dsp-form"',
        'data-endpoint="/config-docs/source_policy"',
        'id="dsp-drift"',
        'id="dsp-meta"',
        'id="dsp-savebar"',
        'id="dsp-dirty-count"',
        'id="dsp-save"',
        'id="dsp-discard"',
        'id="dsp-note"',
        'id="dsp-save-result"',
        'id="dsp-history-open"',
        'id="dsp-baseline-open"',
        # 共享抽屉：两个标签各开各的按钮，但只有一个历史 / 基线抽屉。
        'id="ds-history-drawer"',
        'id="ds-baseline-drawer"',
        'id="ds-baseline-import"',
        static_url("pages/data-sources.css"),
    ):
        assert fragment in html, fragment
    note_line = next(line for line in html.splitlines() if 'id="dsp-note"' in line)
    assert "required" in note_line
    # 标签 B 面板初始隐藏，由标签栏切换显示。
    panel_line = next(line for line in html.splitlines() if 'id="ds-panel-policy"' in line)
    assert "hidden" in panel_line
    # CSP 基线保持：无内联 style / <style>，脚本全部外链。
    assert " style=" not in html
    assert "<style" not in html
    for chunk in html.split("<script")[1:]:
        assert "src=" in chunk.split(">")[0]


def test_module_serves_the_policy_endpoints() -> None:
    module = (STATIC / "data-sources.js").read_text(encoding="utf-8")
    for endpoint in (
        "/config-docs/source_policy",
        "/config-docs/source_policy/history",
        "/config-docs/source_policy/baseline-import",
    ):
        assert endpoint in module, endpoint


def test_policy_document_diff_is_kind_aware() -> None:
    out = _run_data_sources(
        f"""
const doc = {json.dumps(POLICY, ensure_ascii=False)};
const next = structuredClone(doc);
next.revision = "test-policy-2";
next.sources["eastmoney.hk.native.v1"].licensing.status = "approved";
next.sources["eastmoney.hk.native.v1"].licensing.company_keys = ["hsbc-holdings"];
next.sources["eastmoney.hk.native.v1"].technical_acceptance.reports = [
  {{observed_on: "2026-10-01", citation: "https://example.com/r1"}},
  {{observed_on: "2026-10-08", citation: "https://example.com/r2"}},
  {{observed_on: "2026-10-15", citation: "https://example.com/r3"}},
];
delete next.sources["sec.companyfacts.v1"];
next.sources["ifind.hk.v1"] = next.sources["eastmoney.hk.native.v1"];
console.log(JSON.stringify({{
  names: s.policySourceNames(doc),
  entries: s.diffDocuments(doc, next, "source_policy").map((e) => s.diffEntryText(e)),
  same: [s.sameDocument(doc, structuredClone(doc)), s.sameDocument(doc, next)],
  empty: s.diffDocuments(doc, structuredClone(doc), "source_policy"),
}}));
"""
    )
    assert out["names"] == ["eastmoney.hk.native.v1", "sec.companyfacts.v1"]
    assert out["entries"] == [
        "修订标识：test-policy-1 → test-policy-2",
        "移除来源 sec.companyfacts.v1",
        (
            "eastmoney.hk.native.v1：许可 状态 unknown → approved；"
            "许可 公司范围 （空） → hsbc-holdings；"
            "技术验收 验收报告 0 条 → 3 条"
        ),
        "新增来源 ifind.hk.v1",
    ]
    assert out["same"] == [True, False]
    assert out["empty"] == []


def test_policy_tab_flow_approves_saves_and_shares_drawers() -> None:
    """假 DOM + 假 fetch 驱动真实 data-sources.js：切到标签 B → 卡片渲染 →
    批准一行许可（状态 / 公司 chip / 引用）→ 空报告行被拦、必填备注被拦 →
    全文档 PATCH 带 expected_revision → 漂移横幅 + 共享历史 / 基线抽屉。"""

    from tests.integration.test_web_client_contract import FAKE_DOM

    uri = (STATIC / "data-sources.js").resolve().as_uri()
    script = (
        FAKE_DOM
        + f"""
const URI = {json.dumps(uri)};
const FIELDS = {json.dumps(FIELDS, ensure_ascii=False)};
const POLICY = {json.dumps(POLICY, ensure_ascii=False)};
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
let revision = 4;
const calls = [];
const respond = (status, body) => ({{
  status, ok: status >= 200 && status < 300,
  headers: {{ get: () => 'application/json' }},
  json: async () => body, text: async () => JSON.stringify(body),
}});
const fieldsDetail = (value, drifted) => ({{
  kind: 'field_sources', title: '字段来源优先级', value, baseline_value: value,
  revision: 1, updated_at: '2026-11-01T01:02:03Z', updated_by: 'web', imported_at: null, drifted,
}});
const policyDetail = (value, drifted) => ({{
  kind: 'source_policy', title: '来源信任策略', value, baseline_value: POLICY,
  revision, updated_at: '2026-11-01T01:02:03Z', updated_by: 'web', imported_at: null, drifted,
}});
globalThis.fetch = async (path, init = {{}}) => {{
  const method = init.method || 'GET';
  calls.push({{ method, path, body: init.body }});
  if (method === 'GET' && path === '/config-docs/field_sources') return respond(200, fieldsDetail(FIELDS, false));
  if (method === 'GET' && path === '/config-docs/source_policy') return respond(200, policyDetail(POLICY, false));
  if (method === 'GET' && path === '/config-docs/source_policy/history?limit=50') {{
    return respond(200, {{items: [{{
      id: 9, old_value: POLICY,
      new_value: structuredClone(POLICY),
      changed_at: '2026-11-01T02:03:04Z', actor: 'web', note: '审批测试',
    }}]}});
  }}
  if (method === 'PATCH' && path === '/config-docs/source_policy') {{
    const body = JSON.parse(init.body);
    if (body.expected_revision !== revision) return respond(409, {{detail: 'stale'}});
    revision += 1;
    return respond(200, policyDetail(body.value, true));
  }}
  if (method === 'POST' && path === '/config-docs/source_policy/baseline-import') {{
    revision += 1;
    return respond(200, policyDetail(POLICY, false));
  }}
  throw new Error('unexpected ' + method + ' ' + path);
}};
const m = await import(URI);
m.bootDataSources();
await new Promise((r) => setTimeout(r, 20));
const out = {{}};
// 1) 切到标签 B：惰性加载 source_policy，出现两张来源卡与策略版本头。
host('ds-tab-policy').click();
await new Promise((r) => setTimeout(r, 20));
const form = host('dsp-form');
const walk = (node) => {{ const found = []; for (const c of node.children) if (c instanceof FakeNode) found.push(...walk(c), c); return found; }};
const all = () => walk(form);
const byLabel = (label) => all().find((n) => n.getAttribute('aria-label') === label);
const tick = () => new Promise((r) => setTimeout(r, 10));
out.policyPanelShown = host('ds-panel-policy').hidden === false;
out.fieldsPanelHidden = host('ds-panel-fields').hidden === true;
out.cards = all().filter((n) => n.tag === 'section').map((n) => n.getAttribute('data-source'));
out.meta = host('dsp-meta').textContent;
out.dirtyBefore = host('dsp-dirty-count').textContent;
// 2) 批准 eastmoney 许可：状态 → approved、公司 chip、引用。
const HK = 'eastmoney.hk.native.v1';
byLabel(HK + ' 许可状态').value = 'approved';
byLabel(HK + ' 许可状态').dispatch('change');
byLabel(HK + ' 许可公司').value = 'hsbc-holdings';
byLabel('添加' + HK + ' 许可公司').click();
byLabel(HK + ' 许可引用').value = 'https://example.com/license';
byLabel(HK + ' 许可引用').dispatch('input');
out.dirtyAfterEdit = host('dsp-dirty-count').textContent;
out.licensingCardDirty = all().some((n) => n.getAttribute('data-source') === HK && n.classes.has('dirty'));
out.secCardClean = all().some((n) => n.getAttribute('data-source') === 'sec.companyfacts.v1' && !n.classes.has('dirty'));
// 3) 空验收报告行：行内预检拦下保存。
byLabel('为 ' + HK + ' 添加验收报告').click();
host('dsp-note').value = '审批 eastmoney';
host('dsp-save').click();
await tick();
out.reportRowBlocked = host('dsp-save-result').text();
// 填好报告行后 4) 未填备注 → 保存被拦，不产生 PATCH。
byLabel(HK + ' 报告 1 观测日期').value = '2026-10-01';
byLabel(HK + ' 报告 1 观测日期').dispatch('change');
byLabel(HK + ' 报告 1 引用').value = 'https://example.com/r1';
byLabel(HK + ' 报告 1 引用').dispatch('input');
host('dsp-note').value = '';
host('dsp-save').click();
await tick();
out.noteRequired = host('dsp-save-result').text();
out.patchesBeforeNote = calls.filter((c) => c.method === 'PATCH').length;
// 5) 填备注保存：全文档 PATCH + expected_revision；成功后按响应（漂移）刷新。
host('dsp-note').value = '审批 eastmoney 港股来源';
host('dsp-save').click();
await tick();
const patch = JSON.parse(calls.find((c) => c.method === 'PATCH').body);
out.patch = {{
  note: patch.note, expected_revision: patch.expected_revision,
  revisionId: patch.value.revision,
  licensing: patch.value.sources[HK].licensing,
  reports: patch.value.sources[HK].technical_acceptance.reports,
  secUntouched: patch.value.sources['sec.companyfacts.v1'].licensing.status,
}};
out.savedNotice = host('dsp-save-result').text();
out.dirtyAfterSave = host('dsp-dirty-count').textContent;
out.driftShown = host('dsp-drift').hidden === false;
// 6) 共享历史抽屉：渲染 source_policy 的历史（kind-aware 差异）。
host('dsp-history-open').click();
await tick();
out.historyText = host('ds-history').text();
// 7) 共享基线抽屉：漂移差异 + 导入说明必填。
host('dsp-baseline-open').click();
host('ds-baseline-import').click();
await tick();
out.baselineNoteRequired = host('dsp-save-result').text();
host('ds-baseline-note').value = '回到基线';
host('ds-baseline-import').click();
await tick();
out.importCalls = calls.filter((c) => c.method === 'POST').map((c) => c.path);
out.afterImport = [host('dsp-drift').hidden, host('dsp-dirty-count').textContent];
console.log(JSON.stringify(out));
"""
    )
    out = _run_node(script)
    assert out["policyPanelShown"] is True
    assert out["fieldsPanelHidden"] is True
    assert out["cards"] == ["eastmoney.hk.native.v1", "sec.companyfacts.v1"]
    assert "策略版本 test-policy-1" in out["meta"] and "修订 4" in out["meta"]
    assert out["dirtyBefore"] == "没有未保存的修改"
    assert out["dirtyAfterEdit"] == "有未保存的修改"
    assert out["licensingCardDirty"] is True
    assert out["secCardClean"] is True
    assert "验收报告第 1 条缺少观测日期" in out["reportRowBlocked"]
    assert "变更说明必填" in out["noteRequired"]
    assert out["patchesBeforeNote"] == 0
    assert out["patch"] == {
        "note": "审批 eastmoney 港股来源",
        "expected_revision": 4,
        "revisionId": "test-policy-1",
        "licensing": {
            "status": "approved",
            "company_keys": ["hsbc-holdings"],
            "citation": "https://example.com/license",
        },
        "reports": [{"observed_on": "2026-10-01", "citation": "https://example.com/r1"}],
        "secUntouched": "unknown",
    }
    assert "已保存" in out["savedNotice"]
    assert out["dirtyAfterSave"] == "没有未保存的修改"
    assert out["driftShown"] is True
    assert "审批测试" in out["historyText"]
    assert "从基线导入需要填写导入说明" in out["baselineNoteRequired"]
    assert out["importCalls"] == ["/config-docs/source_policy/baseline-import"]
    assert out["afterImport"] == [True, "没有未保存的修改"]


def test_policy_tab_conflict_shows_409_refresh() -> None:
    """409（他人已改）给冲突说明 + 拉取最新版本按钮，PATCH 不重复成功。"""

    from tests.integration.test_web_client_contract import FAKE_DOM

    uri = (STATIC / "data-sources.js").resolve().as_uri()
    script = (
        FAKE_DOM
        + f"""
const URI = {json.dumps(uri)};
const FIELDS = {json.dumps(FIELDS, ensure_ascii=False)};
const POLICY = {json.dumps(POLICY, ensure_ascii=False)};
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
const calls = [];
const respond = (status, body) => ({{
  status, ok: status >= 200 && status < 300,
  headers: {{ get: () => 'application/json' }},
  json: async () => body, text: async () => JSON.stringify(body),
}});
const detail = (value) => ({{
  kind: 'source_policy', title: '来源信任策略', value, baseline_value: POLICY,
  revision: 5, updated_at: '2026-11-01T01:02:03Z', updated_by: 'web', imported_at: null, drifted: false,
}});
globalThis.fetch = async (path, init = {{}}) => {{
  const method = init.method || 'GET';
  calls.push({{ method, path }});
  if (method === 'GET' && path === '/config-docs/field_sources') {{
    return respond(200, {{kind: 'field_sources', value: FIELDS, baseline_value: FIELDS,
      revision: 1, updated_at: null, updated_by: null, imported_at: null, drifted: false}});
  }}
  if (method === 'GET' && path === '/config-docs/source_policy') return respond(200, detail(POLICY));
  if (method === 'PATCH') return respond(409, {{detail: 'stale revision'}});
  throw new Error('unexpected ' + method + ' ' + path);
}};
const m = await import(URI);
m.bootDataSources();
await new Promise((r) => setTimeout(r, 20));
host('ds-tab-policy').click();
await new Promise((r) => setTimeout(r, 20));
const walk = (node) => {{ const found = []; for (const c of node.children) if (c instanceof FakeNode) found.push(...walk(c), c); return found; }};
const byLabel = (label) => walk(host('dsp-form')).find((n) => n.getAttribute('aria-label') === label);
const out = {{}};
byLabel('sec.companyfacts.v1 许可状态').value = 'approved';
byLabel('sec.companyfacts.v1 许可状态').dispatch('change');
host('dsp-note').value = '审批 sec';
host('dsp-save').click();
await new Promise((r) => setTimeout(r, 10));
out.conflict = host('dsp-save-result').text();
out.hasRefreshButton = walk(host('dsp-save-result')).some((n) => n.tag === 'button');
out.dirtyStill = host('dsp-dirty-count').textContent;
console.log(JSON.stringify(out));
"""
    )
    out = _run_node(script)
    assert "保存失败" in out["conflict"] and "修订号不一致" in out["conflict"]
    assert out["hasRefreshButton"] is True
    assert out["dirtyStill"] == "有未保存的修改"
