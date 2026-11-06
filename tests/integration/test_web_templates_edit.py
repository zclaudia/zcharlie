"""Templates pages (config-docs-plan §4.6 WP4): page shells, the CD6 drift
semantics of the list page, and the detail-page 编辑新版本 flow driven through
the real templates.js with a fake DOM + fake fetch (the data-sources pattern).
"""

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


def _login(client: TestClient) -> None:
    response = client.post("/app/login", data={"api_key": "secret", "next": "/app"})
    assert response.status_code == 200


def _run_node(script: str) -> Any:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available")
    result = subprocess.run(
        [node, "--input-type=module", "-e", script], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


# ---- page shells ---------------------------------------------------------------


def test_templates_page_copy_mentions_the_db_authoring_seat(api_client: TestClient) -> None:
    _login(api_client)
    html = api_client.get("/app/templates").text
    for fragment in (
        'id="templates-list"',
        "只作首次导入基线",
        "起草新版本",
        static_url("pages/templates.css"),
    ):
        assert fragment in html, fragment
    assert " style=" not in html
    assert "<style" not in html


def test_template_detail_page_shell(api_client: TestClient) -> None:
    _login(api_client)
    html = api_client.get("/app/templates/quality_growth").text
    assert 'id="template-detail"' in html
    assert 'data-template-id="quality_growth"' in html
    assert "<noscript>" in html
    assert " style=" not in html


def test_app_boots_the_templates_module() -> None:
    source = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "bootTemplates()" in source
    module = (STATIC / "templates.js").read_text(encoding="utf-8")
    for endpoint in (
        "/versions",
        "/activate",
        "/baseline-import",
        "/validate",
        "/config-docs/industry_groups",
    ):
        assert endpoint in module, endpoint


# ---- fake-DOM flows -------------------------------------------------------------

DETAIL = {
    "id": "cand_test",
    "version": 2,
    "latest_version": 2,
    "ref": "cand_test@2",
    "name": "Candidate test",
    "description": "second",
    "thesis": "原策略说明",
    "calibration_status": "uncalibrated",
    "applicable_groups": ["consumer", "banking"],
    "screen_condition_count": 2,
    "param_count": 1,
    "version_count": 2,
    "synced_at": "2026-11-01T01:02:03Z",
    "open_candidates": 0,
    "file_status": "baseline_changed",
    "baseline_version": 1,
    "factor_units": {"roe": "fraction"},
    "definition": {
        "id": "cand_test",
        "name": "Candidate test",
        "description": "second",
        "applicable_groups": ["consumer", "banking"],
        "thesis": "原策略说明",
        "falsification": "证伪线",
        "calibration_status": "uncalibrated",
        "screen": {
            "all": [
                {"factor": "roe", "op": ">=", "value": "$roe_min", "label": "竞争优势"},
                {"factor": "is_st", "op": "==", "value": 0},
            ]
        },
        "candidate_zones": {
            "buy_zone": {"all": [{"factor": "roe", "op": ">=", "value": 0.1}]},
            "drop": {"any": [{"factor": "roe", "op": "<", "value": 0.05}]},
        },
        "holding_zones": {
            "add": {"all": [{"factor": "position_weight", "op": "<", "value": 0.1}]},
            "trim": {"any": [{"factor": "gain_vs_cost", "op": ">", "value": 0.5}]},
            "exit_review": {"any": [{"factor": "roe", "op": "<", "value": 0.05}]},
        },
        "params": {"roe_min": 0.15},
        "param_ranges": {"roe_min": {"min": 0.08, "max": 0.3}},
    },
    "versions": [
        {
            "version": 2,
            "hash": "b" * 64,
            "calibration_status": "uncalibrated",
            "created_at": "2026-11-01T01:02:03Z",
            "created_by": "web",
            "note": "第二轮起草",
        },
        {
            "version": 1,
            "hash": "a" * 64,
            "calibration_status": "uncalibrated",
            "created_at": "2026-10-01T01:02:03Z",
            "created_by": None,
            "note": None,
        },
    ],
}

GROUPS_DOC = {
    "kind": "industry_groups",
    "value": {"version": "1", "groups": {"banking": "银行", "consumer": "消费", "other": "其他"}},
}


def test_detail_page_edit_flow_validates_saves_and_reloads() -> None:
    from tests.integration.test_web_client_contract import FAKE_DOM

    uri = (STATIC / "templates.js").resolve().as_uri()
    script = FAKE_DOM + f"""
const URI = {json.dumps(uri)};
const DETAIL = {json.dumps(DETAIL, ensure_ascii=False)};
const GROUPS = {json.dumps(GROUPS_DOC, ensure_ascii=False)};
class FocusNode extends FakeNode {{ focus() {{}} }}
const hosts = new Map();
const host = (id) => {{ if (!hosts.has(id)) hosts.set(id, new FocusNode('#' + id)); return hosts.get(id); }};
const region = host('template-detail');
region.dataset.templateId = 'cand_test';
globalThis.window = {{ location: {{ search: '' }} }};
globalThis.document = {{
  body: null,
  title: '',
  createElement: (tag) => new FocusNode(tag),
  createTextNode: (text) => Object.assign(new FakeNode('#text'), {{ textContent: text }}),
  getElementById: (id) => host(id),
  querySelector: (sel) => (sel === '#template-detail' ? region : sel === '#template-title' ? host('template-title') : null),
  querySelectorAll: () => [],
}};
const calls = [];
let detail = structuredClone(DETAIL);
let validateIssues = null;  // null = ok
const respond = (status, body) => ({{
  status, ok: status >= 200 && status < 300,
  headers: {{ get: () => 'application/json' }},
  json: async () => body, text: async () => JSON.stringify(body),
}});
globalThis.fetch = async (path, init = {{}}) => {{
  const method = init.method || 'GET';
  calls.push({{ method, path, body: init.body ? JSON.parse(init.body) : null }});
  if (method === 'GET' && path === '/rule-templates/cand_test') return respond(200, detail);
  if (method === 'GET' && path === '/config-docs/industry_groups') return respond(200, GROUPS);
  if (method === 'POST' && path === '/rule-templates/cand_test/validate') {{
    if (validateIssues) return respond(200, {{ ok: false, issues: validateIssues }});
    return respond(200, {{ ok: true, issues: [] }});
  }}
  if (method === 'POST' && path === '/rule-templates/cand_test/versions') {{
    detail = structuredClone(detail);
    detail.version = detail.latest_version = 3;
    detail.versions.unshift({{ version: 3, hash: 'c'.repeat(64), calibration_status: 'uncalibrated',
      created_at: '2026-11-02T00:00:00Z', created_by: 'web', note: calls.at(-1).body.note }});
    detail.version_count = 3;
    return respond(200, {{ action: 'new_version', version: 3, hash: 'c'.repeat(64), created_by: 'web',
      note: calls.at(-1).body.note, detail }});
  }}
  throw new Error('unexpected ' + method + ' ' + path);
}};
const m = await import(URI);
m.bootTemplates();
await new Promise((r) => setTimeout(r, 20));
const walk = (node) => {{ const out = []; for (const c of node.children) if (c instanceof FakeNode) out.push(...walk(c), c); return out; }};
const all = () => walk(region);
const byLabel = (label) => all().find((n) => n.getAttribute('aria-label') === label);
const byText = (text) => all().filter((n) => typeof n.textContent === 'string' && n.textContent.includes(text));
const tick = () => new Promise((r) => setTimeout(r, 10));
const out = {{}};
// 结构化字段来自当前版本；版本表带操作人/备注；漂移横幅 + 从基线导入；激活条存在。
out.nameField = Boolean(byLabel('cand_test 策略说明'));
out.thesisValue = byLabel('cand_test 策略说明').value;
out.screenYaml = byLabel('cand_test 筛选条件 YAML').value;
out.versionMeta = byText('第二轮起草').length > 0 && byText('文件基线').length > 0;
out.driftNotice = byText('基线文件与规则库当前版本不一致').length > 0;
out.baselineHint = byText('v1 一致').length > 0;
out.importButton = byText('从基线导入').length > 0;
out.activateBar = byText('激活此版本（回滚）').length > 0;
out.groupsFromDb = await Promise.resolve();  // 分组多选在文档读取后填充
await tick();
out.groupOptionBanking = all().some((n) => typeof n.textContent === 'string' && n.textContent.includes('银行'));
// 校验：服务端返回逐字段问题（applicable_groups 不在 DB 映射）。
validateIssues = [{{ code: 'unknown_industry_group', path: 'applicable_groups[1]', message: "'fintech' is not an industry group" }}];
byText('校验').filter((n) => n.tag === 'button')[0].click();
await tick();
out.validateCall = calls.find((c) => c.path.endsWith('/validate'));
out.groupIssueShown = byText('unknown_industry_group').length > 0;
// 保存未填备注被拦截，不发 POST。
validateIssues = null;
byText('保存为新版本').filter((n) => n.tag === 'button')[0].click();
await tick();
out.noteRequired = byText('变更说明必填').length > 0;
out.versionsBeforeNote = calls.filter((c) => c.path.endsWith('/versions')).length;
// 与当前版本对比：改策略说明后出现差异行。
byLabel('cand_test 策略说明').value = '新的策略说明';
byText('与当前版本对比').filter((n) => n.tag === 'button')[0].click();
await tick();
out.diffShown = byText('策略说明').some((n) => n.textContent.includes('新的策略说明'));
// 填备注保存：POST versions 的 document 带新字段与 screen 解析结果。
byLabel('cand_test 筛选条件 YAML').value = 'all:\\n  - factor: roe\\n    op: ">="\\n    value: $roe_min\\n    label: 竞争优势\\n  - factor: is_st\\n    op: "=="\\n    value: 0\\n';
byLabel('cand_test 变更说明').value = '调整策略说明';
byText('保存为新版本').filter((n) => n.tag === 'button')[0].click();
await tick();
const saved = calls.filter((c) => c.path.endsWith('/versions')).at(-1);
out.saved = {{ note: saved.body.note, thesis: saved.body.document.thesis,
  screen: saved.body.document.screen, groups: saved.body.document.applicable_groups }};
// 保存成功后重新加载详情（第二次 GET）。
out.getsAfterSave = calls.filter((c) => c.method === 'GET' && c.path === '/rule-templates/cand_test').length;
console.log(JSON.stringify(out));
"""
    out = _run_node(script)
    assert out["nameField"] is True
    assert out["thesisValue"] == "原策略说明"
    assert "$roe_min" in out["screenYaml"]
    assert out["versionMeta"] is True
    assert out["driftNotice"] is True
    assert out["baselineHint"] is True
    assert out["importButton"] is True
    assert out["activateBar"] is True
    assert out["groupOptionBanking"] is True
    # The validate call posts the structured document.
    assert out["validateCall"]["body"]["document"]["thesis"] == "原策略说明"
    assert out["groupIssueShown"] is True
    assert out["noteRequired"] is True
    assert out["versionsBeforeNote"] == 0
    assert out["diffShown"] is True
    assert out["saved"] == {
        "note": "调整策略说明",
        "thesis": "新的策略说明",
        "screen": DETAIL["definition"]["screen"],
        "groups": ["banking", "consumer"],
    }
    assert out["getsAfterSave"] == 2


def test_list_page_shows_drift_and_imports_from_baseline() -> None:
    from tests.integration.test_web_client_contract import FAKE_DOM

    uri = (STATIC / "templates.js").resolve().as_uri()
    script = FAKE_DOM + f"""
const URI = {json.dumps(uri)};
class FocusNode extends FakeNode {{ focus() {{}} }}
const hosts = new Map();
const host = (id) => {{ if (!hosts.has(id)) hosts.set(id, new FocusNode('#' + id)); return hosts.get(id); }};
const region = host('templates-list');
globalThis.window = {{ location: {{ search: '' }} }};
globalThis.document = {{
  body: null,
  createElement: (tag) => new FocusNode(tag),
  createTextNode: (text) => Object.assign(new FakeNode('#text'), {{ textContent: text }}),
  getElementById: (id) => host(id),
  querySelector: (sel) => (sel.startsWith('#templates-') ? host(sel.slice(1)) : null),
  querySelectorAll: () => [],
}};
const drifted = [
  {{ id: 'quality_growth', version: 3, ref: 'quality_growth@3', name: 'Quality growth',
     description: null, thesis: '说明', calibration_status: 'uncalibrated',
     applicable_groups: ['consumer'], screen_condition_count: 4, param_count: 2,
     version_count: 3, synced_at: '2026-11-01T01:02:03Z', open_candidates: 0,
     file_status: 'baseline_changed', baseline_version: 1 }},
  {{ id: 'dividend_blue_chip', version: 1, ref: 'dividend_blue_chip@1', name: 'Dividend',
     description: null, thesis: '说明', calibration_status: 'uncalibrated',
     applicable_groups: ['utility'], screen_condition_count: 3, param_count: 2,
     version_count: 1, synced_at: '2026-11-01T01:02:03Z', open_candidates: 0,
     file_status: 'synced', baseline_version: 1 }},
];
let items = structuredClone(drifted);
const calls = [];
const respond = (status, body) => ({{
  status, ok: status >= 200 && status < 300,
  headers: {{ get: () => 'application/json' }},
  json: async () => body, text: async () => JSON.stringify(body),
}});
globalThis.fetch = async (path, init = {{}}) => {{
  const method = init.method || 'GET';
  calls.push({{ method, path, body: init.body ? JSON.parse(init.body) : null }});
  if (method === 'GET' && path === '/rule-templates') return respond(200, items);
  if (method === 'POST' && path === '/rule-templates/quality_growth/baseline-import') {{
    items = structuredClone(drifted);
    items[0].file_status = 'synced';
    items[0].version = 4;
    return respond(200, {{ action: 'new_version', version: 4 }});
  }}
  throw new Error('unexpected ' + method + ' ' + path);
}};
const m = await import(URI);
m.bootTemplates();
await new Promise((r) => setTimeout(r, 20));
const walk = (node) => {{ const out = []; for (const c of node.children) if (c instanceof FakeNode) out.push(...walk(c), c); return out; }};
const all = () => walk(region);
const byText = (text) => all().filter((n) => typeof n.textContent === 'string' && n.textContent.includes(text));
const byLabel = (label) => all().find((n) => n.getAttribute('aria-label') === label);
const tick = () => new Promise((r) => setTimeout(r, 10));
const out = {{}};
out.driftBadge = byText('基线漂移').length > 0;
out.whichHint = byText('v1 一致').length > 0;
out.noticeCounts = host('templates-notice-counts').textContent;
out.noteInput = Boolean(byLabel('quality_growth 从基线导入说明'));
// 未填说明：不发出请求。
byText('从基线导入').filter((n) => n.tag === 'button')[0].click();
await tick();
out.importsBeforeNote = calls.filter((c) => c.method === 'POST').length;
out.warnShown = byText('请先填写导入说明').length > 0;
// 填说明导入成功 → 列表重新加载，漂移消失。
byLabel('quality_growth 从基线导入说明').value = '同步仓库基线';
byText('从基线导入').filter((n) => n.tag === 'button')[0].click();
await tick();
const imported = calls.find((c) => c.method === 'POST');
out.importCall = {{ path: imported.path, note: imported.body.note }};
out.getsAfterImport = calls.filter((c) => c.method === 'GET').length;
out.driftGone = byText('基线漂移').length === 0;
console.log(JSON.stringify(out));
"""
    out = _run_node(script)
    assert out["driftBadge"] is True
    assert out["whichHint"] is True
    assert out["noticeCounts"] == "2 个模板未校准 · 1 个模板基线漂移"
    assert out["noteInput"] is True
    assert out["importsBeforeNote"] == 0
    assert out["warnShown"] is True
    assert out["importCall"] == {
        "path": "/rule-templates/quality_growth/baseline-import",
        "note": "同步仓库基线",
    }
    assert out["getsAfterImport"] == 2
    assert out["driftGone"] is True
